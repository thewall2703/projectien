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
        self.assertIn("prospective parents", result.summary.lower())
        self.assertEqual(result.notes, "Emphasise campus visit next.")

    def test_invalid_code_raises(self):
        with self.assertRaises(InterpretError) as ctx:
            normalize_interpret_payload(valid_raw(duration="T99"), {"A1-1"})
        self.assertIn("duration", str(ctx.exception))

    def test_unknown_recipe_ref_is_blanked(self):
        result = normalize_interpret_payload(valid_raw(recipe_ref="NOPE-9"), {"A1-1"})
        self.assertEqual(result.recipe_ref, "")
        self.assertEqual(result.audience_cluster, "A")


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
            return_value=valid_raw(recipe_ref="MISSING"),
        ):
            result = interpret_brief(db, InterpretRequest(audience_text="Parents"))
        self.assertEqual(result.recipe_ref, "")


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
