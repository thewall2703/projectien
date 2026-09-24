from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from backend.transcript_search import (
    INDEXED_SOURCE_TYPES,
    SOURCE_DRIVE,
    SOURCE_MEDIA,
    SOURCE_QUOTE,
    SOURCE_STYLE,
    ask,
    chunk_text,
    collect_corpus,
    cosine_similarity,
    parse_embedding,
    rebuild_index,
    retrieve,
    upsert_prepared,
    PreparedChunk,
)


class ChunkingTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        chunks = chunk_text("one two three")
        self.assertEqual(chunks, ["one two three"])

    def test_long_text_overlaps(self):
        words = [f"w{i}" for i in range(100)]
        chunks = chunk_text(" ".join(words), chunk_words=40, overlap_words=10)
        self.assertGreater(len(chunks), 1)
        # Second chunk should start inside the first window.
        first_tail = chunks[0].split()[-10:]
        second_head = chunks[1].split()[:10]
        self.assertEqual(first_tail, second_head)

    def test_line_aware_chunks_keep_newlines(self):
        lines = [f"Speaker: sentence number {i}." for i in range(30)]
        chunks = chunk_text("\n".join(lines), chunk_words=40, overlap_words=8)
        self.assertGreater(len(chunks), 1)
        self.assertIn("\n", chunks[0])


class NormalizeDisplayTests(unittest.TestCase):
    def test_mashed_webvtt_becomes_sentences(self):
        from backend.transcript_search import format_source_sentences, normalize_transcript_text

        raw = (
            "444 01:04:02.029 → 01:04:07.268 pratham mittal: All right, we are done. "
            "445 01:04:07.649 → 01:04:18.249 pratham mittal: Next question about MU Ventures."
        )
        normalized = normalize_transcript_text(raw)
        self.assertNotIn("01:04:02", normalized)
        self.assertNotIn("444", normalized.split()[0] if normalized else "")
        display = format_source_sentences(raw)
        lines = [line for line in display.splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(any("MU Ventures" in line for line in lines))

    def test_trim_to_word_limit(self):
        from backend.transcript_search import trim_to_word_limit

        words = " ".join(f"w{i}" for i in range(250))
        clipped = trim_to_word_limit(words, 200)
        self.assertLessEqual(len(clipped.split()), 201)
        self.assertTrue(clipped.endswith("…"))


class RankingTests(unittest.TestCase):
    def test_cosine_identical_is_one(self):
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)

    def test_parse_embedding(self):
        self.assertEqual(parse_embedding("[1, 2.5]"), [1.0, 2.5])
        self.assertEqual(parse_embedding("nope"), [])


class CorpusExclusionTests(unittest.TestCase):
    def test_indexed_types_exclude_objections(self):
        self.assertNotIn("objection", INDEXED_SOURCE_TYPES)
        self.assertNotIn("qa", INDEXED_SOURCE_TYPES)
        self.assertEqual(
            INDEXED_SOURCE_TYPES,
            frozenset({SOURCE_STYLE, SOURCE_MEDIA, SOURCE_QUOTE, SOURCE_DRIVE}),
        )

    def test_collect_corpus_never_reads_objections(self):
        style = SimpleNamespace(id=1, name="Meeting", raw_text="hello world from a meeting")
        media = SimpleNamespace(id=2, asset_id=9, media_kind="video", transcript="video talk about MU")
        asset = SimpleNamespace(title="Campus walk")
        quote = SimpleNamespace(
            id=3,
            text="We teach by doing",
            status="approved",
            source_name="AMA",
            start_sec=1.0,
            end_sec=2.0,
        )
        objection = SimpleNamespace(id=99, question="Should never appear")

        class FakeQuery:
            def __init__(self, rows):
                self._rows = rows

            def order_by(self, *_args):
                return self

            def all(self):
                return self._rows

        class FakeDb:
            def query(self, model):
                name = getattr(model, "__name__", str(model))
                if name == "StyleTranscript":
                    return FakeQuery([style])
                if name == "MediaIndex":
                    return FakeQuery([media])
                if name == "FounderQuote":
                    return FakeQuery([quote])
                if name == "Objection":
                    raise AssertionError("objections must not be queried")
                raise AssertionError(f"unexpected model {name}")

            def get(self, model, key):
                if key == 9:
                    return asset
                return None

        with mock.patch("backend.transcript_search.Path.is_dir", return_value=False):
            prepared = collect_corpus(FakeDb())
        types = {item.source_type for item in prepared}
        self.assertEqual(types, {SOURCE_STYLE, SOURCE_MEDIA, SOURCE_QUOTE})
        self.assertTrue(all(item.source_type in INDEXED_SOURCE_TYPES for item in prepared))


class AskFlowTests(unittest.TestCase):
    def test_empty_index_message(self):
        class EmptyDb:
            def query(self, _model):
                return SimpleNamespace(all=lambda: [])

        result = ask(EmptyDb(), "What about placements?")
        self.assertIn("empty", result["answer_markdown"].lower())
        self.assertEqual(result["sources"], [])

    def test_ask_uses_retrieved_passages(self):
        chunk = SimpleNamespace(
            id=1,
            source_type=SOURCE_STYLE,
            source_id="1",
            source_name="Parent call",
            text="Median placement is twenty seven LPA.",
            start_ms=None,
            end_ms=None,
            text_hash="abc",
            embedding_json="[1.0, 0.0]",
        )

        class Db:
            def query(self, _model):
                return SimpleNamespace(all=lambda: [chunk])

        def embed_fn(texts):
            return [[1.0, 0.0] for _ in texts]

        def answer_fn(question, passages):
            self.assertEqual(question, "placements?")
            self.assertEqual(len(passages), 1)
            return {
                "answer_markdown": "The **median** is twenty seven LPA.",
                "segments": [
                    {
                        "text": "The **median** is twenty seven LPA.",
                        "source_indexes": [0],
                        "quote": "Median placement is twenty seven LPA.",
                    }
                ],
                "highlights": ["median"],
            }

        result = ask(
            Db(),
            "placements?",
            embed_fn=embed_fn,
            answer_fn=answer_fn,
        )
        self.assertIn("**median**", result["answer_markdown"])
        self.assertEqual(result["highlights"], ["median"])
        self.assertEqual(result["sources"][0]["source_name"], "Parent call")
        self.assertEqual(result["sources"][0]["highlight_lines"], [0])
        self.assertEqual(result["segments"][0]["targets"][0]["source_index"], 0)

    def test_match_quote_line_indexes(self):
        from backend.transcript_search import match_quote_line_indexes

        text = "Speaker: hello world.\nSpeaker: median placement is strong.\nSpeaker: goodbye."
        hits = match_quote_line_indexes(text, "median placement is strong")
        self.assertEqual(hits, [1])

    def test_answer_cache_hit_skips_answerer(self):
        from backend.transcript_search import (
            clear_ask_caches,
            normalize_question,
            question_cache_key,
            store_cached_answer,
        )

        clear_ask_caches(None)
        chunk = SimpleNamespace(
            id=1,
            source_type=SOURCE_STYLE,
            source_id="1",
            source_name="Parent call",
            text="Campus is in Gurugram.",
            start_ms=None,
            end_ms=None,
            text_hash="campus",
            embedding_json="[1.0, 0.0]",
        )
        cached_payload = {
            "answer_markdown": "Cached: campus is in **Gurugram**.",
            "segments": [],
            "highlights": ["Gurugram"],
            "sources": [],
        }
        stored: dict[str, Any] = {}

        class CacheRow:
            def __init__(self, question_hash, index_fingerprint, response_json, question=""):
                self.question_hash = question_hash
                self.index_fingerprint = index_fingerprint
                self.response_json = response_json
                self.question = question

        class CacheQuery:
            def __init__(self, rows):
                self._rows = rows

            def filter(self, *args, **kwargs):
                return self

            def first(self):
                key = question_cache_key("Where is campus?")
                for row in self._rows:
                    if row.question_hash == key:
                        return row
                return None

            def delete(self, synchronize_session=False):
                self._rows.clear()
                return 0

            def all(self):
                return list(self._rows)

        class Db:
            def __init__(self):
                self.cache_rows: list[CacheRow] = []
                self.added = []

            def query(self, model):
                name = getattr(model, "__name__", str(model))
                if name == "AskAnswerCache":
                    return CacheQuery(self.cache_rows)
                return SimpleNamespace(all=lambda: [chunk])

            def add(self, row):
                self.added.append(row)
                self.cache_rows.append(
                    CacheRow(
                        row.question_hash,
                        row.index_fingerprint,
                        row.response_json,
                        row.question,
                    )
                )

            def commit(self):
                return None

            def rollback(self):
                return None

        db = Db()
        # Seed cache as if a prior ask stored it.
        from backend.transcript_search import index_fingerprint

        fp = index_fingerprint(db)
        store_cached_answer(db, "Where is campus?", fp, cached_payload)

        calls = {"answer": 0, "embed": 0}

        def embed_fn(texts):
            calls["embed"] += 1
            return [[1.0, 0.0] for _ in texts]

        def answer_fn(question, passages):
            calls["answer"] += 1
            return cached_payload

        # Production path uses cache: no custom fns.
        hit = ask(db, "  WHERE   is   campus?  ")
        self.assertEqual(hit["answer_markdown"], cached_payload["answer_markdown"])
        self.assertEqual(calls["answer"], 0)
        self.assertEqual(normalize_question("  WHERE   is   campus?  "), "where is campus?")

        # Custom answerer bypasses cache and still works.
        miss = ask(db, "Where is campus?", embed_fn=embed_fn, answer_fn=answer_fn)
        self.assertEqual(miss["answer_markdown"], cached_payload["answer_markdown"])
        self.assertEqual(calls["answer"], 1)
        self.assertEqual(calls["embed"], 1)


class RebuildGuardTests(unittest.TestCase):
    def test_rebuild_skips_unknown_source_types(self):
        prepared = [
            PreparedChunk(SOURCE_STYLE, "1", "A", 0, "alpha"),
            PreparedChunk("objection", "9", "Bad", 0, "should not index"),
        ]
        written_batches: list[list[PreparedChunk]] = []

        def fake_upsert(db, chunks, embed_fn=None, commit=True):
            written_batches.append(list(chunks))
            return len(chunks)

        db = mock.Mock()
        db.query.return_value.delete.return_value = 0
        with mock.patch(
            "backend.transcript_search.collect_corpus", return_value=prepared
        ), mock.patch(
            "backend.transcript_search.upsert_prepared", side_effect=fake_upsert
        ):
            counts = rebuild_index(db, embed_fn=lambda texts: [[0.0] for _ in texts])
        self.assertEqual(counts["written"], 1)
        self.assertEqual(written_batches[0][0].source_type, SOURCE_STYLE)


if __name__ == "__main__":
    unittest.main()
