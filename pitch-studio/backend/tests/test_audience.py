from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.audience import (
    build_listener_profile,
    calibrate_simulator,
    extract_listener_turns,
    latest_listener_profile,
    parse_listener_turns_payload,
    scrub_pii,
)
from backend.database import Base
from backend.models import (
    ListenerProfile,
    ListenerTurn,
    Recipe,
    StyleTranscript,
    StyleTranscriptPersona,
)
from backend.pipeline.audience_sim import listener_notes, listener_passed, simulate_audience


SAMPLE_VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
parent ananya: Hi, I am a parent of a Class 12 student. Is the ROI worth 25 lakh?

2
00:00:04.000 --> 00:00:10.000
pratham mittal: Look at the placement report and come sit in a class.

3
00:00:10.000 --> 00:00:13.000
parent ananya: Okay, that is clear. Thank you.

4
00:00:13.000 --> 00:00:18.000
student ravi: What about faculty quality? Call me at 9876543210 or ravi@example.com

5
00:00:18.000 --> 00:00:24.000
pratham mittal: Practitioners teach here every week.

6
00:00:24.000 --> 00:00:28.000
student ravi: But how is that different from IIM? Still not convinced.
"""


class ScrubPiiTests(unittest.TestCase):
    def test_scrubs_email_and_phone(self):
        text = scrub_pii("Email me at ravi@example.com or 9876543210 please")
        self.assertNotIn("ravi@example.com", text)
        self.assertIn("[EMAIL]", text)
        self.assertIn("[PHONE]", text)


class ParseListenerTurnsTests(unittest.TestCase):
    def test_parse_and_scrub(self):
        turns = parse_listener_turns_payload(
            {
                "turns": [
                    {
                        "self_description": "parent",
                        "concern": "ROI",
                        "question_verbatim": "Call 9988776655 about fees",
                        "reaction_after_answer": "okay",
                        "outcome": "landed",
                        "answer_summary": "Show report",
                        "confidence": 0.9,
                    },
                    {"question_verbatim": "", "outcome": "unclear"},
                ]
            }
        )
        self.assertEqual(len(turns), 1)
        self.assertIn("[PHONE]", turns[0]["question_verbatim"])
        self.assertEqual(turns[0]["outcome"], "landed")


class ExtractListenerTurnsTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()

    def test_extract_idempotent_and_skips_unlabeled(self):
        transcript = StyleTranscript(
            name="AMA",
            raw_text=SAMPLE_VTT,
            text_hash="abc",
            status="processed",
        )
        self.db.add(transcript)
        self.db.commit()

        fixed = [
            {
                "self_description": "parent of Class 12 student",
                "concern": "ROI",
                "question_verbatim": "Is the ROI worth it?",
                "reaction_after_answer": "Okay, that is clear.",
                "outcome": "landed",
                "answer_summary": "Look at placement report",
                "confidence": 0.8,
            }
        ]
        with mock.patch(
            "backend.audience.extract_turns_from_transcript", return_value=fixed
        ):
            first = extract_listener_turns(self.db, transcript.id)
            second = extract_listener_turns(self.db, transcript.id)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        count = (
            self.db.query(ListenerTurn)
            .filter(ListenerTurn.style_transcript_id == transcript.id)
            .count()
        )
        self.assertEqual(count, 1)

    def test_overlapping_chunks_do_not_duplicate_turns(self):
        from backend.audience import extract_turns_from_transcript

        payloads = [
            {"turns": [{"question_verbatim": "Is the ROI worth it?", "outcome": "landed", "confidence": 0.5}]},
            {"turns": [{"question_verbatim": "Is the ROI worth it", "outcome": "not_landed", "confidence": 0.9}]},
        ]
        with mock.patch("backend.audience.chunk_sentences", return_value=[
            {"text": "Parent: Is the ROI worth it?"},
            {"text": "Parent: Is the ROI worth it?"},
        ]):
            turns = extract_turns_from_transcript(SAMPLE_VTT, curate=lambda _text: payloads.pop(0))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["outcome"], "not_landed")

    def test_unlabeled_lines_skipped_in_extraction_input(self):
        from backend.audience import listener_labeled_lines

        lines = listener_labeled_lines("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nno speaker here\n")
        self.assertEqual(lines, [])


class ListenerProfileTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.db.add(
            Recipe(
                ref="A1-1",
                audience_label="UG Parent",
                audience_cluster="A",
                duration="T1",
                channel="CH1",
                intent="I2",
                module_sequence="M01>M14",
                word_budget=200,
            )
        )
        transcript = StyleTranscript(
            name="AMA",
            raw_text="x",
            text_hash="h1",
            status="processed",
        )
        self.db.add(transcript)
        self.db.commit()
        self.db.add(
            StyleTranscriptPersona(
                style_transcript_id=transcript.id,
                persona_label="UG Parent",
            )
        )
        self.db.add(
            ListenerTurn(
                style_transcript_id=transcript.id,
                self_description="parent",
                concern="ROI",
                question_verbatim="Is ROI worth it?",
                reaction_after_answer="okay",
                outcome="landed",
                answer_summary="show report",
                confidence=0.8,
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_profile_versions(self):
        with mock.patch(
            "backend.audience.chat_json",
            return_value={"profile": "Parents worry about ROI and compare to IIM."},
        ):
            first = build_listener_profile(self.db, "UG Parent")
            second = build_listener_profile(self.db, "UG Parent")
        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)
        self.assertEqual(latest_listener_profile(self.db, "UG Parent"), second.profile_text)
        versions = [
            row.version
            for row in self.db.query(ListenerProfile)
            .filter(ListenerProfile.persona_label == "UG Parent")
            .all()
        ]
        self.assertEqual(sorted(versions), [1, 2])


class ListenerNotesTests(unittest.TestCase):
    def test_listener_notes_and_passed(self):
        result = {
            "sections": [
                {
                    "topic_id": 4,
                    "clarity": 2,
                    "lost_at": "then fees",
                    "grammar_issue": "We is",
                    "robotic_line": "leverage synergy",
                }
            ],
            "followability": 3,
            "sounds_human": 3,
            "grammar": 3,
            "takeaway": "confused",
        }
        notes = listener_notes(result, limit=8)
        self.assertTrue(any("lost the thread in section 4" in note for note in notes))
        self.assertTrue(any("machine-written" in note for note in notes))
        self.assertTrue(any("grammar" in note for note in notes))
        self.assertFalse(listener_passed(result))


class CalibrateTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine)()
        transcript = StyleTranscript(name="AMA", raw_text="x", text_hash="h2", status="processed")
        self.db.add(transcript)
        self.db.commit()
        self.db.add(
            StyleTranscriptPersona(style_transcript_id=transcript.id, persona_label="UG Parent")
        )
        for outcome in ("landed", "landed", "not_landed", "not_landed"):
            self.db.add(
                ListenerTurn(
                    style_transcript_id=transcript.id,
                    question_verbatim=f"Q about {outcome}",
                    outcome=outcome,
                    answer_summary="summary",
                    confidence=0.7,
                )
            )
        self.db.add(
            ListenerProfile(
                version=1,
                persona_label="UG Parent",
                profile_text="Parents care about ROI.",
                source_transcript_ids=str(transcript.id),
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_accuracy_math(self):
        predictions = iter(
            [
                {"predicted_outcome": "landed", "confidence": 0.9},
                {"predicted_outcome": "not_landed", "confidence": 0.5},
                {"predicted_outcome": "not_landed", "confidence": 0.8},
                {"predicted_outcome": "landed", "confidence": 0.4},
            ]
        )

        def fake_predict(**_kwargs):
            return next(predictions)

        result = calibrate_simulator(self.db, "UG Parent", predict=fake_predict)
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["correct"], 2)
        self.assertEqual(result["accuracy"], 0.5)
        self.assertEqual(result["confusion_matrix"]["landed_landed"], 1)
        self.assertEqual(result["confusion_matrix"]["landed_not_landed"], 1)
        self.assertEqual(result["confusion_matrix"]["not_landed_not_landed"], 1)
        self.assertEqual(result["confusion_matrix"]["not_landed_landed"], 1)
        self.assertTrue(result["grounded"])


class SimulateAudienceUnitTests(unittest.TestCase):
    def test_uses_listener_role(self):
        with mock.patch(
            "backend.pipeline.audience_sim.chat_json",
            return_value={
                "sections": [],
                "followability": 4,
                "sounds_human": 4,
                "grammar": 4,
                "takeaway": "visit",
            },
        ) as chat:
            result = simulate_audience(
                {"sections": [{"text": "Hello"}], "cta": "Visit"},
                persona_label="UG Parent",
                profile_text="",
                listener_examples=[],
                audience_cluster="A",
                duration="T1",
                channel="CH1",
                intent="I2",
                temperature="X2",
                context_note="",
            )
        self.assertFalse(result["grounded"])
        self.assertEqual(chat.call_args.kwargs.get("role"), "listener")


if __name__ == "__main__":
    unittest.main()
