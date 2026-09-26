"""Claim-hygiene rules from the Masters' Union communication brief."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import LockedFact
from backend.pipeline.claims import (
    BANNED_CLAIM_PHRASES,
    CALIBRATION_CLAIM_LINE,
    CLAIMS_RULES_PROMPT,
    DISCLOSURE_RULE,
    ROUNDED_AGGREGATE_RE,
    SLIDE_CLAIMS_RULES_PROMPT,
    average_without_median,
    claim_violations,
    disclosure_applies,
    lint_facts,
    pair_median_facts,
)
from backend.pipeline.gaps import _RANK_SYSTEM
from backend.pipeline.prompts import _format_facts, script_messages
from backend.pipeline.slide_fill import _FILL_SYSTEM, gate_claim_hygiene
from backend.pipeline.slide_templates import template_spec
from backend.pipeline.validator import validate_script


def fact(
    *,
    name: str,
    value: str,
    status: str = "verified",
    module_ids: str = "M07",
    source: str = "report",
    fid: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=fid,
        fact=name,
        value=value,
        status=status,
        module_ids=module_ids,
        source=source,
    )


class BannedPhraseTests(unittest.TestCase):
    def test_hyphen_and_space_variants(self):
        cases = [
            ("world-class faculty", "world-class"),
            ("a world class campus", "world-class"),
            ("cutting edge labs", "cutting-edge"),
            ("cutting-edge labs", "cutting-edge"),
            ("learn by doing here", "learn by doing"),
            ("learn-by-doing pedagogy", "learn by doing"),
            ("industry immersive weeks", "industry-immersive"),
            ("India's most innovative school", "india's most innovative"),
            ("beat the IIMs tomorrow", "beat the iims"),
            ("guaranteed placement for all", "guaranteed placement"),
            ("a transformative, intense year", "transformative"),
            ("we learn-by-doing.", "learn by doing"),
            ("Masters\u2019 Union is India\u2019s most innovative.", "india's most innovative"),
        ]
        for text, phrase in cases:
            with self.subTest(text=text):
                hits = claim_violations(text, unit_label="Section 1")
                self.assertTrue(any(phrase in item.lower() or phrase.replace("-", " ") in item.lower() for item in hits))
                self.assertTrue(any("banned claim" in item for item in hits))

    def test_banned_list_covers_brief(self):
        for phrase in (
            "world-class",
            "cutting-edge",
            "transformative",
            "learn by doing",
            "reimagining business education",
            "industry-immersive",
            "unparalleled",
            "india's most innovative",
            "beat the iims",
            "guaranteed placement",
        ):
            self.assertIn(phrase, BANNED_CLAIM_PHRASES)


class RoundedAggregateTests(unittest.TestCase):
    def test_positives(self):
        for text in (
            "60Cr+ funding",
            "250+ CXOs",
            "33+ LPA",
            "1000+ students",
            "500 plus recruiters",
        ):
            with self.subTest(text=text):
                self.assertTrue(ROUNDED_AGGREGATE_RE.search(text), text)
                self.assertTrue(claim_violations(text, unit_label="Slide"))

    def test_negatives(self):
        for text in (
            "We teach C++ in the DSAI track",
            "Call +91 98765 43210",
            "The fee model is 2+2 years",
            "Average ₹33.39 LPA, median ₹27.78 LPA",
        ):
            with self.subTest(text=text):
                self.assertIsNone(ROUNDED_AGGREGATE_RE.search(text), text)
                self.assertFalse(
                    any("rounds a figure" in item for item in claim_violations(text, unit_label="X"))
                )


class AverageMedianTests(unittest.TestCase):
    def test_generalised_average_without_median(self):
        self.assertTrue(average_without_median("Our average package of 33 lakh is strong."))
        self.assertTrue(average_without_median("Average CTC is ₹33.39 LPA this year."))
        self.assertTrue(average_without_median("The avg salary is 30 lakh at Masters' Union."))
        self.assertTrue(average_without_median("Average package is thirty-three lakh."))

    def test_average_without_a_figure_or_about_fees_is_not_flagged(self):
        self.assertFalse(average_without_median("Your average salary expectation should be grounded."))
        self.assertFalse(average_without_median("The average student pays 12 lakh in fees."))
        self.assertFalse(average_without_median("An average day starts at 8, and the fee is 12 lakh."))

    def test_calibration_line_passes(self):
        self.assertEqual(claim_violations(CALIBRATION_CLAIM_LINE, unit_label="Section 1"), [])

    def test_pair_passes(self):
        self.assertFalse(
            average_without_median("average ₹33.39 LPA, median ₹27.78 LPA")
        )
        self.assertFalse(
            average_without_median("Average CTC is ₹33.39 LPA and the median is ₹27.78 LPA.")
        )

    def test_literal_33_39_still_requires_median(self):
        self.assertTrue(average_without_median("We published 33.39 this cohort."))
        self.assertFalse(average_without_median("We published 33.39 and median 27.78."))


class ValidateScriptIntegrationTests(unittest.TestCase):
    def test_average_without_median_is_a_violation(self):
        script = {
            "sections": [{"module_id": "M07", "text": "Average CTC is ₹33.39 LPA this year."}],
            "cta": "Talk to admissions",
        }
        violations = validate_script(script, [], ["M07"], 8)
        self.assertTrue(any("median" in item.lower() for item in violations))

    def test_banned_claim_in_section_and_cta(self):
        script = {
            "sections": [{"module_id": "M01", "text": "This is a world-class programme for builders."}],
            "cta": "Get your guaranteed placement today.",
        }
        violations = validate_script(script, [], ["M01"], 20)
        self.assertTrue(any("world-class" in item for item in violations))
        self.assertTrue(any("guaranteed placement" in item for item in violations))
        self.assertTrue(any("Section 1" in item for item in violations))
        self.assertTrue(any("final ask" in item.lower() for item in violations))


class SlideClaimGateTests(unittest.TestCase):
    def test_gate_rejects_banned_and_unpaired_average(self):
        spec = template_spec("section-divider-light")
        banned = gate_claim_hygiene(spec, {"title": "World-class placements", "subtitle": ""})
        self.assertTrue(any("banned claim" in item for item in banned))
        unpaired = gate_claim_hygiene(
            spec, {"title": "Average CTC ₹33.39 LPA", "subtitle": ""}
        )
        self.assertTrue(any("median" in item.lower() for item in unpaired))
        ok = gate_claim_hygiene(
            spec,
            {"title": "Average ₹33.39 LPA, median ₹27.78 LPA", "subtitle": ""},
        )
        self.assertFalse(ok)

    def test_fill_system_includes_claims_block(self):
        self.assertIn(SLIDE_CLAIMS_RULES_PROMPT, _FILL_SYSTEM)
        self.assertNotIn("DISCLOSURE", _FILL_SYSTEM)

    def test_gaps_rank_system_has_no_copy_rules(self):
        self.assertNotIn("HOW WE MAKE CLAIMS", _RANK_SYSTEM)


class MedianPairingTests(unittest.TestCase):
    def test_format_facts_pairs_median(self):
        average = fact(name="Average CTC", value="₹33.39 LPA", fid=1, module_ids="M07")
        median = fact(name="Median CTC", value="₹27.78 LPA", fid=2, module_ids="M07")
        # Median on a different module so relevance alone would drop it.
        median_other = fact(
            name="Median CTC", value="₹27.78 LPA", fid=3, module_ids="M99"
        )
        locked, _ = _format_facts([average, median_other], ["M07"])
        self.assertIn("Average CTC", locked)
        self.assertIn("Median CTC", locked)
        self.assertIn("27.78", locked)

        paired = pair_median_facts([average], pool=[average, median])
        self.assertEqual(len(paired), 2)
        self.assertTrue(any(item.fact == "Median CTC" for item in paired))

    def test_rounded_locked_fact_carries_note(self):
        cxo = fact(name="CXOs on campus", value="250+", module_ids="M07")
        locked, _ = _format_facts([cxo], ["M07"])
        self.assertIn("more than N", locked)
        self.assertEqual(claim_violations("More than 250 CXOs came to campus.", unit_label="S"), [])

    def test_unusable_median_is_not_paired(self):
        average = fact(name="Average CTC", value="₹33.39 LPA", fid=1)
        median = fact(
            name="Median CTC", value="₹27.78 LPA", fid=2, status="needs_source"
        )
        paired = pair_median_facts([average], pool=[average, median])
        self.assertEqual(len(paired), 1)


class LintFactsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def test_lint_flags_banned_rounded_and_unpaired(self):
        self.db.add_all(
            [
                LockedFact(
                    fact="Faculty quality",
                    value="World-class mentors on campus",
                    status="verified",
                ),
                LockedFact(
                    fact="Funding",
                    value="60Cr+ raised by alumni",
                    status="verified",
                ),
                LockedFact(
                    fact="Average CTC",
                    value="₹33.39 LPA average CTC",
                    status="verified",
                ),
            ]
        )
        self.db.commit()
        problems = lint_facts(self.db)
        kinds = " | ".join(row["problem"] for row in problems)
        self.assertIn("banned claim", kinds)
        self.assertIn("rounded aggregate", kinds)
        self.assertIn("no usable median fact", kinds)

    def test_lint_does_not_flag_average_when_median_fact_exists(self):
        self.db.add_all(
            [
                LockedFact(fact="Average CTC", value="₹33.39 LPA", status="verified"),
                LockedFact(fact="Median CTC", value="₹27.78 LPA", status="verified"),
            ]
        )
        self.db.commit()
        self.assertEqual(lint_facts(self.db), [])


class PromptClaimsTests(unittest.TestCase):
    def _messages(self, duration: str, **kwargs):
        return script_messages(
            audience_cluster="A",
            duration=duration,
            channel="CH1",
            intent="I2",
            temperature="X2",
            context_note="",
            modules=[],
            sequence=["M01"],
            facts=[],
            word_budget=600 if duration == "T2" else 240,
            **kwargs,
        )

    def test_claims_block_and_calibration_example(self):
        system = self._messages("T1")[0]["content"]
        self.assertIn("HOW WE MAKE CLAIMS", system)
        self.assertIn(CALIBRATION_CLAIM_LINE, system)
        self.assertIn("Average/median pairing", system)
        self.assertIn("SLIDE WINS ON CONFLICT", system)
        self.assertNotIn("If you mention average CTC, the median CTC must appear", system)

    def test_disclosure_only_for_long_scripts(self):
        short = self._messages("T1")[0]["content"]
        long = self._messages("T2")[0]["content"]
        self.assertFalse(disclosure_applies("T1"))
        self.assertTrue(disclosure_applies("T2"))
        self.assertNotIn("DISCLOSURE (pitches of ~5 minutes", short)
        self.assertIn("DISCLOSURE (pitches of ~5 minutes", long)
        self.assertIn(DISCLOSURE_RULE.strip().splitlines()[0], long)

    def test_personality_guidance_replaces_do_not_force_hindi(self):
        system = self._messages("T1", style_guide="Be direct.")[0]["content"]
        self.assertNotIn("do not force Hindi", system)
        self.assertIn("tag questions", system)
        self.assertIn("Hindi/Hinglish", system)


if __name__ == "__main__":
    unittest.main()
