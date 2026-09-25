import json
import unittest
from types import SimpleNamespace

from backend.pipeline.brand_deck import CLOSING_PAGE, plan_pages
from backend.pipeline.dsai_deck import (
    DSAI_SOURCE,
    DsaiSlide,
    dsai_slide_budget,
    insert_before_closing,
    is_dsai_request,
    select_dsai_slides,
)
from backend.pipeline.script_flow import build_script_topics


def _topic(topic_id, pages, *, deck="dsai", recs=(), verdicts=None, modules="", order=0):
    return SimpleNamespace(
        id=topic_id,
        deck=deck,
        sort_order=order,
        title=f"Topic {topic_id}",
        pages_json=json.dumps(list(pages)),
        summary=f"Summary {topic_id}",
        vision="",
        module_ids=modules,
        recommendations_json=json.dumps(
            {"items": [{"recipe_ref": ref, "confidence": conf} for ref, conf in recs]}
        ),
        feedback_json=json.dumps({"verdicts": verdicts or {}, "added": []}),
    )


class DetectionTests(unittest.TestCase):
    def test_detects_ds_ai_context(self):
        for text in (
            "Pitch for our DS AI programme",
            "data science aspirants",
            "Students interested in AI",
            "A DS/AI masterclass",
            "machine learning careers",
        ):
            self.assertTrue(is_dsai_request(text), text)

    def test_ignores_unrelated_context(self):
        for text in ("", None, "A Zoom call with parents", "Explain the admissions process"):
            self.assertFalse(is_dsai_request(text), text)

    def test_budget_is_a_minority_share(self):
        self.assertEqual(dsai_slide_budget(8), 2)
        self.assertEqual(dsai_slide_budget(51), 17)


class SelectionTests(unittest.TestCase):
    def test_persona_matches_first_and_unrecommended_topics_skipped(self):
        topics = [
            _topic(1, range(1, 5), order=0),  # workshop, nobody recommended
            _topic(2, range(82, 86), recs=[("A1-1", 0.9)], modules="M04", order=1),
            _topic(3, range(94, 97), recs=[("C1-1", 0.8)], modules="M05", order=2),
        ]
        slides = select_dsai_slides(topics, recipe_ref="A1-1", sequence=["M04", "M14"], budget=3)
        pages = [slide.page for slide in slides]
        self.assertNotIn(1, pages)
        self.assertIn(82, pages)
        self.assertEqual(pages, sorted(pages))
        self.assertTrue(all(slide.source == DSAI_SOURCE for slide in slides))
        self.assertEqual(next(s for s in slides if s.page == 82).module_id, "M04")

    def test_rejected_topic_is_never_used(self):
        topics = [_topic(2, range(82, 86), recs=[("A1-1", 0.9)], verdicts={"A1-1": {"verdict": "no"}})]
        self.assertEqual(select_dsai_slides(topics, recipe_ref="A1-1", sequence=[], budget=4), [])

    def test_respects_budget(self):
        topics = [_topic(2, range(82, 100), recs=[("A1-1", 0.9)])]
        self.assertEqual(len(select_dsai_slides(topics, recipe_ref="A1-1", sequence=[], budget=4)), 4)


class PlanTests(unittest.TestCase):
    def test_dsai_slides_sit_before_closing_alongside_brand_deck(self):
        plan = plan_pages(["M01", "M04", "M14"], 8)
        dsai = [DsaiSlide(82, "", "We believe in learning by doing"), DsaiSlide(96, "", "Stipend")]
        merged = insert_before_closing(plan, dsai)
        self.assertEqual(len(merged), len(plan) + 2)
        self.assertEqual(merged[-1].page, CLOSING_PAGE)
        self.assertEqual([s.slide_key for s in merged[-3:-1]], ["dsai:p82", "dsai:p96"])
        self.assertEqual(merged[:-3], plan[:-1])

    def test_script_topics_map_dsai_pages_to_dsai_topics(self):
        plan = plan_pages(["M01", "M14"], 6)
        brand_topics = [
            _topic(10, range(1, 93), deck="brand", modules="M01,M14"),
        ]
        dsai_topics = [_topic(20, range(82, 90), recs=[("A1-1", 0.9)])]
        merged = insert_before_closing(plan, [DsaiSlide(82, "", "Learning by doing")])
        flow = build_script_topics(merged, brand_topics + dsai_topics, ["M01", "M14"])
        dsai_beats = [topic for topic in flow if "dsai:p82" in topic.slide_keys]
        self.assertEqual(len(dsai_beats), 1)
        self.assertEqual(dsai_beats[0].summary, "Summary 20")
        brand_keys = [key for topic in flow for key in topic.slide_keys if key.startswith("brand:")]
        self.assertEqual(brand_keys, [slide.slide_key for slide in plan])


if __name__ == "__main__":
    unittest.main()
