from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from backend.qa_extraction import (
    approve_candidate,
    candidate_fingerprint,
    deterministic_match,
    extract_pairs_from_transcript,
    merge_extracted_pairs,
    normalize_question,
    parse_extract_payload,
    parse_match_payload,
    persist_candidates,
    rank_objections_for_pitch,
    reject_candidate,
    run_qa_extraction,
    semantic_match_pairs,
)
from backend.models import Objection, QaCandidate, QaExtractionRun, StyleTranscript


class NormalizeTests(unittest.TestCase):
    def test_normalize_question_strips_punctuation(self):
        self.assertEqual(
            normalize_question("Why not an IIM?!"),
            "why not an iim",
        )

    def test_fingerprint_is_stable(self):
        a = candidate_fingerprint("Why ROI?")
        b = candidate_fingerprint("why roi?")
        self.assertEqual(a, b)

    def test_extract_prompt_requires_employee_voice(self):
        from backend.qa_extraction import EXTRACT_SYSTEM

        self.assertIn("EMPLOYEES", EXTRACT_SYSTEM)
        self.assertIn("Never write I/me/my", EXTRACT_SYSTEM)
        self.assertIn("we", EXTRACT_SYSTEM.lower())

    def test_rewrite_strips_my_to_our(self):
        from backend.qa_extraction import rewrite_answer_as_employee

        rewritten = rewrite_answer_as_employee(
            "What is your long-term vision?",
            "My very single goal is to build Masters' Union as a top 10 university. I'm on that duty.",
            rewrite=lambda _q, _a: {
                "proposed_answer": (
                    "Our single goal is to build Masters' Union as a top 10 ranked global university. "
                    "That's the national duty we're on."
                )
            },
        )
        self.assertIn("Our single goal", rewritten)
        self.assertNotRegex(rewritten, r"\b(I|my|I'm)\b")


class RewriteVoiceTests(unittest.TestCase):
    def test_rewrite_answer_as_employee(self):
        from backend.qa_extraction import rewrite_answer_as_employee

        rewritten = rewrite_answer_as_employee(
            "Why did you start MU?",
            "I started Masters' Union because I wanted to fix MBA education.",
            rewrite=lambda _q, _a: {
                "proposed_answer": "Pratham started Masters' Union to fix what was broken in MBA education."
            },
        )
        self.assertIn("Pratham started", rewritten)
        self.assertNotIn("I started", rewritten)


class ParseExtractTests(unittest.TestCase):
    def test_parse_extract_payload_keeps_valid_pairs(self):
        pairs = parse_extract_payload(
            {
                "pairs": [
                    {
                        "question_verbatim": "What about placements?",
                        "answer_verbatim": "We publish outcomes.",
                        "proposed_question": "What about placements?",
                        "proposed_answer": "We publish outcomes every year.",
                        "proposed_who_asks": "Parent",
                        "proposed_move": "Show evidence",
                        "confidence": 0.8,
                        "keep": True,
                    },
                    {"proposed_question": "Skip", "proposed_answer": "x", "keep": False},
                ]
            }
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["proposed_who_asks"], "Parent")

    def test_merge_extracted_pairs_dedupes_overlap(self):
        merged = merge_extracted_pairs(
            [
                {
                    "proposed_question": "Why not IIM?",
                    "proposed_answer": "Short",
                    "confidence": 0.6,
                    "question_verbatim": "Why not IIM?",
                    "answer_verbatim": "Short",
                    "proposed_who_asks": "",
                    "proposed_move": "",
                },
                {
                    "proposed_question": "why not iim?",
                    "proposed_answer": "Longer richer answer about outcomes",
                    "confidence": 0.9,
                    "question_verbatim": "why not iim?",
                    "answer_verbatim": "Longer richer answer about outcomes",
                    "proposed_who_asks": "Applicant",
                    "proposed_move": "Reframe",
                },
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertIn("Longer richer", merged[0]["proposed_answer"])
        self.assertAlmostEqual(merged[0]["confidence"], 0.9)


class MatchTests(unittest.TestCase):
    def test_deterministic_match_finds_same_question(self):
        objections = [
            SimpleNamespace(id=1, question="Why not an IIM?"),
            SimpleNamespace(id=2, question="What is the fee?"),
        ]
        hit = deterministic_match("why not an iim", objections)
        self.assertEqual(hit.id, 1)

    def test_parse_match_payload_blanks_unknown_ids(self):
        matches = parse_match_payload(
            {"matches": [{"candidate_index": 0, "matched_objection_id": 99, "confidence": 0.9, "match_type": "enrich"}]},
            candidate_count=1,
            known_ids={1, 2},
        )
        self.assertEqual(matches[0]["matched_objection_id"], 0)
        self.assertEqual(matches[0]["match_type"], "new")

    def test_semantic_match_pairs_uses_deterministic_first(self):
        pairs = [
            {
                "proposed_question": "Why not an IIM?",
                "proposed_answer": "Different model",
                "confidence": 0.8,
                "question_verbatim": "Why not an IIM?",
                "answer_verbatim": "Different model",
                "proposed_who_asks": "",
                "proposed_move": "",
                "fingerprint": candidate_fingerprint("Why not an IIM?"),
            }
        ]
        objections = [
            Objection(
                id=7,
                question="Why not an IIM?",
                who_asks="Parent",
                move="Reframe",
                answer="Old answer",
                status="approved",
            )
        ]
        matched = semantic_match_pairs(pairs, objections, matcher=lambda *_: {"matches": []})
        self.assertEqual(matched[0]["match_type"], "enrich")
        self.assertEqual(matched[0]["matched_objection_id"], 7)
        self.assertEqual(matched[0]["prior_answer"], "Old answer")


class RankTests(unittest.TestCase):
    def test_rank_objections_prefers_audience_hints(self):
        rows = [
            Objection(id=1, question="Fee?", who_asks="Investor", move="", answer="a", status="approved"),
            Objection(id=2, question="Placements?", who_asks="Parent of UG aspirant", move="", answer="b", status="approved"),
            Objection(id=3, question="Faculty?", who_asks="Faculty candidate", move="", answer="c", status="approved"),
        ]
        ranked = rank_objections_for_pitch(rows, audience_cluster="A", audience_label="Parent of UG aspirant", limit=2)
        self.assertEqual(ranked[0].id, 2)

    def test_select_objections_prefers_ama_approved(self):
        from unittest.mock import MagicMock
        from backend.pipeline.runner import _select_objections

        db = MagicMock()
        seed_obj = Objection(id=5, question="Seed?", who_asks="Parent", status="approved", source_candidate_id=0, source_name="")
        ama_obj = Objection(id=20, question="AMA Brand?", who_asks="Prospective applicant", status="approved", source_candidate_id=29, source_name="AMA")

        # Mocking db query
        def filter_mock(*args, **kwargs):
            query_mock = MagicMock()
            query_mock.all.return_value = [ama_obj]
            return query_mock

        db.query.return_value.filter.side_effect = filter_mock
        selected = _select_objections(db, intent="I1", audience_cluster="A", recipe_ref="")
        self.assertTrue(any(item.id == 20 for item in selected))


class ExtractionFlowTests(unittest.TestCase):
    def test_extract_pairs_from_transcript_with_stub_curate(self):
        raw = "Host: Welcome\nStudent: Why ROI?\nPratham: Because outcomes compound."

        def curate(_text: str):
            return {
                "pairs": [
                    {
                        "question_verbatim": "Why ROI?",
                        "answer_verbatim": "Because outcomes compound.",
                        "proposed_question": "Why focus on ROI?",
                        "proposed_answer": "Because outcomes compound over careers.",
                        "proposed_who_asks": "PG aspirant",
                        "proposed_move": "Explain mechanism",
                        "confidence": 0.85,
                        "keep": True,
                    }
                ]
            }

        pairs = extract_pairs_from_transcript(raw, curate=curate)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["proposed_question"], "Why focus on ROI?")

    def test_malformed_curate_is_skipped(self):
        pairs = extract_pairs_from_transcript(
            "Q: Hello?\nA: Hi.",
            curate=lambda _text: "not-json-object",
        )
        self.assertEqual(pairs, [])


class ApprovalTests(unittest.TestCase):
    def test_approve_new_creates_objection_only_after_approval(self):
        db = mock.Mock()
        transcript = StyleTranscript(id=3, name="AMA 1", raw_text="x", text_hash="abc")
        candidate = QaCandidate(
            id=11,
            run_id=1,
            style_transcript_id=3,
            fingerprint="fp",
            match_type="new",
            matched_objection_id=0,
            confidence=0.9,
            status="pending",
            proposed_question="Why ROI?",
            proposed_who_asks="Student",
            proposed_move="Show outcomes",
            proposed_answer="Because careers compound.",
        )

        def get_side_effect(model, key):
            if model is QaCandidate and key == 11:
                return candidate
            if model is StyleTranscript and key == 3:
                return transcript
            return None

        db.get.side_effect = get_side_effect
        approved = approve_candidate(db, 11)
        self.assertEqual(approved.status, "approved")
        self.assertTrue(db.add.called)
        added = db.add.call_args[0][0]
        self.assertIsInstance(added, Objection)
        self.assertEqual(added.question, "Why ROI?")
        self.assertEqual(added.source_candidate_id, 11)
        self.assertTrue(added.edited)
        db.commit.assert_called()

    def test_approve_enrich_overwrites_existing(self):
        db = mock.Mock()
        transcript = StyleTranscript(id=3, name="AMA 1", raw_text="x", text_hash="abc")
        objection = Objection(
            id=5,
            question="Why ROI?",
            who_asks="Old",
            move="Old move",
            answer="Old answer",
            status="approved",
            edited=False,
        )
        candidate = QaCandidate(
            id=12,
            run_id=1,
            style_transcript_id=3,
            fingerprint="fp",
            match_type="enrich",
            matched_objection_id=5,
            confidence=0.9,
            status="pending",
            proposed_question="Why ROI?",
            proposed_who_asks="PG aspirant",
            proposed_move="Use outcomes",
            proposed_answer="Richer answer",
            prior_answer="Old answer",
        )

        def get_side_effect(model, key):
            if model is QaCandidate and key == 12:
                return candidate
            if model is StyleTranscript and key == 3:
                return transcript
            if model is Objection and key == 5:
                return objection
            return None

        db.get.side_effect = get_side_effect
        approve_candidate(db, 12, answer="Edited richer answer")
        self.assertEqual(objection.answer, "Edited richer answer")
        self.assertTrue(objection.edited)
        self.assertEqual(candidate.status, "approved")
        self.assertEqual(candidate.applied_objection_id, 5)

    def test_reject_leaves_objection_untouched(self):
        db = mock.Mock()
        candidate = QaCandidate(id=13, status="pending", style_transcript_id=1, run_id=1, fingerprint="x")
        db.get.return_value = candidate
        reject_candidate(db, 13, review_note="Not useful")
        self.assertEqual(candidate.status, "rejected")
        self.assertEqual(candidate.review_note, "Not useful")
        db.add.assert_not_called()


class PersistAndRunTests(unittest.TestCase):
    def test_persist_candidates_skips_duplicate_fingerprint(self):
        db = mock.Mock()
        run = QaExtractionRun(id=1, style_transcript_id=9, transcript_hash="h")
        transcript = StyleTranscript(id=9, name="AMA", raw_text="t", text_hash="h")
        existing = QaCandidate(
            id=1,
            run_id=1,
            style_transcript_id=9,
            fingerprint=candidate_fingerprint("Why ROI?"),
            status="pending",
            proposed_question="Why ROI?",
            proposed_answer="Old",
        )
        query = db.query.return_value
        query.filter.return_value.first.return_value = existing
        created = persist_candidates(
            db,
            run,
            transcript,
            [
                {
                    "fingerprint": existing.fingerprint,
                    "proposed_question": "Why ROI?",
                    "proposed_answer": "New richer",
                    "match_type": "new",
                    "matched_objection_id": 0,
                    "confidence": 0.8,
                    "question_verbatim": "Why ROI?",
                    "answer_verbatim": "New richer",
                    "proposed_who_asks": "",
                    "proposed_move": "",
                    "evidence": {},
                }
            ],
        )
        self.assertEqual(created, 0)
        self.assertEqual(existing.proposed_answer, "New richer")
        db.add.assert_not_called()

    def test_run_qa_extraction_end_to_end_with_stubs(self):
        db = mock.Mock()
        run = QaExtractionRun(id=4, style_transcript_id=2, transcript_hash="hash", status="queued")
        transcript = StyleTranscript(
            id=2,
            name="AMA",
            raw_text="Student: Why fees?\nPratham: Transparent pricing.",
            text_hash="hash",
        )

        def get_side_effect(model, key):
            if model is QaExtractionRun and key == 4:
                return run
            if model is StyleTranscript and key == 2:
                return transcript
            return None

        db.get.side_effect = get_side_effect
        db.query.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter.return_value.count.return_value = 1

        def curate(_text: str):
            return {
                "pairs": [
                    {
                        "question_verbatim": "Why fees?",
                        "answer_verbatim": "Transparent pricing.",
                        "proposed_question": "Why these fees?",
                        "proposed_answer": "Transparent pricing with outcomes.",
                        "confidence": 0.9,
                        "keep": True,
                    }
                ]
            }

        result = run_qa_extraction(db, 4, curate=curate, matcher=lambda *_: {"matches": []})
        self.assertEqual(result.status, "done")
        self.assertTrue(db.add.called)


if __name__ == "__main__":
    unittest.main()
