"""Stat-template (p58) specific Stage 6 tests.

The generic manifest/registry snapshots for the stat template live in
``test_slide_templates``; the generic Stage-4 fill/gate/render/degrade flow is
exercised with the section-divider and campus specs in ``test_slide_fill``.

This module pins the parts that are *specific to the stat template* and that the
task asks for explicitly, all without a browser:

* **Registry** — both variants resolve to coherent specs whose fillable/required
  slots exist, whose budgets are declared, and whose reference pages model p58
  (and never p22's oversized single-numeral treatment).
* **Slot budgets / provenance** — the exact per-slot word/char budgets bite on
  the stat spec, and every figure on the slide (hero + supporting rows) must
  trace to a locked fact / report passage or the slide is rejected.
* **Routing** — a ``stat-*`` placeholder flows through
  :func:`realize_generated_slides` to a persisted :class:`GeneratedSlideInstance`
  (cache miss -> fill -> gates -> render -> persist), and degrades to the brand
  page it displaced when a figure cannot be sourced.
* **Rendering** — :func:`build_slide_html` paints the hero figure in the brand
  **sans** while the supporting figures use the isolated italic **serif** token,
  and the per-slot character budget still rejects an over-long figure.
"""

from __future__ import annotations

import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import GeneratedSlide
from backend.pipeline.brand_deck import BrandSlide
from backend.pipeline.gaps import (
    CLOSING_PAGE,
    COVER_PAGE,
    GeneratedSlidePlaceholder,
)
from backend.pipeline.slide_fill import (
    GeneratedSlideInstance,
    gate_budgets,
    gate_number_provenance,
    gate_schema,
    realize_generated_slides,
)
from backend.pipeline.slide_render import (
    SERIF_FALLBACK_STACK,
    SlideOverflowError,
    build_slide_html,
)
from backend.pipeline.slide_templates import (
    STAT_ROW_COUNT,
    STAT_TEMPLATE_VERSION,
    stat_manifest,
    template_spec,
)

SLIDE_MODULE = "backend.pipeline.slide_fill"


def make_jpeg(color=(20, 20, 22)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def fact(fid: int, name: str, value: str, status: str = "verified") -> SimpleNamespace:
    return SimpleNamespace(id=fid, fact=name, value=value, status=status)


def stat_placeholder(**overrides) -> GeneratedSlidePlaceholder:
    base = dict(
        slide_key="generated:m07-placements",
        gap_kind="fact",
        claim="Our graduates land high-impact roles with strong compensation",
        title="Placements",
        module_id="M07",
        template_id="stat-dark",
        tone="dark",
        subtitle="",
        recipe_modules=("M07",),
        source_fact_ids=(1, 2, 3),
        replaced_page=51,
        replaced_module_id="M04",
        replaced_label="The five Outclass challenges",
    )
    base.update(overrides)
    return GeneratedSlidePlaceholder(**base)


# A well-formed stat fill: a sans hero figure, an italic-serif accent word in the
# title, and supporting rows whose figures all trace to the facts below.
_STAT_FILL = {
    "title": "Placements at the *union*",
    "subtitle": "Graduates step into high-impact roles",
    "hero_value": "3.03x",
    "hero_label": "Average salary increase from *pre-MBA* levels",
    "stat1_value": "27.78L",
    "stat1_label": "Median **CTC Secured**",
    "stat2_value": "33.39L",
    "stat2_label": "Average **CTC Secured**",
    "stat3_value": "",
    "stat3_label": "",
    "stat4_value": "",
    "stat4_label": "",
    "stat5_value": "",
    "stat5_label": "",
    "footnote": "Source: 2025 Placement Report",
}

# Locked facts that support every numeral used in ``_STAT_FILL``.
_STAT_FACTS = [
    fact(1, "Salary uplift", "3.03x average increase from pre-MBA levels"),
    fact(2, "Median CTC", "27.78L median CTC secured"),
    fact(3, "Average CTC", "33.39L average CTC; 2025 placement report"),
]


class StatRegistryTests(unittest.TestCase):
    def test_both_variants_resolve_to_coherent_specs(self):
        for template_id in ("stat-dark", "stat-light"):
            with self.subTest(template=template_id):
                spec = template_spec(template_id)
                self.assertEqual(spec.template_id, template_id)
                self.assertEqual(spec.version, STAT_TEMPLATE_VERSION)
                self.assertEqual(spec.manifest.template_id, template_id)
                text_names = {s.name for s in spec.manifest.text_slots}
                self.assertLessEqual(set(spec.fillable_slots), text_names)
                self.assertLessEqual(set(spec.required_slots), set(spec.fillable_slots))
                for name in spec.fillable_slots:
                    self.assertIn(name, spec.budgets, f"{name} has no budget")
                # The dark treatment may carry the approved full-bleed
                # placement image; light remains furniture-only.
                self.assertEqual(
                    spec.photo_slots,
                    ("background_photo",) if template_id == "stat-dark" else (),
                )
                self.assertFalse(spec.fillable_lists)
                self.assertFalse(spec.requires_photo)

    def test_hero_and_every_supporting_row_are_fillable(self):
        spec = template_spec("stat-dark")
        self.assertIn("hero_value", spec.required_slots)
        self.assertIn("hero_label", spec.required_slots)
        for i in range(1, STAT_ROW_COUNT + 1):
            self.assertIn(f"stat{i}_value", spec.fillable_slots)
            self.assertIn(f"stat{i}_label", spec.fillable_slots)

    def test_models_p58_and_avoids_p22_numeral_treatment(self):
        spec = template_spec("stat-dark")
        # Reference pages are the real p58 stat page (and its neighbour), never
        # p22, whose single oversized numeral is a different treatment.
        self.assertIn(58, spec.reference_pages)
        self.assertNotIn(22, spec.reference_pages)
        # The style exemplars carry short, unit-bearing figures rather than one
        # giant bare numeral.
        self.assertTrue(spec.exemplars)
        self.assertTrue(any(ch.isdigit() for ex in spec.exemplars for ch in ex))


class StatBudgetProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.spec = template_spec("stat-dark")

    def test_schema_gate_requires_hero_figure_and_label(self):
        violations = gate_schema(self.spec, {"title": "Placements"})
        self.assertTrue(any("hero_value" in v for v in violations))
        self.assertTrue(any("hero_label" in v for v in violations))
        self.assertFalse(gate_schema(self.spec, _STAT_FILL))

    def test_budget_gate_flags_an_over_long_hero_figure(self):
        values = {**_STAT_FILL, "hero_value": "3.03x per year uplift"}  # > 12 chars
        violations = gate_budgets(self.spec, values)
        self.assertTrue(any("hero_value" in v for v in violations))

    def test_budget_gate_passes_well_formed_copy(self):
        self.assertFalse(gate_budgets(self.spec, _STAT_FILL))

    def test_number_gate_rejects_an_unsupported_figure(self):
        allowed = {"3.03", "27.78"}  # 33.39 deliberately unsupported
        violations = gate_number_provenance(self.spec, _STAT_FILL, allowed)
        self.assertTrue(any("33.39" in v for v in violations))

    def test_number_gate_accepts_fully_sourced_figures(self):
        allowed = {"3.03", "27.78", "33.39", "2025"}
        self.assertFalse(gate_number_provenance(self.spec, _STAT_FILL, allowed))


class StatRenderingTests(unittest.TestCase):
    """`build_slide_html` — pure, no browser."""

    def _html(self, values=None):
        return build_slide_html(
            stat_manifest("dark"), values or _STAT_FILL, embed_fonts=False
        )

    def test_hero_is_sans_and_supporting_figures_are_italic_serif(self):
        html = self._html()
        # The isolated serif token is present (the supporting stat figures).
        self.assertIn(SERIF_FALLBACK_STACK, html)
        self.assertIn("3.03x", html)  # hero figure drawn
        self.assertIn("27.78L", html)  # a supporting figure drawn
        # The supporting figure box renders italic (its slot font is serif+italic).
        self.assertIn('data-slot="stat1_value"', html)
        self.assertIn("font-style:italic", html)

    def test_empty_supporting_rows_are_not_drawn(self):
        html = self._html()
        self.assertIn('data-slot="stat1_value"', html)
        self.assertNotIn('data-slot="stat3_value"', html)  # left blank -> omitted

    def test_title_accent_word_becomes_an_italic_serif_run(self):
        html = self._html()
        self.assertIn('<span class="emph">union</span>', html)

    def test_over_budget_hero_figure_is_rejected(self):
        values = {**_STAT_FILL, "hero_value": "3,030,000 rupees a year"}  # > char_budget
        with self.assertRaises(SlideOverflowError) as ctx:
            self._html(values)
        self.assertIn("hero_value", ctx.exception.slots)


class FakeRenderer:
    def __init__(self, jpeg: bytes | None = None, raises=None):
        self.jpeg = jpeg if jpeg is not None else make_jpeg()
        self.raises = raises
        self.calls: list[dict] = []

    def render(self, manifest, values, *, photos=None):
        self.calls.append(dict(values))
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(
            jpeg=self.jpeg, template_id=manifest.template_id, fitted_sizes={}
        )


class StatRoutingTests(unittest.TestCase):
    """A stat placeholder routes through Stage 4 exactly like any template."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def _realize(self, plan, *, fill_fn, vision_fn, renderer, facts, file_exists=True):
        with mock.patch(
            f"{SLIDE_MODULE}.save_generated_slide_image",
            return_value="generated-slides/stat.jpg",
        ) as save, mock.patch(f"{SLIDE_MODULE}.file_exists", return_value=file_exists):
            result = realize_generated_slides(
                self.db,
                plan,
                facts=facts,
                passages=[],
                fill_fn=fill_fn,
                vision_fn=vision_fn,
                renderer=renderer,
                brand_page_fn=lambda page: make_jpeg((10, 10, 10)),
            )
        return result, save

    def test_stat_placeholder_is_filled_rendered_and_persisted(self):
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            stat_placeholder(),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        renderer = FakeRenderer()
        fill = mock.Mock(return_value=_STAT_FILL)
        vision = mock.Mock(return_value={"passed": True, "violations": []})

        result, save = self._realize(
            plan, fill_fn=fill, vision_fn=vision, renderer=renderer, facts=_STAT_FACTS
        )

        slide = result[1]
        self.assertIsInstance(slide, GeneratedSlideInstance)
        self.assertEqual(slide.title, "Placements at the union")  # markup stripped
        save.assert_called_once()
        # The hero figure and a supporting figure both reached the renderer.
        self.assertEqual(renderer.calls[-1]["hero_value"], "3.03x")
        self.assertEqual(renderer.calls[-1]["stat1_value"], "27.78L")
        row = self.db.query(GeneratedSlide).one()
        self.assertEqual(row.template_id, "stat-dark")
        self.assertEqual(row.tone, "dark")
        self.assertEqual(row.template_version, STAT_TEMPLATE_VERSION)

    def test_unsupported_figure_degrades_to_the_displaced_brand_page(self):
        plan = [
            BrandSlide(COVER_PAGE, "", "Cover"),
            stat_placeholder(),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]
        renderer = FakeRenderer()
        # The model keeps inventing a figure no fact supports.
        bad = {**_STAT_FILL, "hero_value": "9.99x"}
        fill = mock.Mock(return_value=bad)
        vision = mock.Mock(return_value={"passed": True, "violations": []})

        result, save = self._realize(
            plan, fill_fn=fill, vision_fn=vision, renderer=renderer, facts=_STAT_FACTS
        )

        restored = result[1]
        self.assertIsInstance(restored, BrandSlide)
        self.assertEqual(restored.page, 51)  # the displaced brand page
        self.assertEqual(fill.call_count, 3)  # three attempts, all rejected
        self.assertEqual(renderer.calls, [])  # provenance gate precedes the render
        save.assert_not_called()
        self.assertEqual(self.db.query(GeneratedSlide).count(), 0)

    def test_second_stat_render_hits_the_shared_cache(self):
        renderer = FakeRenderer()
        fill = mock.Mock(return_value=_STAT_FILL)
        vision = mock.Mock(return_value={"passed": True, "violations": []})
        plan = lambda: [
            BrandSlide(COVER_PAGE, "", "Cover"),
            stat_placeholder(),
            BrandSlide(CLOSING_PAGE, "M14", "Close"),
        ]

        self._realize(plan(), fill_fn=fill, vision_fn=vision, renderer=renderer, facts=_STAT_FACTS)
        self.assertEqual(fill.call_count, 1)

        # A fresh plan for the same claim must reuse the row, no model/render.
        fill2 = mock.Mock(side_effect=AssertionError("model must not run on a cache hit"))
        vision2 = mock.Mock(side_effect=AssertionError("vision must not run on a cache hit"))
        renderer2 = FakeRenderer(raises=AssertionError("render must not run on a cache hit"))
        result, save2 = self._realize(
            plan(), fill_fn=fill2, vision_fn=vision2, renderer=renderer2, facts=_STAT_FACTS
        )
        self.assertIsInstance(result[1], GeneratedSlideInstance)
        save2.assert_not_called()
        self.assertEqual(self.db.query(GeneratedSlide).count(), 1)


if __name__ == "__main__":
    unittest.main()
