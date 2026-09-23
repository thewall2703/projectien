from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.pipeline.brand_deck import CLOSING_PAGE, COVER_PAGE, MODULE_PAGES, BrandSlide, plan_pages
from backend.pipeline.deck import GENERATED_SOURCE, PlannedSlide, slide_count_for
from backend.pipeline import gaps as gaps_module
from backend.pipeline.gaps import (
    DEFAULT_TEMPLATE_ID,
    NO_PHOTO_TEMPLATE_IDS,
    SUPPORTED_TEMPLATE_IDS,
    GeneratedSlidePlaceholder,
    allowed_template_ids,
    build_gap_plan,
    detect_gaps,
    generated_slide_budget,
    rank_and_template,
    rule_zero_page,
    weakest_body_index,
)
from backend.pipeline.slide_templates import PHOTO_REQUIRED_TEMPLATE_IDS
from backend.pipeline.script_flow import ScriptTopic, build_script_topics
from backend.pipeline.validator import align_script_to_topics, validate_script


def module(module_id: str, name: str = "", job: str = "") -> SimpleNamespace:
    return SimpleNamespace(id=module_id, name=name or module_id, job=job, core_content="")


def objection(oid: int, question: str, move: str = "") -> SimpleNamespace:
    return SimpleNamespace(id=oid, question=question, who_asks="", move=move)


def fact(fid: int, name: str, value: str, status: str, module_ids: str) -> SimpleNamespace:
    return SimpleNamespace(id=fid, fact=name, value=value, status=status, module_ids=module_ids)


def brand_pages(plan) -> list[int]:
    return [slide.page for slide in plan if slide.source == "brand"]


class BudgetTests(unittest.TestCase):
    def test_budget_is_slide_count_over_eight_and_zero_at_t0(self):
        self.assertEqual(generated_slide_budget("T0"), 0)
        self.assertEqual(generated_slide_budget("T1"), slide_count_for("T1") // 8)
        self.assertEqual(generated_slide_budget("T2"), 2)
        self.assertEqual(generated_slide_budget("T4"), slide_count_for("T4") // 8)

    def test_generation_never_exceeds_budget(self):
        # Three distinct, uncoverable context claims but a T1 budget of one.
        plan = [
            BrandSlide(COVER_PAGE, "", "Learn by Doing"),
            BrandSlide(6, "M01", "Hence born"),
            BrandSlide(2, "M01", "Origin story"),
            BrandSlide(4, "M01", "Medical students"),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        context = "Xylophone qwerty flumox. Bazinga quokka snorlax. Zibbit wobble frobnicate."
        result = build_gap_plan(plan, ["M01", "M14"], "T1", context_note=context)
        self.assertEqual(generated_slide_budget("T1"), 1)
        self.assertEqual(len(result.generated), 1)
        self.assertEqual(len(result.plan), len(plan))  # no extension


class NoOpTests(unittest.TestCase):
    def test_t0_is_left_exactly_as_planned(self):
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            BrandSlide(6, "M01", "Origin"),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        context = "Zibbit wobble frobnicate concerns nobody addressed anywhere."
        result = build_gap_plan(plan, ["M01", "M14"], "T0", context_note=context)
        self.assertEqual(result.plan, plan)
        self.assertEqual(result.generated, [])
        self.assertEqual(result.swapped_pages, [])

    def test_no_true_gap_leaves_the_plan_unchanged(self):
        plan = plan_pages(["M01", "M04", "M14"], slide_count_for("T2"))
        result = build_gap_plan(plan, ["M01", "M04", "M14"], "T2", context_note="")
        self.assertEqual(result.plan, plan)
        self.assertEqual(result.generated, [])
        self.assertEqual(result.swapped_pages, [])


class RuleZeroTests(unittest.TestCase):
    def test_module_gap_is_filled_with_a_real_unused_brand_page(self):
        # M09 is in the recipe but no M09 page was selected, and there is a
        # redundant M01 page to sacrifice. RULE ZERO must swap in a real page.
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            BrandSlide(6, "M01", "Hence born"),
            BrandSlide(2, "M01", "Origin story"),
            BrandSlide(4, "M01", "Medical students"),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        result = build_gap_plan(plan, ["M01", "M09", "M14"], "T2", context_note="")
        best_m09_page = MODULE_PAGES["M09"][0][0]
        self.assertEqual(result.swapped_pages, [best_m09_page])
        self.assertEqual(result.generated, [])  # a real page beats generation
        self.assertIn(best_m09_page, brand_pages(result.plan))
        self.assertEqual(len(result.plan), len(plan))
        # Cover and closing untouched.
        self.assertEqual(result.plan[0].page, COVER_PAGE)
        self.assertEqual(result.plan[-1].page, CLOSING_PAGE)

    def test_rule_zero_returns_none_when_no_label_matches(self):
        from backend.pipeline.gaps import GapCandidate

        candidate = GapCandidate(
            kind="context",
            key="context-0",
            claim="glorified weekend bootcamp reselling recycled moocs",
            title="what we are not",
            keywords=frozenset({"glorified", "weekend", "bootcamp", "reselling", "recycled", "moocs"}),
            priority=(3, 0),
        )
        self.assertIsNone(rule_zero_page(candidate, used_pages=set()))


class WeakestBodyTests(unittest.TestCase):
    def test_never_returns_cover_or_closing(self):
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            BrandSlide(6, "M01", "A"),
            BrandSlide(2, "M01", "B"),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        index = weakest_body_index(plan, {"M01", "M14"}, set())
        self.assertIn(index, (1, 2))

    def test_sole_carrier_of_a_recipe_module_is_never_removed(self):
        # Every body page is the only carrier of its recipe module -> nothing
        # can be replaced without opening a new gap.
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            BrandSlide(6, "M01", "Origin"),
            BrandSlide(51, "M04", "Outclass"),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        self.assertIsNone(weakest_body_index(plan, {"M01", "M04", "M14"}, set()))

    def test_off_recipe_top_up_is_weakest(self):
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            BrandSlide(6, "M01", "Origin"),  # sole carrier of a recipe module
            BrandSlide(67, "M12", "Student life"),  # off-recipe top-up
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        self.assertEqual(weakest_body_index(plan, {"M01", "M14"}, set()), 2)


class DetectionTests(unittest.TestCase):
    def test_verified_uncovered_fact_becomes_a_candidate(self):
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            BrandSlide(6, "M01", "Origin"),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        facts = [
            fact(1, "Endowment", "₹500 crore research endowment", "verified", "M08"),
            fact(2, "Draft claim", "unverified marketing puff", "needs_source", "M08"),
        ]
        candidates = detect_gaps(
            plan,
            ["M01", "M08", "M14"],
            context_note="",
            objections=[],
            facts=facts,
            module_info={},
        )
        kinds = {(candidate.kind, candidate.key) for candidate in candidates}
        # M08 has no selected carrier -> module gap; the verified fact rides M08
        # too and is de-duplicated behind the higher-priority module gap.
        self.assertIn(("module", "module-m08"), kinds)
        # The unverified fact is never a candidate.
        self.assertFalse(any(candidate.key == "fact-2" for candidate in candidates))

    def test_objection_answered_by_a_selected_slide_is_not_a_gap(self):
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            BrandSlide(58, "M07", "Placements median average outcomes"),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        objections = [objection(1, "What are the placement outcomes and median?", "Show placements")]
        candidates = detect_gaps(
            plan,
            ["M07", "M14"],
            context_note="",
            objections=objections,
            facts=[],
            module_info={},
        )
        self.assertFalse(any(candidate.kind == "objection" for candidate in candidates))


class CanonicalM13Tests(unittest.TestCase):
    """M13's "here is what we are *not*" — the brand deck has no such page."""

    def _plan_and_inputs(self):
        sequence = ["M01", "M04", "M07", "M13", "M14"]
        plan = plan_pages(sequence, slide_count_for("T2"))
        # M13 is carried by its recognition pages, but they answer a different
        # question than "what are you NOT"; no brand page argues that.
        self.assertIn("M13", {slide.module_id for slide in plan})
        context = "Here is what we are not: not a glorified weekend bootcamp reselling recycled MOOCs."
        modules = [module("M13", "The honest boundary", job="We are not a glorified bootcamp; we are a real institution")]
        return sequence, plan, context, modules

    def test_generates_one_placeholder_tied_to_m13(self):
        sequence, plan, context, modules = self._plan_and_inputs()
        result = build_gap_plan(plan, sequence, "T2", context_note=context, modules=modules)

        self.assertEqual(len(result.generated), 1)
        self.assertEqual(result.swapped_pages, [])  # brand deck cannot answer it
        placeholder = result.generated[0]
        self.assertEqual(placeholder.gap_kind, "context")
        self.assertEqual(placeholder.module_id, "M13")
        self.assertIn("bootcamp", placeholder.claim.lower())

        # No extension; cover and closing untouched.
        self.assertEqual(len(result.plan), len(plan))
        self.assertEqual(result.plan[0].page, COVER_PAGE)
        self.assertEqual(result.plan[-1].page, CLOSING_PAGE)

        # The placeholder replaced a body page (it appears once, in the middle).
        generated_positions = [i for i, s in enumerate(result.plan) if s.source == GENERATED_SOURCE]
        self.assertEqual(len(generated_positions), 1)
        self.assertNotIn(generated_positions[0], (0, len(result.plan) - 1))

    def test_placeholder_satisfies_planned_slide_contract(self):
        sequence, plan, context, modules = self._plan_and_inputs()
        result = build_gap_plan(plan, sequence, "T2", context_note=context, modules=modules)
        placeholder = result.generated[0]

        self.assertIsInstance(placeholder, PlannedSlide)
        self.assertEqual(placeholder.source, GENERATED_SOURCE)
        self.assertIsNone(placeholder.page)  # never a fake brand page
        self.assertEqual(placeholder.image_url, "")
        self.assertTrue(placeholder.slide_key.startswith("generated:"))
        # URL-safe key: only lowercase letters, digits and separators.
        key_body = placeholder.slide_key.split(":", 1)[1]
        self.assertRegex(key_body, r"^[a-z0-9][a-z0-9-]*$")
        self.assertIn(placeholder.template_id, SUPPORTED_TEMPLATE_IDS)
        metadata = placeholder.claim_metadata()
        self.assertEqual(metadata["slide_key"], placeholder.slide_key)
        self.assertEqual(metadata["template_id"], placeholder.template_id)

    def test_slide_keys_stay_unique_across_the_augmented_plan(self):
        sequence, plan, context, modules = self._plan_and_inputs()
        result = build_gap_plan(plan, sequence, "T2", context_note=context, modules=modules)
        keys = [slide.slide_key for slide in result.plan]
        self.assertEqual(len(keys), len(set(keys)))


class ModelRankingTests(unittest.TestCase):
    def _setup(self):
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            BrandSlide(6, "M01", "Hence born"),
            BrandSlide(2, "M01", "Origin story"),
            BrandSlide(4, "M01", "Medical students"),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        context = "Zibbit wobble frobnicate objection nobody has answered yet anywhere."
        return plan, context

    def test_model_can_select_a_supported_dark_template(self):
        plan, context = self._setup()
        dark = f"{SUPPORTED_TEMPLATE_IDS[0].rsplit('-', 1)[0]}-dark"

        def rank_fn(payload):
            key = payload["gaps"][0]["key"]
            return {"slides": [{"key": key, "template_id": dark}]}

        result = build_gap_plan(plan, ["M01", "M14"], "T2", context_note=context, rank_fn=rank_fn)
        self.assertEqual(len(result.generated), 1)
        self.assertEqual(result.generated[0].template_id, dark)
        self.assertEqual(result.generated[0].tone, "dark")

    def test_model_failure_falls_back_to_deterministic_generation(self):
        plan, context = self._setup()

        def rank_fn(payload):
            raise RuntimeError("model unavailable")

        result = build_gap_plan(plan, ["M01", "M14"], "T2", context_note=context, rank_fn=rank_fn)
        self.assertEqual(len(result.generated), 1)
        self.assertEqual(result.generated[0].template_id, DEFAULT_TEMPLATE_ID)

    def test_invalid_template_choice_falls_back_to_default(self):
        plan, context = self._setup()

        def rank_fn(payload):
            key = payload["gaps"][0]["key"]
            return {"slides": [{"key": key, "template_id": "carousel-9000"}]}

        result = build_gap_plan(plan, ["M01", "M14"], "T2", context_note=context, rank_fn=rank_fn)
        self.assertEqual(result.generated[0].template_id, DEFAULT_TEMPLATE_ID)

    def test_malformed_model_payload_falls_back(self):
        plan, context = self._setup()
        result = build_gap_plan(
            plan, ["M01", "M14"], "T2", context_note=context, rank_fn=lambda payload: {"nonsense": True}
        )
        self.assertEqual(len(result.generated), 1)
        self.assertEqual(result.generated[0].template_id, DEFAULT_TEMPLATE_ID)

    def test_objection_without_numbers_cannot_use_stat_template(self):
        from backend.pipeline.gaps import GapCandidate

        candidate = GapCandidate(
            kind="objection",
            key="objection-1",
            claim="Is this a real degree?",
            title="Is this a real degree?",
            keywords=frozenset({"real", "degree"}),
            priority=(1, 0),
        )
        picked = rank_and_template(
            [candidate],
            1,
            lambda payload: {
                "slides": [{"key": "objection-1", "template_id": "stat-dark"}]
            },
        )
        self.assertEqual(picked[0][1], "section-divider-dark")

    def test_numeric_fact_can_use_stat_template(self):
        from backend.pipeline.gaps import GapCandidate

        candidate = GapCandidate(
            kind="fact",
            key="fact-1",
            claim="Median CTC: 27.78 LPA",
            title="Median CTC",
            keywords=frozenset({"median", "ctc"}),
            priority=(2, 0),
            source_fact_ids=(1,),
        )
        picked = rank_and_template(
            [candidate],
            1,
            lambda payload: {
                "slides": [{"key": "fact-1", "template_id": "stat-light"}]
            },
        )
        self.assertEqual(picked[0][1], "stat-light")

    def test_ranker_receives_template_ids_with_purposes(self):
        from backend.pipeline.gaps import GapCandidate

        candidate = GapCandidate(
            kind="context",
            key="context-1",
            claim="A boundary the deck must explain",
            title="Boundary",
            keywords=frozenset({"boundary"}),
            priority=(3, 0),
        )

        def rank_fn(payload):
            self.assertEqual(
                {row["id"] for row in payload["templates"]},
                set(SUPPORTED_TEMPLATE_IDS),
            )
            self.assertTrue(all(row["purpose"] for row in payload["templates"]))
            return {
                "slides": [
                    {"key": "context-1", "template_id": DEFAULT_TEMPLATE_ID}
                ]
            }

        rank_and_template([candidate], 1, rank_fn)


class PhotoAwarenessTests(unittest.TestCase):
    """The planner may only pick a photo-required template when a photo exists."""

    CAMPUS = "campus-photo-dark"

    def _setup(self):
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            BrandSlide(6, "M01", "Hence born"),
            BrandSlide(2, "M01", "Origin story"),
            BrandSlide(4, "M01", "Medical students"),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        context = "Zibbit wobble frobnicate objection nobody has answered yet anywhere."
        return plan, context

    def test_allowed_ids_drop_photo_templates_when_no_photo(self):
        self.assertEqual(allowed_template_ids(True), SUPPORTED_TEMPLATE_IDS)
        allowed = allowed_template_ids(False)
        self.assertEqual(allowed, NO_PHOTO_TEMPLATE_IDS)
        self.assertIn(self.CAMPUS, PHOTO_REQUIRED_TEMPLATE_IDS)
        self.assertNotIn(self.CAMPUS, allowed)
        # No photo-required id survives into the no-photo set.
        self.assertFalse(set(allowed) & set(PHOTO_REQUIRED_TEMPLATE_IDS))

    def test_rank_and_template_rejects_photo_template_without_photos(self):
        from backend.pipeline.gaps import GapCandidate

        candidate = GapCandidate(
            kind="context", key="context-0", claim="a campus classroom moment",
            title="Campus", keywords=frozenset({"campus", "classroom"}), priority=(3, 0),
        )
        picked = rank_and_template(
            [candidate], 1,
            lambda payload: {"slides": [{"key": "context-0", "template_id": self.CAMPUS}]},
            allow_photo_templates=False,
        )
        self.assertEqual(picked[0][1], DEFAULT_TEMPLATE_ID)
        # The photo-required id is not even offered to the model.
        picked_ok = rank_and_template(
            [candidate], 1,
            lambda payload: (
                self.assertNotIn(
                    self.CAMPUS,
                    {template["id"] for template in payload["templates"]},
                )
                or {"slides": [{"key": "context-0", "template_id": DEFAULT_TEMPLATE_ID}]}
            ),
            allow_photo_templates=False,
        )
        self.assertEqual(picked_ok[0][1], DEFAULT_TEMPLATE_ID)

    def test_planner_can_pick_photo_template_when_photos_allowed(self):
        plan, context = self._setup()

        def rank_fn(payload):
            self.assertIn(
                self.CAMPUS,
                {template["id"] for template in payload["templates"]},
            )
            return {"slides": [{"key": payload["gaps"][0]["key"], "template_id": self.CAMPUS}]}

        result = build_gap_plan(
            plan, ["M01", "M14"], "T2", context_note=context,
            rank_fn=rank_fn, allow_photo_templates=True,
        )
        self.assertEqual(result.generated[0].template_id, self.CAMPUS)

    def test_planner_rejects_photo_template_when_photos_disallowed(self):
        plan, context = self._setup()

        def rank_fn(payload):
            return {"slides": [{"key": payload["gaps"][0]["key"], "template_id": self.CAMPUS}]}

        result = build_gap_plan(
            plan, ["M01", "M14"], "T2", context_note=context,
            rank_fn=rank_fn, allow_photo_templates=False,
        )
        self.assertEqual(result.generated[0].template_id, DEFAULT_TEMPLATE_ID)


class RecipePhotoLookupTests(unittest.TestCase):
    def test_blank_recipe_ref_is_treated_as_no_photo(self):
        self.assertFalse(gaps_module._recipe_has_approved_photos(object(), ""))

    def test_approved_picture_enables_photo_templates(self):
        import backend.media_index as media_index

        original = media_index.pick_recommended_media
        media_index.pick_recommended_media = lambda db, ref: ([], [{"asset_id": 1}])
        try:
            self.assertTrue(gaps_module._recipe_has_approved_photos(object(), "recipe-1"))
        finally:
            media_index.pick_recommended_media = original

    def test_media_lookup_failure_degrades_to_no_photo(self):
        import backend.media_index as media_index

        def boom(db, ref):
            raise RuntimeError("db down")

        original = media_index.pick_recommended_media
        media_index.pick_recommended_media = boom
        try:
            self.assertFalse(gaps_module._recipe_has_approved_photos(object(), "recipe-1"))
        finally:
            media_index.pick_recommended_media = original


class SyntheticTopicTests(unittest.TestCase):
    def _placeholder(self) -> GeneratedSlidePlaceholder:
        return GeneratedSlidePlaceholder(
            slide_key="generated:m13-what-we-are-not",
            gap_kind="context",
            claim="Here is what we are not: not a bootcamp.",
            title="What we are not",
            module_id="M13",
            recipe_modules=("M13",),
            summary="Here is what we are not: not a bootcamp.",
        )

    def test_generated_slide_becomes_its_own_beat_with_no_pages(self):
        placeholder = self._placeholder()
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            placeholder,
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        topics = [
            SimpleNamespace(id=1, title="Open", pages_json="[1]", summary="Start", vision="", module_ids=""),
            SimpleNamespace(id=9, title="Close", pages_json="[92]", summary="Ask", vision="", module_ids="M14"),
        ]
        flow = build_script_topics(plan, topics, ["M13", "M14"])
        self.assertEqual([topic.topic_id for topic in flow], [1, 2, 3])
        generated = flow[1]
        self.assertEqual(generated.pages, [])  # no fake brand page
        self.assertEqual(generated.slide_keys, ["generated:m13-what-we-are-not"])
        self.assertEqual(generated.recipe_modules, ["M13"])
        self.assertEqual(generated.title, "What we are not")
        # Empty pages must not break the prompt serialisation.
        self.assertEqual(generated.to_prompt_dict()["page_range"], "")

    def test_generated_topic_passes_order_and_coverage_validation(self):
        topics = [
            ScriptTopic(topic_id=1, title="Origin", pages=[6], recipe_modules=["M01"], slide_keys=["brand:p6"]),
            ScriptTopic(
                topic_id=2,
                title="What we are not",
                pages=[],
                recipe_modules=["M13"],
                slide_keys=["generated:m13-what-we-are-not"],
                summary="Here is what we are not.",
            ),
            ScriptTopic(topic_id=3, title="Close", pages=[92], recipe_modules=["M14"], slide_keys=["brand:p92"]),
        ]
        script = {"sections": [], "cta": "Come and see the campus this week."}
        aligned = align_script_to_topics(script, topics, [])
        violations = validate_script(aligned, [], ["M01", "M13", "M14"], 200, topics=topics)
        self.assertFalse(any("order" in item for item in violations))
        self.assertFalse(any("coverage" in item for item in violations))
        self.assertFalse(any("pages" in item for item in violations))
        # The generated beat exists and carries no pages.
        self.assertEqual([section["topic_id"] for section in aligned["sections"]], [1, 2, 3])
        self.assertEqual(aligned["sections"][1]["pages"], [])


if __name__ == "__main__":
    unittest.main()
