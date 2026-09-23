"""Tests for the section-divider template and its serialised fixtures."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend.pipeline.slide_render import (
    SERIF_FALLBACK_FAMILY,
    SlideManifest,
    build_slide_html,
)
from backend.pipeline.slide_templates import (
    DEFAULT_BRANDING_LEFT,
    DEFAULT_BRANDING_RIGHT,
    PHOTO_REQUIRED_TEMPLATE_IDS,
    STAT_ROW_COUNT,
    SUPPORTED_TEMPLATE_IDS,
    campus_photo_manifest,
    programme_list_manifest,
    section_divider_manifest,
    section_divider_values,
    stat_manifest,
    template_spec,
)

MANIFESTS_DIR = Path(__file__).resolve().parent / "manifests"


class SectionDividerTemplateTests(unittest.TestCase):
    def test_light_and_dark_variants_build(self):
        light = section_divider_manifest("light")
        dark = section_divider_manifest("dark")
        self.assertEqual(light.template_id, "section-divider-light")
        self.assertEqual(dark.template_id, "section-divider-dark")
        self.assertEqual(light.background, "#FFFFFF")
        self.assertNotEqual(dark.background, light.background)

    def test_unknown_variant_raises(self):
        with self.assertRaises(ValueError):
            section_divider_manifest("neon")  # type: ignore[arg-type]

    def test_expected_slots_present(self):
        manifest = section_divider_manifest("light")
        names = {s.name for s in manifest.text_slots}
        self.assertEqual(
            names, {"branding_left", "branding_right", "title", "subtitle"}
        )
        title = manifest.slot("title")
        self.assertTrue(title.required)
        self.assertGreater(title.font_size, title.min_font_size)

    def test_furniture_layer_references_cleaned_asset(self):
        manifest = section_divider_manifest("dark")
        assets = [l.asset for l in manifest.layers]
        self.assertIn("section-divider-dark.svg", assets)
        # The flattened furniture has no live text or photography. The one
        # embedded raster is the static lockup lifted from the real divider.
        svg = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "slide-templates"
            / "section-divider-dark.svg"
        ).read_text()
        self.assertNotIn("<text", svg)
        self.assertNotIn("data:image/png", svg)
        self.assertNotIn("data:image/jpeg", svg)
        self.assertIn('id="masters-union-lockup"', svg)
        self.assertEqual(svg.count("data:image/webp"), 1)

    def test_geometry_matches_known_rails_and_card(self):
        # Known geometry from the real divider pages, kept honest in the SVG.
        svg = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "slide-templates"
            / "section-divider-light.svg"
        ).read_text()
        self.assertIn('y1="116.75"', svg)
        self.assertIn('y1="959.75"', svg)
        # Brand ribbon treatment: pale yellow through gold to warm orange.
        self.assertIn('id="accent-ribbon"', svg)
        self.assertIn('stroke-width="72"', svg)
        self.assertIn("#FFF4BF", svg)
        self.assertIn("#E38330", svg)
        self.assertIn("#F7D344", svg)

    def test_values_helper_defaults_to_brand_furniture(self):
        values = section_divider_values("Immersions", "How students learn by traveling")
        self.assertEqual(values["branding_left"], DEFAULT_BRANDING_LEFT)
        self.assertEqual(values["branding_right"], DEFAULT_BRANDING_RIGHT)
        self.assertEqual(values["title"], "Immersions")

    def test_serif_token_is_isolated_fallback(self):
        # Until the licensed serif is bundled, emphasis resolves to the
        # high-contrast editorial fallback family, swappable in one place.
        html = build_slide_html(
            section_divider_manifest("light"),
            section_divider_values("*Immersions*", ""),
            embed_fonts=False,
        )
        self.assertIn(SERIF_FALLBACK_FAMILY, html)


class SectionDividerFixtureTests(unittest.TestCase):
    def test_serialised_manifests_match_template(self):
        for variant in ("light", "dark"):
            fixture = json.loads(
                (MANIFESTS_DIR / f"section_divider.{variant}.json").read_text()
            )
            live = section_divider_manifest(variant).to_dict()
            self.assertEqual(
                fixture,
                live,
                f"{variant} fixture drifted from template; regenerate it",
            )

    def test_fixture_round_trips_into_a_manifest(self):
        fixture = json.loads((MANIFESTS_DIR / "section_divider.light.json").read_text())
        manifest = SlideManifest.from_dict(fixture)
        self.assertEqual(manifest.template_id, "section-divider-light")

    def test_reconstruction_fixtures_are_well_formed(self):
        for name in ("p019.json", "p052.json"):
            fixture = json.loads((MANIFESTS_DIR / name).read_text())
            self.assertEqual(fixture["template"], "section-divider")
            self.assertIn("title", fixture["values"])
            self.assertIn("structural_regions", fixture["comparison"])
            self.assertIn("baseline", fixture["comparison"])


def _asset_path(name: str) -> Path:
    return (
        Path(__file__).resolve().parents[1] / "assets" / "slide-templates" / name
    )


class TemplateRegistryTests(unittest.TestCase):
    """The Stage 6 families must be registered and fully usable, not dangling."""

    def test_every_supported_id_builds_a_coherent_spec(self):
        for template_id in SUPPORTED_TEMPLATE_IDS:
            with self.subTest(template=template_id):
                spec = template_spec(template_id)
                self.assertEqual(spec.template_id, template_id)
                self.assertEqual(spec.manifest.template_id, template_id)
                text_names = {s.name for s in spec.manifest.text_slots}
                list_names = {s.name for s in spec.manifest.list_slots}
                photo_names = {p.name for p in spec.manifest.photo_slots}
                # Every fillable/required slot exists on the manifest.
                self.assertLessEqual(set(spec.fillable_slots), text_names)
                self.assertLessEqual(set(spec.required_slots), set(spec.fillable_slots))
                self.assertLessEqual(set(spec.fillable_lists), list_names)
                self.assertLessEqual(set(spec.photo_slots), photo_names)
                self.assertLessEqual(set(spec.required_photo_slots), photo_names)
                # Budgets are declared for every fillable text/list slot.
                for name in spec.fillable_slots:
                    self.assertIn(name, spec.budgets, f"{template_id}:{name} has no budget")
                for name in spec.fillable_lists:
                    self.assertIn(name, spec.list_budgets)
                # A version tag exists so the render hash changes with the template.
                self.assertTrue(spec.version)
                self.assertTrue(spec.reference_pages)
                self.assertTrue(spec.exemplars)
                self.assertTrue(spec.purpose)

    def test_only_stat_templates_require_numbers(self):
        number_templates = {
            template_id
            for template_id in SUPPORTED_TEMPLATE_IDS
            if template_spec(template_id).needs_numbers
        }
        self.assertEqual(number_templates, {"stat-dark", "stat-light"})

    def test_char_budget_matches_manifest_floor(self):
        for template_id in SUPPORTED_TEMPLATE_IDS:
            spec = template_spec(template_id)
            for name, budget in spec.budgets.items():
                slot = spec.manifest.slot(name)
                if slot.char_budget is not None:
                    self.assertLessEqual(
                        budget.max_chars,
                        slot.char_budget,
                        f"{template_id}:{name} budget exceeds render floor",
                    )

    def test_unsupported_id_raises(self):
        with self.assertRaises(KeyError):
            template_spec("does-not-exist")

    def test_photo_required_ids_are_a_subset_of_supported(self):
        self.assertLessEqual(set(PHOTO_REQUIRED_TEMPLATE_IDS), set(SUPPORTED_TEMPLATE_IDS))
        for template_id in PHOTO_REQUIRED_TEMPLATE_IDS:
            self.assertTrue(template_spec(template_id).requires_photo)
        # No non-photo-required template mandates a photo.
        for template_id in set(SUPPORTED_TEMPLATE_IDS) - set(PHOTO_REQUIRED_TEMPLATE_IDS):
            self.assertFalse(template_spec(template_id).requires_photo)

    def test_furniture_assets_exist_and_carry_no_live_text_or_photos(self):
        for template_id in SUPPORTED_TEMPLATE_IDS:
            manifest = template_spec(template_id).manifest
            for layer in manifest.layers:
                svg = _asset_path(layer.asset)
                self.assertTrue(svg.exists(), f"missing furniture: {layer.asset}")
                text = svg.read_text()
                self.assertNotIn("<text", text)
                self.assertNotIn("data:image/png", text)
                self.assertNotIn("data:image/jpeg", text)
                # Divider SVGs may contain exactly one transparent WebP:
                # the fixed Masters' Union lockup, never a content photo.
                webp_count = text.count("data:image/webp")
                expected = 1 if template_id.startswith("section-divider-") else 0
                self.assertEqual(webp_count, expected)


class StatTemplateTests(unittest.TestCase):
    def test_dark_and_light_build(self):
        self.assertEqual(stat_manifest("dark").template_id, "stat-dark")
        self.assertEqual(stat_manifest("light").template_id, "stat-light")

    def test_hero_plus_supporting_rows_present(self):
        manifest = stat_manifest("dark")
        names = {s.name for s in manifest.text_slots}
        self.assertIn("hero_value", names)
        self.assertIn("hero_label", names)
        for i in range(1, STAT_ROW_COUNT + 1):
            self.assertIn(f"stat{i}_value", names)
            self.assertIn(f"stat{i}_label", names)

    def test_stat_values_use_the_italic_serif_accent(self):
        manifest = stat_manifest("dark")
        value = manifest.slot("stat1_value")
        self.assertEqual(value.font, "serif")
        self.assertTrue(value.italic)
        # The hero figure and title remain the brand sans.
        self.assertEqual(manifest.slot("title").font, "sans")

    def test_snapshot_matches_live(self):
        fixture = json.loads((MANIFESTS_DIR / "stat.dark.json").read_text())
        self.assertEqual(fixture, stat_manifest("dark").to_dict())


class CampusTemplateTests(unittest.TestCase):
    def test_requires_a_photo_and_has_a_scrim(self):
        spec = template_spec("campus-photo-dark")
        self.assertTrue(spec.requires_photo)
        self.assertEqual(spec.required_photo_slots, ("hero_photo",))
        manifest = spec.manifest
        self.assertEqual(manifest.photo_slot("hero_photo").fit, "cover")
        self.assertTrue(manifest.photo_slot("hero_photo").required)
        self.assertTrue(manifest.gradient_layers, "campus template needs a scrim")

    def test_caption_sits_in_the_brand_band(self):
        # 7-11 words / 43-72 chars, enforced as a floor+ceiling.
        budget = template_spec("campus-photo-dark").budgets["caption"]
        self.assertEqual((budget.min_words, budget.max_words), (7, 11))
        self.assertEqual((budget.min_chars, budget.max_chars), (43, 72))

    def test_headline_is_italic_serif(self):
        headline = campus_photo_manifest().slot("headline")
        self.assertEqual(headline.font, "serif")
        self.assertTrue(headline.italic)

    def test_snapshot_matches_live(self):
        fixture = json.loads((MANIFESTS_DIR / "campus-photo.dark.json").read_text())
        self.assertEqual(fixture, campus_photo_manifest().to_dict())


class ProgrammeListTemplateTests(unittest.TestCase):
    def test_has_bounded_list_and_required_photos(self):
        spec = template_spec("programme-list-light")
        self.assertEqual(spec.fillable_lists, ("items",))
        self.assertEqual(spec.list_budgets["items"].max_items, 6)
        self.assertEqual(spec.photo_slots, ("photo_1", "photo_2"))
        self.assertEqual(spec.required_photo_slots, spec.photo_slots)
        self.assertTrue(spec.requires_photo)
        self.assertTrue(all(slot.required for slot in spec.manifest.photo_slots))

    def test_programme_type_is_italic_serif(self):
        prog = programme_list_manifest("light").slot("programme_type")
        self.assertEqual(prog.font, "serif")
        self.assertTrue(prog.italic)

    def test_blue_variant_builds(self):
        self.assertEqual(
            programme_list_manifest("blue").template_id, "programme-list-blue"
        )

    def test_list_slot_is_ring_bulleted_and_bounded(self):
        items = programme_list_manifest("light").list_slot("items")
        self.assertEqual(items.bullet, "ring")
        self.assertEqual(items.max_items, 6)
        self.assertIsNotNone(items.char_budget_per_item)

    def test_snapshot_matches_live(self):
        fixture = json.loads((MANIFESTS_DIR / "programme-list.light.json").read_text())
        self.assertEqual(fixture, programme_list_manifest("light").to_dict())


if __name__ == "__main__":
    unittest.main()
