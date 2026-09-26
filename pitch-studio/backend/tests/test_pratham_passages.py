from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.generation_cache import SCRIPT_PIPELINE_VERSION, compute_cache_key
from backend.models import Module, PrathamMove, PrathamPassage, StyleTranscript, StyleTranscriptPersona
from backend.pipeline.prompts import script_messages
from backend.pipeline.script_flow import ScriptTopic
from backend.pipeline.script_plan import normalize_script_plan
from backend.pratham_passages import (
    format_passages_for_prompt,
    index_passages,
    link_rate,
    parse_tag_payload,
    phrase_reuse_rate,
    select_passages_for_topics,
    split_passages,
)
from backend.pratham_playbook import select_reference
from backend.transcripts import CURATION_SYSTEM

PRATHAM_VTT = """WEBVTT

00:00:01.000 --> 00:00:20.000
Pratham Mittal: Think of this campus like a gym. Paying the fee does not make you fit. Showing up every day does. A renter never fixes the leaking tap. An owner fixes it the same night. Be an owner here. The buffet is laid out. Now you have to get up. This is not playboy school. That is not this program.
"""


def fake_embed(texts: list[str]) -> list[list[float]]:
    keywords = ("gym", "faculty", "owner", "hostel", "builders")
    return [[float(text.lower().count(word)) for word in keywords] + [0.01] for text in texts]


def vector(*weights: float) -> str:
    return json.dumps(list(weights) + [0.01])


class SplitPassagesTests(unittest.TestCase):
    def test_contiguous_no_split_no_overlap_trailing_merge(self):
        sentences = [
            " ".join([f"w{i}"] * 50) + "."
            for i in range(10)
        ]
        text = " ".join(sentences)
        passages = split_passages(text, target_words=100, min_words=60, max_words=150)
        joined = " ".join(passages)
        # Reconstruct original sentence order with no overlap.
        self.assertEqual(joined, text)
        for passage in passages:
            self.assertLessEqual(len(passage.split()), 150)
        for passage in passages[:-1]:
            self.assertGreaterEqual(len(passage.split()), 60)

    def test_max_respected_unless_single_sentence(self):
        short = " ".join(["alpha"] * 40) + "."
        long_one = " ".join(["beta"] * 200) + "."
        passages = split_passages(f"{short} {long_one}", target_words=80, min_words=40, max_words=100)
        self.assertTrue(any(len(p.split()) > 100 for p in passages))
        self.assertTrue(any("beta" in p for p in passages))

    def test_trailing_merge(self):
        # Three chunks near target, last tiny → merge into previous.
        parts = [
            " ".join(["one"] * 80) + ".",
            " ".join(["two"] * 80) + ".",
            " ".join(["three"] * 20) + ".",
        ]
        passages = split_passages(" ".join(parts), target_words=80, min_words=60, max_words=120)
        self.assertEqual(len(passages), 2)
        self.assertIn("three", passages[-1])


class ParseTagPayloadTests(unittest.TestCase):
    def test_drops_unknown_normalises_caps_usable(self):
        parsed = parse_tag_payload(
            {
                "modules": [
                    {"id": "M05", "strength": "STRONG"},
                    {"id": "M99", "strength": "strong"},
                    {"id": "M07", "strength": "maybe"},
                    {"id": "M08", "strength": "passing"},
                    {"id": "M09", "strength": "passing"},
                ],
                "audience": "  parents  ",
                "summary": "x" * 500,
                "usable": "yes",
            },
            {"M05", "M07", "M08", "M09"},
        )
        self.assertEqual(parsed["module_ids"], "M05,M07,M08")
        self.assertEqual(parsed["module_strengths"]["M05"], "strong")
        self.assertEqual(parsed["module_strengths"]["M07"], "passing")
        self.assertTrue(parsed["usable"])
        self.assertEqual(len(parsed["summary"]), 400)
        self.assertEqual(parsed["audience"], "parents")


class IndexAndSelectTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine, autoflush=False)()
        self.solo = StyleTranscript(
            name="Pratham only · A", raw_text=PRATHAM_VTT, text_hash="a", status="processed"
        )
        self.db.add(self.solo)
        self.db.add(Module(id="M05", name="Builders", job="prove", core_content="builders", sort_order=5))
        self.db.add(Module(id="M08", name="Faculty", job="teach", core_content="faculty", sort_order=8))
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_index_replaces_and_total_tagger_failure_keeps_rows(self):
        def tag_ok(_text: str):
            return {
                "modules": [{"id": "M05", "strength": "strong"}],
                "audience": "students",
                "summary": "gym",
                "usable": True,
            }

        def tag_down(_text: str):
            raise RuntimeError("tagger down")

        with mock.patch("backend.generation_cache.bump_content_version"):
            first = index_passages(self.db, self.solo.id, tag=tag_ok, embed_fn=fake_embed, workers=2)
            second = index_passages(self.db, self.solo.id, tag=tag_ok, embed_fn=fake_embed, workers=2)
            with self.assertRaises(RuntimeError):
                index_passages(self.db, self.solo.id, tag=tag_down, embed_fn=fake_embed, workers=2)
        rows = self.db.query(PrathamPassage).order_by(PrathamPassage.passage_index).all()
        self.assertGreaterEqual(first["passages"], 1)
        self.assertEqual(second["passages"], len(rows))
        self.assertTrue(all(row.usable and row.module_ids == "M05" for row in rows))

    def test_select_module_beats_cosine_persona_boost_dedupe_cap_fallback(self):
        self.db.add(
            StyleTranscriptPersona(style_transcript_id=self.solo.id, persona_label="Current student")
        )
        other = StyleTranscript(
            name="Other Pratham",
            raw_text=PRATHAM_VTT,
            text_hash="b",
            status="processed",
        )
        self.db.add(other)
        self.db.commit()

        # Module-matched but low gym cosine; unmatched high gym cosine.
        self.db.add_all(
            [
                PrathamPassage(
                    style_transcript_id=self.solo.id,
                    passage_index=0,
                    text="Faculty practitioners teach every week on campus.",
                    word_count=7,
                    module_ids="M08",
                    module_strengths_json=json.dumps({"M08": "strong"}),
                    usable=True,
                    source_name=self.solo.name,
                    embedding_json=vector(0, 0, 0, 0, 0),
                    text_hash="p1",
                ),
                PrathamPassage(
                    style_transcript_id=other.id,
                    passage_index=0,
                    text="Gym gym gym discipline every morning.",
                    word_count=6,
                    module_ids="",
                    module_strengths_json="{}",
                    usable=True,
                    source_name=other.name,
                    embedding_json=vector(5, 0, 0, 0, 0),
                    text_hash="p2",
                ),
                PrathamPassage(
                    style_transcript_id=other.id,
                    passage_index=1,
                    text="Builders build companies in the program.",
                    word_count=6,
                    module_ids="M05",
                    module_strengths_json=json.dumps({"M05": "passing"}),
                    usable=True,
                    source_name=other.name,
                    embedding_json=vector(0, 0, 0, 0, 3),
                    text_hash="p3",
                ),
                PrathamPassage(
                    style_transcript_id=self.solo.id,
                    passage_index=1,
                    text="Unusable logistics please sit down.",
                    word_count=5,
                    module_ids="M08",
                    module_strengths_json=json.dumps({"M08": "strong"}),
                    usable=False,
                    source_name=self.solo.name,
                    embedding_json=vector(0, 5, 0, 0, 0),
                    text_hash="p4",
                ),
                PrathamPassage(
                    style_transcript_id=other.id,
                    passage_index=2,
                    text="Weak cosine fallback candidate about nothing special.",
                    word_count=8,
                    module_ids="",
                    module_strengths_json="{}",
                    usable=True,
                    source_name=other.name,
                    embedding_json=vector(0, 0, 0, 0, 0),
                    text_hash="p5",
                ),
            ]
        )
        self.db.commit()

        topics = [
            ScriptTopic(topic_id=1, title="Faculty", pages=[1], recipe_modules=["M08"], summary="teachers"),
            ScriptTopic(topic_id=2, title="Builders", pages=[2], module_ids=["M05"], summary="companies"),
            ScriptTopic(topic_id=3, title="Odd", pages=[3], summary="unrelated"),
        ]
        # Untagged persona still gets passages.
        untagged = select_passages_for_topics(
            self.db,
            topics=topics,
            persona_label="Working professional, 24–30 (PG switch)",
            embed_fn=fake_embed,
            per_topic=2,
            word_cap=500,
        )
        self.assertTrue(untagged["topics"])
        faculty = next(t for t in untagged["topics"] if t["topic_id"] == 1)
        self.assertEqual(faculty["passages"][0]["id"], 1)  # module match beats gym cosine
        self.assertEqual(faculty["passages"][0]["match"], "module")
        self.assertNotIn("Unusable", faculty["passages"][0]["text"])

        tagged = select_passages_for_topics(
            self.db,
            topics=[ScriptTopic(topic_id=1, title="Faculty", pages=[1], recipe_modules=["M08"])],
            persona_label="Current student",
            embed_fn=fake_embed,
            per_topic=1,
            word_cap=500,
        )
        self.assertEqual(tagged["topics"][0]["passages"][0]["source_name"], self.solo.name)

        # No passage used twice across topics.
        both = select_passages_for_topics(
            self.db,
            topics=topics[:2],
            persona_label="Current student",
            embed_fn=fake_embed,
            per_topic=1,
            word_cap=500,
        )
        ids = [p["id"] for t in both["topics"] for p in t["passages"]]
        self.assertEqual(len(ids), len(set(ids)))

        capped = select_passages_for_topics(
            self.db,
            topics=topics[:2],
            persona_label="Current student",
            embed_fn=fake_embed,
            per_topic=2,
            word_cap=7,
        )
        self.assertLessEqual(capped["fed_words"], 7)

        # Fallback below 0.3 cosine dropped → odd topic empty.
        def orthogonal_embed(texts: list[str]) -> list[list[float]]:
            out = []
            for text in texts:
                lower = text.lower()
                if "zzzz" in lower or "odd" in lower:
                    out.append([0.0, 0.0, 0.0, 0.0, 0.0, 1.0])
                else:
                    out.append(fake_embed([text])[0])
            return out

        # Mark all remaining passages as near-orthogonal to the odd query.
        for row in self.db.query(PrathamPassage).all():
            row.embedding_json = vector(1, 0, 0, 0, 0)
        self.db.commit()
        odd = select_passages_for_topics(
            self.db,
            topics=[ScriptTopic(topic_id=3, title="Odd", pages=[3], summary="zzzz")],
            persona_label="Nobody",
            embed_fn=orthogonal_embed,
            per_topic=1,
            word_cap=500,
        )
        self.assertEqual(odd["topics"], [])
        self.assertIn(3, odd["empty_topic_ids"])

    def test_near_duplicate_retelling_not_fed_twice(self):
        other = StyleTranscript(name="Other Pratham", raw_text=PRATHAM_VTT, text_hash="b", status="processed")
        self.db.add(other)
        self.db.commit()
        for tid, index, modules, weights, text_hash in (
            (self.solo.id, 0, {"M05": "strong"}, (0, 0, 0, 0, 3), "d1"),
            (other.id, 0, {"M08": "strong"}, (0, 0.1, 0, 0, 3), "d2"),
            (other.id, 1, {"M08": "strong"}, (0, 3, 0, 0, 0), "d3"),
        ):
            self.db.add(
                PrathamPassage(
                    style_transcript_id=tid,
                    passage_index=index,
                    text=f"Passage {text_hash} about builders.",
                    word_count=4,
                    module_ids=",".join(modules),
                    module_strengths_json=json.dumps(modules),
                    usable=True,
                    source_name="s",
                    embedding_json=vector(*weights),
                    text_hash=text_hash,
                )
            )
        self.db.commit()
        selection = select_passages_for_topics(
            self.db,
            topics=[
                ScriptTopic(topic_id=1, title="Builders", pages=[1], module_ids=["M05"]),
                ScriptTopic(topic_id=2, title="Faculty", pages=[2], module_ids=["M08"]),
            ],
            persona_label="Nobody",
            embed_fn=fake_embed,
            per_topic=1,
            word_cap=500,
        )
        texts = [p["text"] for t in selection["topics"] for p in t["passages"]]
        self.assertIn("Passage d1 about builders.", texts)
        self.assertNotIn("Passage d2 about builders.", texts)
        self.assertIn("Passage d3 about builders.", texts)

    def test_format_and_rates(self):
        selection = {
            "topics": [
                {
                    "topic_id": 5,
                    "title": "Builders, Not Backgrounds",
                    "modules": ["M05", "M06"],
                    "passages": [
                        {
                            "id": 1,
                            "text": "This is not playboy school. That is not this program.",
                            "source_name": "Session A",
                            "modules": ["M05"],
                            "match": "module",
                            "score": 1.2,
                            "word_count": 10,
                        }
                    ],
                }
            ]
        }
        block = format_passages_for_prompt(selection)
        self.assertIn("topic_id=5", block)
        self.assertIn("Session A", block)
        self.assertEqual(format_passages_for_prompt({"topics": []}), "")

        chained = (
            "This is not playboy school. That is not this program. "
            "This program is the buffet is laid out. Now you have to get up."
        )
        self.assertGreater(link_rate(chained), 0.5)
        cold = (
            "A practical model needs an institution behind it. "
            "Our purpose is practitioner-led education. "
            "Formal governance challenges delivery."
        )
        self.assertLess(link_rate(cold), link_rate(chained))
        source = "the buffet is laid out now you have to get up and own the room"
        script = "now you have to get up and own the room today"
        self.assertGreater(phrase_reuse_rate(script, source), 0.0)
        self.assertEqual(phrase_reuse_rate("short", source), 0.0)


class PlaybookRankNotGateTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine, autoflush=False)()
        self.solo = StyleTranscript(
            name="Pratham only · A", raw_text=PRATHAM_VTT, text_hash="a", status="processed"
        )
        self.db.add(self.solo)
        self.db.commit()
        self.db.add(
            PrathamMove(
                style_transcript_id=self.solo.id,
                kind="analogy",
                label="Campus as gym",
                excerpt="Think of this campus like a gym.",
                use_when="effort",
                embedding_json=vector(3, 0, 0, 0, 0),
                text_hash="m1",
                source_name=self.solo.name,
            )
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_untagged_persona_gets_moves_and_passage_limit_zero(self):
        ref = select_reference(
            self.db,
            persona_label="Working professional, 24–30 (PG switch)",
            topics=["gym"],
            embed_fn=fake_embed,
        )
        self.assertEqual(len(ref["moves"]), 1)
        empty_passages = select_reference(
            self.db,
            persona_label="Working professional, 24–30 (PG switch)",
            topics=["gym"],
            embed_fn=fake_embed,
            passage_limit=0,
        )
        self.assertEqual(empty_passages["passages"], [])
        self.assertEqual(len(empty_passages["moves"]), 1)


class PromptAndPlanTests(unittest.TestCase):
    def test_writer_has_chaining_and_pratham_move(self):
        plan = {
            "throughline": "t",
            "listener_start": "a",
            "listener_end": "b",
            "arc": "c",
            "ask": "d",
            "beats": [
                {
                    "topic_id": 1,
                    "role": "OPENING + CLOSE",
                    "point": "p",
                    "proof": "",
                    "proof_source": "NONE",
                    "story_device": "none",
                    "bridge_in": "",
                    "pratham_move": "borrow gym analogy",
                    "approx_words": 80,
                }
            ],
        }
        messages = script_messages(
            audience_cluster="A",
            duration="T2",
            channel="CH1",
            intent="I1",
            temperature="",
            context_note="",
            modules=[],
            sequence=[],
            facts=[],
            word_budget=240,
            plan=plan,
            pratham_reference="PRATHAM BY BEAT — test",
        )
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("CHAINED", system)
        self.assertIn("pick up the one before", system)
        self.assertIn("pratham_move=borrow gym analogy", user)
        self.assertIn("PRATHAM BY BEAT AND PLAYBOOK", system)

    def test_plan_keeps_pratham_move(self):
        topics = [ScriptTopic(topic_id=1, title="A", pages=[1])]
        plan = normalize_script_plan(
            {
                "throughline": "t",
                "beats": [
                    {
                        "topic_id": 1,
                        "point": "p",
                        "proof": "",
                        "proof_source": "NONE",
                        "story_device": "none",
                        "pratham_move": "use buffet line",
                        "approx_words": 80,
                    }
                ],
                "ask": "come",
            },
            topic_flow=topics,
            word_budget=80,
        )
        self.assertEqual(plan["beats"][0]["pratham_move"], "use buffet line")

    def test_curation_prompt_no_m07_example(self):
        self.assertNotIn('"module_ids":["M07"]', CURATION_SYSTEM)
        self.assertIn('"module_ids":[]', CURATION_SYSTEM)

    def test_cache_version_and_passage_settings(self):
        from backend.config import settings

        db = mock.MagicMock()
        db.get.return_value = None
        axes = {"audience_cluster": "A", "duration": "T1", "channel": "CH1", "intent": "I2"}
        key1 = compute_cache_key(axes, "X2", "note", "", db)
        with mock.patch.object(settings, "pratham_passages_per_topic", 9):
            key2 = compute_cache_key(axes, "X2", "note", "", db)
        self.assertNotEqual(key1, key2)
        self.assertEqual(SCRIPT_PIPELINE_VERSION, "5")


class RunnerWiringTests(unittest.TestCase):
    def test_beat_passages_reach_prompts_and_trace(self):
        from backend.pipeline.brand_deck import BrandSlide
        from backend.pipeline.runner import generate_script_phase

        target = SimpleNamespace(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X2",
            context_note="",
            recipe_ref="",
            deck_use_case="",
            module_sequence="",
            status="",
            script_json="",
            validation_report="",
            error="",
            founder_quote_ids="",
            report_asset_ids="",
            report_passages_json="",
            script_plan_json="",
            quality_trace_json="",
        )
        topic = ScriptTopic(topic_id=1, title="Open", pages=[1], recipe_modules=["M01"])
        quote = SimpleNamespace(
            id=7,
            text="Stay in India",
            speaker="Pratham Mittal",
            topic="vision",
            module_ids="M12",
            source_name="C0005.MP4",
            source_file_id="abc",
            start_sec=10,
        )
        script = {
            "sections": [{"topic_id": 1, "heading": "Open", "text": "This is not playboy school. That is not this program."}],
            "cta": "Come Saturday",
        }
        selection = {
            "topics": [
                {
                    "topic_id": 1,
                    "title": "Open",
                    "modules": ["M01"],
                    "passages": [
                        {
                            "id": 42,
                            "text": "This is not playboy school. That is not this program.",
                            "source_name": "Session A",
                            "modules": ["M01"],
                            "match": "module",
                            "score": 1.0,
                            "word_count": 10,
                        }
                    ],
                }
            ],
            "fed_words": 10,
            "fallback_topic_ids": [],
            "empty_topic_ids": [],
        }
        captured: dict[str, str] = {}

        def capture_plan(**kwargs):
            captured["plan_ref"] = kwargs.get("pratham_reference") or ""
            return {
                "throughline": "x",
                "listener_start": "a",
                "listener_end": "b",
                "arc": "c",
                "ask": "d",
                "beats": [
                    {
                        "topic_id": 1,
                        "role": "OPENING + CLOSE",
                        "point": "p",
                        "proof": "",
                        "proof_source": "NONE",
                        "story_device": "none",
                        "bridge_in": "",
                        "pratham_move": "",
                        "approx_words": 80,
                    }
                ],
            }

        def capture_chat(messages, **_kwargs):
            captured["writer"] = messages[0]["content"] + messages[1]["content"]
            return script

        settings_mod = __import__("backend.pipeline.runner", fromlist=["settings"]).settings
        db = mock.MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            audience_label="Current student"
        )
        with mock.patch.object(settings_mod, "script_plan_enabled", True), mock.patch.object(
            settings_mod, "flow_check_enabled", False
        ), mock.patch.object(settings_mod, "listener_enabled", False), mock.patch.object(
            settings_mod, "pratham_passages_enabled", True
        ), mock.patch(
            "backend.pipeline.runner.resolve_recipe",
            return_value=SimpleNamespace(ref="A1-1", module_sequence=["M01"], word_budget=200),
        ), mock.patch(
            "backend.pipeline.runner.plan_pages",
            return_value=[BrandSlide(1, "", "Cover")],
        ), mock.patch(
            "backend.pipeline.runner.plan_with_generated_slides",
            side_effect=lambda db, plan, *a, **k: list(plan),
        ), mock.patch("backend.pipeline.runner.pick_report_passages", return_value=[]), mock.patch(
            "backend.pipeline.runner.load_script_topics", return_value=[topic]
        ), mock.patch(
            "backend.pipeline.runner.pick_founder_quotes", return_value=[quote]
        ), mock.patch(
            "backend.pipeline.runner.latest_style_guide", return_value=""
        ), mock.patch(
            "backend.pratham_passages.select_passages_for_topics", return_value=selection
        ), mock.patch(
            "backend.pratham_playbook.select_reference",
            return_value={"moves": [{"row": SimpleNamespace(
                kind="analogy", label="Gym", personal=False, use_when="x",
                excerpt="e", source_name="s"
            )}], "passages": []},
        ), mock.patch(
            "backend.pipeline.runner.plan_script", side_effect=capture_plan
        ), mock.patch(
            "backend.pipeline.runner.chat_json", side_effect=capture_chat
        ), mock.patch(
            "backend.pipeline.runner.ScriptPayload.model_validate", return_value=None
        ), mock.patch(
            "backend.pipeline.runner.align_script_to_topics",
            side_effect=lambda payload, topics, modules: payload,
        ), mock.patch(
            "backend.pipeline.runner.review_pratham_voice",
            return_value={"passed": True, "score": 1.0, "violations": []},
        ), mock.patch(
            "backend.pipeline.runner.validate_script", return_value=[]
        ):
            result = generate_script_phase(db, target, lambda _s: None)

        self.assertIsNotNone(result)
        self.assertIn("PRATHAM BY BEAT", captured["plan_ref"])
        self.assertIn("Session A", captured["writer"])
        trace = json.loads(target.quality_trace_json)
        self.assertIn("pratham", trace)
        self.assertEqual(trace["pratham"]["fed_words"], 10)
        self.assertIn("reuse_rate", trace["pratham"])
        self.assertIn("link_rate", trace["pratham"])

    def test_selection_exception_does_not_fail_run(self):
        from backend.pipeline.brand_deck import BrandSlide
        from backend.pipeline.runner import generate_script_phase

        target = SimpleNamespace(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X2",
            context_note="",
            recipe_ref="",
            deck_use_case="",
            module_sequence="",
            status="",
            script_json="",
            validation_report="",
            error="",
            founder_quote_ids="",
            report_asset_ids="",
            report_passages_json="",
            script_plan_json="",
            quality_trace_json="",
        )
        topic = ScriptTopic(topic_id=1, title="Open", pages=[1], recipe_modules=["M01"])
        quote = SimpleNamespace(
            id=7,
            text="Stay in India",
            speaker="Pratham Mittal",
            topic="vision",
            module_ids="M12",
            source_name="C0005.MP4",
            source_file_id="abc",
            start_sec=10,
        )
        script = {"sections": [{"topic_id": 1, "heading": "Open", "text": "Hello there friends."}], "cta": "Come"}
        settings_mod = __import__("backend.pipeline.runner", fromlist=["settings"]).settings
        db = mock.MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            audience_label="Current student"
        )
        with mock.patch.object(settings_mod, "script_plan_enabled", False), mock.patch.object(
            settings_mod, "flow_check_enabled", False
        ), mock.patch.object(settings_mod, "listener_enabled", False), mock.patch.object(
            settings_mod, "pratham_passages_enabled", True
        ), mock.patch(
            "backend.pipeline.runner.resolve_recipe",
            return_value=SimpleNamespace(ref="A1-1", module_sequence=["M01"], word_budget=200),
        ), mock.patch(
            "backend.pipeline.runner.plan_pages",
            return_value=[BrandSlide(1, "", "Cover")],
        ), mock.patch(
            "backend.pipeline.runner.plan_with_generated_slides",
            side_effect=lambda db, plan, *a, **k: list(plan),
        ), mock.patch("backend.pipeline.runner.pick_report_passages", return_value=[]), mock.patch(
            "backend.pipeline.runner.load_script_topics", return_value=[topic]
        ), mock.patch(
            "backend.pipeline.runner.pick_founder_quotes", return_value=[quote]
        ), mock.patch(
            "backend.pipeline.runner.latest_style_guide", return_value=""
        ), mock.patch(
            "backend.pratham_passages.select_passages_for_topics",
            side_effect=RuntimeError("boom"),
        ), mock.patch(
            "backend.pipeline.runner.chat_json", return_value=script
        ), mock.patch(
            "backend.pipeline.runner.ScriptPayload.model_validate", return_value=None
        ), mock.patch(
            "backend.pipeline.runner.align_script_to_topics",
            side_effect=lambda payload, topics, modules: payload,
        ), mock.patch(
            "backend.pipeline.runner.review_pratham_voice",
            return_value={"passed": True, "score": 1.0, "violations": []},
        ), mock.patch(
            "backend.pipeline.runner.validate_script", return_value=[]
        ):
            result = generate_script_phase(db, target, lambda _s: None)
        self.assertIsNotNone(result)
        trace = json.loads(target.quality_trace_json)
        self.assertIn("boom", trace["pratham"]["error"])


if __name__ == "__main__":
    unittest.main()
