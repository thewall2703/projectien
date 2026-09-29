import unittest

from backend.config import settings
from backend.master_script.editorial import (
    EDITORIAL_AGENT_CONTEXT,
    EDITORIAL_AGENT_SYSTEM,
    load_editorial_context,
)
from backend.pipeline.llm import role_defaults


class EditorialContextTests(unittest.TestCase):
    def test_durable_context_contains_core_editorial_decisions(self):
        self.assertIn(
            "If removing a detail leaves the argument equally strong",
            EDITORIAL_AGENT_CONTEXT,
        )
        self.assertIn("screenplay quality", EDITORIAL_AGENT_CONTEXT)
        self.assertIn("Pratham transcripts as a factual source", EDITORIAL_AGENT_CONTEXT)
        self.assertIn("reviewed after editorial corrections", EDITORIAL_AGENT_CONTEXT)

    def test_pg_profile_is_explicit_and_does_not_leak_into_durable_context(self):
        combined = load_editorial_context("pg_intro_30min.md")
        self.assertIn("40% practitioners", combined)
        self.assertIn("Food Lab is a lab", combined)
        self.assertIn("The Reel Store", combined)
        self.assertNotIn("40% practitioners", EDITORIAL_AGENT_CONTEXT)
        self.assertNotIn("Food Lab is a lab", EDITORIAL_AGENT_CONTEXT)

    def test_editorial_system_marks_context_as_non_evidence(self):
        self.assertIn(
            "never treat the context itself as evidence",
            EDITORIAL_AGENT_SYSTEM,
        )

    def test_editor_has_an_independent_llm_role(self):
        defaults = role_defaults("ms_editor")
        self.assertEqual(defaults["model"], settings.ms_editor_model)
        self.assertEqual(defaults["effort"], settings.ms_editor_reasoning_effort)
        self.assertEqual(defaults["timeout"], settings.ms_editor_timeout)

    def test_profile_name_must_be_a_plain_markdown_filename(self):
        with self.assertRaises(ValueError):
            load_editorial_context("../pg_intro_30min.md")
        with self.assertRaises(ValueError):
            load_editorial_context("pg_intro_30min.txt")


if __name__ == "__main__":
    unittest.main()
