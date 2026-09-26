from __future__ import annotations

import json
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import PrathamMove, StyleTranscript, StyleTranscriptPersona, TranscriptChunk
from backend.pipeline.prompts import script_messages
from backend.pratham_playbook import (
    build_pratham_reference,
    extract_moves,
    parse_moves_payload,
)

KEYWORDS = ("gym", "owner", "parent", "hostel")

PRATHAM_VTT = """WEBVTT

00:00:01.000 --> 00:00:05.000
Pratham Mittal: Think of this campus like a gym. Paying the fee does not make you fit, showing up every day does.

00:00:05.000 --> 00:00:09.000
Pratham Mittal: A renter never fixes the leaking tap, an owner fixes it the same night. Be an owner here.
"""

MIXED_VTT = """WEBVTT

00:00:01.000 --> 00:00:05.000
Host: What about the hostel for parents who worry?

00:00:05.000 --> 00:00:09.000
Pratham Mittal: Every parent asks about the hostel and I tell them to come and stay a night.
"""


def fake_embed(texts: list[str]) -> list[list[float]]:
    return [[float(text.lower().count(word)) for word in KEYWORDS] + [0.01] for text in texts]


def vector(*weights: float) -> str:
    return json.dumps(list(weights) + [0.01])


class ParseMovesTests(unittest.TestCase):
    def test_keeps_only_verbatim_known_kinds(self):
        chunk = "Think of this campus like a gym. Paying the fee does not make you fit, showing up every day does."
        moves = parse_moves_payload(
            {
                "moves": [
                    {"kind": "Analogy", "label": "Campus as gym", "excerpt": "Think of this campus like a gym. "
                     "Paying the fee does not make you fit", "use_when": "effort", "personal": "false"},
                    {"kind": "analogy", "label": "Invented", "excerpt": "College is a marathon not a sprint for everyone"},
                    {"kind": "slogan", "label": "Bad kind", "excerpt": chunk},
                ]
            },
            chunk,
        )
        self.assertEqual([move["label"] for move in moves], ["Campus as gym"])
        self.assertFalse(moves[0]["personal"])


class PlaybookTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine, autoflush=False)()
        self.solo = StyleTranscript(name="Pratham only · A", raw_text=PRATHAM_VTT, text_hash="a", status="processed")
        self.mixed = StyleTranscript(name="Parents AMA", raw_text=MIXED_VTT, text_hash="b", status="processed")
        self.db.add_all([self.solo, self.mixed])
        self.db.commit()
        for transcript in (self.solo, self.mixed):
            self.db.add(StyleTranscriptPersona(style_transcript_id=transcript.id, persona_label="Current student"))
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_extract_dedupes_overlapping_moves_and_replaces_previous(self):
        excerpt = "A renter never fixes the leaking tap, an owner fixes it the same night."
        payload = {"moves": [{"kind": "framework", "label": "Renter vs owner", "excerpt": excerpt, "use_when": "ownership"}]}
        with mock.patch("backend.pratham_playbook._chunks", return_value=["A renter never fixes the leaking tap, "
                        "an owner fixes it the same night. Be an owner here."] * 2), mock.patch(
            "backend.generation_cache.bump_content_version"
        ):
            first = extract_moves(self.db, self.solo.id, extract=lambda _t: payload, embed_fn=fake_embed)
            second = extract_moves(self.db, self.solo.id, extract=lambda _t: payload, embed_fn=fake_embed)
        self.assertEqual(first["moves"], 1)
        self.assertEqual(second["moves"], 1)
        rows = self.db.query(PrathamMove).all()
        self.assertEqual(len(rows), 1)
        self.assertTrue(json.loads(rows[0].embedding_json))

    def test_reference_ranks_by_topic_and_uses_only_pratham_only_passages(self):
        self.db.add_all(
            [
                PrathamMove(style_transcript_id=self.solo.id, kind="analogy", label="Campus as gym",
                            excerpt="Think of this campus like a gym.", use_when="effort",
                            embedding_json=vector(3, 0, 0, 0), text_hash="m1", source_name=self.solo.name),
                PrathamMove(style_transcript_id=self.solo.id, kind="framework", label="Renter vs owner",
                            excerpt="Be an owner here.", use_when="ownership", personal=True,
                            embedding_json=vector(0, 3, 0, 0), text_hash="m2", source_name=self.solo.name),
                PrathamMove(style_transcript_id=999, kind="story", label="Other persona",
                            excerpt="Not for this persona.", embedding_json=vector(3, 3, 3, 3), text_hash="m3"),
                TranscriptChunk(source_type="style", source_id=str(self.solo.id), source_name=self.solo.name,
                                chunk_index=0, text="Pratham Mittal: Think of this campus like a gym.",
                                embedding_json=vector(3, 0, 0, 0)),
                TranscriptChunk(source_type="style", source_id=str(self.mixed.id), source_name=self.mixed.name,
                                chunk_index=0, text="Host: What about the hostel?",
                                embedding_json=vector(0, 0, 3, 3)),
            ]
        )
        self.db.commit()
        block = build_pratham_reference(
            self.db, persona_label="Current student", topics=["gym discipline"], embed_fn=fake_embed
        )
        self.assertIn("PRATHAM PLAYBOOK", block)
        self.assertLess(block.index("Campus as gym"), block.index("Renter vs owner"))
        self.assertIn("personal — attribute to Pratham in third person", block)
        self.assertNotIn("Other persona", block)
        self.assertIn("PRATHAM PASSAGES", block)
        self.assertNotIn("Host:", block)
        self.assertNotIn("Pratham Mittal:", block)

    def test_reference_skips_near_duplicate_moves(self):
        for index, kind in enumerate(("analogy", "story")):
            self.db.add(
                PrathamMove(style_transcript_id=self.solo.id, kind=kind, label=f"Gym take {index}",
                            excerpt="Think of this campus like a gym.", embedding_json=vector(3, 0, 0, 0),
                            text_hash=f"dup{index}", source_name=self.solo.name)
            )
        self.db.commit()
        block = build_pratham_reference(self.db, persona_label="Current student", topics=["gym"], embed_fn=fake_embed)
        self.assertEqual(block.count("Gym take"), 1)

    def test_reference_returns_moves_for_untagged_persona(self):
        self.db.add(
            PrathamMove(
                style_transcript_id=self.solo.id,
                kind="analogy",
                label="Campus as gym",
                excerpt="Think of this campus like a gym.",
                embedding_json=vector(3, 0, 0, 0),
                text_hash="untagged",
                source_name=self.solo.name,
            )
        )
        self.db.commit()
        block = build_pratham_reference(
            self.db, persona_label="Nobody", topics=["gym"], embed_fn=fake_embed
        )
        self.assertIn("Campus as gym", block)

    def test_passage_limit_zero_omits_passages(self):
        self.db.add(
            PrathamMove(
                style_transcript_id=self.solo.id,
                kind="analogy",
                label="Campus as gym",
                excerpt="Think of this campus like a gym.",
                embedding_json=vector(3, 0, 0, 0),
                text_hash="lim0",
                source_name=self.solo.name,
            )
        )
        self.db.add(
            TranscriptChunk(
                source_type="style",
                source_id=str(self.solo.id),
                source_name=self.solo.name,
                chunk_index=0,
                text="Think of this campus like a gym.",
                embedding_json=vector(3, 0, 0, 0),
            )
        )
        self.db.commit()
        from backend.pratham_playbook import select_reference

        selection = select_reference(
            self.db,
            persona_label="Current student",
            topics=["gym"],
            embed_fn=fake_embed,
            passage_limit=0,
        )
        self.assertEqual(selection["passages"], [])
        self.assertTrue(selection["moves"])


class PromptWiringTests(unittest.TestCase):
    def test_writer_prompt_carries_reference_and_rules_only_when_given(self):
        kwargs = dict(
            audience_cluster="A", duration="T2", channel="CH1", intent="I1", temperature="",
            context_note="", modules=[], sequence=[], facts=[], word_budget=240,
        )
        with_ref = script_messages(**kwargs, pratham_reference="PRATHAM PLAYBOOK — test block")
        without = script_messages(**kwargs)
        self.assertIn("PRATHAM PLAYBOOK — test block", with_ref[1]["content"])
        self.assertIn("PRATHAM BY BEAT AND PLAYBOOK", with_ref[0]["content"])
        self.assertNotIn("PRATHAM PLAYBOOK", without[0]["content"] + without[1]["content"])
        self.assertNotIn("PRATHAM BY BEAT", without[0]["content"] + without[1]["content"])


if __name__ == "__main__":
    unittest.main()
