from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.pipeline.prompts import script_messages
from backend.transcripts import (
    chunk_sentences,
    format_timestamp,
    is_verbatim,
    normalize_module_ids,
    parse_curation_payload,
    pick_founder_quotes,
    quote_hash,
)


class ChunkTests(unittest.TestCase):
    def test_chunk_respects_word_target_and_times(self):
        sentences = [
            {"text": f"word{index}", "start": float(index), "end": float(index) + 0.5}
            for index in range(1, 250)
        ]
        chunks = chunk_sentences(sentences, target_words=50, overlap=10)
        self.assertGreaterEqual(len(chunks), 4)
        self.assertLessEqual(len(chunks[0]["text"].split()), 60)
        self.assertGreaterEqual(chunks[0]["end"], chunks[0]["start"])


class ModuleIdTests(unittest.TestCase):
    def test_normalizes_and_dedupes(self):
        self.assertEqual(normalize_module_ids(["m7", "M13", "M99", "M07"]), "M07,M13")
        self.assertEqual(normalize_module_ids("M2, M04"), "M02,M04")
        self.assertEqual(normalize_module_ids(""), "")


class CurationParseTests(unittest.TestCase):
    def test_keeps_only_allowed_topics(self):
        snippets = parse_curation_payload(
            {
                "snippets": [
                    {
                        "text": "Stay in India and build at Masters' Union.",
                        "topic": "vision",
                        "module_ids": ["M12"],
                        "keep": True,
                    },
                    {"text": "CRISPR can rewrite DNA.", "topic": "science", "keep": True},
                    {"text": "Ignore me", "topic": "students", "keep": False},
                ]
            }
        )
        self.assertEqual(len(snippets), 1)
        self.assertEqual(snippets[0]["topic"], "vision")
        self.assertEqual(snippets[0]["module_ids"], "M12")


class VerbatimTests(unittest.TestCase):
    def test_whitespace_normalized_substring(self):
        source = "Welcome to the union.  Your class of 2030."
        self.assertTrue(is_verbatim("Welcome to the union. Your class of 2030.", source))
        self.assertFalse(is_verbatim("Welcome to another union.", source))


class QuoteSelectTests(unittest.TestCase):
    def test_selects_by_sequence_and_topic_diversity(self):
        quotes = [
            SimpleNamespace(id=1, status="approved", topic="vision", module_ids="M12"),
            SimpleNamespace(id=2, status="approved", topic="vision", module_ids="M12"),
            SimpleNamespace(id=3, status="approved", topic="students", module_ids="M07,M13"),
            SimpleNamespace(id=4, status="rejected", topic="students", module_ids="M07"),
        ]
        selected = pick_founder_quotes(quotes, ["M02", "M07", "M12", "M13"], limit=3)
        ids = [item.id for item in selected]
        self.assertIn(1, ids)
        self.assertIn(3, ids)
        self.assertNotIn(4, ids)


class PromptVoiceTests(unittest.TestCase):
    def test_script_messages_include_founder_voice(self):
        quote = SimpleNamespace(
            text="Stay in India and ride the wave.",
            topic="vision",
            module_ids="M12",
            speaker="Pratham Mittal",
            source_name="C0005.MP4",
            source_file_id="abc",
            start_sec=750,
        )
        messages = script_messages(
            audience_cluster="A",
            duration="T1",
            channel="CH1",
            intent="I2",
            temperature="X3",
            context_note="",
            modules=[],
            sequence=["M12"],
            facts=[],
            word_budget=200,
            founder_quotes=[quote],
        )
        user = messages[1]["content"]
        self.assertIn("FOUNDER VOICE (Pratham Mittal)", user)
        self.assertIn("Stay in India and ride the wave.", user)
        self.assertIn("C0005.MP4 @ 12:30", user)
        self.assertIn("all numbers must come from LOCKED facts", messages[0]["content"])


class HashTests(unittest.TestCase):
    def test_hash_is_stable(self):
        first = quote_hash("file", "  Hello   world ")
        second = quote_hash("file", "Hello world")
        self.assertEqual(first, second)
        self.assertNotEqual(first, quote_hash("other", "Hello world"))
        self.assertEqual(format_timestamp(750), "12:30")


if __name__ == "__main__":
    unittest.main()
