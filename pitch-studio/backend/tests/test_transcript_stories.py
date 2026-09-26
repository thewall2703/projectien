from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Module, StyleTranscript, TranscriptStory
from backend.pipeline.script_flow import ScriptTopic
from backend.transcript_stories import (
    chunk_cues,
    format_stories_for_prompt,
    index_stories,
    non_pratham_cues,
    parse_story_items,
    select_stories_for_topics,
)

MIXED_VTT = """WEBVTT

00:00:01.000 --> 00:00:20.000
Pratham Mittal: Think of this campus like a gym and show up every day with intent for the full year.

00:00:20.000 --> 00:00:50.000
Jaishree Soni: In the 2025 cohort, student Ananya built a logistics startup during the programme and raised a seed round from an alumni angel. The venture still operates out of the campus founders office and hired two juniors from the next batch.

00:00:50.000 --> 00:01:10.000
Pratham Yadav: Am I audible? Also can someone share the Zoom link in the chat please for late joiners.

00:01:10.000 --> 00:01:40.000
Tegpreet Chawla: The PG programme has sixteen specialty tracks and a twelve week industry project with partner companies across India. Placement support includes mock interviews and a dedicated careers office that works with hiring managers every term.
"""


def fake_embed(texts: list[str]) -> list[list[float]]:
    keywords = ("startup", "tracks", "gym", "placement", "cohort")
    return [[float(text.lower().count(word)) for word in keywords] + [0.01] for text in texts]


def vector(*weights: float) -> str:
    return json.dumps(list(weights) + [0.01])


class NonPrathamParseTests(unittest.TestCase):
    def test_excludes_pratham_mittal_keeps_others(self):
        cues = non_pratham_cues(MIXED_VTT)
        speakers = {c["speaker"] for c in cues}
        self.assertNotIn("Pratham Mittal", speakers)
        self.assertIn("Jaishree Soni", speakers)
        self.assertIn("Pratham Yadav", speakers)
        self.assertIn("Tegpreet Chawla", speakers)
        joined = " ".join(c["text"] for c in cues)
        self.assertNotIn("campus like a gym", joined)


class ParseAndIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine, autoflush=False)()
        self.db.add(Module(id="M05", name="Builders", job="prove", core_content="builders", sort_order=5))
        self.db.add(Module(id="M06", name="Programme", job="explain", core_content="tracks", sort_order=6))
        self.row = StyleTranscript(
            name="PG webinar",
            raw_text=MIXED_VTT,
            text_hash="s1",
            status="processed",
            source_url="https://example.com/v",
        )
        self.db.add(self.row)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_parse_validates_excerpt_and_maps_times(self):
        chunk_text = (
            "In the 2025 cohort, student Ananya built a logistics startup during the programme "
            "and raised a seed round from an alumni angel. The venture still operates out of "
            "the campus founders office and hired two juniors from the next batch."
        )
        cues = non_pratham_cues(MIXED_VTT)
        items = parse_story_items(
            {
                "items": [
                    {
                        "kind": "story",
                        "title": "Ananya logistics startup",
                        "text": (
                            "In the 2025 cohort, Ananya built a logistics startup during the "
                            "programme, raised a seed round from an alumni angel, and later "
                            "hired juniors from the next batch while operating from campus."
                        ),
                        "excerpt": chunk_text,
                        "speaker": "Jaishree Soni",
                        "modules": [{"id": "M05", "strength": "strong"}],
                        "entities": ["Ananya"],
                        "figures": ["2025"],
                        "usable": True,
                    },
                    {
                        "kind": "fact",
                        "title": "Invented",
                        "text": " ".join(["word"] * 40),
                        "excerpt": "this excerpt is not in the chunk at all and should be dropped",
                        "speaker": "X",
                        "modules": [{"id": "M06", "strength": "passing"}],
                        "usable": True,
                    },
                ]
            },
            chunk_text,
            [c for c in cues if c["speaker"] == "Jaishree Soni"],
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "story")
        self.assertEqual(items[0]["start_sec"], 20.0)
        self.assertEqual(items[0]["end_sec"], 50.0)

    def test_index_dedupes_overlap_and_total_failure_keeps_rows(self):
        excerpt = (
            "In the 2025 cohort, student Ananya built a logistics startup during the programme "
            "and raised a seed round from an alumni angel. The venture still operates out of "
            "the campus founders office and hired two juniors from the next batch."
        )
        paraphrase = (
            "In the 2025 cohort, Ananya built a logistics startup during the programme, "
            "raised a seed round from an alumni angel, and later hired juniors from the "
            "next batch while still operating from the campus founders office."
        )

        def extract_ok(_text: str):
            return {
                "items": [
                    {
                        "kind": "story",
                        "title": "Ananya logistics",
                        "text": paraphrase,
                        "excerpt": excerpt,
                        "speaker": "Jaishree Soni",
                        "modules": [{"id": "M05", "strength": "strong"}],
                        "entities": ["Ananya"],
                        "figures": ["2025"],
                        "usable": True,
                    }
                ]
            }

        def extract_down(_text: str):
            raise RuntimeError("extractor down")

        with mock.patch("backend.generation_cache.bump_content_version"):
            first = index_stories(
                self.db, self.row.id, extract=extract_ok, embed_fn=fake_embed, workers=2
            )
            second = index_stories(
                self.db, self.row.id, extract=extract_ok, embed_fn=fake_embed, workers=2
            )
            with self.assertRaises(RuntimeError):
                index_stories(
                    self.db, self.row.id, extract=extract_down, embed_fn=fake_embed, workers=2
                )
        rows = self.db.query(TranscriptStory).all()
        self.assertEqual(first["usable"], 1)
        self.assertEqual(second["stories"], len(rows))
        self.assertEqual(len(rows), 1)
        self.assertNotIn("Pratham Mittal", rows[0].speaker)
        self.assertNotIn("campus like a gym", rows[0].excerpt)
        self.assertEqual(rows[0].start_sec, 20.0)

    def test_chunk_overlap_preserves_cues(self):
        cues = non_pratham_cues(MIXED_VTT)
        chunks = chunk_cues(cues, target_words=40, overlap_words=10)
        self.assertGreaterEqual(len(chunks), 1)
        self.assertTrue(all(chunk["cues"] for chunk in chunks))


class SelectAndFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine, autoflush=False)()
        src = StyleTranscript(name="Session B", raw_text="x", text_hash="b", status="processed")
        self.db.add(src)
        self.db.commit()
        self.db.add_all(
            [
                TranscriptStory(
                    style_transcript_id=src.id,
                    story_index=0,
                    kind="story",
                    title="Ananya startup",
                    text="Ananya built a logistics startup in the 2025 cohort and raised seed funding.",
                    excerpt="Ananya built a logistics startup",
                    speaker="Jaishree Soni",
                    start_sec=20.0,
                    module_ids="M05",
                    module_strengths_json=json.dumps({"M05": "strong"}),
                    usable=True,
                    source_name="Session B",
                    embedding_json=vector(3, 0, 0, 0, 0),
                    text_hash="t1",
                ),
                TranscriptStory(
                    style_transcript_id=src.id,
                    story_index=1,
                    kind="fact",
                    title="Sixteen tracks",
                    text="The PG programme has sixteen specialty tracks and industry projects.",
                    excerpt="sixteen specialty tracks",
                    speaker="Tegpreet Chawla",
                    start_sec=70.0,
                    module_ids="M06",
                    module_strengths_json=json.dumps({"M06": "strong"}),
                    usable=True,
                    source_name="Session B",
                    embedding_json=vector(0, 3, 0, 0, 0),
                    text_hash="t2",
                ),
                TranscriptStory(
                    style_transcript_id=src.id,
                    story_index=2,
                    kind="story",
                    title="Near dup Ananya",
                    text="Ananya built a logistics startup in the 2025 cohort and raised seed funding again.",
                    excerpt="near duplicate",
                    speaker="Other",
                    start_sec=90.0,
                    module_ids="M05",
                    module_strengths_json=json.dumps({"M05": "strong"}),
                    usable=True,
                    source_name="Session B",
                    embedding_json=vector(2.9, 0.1, 0, 0, 0),
                    text_hash="t3",
                ),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_select_module_near_dup_and_word_cap(self):
        selection = select_stories_for_topics(
            self.db,
            topics=[
                ScriptTopic(topic_id=1, title="Builders startup", pages=[1], module_ids=["M05"]),
                ScriptTopic(topic_id=2, title="Programme tracks", pages=[2], module_ids=["M06"]),
            ],
            embed_fn=fake_embed,
            per_topic=2,
            word_cap=500,
        )
        by_topic = {t["topic_id"]: t["stories"] for t in selection["topics"]}
        self.assertEqual(by_topic[1][0]["title"], "Ananya startup")
        titles = [s["title"] for stories in by_topic.values() for s in stories]
        self.assertNotIn("Near dup Ananya", titles)
        self.assertEqual(by_topic[2][0]["kind"], "fact")

        capped = select_stories_for_topics(
            self.db,
            topics=[
                ScriptTopic(topic_id=1, title="Builders startup", pages=[1], module_ids=["M05"]),
                ScriptTopic(topic_id=2, title="Programme tracks", pages=[2], module_ids=["M06"]),
            ],
            embed_fn=fake_embed,
            per_topic=2,
            word_cap=12,
        )
        self.assertLessEqual(capped["fed_words"], 12)

    def test_format_block(self):
        selection = {
            "topics": [
                {
                    "topic_id": 1,
                    "title": "Builders",
                    "modules": ["M05"],
                    "stories": [
                        {
                            "id": 1,
                            "kind": "story",
                            "title": "Ananya startup",
                            "text": "Ananya built a logistics startup.",
                            "speaker": "Jaishree Soni",
                            "source_name": "Session B",
                            "start_sec": 20.0,
                            "modules": ["M05"],
                            "match": "module",
                            "score": 1.2,
                            "word_count": 5,
                        }
                    ],
                }
            ]
        }
        block = format_stories_for_prompt(selection)
        self.assertIn("REAL STORIES & FACTS BY BEAT", block)
        self.assertIn("[story] Ananya startup", block)
        self.assertIn("Jaishree Soni, Session B @", block)
        self.assertEqual(format_stories_for_prompt({"topics": []}), "")


class RunnerStoryWiringTests(unittest.TestCase):
    def test_empty_stories_and_selection_error_do_not_break(self):
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
            "sections": [{"topic_id": 1, "heading": "Open", "text": "Hello there friends."}],
            "cta": "Come",
        }
        settings_mod = __import__("backend.pipeline.runner", fromlist=["settings"]).settings
        db = mock.MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            audience_label="Current student"
        )

        def run_once(*, stories_side_effect):
            with mock.patch.object(settings_mod, "script_plan_enabled", False), mock.patch.object(
                settings_mod, "flow_check_enabled", False
            ), mock.patch.object(settings_mod, "listener_enabled", False), mock.patch.object(
                settings_mod, "pratham_passages_enabled", True
            ), mock.patch.object(
                settings_mod, "transcript_stories_enabled", True
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
                return_value={"topics": [], "fed_words": 0, "fallback_topic_ids": [], "empty_topic_ids": []},
            ), mock.patch(
                "backend.transcript_stories.select_stories_for_topics",
                side_effect=stories_side_effect,
            ), mock.patch(
                "backend.pratham_playbook.select_reference",
                return_value={"moves": [], "passages": []},
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
                return generate_script_phase(db, target, lambda _s: None)

        result = run_once(stories_side_effect=lambda *a, **k: {"topics": [], "fed_words": 0})
        self.assertIsNotNone(result)
        trace = json.loads(target.quality_trace_json)
        self.assertEqual(trace["pratham"]["story_ids"], [])
        self.assertEqual(trace["pratham"]["story_words"], 0)

        result2 = run_once(stories_side_effect=RuntimeError("stories boom"))
        self.assertIsNotNone(result2)
        trace2 = json.loads(target.quality_trace_json)
        self.assertEqual(trace2["pratham"]["story_ids"], [])
        self.assertEqual(trace2["pratham"].get("error"), "")


if __name__ == "__main__":
    unittest.main()
