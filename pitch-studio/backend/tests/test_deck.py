from __future__ import annotations

import unittest
from unittest import mock

from backend.config import settings
from backend.pipeline.deck import DeckSlide, DeckSpec, render_pptx


class DeckRenderTests(unittest.TestCase):
    def test_renders_layouts(self):
        spec = DeckSpec(
            slides=[
                DeckSlide(layout="title", title="Masters' Union", subtitle="Pitch", module_id="M01"),
                DeckSlide(layout="unknown", title="Fallback", bullets=["One", "Two"], module_id="M04"),
                DeckSlide(layout="cta", title="Apply", subtitle="admissions.mastersunion.org", module_id="M14"),
            ]
        )
        # Force local filesystem storage so the test is hermetic regardless of
        # whether Spaces/S3 credentials are configured in the environment.
        with mock.patch.object(type(settings), "uses_spaces", property(lambda self: False)):
            path = render_pptx(spec, 999001, ["M01", "M04", "M14"])
        self.assertTrue(path)
        from pptx import Presentation

        deck = Presentation(path)
        self.assertEqual(len(deck.slides), 3)
