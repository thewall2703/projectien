from __future__ import annotations

import unittest
from types import SimpleNamespace
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
