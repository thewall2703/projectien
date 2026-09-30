import unittest
from copy import deepcopy
from unittest.mock import patch

import httpx

from backend.master_script.editor import edit_and_review, apply_edits, compact_authorities, _call
from backend.pipeline.llm import chat_json, _post_openrouter, LLMError


class EditorTests(unittest.TestCase):
    def run_editor(self, source_failures=0, optional=False, incomplete=False):
        calls = []
        finding = {"section_id": "1", "quote": "Draft", "fix": "Correct claim",
                   "severity": "optional" if optional else "mandatory"}

        def call(name, role, system, payload, tokens):
            calls.append((name, role, deepcopy(payload)))
            if name.startswith("editor"):
                return {"edits": [], "unresolved": []}
            checked = ["1"] if incomplete else ["1", "cta"]
            if name.startswith("source"):
                fail = int(name[-1]) < source_failures
                return {"checked_sections": checked, "passed": not fail or optional,
                        "issues": [finding] if fail else []}
            return {"checked_sections": checked,
                    "clarity": {"passed": True, "issues": []},
                    "screenplay": {"passed": True, "issues": []}}

        result = edit_and_review([{"section_id": "1", "text": "Draft"}],
                                 brief="test", authorities={"source_text": "truth"},
                                 evidence=[], references=["voice"], plan={}, call=call)
        return result, calls

    def test_clean_run_uses_three_calls_and_separates_review_context(self):
        result, calls = self.run_editor()
        self.assertTrue(result["passed"])
        self.assertEqual(len(calls), 3)
        source = next(c[2] for c in calls if c[0] == "source_0")
        delivery = next(c[2] for c in calls if c[0] == "delivery_0")
        self.assertNotIn("voice_references", source)
        self.assertNotIn("authorities", delivery)
        self.assertNotIn("evidence", delivery)

    def test_one_repair_is_reaudited_and_then_stops(self):
        result, calls = self.run_editor(source_failures=2)
        self.assertFalse(result["passed"])
        self.assertEqual(len(calls), 6)
        self.assertTrue(result["issues"])
        self.assertTrue(next(c[2] for c in calls if c[0] == "editor_1")["findings"])

    def test_repair_can_pass_final_reviews(self):
        result, calls = self.run_editor(source_failures=1)
        self.assertTrue(result["passed"])
        self.assertEqual(len(calls), 6)

    def test_optional_preferences_do_not_trigger_rewrites(self):
        result, calls = self.run_editor(source_failures=2, optional=True)
        self.assertTrue(result["passed"])
        self.assertEqual(len(calls), 3)

    def test_partial_audit_cannot_pass(self):
        with self.assertRaisesRegex(ValueError, "every section"):
            self.run_editor(incomplete=True)

    def test_replacements_preserve_input_and_unedited_metadata(self):
        original = [{"section_id": "1", "text": "Awkward copy", "slides": [2]}]
        result, _ = apply_edits(original, "", {"edits": [
            {"section_id": "1", "before": "Awkward", "after": "Clear", "reason": "clarity"}
        ], "unresolved": []})
        self.assertEqual(result[0]["text"], "Clear copy")
        self.assertEqual(result[0]["slides"], [2])
        self.assertEqual(original[0]["text"], "Awkward copy")

    def test_ambiguous_or_lock_changing_edit_is_rejected(self):
        for text, before in [("copy copy", "copy"), ("{{LOCK:a}}", "{{LOCK:a}}")]:
            with self.assertRaises(ValueError):
                apply_edits([{"section_id": "1", "text": text}], "", {"edits": [
                    {"section_id": "1", "before": before, "after": "changed", "reason": "test"}
                ], "unresolved": []})

    def test_compaction_keeps_complete_authority_and_global_restrictions(self):
        doc = {"sections": [{"id": "1", "source_text": "Full text", "locked": ["lock"],
                             "duplicate_metadata": "omit"}, {"id": "2", "source_text": "other"}],
               "never_say": ["blocked"], "open_items": ["pending"]}
        result = compact_authorities(doc, {"1"})
        self.assertEqual(result["sections"], [{"id": "1", "source_text": "Full text", "locked": ["lock"]}])
        self.assertEqual(result["open_items"], ["pending"])

    @patch("backend.pipeline.llm._post_openrouter", return_value='{"edits": []}')
    def test_single_attempt_is_forwarded_to_transport(self, post):
        chat_json([], role="ms_editor", timeout=240, max_attempts=1)
        self.assertEqual(post.call_args.kwargs["max_attempts"], 1)

    @patch("backend.pipeline.llm.time.sleep")
    @patch("backend.pipeline.llm.settings.openrouter_api_key", "test-key")
    @patch("backend.pipeline.llm.httpx.Client")
    def test_provider_400_is_retried_once(self, client, sleep):
        post = client.return_value.__enter__.return_value.post
        post.side_effect = [
            httpx.Response(400, json={"error": {"message": "Provider returned error"}}),
            httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}),
        ]

        self.assertEqual(_post_openrouter({"model": "test"}, 1, max_attempts=2), "{}")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("backend.master_script.editor.chat_json", side_effect=LLMError("400 bad request"))
    def test_editor_error_names_the_failed_call(self, _chat):
        with self.assertRaisesRegex(LLMError, r"editor_0 \(ms_editor\) failed"):
            _call("editor_0", "ms_editor", "system", {}, 100)

    @patch(
        "backend.master_script.editor.chat_json",
        side_effect=[LLMError("400 Provider returned error"), {"edits": [], "unresolved": []}],
    )
    def test_editor_uses_cross_provider_fallback_for_provider_rejection(self, chat):
        result = _call("editor_0", "ms_editor", "system", {}, 100)
        self.assertEqual(result, {"edits": [], "unresolved": []})
        self.assertEqual(chat.call_count, 2)
        self.assertNotIn("model", chat.call_args_list[0].kwargs)
        self.assertEqual(
            chat.call_args_list[1].kwargs["model"],
            "anthropic/claude-opus-5.5",
        )


if __name__ == "__main__":
    unittest.main()
