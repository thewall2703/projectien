from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import FounderQuote, Recipe, StyleTranscript, StyleTranscriptPersona, VoiceStyleGuide
from backend.pipeline.prompts import script_messages, voice_review_messages
from backend.pipeline.style_guide import (
    DuplicateStyleTranscriptError,
    StyleTranscriptPersonaError,
    index_style_transcript,
    ingest_style_transcript,
    latest_style_guide,
    parse_webvtt,
    pratham_lines,
    store_style_transcript,
    update_style_transcript_personas,
)

SAMPLE_VTT = """WEBVTT

1
00:00:39.369 --> 00:00:42.359
shivas behl: Welcome back to the AMA.

2
00:00:42.359 --> 00:00:45.000
shivas behl: First question is from Ananya.

3
00:00:45.000 --> 00:00:50.000
pratham mittal: I disagree with your data.

4
00:00:50.000 --> 00:00:55.000
pratham mittal: but I understand the spirit of your question.
"""


def _is_distill(messages: list[dict[str, str]]) -> bool:
    system = messages[0]["content"] if messages else ""
    return "distilling how Pratham Mittal" in system


class ParseWebvttTests(unittest.TestCase):
    def test_strips_header_timestamps_and_merges_speakers(self):
        lines = parse_webvtt(SAMPLE_VTT)
        joined = "\n".join(lines)
        self.assertNotIn("WEBVTT", joined)
        self.assertNotIn("-->", joined)
        self.assertFalse(any(line.strip().isdigit() for line in lines))
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("shivas behl:"))
        self.assertIn("Welcome back to the AMA.", lines[0])
        self.assertIn("First question is from Ananya.", lines[0])
        self.assertTrue(lines[1].startswith("pratham mittal:"))
        self.assertIn("I disagree with your data.", lines[1])
        self.assertIn("but I understand the spirit of your question.", lines[1])

    def test_accepts_bom_and_leading_whitespace(self):
        lines = parse_webvtt("\ufeff\n  WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\npratham mittal: Hello\n")
        self.assertEqual(lines, ["pratham mittal: Hello"])

    def test_plain_text_fallback(self):
        lines = parse_webvtt("Opening remarks\n\nWe are live.\n")
        self.assertEqual(lines, ["Opening remarks", "We are live."])
        self.assertEqual(parse_webvtt("just a paragraph"), ["just a paragraph"])


class PrathamLinesTests(unittest.TestCase):
    def test_keeps_only_pratham_and_strips_prefix(self):
        text = pratham_lines(
            [
                "shivas behl: Welcome back to the AMA.",
                "pratham mittal: I disagree with your data.",
                "Pratham Mittal: Stay in India and build.",
                "guest: ignore me",
            ]
        )
        self.assertNotIn("pratham mittal:", text.lower())
        self.assertIn("I disagree with your data.", text)
        self.assertIn("Stay in India and build.", text)
        self.assertNotIn("Welcome back", text)
        self.assertNotIn("ignore me", text)


class IngestStyleTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.distill_users: list[str] = []
        self.db.add_all(
            [
                Recipe(
                    ref="A10-1",
                    audience_label="International / NRI applicant",
                    audience_cluster="A",
                    duration="T2",
                    channel="CH3",
                    intent="I2",
                    module_sequence="M01>M14",
                ),
                Recipe(
                    ref="A10-2",
                    audience_label="International / NRI applicant",
                    audience_cluster="A",
                    duration="T3",
                    channel="CH3",
                    intent="I2",
                    module_sequence="M01>M14",
                ),
                Recipe(
                    ref="A7-3",
                    audience_label="Mid-career executive",
                    audience_cluster="A",
                    duration="T4",
                    channel="CH3",
                    intent="I2",
                    module_sequence="M01>M14",
                ),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def _fake_chat_json(self, messages, **_kwargs):
        if _is_distill(messages):
            user = messages[1]["content"]
            self.distill_users.append(user)
            if "(empty)" in user:
                return {"guide": "GUIDE_V1 structure and style"}
            return {"guide": f"GUIDE_MERGED from {len(self.distill_users)}"}
        return {
            "snippets": [
                {
                    "text": "I disagree with your data.",
                    "topic": "founder",
                    "module_ids": ["M01"],
                    "keep": True,
                }
            ]
        }

    def test_stores_unindexed_then_indexes_explicitly(self):
        stored = store_style_transcript(self.db, "AMA staged", SAMPLE_VTT)
        self.assertEqual(stored.status, "uploaded")
        self.assertEqual(self.db.query(StyleTranscriptPersona).count(), 0)
        self.assertEqual(self.db.query(VoiceStyleGuide).count(), 0)
        self.assertEqual(self.db.query(FounderQuote).count(), 0)

        with mock.patch("backend.pipeline.style_guide.chat_json", side_effect=self._fake_chat_json), mock.patch(
            "backend.transcripts.chat_json", side_effect=self._fake_chat_json
        ):
            result = index_style_transcript(
                self.db,
                stored.id,
                ["International / NRI applicant"],
            )

        self.db.refresh(stored)
        self.assertEqual(stored.status, "processed")
        self.assertEqual(result["transcript_id"], stored.id)
        self.assertEqual(result["persona_labels"], ["International / NRI applicant"])
        self.assertEqual(self.db.query(StyleTranscriptPersona).count(), 1)
        self.assertTrue(latest_style_guide(self.db, "International / NRI applicant"))
        self.assertGreaterEqual(self.db.query(FounderQuote).count(), 1)

    def test_video_link_is_stored_and_carried_to_quotes(self):
        url = "https://drive.google.com/file/d/abc123/view"
        stored = store_style_transcript(self.db, "Parents session", SAMPLE_VTT, f"  {url}  ")
        self.assertEqual(stored.source_url, url)
        with mock.patch("backend.pipeline.style_guide.chat_json", side_effect=self._fake_chat_json), mock.patch(
            "backend.transcripts.chat_json", side_effect=self._fake_chat_json
        ):
            index_style_transcript(self.db, stored.id, ["International / NRI applicant"])
        quotes = self.db.query(FounderQuote).all()
        self.assertTrue(quotes)
        self.assertTrue(all(quote.source_url == url for quote in quotes))

    def test_same_quote_from_overlapping_chunks_is_stored_once(self):
        db = sessionmaker(bind=self.db.get_bind(), autoflush=False)()
        line = "pratham mittal: I disagree with your data. " + "We keep building every single day. " * 60
        stored = store_style_transcript(db, "Long session", f"{line}\n{line}")
        with mock.patch("backend.pipeline.style_guide.chat_json", side_effect=self._fake_chat_json), mock.patch(
            "backend.transcripts.chat_json", side_effect=self._fake_chat_json
        ):
            result = index_style_transcript(db, stored.id, ["International / NRI applicant"])
        self.assertEqual(result["quotes_kept"], 1)
        self.assertGreaterEqual(result["quotes_skipped"], 1)
        self.assertEqual(db.query(FounderQuote).count(), 1)
        db.close()

    def test_rejects_non_http_video_link(self):
        with self.assertRaises(ValueError):
            store_style_transcript(self.db, "Bad link", SAMPLE_VTT, "drive/file/abc")

    def test_creates_persona_scoped_guides_and_rejects_duplicate(self):
        with mock.patch("backend.pipeline.style_guide.chat_json", side_effect=self._fake_chat_json), mock.patch(
            "backend.transcripts.chat_json", side_effect=self._fake_chat_json
        ):
            first = ingest_style_transcript(
                self.db,
                "AMA one",
                SAMPLE_VTT,
                persona_labels=["International / NRI applicant"],
            )
            second = ingest_style_transcript(
                self.db,
                "AMA two",
                "pratham mittal: Stay in India and do not come to Masters Union.\n",
                persona_labels=["International / NRI applicant", "Mid-career executive"],
            )
            with self.assertRaises(DuplicateStyleTranscriptError):
                ingest_style_transcript(
                    self.db,
                    "AMA one again",
                    SAMPLE_VTT,
                    persona_labels=["International / NRI applicant"],
                )

        self.assertEqual(first["persona_labels"], ["International / NRI applicant"])
        self.assertEqual(
            second["persona_labels"],
            ["International / NRI applicant", "Mid-career executive"],
        )
        self.assertGreaterEqual(first["quotes_kept"], 1)
        self.assertEqual(self.db.query(StyleTranscript).count(), 2)
        self.assertEqual(self.db.query(StyleTranscriptPersona).count(), 3)

        nri_guide = latest_style_guide(self.db, "International / NRI applicant")
        exec_guide = latest_style_guide(self.db, "Mid-career executive")
        global_guide = latest_style_guide(self.db, "")
        self.assertTrue(nri_guide)
        self.assertTrue(exec_guide)
        self.assertEqual(global_guide, "")
        self.assertNotEqual(nri_guide, exec_guide)

        quotes = self.db.query(FounderQuote).all()
        self.assertTrue(quotes)
        self.assertTrue(all(quote.speaker == "Pratham Mittal" for quote in quotes))
        self.assertTrue(all(quote.source_style_transcript_id > 0 for quote in quotes))

    def test_rejects_unknown_or_empty_personas(self):
        with self.assertRaises(StyleTranscriptPersonaError):
            ingest_style_transcript(self.db, "AMA", SAMPLE_VTT, persona_labels=[])
        with self.assertRaises(StyleTranscriptPersonaError):
            ingest_style_transcript(
                self.db,
                "AMA",
                SAMPLE_VTT,
                persona_labels=["Totally Fake Persona"],
            )

    def test_reassignment_rebuilds_only_affected_personas(self):
        with mock.patch("backend.pipeline.style_guide.chat_json", side_effect=self._fake_chat_json), mock.patch(
            "backend.transcripts.chat_json", side_effect=self._fake_chat_json
        ):
            first = ingest_style_transcript(
                self.db,
                "AMA one",
                SAMPLE_VTT,
                persona_labels=["International / NRI applicant"],
            )
            update_style_transcript_personas(
                self.db,
                first["transcript_id"],
                ["Mid-career executive"],
            )

        links = (
            self.db.query(StyleTranscriptPersona)
            .filter(StyleTranscriptPersona.style_transcript_id == first["transcript_id"])
            .all()
        )
        self.assertEqual([row.persona_label for row in links], ["Mid-career executive"])
        self.assertEqual(latest_style_guide(self.db, "International / NRI applicant"), "")
        self.assertTrue(latest_style_guide(self.db, "Mid-career executive"))

        # Global legacy guides must never become a persona fallback.
        self.db.add(
            VoiceStyleGuide(
                version=99,
                persona_label="",
                guide_text="LEGACY GLOBAL",
                source_transcript_ids="1",
            )
        )
        self.db.commit()
        self.assertEqual(latest_style_guide(self.db, "International / NRI applicant"), "")
        self.assertNotEqual(latest_style_guide(self.db, "Mid-career executive"), "LEGACY GLOBAL")


class ScriptMessageStyleGuideTests(unittest.TestCase):
    def _messages(self, style_guide: str = ""):
        return script_messages(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X3",
            context_note="",
            modules=[],
            sequence=["M01"],
            facts=[],
            word_budget=200,
            style_guide=style_guide,
        )

    def test_includes_guide_block_when_present(self):
        messages = self._messages("Open in three parts. Be direct.")
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("PRATHAM-DERIVED EMPLOYEE STYLE GUIDE", user)
        self.assertIn("Open in three parts. Be direct.", user)
        self.assertIn("complements FOUNDER VOICE", system)
        self.assertIn("transferable structure and register", system)
        self.assertIn("Masters' Union EMPLOYEE", system)
        self.assertIn("not Pratham Mittal", system)
        self.assertIn("third-person reference to Pratham is correct", system)
        self.assertIn("Excerpt sentences and phrasing may be reused", system)
        self.assertIn("Never first-person founder biography", system)
        self.assertNotIn("Do not quote or closely paraphrase", system)
        self.assertIn("Masters' Union EMPLOYEE", system)
        self.assertIn("not Pratham Mittal", system)

    def test_omits_guide_block_when_empty(self):
        messages = self._messages("")
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertNotIn("PRATHAM-DERIVED EMPLOYEE STYLE GUIDE", system)
        self.assertNotIn("PRATHAM-DERIVED EMPLOYEE STYLE GUIDE", user)
        self.assertNotIn("complements FOUNDER VOICE", system)
        review = voice_review_messages({"sections": [], "cta": "Visit"}, [], style_guide="")
        self.assertNotIn("PRATHAM-DERIVED EMPLOYEE STYLE GUIDE", review[0]["content"])
        self.assertNotIn("PRATHAM-DERIVED EMPLOYEE STYLE GUIDE", review[1]["content"])

    def test_voice_review_checks_employee_style_not_founder_identity(self):
        review = voice_review_messages(
            {
                "sections": [
                    {"text": "Pratham started Masters' Union because he wanted education to feel real."}
                ],
                "cta": "Come visit us.",
            },
            [],
            style_guide="Use direct language. Hindi can help in some informal settings.",
            duration="T1",
            channel="CH3",
            intent="I2",
            context_note="Prospective student meeting",
        )
        system = review[0]["content"]
        user = review[1]["content"]
        self.assertIn("EMPLOYEE or representative", system)
        self.assertIn("NOT the speaker", system)
        self.assertIn("Third-person references to Pratham are correct", system)
        self.assertIn("NEVER fail", system)
        self.assertIn("absence of Hindi", system)
        self.assertIn("attributed quotes and anecdotes", system)
        self.assertNotIn("copies or closely paraphrases transcript sentences", system)
        self.assertNotIn("style samples, not an authoritative", system)
        self.assertIn("Duration: T1", user)
        self.assertIn("Channel: CH3", user)
        self.assertIn("Intent: I2", user)

    def test_distillation_guide_is_employee_safe_and_contextual(self):
        from backend.pipeline.style_guide import DISTILL_SYSTEM

        self.assertIn("EMPLOYEES", DISTILL_SYSTEM)
        self.assertIn("never the implied speaker", DISTILL_SYSTEM)
        self.assertIn("WHEN USEFUL", DISTILL_SYSTEM)
        self.assertIn("FORMAT-SPECIFIC", DISTILL_SYSTEM)
        self.assertIn("not verified facts", DISTILL_SYSTEM)


if __name__ == "__main__":
    unittest.main()
