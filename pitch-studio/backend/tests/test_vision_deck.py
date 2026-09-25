"""Tests for the Deck - Vision Mapping planner and seed parsers."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from types import SimpleNamespace

from backend.pipeline.brand_deck import CLOSING_PAGE, COVER_PAGE, BrandSlide, plan_pages
from backend.pipeline.deck import slide_ceiling_for
from backend.pipeline.gaps import (
    build_gap_plan,
    generated_slide_budget,
    vision_candidates_from_instructions,
)
from backend.pipeline.vision_deck import (
    VisionInstruction,
    VisionSectionPlan,
    _allocate_slots,
    classify_logline,
    insert_at_section,
    module_sequence_from_slides,
    normalize_use_case,
    parse_page_list,
    parse_vision_sheet_rows,
    plan_vision_pages,
    slides_included_to_text,
)


class ParseHelpersTests(unittest.TestCase):
    def test_excel_date_becomes_day_month_range(self):
        self.assertEqual(slides_included_to_text(datetime(2026, 11, 1)), "1-11")
        self.assertEqual(slides_included_to_text(datetime(2026, 11, 9)), "9-11")
        self.assertEqual(parse_page_list(datetime(2026, 11, 1)), list(range(1, 12)))

    def test_ordered_page_list_preserves_authored_order(self):
        self.assertEqual(
            parse_page_list("19-23, 25, 26, 49, 28, 30"),
            [19, 20, 21, 22, 23, 25, 26, 49, 28, 30],
        )

    def test_dash_and_na_are_empty(self):
        self.assertEqual(parse_page_list("-"), [])
        self.assertEqual(parse_page_list("N/A"), [])
        self.assertEqual(parse_page_list("NA"), [])

    def test_forward_fill_merged_section_cells(self):
        rows = [
            ("Sl No.", "Titles / Section", "Slides Included", "Use Case", "Relevant Slides", "More", "logline", "", ""),
            (1, "The Founding Story", "1-6", "school_fair", "1-3, 6", "No", "N/A", "", ""),
            (2, None, None, "UG TBM aspirant", "1-3, 6, 8, 9", "No", "NA", "", ""),
            (3, "Outcomes", "55-64", "Parents, undecided", "-", "Yes", "Add slide: proof", "", ""),
        ]
        parsed = parse_vision_sheet_rows(rows)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0].section, "The Founding Story")
        self.assertEqual(parsed[1].section, "The Founding Story")
        self.assertEqual(parsed[1].use_case, "ug_tbm")
        self.assertEqual(parsed[1].section_pages, [1, 2, 3, 4, 5, 6])
        self.assertEqual(parsed[1].pages, [1, 2, 3, 6, 8, 9])
        self.assertEqual(parsed[2].pages, [])
        self.assertTrue(parsed[2].needs_more)

    def test_normalize_use_case(self):
        self.assertEqual(normalize_use_case("UG DSAI aspirant"), "ug_dsai")
        self.assertEqual(normalize_use_case("parents_decided"), "parents_decided")
        self.assertEqual(normalize_use_case("a recruiter"), "")

    def test_normalize_use_case_rejects_fragments(self):
        self.assertEqual(normalize_use_case("ug"), "")
        self.assertEqual(normalize_use_case("fair"), "")
        self.assertEqual(normalize_use_case("parents"), "")
        self.assertEqual(normalize_use_case("Parents, decided (X5 follow-up)"), "parents_decided")


class ClassifyLoglineTests(unittest.TestCase):
    def test_replace_and_add(self):
        items = classify_logline("Redo slide 55: What MUU Undergrads are doing right now")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].action, "replace")
        self.assertEqual(items[0].target_page, 55)

        items = classify_logline("Add slide: what MUU UG is doing right now")
        self.assertEqual(items[0].action, "add")
        self.assertIsNone(items[0].target_page)

    def test_design_only_ignored_patterns(self):
        items = classify_logline(
            "Slides 82-87: match visual language to the rest of the presentation"
        )
        self.assertTrue(items)
        self.assertTrue(all(item.action == "design_only" for item in items))

    def test_media_swaps_are_design_only(self):
        for logline in ("Replace slide 35: food lab video", "Slide 70: video (?)"):
            items = classify_logline(logline)
            self.assertEqual([item.action for item in items], ["design_only"], logline)

    def test_leading_slide_reference_is_replace(self):
        items = classify_logline("Slide 67: replace with life @ mu")
        self.assertEqual([(item.action, item.target_page) for item in items], [("replace", 67)])

    def test_slide_list_targets_every_page(self):
        items = classify_logline("Redesign slides 25, 26:\n- compile biggest hitters\n- highlight more women")
        self.assertEqual([item.target_page for item in items], [25, 26])
        self.assertTrue(all(item.action == "replace" for item in items))
        self.assertIn("highlight more women", items[0].brief)

    def test_header_and_bullets_are_one_slide(self):
        items = classify_logline(
            "The rationale behind learn by doing:\n"
            "- why textbook learning is not the same\n"
            "- how it is incremental (v/s b-school)\n"
            "- student experience"
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].action, "add")
        self.assertIn("v/s b-school", items[0].brief)

    def test_add_slides_on_fans_out_per_bullet(self):
        items = classify_logline(
            "Redo placement section, add slides on:\n"
            "- summer placements\n"
            "- career growth/ pivot for CMT\n"
            "- condense existing slides"
        )
        self.assertEqual([item.action for item in items], ["add", "add", "design_only"])
        self.assertIn("career growth/ pivot for CMT", items[1].brief)

    def test_semicolon_reason_stays_with_its_slide(self):
        items = classify_logline("Redesign slide 19; focus isn't on 'learn differently'")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].target_page, 19)
        self.assertIn("learn differently", items[0].brief)

    def test_bullet_naming_its_own_slide_is_separate(self):
        items = classify_logline(
            "More technical tweak of slides 3-4\n- DSAI examples\n- Do over slide 8 completely"
        )
        self.assertEqual([item.target_page for item in items], [3, 4, 8])
        self.assertEqual(items[2].brief, "Do over slide 8 completely")


class PlanVisionPagesTests(unittest.TestCase):
    def _sections(self) -> list[VisionSectionPlan]:
        return [
            VisionSectionPlan(
                "The Founding Story",
                0,
                [1, 2, 3, 6, 9, 10],
                [],
            ),
            VisionSectionPlan(
                "Purpose",
                1,
                [9, 10, 11],
                [],
            ),
            VisionSectionPlan(
                "Outcomes",
                2,
                [55, 56, 57, 58, 64],
                [
                    VisionInstruction("replace", "Redo slide 55", 55, "Outcomes", 2),
                    VisionInstruction("add", "Add slide: cool shit", None, "Outcomes", 2),
                    VisionInstruction("design_only", "different image", None, "Outcomes", 2),
                ],
            ),
        ]

    def test_full_spine_at_t5(self):
        result = plan_vision_pages(self._sections(), "T5", use_case="school_fair")
        pages = [slide.page for slide in result.slides]
        self.assertEqual(pages[0], COVER_PAGE)
        self.assertEqual(pages[-1], CLOSING_PAGE)
        # Cover skipped from body; 9/10 deduped on first occurrence.
        self.assertEqual(pages[1:-1], [2, 3, 6, 9, 10, 11, 55, 56, 57, 58, 64])
        self.assertEqual(len(pages), len(set(pages)))

    def test_trim_keeps_every_section_within_ceiling(self):
        result = plan_vision_pages(self._sections(), "T1", use_case="school_fair")
        ceiling = slide_ceiling_for("T1")
        self.assertLessEqual(len(result.slides), ceiling)
        sections_present = {
            getattr(slide, "section", "")
            for slide in result.slides[1:-1]
            if getattr(slide, "section", "")
        }
        self.assertIn("The Founding Story", sections_present)
        self.assertIn("Purpose", sections_present)
        self.assertIn("Outcomes", sections_present)
        self.assertEqual(result.slides[-1].page, CLOSING_PAGE)

    def test_authored_order_preserved_after_trim(self):
        sections = [
            VisionSectionPlan(
                "Learning",
                0,
                [19, 20, 21, 22, 23, 25, 26, 49, 28],
                [],
            ),
        ]
        result = plan_vision_pages(sections, "T2", use_case="school_fair", ceiling=8)
        body = [slide.page for slide in result.slides[1:-1]]
        # Kept pages stay in authored order (not re-sorted by rank).
        self.assertEqual(body, sorted(body, key=lambda page: [19, 20, 21, 22, 23, 25, 26, 49, 28].index(page)))

    def test_design_only_instructions_dropped_from_result(self):
        result = plan_vision_pages(self._sections(), "T5", use_case="school_fair")
        actions = {item.action for item in result.instructions}
        self.assertEqual(actions, {"replace", "add"})

    def test_allocation_is_proportional_and_exact(self):
        allot = _allocate_slots([20, 5, 1, 0], 10)
        self.assertEqual(sum(allot), 10)
        self.assertEqual(allot[3], 0)
        self.assertEqual(allot[2], 1)
        self.assertGreater(allot[0], allot[1])
        self.assertEqual(_allocate_slots([3, 3, 3], 2), [1, 1, 0])
        self.assertEqual(_allocate_slots([2, 9], 20), [2, 9])

    def test_insert_at_section_places_after_earlier_sections(self):
        sections = [
            VisionSectionPlan("Founding", 0, []),
            VisionSectionPlan("Why", 1, [12, 18]),
            VisionSectionPlan("Blank middle", 2, []),
            VisionSectionPlan("Outcomes", 3, [55]),
        ]
        result = plan_vision_pages(sections, "T5")
        extra = [SimpleNamespace(page=7, module_id="M01", source="dsai")]
        first = insert_at_section(result.slides, extra, result, 0)
        self.assertIs(first[1], extra[0])
        middle = insert_at_section(result.slides, extra, result, 2)
        self.assertEqual([getattr(s, "page", None) for s in middle], [COVER_PAGE, 12, 18, 7, 55, CLOSING_PAGE])

    def test_module_sequence_from_slides_skips_bookends(self):
        slides = [
            BrandSlide(COVER_PAGE, "M01", "cover"),
            SimpleNamespace(module_id="M07"),
            SimpleNamespace(module_id=""),
            SimpleNamespace(module_id="M03"),
            BrandSlide(CLOSING_PAGE, "M14", "closing"),
        ]
        self.assertEqual(module_sequence_from_slides(slides), ["M07", "M03", "M14"])

    def test_module_sequence_derived_from_kept_pages(self):
        result = plan_vision_pages(self._sections(), "T5", use_case="school_fair")
        self.assertTrue(result.module_sequence)
        self.assertEqual(result.module_sequence[-1], "M14")


class VisionGapsTests(unittest.TestCase):
    def test_replace_lands_on_target_page(self):
        plan = plan_pages(["M01", "M07", "M14"], 12)
        target = next(slide.page for slide in plan if slide.page == 58 or slide.module_id == "M07")
        # Prefer page 58 if present; otherwise first M07 page.
        pages = {slide.page for slide in plan}
        target = 58 if 58 in pages else target
        instructions = [VisionInstruction("replace", "Redo placements", target, "Outcomes", 0)]
        candidates = vision_candidates_from_instructions(instructions)
        result = build_gap_plan(
            plan,
            ["M01", "M07", "M14"],
            "T3",
            vision_candidates=candidates,
            rank_fn=None,
            match_fn=None,
            allow_photo_templates=False,
        )
        generated = [slide for slide in result.plan if getattr(slide, "source", "") == "generated"]
        self.assertTrue(generated)
        replaced = next(
            (slide for slide in generated if getattr(slide, "replaced_page", None) == target),
            None,
        )
        self.assertIsNotNone(replaced)
        # The target brand page is no longer in the plan.
        self.assertNotIn(target, [s.page for s in result.plan if getattr(s, "page", None)])

    def test_add_inserts_before_closing(self):
        from dataclasses import replace as dc_replace

        plan = plan_pages(["M01", "M14"], 8)
        plan = [
            dc_replace(slide, section="Outcomes")
            if getattr(slide, "page", 0) not in (COVER_PAGE, CLOSING_PAGE)
            else slide
            for slide in plan
        ]
        instructions = [VisionInstruction("add", "Add slide: cool shit kids do", None, "Outcomes", 0)]
        candidates = vision_candidates_from_instructions(instructions)
        result = build_gap_plan(
            plan,
            ["M01", "M14"],
            "T3",
            vision_candidates=candidates,
            rank_fn=None,
            match_fn=None,
            allow_photo_templates=False,
            ceiling=20,
        )
        generated = [slide for slide in result.plan if getattr(slide, "source", "") == "generated"]
        self.assertTrue(generated)
        self.assertEqual(result.plan[-1].page, CLOSING_PAGE)
        gen_index = next(i for i, s in enumerate(result.plan) if getattr(s, "source", "") == "generated")
        self.assertLess(gen_index, len(result.plan) - 1)

    def _sectioned_plan(self, ceiling: int):
        from dataclasses import replace as dc_replace

        return [
            dc_replace(slide, section="Outcomes")
            if slide.page not in (COVER_PAGE, CLOSING_PAGE)
            else slide
            for slide in plan_pages(["M01", "M07", "M14"], ceiling)
        ]

    def test_t0_leaves_plan_untouched_even_with_vision(self):
        plan = plan_pages(["M01", "M07", "M14"], slide_ceiling_for("T0"))
        candidates = vision_candidates_from_instructions(
            [VisionInstruction("add", "Add slide: proof", None, "Outcomes", 0)]
        )
        result = build_gap_plan(
            plan, ["M01", "M07", "M14"], "T0",
            vision_candidates=candidates, rank_fn=None, match_fn=None,
        )
        self.assertEqual(result.plan, list(plan))
        self.assertEqual(result.generated, [])

    def test_replaced_page_is_not_replanted(self):
        plan = plan_pages(["M01", "M07", "M14"], 12)
        target = next(slide.page for slide in plan[1:-1] if slide.module_id == "M07")
        candidates = vision_candidates_from_instructions(
            [VisionInstruction("replace", "Redo placements", target, "Outcomes", 0)]
        )
        result = build_gap_plan(
            plan, ["M01", "M07", "M14"], "T3",
            vision_candidates=candidates, rank_fn=None, match_fn=None,
            allow_photo_templates=False,
        )
        brand_pages = [s.page for s in result.plan if getattr(s, "source", "brand") == "brand"]
        self.assertNotIn(target, brand_pages)

    def test_multi_page_brief_becomes_one_slide(self):
        plan = self._sectioned_plan(12)
        in_plan = [slide.page for slide in plan[1:-1]][:3]
        instructions = [
            VisionInstruction("replace", "Tweak slides", page, "Outcomes", 0) for page in [999, *in_plan]
        ]
        result = build_gap_plan(
            plan, ["M01", "M07", "M14"], "T3",
            vision_candidates=vision_candidates_from_instructions(instructions),
            rank_fn=None, match_fn=None, allow_photo_templates=False,
        )
        briefs = [g for g in result.generated if g.claim == "Tweak slides"]
        self.assertEqual(len(briefs), 1)
        self.assertEqual(briefs[0].replaced_page, in_plan[0])

    def test_add_at_ceiling_keeps_a_sole_section_slide(self):
        from dataclasses import replace as dc_replace

        plan = plan_pages(["M01", "M07", "M14"], 6)
        plan = [
            dc_replace(slide, section="Solo" if index == 1 else "Other")
            if slide.page not in (COVER_PAGE, CLOSING_PAGE)
            else slide
            for index, slide in enumerate(plan)
        ]
        solo_page = plan[1].page
        candidates = vision_candidates_from_instructions(
            [VisionInstruction("add", "Add slide: solo proof", None, "Solo", 0)]
        )
        result = build_gap_plan(
            plan, ["M01", "M07", "M14"], "T3",
            vision_candidates=candidates, rank_fn=None, match_fn=None,
            allow_photo_templates=False, ceiling=len(plan),
        )
        self.assertIn(solo_page, [getattr(s, "page", None) for s in result.plan])
        self.assertLessEqual(len(result.plan), len(plan))

    def test_design_only_produces_no_candidates(self):
        instructions = [VisionInstruction("design_only", "different image", None, "Campus", 0)]
        self.assertEqual(vision_candidates_from_instructions(instructions), [])

    def test_vision_budget_raised(self):
        base = generated_slide_budget("T3")
        raised = generated_slide_budget("T3", vision_count=6, ceiling=40)
        self.assertGreaterEqual(raised, base)
        self.assertGreaterEqual(raised, 6)
        capped = generated_slide_budget("T3", vision_count=20, ceiling=22)
        self.assertLessEqual(capped, max(base, 22 // 4))


class LegacyRegressionTests(unittest.TestCase):
    def test_unmapped_use_case_matches_plan_pages(self):
        sequence = ["M01", "M04", "M07", "M14"]
        legacy = plan_pages(sequence, slide_ceiling_for("T3"))
        # Empty vision sections → caller must fall back; plan_vision_pages with
        # no sections still bookends cover/closing only.
        empty = plan_vision_pages([], "T3", use_case="")
        self.assertEqual([s.page for s in empty.slides], [COVER_PAGE, CLOSING_PAGE])
        # The real regression: legacy plan_pages is unchanged.
        self.assertEqual(legacy[0].page, COVER_PAGE)
        self.assertEqual(legacy[-1].page, CLOSING_PAGE)
        self.assertGreater(len(legacy), 2)


class SeedVisionRowShapeTests(unittest.TestCase):
    def test_deck_vision_row_payload_round_trip(self):
        row = SimpleNamespace(
            use_case="school_fair",
            section_order=0,
            section="The Founding Story",
            pages_json=json.dumps([1, 2, 3]),
            section_pages_json=json.dumps([1, 2, 3, 4, 5, 6]),
            needs_more=False,
            logline="",
            instructions_json="[]",
        )
        from backend.pipeline.vision_deck import sections_from_db_rows

        sections = sections_from_db_rows([row])
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].pages, [1, 2, 3])
        self.assertEqual(sections[0].section_pages, [1, 2, 3, 4, 5, 6])

    def test_rule_instructions_recomputed_llm_kept(self):
        from backend.pipeline.vision_deck import sections_from_db_rows

        stale = json.dumps([{"action": "add", "brief": "Slide 67: replace", "source": "rules"}])
        llm = json.dumps([{"action": "add", "brief": "LLM brief", "source": "llm"}])
        base = dict(section="Campus", pages_json="[]", section_pages_json="[]", needs_more=True)
        rules_row = SimpleNamespace(
            use_case="school_fair", section_order=0, logline="Slide 67: replace with life @ mu",
            instructions_json=stale, **base,
        )
        llm_row = SimpleNamespace(
            use_case="school_fair", section_order=1, logline="Slide 67: replace with life @ mu",
            instructions_json=llm, **base,
        )
        rules_section, llm_section = sections_from_db_rows([rules_row, llm_row])
        self.assertEqual(
            [(i.action, i.target_page) for i in rules_section.instructions], [("replace", 67)]
        )
        self.assertEqual([i.brief for i in llm_section.instructions], ["LLM brief"])


if __name__ == "__main__":
    unittest.main()
