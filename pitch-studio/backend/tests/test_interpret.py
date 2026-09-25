from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from backend.pipeline.interpret import (
    InterpretError,
    build_interpret_messages,
    interpret_brief,
    normalize_interpret_payload,
)
from backend.routers.generation_routes import interpret_generation
from backend.schemas import InterpretRequest


def valid_raw(**overrides):
    payload = {
        "audience_cluster": "A",
        "duration": "T4",
        "channel": "CH3",
        "intent": "I1",
        "temperature": "X1",
        "recipe_ref": "A1-1",
        "persona_candidates": [
            {"recipe_ref": "A1-1", "confidence": 0.92, "rationale": "Parents of UG aspirants"},
        ],
        "summary": "A cold introduction to prospective parents on a 30-minute Zoom with a deck.",
        "notes": "Emphasise campus visit next.",
    }
    payload.update(overrides)
    return payload


class NormalizeInterpretTests(unittest.TestCase):
    def test_valid_mapping_keeps_recipe_ref(self):
        result = normalize_interpret_payload(valid_raw(), {"A1-1", "B2-1"})
        self.assertEqual(result.audience_cluster, "A")
        self.assertEqual(result.duration, "T4")
        self.assertEqual(result.channel, "CH3")
        self.assertEqual(result.intent, "I1")
        self.assertEqual(result.temperature, "X1")
        self.assertEqual(result.recipe_ref, "A1-1")
        self.assertEqual(len(result.persona_candidates), 1)
        self.assertEqual(result.persona_candidates[0].recipe_ref, "A1-1")
        self.assertAlmostEqual(result.persona_candidates[0].confidence, 0.92)
        self.assertIn("prospective parents", result.summary.lower())
        self.assertEqual(result.notes, "Emphasise campus visit next.")

    def test_deck_use_case_normalized_and_parent_temperature_nudge(self):
        result = normalize_interpret_payload(
            valid_raw(deck_use_case="parents_undecided", temperature="X4"),
            {"A2-1"},
        )
        self.assertEqual(result.deck_use_case, "parents_decided")
        result = normalize_interpret_payload(
            valid_raw(deck_use_case="UG DSAI aspirant", temperature="X1"),
            {"A1-1"},
        )
        self.assertEqual(result.deck_use_case, "ug_dsai")
        result = normalize_interpret_payload(
            valid_raw(deck_use_case="recruiter", temperature="X1"),
            {"A1-1"},
        )
        self.assertEqual(result.deck_use_case, "")

    def test_invalid_code_raises(self):
        with self.assertRaises(InterpretError) as ctx:
            normalize_interpret_payload(valid_raw(duration="T99"), {"A1-1"})
        self.assertIn("duration", str(ctx.exception))

    def test_unknown_recipe_ref_is_blanked(self):
        result = normalize_interpret_payload(
            valid_raw(recipe_ref="NOPE-9", persona_candidates=[]),
            {"A1-1"},
        )
        self.assertEqual(result.recipe_ref, "")
        self.assertEqual(result.audience_cluster, "A")

    def test_close_persona_race_leaves_recipe_ref_blank(self):
        result = normalize_interpret_payload(
            valid_raw(
                recipe_ref="",
                persona_candidates=[
                    {"recipe_ref": "A4-1", "confidence": 0.62, "rationale": "PG undergrad"},
                    {"recipe_ref": "A5-1", "confidence": 0.58, "rationale": "Working professional"},
                ],
            ),
            {"A4-1", "A5-1"},
        )
        self.assertEqual(result.recipe_ref, "")
        self.assertEqual([c.recipe_ref for c in result.persona_candidates], ["A4-1", "A5-1"])

    def test_top_persona_at_seventy_is_auto_selected(self):
        result = normalize_interpret_payload(
            valid_raw(
                recipe_ref="",
                persona_candidates=[
                    {"recipe_ref": "A5-2", "confidence": 0.8, "rationale": "Working professional"},
                    {"recipe_ref": "A7-3", "confidence": 0.7, "rationale": "Mid-career"},
                ],
            ),
            {"A5-2", "A7-3"},
        )
        self.assertEqual(result.recipe_ref, "A5-2")

    def test_clear_persona_winner_is_auto_selected(self):
        result = normalize_interpret_payload(
            valid_raw(
                recipe_ref="",
                persona_candidates=[
                    {"recipe_ref": "A5-1", "confidence": 0.81, "rationale": "Career switcher"},
                    {"recipe_ref": "A4-1", "confidence": 0.4, "rationale": "Less likely"},
                ],
            ),
            {"A4-1", "A5-1"},
        )
        self.assertEqual(result.recipe_ref, "A5-1")

    def test_recipe_ref_without_candidates_is_kept(self):
        result = normalize_interpret_payload(
            valid_raw(persona_candidates=[]),
            {"A1-1"},
        )
        self.assertEqual(result.recipe_ref, "A1-1")
        self.assertEqual(result.persona_candidates[0].recipe_ref, "A1-1")
        self.assertAlmostEqual(result.persona_candidates[0].confidence, 1.0)


class BuildMessagesTests(unittest.TestCase):
    def test_prompt_includes_catalogs_and_brief(self):
        recipe = SimpleNamespace(
            ref="A1-1",
            audience_label="School student",
            audience_cluster="A",
            duration="T1",
            channel="CH3",
            intent="I2",
        )
        messages = build_interpret_messages(
            "Prospective parents",
            "A 30-minute Zoom call",
            "A first introduction",
            [recipe],
        )
        self.assertEqual(messages[0]["role"], "system")
        user = messages[1]["content"]
        self.assertIn("Audience clusters", user)
        self.assertIn("Durations", user)
        self.assertIn("A1-1", user)
        self.assertIn("School student", user)
        self.assertIn("Prospective parents", user)
        self.assertIn("30-minute Zoom", user)
        self.assertIn("persona_candidates", messages[0]["content"])
        self.assertIn("deck_use_case", messages[0]["content"])
        self.assertIn("school_fair", user)
        self.assertIn("Deck use cases", user)


class InterpretBriefTests(unittest.TestCase):
    def test_valid_mapping_returns_codes_and_recipe_ref(self):
        recipe = SimpleNamespace(
            ref="A1-1",
            audience_label="Prospective parents",
            audience_cluster="A",
            duration="T4",
            channel="CH3",
            intent="I1",
        )
        db = mock.Mock()
        db.query.return_value.order_by.return_value.all.return_value = [recipe]

        with mock.patch(
            "backend.pipeline.interpret.chat_json",
            return_value=valid_raw(),
        ) as chat:
            result = interpret_brief(
                db,
                InterpretRequest(
                    audience_text="Prospective parents",
                    setting_text="A 30-minute Zoom call",
                    goal_text="A first introduction",
                ),
            )
        chat.assert_called_once()
        self.assertEqual(result.recipe_ref, "A1-1")
        self.assertEqual(result.temperature, "X1")

    def test_invalid_code_from_llm_raises(self):
        db = mock.Mock()
        db.query.return_value.order_by.return_value.all.return_value = []
        with mock.patch(
            "backend.pipeline.interpret.chat_json",
            return_value=valid_raw(channel="CH99", recipe_ref=""),
        ):
            with self.assertRaises(InterpretError):
                interpret_brief(db, InterpretRequest(audience_text="Investor"))

    def test_unknown_recipe_ref_gets_blanked(self):
        db = mock.Mock()
        db.query.return_value.order_by.return_value.all.return_value = [
            SimpleNamespace(
                ref="A1-1",
                audience_label="School student",
                audience_cluster="A",
                duration="T1",
                channel="CH3",
                intent="I2",
            )
        ]
        with mock.patch(
            "backend.pipeline.interpret.chat_json",
            return_value=valid_raw(recipe_ref="MISSING", persona_candidates=[]),
        ):
            result = interpret_brief(db, InterpretRequest(audience_text="Parents"))
        self.assertEqual(result.recipe_ref, "")
        self.assertEqual(result.persona_candidates, [])


class InterpretRouteTests(unittest.TestCase):
    def test_route_returns_result_on_success(self):
        expected = normalize_interpret_payload(valid_raw(), {"A1-1"})
        db = mock.Mock()
        user = SimpleNamespace(id=1)
        with mock.patch(
            "backend.routers.generation_routes.interpret_brief",
            return_value=expected,
        ):
            result = interpret_generation(
                InterpretRequest(audience_text="Parents"),
                db=db,
                user=user,
            )
        self.assertEqual(result.recipe_ref, "A1-1")

    def test_route_returns_502_on_invalid_codes(self):
        db = mock.Mock()
        user = SimpleNamespace(id=1)
        with mock.patch(
            "backend.routers.generation_routes.interpret_brief",
            side_effect=InterpretError("Interpreter returned invalid axis codes: duration='T99'"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                interpret_generation(
                    InterpretRequest(audience_text="Parents"),
                    db=db,
                    user=user,
                )
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("invalid axis codes", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
