from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.config import REPO_ROOT
from backend.database import SessionLocal, engine, ensure_schema
from backend.models import Base, FounderQuote
from backend.pipeline.llm import chat_json

ALLOWED_TOPICS = {"institution", "vision", "students", "challenges", "founder"}
VALID_MODULES = {f"M{index:02d}" for index in range(1, 15)}
CHUNK_WORDS = 180
CHUNK_OVERLAP = 40
WHITESPACE_RE = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    return WHITESPACE_RE.sub(" ", (text or "").strip())


def quote_hash(file_id: str, text: str) -> str:
    payload = f"{file_id}|{normalize_whitespace(text)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_module_ids(raw: Any) -> str:
    values: list[str] = []
    if isinstance(raw, str):
        parts = re.split(r"[,\s]+", raw)
    elif isinstance(raw, list):
        parts = [str(item) for item in raw]
    else:
        parts = []
    for part in parts:
        token = part.strip().upper()
        if not token:
            continue
        if re.fullmatch(r"M\d{1,2}", token):
            token = f"M{int(token[1:]):02d}"
        if token in VALID_MODULES and token not in values:
            values.append(token)
    return ",".join(values)


def is_verbatim(excerpt: str, source: str) -> bool:
    needle = normalize_whitespace(excerpt)
    haystack = normalize_whitespace(source)
    return bool(needle) and needle in haystack


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def chunk_sentences(
    sentences: list[dict[str, Any]],
    target_words: int = CHUNK_WORDS,
    overlap: int = CHUNK_OVERLAP,
) -> list[dict[str, Any]]:
    packed: list[tuple[list[str], float, float]] = []
    words: list[str] = []
    start = 0.0
    end = 0.0
    for sentence in sentences:
        text = normalize_whitespace(str(sentence.get("text") or ""))
        if not text:
            continue
        pieces = text.split()
        if not words:
            start = float(sentence.get("start") or 0.0)
        words.extend(pieces)
        end = float(sentence.get("end") or end)
        if len(words) >= target_words:
            packed.append((words[:], start, end))
            keep = words[-overlap:] if overlap else []
            words = keep[:]
            start = end
    if words:
        packed.append((words, start, end))
    chunks = []
    for words, start, end in packed:
        text = " ".join(words)
        if text:
            chunks.append({"text": text, "start": start, "end": end})
    return chunks


def parse_curation_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("snippets") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    snippets: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        keep = item.get("keep", True)
        if keep is False or str(keep).lower() in {"false", "0", "no"}:
            continue
        text = normalize_whitespace(str(item.get("text") or ""))
        topic = str(item.get("topic") or "").strip().lower()
        if not text or topic not in ALLOWED_TOPICS:
            continue
        snippets.append(
            {
                "text": text,
                "topic": topic,
                "module_ids": normalize_module_ids(item.get("module_ids")),
            }
        )
    return snippets


def discover_transcripts(root: Path) -> list[Path]:
    return sorted(path for path in root.glob("*/*.json") if path.name != "manifest.json")


def load_transcript(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    sentences = payload.get("sentences") or []
    return {
        "file_id": str(payload.get("file_id") or path.stem),
        "source_name": str(payload.get("source_name") or path.stem),
        "source_url": str(payload.get("source_url") or ""),
        "sentences": sentences,
        "text": normalize_whitespace(str(payload.get("text") or "")),
    }


CURATION_SYSTEM = (
    "You extract Masters' Union founder-voice snippets from a transcript chunk. "
    "Return JSON only.\n"
    "Keep only content about Masters' Union (the institution, campus, programs), "
    "its vision, students/outcomes, or what Pratham Mittal says about MU.\n"
    "Drop science tangents (CRISPR, aging biology), unrelated personal anecdotes, "
    "and crowd-work with no institutional content.\n"
    "Return text as a contiguous excerpt from the chunk.\n"
    "module_ids: only modules the quote is clearly about; [] otherwise.\n"
    'Output: {"snippets":[{"text":"...","topic":"institution|vision|students|challenges|founder",'
    '"module_ids":[],"keep":true}]}'
)


def curate_chunk(chunk_text: str) -> list[dict[str, Any]]:
    payload = chat_json(
        [
            {"role": "system", "content": CURATION_SYSTEM},
            {"role": "user", "content": f"Transcript chunk:\n{chunk_text}"},
        ]
    )
    return parse_curation_payload(payload)


def is_pratham_quote(quote: FounderQuote) -> bool:
    return "pratham" in (getattr(quote, "speaker", "") or "").lower()


def quote_matches_persona(
    quote: FounderQuote,
    *,
    persona_label: str = "",
    allowed_transcript_ids: set[int] | None = None,
) -> bool:
    """Style-harvested quotes must match the persona; legacy quotes stay global."""
    source_id = int(getattr(quote, "source_style_transcript_id", 0) or 0)
    if source_id <= 0:
        source_file_id = (getattr(quote, "source_file_id", "") or "").strip()
        if source_file_id.startswith("style-"):
            token = source_file_id[6:]
            if token.isdigit():
                source_id = int(token)
    if source_id <= 0:
        return True
    label = (persona_label or "").strip()
    if not label:
        return False
    if allowed_transcript_ids is None:
        return False
    return source_id in allowed_transcript_ids


def pick_founder_quotes(
    quotes: list[FounderQuote],
    sequence: list[str],
    limit: int = 6,
    *,
    persona_label: str = "",
    allowed_transcript_ids: set[int] | None = None,
) -> list[FounderQuote]:
    sequence_set = set(sequence)
    buckets: dict[str, list[FounderQuote]] = {}
    leftovers: list[FounderQuote] = []
    usable = [
        quote
        for quote in quotes
        if quote.status == "approved"
        and is_pratham_quote(quote)
        and quote_matches_persona(
            quote,
            persona_label=persona_label,
            allowed_transcript_ids=allowed_transcript_ids,
        )
    ]
    usable.sort(key=lambda quote: (0 if getattr(quote, "verbatim", False) else 1, quote.id if hasattr(quote, "id") else 0))
    for quote in usable:
        ids = [part.strip() for part in (quote.module_ids or "").split(",") if part.strip()]
        matches = [mid for mid in ids if mid in sequence_set]
        if matches:
            buckets.setdefault(quote.topic or "founder", []).append(quote)
        else:
            leftovers.append(quote)
    selected: list[FounderQuote] = []
    while len(selected) < limit and any(buckets.values()):
        for topic in sorted(buckets):
            if buckets[topic]:
                selected.append(buckets[topic].pop(0))
                if len(selected) >= limit:
                    break
    if len(selected) < limit:
        selected.extend(leftovers[: limit - len(selected)])
    return selected


def format_founder_line(quote: FounderQuote) -> str:
    stamp = format_timestamp(quote.start_sec)
    source = quote.source_name or quote.source_file_id or "transcript"
    modules = quote.module_ids or "—"
    return (
        f'- [{quote.topic}|{modules}] "{quote.text}" '
        f"— {quote.speaker}, {source} @ {stamp}"
    )


def ingest_transcripts(
    root: Path | None = None,
    force: bool = False,
    limit: int | None = None,
    db: Session | None = None,
    curate=curate_chunk,
) -> dict[str, int]:
    close = False
    if db is None:
        Base.metadata.create_all(bind=engine)
        ensure_schema()
        db = SessionLocal()
        close = True
    directory = Path(root or (REPO_ROOT / "output" / "transcripts"))
    counts: Counter[str] = Counter()
    processed = 0
    try:
        for path in discover_transcripts(directory):
            if limit is not None and processed >= limit:
                break
            transcript = load_transcript(path)
            chunks = chunk_sentences(transcript["sentences"])
            counts["files"] += 1
            counts["chunks"] += len(chunks)
            processed += 1
            print(f"Curating {path.stem} ({len(chunks)} chunks)", flush=True)
            for chunk in chunks:
                try:
                    snippets = curate(chunk["text"])
                except Exception as exc:  # noqa: BLE001
                    print(f"  curation failed: {exc}", flush=True)
                    counts["dropped"] += 1
                    continue
                if not snippets:
                    counts["dropped"] += 1
                    continue
                for snippet in snippets:
                    digest = quote_hash(transcript["file_id"], snippet["text"])
                    existing = db.query(FounderQuote).filter(FounderQuote.text_hash == digest).first()
                    if existing and not force:
                        counts["skipped"] += 1
                        continue
                    verbatim = is_verbatim(snippet["text"], transcript["text"] or chunk["text"])
                    if existing:
                        existing.text = snippet["text"]
                        existing.verbatim = verbatim
                        existing.topic = snippet["topic"]
                        existing.module_ids = snippet["module_ids"]
                        existing.source_name = transcript["source_name"]
                        existing.source_file_id = transcript["file_id"]
                        existing.source_url = transcript["source_url"]
                        existing.start_sec = float(chunk["start"])
                        existing.end_sec = float(chunk["end"])
                    else:
                        db.add(
                            FounderQuote(
                                text=snippet["text"],
                                verbatim=verbatim,
                                topic=snippet["topic"],
                                module_ids=snippet["module_ids"],
                                source_name=transcript["source_name"],
                                source_file_id=transcript["file_id"],
                                source_url=transcript["source_url"],
                                start_sec=float(chunk["start"]),
                                end_sec=float(chunk["end"]),
                                speaker="Pratham Mittal",
                                text_hash=digest,
                                status="approved",
                            )
                        )
                    counts["kept"] += 1
            db.commit()
        # Refresh semantic index for Drive bundles + newly curated quotes.
        try:
            from backend.transcript_search import rebuild_index

            rebuild_index(db, drive_root=directory)
        except Exception as exc:  # noqa: BLE001
            print(f"  transcript index rebuild skipped: {exc}", flush=True)
        return dict(counts)
    except Exception:
        db.rollback()
        raise
    finally:
        if close:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest MU-relevant founder quotes from transcripts")
    parser.add_argument("--dir", default=str(REPO_ROOT / "output" / "transcripts"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    counts = ingest_transcripts(
        root=Path(args.dir),
        force=args.force,
        limit=args.limit or None,
    )
    print(
        "Ingest complete:",
        ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "nothing",
    )


if __name__ == "__main__":
    main()
