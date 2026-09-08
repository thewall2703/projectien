from __future__ import annotations

import unittest
from unittest import mock

from backend.config import settings
from backend.pipeline.deck import (
    DeckSlide,
    DeckSpec,
    expand_deck_from_script,
    merge_deck_specs,
    normalize_deck_order,
    render_pptx,
)


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

    def test_renders_agenda_and_speaker_notes(self):
        spec = DeckSpec(
            slides=[
                DeckSlide(layout="title", title="Cover", subtitle="Pitch", module_id="M01"),
                DeckSlide(layout="agenda", title="Agenda", bullets=["Hook", "Proof", "Ask"], module_id="M01"),
                DeckSlide(layout="section", title="Proof", module_id="M04"),
                DeckSlide(layout="stat_pair", title="Outcomes", stats=[{"label": "Median", "value": "27.78"}], module_id="M04"),
            ]
        )
        with mock.patch.object(type(settings), "uses_spaces", property(lambda self: False)):
            path = render_pptx(
                spec,
                999003,
                ["M01", "M04"],
                notes_by_module={"M01": "Open with the institution.", "M04": "Stay on median CTC."},
            )
        self.assertTrue(path)
        from pptx import Presentation

        deck = Presentation(path)
        self.assertEqual(len(deck.slides), 4)
        first_notes = deck.slides[0].notes_slide.notes_text_frame.text
        self.assertIn("institution", first_notes)

    def test_normalizes_out_of_order_slides(self):
        spec = DeckSpec(
            slides=[
                DeckSlide(layout="title", title="Title", module_id="M01"),
                DeckSlide(layout="cta", title="Apply", module_id="M14"),
                DeckSlide(layout="bullets", title="Body", module_id="M04"),
            ]
        )
        ordered = normalize_deck_order(spec, ["M01", "M04", "M14"])
        self.assertEqual(
            [slide.module_id for slide in ordered.slides],
            ["M01", "M04", "M14"],
        )

    def test_unknown_module_slides_stay_anchored(self):
        spec = DeckSpec(
            slides=[
                DeckSlide(layout="title", title="Title", module_id=""),
                DeckSlide(layout="section", title="B", module_id="M04"),
                DeckSlide(layout="bullets", title="B detail", module_id=""),
                DeckSlide(layout="section", title="A", module_id="M01"),
            ]
        )
        ordered = normalize_deck_order(spec, ["M01", "M04"])
        self.assertEqual(
            [(slide.title, slide.module_id) for slide in ordered.slides],
            [("Title", ""), ("A", "M01"), ("B", "M04"), ("B detail", "")],
        )

    def test_renders_reordered_deck(self):
        spec = DeckSpec(
            slides=[
                DeckSlide(layout="cta", title="Apply", module_id="M14"),
                DeckSlide(layout="title", title="Title", module_id="M01"),
            ]
        )
        with mock.patch.object(type(settings), "uses_spaces", property(lambda self: False)):
            path = render_pptx(spec, 999002, ["M01", "M14"])
        self.assertTrue(path)

    def test_expands_thin_script_into_full_deck(self):
        script = {
            "sections": [
                {
                    "module_id": "M01",
                    "heading": "Who we are",
                    "text": "Masters' Union trains operators, not spectators. "
                    "The median CTC is ₹27.78 LPA and the average is ₹33.39 LPA. "
                    "Pratham Mittal said \"the building is the argument.\" "
                    "Recruiters come because the work is already public.",
                },
                {
                    "module_id": "M14",
                    "heading": "The ask",
                    "text": "Come to campus this week. Sit in a class. Then decide.",
                },
            ],
            "cta": "Book a campus walkthrough",
        }
        spec = expand_deck_from_script(script, ["M01", "M14"])
        layouts = [slide.layout for slide in spec.slides]
        self.assertIn("title", layouts)
        self.assertIn("agenda", layouts)
        self.assertIn("stat_pair", layouts)
        self.assertIn("quote", layouts)
        self.assertIn("cta", layouts)
        self.assertGreaterEqual(len(spec.slides), 8)
        thin = DeckSpec(slides=[DeckSlide(layout="title", title="Only this", module_id="M01")])
        merged = merge_deck_specs(thin, spec, ["M01", "M14"])
        self.assertGreaterEqual(len(merged.slides), 8)
