from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.pipeline.llm import _extract_json, _multimodal_payload, _request_payload
from backend.pipeline.prompts import UNIVERSITY_STATUS_LINE, duration_framework, script_messages
from backend.pipeline.resolver import _compose_fallback, parse_sequence
from backend.pipeline.runner import pick_assets
from backend.pipeline.validator import align_script_to_recipe, count_script_words, validate_script


def fact(status: str, value: str, name: str = "x") -> SimpleNamespace:
    return SimpleNamespace(status=status, value=value, fact=name)


class ValidatorTests(unittest.TestCase):
    def test_forbidden_fact_is_caught(self):
        script = {
            "sections": [{"module_id": "M01", "text": "Blue Brew raised a huge valuation."}],
            "cta": "Apply",
        }
        facts = [fact("do_not_use", "Blue Brew raised a huge valuation.")]
        violations = validate_script(script, facts, ["M01"], 10)
        self.assertTrue(any("Forbidden" in item for item in violations))

    def test_average_requires_median(self):
        script = {
            "sections": [{"module_id": "M07", "text": "Average CTC is ₹33.39 LPA this year."}],
            "cta": "Talk to admissions",
        }
        violations = validate_script(script, [], ["M07"], 8)
        self.assertTrue(any("median" in item.lower() for item in violations))

    def test_average_with_median_passes_pair_rule(self):
        text = (
            "Average CTC is ₹33.39 LPA and the median is ₹27.78 LPA, "
            "side by side, audited carefully for this conversation."
        )
        script = {"sections": [{"module_id": "M07", "text": text}], "cta": "Apply now"}
        violations = validate_script(script, [], ["M07"], len(text.split()))
        self.assertFalse(any("median" in item.lower() for item in violations))

    def test_sequence_mismatch(self):
        script = {"sections": [{"module_id": "M02", "text": "Institution first."}], "cta": "x"}
        violations = validate_script(script, [], ["M01", "M02"], 3)
        self.assertTrue(any("order" in item for item in violations))

    def test_align_restores_missing_close(self):
        script = {
            "sections": [
                {"module_id": "M01", "heading": "Hook", "text": "We train operators."},
                {"module_id": "M02", "heading": "Institution", "text": "The school is the proof."},
            ],
            "cta": "Come to campus this week.",
        }
        m14 = SimpleNamespace(
            id="M14",
            name="The close",
            job="Ask for the next step",
            core_content="Name one next step. Sit in a class. Then decide.",
        )
        aligned = align_script_to_recipe(script, ["M01", "M02", "M14"], [m14])
        self.assertEqual([section["module_id"] for section in aligned["sections"]], ["M01", "M02", "M14"])
        self.assertIn("campus", aligned["sections"][-1]["text"].lower())
        violations = validate_script(aligned, [], ["M01", "M02", "M14"], 20)
        self.assertFalse(any("order" in item for item in violations))

    def test_cta_is_not_counted_twice(self):
        script = {
            "sections": [
                {"module_id": "M14", "heading": "Ask", "text": "Come to campus this Saturday."}
            ],
            "cta": "Come to campus this Saturday.",
        }
        self.assertEqual(count_script_words(script), 5)

    def test_accreditation_wording(self):
        script = {
            "sections": [
                {
                    "module_id": "M02",
                    "text": "We have AACSB accreditation and that settles it.",
                }
            ],
            "cta": "Visit campus",
        }
        violations = validate_script(script, [], ["M02"], 8)
        self.assertTrue(any("accreditation" in item.lower() for item in violations))


class PromptTests(unittest.TestCase):
    def test_duration_framework_progresses_with_time(self):
        self.assertIn("precise, specific, and factual", duration_framework("T0"))
        self.assertIn("position Masters' Union", duration_framework("T1"))
        self.assertIn("emotional state", duration_framework("T2"))
        self.assertIn("student case studies", duration_framework("T3"))

    def test_selected_duration_strategy_is_in_script_prompt(self):
        messages = script_messages(
            audience_cluster="A",
            duration="T2",
            channel="CH1",
            intent="I2",
            temperature="X3",
            context_note="",
            modules=[],
            sequence=["M01"],
            facts=[],
            word_budget=700,
        )
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("IMPACT MUST BE PROPORTIONAL TO TIME", system)
        self.assertIn("Duration strategy — follow this as the governing narrative brief", user)
        self.assertIn("emotional state", user)

    def test_university_status_uses_natural_approved_wording(self):
        old_wording = "Masters' Union University — bill passed by the Government of Haryana"
        status_fact = SimpleNamespace(
            fact="University status",
            value=old_wording,
            source="Haryana state legislation",
            status="verified",
            module_ids="M02",
        )
        messages = script_messages(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I1",
            temperature="X2",
            context_note="",
            modules=[],
            sequence=["M02"],
            facts=[status_fact],
            word_budget=280,
        )
        user = messages[1]["content"]
        self.assertIn(UNIVERSITY_STATUS_LINE, user)
        self.assertNotIn(old_wording, user)

    def test_revision_includes_draft_and_range(self):
        draft = {
            "sections": [{"module_id": "M01", "heading": "Origin", "text": "Why this exists."}],
            "cta": "Visit campus.",
        }
        messages = script_messages(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X3",
            context_note="",
            modules=[],
            sequence=["M01"],
            facts=[],
            word_budget=280,
            corrections=["Word count 340 is outside budget 280 (±15%, 238-322)"],
            draft=draft,
        )
        user = messages[1]["content"]
        self.assertIn("238-322", user)
        self.assertIn("Why this exists.", user)
        self.assertIn("Rewrite THAT draft", user)


class JsonExtractTests(unittest.TestCase):
    def test_strips_markdown_fence(self):
        payload = _extract_json('```json\n{"sections": [], "cta": "Apply"}\n```')
        self.assertEqual(payload["cta"], "Apply")

    def test_empty_raises(self):
        with self.assertRaises(Exception):
            _extract_json("")


class LLMRequestTests(unittest.TestCase):
    def test_uses_opus_46_with_medium_adaptive_reasoning(self):
        payload = _request_payload([{"role": "user", "content": "Write the pitch."}])
        self.assertEqual(payload["model"], "anthropic/claude-opus-4.6")
        self.assertEqual(payload["reasoning"], {"enabled": True})
        self.assertEqual(payload["verbosity"], "medium")

    def test_multimodal_payload_sends_image_parts_without_json_format(self):
        payload = _multimodal_payload("Describe these images.", [b"abc", b"xyz"])
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Describe these images."})
        self.assertEqual(len(content), 3)
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("response_format", payload)


class ResolverHelperTests(unittest.TestCase):
    def test_parse_sequence(self):
        self.assertEqual(parse_sequence("M02 > M04 > M14"), ["M02", "M04", "M14"])

    def test_short_fallback_trims(self):
        sequence = _compose_fallback("A", "T1", "I2", "X2")
        self.assertEqual(sequence[-1], "M14")
        self.assertLessEqual(len(sequence), 3)

    def test_closing_adds_honest_module(self):
        sequence = _compose_fallback("A", "T2", "I2", "X4")
        self.assertIn("M13", sequence)


class AssetPickTests(unittest.TestCase):
    def test_round_robin_includes_later_modules(self):
        assets = [
            SimpleNamespace(
                title="Prospectus",
                module_ids="M02,M10",
                file_status="stored",
                file_key="a",
                status="exists",
            ),
            SimpleNamespace(
                title="Another Prospectus",
                module_ids="M02,M10",
                file_status="stored",
                file_key="b",
                status="exists",
            ),
            SimpleNamespace(
                title="PGP TBM Placement Report 2024",
                module_ids="M07,M13",
                file_status="stored",
                file_key="c",
                status="exists",
            ),
            SimpleNamespace(
                title="Brand Deck",
                module_ids="M01,M02,M07",
                file_status="external",
                file_key="",
                status="exists",
            ),
        ]
        selected = pick_assets(assets, ["M02", "M04", "M07", "M13", "M14"], limit=3)
        titles = [item.title for item in selected]
        self.assertIn("PGP TBM Placement Report 2024", titles)
        self.assertNotIn("Brand Deck", titles)


if __name__ == "__main__":
    unittest.main()
