"""Semantic index over ingested transcripts for Library → Ask the library.

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
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx
from sqlalchemy.orm import Session

from backend.config import REPO_ROOT, settings
from backend.models import AskAnswerCache, Asset, FounderQuote, MediaIndex, StyleTranscript, TranscriptChunk

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

# Process-local vector cache for retrieve(); invalidated when the index changes.
_VECTOR_CACHE_LOCK = threading.Lock()
_VECTOR_CACHE_FINGERPRINT = ""
_VECTOR_CACHE_ROWS: list[dict[str, Any]] = []
# Small process-local cache of question embeddings (normalized question → vector).
_QUERY_EMBED_LOCK = threading.Lock()
_QUERY_EMBED_CACHE: dict[str, list[float]] = {}
_QUERY_EMBED_CACHE_MAX = 256

ASK_SYSTEM = (
    "You answer questions using ONLY the provided transcript passages from "
    "Masters' Union conversations and materials. Keep the answer to **200 words "
    "or fewer**. Split the answer into short segments (one claim or sentence each). "
    "For every segment, cite the passage index it comes from and copy a short "
    "verbatim quote from that passage that supports it. Wrap key figures and "
    "takeaways in **bold**. If the passages do not support an answer, return one "
    "segment saying so with an empty quote and no source_indexes. "
    "Passages are numbered from 0. Return strict JSON: "
    '{"segments":[{"text":"...","source_indexes":[0],"quote":"verbatim from passage"}],'
    '"highlights":["..."]}.'
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

    def pack_words(words: list[str]) -> list[str]:
        if not words:
            return []
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

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if len(lines) >= 2:
        packed: list[str] = []
        buffer: list[str] = []
        buf_words = 0
        for line in lines:
            line_words = _word_list(line)
            # A single oversized line must be word-split, not stored whole.
            if len(line_words) > chunk_words:
                if buffer:
                    packed.append("\n".join(buffer))
                    buffer = []
                    buf_words = 0
                packed.extend(pack_words(line_words))
                continue
            if buffer and buf_words + len(line_words) > chunk_words:
                packed.append("\n".join(buffer))
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
            buf_words += len(line_words)
        if buffer:
            packed.append("\n".join(buffer))
        return packed

    return pack_words(_word_list(normalized))


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


def normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", (question or "").strip().lower())


def question_cache_key(question: str) -> str:
    return text_hash(normalize_question(question))


def index_fingerprint(db: Session) -> str:
    """Cheap fingerprint of the semantic index for cache invalidation."""
    try:
        rows = db.query(TranscriptChunk).all()
    except Exception:
        return "empty"
    if not rows:
        return "empty"
    digest = hashlib.sha256()
    digest.update(f"{len(rows)}".encode())
    normalized: list[tuple[int, str]] = []
    for row in rows:
        row_id = int(getattr(row, "id", 0) or 0)
        row_hash = str(getattr(row, "text_hash", "") or "")
        if not row_hash:
            row_hash = text_hash(str(getattr(row, "text", "") or ""))
        normalized.append((row_id, row_hash))
    for row_id, row_hash in sorted(normalized):
        digest.update(f"{row_id}:{row_hash}|".encode())
    return digest.hexdigest()[:32]


def clear_ask_caches(db: Session | None = None) -> None:
    """Drop process-local caches and, when possible, persisted answer rows."""
    global _VECTOR_CACHE_FINGERPRINT, _VECTOR_CACHE_ROWS
    with _VECTOR_CACHE_LOCK:
        _VECTOR_CACHE_FINGERPRINT = ""
        _VECTOR_CACHE_ROWS = []
    with _QUERY_EMBED_LOCK:
        _QUERY_EMBED_CACHE.clear()
    if db is None:
        return
    try:
        db.query(AskAnswerCache).delete(synchronize_session=False)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def get_cached_answer(db: Session, question: str, fingerprint: str) -> dict[str, Any] | None:
    key = question_cache_key(question)
    if not key or fingerprint == "empty":
        return None
    try:
        row = (
            db.query(AskAnswerCache)
            .filter(
                AskAnswerCache.question_hash == key,
                AskAnswerCache.index_fingerprint == fingerprint,
            )
            .first()
        )
    except Exception:
        return None
    if row is None or not (row.response_json or "").strip():
        return None
    try:
        payload = json.loads(row.response_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def store_cached_answer(
    db: Session,
    question: str,
    fingerprint: str,
    response: dict[str, Any],
) -> None:
    key = question_cache_key(question)
    if not key or fingerprint == "empty":
        return
    try:
        existing = (
            db.query(AskAnswerCache)
            .filter(
                AskAnswerCache.question_hash == key,
                AskAnswerCache.index_fingerprint == fingerprint,
            )
            .first()
        )
        blob = json.dumps(response, ensure_ascii=False)
        if existing is None:
            db.add(
                AskAnswerCache(
                    question_hash=key,
                    question=normalize_question(question)[:2000],
                    index_fingerprint=fingerprint,
                    response_json=blob,
                )
            )
        else:
            existing.question = normalize_question(question)[:2000]
            existing.response_json = blob
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _cached_query_embedding(question: str, embed: EmbedFn) -> list[float]:
    key = normalize_question(question)
    with _QUERY_EMBED_LOCK:
        cached = _QUERY_EMBED_CACHE.get(key)
    if cached is not None:
        return cached
    vector = embed([question])[0]
    with _QUERY_EMBED_LOCK:
        if len(_QUERY_EMBED_CACHE) >= _QUERY_EMBED_CACHE_MAX:
            # Drop an arbitrary oldest entry; insertion order is fine here.
            _QUERY_EMBED_CACHE.pop(next(iter(_QUERY_EMBED_CACHE)), None)
        _QUERY_EMBED_CACHE[key] = vector
    return vector


def _load_vector_rows(db: Session, fingerprint: str) -> list[dict[str, Any]]:
    global _VECTOR_CACHE_FINGERPRINT, _VECTOR_CACHE_ROWS
    with _VECTOR_CACHE_LOCK:
        if _VECTOR_CACHE_FINGERPRINT == fingerprint and _VECTOR_CACHE_ROWS:
            return list(_VECTOR_CACHE_ROWS)
    rows = db.query(TranscriptChunk).all()
    loaded: list[dict[str, Any]] = []
    for row in rows:
        vector = parse_embedding(row.embedding_json)
        if not vector:
            continue
        loaded.append(
            {
                "id": row.id,
                "source_type": row.source_type,
                "source_id": row.source_id,
                "source_name": row.source_name,
                "text": row.text,
                "start_ms": row.start_ms,
                "end_ms": row.end_ms,
                "vector": vector,
            }
        )
    with _VECTOR_CACHE_LOCK:
        _VECTOR_CACHE_FINGERPRINT = fingerprint
        _VECTOR_CACHE_ROWS = loaded
        return list(loaded)


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
    # text-embedding-3-small caps around 8192 tokens; keep a safe word budget.
    safe_texts: list[str] = []
    for text in texts:
        words = _word_list(text)
        if len(words) > 1200:
            safe_texts.append(" ".join(words[:1200]))
        else:
            safe_texts.append(text)
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:5173",
        "X-Title": "Pitch Studio",
    }
    out: list[list[float]] = []
    with httpx.Client(timeout=120.0) as client:
        for start in range(0, len(safe_texts), EMBED_BATCH):
            batch = safe_texts[start : start + EMBED_BATCH]
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
        clear_ask_caches(db)
    else:
        clear_ask_caches(None)
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
        clear_ask_caches(db)
        try:
            from backend.generation_cache import bump_content_version

            bump_content_version()
        except Exception:
            logger.exception("Content version bump after upsert_source delete failed")
        return 0
    written = upsert_prepared(db, chunks, embed_fn=embed_fn, commit=True)
    try:
        from backend.generation_cache import bump_content_version

        bump_content_version()
    except Exception:
        logger.exception("Content version bump after upsert_source failed")
    return written


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
    clear_ask_caches(db)
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
    try:
        from backend.generation_cache import bump_content_version

        bump_content_version()
    except Exception:
        logger.exception("Content version bump after rebuild_index failed")
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
    fingerprint = index_fingerprint(db)
    rows = _load_vector_rows(db, fingerprint)
    if not rows:
        return []
    embed = embed_fn or default_embed_texts
    # Only reuse cached question embeddings for the default embedder.
    if embed_fn is None:
        query_vector = _cached_query_embedding(query, embed)
    else:
        query_vector = embed([query])[0]
    scored: list[dict[str, Any]] = []
    for row in rows:
        score = cosine_similarity(query_vector, row["vector"])
        scored.append(
            {
                "id": row["id"],
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "source_name": row["source_name"],
                "text": row["text"],
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
                "score": round(float(score), 6),
            }
        )
    scored.sort(key=lambda item: item["score"], reverse=True)
    return _dedupe_hits(scored)[: max(1, top_k)]


def source_lines(text: str) -> list[str]:
    formatted = format_source_sentences(text)
    return [line.strip() for line in formatted.splitlines() if line.strip()]


def match_quote_line_indexes(source_text: str, quote: str) -> list[int]:
    """Find display-line indexes in ``source_text`` that support ``quote``."""
    needle = re.sub(r"\s+", " ", (quote or "").strip().lower())
    if len(needle) < 8:
        return []
    lines = source_lines(source_text)
    if not lines:
        return []
    needle_words = {word for word in _word_list(needle) if len(word) > 2}
    hits: list[int] = []
    for index, line in enumerate(lines):
        hay = re.sub(r"\s+", " ", line.lower())
        if needle in hay or hay in needle:
            hits.append(index)
            continue
        if len(needle) >= 24:
            # Partial containment for longer quotes spanning a line.
            for size in (48, 32, 24):
                if len(needle) < size:
                    continue
                fragment = needle[:size]
                if fragment in hay:
                    hits.append(index)
                    break
            else:
                line_words = {word for word in _word_list(hay) if len(word) > 2}
                if needle_words and line_words:
                    overlap = len(needle_words & line_words) / max(1, len(needle_words))
                    if overlap >= 0.55:
                        hits.append(index)
        elif needle_words:
            line_words = {word for word in _word_list(hay) if len(word) > 2}
            if line_words:
                overlap = len(needle_words & line_words) / max(1, len(needle_words))
                if overlap >= 0.7:
                    hits.append(index)
    return hits


def _default_answer_fn(question: str, passages: list[dict[str, Any]]) -> dict[str, Any]:
    from backend.pipeline.llm import chat_json

    packed = [
        {
            "index": index,
            "source_name": item.get("source_name"),
            "source_type": item.get("source_type"),
            "text": item.get("text"),
        }
        for index, item in enumerate(passages)
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
        max_tokens=1200,
    )
    raw_segments = payload.get("segments")
    segments: list[dict[str, Any]] = []
    if isinstance(raw_segments, list):
        for row in raw_segments:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            indexes_raw = row.get("source_indexes") or row.get("source_index") or []
            if isinstance(indexes_raw, int):
                indexes = [indexes_raw]
            elif isinstance(indexes_raw, list):
                indexes = []
                for value in indexes_raw:
                    try:
                        indexes.append(int(value))
                    except (TypeError, ValueError):
                        continue
            else:
                indexes = []
            indexes = [value for value in indexes if 0 <= value < len(passages)]
            quote = str(row.get("quote") or "").strip()
            segments.append(
                {
                    "text": text,
                    "source_indexes": indexes,
                    "quote": quote,
                }
            )

    if not segments:
        # Legacy fallback if the model ignored the segment schema.
        legacy = str(payload.get("answer_markdown") or "").strip()
        if legacy:
            segments = [{"text": legacy, "source_indexes": [0] if passages else [], "quote": ""}]

    joined = " ".join(str(item["text"]) for item in segments)
    joined = trim_to_word_limit(joined, MAX_ANSWER_WORDS)
    # Re-trim segment list if the join exceeded the cap.
    kept: list[dict[str, Any]] = []
    used = 0
    for item in segments:
        words = _word_list(str(item["text"]))
        if used >= MAX_ANSWER_WORDS:
            break
        if used + len(words) > MAX_ANSWER_WORDS:
            clipped = trim_to_word_limit(str(item["text"]), MAX_ANSWER_WORDS - used)
            if clipped:
                kept.append({**item, "text": clipped})
            break
        kept.append(item)
        used += len(words)
    segments = kept

    highlights = payload.get("highlights") or []
    if not isinstance(highlights, list):
        highlights = []
    highlights = [str(item).strip() for item in highlights if str(item).strip()]
    if not highlights:
        highlights = re.findall(r"\*\*(.+?)\*\*", " ".join(item["text"] for item in segments))
    return {
        "answer_markdown": "\n\n".join(item["text"] for item in segments),
        "segments": segments,
        "highlights": highlights,
    }


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
            "answer_markdown": "Ask a question about the library.",
            "segments": [],
            "highlights": [],
            "sources": [],
        }
    # Only cache the production path (default embedder + answerer).
    use_cache = embed_fn is None and answer_fn is None
    fingerprint = index_fingerprint(db) if use_cache else ""
    if use_cache:
        cached = get_cached_answer(db, query, fingerprint)
        if cached is not None:
            return cached

    sources = retrieve(db, query, top_k=top_k, embed_fn=embed_fn)
    if not sources:
        return {
            "answer_markdown": (
                "The library index is empty, or nothing relevant was found. "
                "An admin needs to rebuild the index after uploading source material."
            ),
            "segments": [],
            "highlights": [],
            "sources": [],
        }
    answerer = answer_fn or _default_answer_fn
    # Present cleaned sentence text to the model so quotes match UI lines.
    model_passages = [
        {
            **item,
            "text": format_source_sentences(str(item.get("text") or "")),
        }
        for item in sources
    ]
    result = answerer(query, model_passages)
    segments = list(result.get("segments") or [])
    answer = trim_to_word_limit(str(result.get("answer_markdown") or "").strip(), MAX_ANSWER_WORDS)

    enriched_sources: list[dict[str, Any]] = []
    for index, item in enumerate(sources):
        display = format_source_sentences(str(item.get("text") or ""))
        highlight_lines: set[int] = set()
        for segment in segments:
            indexes = segment.get("source_indexes") or []
            if index not in indexes:
                continue
            quote = str(segment.get("quote") or "").strip()
            if quote:
                highlight_lines.update(match_quote_line_indexes(display, quote))
        enriched_sources.append(
            {
                "source_type": item["source_type"],
                "source_name": item["source_name"],
                "text": display,
                "score": item["score"],
                "start_ms": item.get("start_ms"),
                "end_ms": item.get("end_ms"),
                "highlight_lines": sorted(highlight_lines),
            }
        )

    # Attach resolved line indexes onto each segment for the UI jump target.
    resolved_segments: list[dict[str, Any]] = []
    for segment in segments:
        targets: list[dict[str, Any]] = []
        indexes = segment.get("source_indexes") or []
        quote = str(segment.get("quote") or "").strip()
        for source_index in indexes:
            if not (0 <= int(source_index) < len(enriched_sources)):
                continue
            source = enriched_sources[int(source_index)]
            line_indexes = (
                match_quote_line_indexes(str(source["text"]), quote)
                if quote
                else list(source.get("highlight_lines") or [])
            )
            if not line_indexes and source.get("highlight_lines"):
                line_indexes = list(source["highlight_lines"])[:1]
            if not line_indexes:
                line_indexes = [0] if source_lines(str(source["text"])) else []
            targets.append(
                {
                    "source_index": int(source_index),
                    "line_indexes": line_indexes,
                }
            )
        resolved_segments.append(
            {
                "text": segment.get("text") or "",
                "source_indexes": list(indexes),
                "quote": quote,
                "targets": targets,
            }
        )

    payload = {
        "answer_markdown": answer,
        "segments": resolved_segments,
        "highlights": list(result.get("highlights") or []),
        "sources": enriched_sources,
    }
    if use_cache:
        store_cached_answer(db, query, fingerprint, payload)
    return payload


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
