from __future__ import annotations

import unittest
from unittest import mock

from backend.config import settings
from backend.pipeline.brand_deck import (
    CLOSING_PAGE,
    COVER_PAGE,
    MODULE_PAGES,
    SOURCE_PAGE_COUNT,
    BrandDeckUnavailable,
    deck_spec_from_plan,
    plan_pages,
    render_pptx,
    resolve_source,
)
from backend.pipeline.deck import slide_count_for
from backend.pipeline.resolver import FALLBACK_BY_INTENT


class BrandDeckMapTests(unittest.TestCase):
    def test_every_source_page_is_mapped_exactly_once(self):
        pages = [COVER_PAGE, CLOSING_PAGE]
        for entries in MODULE_PAGES.values():
            pages.extend(page for page, _label in entries)
        self.assertEqual(len(pages), len(set(pages)), "a page is mapped to two modules")
        self.assertEqual(sorted(pages), list(range(1, SOURCE_PAGE_COUNT + 1)))

    def test_every_page_has_a_label(self):
        for module_id, entries in MODULE_PAGES.items():
            for page, label in entries:
                self.assertTrue(label.strip(), f"{module_id} p{page} has no label")


class BrandDeckPlanTests(unittest.TestCase):
    def test_plan_is_bookended_and_within_budget(self):
        plan = plan_pages(["M01", "M04", "M07", "M14"], 12)
        self.assertEqual(len(plan), 12)
        self.assertEqual(plan[0].page, COVER_PAGE)
        self.assertEqual(plan[-1].page, CLOSING_PAGE)

    def test_no_page_repeats(self):
        plan = plan_pages(["M01", "M04", "M07", "M14"], 22)
        pages = [slide.page for slide in plan]
        self.assertEqual(len(pages), len(set(pages)))

    def test_modules_appear_in_recipe_order(self):
        sequence = ["M07", "M01", "M12"]
        plan = plan_pages(sequence, 16)
        seen: list[str] = []
        for slide in plan[1:-1]:
            if slide.module_id and (not seen or seen[-1] != slide.module_id):
                seen.append(slide.module_id)
        self.assertEqual([mid for mid in seen if mid in sequence][: len(sequence)], sequence)

    def test_pages_within_a_module_stay_in_deck_order(self):
        plan = plan_pages(["M08"], 8)
        pages = [slide.page for slide in plan if slide.module_id == "M08"]
        self.assertEqual(pages, sorted(pages))

    def test_short_deck_takes_each_modules_best_page(self):
        # T0 is 8 slides: cover + closing leaves 6 for three modules.
        plan = plan_pages(["M01", "M04", "M14"], 8)
        self.assertEqual(len(plan), 8)
        self.assertIn(6, [slide.page for slide in plan])  # M01's best page
        self.assertIn(51, [slide.page for slide in plan])  # M04's best page

    def test_thin_sequence_is_topped_up_to_the_slide_budget(self):
        # M13 has two pages and M14 one, so the budget can only be met by
        # topping up from the rest of the deck.
        plan = plan_pages(["M13", "M14"], 16)
        self.assertEqual(len(plan), 16)

    def test_every_recipe_fallback_fills_every_duration(self):
        for intent, sequence in FALLBACK_BY_INTENT.items():
            for duration in ("T0", "T1", "T2", "T3", "T4", "T5"):
                count = slide_count_for(duration)
                plan = plan_pages(list(sequence), count)
                self.assertEqual(
                    len(plan), count, f"{intent}/{duration} produced {len(plan)} of {count} slides"
                )

    def test_unknown_modules_are_ignored(self):
        plan = plan_pages(["M99", "M01"], 6)
        self.assertEqual(len(plan), 6)
        self.assertTrue(all(slide.module_id != "M99" for slide in plan))

    def test_spec_carries_page_and_image_url(self):
        spec = deck_spec_from_plan(plan_pages(["M01", "M14"], 6))
        self.assertTrue(spec.slides)
        for slide in spec.slides:
            self.assertEqual(slide.layout, "image")
            self.assertGreater(slide.page, 0)
            self.assertEqual(slide.image_url, f"/api/brand-deck/pages/{slide.page}.jpg")


class BrandDeckRenderTests(unittest.TestCase):
    """Needs the brand deck PDF, so it skips where the source is not synced."""

    def setUp(self):
        try:
            resolve_source()
        except BrandDeckUnavailable as exc:
            self.skipTest(str(exc))

    def test_renders_one_full_bleed_picture_per_planned_page(self):
        from pptx import Presentation

        plan = plan_pages(["M01", "M07", "M14"], 8)
        with mock.patch.object(type(settings), "uses_spaces", property(lambda self: False)):
            path = render_pptx(plan, 999200, notes_by_module={"M01": "Origin note."})
        presentation = Presentation(path)
        self.assertEqual(len(presentation.slides._sldIdLst), len(plan))
        for slide in presentation.slides:
            picture = slide.shapes[0]
            self.assertEqual(picture.width, presentation.slide_width)
            self.assertEqual(picture.height, presentation.slide_height)
