"""Tests for the layered slide renderer.

Pure tests (manifest (de)serialisation, HTML building, escaping, character
budgets) run everywhere. Integration tests that need a real browser are the
only ones skipped when Playwright/Chromium is unavailable, with a clear reason.
"""

from __future__ import annotations

import io
import unittest
from functools import lru_cache

from backend.pipeline import slide_render
from backend.pipeline.slide_render import (
    FONT_STACKS,
    SERIF_FALLBACK_STACK,
    SLIDE_HEIGHT,
    SLIDE_WIDTH,
    GradientLayer,
    ListSlot,
    PhotoSlot,
    Rect,
    SlideManifest,
    SlideOverflowError,
    SlideRenderError,
    SlideRenderer,
    SvgLayer,
    TextSlot,
    _photo_data_uri,
    _strip_markup,
    build_slide_html,
)

# 1x1 pixels, smallest valid encodings, used as approved "photo" bytes.
_PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
    b"\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
    b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
_JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 16


@lru_cache(maxsize=1)
def _browser_available() -> bool:
    return slide_render.playwright_available()


BROWSER_REASON = "Playwright/Chromium not installed (integration render skipped)"


def _simple_manifest(**slot_kwargs) -> SlideManifest:
    return SlideManifest(
        template_id="unit",
        layers=(),
        text_slots=(
            TextSlot(name="title", rect=Rect(96, 300, 1200, 200), **slot_kwargs),
        ),
        background="#FFFFFF",
    )


class ManifestSerializationTests(unittest.TestCase):
    def test_round_trips_through_dict(self):
        manifest = SlideManifest(
            template_id="demo",
            layers=(SvgLayer(name="bg", asset="section-divider-light.svg", opacity=0.79),),
            text_slots=(
                TextSlot(
                    name="title",
                    rect=Rect(96, 300, 1216, 232),
                    font="sans",
                    font_size=104,
                    min_font_size=44,
                    weight=700,
                    tracking_em=-0.01,
                    color="#111111",
                    char_budget=120,
                    required=True,
                ),
                TextSlot(name="subtitle", rect=Rect(100, 548, 900, 60), font_size=34),
            ),
            background="#FFFFFF",
        )
        restored = SlideManifest.from_dict(manifest.to_dict())
        self.assertEqual(restored.to_dict(), manifest.to_dict())
        self.assertEqual(restored.template_id, "demo")
        self.assertEqual(restored.slot("title").char_budget, 120)
        self.assertTrue(restored.slot("title").required)

    def test_slot_lookup_raises_for_unknown_name(self):
        with self.assertRaises(KeyError):
            _simple_manifest().slot("nope")


class HtmlBuildingTests(unittest.TestCase):
    def test_output_is_pinned_to_slide_dimensions(self):
        html = build_slide_html(_simple_manifest(), {"title": "Hi"}, embed_fonts=False)
        self.assertIn(f"width:{SLIDE_WIDTH}px", html)
        self.assertIn(f"height:{SLIDE_HEIGHT}px", html)

    def test_escapes_html_in_values(self):
        html = build_slide_html(
            _simple_manifest(), {"title": "<script>alert('x')</script>"}, embed_fonts=False
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_emphasis_markup_becomes_italic_serif_run(self):
        html = build_slide_html(
            _simple_manifest(), {"title": "How students *learn by doing*"}, embed_fonts=False
        )
        self.assertIn('<span class="emph">learn by doing</span>', html)
        # the emphasis class is wired to the (isolated, swappable) serif token
        self.assertIn(SERIF_FALLBACK_STACK, html)

    def test_emphasis_content_is_escaped_before_wrapping(self):
        html = build_slide_html(
            _simple_manifest(), {"title": "danger *<b>x</b>*"}, embed_fonts=False
        )
        self.assertIn('<span class="emph">&lt;b&gt;x&lt;/b&gt;</span>', html)
        self.assertNotIn("<b>x</b>", html)

    def test_empty_optional_slot_is_not_drawn(self):
        manifest = SlideManifest(
            template_id="unit",
            text_slots=(
                TextSlot(name="title", rect=Rect(0, 0, 100, 100)),
                TextSlot(name="subtitle", rect=Rect(0, 100, 100, 100)),
            ),
        )
        html = build_slide_html(manifest, {"title": "x", "subtitle": ""}, embed_fonts=False)
        self.assertIn('data-slot="title"', html)
        self.assertNotIn('data-slot="subtitle"', html)

    def test_char_budget_overflow_is_rejected(self):
        manifest = _simple_manifest(char_budget=10)
        with self.assertRaises(SlideOverflowError) as ctx:
            build_slide_html(manifest, {"title": "way too many characters here"})
        self.assertIn("title", ctx.exception.slots)

    def test_missing_required_slot_is_rejected(self):
        manifest = _simple_manifest(required=True)
        with self.assertRaises(SlideRenderError):
            build_slide_html(manifest, {"title": "   "})

    def test_font_tokens_present(self):
        self.assertIn("Galano Grotesque Alt", FONT_STACKS["sans"])
        self.assertEqual(FONT_STACKS["serif"], SERIF_FALLBACK_STACK)

    def test_embedded_fonts_present_when_requested(self):
        html = build_slide_html(_simple_manifest(), {"title": "x"}, embed_fonts=True)
        self.assertIn("@font-face", html)
        self.assertIn("data:font/otf;base64,", html)

    def test_missing_furniture_asset_raises(self):
        manifest = SlideManifest(
            template_id="unit",
            layers=(SvgLayer(name="bg", asset="does-not-exist.svg"),),
            text_slots=(),
        )
        with self.assertRaises(SlideRenderError):
            build_slide_html(manifest, {}, embed_fonts=False)


class InlineMarkupTests(unittest.TestCase):
    def test_strong_markup_becomes_bold_sans_run(self):
        html = build_slide_html(
            _simple_manifest(), {"title": "UG in **Data Science**"}, embed_fonts=False
        )
        self.assertIn('<span class="strong">Data Science</span>', html)
        self.assertIn(
            ".content .strong{font-weight:700;color:var(--strong-color, inherit);}",
            html,
        )

    def test_strong_and_emphasis_coexist(self):
        html = build_slide_html(
            _simple_manifest(),
            {"title": "**Bold** and *serif*"},
            embed_fonts=False,
        )
        self.assertIn('<span class="strong">Bold</span>', html)
        self.assertIn('<span class="emph">serif</span>', html)

    def test_strip_markup_drops_delimiters_for_budgeting(self):
        # The character budget counts the *visible* copy, not the markup.
        self.assertEqual(_strip_markup("UG in **Data Science & AI**"), "UG in Data Science & AI")
        self.assertEqual(_strip_markup("the *union*"), "the union")

    def test_strong_markup_is_not_split_into_two_emphasis_runs(self):
        html = build_slide_html(
            _simple_manifest(), {"title": "**x**"}, embed_fonts=False
        )
        self.assertIn('<span class="strong">x</span>', html)
        self.assertNotIn('<span class="emph">', html)


class PhotoDataUriTests(unittest.TestCase):
    def test_jpeg_bytes_become_jpeg_data_uri(self):
        uri = _photo_data_uri(_JPEG_MAGIC)
        self.assertTrue(uri.startswith("data:image/jpeg;base64,"))

    def test_png_bytes_are_detected_by_magic(self):
        uri = _photo_data_uri(_PNG_1PX)
        self.assertTrue(uri.startswith("data:image/png;base64,"))

    def test_existing_data_uri_passes_through(self):
        original = "data:image/png;base64,AAAA"
        self.assertEqual(_photo_data_uri(original), original)

    def test_remote_url_is_rejected(self):
        # A render must never depend on a browser network fetch.
        with self.assertRaises(SlideRenderError):
            _photo_data_uri("https://example.com/photo.jpg")

    def test_empty_bytes_are_rejected(self):
        with self.assertRaises(SlideRenderError):
            _photo_data_uri(b"")


def _photo_manifest(**photo_kwargs) -> SlideManifest:
    defaults = dict(name="hero", rect=Rect(0, 0, 1920, 1080))
    defaults.update(photo_kwargs)
    return SlideManifest(
        template_id="photo-unit",
        photo_slots=(PhotoSlot(**defaults),),
        text_slots=(TextSlot(name="cap", rect=Rect(60, 60, 800, 100)),),
        background="#000000",
    )


class PhotoLayerTests(unittest.TestCase):
    def test_photo_bytes_are_embedded_as_data_uri(self):
        html = build_slide_html(
            _photo_manifest(), {"cap": "x"}, photos={"hero": _JPEG_MAGIC}, embed_fonts=False
        )
        self.assertIn("data:image/jpeg;base64,", html)
        self.assertIn('class="layer photo"', html)

    def test_focus_and_fit_drive_object_position(self):
        html = build_slide_html(
            _photo_manifest(focus_x=0.25, focus_y=0.8, fit="cover"),
            {"cap": "x"},
            photos={"hero": _JPEG_MAGIC},
            embed_fonts=False,
        )
        self.assertIn("object-fit:cover", html)
        self.assertIn("object-position:25.00% 80.00%", html)

    def test_frame_and_rotation_render_polaroid_furniture(self):
        html = build_slide_html(
            _photo_manifest(rotation_deg=-4.0, frame=True, frame_width=12.0, shadow=True),
            {"cap": "x"},
            photos={"hero": _JPEG_MAGIC},
            embed_fonts=False,
        )
        self.assertIn("transform:rotate(-4.0deg)", html)
        self.assertIn("box-shadow:", html)
        self.assertIn("padding:12px", html)

    def test_missing_required_photo_is_rejected(self):
        with self.assertRaises(SlideRenderError):
            build_slide_html(
                _photo_manifest(required=True), {"cap": "x"}, photos={}, embed_fonts=False
            )

    def test_optional_photo_absent_is_not_drawn(self):
        html = build_slide_html(
            _photo_manifest(required=False), {"cap": "x"}, photos={}, embed_fonts=False
        )
        self.assertNotIn('class="layer photo"', html)

    def test_photo_sits_below_text_and_gradient_above_photo(self):
        manifest = SlideManifest(
            template_id="z-order",
            photo_slots=(PhotoSlot(name="hero", rect=Rect(0, 0, 1920, 1080), z=5),),
            gradient_layers=(
                GradientLayer(
                    name="scrim",
                    rect=Rect(0, 0, 1920, 470),
                    css="linear-gradient(180deg, rgba(0,0,0,0.8) 0%, rgba(0,0,0,0) 100%)",
                    z=20,
                ),
            ),
            text_slots=(TextSlot(name="cap", rect=Rect(60, 60, 800, 100)),),
        )
        html = build_slide_html(manifest, {"cap": "hi"}, photos={"hero": _JPEG_MAGIC}, embed_fonts=False)
        photo_at = html.index('class="layer photo"')
        grad_at = html.index("linear-gradient")
        # Paint order in the document: photo (z5) before gradient (z20); text
        # copy always carries a higher z-index than any visual layer.
        self.assertLess(photo_at, grad_at)
        self.assertIn(f"z-index:{slide_render._TEXT_Z}", html)

    def test_crop_metadata_is_deterministic_descriptor(self):
        slot = PhotoSlot(name="hero", rect=Rect(0, 0, 1920, 1080), focus_x=0.3333333, fit="cover")
        meta = slot.crop_metadata()
        self.assertEqual(meta["fit"], "cover")
        self.assertEqual(meta["focus_x"], 0.3333)
        self.assertEqual(meta["rect"], [0, 0, 1920, 1080])
        self.assertEqual(meta, slot.crop_metadata())


class GradientLayerTests(unittest.TestCase):
    def test_valid_linear_gradient_builds(self):
        g = GradientLayer(
            name="scrim",
            rect=Rect(0, 0, 1920, 470),
            css="linear-gradient(180deg, rgba(0,0,0,0.8) 0%, rgba(0,0,0,0) 100%)",
        )
        self.assertIn("linear-gradient", g.css)

    def test_unsafe_css_is_rejected_at_construction(self):
        with self.assertRaises(SlideRenderError):
            GradientLayer(name="x", rect=Rect(0, 0, 10, 10), css="url(javascript:alert(1))")

    def test_gradient_is_painted_as_background(self):
        manifest = SlideManifest(
            template_id="grad",
            gradient_layers=(
                GradientLayer(
                    name="wash",
                    rect=Rect(0, 0, 1920, 1080),
                    css="linear-gradient(90deg, #fff 0%, #000 100%)",
                ),
            ),
            text_slots=(TextSlot(name="t", rect=Rect(0, 0, 100, 100)),),
        )
        html = build_slide_html(manifest, {"t": "x"}, embed_fonts=False)
        self.assertIn("background:linear-gradient(90deg, #fff 0%, #000 100%)", html)


def _list_manifest(**list_kwargs) -> SlideManifest:
    defaults = dict(
        name="items",
        rect=Rect(100, 240, 340, 260),
        max_items=6,
        item_height=42,
        divider=True,
        bullet="ring",
        char_budget_per_item=48,
    )
    defaults.update(list_kwargs)
    return SlideManifest(
        template_id="list-unit",
        text_slots=(TextSlot(name="title", rect=Rect(100, 90, 330, 60)),),
        list_slots=(ListSlot(**defaults),),
        background="#FFFFFF",
    )


class ListSlotTests(unittest.TestCase):
    def test_each_item_becomes_a_measured_row_with_bullet(self):
        html = build_slide_html(
            _list_manifest(),
            {"title": "Undergraduate", "items": ["UG in **A**", "UG in **B**", "UG in **C**"]},
            embed_fonts=False,
        )
        self.assertEqual(html.count('class="bullet"'), 3)
        self.assertEqual(html.count('data-slot="items#'), 3)
        # A hair divider between rows: n-1 for 3 items.
        self.assertEqual(html.count('class="divider"'), 2)

    def test_items_are_capped_at_max_items(self):
        html = build_slide_html(
            _list_manifest(max_items=2),
            {"title": "x", "items": ["one", "two", "three", "four"]},
            embed_fonts=False,
        )
        self.assertEqual(html.count('data-slot="items#'), 2)

    def test_blank_items_are_skipped(self):
        html = build_slide_html(
            _list_manifest(),
            {"title": "x", "items": ["kept", "  ", "", "also kept"]},
            embed_fonts=False,
        )
        self.assertEqual(html.count('data-slot="items#'), 2)

    def test_per_item_char_budget_counts_visible_copy_only(self):
        # 45 visible chars is under budget even though the markup makes it longer.
        ok = "UG in **" + "a" * 37 + "**"  # 43 visible chars
        html = build_slide_html(
            _list_manifest(char_budget_per_item=48),
            {"title": "x", "items": [ok]},
            embed_fonts=False,
        )
        self.assertIn('data-slot="items#0"', html)

    def test_per_item_char_budget_overflow_is_rejected(self):
        with self.assertRaises(SlideOverflowError) as ctx:
            build_slide_html(
                _list_manifest(char_budget_per_item=10),
                {"title": "x", "items": ["a short one", "way too many characters here"]},
                embed_fonts=False,
            )
        self.assertIn("items#1", ctx.exception.slots)
        self.assertIn("items#1", str(ctx.exception))

    def test_required_empty_list_is_rejected(self):
        with self.assertRaises(SlideRenderError):
            build_slide_html(
                _list_manifest(required=True),
                {"title": "x", "items": ["", "   "]},
                embed_fonts=False,
            )

    def test_bullet_none_draws_no_bullets(self):
        html = build_slide_html(
            _list_manifest(bullet="none"),
            {"title": "x", "items": ["one", "two"]},
            embed_fonts=False,
        )
        self.assertNotIn('class="bullet"', html)


class Stage6SerializationTests(unittest.TestCase):
    def test_round_trips_photo_gradient_and_list_slots(self):
        manifest = SlideManifest(
            template_id="stage6",
            photo_slots=(
                PhotoSlot(
                    name="hero", rect=Rect(0, 0, 1920, 1080), fit="cover",
                    focus_x=0.4, rotation_deg=-4.0, frame=True, frame_width=12.0,
                    shadow=True, required=True, z=5,
                ),
            ),
            gradient_layers=(
                GradientLayer(
                    name="scrim", rect=Rect(0, 0, 1920, 470),
                    css="linear-gradient(180deg, rgba(0,0,0,0.8) 0%, rgba(0,0,0,0) 100%)",
                    z=20,
                ),
            ),
            list_slots=(
                ListSlot(
                    name="items", rect=Rect(100, 240, 340, 260), max_items=6,
                    item_height=42, char_budget_per_item=48, required=True,
                ),
            ),
            text_slots=(TextSlot(name="t", rect=Rect(0, 0, 100, 100)),),
        )
        restored = SlideManifest.from_dict(manifest.to_dict())
        self.assertEqual(restored.to_dict(), manifest.to_dict())
        self.assertEqual(restored.photo_slot("hero").focus_x, 0.4)
        self.assertTrue(restored.photo_slot("hero").required)
        self.assertEqual(restored.list_slot("items").max_items, 6)
        self.assertEqual(len(restored.required_photo_slots), 1)


@unittest.skipUnless(_browser_available(), BROWSER_REASON)
class RenderIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = SlideRenderer()
        cls.renderer.start()

    @classmethod
    def tearDownClass(cls):
        cls.renderer.close()

    def _dims(self, jpeg: bytes):
        from PIL import Image

        return Image.open(io.BytesIO(jpeg)).size

    def test_renders_full_hd_jpeg(self):
        result = self.renderer.render(_simple_manifest(), {"title": "Hello"})
        self.assertEqual(self._dims(result.jpeg), (SLIDE_WIDTH, SLIDE_HEIGHT))
        self.assertEqual(result.jpeg[:2], b"\xff\xd8")  # JPEG SOI marker

    def test_output_is_deterministic(self):
        a = self.renderer.render(_simple_manifest(), {"title": "Immersions"})
        b = self.renderer.render(_simple_manifest(), {"title": "Immersions"})
        self.assertEqual(a.jpeg, b.jpeg)

    def test_renders_a_photo_slide_at_full_hd(self):
        result = self.renderer.render(
            _photo_manifest(), {"cap": "Classrooms"}, photos={"hero": _PNG_1PX}
        )
        self.assertEqual(self._dims(result.jpeg), (SLIDE_WIDTH, SLIDE_HEIGHT))

    def test_autofit_shrinks_but_does_not_exceed_max(self):
        manifest = _simple_manifest(font_size=140, min_font_size=30)
        short = self.renderer.render(manifest, {"title": "Hi"})
        long = self.renderer.render(
            manifest, {"title": "A considerably longer headline that must wrap"}
        )
        self.assertLessEqual(short.fitted_sizes["title"], 140)
        self.assertLess(long.fitted_sizes["title"], short.fitted_sizes["title"])
        self.assertGreaterEqual(long.fitted_sizes["title"], 30)

    def test_overflow_below_floor_is_rejected(self):
        manifest = SlideManifest(
            template_id="tiny",
            text_slots=(
                TextSlot(name="title", rect=Rect(0, 0, 120, 40), font_size=90, min_font_size=70),
            ),
        )
        with self.assertRaises(SlideOverflowError):
            self.renderer.render(
                manifest, {"title": "This headline cannot possibly fit in a tiny box"}
            )

    def test_warm_renderer_is_reused_across_renders(self):
        thread_before = self.renderer._thread
        self.renderer.render(_simple_manifest(), {"title": "one"})
        self.renderer.render(_simple_manifest(), {"title": "two"})
        self.assertIs(self.renderer._thread, thread_before)
        self.assertTrue(thread_before.is_alive())


@unittest.skipUnless(_browser_available(), BROWSER_REASON)
class RendererLifecycleTests(unittest.TestCase):
    def test_module_singleton_start_and_shutdown(self):
        r1 = slide_render.get_renderer()
        r2 = slide_render.get_renderer()
        self.assertIs(r1, r2)
        self.assertTrue(r1._thread.is_alive())
        slide_render.shutdown_renderer()
        self.assertIsNone(r1._thread)


if __name__ == "__main__":
    unittest.main()
