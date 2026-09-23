"""Semantic index over ingested transcripts for Library → Ask the transcripts.

Corpus (combined): style transcripts, media_index video STT, founder quotes, and
Drive file bundles under output/transcripts/. Objections and AMA Q&A candidates
are never indexed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx
from sqlalchemy.orm import Session

from backend.config import REPO_ROOT, settings
from backend.models import Asset, FounderQuote, MediaIndex, StyleTranscript, TranscriptChunk

logger = logging.getLogger(__name__)

OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"

SOURCE_STYLE = "style"
SOURCE_MEDIA = "media"
SOURCE_QUOTE = "quote"
SOURCE_DRIVE = "drive"
INDEXED_SOURCE_TYPES = frozenset({SOURCE_STYLE, SOURCE_MEDIA, SOURCE_QUOTE, SOURCE_DRIVE})

# Approximate tokens as words for chunk sizing (English spoken transcripts).
CHUNK_WORDS = 550
CHUNK_OVERLAP_WORDS = 80
TOP_K = 12
EMBED_BATCH = 32

ASK_SYSTEM = (
    "You answer questions using ONLY the provided transcript passages from "
    "Masters' Union conversations and materials. Keep the answer to **200 words "
    "or fewer** — concise, not a wall of text. Use short paragraphs or a few "
    "bullets. Wrap the most important figures, claims, and takeaways in "
    "**bold** markdown. Cite sources by their source_name when natural. If the "
    "passages do not support an answer, say so plainly — do not invent facts. "
    'Return strict JSON: {"answer_markdown":"...","highlights":["..."]}.'
)

MAX_ANSWER_WORDS = 200

# WebVTT cue noise that often gets mashed into one line in stored uploads.
_CUE_NOISE_RE = re.compile(
    r"(?:^|\s+)\d{1,5}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\s*(?:-->|→)\s*"
    r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s*",
    re.MULTILINE,
)
_STANDALONE_TIMESTAMP_RE = re.compile(
    r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\s*(?:-->|→)\s*\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"
)

EmbedFn = Callable[[list[str]], list[list[float]]]
AnswerFn = Callable[[str, list[dict[str, Any]]], dict[str, Any]]


@dataclass(frozen=True)
class PreparedChunk:
    source_type: str
    source_id: str
    source_name: str
    chunk_index: int
    text: str
    start_ms: int | None = None
    end_ms: int | None = None


def _word_list(text: str) -> list[str]:
    return [part for part in re.split(r"\s+", (text or "").strip()) if part]


def normalize_transcript_text(text: str) -> str:
    """Strip WebVTT cue numbers/timestamps into one utterance per line.

    Style uploads often store raw WebVTT. When that is chunked by words the cue
    metadata becomes an unreadable wall of text in search results. Prefer the
    existing WebVTT parser when the file is well-formed; otherwise peel cue
    noise out of mashed single-line transcripts.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    from backend.pipeline.style_guide import parse_webvtt

    probe = raw.lstrip("\ufeff \t\r\n")
    if probe.upper().startswith("WEBVTT") or "-->" in raw or "→" in raw:
        lines = parse_webvtt(raw)
        if lines:
            joined = "\n".join(lines)
            # If cue numbers/timestamps survived (newlines were lost upstream), scrub.
            if not _CUE_NOISE_RE.search(joined) and not re.search(
                r"\d{1,5}\s+\d{2}:\d{2}:\d{2}", joined
            ):
                return joined

    scrubbed = _CUE_NOISE_RE.sub("\n", raw)
    scrubbed = _STANDALONE_TIMESTAMP_RE.sub("\n", scrubbed)
    scrubbed = re.sub(r"\n{2,}", "\n", scrubbed)
    # Split on speaker labels for mashed "name: utter name: utter" runs.
    pieces: list[str] = []
    for block in scrubbed.splitlines():
        block = block.strip()
        if not block:
            continue
        # Drop leftover bare cue numbers.
        if re.fullmatch(r"\d{1,5}", block):
            continue
        parts = re.split(r"(?=\b[A-Za-z][A-Za-z .'-]{1,40}:\s)", block)
        for part in parts:
            part = part.strip(" -\t")
            if part:
                pieces.append(part)
    if not pieces:
        return " ".join(_word_list(raw))
    return "\n".join(pieces)


def format_source_sentences(text: str) -> str:
    """One clear sentence/utterance per line for UI display."""
    normalized = normalize_transcript_text(text)
    if not normalized:
        return ""
    lines: list[str] = []
    for line in normalized.splitlines():
        line = line.strip()
        if not line:
            continue
        # Further split long lines on sentence boundaries when no speaker breaks.
        if ": " in line[:60]:
            lines.append(line)
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'])", line)
        for part in parts:
            part = part.strip()
            if part:
                lines.append(part)
    return "\n".join(lines)


def trim_to_word_limit(text: str, limit: int = MAX_ANSWER_WORDS) -> str:
    """Hard-cap answer length while keeping markdown **bold** markers intact."""
    words = _word_list(text)
    if len(words) <= limit:
        return (text or "").strip()
    clipped = " ".join(words[:limit]).rstrip(" ,;:")
    if not clipped.endswith((".", "!", "?", "…")):
        clipped += "…"
    return clipped


def chunk_text(
    text: str,
    *,
    chunk_words: int = CHUNK_WORDS,
    overlap_words: int = CHUNK_OVERLAP_WORDS,
) -> list[str]:
    """Split plain text into overlapping windows, preserving line breaks when present."""
    normalized = (text or "").strip()
    if not normalized:
        return []
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if len(lines) >= 2:
        packed: list[str] = []
        buffer: list[str] = []
        buf_words = 0
        for line in lines:
            line_words = len(_word_list(line))
            if buffer and buf_words + line_words > chunk_words:
                packed.append("\n".join(buffer))
                # Overlap by trailing lines until we cover ~overlap_words.
                keep: list[str] = []
                keep_words = 0
                for prior in reversed(buffer):
                    keep.insert(0, prior)
                    keep_words += len(_word_list(prior))
                    if keep_words >= overlap_words:
                        break
                buffer = keep
                buf_words = keep_words
            buffer.append(line)
            buf_words += line_words
        if buffer:
            packed.append("\n".join(buffer))
        return packed

    words = _word_list(normalized)
    if len(words) <= chunk_words:
        return [" ".join(words)]
    step = max(1, chunk_words - overlap_words)
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(len(words), start + chunk_words)
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start += step
    return chunks


def chunk_timed_sentences(
    sentences: Sequence[MappingLike],
    *,
    chunk_words: int = CHUNK_WORDS,
    overlap_words: int = CHUNK_OVERLAP_WORDS,
) -> list[tuple[str, int | None, int | None]]:
    """Chunk Drive JSON sentences while preserving start/end ms when present."""
    rows: list[tuple[str, float, float]] = []
    for item in sentences:
        if isinstance(item, dict):
            body = str(item.get("text") or "").strip()
            start = float(item.get("start") or item.get("start_sec") or 0.0)
            end = float(item.get("end") or item.get("end_sec") or start)
        else:
            continue
        if body:
            rows.append((body, start, end))
    if not rows:
        return []

    packed: list[tuple[str, int | None, int | None]] = []
    buffer: list[str] = []
    buf_start: float | None = None
    buf_end: float | None = None
    buf_words = 0

    def flush() -> None:
        nonlocal buffer, buf_start, buf_end, buf_words
        if not buffer:
            return
        packed.append(
            (
                " ".join(buffer),
                int(buf_start * 1000) if buf_start is not None else None,
                int(buf_end * 1000) if buf_end is not None else None,
            )
        )
        # Overlap: keep the last overlap_words of the buffer.
        if overlap_words > 0 and buf_words > overlap_words:
            kept = _word_list(" ".join(buffer))[-overlap_words:]
            buffer = kept
            buf_words = len(kept)
            # Timestamps for the overlap window are approximate.
            buf_start = buf_end
        else:
            buffer = []
            buf_words = 0
            buf_start = None
            buf_end = None

    for body, start, end in rows:
        words = _word_list(body)
        if not words:
            continue
        if buf_start is None:
            buf_start = start
        buffer.append(body)
        buf_end = end
        buf_words += len(words)
        if buf_words >= chunk_words:
            flush()
    flush()
    return packed


# Typing alias without importing Mapping for sentence dicts.
MappingLike = dict[str, Any]


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for left, right in zip(a, b):
        dot += left * right
        norm_a += left * left
        norm_b += right * right
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def parse_embedding(raw: str) -> list[float]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    values: list[float] = []
    for item in payload:
        try:
            values.append(float(item))
        except (TypeError, ValueError):
            return []
    return values


def default_embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts via OpenRouter embeddings API."""
    if not texts:
        return []
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:5173",
        "X-Title": "Pitch Studio",
    }
    out: list[list[float]] = []
    with httpx.Client(timeout=120.0) as client:
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start : start + EMBED_BATCH]
            response = client.post(
                OPENROUTER_EMBEDDINGS_URL,
                headers=headers,
                json={
                    "model": settings.openrouter_embedding_model,
                    "input": batch,
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Embedding failed ({response.status_code}): {response.text[:400]}"
                )
            body = response.json()
            rows = body.get("data") if isinstance(body, dict) else None
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise RuntimeError("Embedding response missing vectors")
            ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
            for row in ordered:
                vector = row.get("embedding")
                if not isinstance(vector, list):
                    raise RuntimeError("Embedding row missing vector")
                out.append([float(value) for value in vector])
    return out


def _prepared_from_plain(
    *,
    source_type: str,
    source_id: str,
    source_name: str,
    text: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[PreparedChunk]:
    body = normalize_transcript_text(text)
    if not body:
        return []
    pieces = chunk_text(body)
    return [
        PreparedChunk(
            source_type=source_type,
            source_id=source_id,
            source_name=source_name or source_id,
            chunk_index=index,
            text=piece,
            start_ms=start_ms if index == 0 else None,
            end_ms=end_ms if index == len(pieces) - 1 else None,
        )
        for index, piece in enumerate(pieces)
    ]


def collect_corpus(db: Session, drive_root: Path | None = None) -> list[PreparedChunk]:
    """Build prepared chunks from all allowed sources (never objections)."""
    prepared: list[PreparedChunk] = []

    for row in db.query(StyleTranscript).order_by(StyleTranscript.id).all():
        prepared.extend(
            _prepared_from_plain(
                source_type=SOURCE_STYLE,
                source_id=str(row.id),
                source_name=row.name or f"Style transcript {row.id}",
                text=row.raw_text or "",
            )
        )

    for row in db.query(MediaIndex).order_by(MediaIndex.id).all():
        if (row.media_kind or "") != "video":
            continue
        body = (row.transcript or "").strip()
        if not body:
            continue
        asset = db.get(Asset, row.asset_id)
        name = (asset.title if asset is not None else "") or f"Media {row.id}"
        prepared.extend(
            _prepared_from_plain(
                source_type=SOURCE_MEDIA,
                source_id=str(row.id),
                source_name=name,
                text=body,
            )
        )

    for row in db.query(FounderQuote).order_by(FounderQuote.id).all():
        if (row.status or "approved") != "approved":
            continue
        body = (row.text or "").strip()
        if not body:
            continue
        start_ms = int(float(row.start_sec or 0.0) * 1000) if row.start_sec else None
        end_ms = int(float(row.end_sec or 0.0) * 1000) if row.end_sec else None
        prepared.append(
            PreparedChunk(
                source_type=SOURCE_QUOTE,
                source_id=str(row.id),
                source_name=row.source_name or f"Quote {row.id}",
                chunk_index=0,
                text=body,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )

    directory = Path(drive_root or (REPO_ROOT / "output" / "transcripts"))
    if directory.is_dir():
        from backend.transcripts import discover_transcripts, load_transcript

        for path in discover_transcripts(directory):
            try:
                transcript = load_transcript(path)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to load drive transcript %s", path)
                continue
            file_id = str(transcript.get("file_id") or path.stem)
            name = str(transcript.get("source_name") or path.stem)
            sentences = transcript.get("sentences") or []
            if isinstance(sentences, list) and sentences:
                timed = chunk_timed_sentences(sentences)
                for index, (piece, start_ms, end_ms) in enumerate(timed):
                    prepared.append(
                        PreparedChunk(
                            source_type=SOURCE_DRIVE,
                            source_id=file_id,
                            source_name=name,
                            chunk_index=index,
                            text=piece,
                            start_ms=start_ms,
                            end_ms=end_ms,
                        )
                    )
            else:
                prepared.extend(
                    _prepared_from_plain(
                        source_type=SOURCE_DRIVE,
                        source_id=file_id,
                        source_name=name,
                        text=str(transcript.get("text") or ""),
                    )
                )

    return prepared


def _delete_source(db: Session, source_type: str, source_id: str) -> None:
    (
        db.query(TranscriptChunk)
        .filter(
            TranscriptChunk.source_type == source_type,
            TranscriptChunk.source_id == source_id,
        )
        .delete(synchronize_session=False)
    )


def upsert_prepared(
    db: Session,
    chunks: Sequence[PreparedChunk],
    *,
    embed_fn: EmbedFn | None = None,
    commit: bool = True,
) -> int:
    """Replace chunks for the sources present in ``chunks`` and embed them."""
    if not chunks:
        return 0
    embed = embed_fn or default_embed_texts
    by_source: dict[tuple[str, str], list[PreparedChunk]] = {}
    for chunk in chunks:
        if chunk.source_type not in INDEXED_SOURCE_TYPES:
            continue
        by_source.setdefault((chunk.source_type, chunk.source_id), []).append(chunk)

    written = 0
    for (source_type, source_id), group in by_source.items():
        group = sorted(group, key=lambda item: item.chunk_index)
        _delete_source(db, source_type, source_id)
        texts = [item.text for item in group]
        vectors = embed(texts)
        if len(vectors) != len(group):
            raise RuntimeError("Embedding count does not match chunk count")
        for item, vector in zip(group, vectors):
            db.add(
                TranscriptChunk(
                    source_type=item.source_type,
                    source_id=item.source_id,
                    source_name=item.source_name,
                    chunk_index=item.chunk_index,
                    text=item.text,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                    text_hash=text_hash(item.text),
                    embedding_json=json.dumps(vector),
                )
            )
            written += 1
    if commit:
        db.commit()
    return written


def upsert_source(
    db: Session,
    *,
    source_type: str,
    source_id: str,
    source_name: str,
    text: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    embed_fn: EmbedFn | None = None,
) -> int:
    chunks = _prepared_from_plain(
        source_type=source_type,
        source_id=source_id,
        source_name=source_name,
        text=text,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    if not chunks:
        _delete_source(db, source_type, source_id)
        db.commit()
        return 0
    return upsert_prepared(db, chunks, embed_fn=embed_fn, commit=True)


def safe_upsert_source(**kwargs: Any) -> None:
    """Best-effort hook wrapper — never fail the caller’s primary write."""
    try:
        upsert_source(**kwargs)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Transcript index upsert failed for %s:%s",
            kwargs.get("source_type"),
            kwargs.get("source_id"),
        )
        db = kwargs.get("db")
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass


def rebuild_index(
    db: Session,
    *,
    drive_root: Path | None = None,
    embed_fn: EmbedFn | None = None,
) -> dict[str, int]:
    """Wipe and rebuild the full semantic index from allowed sources only."""
    deleted = db.query(TranscriptChunk).delete(synchronize_session=False)
    db.commit()
    prepared = collect_corpus(db, drive_root=drive_root)
    # Guard: never index objection-looking types.
    prepared = [item for item in prepared if item.source_type in INDEXED_SOURCE_TYPES]
    written = upsert_prepared(db, prepared, embed_fn=embed_fn, commit=True)
    counts = {
        "deleted": int(deleted or 0),
        "written": written,
        "style": sum(1 for item in prepared if item.source_type == SOURCE_STYLE),
        "media": sum(1 for item in prepared if item.source_type == SOURCE_MEDIA),
        "quote": sum(1 for item in prepared if item.source_type == SOURCE_QUOTE),
        "drive": sum(1 for item in prepared if item.source_type == SOURCE_DRIVE),
    }
    return counts


def _dedupe_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop near-duplicate passages (same source + high text overlap)."""
    kept: list[dict[str, Any]] = []
    for hit in hits:
        hit_words = {word.lower() for word in _word_list(str(hit.get("text") or ""))}
        duplicate = False
        for other in kept:
            if other.get("source_type") != hit.get("source_type"):
                continue
            if other.get("source_id") != hit.get("source_id"):
                continue
            other_words = {word.lower() for word in _word_list(str(other.get("text") or ""))}
            if not hit_words or not other_words:
                continue
            overlap = len(hit_words & other_words) / max(1, min(len(hit_words), len(other_words)))
            if overlap >= 0.85:
                duplicate = True
                break
        if not duplicate:
            kept.append(hit)
    return kept


def retrieve(
    db: Session,
    question: str,
    *,
    top_k: int = TOP_K,
    embed_fn: EmbedFn | None = None,
) -> list[dict[str, Any]]:
    query = (question or "").strip()
    if not query:
        return []
    rows = db.query(TranscriptChunk).all()
    if not rows:
        return []
    embed = embed_fn or default_embed_texts
    query_vector = embed([query])[0]
    scored: list[dict[str, Any]] = []
    for row in rows:
        vector = parse_embedding(row.embedding_json)
        if not vector:
            continue
        score = cosine_similarity(query_vector, vector)
        scored.append(
            {
                "id": row.id,
                "source_type": row.source_type,
                "source_id": row.source_id,
                "source_name": row.source_name,
                "text": row.text,
                "start_ms": row.start_ms,
                "end_ms": row.end_ms,
                "score": round(float(score), 6),
            }
        )
    scored.sort(key=lambda item: item["score"], reverse=True)
    return _dedupe_hits(scored)[: max(1, top_k)]


def _default_answer_fn(question: str, passages: list[dict[str, Any]]) -> dict[str, Any]:
    from backend.pipeline.llm import chat_json

    packed = [
        {
            "source_name": item.get("source_name"),
            "source_type": item.get("source_type"),
            "text": item.get("text"),
        }
        for item in passages
    ]
    payload = chat_json(
        [
            {"role": "system", "content": ASK_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {"question": question, "passages": packed},
                    ensure_ascii=False,
                ),
            },
        ],
        timeout=180.0,
        model=settings.openrouter_model,
        reasoning=True,
        max_tokens=900,
    )
    answer = str(payload.get("answer_markdown") or "").strip()
    answer = trim_to_word_limit(answer, MAX_ANSWER_WORDS)
    highlights = payload.get("highlights") or []
    if not isinstance(highlights, list):
        highlights = []
    highlights = [str(item).strip() for item in highlights if str(item).strip()]
    if not highlights:
        highlights = re.findall(r"\*\*(.+?)\*\*", answer)
    return {"answer_markdown": answer, "highlights": highlights}


def ask(
    db: Session,
    question: str,
    *,
    embed_fn: EmbedFn | None = None,
    answer_fn: AnswerFn | None = None,
    top_k: int = TOP_K,
) -> dict[str, Any]:
    """Retrieve relevant passages and write a long grounded answer."""
    query = (question or "").strip()
    if not query:
        return {
            "answer_markdown": "Ask a question about the ingested transcripts.",
            "highlights": [],
            "sources": [],
        }
    sources = retrieve(db, query, top_k=top_k, embed_fn=embed_fn)
    if not sources:
        return {
            "answer_markdown": (
                "The transcript index is empty, or nothing relevant was found. "
                "An admin needs to rebuild the index after uploading transcripts."
            ),
            "highlights": [],
            "sources": [],
        }
    answerer = answer_fn or _default_answer_fn
    result = answerer(query, sources)
    answer = trim_to_word_limit(str(result.get("answer_markdown") or "").strip(), MAX_ANSWER_WORDS)
    return {
        "answer_markdown": answer,
        "highlights": list(result.get("highlights") or []),
        "sources": [
            {
                "source_type": item["source_type"],
                "source_name": item["source_name"],
                "text": format_source_sentences(str(item.get("text") or "")),
                "score": item["score"],
                "start_ms": item.get("start_ms"),
                "end_ms": item.get("end_ms"),
            }
            for item in sources
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the transcript semantic index")
    parser.add_argument("command", choices=["rebuild"], help="Index command")
    parser.add_argument(
        "--dir",
        default=str(REPO_ROOT / "output" / "transcripts"),
        help="Drive transcript bundle root",
    )
    args = parser.parse_args()
    from backend.database import SessionLocal, ensure_schema

    ensure_schema()
    db = SessionLocal()
    try:
        if args.command == "rebuild":
            counts = rebuild_index(db, drive_root=Path(args.dir))
            print(json.dumps(counts, indent=2), flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
