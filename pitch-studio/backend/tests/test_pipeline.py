from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.pipeline.llm import _extract_json
from backend.pipeline.resolver import _compose_fallback, parse_sequence
from backend.pipeline.runner import pick_assets
from backend.pipeline.validator import validate_script


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


class JsonExtractTests(unittest.TestCase):
    def test_strips_markdown_fence(self):
        payload = _extract_json('```json\n{"sections": [], "cta": "Apply"}\n```')
        self.assertEqual(payload["cta"], "Apply")

    def test_empty_raises(self):
        with self.assertRaises(Exception):
            _extract_json("")


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
