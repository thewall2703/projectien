"""Tests for Master Script three-engine mode."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.master_script.evidence import is_excluded_claim
from backend.master_script.source import (
    load_master_script,
    resolve_route,
)
from backend.master_script.verify import (
    check_never_say,
    check_number_traceability,
    remaining_placeholders,
    substitute_locked_placeholders,
    verify_script,
)
from backend.pipeline.vision_modules_flow import normalize_generation_mode
from backend.schemas import GenerationCreate


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = load_master_script()

    def test_school_fair_route(self):
        plan = resolve_route(
            self.doc,
            deck_use_case="school_fair",
            duration="T4",
            persona="School students career fair",
        )
        self.assertEqual(plan.route_id, "school_students")
        self.assertIn("1", plan.sections)
        self.assertIn("7", plan.sections)
        self.assertNotIn("4E", plan.sections)

    def test_cutdown_drops_sections(self):
        plan = resolve_route(
            self.doc,
            deck_use_case="school_fair",
            duration="T1",
        )
        self.assertEqual(plan.length, "cutdown")
        self.assertLessEqual(len(plan.sections), 4)
        self.assertTrue(set(plan.sections) <= {"1", "2", "3", "5", "7"})

    def test_fallback_standard_pitch(self):
        plan = resolve_route(
            self.doc,
            audience_cluster="ZZ",
            duration="T4",
            persona="completely unknown room",
            context_note="",
        )
        self.assertEqual(plan.route_id, "standard_pitch")
        self.assertIn("fallback", plan.match_reason)


class LockedPlaceholderTests(unittest.TestCase):
    def test_substitution_and_leftovers(self):
        locked = {"S1.1": "We started in 2020 with sixty students."}
        text, errors = substitute_locked_placeholders(
            "Open. {{LOCK:S1.1}} Close.", locked
        )
        self.assertEqual(errors, [])
        self.assertIn("We started in 2020", text)
        self.assertEqual(remaining_placeholders(text), [])

    def test_unknown_placeholder(self):
        text, errors = substitute_locked_placeholders("{{LOCK:S9.9}}", {})
        self.assertTrue(errors)
        self.assertEqual(remaining_placeholders(text), ["S9.9"])


class VerifierTests(unittest.TestCase):
    def test_never_say_business_school_and_mu(self):
        issues = check_never_say("Welcome to our business school, MU.")
        codes = [i.code for i in issues]
        self.assertIn("never_say", codes)
        joined = " ".join(i.message for i in issues)
        self.assertIn("business school", joined.lower())
        self.assertIn("MU", joined)

    def test_mu_ventures_allowed(self):
        issues = check_never_say(
            "MU Ventures backs founders under twenty-five at Masters' Union University."
        )
        self.assertEqual(issues, [])

    def test_accredited_negation_in_locked_style(self):
        issues = check_never_say(
            "I'm not going to call them accreditations. They are memberships."
        )
        self.assertEqual(issues, [])
        issues2 = check_never_say("We are AACSB accredited.")
        self.assertTrue(issues2)

    def test_average_requires_median(self):
        issues = check_never_say("The average package is 33.39 lakh.")
        self.assertTrue(any("median" in i.message.lower() for i in issues))
        ok = check_never_say(
            "The average package is 33.39 lakh. The median is 27.78 lakh."
        )
        self.assertEqual(ok, [])

    def test_number_traceability(self):
        issues = check_number_traceability(
            "We have 2,500 students and raised 100 crore.",
            allowed_texts=["We have over two and a half thousand students."],
        )
        self.assertTrue(issues)
        ok = check_number_traceability(
            "We have 2,500 students.",
            allowed_texts=["2,500 students enrolled"],
        )
        self.assertEqual(ok, [])

    def test_verify_script_catches_placeholder(self):
        report = verify_script(
            {
                "sections": [
                    {
                        "ms_section_id": "1",
                        "heading": "Founding",
                        "text": "Hello {{LOCK:S1.1}}",
                        "topic_id": 1,
                    }
                ],
                "cta": "Apply",
            },
            locked={"S1.1": "locked line"},
            cards_by_section={},
            plan_by_section={"1": {"locked_slots": [{"id": "S1.1"}]}},
            requested_lock_ids={"S1.1"},
        )
        self.assertFalse(report.passed)
        self.assertTrue(any(i.code == "lock_placeholder" for i in report.issues))


class EvidenceExclusionTests(unittest.TestCase):
    def test_blue_brew_excluded(self):
        self.assertIn("Blue Brew", is_excluded_claim("Blue Brew did 2 crore revenue", "revenue"))

    def test_jobs_created_excluded(self):
        self.assertIn("jobs", is_excluded_claim("1200 jobs created", "").lower())

    def test_unlabelled_venture_excluded(self):
        reason = is_excluded_claim("The venture hit 5 crore", "")
        self.assertIn("unlabelled", reason.lower())

    def test_labelled_venture_ok(self):
        self.assertEqual(
            is_excluded_claim("PlaySuper raised 5 crore", "raised"),
            "",
        )


class ModeDispatchTests(unittest.TestCase):
    def test_normalize_accepts_master_script(self):
        self.assertEqual(normalize_generation_mode("master_script"), "master_script")
        self.assertEqual(normalize_generation_mode("vision_modules"), "vision_modules")
        self.assertEqual(normalize_generation_mode("nope"), "classic")

    def test_schema_accepts_master_script(self):
        payload = GenerationCreate(temperature="X1", generation_mode="master_script")
        self.assertEqual(payload.generation_mode, "master_script")
        with self.assertRaises(ValidationError):
            GenerationCreate(temperature="X1", generation_mode="nope")

    def test_generate_script_phase_dispatches(self):
        from backend.pipeline import runner

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()

        target = mock.Mock()
        target.generation_mode = "master_script"
        target.audience_cluster = "A1"
        target.duration = "T2"
        target.channel = "C1"
        target.intent = "I1"
        target.temperature = "X1"
        target.context_note = ""
        target.recipe_ref = ""
        target.deck_use_case = "school_fair"
        target.module_sequence = ""
        target.script_json = ""
        target.validation_report = ""
        target.error = ""
        target.founder_quote_ids = ""
        target.report_asset_ids = ""
        target.report_passages_json = ""
        target.master_script_trace_json = ""

        sentinel = object()

        with mock.patch(
            "backend.master_script.flow.generate_script_phase",
            return_value=sentinel,
        ) as mocked:
            result = runner.generate_script_phase(db, target, lambda _s: None)
        self.assertIs(result, sentinel)
        mocked.assert_called_once()
        db.close()


if __name__ == "__main__":
    unittest.main()
