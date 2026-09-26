"""Pratham playbook: reusable moves from his real speech, retrieved per script.

extract   -> per style transcript, pull frameworks / analogies / stories / openers /
             closers / objection-handling moves as verified verbatim excerpts
reference -> at generation time, rank the persona's moves and Pratham-only passages
             against the script's topics and format them for the planner and writer
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from sqlalchemy.orm import Session

from backend.database import SessionLocal, ensure_schema
from backend.models import PrathamMove, StyleTranscript, StyleTranscriptPersona, TranscriptChunk
from backend.pipeline.llm import chat_json
from backend.pipeline.style_guide import SENTENCE_SPLIT_RE, _split_speaker, parse_webvtt, pratham_lines
from backend.transcript_search import (
    SOURCE_STYLE,
    EmbedFn,
    cosine_similarity,
    default_embed_texts,
    parse_embedding,
)
from backend.transcripts import chunk_sentences, normalize_module_ids, normalize_whitespace

MOVE_KINDS = ("framework", "analogy", "story", "opener", "closer", "objection_handling", "audience_hook")
CHUNK_WORDS = 900
CHUNK_OVERLAP = 120
MOVE_LIMIT = 8
PASSAGE_LIMIT = 4
PASSAGE_WORDS = 320
NEAR_DUPLICATE = 0.9
SPEAKER_PREFIX_RE = re.compile(r"^\s*pratham(?: mittal)?\s*:\s*", re.IGNORECASE | re.MULTILINE)
LOOSE_RE = re.compile(r"[^a-z0-9]+")

EXTRACT_SYSTEM = (
    "You mine a chunk of Pratham Mittal's live speech (founder, Masters' Union) for REUSABLE "
    "DELIVERY MOVES that a Masters' Union employee could adapt in their own pitch.\n"
    "Kinds:\n"
    "- framework: a named or nameable mental model (e.g. a two-way contrast, a staged model, a metaphor system).\n"
    "- analogy: a concrete comparison that makes an abstract point land.\n"
    "- story: a short anecdote with a point (his own life, a student, a company).\n"
    "- opener: how he grabs a room at the start of a point (question, provocation, image).\n"
    "- closer: how he lands a point or makes an ask.\n"
    "- objection_handling: how he answers doubt, pushback, or a hard question.\n"
    "- audience_hook: live-room engagement (a poll, a challenge, a bet, calling on someone).\n"
    "Rules:\n"
    "- excerpt MUST be copied verbatim and contiguous from the chunk (1-6 sentences). Do not fix grammar.\n"
    "- Skip garbled transcription, filler, crowd logistics, and moves with no transferable point.\n"
    "- personal=true when the excerpt is about Pratham's own life, memories, or decisions "
    "(an employee must then attribute it to him in third person).\n"
    "- use_when: one sentence on when an employee pitch would use this move and what it proves.\n"
    "- label: 2-6 word name for the move.\n"
    "- module_ids: optional Masters' Union module ids M01-M14 if clearly relevant, else [].\n"
    "Return at most 6 of the strongest moves; [] is fine.\n"
    'Return JSON only: {"moves":[{"kind":"framework","label":"...","excerpt":"...",'
    '"use_when":"...","personal":false,"module_ids":[]}]}'
)


def _loose(text: str) -> str:
    return LOOSE_RE.sub(" ", (text or "").lower()).strip()


def is_loose_verbatim(excerpt: str, source: str) -> bool:
    needle = _loose(excerpt)
    return len(needle.split()) >= 6 and needle in _loose(source)


def move_hash(transcript_id: int, excerpt: str) -> str:
    return hashlib.sha256(f"{transcript_id}|{_loose(excerpt)}".encode("utf-8")).hexdigest()


def parse_moves_payload(raw: Any, chunk_text: str) -> list[dict[str, Any]]:
    items = raw.get("moves") if isinstance(raw, dict) else raw
    moves: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower().replace(" ", "_").replace("-", "_")
        excerpt = normalize_whitespace(str(item.get("excerpt") or ""))
        if kind not in MOVE_KINDS or not is_loose_verbatim(excerpt, chunk_text):
            continue
        personal = item.get("personal")
        moves.append(
            {
                "kind": kind,
                "label": normalize_whitespace(str(item.get("label") or ""))[:200],
                "excerpt": excerpt,
                "use_when": normalize_whitespace(str(item.get("use_when") or ""))[:600],
                "personal": personal is True or str(personal).lower() in {"true", "1", "yes"},
                "module_ids": normalize_module_ids(item.get("module_ids")),
            }
        )
    return moves


def pratham_text(raw_text: str) -> str:
    lines = parse_webvtt(raw_text or "")
    return pratham_lines(lines)


def _chunks(text: str) -> list[str]:
    sentences = [
        {"text": sentence, "start": 0.0, "end": 0.0}
        for line in text.splitlines()
        for sentence in SENTENCE_SPLIT_RE.split(line)
        if sentence.strip()
    ]
    return [chunk["text"] for chunk in chunk_sentences(sentences, target_words=CHUNK_WORDS, overlap=CHUNK_OVERLAP)]


def _default_extract(chunk_text: str) -> Any:
    return chat_json(
        [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": f"Speech chunk:\n{chunk_text}"},
        ],
        timeout=180.0,
    )


def extract_moves(
    db: Session,
    transcript_id: int,
    *,
    extract: Callable[[str], Any] | None = None,
    embed_fn: EmbedFn | None = None,
    workers: int = 4,
) -> dict[str, int]:
    """Replace the playbook moves for one style transcript."""
    transcript = db.get(StyleTranscript, transcript_id)
    if transcript is None:
        raise ValueError("Style transcript not found")
    text = pratham_text(transcript.raw_text or "")
    chunks = _chunks(text) if text.strip() else []
    extract_fn = extract or _default_extract

    def run(chunk_text: str) -> list[dict[str, Any]]:
        try:
            return parse_moves_payload(extract_fn(chunk_text), chunk_text)
        except Exception:  # noqa: BLE001
            return []

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        per_chunk = list(pool.map(run, chunks))

    kept: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    seen_labels: set[tuple[str, str]] = set()
    for moves in per_chunk:
        for move in moves:
            digest = move_hash(transcript_id, move["excerpt"])
            label_key = (move["kind"], move["label"].casefold())
            # Overlapping chunks re-surface the same move with slightly different bounds.
            if digest in seen_hashes or (move["label"] and label_key in seen_labels):
                continue
            seen_hashes.add(digest)
            seen_labels.add(label_key)
            kept.append({**move, "text_hash": digest})

    embed = embed_fn or default_embed_texts
    vectors = embed([f"{m['label']}. {m['use_when']} {m['excerpt']}" for m in kept]) if kept else []

    db.query(PrathamMove).filter(PrathamMove.style_transcript_id == transcript_id).delete()
    for move, vector in zip(kept, vectors):
        db.add(
            PrathamMove(
                style_transcript_id=transcript_id,
                kind=move["kind"],
                label=move["label"],
                excerpt=move["excerpt"],
                use_when=move["use_when"],
                personal=move["personal"],
                module_ids=move["module_ids"],
                source_name=transcript.name or "",
                embedding_json=json.dumps(vector),
                text_hash=move["text_hash"],
            )
        )
    db.commit()
    try:
        from backend.generation_cache import bump_content_version

        bump_content_version()
    except Exception:  # noqa: BLE001
        pass
    kinds = Counter(move["kind"] for move in kept)
    return {"chunks": len(chunks), "moves": len(kept), **kinds}


def persona_transcript_ids(db: Session, persona_label: str) -> list[int]:
    label = (persona_label or "").strip()
    if not label:
        return []
    rows = (
        db.query(StyleTranscriptPersona.style_transcript_id)
        .join(StyleTranscript, StyleTranscript.id == StyleTranscriptPersona.style_transcript_id)
        .filter(StyleTranscriptPersona.persona_label == label, StyleTranscript.status == "processed")
        .all()
    )
    return sorted({int(row[0]) for row in rows if row[0]})


def _is_pratham_only(raw_text: str) -> bool:
    speakers = set()
    for line in parse_webvtt(raw_text or ""):
        speaker, _utterance = _split_speaker(line)
        if speaker:
            speakers.add(speaker.lower())
    return bool(speakers) and all("pratham" in speaker for speaker in speakers)


def _trim_words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit]) + " …"


def _rank(items: list[dict[str, Any]], query_vectors: list[list[float]]) -> list[dict[str, Any]]:
    for item in items:
        item["score"] = max((cosine_similarity(q, item["vector"]) for q in query_vectors), default=0.0)
    return sorted(items, key=lambda item: item["score"], reverse=True)


def select_reference(
    db: Session,
    *,
    persona_label: str,
    topics: list[str],
    context_note: str = "",
    embed_fn: EmbedFn | None = None,
    move_limit: int = MOVE_LIMIT,
    passage_limit: int = PASSAGE_LIMIT,
) -> dict[str, list[dict[str, Any]]]:
    tagged_ids = set(persona_transcript_ids(db, persona_label))
    processed = db.query(StyleTranscript).filter(StyleTranscript.status == "processed").all()
    pratham_only_ids = {row.id for row in processed if _is_pratham_only(row.raw_text or "")}
    # Persona tags boost ranking; they never gate. Untagged personas still get
    # every processed Pratham-only transcript.
    candidate_ids = set(tagged_ids) | set(pratham_only_ids) if tagged_ids else set(pratham_only_ids)
    if not candidate_ids:
        return {"moves": [], "passages": []}

    moves = [
        {
            "row": row,
            "vector": parse_embedding(row.embedding_json),
            "tagged": row.style_transcript_id in tagged_ids,
        }
        for row in db.query(PrathamMove)
        .filter(PrathamMove.style_transcript_id.in_(candidate_ids), PrathamMove.status == "approved")
        .all()
    ]
    moves = [item for item in moves if item["vector"]]

    passage_source_ids = {str(tid) for tid in (candidate_ids & pratham_only_ids)}
    passages = [
        {
            "row": row,
            "vector": parse_embedding(row.embedding_json),
            "tagged": int(row.source_id) in tagged_ids if str(row.source_id).isdigit() else False,
        }
        for row in db.query(TranscriptChunk)
        .filter(TranscriptChunk.source_type == SOURCE_STYLE, TranscriptChunk.source_id.in_(passage_source_ids))
        .all()
    ] if passage_source_ids else []
    passages = [item for item in passages if item["vector"]]
    if not moves and not passages:
        return {"moves": [], "passages": []}

    base = f"{persona_label}. {context_note}".strip()
    queries = [base] + [f"{persona_label}: {topic}" for topic in topics[:10] if topic]
    embed = embed_fn or default_embed_texts
    query_vectors = embed(queries)

    for item in moves:
        item["score"] = max((cosine_similarity(q, item["vector"]) for q in query_vectors), default=0.0)
        if item["tagged"]:
            item["score"] += 0.1
    moves = sorted(moves, key=lambda item: item["score"], reverse=True)

    picked_moves: list[dict[str, Any]] = []
    picked_vectors: list[list[float]] = []
    per_kind: Counter[str] = Counter()
    for item in moves:
        row = item["row"]
        if per_kind[row.kind] >= 2:
            continue
        # The same move often recurs across sessions of one talk; keep one copy.
        if any(cosine_similarity(item["vector"], seen) >= NEAR_DUPLICATE for seen in picked_vectors):
            continue
        per_kind[row.kind] += 1
        picked_vectors.append(item["vector"])
        picked_moves.append({"row": row, "score": item["score"]})
        if len(picked_moves) >= move_limit:
            break

    for item in passages:
        item["score"] = max((cosine_similarity(q, item["vector"]) for q in query_vectors), default=0.0)
        if item["tagged"]:
            item["score"] += 0.1
    passages = sorted(passages, key=lambda item: item["score"], reverse=True)

    picked_passages: list[dict[str, Any]] = []
    per_source: Counter[str] = Counter()
    for item in passages:
        if passage_limit <= 0:
            break
        row = item["row"]
        if per_source[row.source_id] >= 2:
            continue
        per_source[row.source_id] += 1
        picked_passages.append({"row": row, "score": item["score"]})
        if len(picked_passages) >= passage_limit:
            break
    return {"moves": picked_moves, "passages": picked_passages}


def format_reference(selection: dict[str, list[dict[str, Any]]]) -> str:
    blocks: list[str] = []
    moves = selection.get("moves") or []
    if moves:
        lines = []
        for item in moves:
            row = item["row"]
            tag = "personal — attribute to Pratham in third person" if row.personal else "transferable"
            lines.append(
                f'- [{row.kind}] {row.label} ({tag}). Use when: {row.use_when}\n'
                f'  "{row.excerpt}" — {row.source_name}'
            )
        blocks.append("PRATHAM PLAYBOOK — reusable moves from his real sessions:\n" + "\n".join(lines))
    passages = selection.get("passages") or []
    if passages:
        lines = []
        for index, item in enumerate(passages, start=1):
            row = item["row"]
            body = _trim_words(normalize_whitespace(SPEAKER_PREFIX_RE.sub("", row.text or "")), PASSAGE_WORDS)
            lines.append(f"[{index}] {row.source_name}:\n{body}")
        blocks.append(
            "PRATHAM PASSAGES — longer real passages matched to this audience and topics:\n" + "\n\n".join(lines)
        )
    return "\n\n".join(blocks)


def build_pratham_reference(
    db: Session,
    *,
    persona_label: str,
    topics: list[str],
    context_note: str = "",
    embed_fn: EmbedFn | None = None,
    move_limit: int = MOVE_LIMIT,
    passage_limit: int = PASSAGE_LIMIT,
) -> str:
    return format_reference(
        select_reference(
            db,
            persona_label=persona_label,
            topics=topics,
            context_note=context_note,
            embed_fn=embed_fn,
            move_limit=move_limit,
            passage_limit=passage_limit,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    extract = sub.add_parser("extract", help="extract moves for processed Pratham-only style transcripts")
    extract.add_argument("--transcript-id", type=int, default=0)
    extract.add_argument("--force", action="store_true", help="re-extract transcripts that already have moves")
    preview = sub.add_parser("preview", help="print the reference block for a persona")
    preview.add_argument("persona")
    preview.add_argument("--topic", action="append", default=[])
    args = parser.parse_args(argv)

    ensure_schema()
    db = SessionLocal()
    try:
        if args.command == "preview":
            print(build_pratham_reference(db, persona_label=args.persona, topics=args.topic) or "(nothing)")
            return 0
        query = db.query(StyleTranscript).filter(StyleTranscript.status == "processed")
        if args.transcript_id:
            query = query.filter(StyleTranscript.id == args.transcript_id)
        for row in query.order_by(StyleTranscript.id.asc()).all():
            if not args.transcript_id and not _is_pratham_only(row.raw_text or ""):
                continue
            has_moves = db.query(PrathamMove).filter(PrathamMove.style_transcript_id == row.id).first()
            if has_moves and not args.force:
                print(f"ST {row.id}: already has moves", flush=True)
                continue
            try:
                result = extract_moves(db, row.id)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"ST {row.id}: FAILED {exc}", flush=True)
                continue
            print(f"ST {row.id} {row.name}: {result}", flush=True)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
