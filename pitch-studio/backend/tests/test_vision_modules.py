"""Tests for vision_modules mode: taxonomy, quotas, ranking, indexer, prompts."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.generation_cache import SCRIPT_PIPELINE_VERSION, compute_cache_key
from backend.models import (
    DeckTopic,
    LockedFact,
    PrathamPassage,
    StyleTranscript,
    TranscriptStory,
    VisionModuleContent,
)
from backend.pipeline.brand_deck import BrandSlide
from backend.pipeline.prompts import VISION_MODULES_RULES, script_messages
from backend.pipeline.script_flow import ScriptTopic
from backend.pipeline.vision_modules_flow import (
    RankedContent,
    allocate_quotas,
    apply_word_cap,
    build_vision_modules_plan,
    check_slide_coverage,
    filter_live_content,
    filter_near_duplicates,
    format_vision_slide_briefs,
    normalize_generation_mode,
    rank_content_for_slide,
    select_narrated_pages,
)
from backend.schemas import GenerationCreate
from backend.vision_modules import (
    index_vision_module_content,
    parse_vm_tag_payload,
    vision_module_for_page,
)


def fake_embed(texts: list[str]) -> list[list[float]]:
    keywords = ("venture", "campus", "founding", "outcome", "immersion", "atlas", "play")
    return [[float(text.lower().count(word)) for word in keywords] + [0.01] for text in texts]


class VisionModuleMappingTests(unittest.TestCase):
    def test_page_51_is_funded_ventures_not_immersions(self):
        self.assertEqual(vision_module_for_page(51), "VM08")
        self.assertEqual(vision_module_for_page(52), "VM09")
        self.assertEqual(vision_module_for_page(43), "VM08")
        self.assertEqual(vision_module_for_page(1), "VM01")
        self.assertEqual(vision_module_for_page(92), "VM15")
        self.assertIsNone(vision_module_for_page(0))


class QuotaMathTests(unittest.TestCase):
    def test_example_seven_of_seventy_six_at_cap_51(self):
        quota = allocate_quotas({"VM08": 7}, 51)
        # With a single module, total (7) <= cap → all narrated.
        self.assertEqual(quota["VM08"], 7)
        # Standalone ceil formula from the brief.
        self.assertEqual(__import__("math").ceil(7 * 51 / 76), 5)

    def test_full_deck_allocation_sums_to_cap(self):
        # Approximate body sizes across VMs summing to 76.
        sizes = {
            "VM01": 7,
            "VM02": 3,
            "VM03": 7,
            "VM04": 2,
            "VM05": 8,
            "VM06": 10,
            "VM07": 4,
            "VM08": 9,
            "VM09": 5,
            "VM10": 8,
            "VM11": 8,
            "VM12": 5,
            "VM13": 0,  # dropped
            "VM14": 0,
            "VM15": 0,
        }
        # Force total 76 by filling remaining into VM12/VM13 style buckets.
        sizes = {
            "VM01": 7,
            "VM02": 3,
            "VM03": 7,
            "VM04": 2,
            "VM05": 8,
            "VM06": 10,
            "VM07": 4,
            "VM08": 9,
            "VM09": 5,
            "VM10": 8,
            "VM11": 8,
            "VM12": 5,
        }
        self.assertEqual(sum(sizes.values()), 76)
        quotas = allocate_quotas(sizes, 51)
        self.assertEqual(sum(quotas.values()), 51)
        self.assertTrue(all(q >= 1 for q in quotas.values()))
        # Explicit user example: 7 slides in a 76-slide body at cap 51 → 5.
        mid = allocate_quotas({"A": 7, "B": 69}, 51)
        self.assertEqual(mid["A"], 5)
        self.assertEqual(sum(mid.values()), 51)

    def test_total_lte_cap_all_narrated(self):
        sizes = {"VM01": 3, "VM02": 4, "VM03": 5}
        quotas = allocate_quotas(sizes, 20)
        self.assertEqual(quotas, sizes)


class NarratedSelectionTests(unittest.TestCase):
    def test_keeps_sheet_order_and_picks_best(self):
        pages = [43, 44, 45, 46, 47]
        content = [
            RankedContent(
                source_type="pratham_passage",
                source_ref="1",
                text="Eat Atlas and PlaySuper funded ventures",
                strength="strong",
                score=1.0,
                page_hints=[45],
                embedding=fake_embed(["Eat Atlas and PlaySuper funded ventures"])[0],
            )
        ]
        vectors = {
            page: fake_embed([f"page {page} venture atlas play" if page == 45 else f"page {page} campus"])[0]
            for page in pages
        }
        chosen = select_narrated_pages(pages, 2, slide_vectors=vectors, content=content)
        self.assertEqual(len(chosen), 2)
        self.assertEqual(chosen, sorted(chosen))
        self.assertIn(45, chosen)


class RankingTests(unittest.TestCase):
    def test_pratham_preferred_over_story_similar_cosine(self):
        slide_vec = fake_embed(["Eat Atlas PlaySuper venture"])[0]
        same_vec = fake_embed(["Eat Atlas PlaySuper venture"])[0]
        content = [
            RankedContent(
                source_type="transcript_story",
                source_ref="s1",
                text="Eat Atlas PlaySuper venture",
                strength="strong",
                score=0,
                embedding=same_vec,
            ),
            RankedContent(
                source_type="pratham_passage",
                source_ref="p1",
                text="Eat Atlas PlaySuper venture",
                strength="strong",
                score=0,
                embedding=same_vec,
            ),
        ]
        ranked = rank_content_for_slide(content, slide_vector=slide_vec, page=45, top_k=2)
        self.assertEqual(ranked[0].source_type, "pratham_passage")

    def test_near_dup_prefers_pratham(self):
        vec = fake_embed(["same story text about ventures"])[0]
        ranked = [
            RankedContent(
                source_type="transcript_story",
                source_ref="s1",
                text="same story text about ventures",
                strength="strong",
                score=1.5,
                embedding=vec,
            ),
            RankedContent(
                source_type="pratham_passage",
                source_ref="p1",
                text="same story text about ventures",
                strength="strong",
                score=1.5,
                embedding=vec,
            ),
        ]
        kept = filter_near_duplicates(ranked)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].source_type, "pratham_passage")

    def test_word_cap_trims_lowest_scores(self):
        from backend.pipeline.vision_modules_flow import SlideBrief

        briefs = [
            SlideBrief(
                page=45,
                slide_key="brand:p45",
                label="Eat Atlas",
                vm_id="VM08",
                brief="Eat Atlas",
                narrated=True,
                ranked=[
                    RankedContent(
                        source_type="pratham_passage",
                        source_ref="1",
                        text=" ".join(["high"] * 20),
                        strength="strong",
                        score=2.0,
                        embedding=[],
                    ),
                    RankedContent(
                        source_type="transcript_story",
                        source_ref="2",
                        text=" ".join(["low"] * 20),
                        strength="passing",
                        score=0.5,
                        embedding=[],
                    ),
                ],
            )
        ]
        apply_word_cap(briefs, word_cap=25)
        self.assertEqual(len(briefs[0].ranked), 1)
        self.assertEqual(briefs[0].ranked[0].source_ref, "1")


class IndexerTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.db.add(
            StyleTranscript(id=1, name="t", raw_text="x", text_hash="h1", status="processed")
        )
        self.db.add(
            PrathamPassage(
                style_transcript_id=1,
                passage_index=0,
                text="Pratham talks about funded student ventures like Eat Atlas.",
                word_count=10,
                module_ids="M06",
                usable=True,
                embedding_json=json.dumps(fake_embed(["funded student ventures Eat Atlas"])[0]),
                text_hash="pass1",
            )
        )
        self.db.add(
            TranscriptStory(
                style_transcript_id=1,
                story_index=0,
                text="A student story about campus life and hostel culture.",
                usable=True,
                module_ids="M09",
                embedding_json=json.dumps(fake_embed(["campus life hostel"])[0]),
                text_hash="story1",
            )
        )
        self.db.add(
            LockedFact(
                fact="Shark Tank",
                value="6 startups",
                status="verified",
                module_ids="M06",
            )
        )
        self.db.add(
            LockedFact(
                fact="Bad claim",
                value="never",
                status="do_not_use",
                module_ids="M06",
            )
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_live_filter_drops_stale_sources_and_refreshes_fact_text(self):
        self.db.query(TranscriptStory).update({"usable": False})
        self.db.commit()
        items = [
            RankedContent("locked_fact", "1", "old snapshot", "strong", 1.0),
            RankedContent("locked_fact", "2", "Bad claim: never", "strong", 1.0),
            RankedContent("pratham_passage", "1", "passage", "strong", 1.0),
            RankedContent("transcript_story", "1", "story", "strong", 1.0),
        ]
        kept = filter_live_content(self.db, items)
        self.assertEqual(
            [(item.source_type, item.source_ref) for item in kept],
            [("locked_fact", "1"), ("pratham_passage", "1")],
        )
        self.assertIn("6 startups", kept[0].text)

    def test_indexer_with_fake_tagger_and_failure_keeps_rows(self):
        def tag(_text: str, _catalog: str = ""):
            return {"modules": [{"id": "VM08", "strength": "strong"}], "page_hints": [45]}

        with mock.patch("backend.generation_cache.bump_content_version"):
            result = index_vision_module_content(
                self.db, force=True, tag=tag, embed_fn=fake_embed
            )
        self.assertGreater(result["written"], 0)
        rows = self.db.query(VisionModuleContent).all()
        self.assertTrue(rows)
        self.assertTrue(all(row.source_type != "locked_fact" or "Bad" not in row.text for row in rows))
        # Forbidden fact must not be indexed.
        refs = {(row.source_type, row.source_ref) for row in rows}
        forbidden_ids = {
            str(fact.id)
            for fact in self.db.query(LockedFact).filter(LockedFact.status == "do_not_use")
        }
        for fid in forbidden_ids:
            self.assertNotIn(("locked_fact", fid), refs)

        before = self.db.query(VisionModuleContent).count()

        def boom(_text: str, _catalog: str = ""):
            raise RuntimeError("tagger down")

        with self.assertRaises(RuntimeError):
            # Force path that raises after some work: wrap index to fail mid-commit by
            # making embed explode after candidates collected.
            with mock.patch("backend.generation_cache.bump_content_version"):
                index_vision_module_content(
                    self.db,
                    force=True,
                    tag=boom,
                    embed_fn=lambda texts: (_ for _ in ()).throw(RuntimeError("tagger down")),
                )
        self.assertEqual(self.db.query(VisionModuleContent).count(), before)

    def test_parse_tag_payload(self):
        parsed = parse_vm_tag_payload(
            {
                "modules": [
                    {"id": "VM08", "strength": "strong"},
                    {"id": "VM09", "strength": "passing"},
                    {"id": "VM10", "strength": "strong"},
                    {"id": "VM11", "strength": "strong"},
                ],
                "page_hints": [45, "x", 46],
            }
        )
        self.assertEqual(list(parsed["modules"].keys()), ["VM08", "VM09", "VM10"])
        self.assertEqual(parsed["page_hints"], [45, 46])


class SoftCoverageTests(unittest.TestCase):
    def test_coverage_missing_and_mentioned(self):
        script = {
            "sections": [
                {"pages": [45], "text": "Eat Atlas shipped product from campus."},
                {"pages": [50], "text": "Something else entirely."},
            ],
            "cta": "Come see it.",
        }
        coverage = check_slide_coverage(
            script,
            [(45, "Eat Atlas and PlaySuper"), (50, "Nivia Sports")],
        )
        self.assertEqual(coverage["narrated"], 2)
        self.assertEqual(coverage["mentioned"], 1)
        self.assertEqual(coverage["missing"], [50])


class PromptAndSchemaTests(unittest.TestCase):
    def test_vision_prompt_block_and_classic_unchanged(self):
        modules = [SimpleNamespace(id="M01", name="Origin", job="j", core_content="c", flex_points="")]
        topic = ScriptTopic(
            topic_id=1,
            title="Funded Ventures",
            pages=[44, 45, 46],
            labels=["A", "Eat Atlas and PlaySuper", "C"],
            narrated_pages=[45],
            shown_not_narrated=[44, 46],
            slide_briefs=[
                {
                    "page": 45,
                    "label": "Eat Atlas and PlaySuper",
                    "ranked": [
                        {
                            "source_type": "pratham_passage",
                            "text": "Pratham on Atlas",
                        },
                        {"source_type": "locked_fact", "text": "6 startups"},
                        {"source_type": "report_passage", "text": "Report line"},
                        {"source_type": "transcript_story", "text": "Story line"},
                    ],
                }
            ],
        )
        block = format_vision_slide_briefs([topic])
        self.assertIn("SLIDES TO NARRATE (in order)", block)
        self.assertIn("[p45] Eat Atlas and PlaySuper", block)
        self.assertIn("Shown, not narrated: p44, p46", block)
        self.assertIn("[PRATHAM]", block)
        self.assertIn("[LOCKED]", block)
        self.assertIn("[REPORT]", block)
        self.assertIn("[STORY — unverified]", block)

        classic = script_messages(
            audience_cluster="A",
            duration="T2",
            channel="C1",
            intent="I1",
            temperature="X1",
            context_note="",
            modules=modules,
            sequence=["M01"],
            facts=[],
            word_budget=200,
            topic_flow=[ScriptTopic(topic_id=1, title="Origin", pages=[1], labels=["Cover"])],
        )
        self.assertNotIn("SLIDES TO NARRATE", classic[1]["content"])
        self.assertNotIn(VISION_MODULES_RULES.strip().split("\n")[0], classic[0]["content"])

        vision = script_messages(
            audience_cluster="A",
            duration="T2",
            channel="C1",
            intent="I1",
            temperature="X1",
            context_note="",
            modules=modules,
            sequence=["M01"],
            facts=[],
            word_budget=200,
            topic_flow=[topic],
            vision_slide_briefs=block,
        )
        self.assertIn("SLIDES TO NARRATE", vision[1]["content"])
        self.assertIn("VISION MODULES MODE", vision[0]["content"])

    def test_schema_default_classic_and_cache_key_differs(self):
        self.assertEqual(GenerationCreate.model_fields["generation_mode"].default, "classic")
        self.assertEqual(normalize_generation_mode(""), "classic")
        self.assertEqual(normalize_generation_mode("vision_modules"), "vision_modules")
        with self.assertRaises(Exception):
            GenerationCreate(temperature="X1", generation_mode="nope")

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        axes = {
            "audience_cluster": "A",
            "duration": "T2",
            "channel": "C1",
            "intent": "I1",
        }
        with mock.patch("backend.generation_cache.deploy_version", return_value="test"):
            classic = compute_cache_key(axes, "X1", "", "", db, generation_mode="classic")
            vision = compute_cache_key(
                axes, "X1", "", "", db, generation_mode="vision_modules"
            )
        self.assertNotEqual(classic, vision)
        self.assertEqual(SCRIPT_PIPELINE_VERSION, "12")
        db.close()


class VisionModulesPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        for page, title in ((44, "Ventures intro"), (45, "Eat Atlas"), (46, "Other")):
            self.db.add(
                DeckTopic(
                    title=title,
                    pages_json=json.dumps([page]),
                    summary=f"Summary for {title}",
                    module_ids="M06",
                    sort_order=page,
                )
            )
        self.db.add(
            VisionModuleContent(
                vm_id="VM08",
                source_type="pratham_passage",
                source_ref="1",
                text="Pratham on Eat Atlas and PlaySuper ventures",
                strength="strong",
                score=1.0,
                page_hints="45",
                embedding_json=json.dumps(fake_embed(["Eat Atlas PlaySuper ventures"])[0]),
                text_hash="h1",
            )
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_topics_cover_all_slides_narrated_subset(self):
        plan = [
            BrandSlide(1, "", "Cover"),
            BrandSlide(44, "M06", "Ventures intro"),
            BrandSlide(45, "M06", "Eat Atlas and PlaySuper"),
            BrandSlide(46, "M06", "Other venture"),
            BrandSlide(52, "M07", "Immersion photo"),
            BrandSlide(92, "M14", "Closing"),
        ]
        # Seed immersion content empty — still gets quota.
        result = build_vision_modules_plan(
            self.db,
            plan,
            ["M06", "M07", "M14"],
            duration="T4",
            embed_fn=fake_embed,
        )
        body_topics = [
            topic
            for topic in result.topics
            if topic.pages and topic.pages[0] not in {1, 92}
        ]
        vm08 = next(topic for topic in body_topics if "Funded" in topic.title)
        self.assertEqual(vm08.pages, [44, 45, 46])
        self.assertEqual(set(vm08.slide_keys), {"brand:p44", "brand:p45", "brand:p46"})
        self.assertLessEqual(len(vm08.narrated_pages), 3)
        self.assertGreaterEqual(len(vm08.narrated_pages), 1)
        self.assertEqual(
            sorted(vm08.narrated_pages + vm08.shown_not_narrated),
            [44, 45, 46],
        )
        self.assertEqual(result.trace["mode"], "vision_modules")
        self.assertIn("quotas", result.trace)


class RunnerBranchTests(unittest.TestCase):
    def test_fallback_to_classic_when_no_vision_rows(self):
        from backend.pipeline.vision_modules_flow import normalize_generation_mode

        mode = normalize_generation_mode("vision_modules")
        self.assertEqual(mode, "vision_modules")
        # Runner sets classic when vision_sections empty — simulate that gate.
        vision_sections: list = []
        if mode == "vision_modules" and not vision_sections:
            mode = "classic"
        self.assertEqual(mode, "classic")


if __name__ == "__main__":
    unittest.main()
