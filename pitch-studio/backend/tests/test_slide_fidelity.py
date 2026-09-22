"""Fidelity gate: rebuild real divider pages and pixel-diff the reconstruction.

The reconstruction is compared to the cached brand-deck page two ways (see
:mod:`backend.tests.slide_fidelity`): a hard *structural* gate over the shared
background/grid frame, and an informational *text* metric over the title (loose,
because the licensed serif is not bundled and the source overlays a raster brush
behind the type). The render itself is skipped — and only the render — when
Playwright/Chromium is unavailable; fixture wiring still gets checked.
"""

from __future__ import annotations

import unittest
from functools import lru_cache

from backend.pipeline import slide_render
from backend.tests import slide_fidelity


@lru_cache(maxsize=1)
def _browser_available() -> bool:
    return slide_render.playwright_available()


BROWSER_REASON = "Playwright/Chromium not installed (integration render skipped)"


class FidelityFixtureTests(unittest.TestCase):
    """Cheap checks that run without a browser."""

    def test_reference_pages_exist(self):
        for name in slide_fidelity.FIXTURES:
            fixture = slide_fidelity.load_fixture(name)
            path = slide_fidelity.reference_path(fixture)
            self.assertTrue(path.exists(), f"missing cached reference page: {path}")

    def test_fixtures_bind_to_a_known_template(self):
        for name in slide_fidelity.FIXTURES:
            fixture = slide_fidelity.load_fixture(name)
            manifest = slide_fidelity.manifest_for(fixture)
            text_names = {s.name for s in manifest.text_slots}
            list_names = {s.name for s in manifest.list_slots}
            photo_names = {s.name for s in manifest.photo_slots}
            for key in fixture["values"]:
                self.assertIn(
                    key,
                    text_names | list_names,
                    f"{name}: value {key!r} has no text/list slot",
                )
            for key in fixture.get("photos", {}):
                self.assertIn(
                    key, photo_names, f"{name}: photo {key!r} has no photo slot"
                )

    def test_photo_fixtures_only_reference_local_bytes(self):
        # No base64 lives in the fixtures; photo bytes must come from a local
        # file (the cached page, or an approved asset path under FILES_DIR).
        for name in slide_fidelity.FIXTURES:
            fixture = slide_fidelity.load_fixture(name)
            photos = slide_fidelity._photos_for(fixture)
            if photos is None:
                continue
            for slot, data in photos.items():
                self.assertIsInstance(data, (bytes, bytearray))
                self.assertGreater(len(data), 0, f"{name}: empty photo for {slot}")


@unittest.skipUnless(_browser_available(), BROWSER_REASON)
class FidelityRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metrics = {}
        for name in slide_fidelity.FIXTURES:
            fixture = slide_fidelity.load_fixture(name)
            recon = slide_render.get_renderer().render(
                slide_fidelity.manifest_for(fixture),
                fixture["values"],
                photos=slide_fidelity._photos_for(fixture),
            ).jpeg
            cls.metrics[name] = (fixture, slide_fidelity.compare(recon, fixture))

    @classmethod
    def tearDownClass(cls):
        slide_render.shutdown_renderer()

    def test_structural_frame_matches_within_tolerance(self):
        for name, (fixture, metrics) in self.metrics.items():
            with self.subTest(page=metrics.page):
                self.assertLessEqual(
                    metrics.structural_mad,
                    metrics.structural_mad_max,
                    f"{name}: structural MAD {metrics.structural_mad:.2f} exceeds "
                    f"{metrics.structural_mad_max} — furniture/registration regressed",
                )

    def test_reconstruction_is_not_blank(self):
        # The loose text bound catches a catastrophic (blank/garbage) render even
        # though the serif is a fallback.
        for name, (fixture, metrics) in self.metrics.items():
            with self.subTest(page=metrics.page):
                self.assertLessEqual(metrics.text_mad, metrics.text_mad_max)
                self.assertGreater(metrics.full_mad, 0.0)

    def test_structural_fidelity_tracks_recorded_baseline(self):
        # Where a fixture records a numeric structural baseline (the dividers,
        # measured on a browser), assert we stay within a small margin of it so a
        # silent furniture shift is caught early rather than drifting to the cap.
        # Fixtures whose serif/photo is still provisional record only a string
        # note and are gated solely by the tolerance test above.
        for name, (fixture, metrics) in self.metrics.items():
            baseline = fixture["comparison"].get("baseline", {})
            recorded = baseline.get("structural_mad")
            if not isinstance(recorded, (int, float)):
                continue
            with self.subTest(page=metrics.page):
                self.assertLess(metrics.structural_mad, float(recorded) + 2.0)


if __name__ == "__main__":
    unittest.main()
