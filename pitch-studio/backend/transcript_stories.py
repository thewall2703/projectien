"""Transcript stories: concrete facts and real-world stories from non-Pratham speech.

index   -> extract stories/facts from other speakers in style transcripts, tag, embed
select  -> for each script topic, pick stories/facts by module match + cosine
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

from backend.config import settings
from backend.database import SessionLocal, ensure_schema
from backend.models import StyleTranscript, TranscriptStory
from backend.pipeline.llm import chat_json
from backend.pipeline.style_guide import is_pratham_mittal_speaker, parse_webvtt_cues
from backend.pratham_passages import (
    COSINE_WEIGHT,
    FALLBACK_COSINE,
    NEAR_DUP_COSINE,
    STRENGTH_PASSING,
    STRENGTH_STRONG,
    _catalog_block,
    _module_match_score,
    _topic_modules,
    _topic_payload,
    parse_tag_payload,
)
from backend.pratham_playbook import is_loose_verbatim
from backend.transcript_search import (
    EmbedFn,
    cosine_similarity,
    default_embed_texts,
    parse_embedding,
)
from backend.transcripts import VALID_MODULES, format_timestamp, normalize_whitespace

CHUNK_TARGET_WORDS = 1500
CHUNK_OVERLAP_WORDS = 150
MIN_TEXT_WORDS = 30
MAX_TEXT_WORDS = 160
MAX_EXCERPT_WORDS = 600
EXTRACT_SYSTEM = (
    "You extract CONCRETE real-world stories and CONCRETE facts from a Masters' Union "
    "session transcript chunk (speakers other than founder Pratham Mittal).\n"
    "Stories: named student/alumni/founder/company/venture, what happened, outcome.\n"
    "Facts: programme structure, numbers, processes, partners, placements, ventures, "
    "fees/process details that could strengthen a Masters' Union pitch.\n"
    "Skip generic opinions, marketing fluff, logistics, Q&A housekeeping, "
    "audibility checks, and chat noise — mark those usable=false if you include them.\n"
    "Each item MUST have a verbatim `excerpt` copied from the chunk (do not invent).\n"
    "Tag up to 3 modules strong/passing from the MODULE CATALOG.\n"
    "text: self-contained third-person paraphrase, 30–160 words, plain English.\n"
    "Return JSON only:\n"
    '{"items":[{"kind":"story|fact","title":"...","text":"...","excerpt":"...",'
    '"speaker":"...","modules":[{"id":"M__","strength":"strong|passing"}],'
    '"entities":["..."],"figures":["..."],"usable":true}]}'
)


def _csv_join(values: list[str], limit: int = 500) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = normalize_whitespace(str(value or ""))
        if not token:
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(token)
    return ",".join(cleaned)[:limit]


def non_pratham_cues(raw_text: str) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    for cue in parse_webvtt_cues(raw_text or ""):
        speaker = cue.get("speaker")
        if speaker and is_pratham_mittal_speaker(str(speaker)):
            continue
        text = normalize_whitespace(str(cue.get("text") or ""))
        if not text:
            continue
        cues.append(
            {
                "start_sec": cue.get("start_sec"),
                "end_sec": cue.get("end_sec"),
                "speaker": (str(speaker).strip() if speaker else "")[:200],
                "text": text,
            }
        )
    return cues


def _labelled_text(cues: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    previous: str | None = None
    for cue in cues:
        speaker = cue.get("speaker") or ""
        if speaker and speaker != previous:
            lines.append(f"{speaker}: {cue['text']}")
        elif lines:
            lines[-1] = f"{lines[-1]} {cue['text']}"
        else:
            lines.append(cue["text"])
        previous = speaker or previous
    return "\n".join(lines)


def chunk_cues(
    cues: list[dict[str, Any]],
    *,
    target_words: int = CHUNK_TARGET_WORDS,
    overlap_words: int = CHUNK_OVERLAP_WORDS,
) -> list[dict[str, Any]]:
    if not cues:
        return []
    chunks: list[dict[str, Any]] = []
    start_index = 0
    while start_index < len(cues):
        words = 0
        end_index = start_index
        while end_index < len(cues) and (
            words < target_words or end_index == start_index
        ):
            words += len(str(cues[end_index]["text"]).split())
            end_index += 1
        window = cues[start_index:end_index]
        text = " ".join(cue["text"] for cue in window)
        chunks.append(
            {
                "text": text,
                "labelled_text": _labelled_text(window),
                "cues": window,
                "start_sec": window[0].get("start_sec"),
                "end_sec": window[-1].get("end_sec"),
            }
        )
        if end_index >= len(cues):
            break
        if overlap_words <= 0:
            start_index = end_index
            continue
        back_words = 0
        next_start = end_index
        while next_start > start_index + 1 and back_words < overlap_words:
            next_start -= 1
            back_words += len(str(cues[next_start]["text"]).split())
        start_index = next_start if next_start > start_index else end_index
    return chunks


def _story_hash(transcript_id: int, text: str, excerpt: str) -> str:
    payload = f"{transcript_id}|{normalize_whitespace(text)}|{normalize_whitespace(excerpt)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _excerpt_hash(excerpt: str) -> str:
    return hashlib.sha256(normalize_whitespace(excerpt).casefold().encode("utf-8")).hexdigest()


def map_excerpt_to_cues(
    excerpt: str,
    cues: list[dict[str, Any]],
) -> tuple[float | None, float | None, str]:
    """Best-effort start/end and dominant speaker for a verbatim excerpt."""
    if not cues or not excerpt.strip():
        return None, None, ""
    loose_excerpt = re.sub(r"[^a-z0-9]+", " ", excerpt.lower()).strip()
    if len(loose_excerpt.split()) < 4:
        return None, None, ""
    hits: list[dict[str, Any]] = []
    for cue in cues:
        loose_cue = re.sub(r"[^a-z0-9]+", " ", str(cue.get("text") or "").lower()).strip()
        if not loose_cue:
            continue
        if loose_cue in loose_excerpt or loose_excerpt in loose_cue:
            hits.append(cue)
            continue
        # Partial overlap: shared 4-word phrase
        cue_words = loose_cue.split()
        if len(cue_words) >= 4:
            for i in range(len(cue_words) - 3):
                phrase = " ".join(cue_words[i : i + 4])
                if phrase in loose_excerpt:
                    hits.append(cue)
                    break
    if not hits:
        return None, None, ""
    starts = [c["start_sec"] for c in hits if c.get("start_sec") is not None]
    ends = [c["end_sec"] for c in hits if c.get("end_sec") is not None]
    speakers = [str(c.get("speaker") or "") for c in hits if c.get("speaker")]
    speaker = max(set(speakers), key=speakers.count) if speakers else ""
    return (
        float(min(starts)) if starts else None,
        float(max(ends)) if ends else None,
        speaker[:200],
    )


def parse_story_items(
    raw: Any,
    chunk_text: str,
    cues: list[dict[str, Any]],
    labelled_text: str = "",
) -> list[dict[str, Any]]:
    items_raw = raw.get("items") if isinstance(raw, dict) else raw
    kept: list[dict[str, Any]] = []
    for item in items_raw or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in {"story", "fact"}:
            continue
        excerpt = normalize_whitespace(str(item.get("excerpt") or ""))
        text = normalize_whitespace(str(item.get("text") or ""))
        if not excerpt or not text:
            continue
        if len(excerpt.split()) > MAX_EXCERPT_WORDS:
            excerpt = " ".join(excerpt.split()[:MAX_EXCERPT_WORDS])
        word_n = len(text.split())
        if word_n < MIN_TEXT_WORDS or word_n > MAX_TEXT_WORDS:
            continue
        if not (
            is_loose_verbatim(excerpt, chunk_text)
            or (labelled_text and is_loose_verbatim(excerpt, labelled_text))
        ):
            continue
        tagged = parse_tag_payload(
            {"modules": item.get("modules") or [], "usable": item.get("usable", True)},
            VALID_MODULES,
        )
        start_sec, end_sec, mapped_speaker = map_excerpt_to_cues(excerpt, cues)
        speaker = normalize_whitespace(str(mapped_speaker or item.get("speaker") or ""))[:200]
        entities = item.get("entities") if isinstance(item.get("entities"), list) else []
        figures = item.get("figures") if isinstance(item.get("figures"), list) else []
        kept.append(
            {
                "kind": kind,
                "title": normalize_whitespace(str(item.get("title") or ""))[:200],
                "text": text,
                "excerpt": excerpt,
                "speaker": speaker,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "module_ids": tagged["module_ids"],
                "module_strengths": tagged["module_strengths"],
                "entities": _csv_join([str(v) for v in entities]),
                "figures": _csv_join([str(v) for v in figures]),
                "usable": bool(tagged["usable"]),
            }
        )
    return kept


def _default_extract(chunk_text: str, catalog: str) -> Any:
    model = (settings.passage_tagger_model or "").strip() or settings.openrouter_interpret_model
    return chat_json(
        [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {
                "role": "user",
                "content": f"MODULE CATALOG:\n{catalog}\n\nCHUNK:\n{chunk_text}",
            },
        ],
        model=model,
        timeout=180.0,
    )


def index_stories(
    db: Session,
    transcript_id: int,
    *,
    extract: Callable[[str], Any] | None = None,
    embed_fn: EmbedFn | None = None,
    workers: int = 4,
) -> dict[str, Any]:
    """Replace stories for one transcript. Total extract failure keeps existing rows."""
    transcript = db.get(StyleTranscript, transcript_id)
    if transcript is None:
        raise ValueError("Style transcript not found")
    cues = non_pratham_cues(transcript.raw_text or "")
    chunks = chunk_cues(cues)
    catalog = _catalog_block(db)
    extract_fn = extract or (lambda body: _default_extract(body, catalog))

    def run(chunk: dict[str, Any]) -> list[dict[str, Any]] | None:
        for _attempt in range(3):
            try:
                return parse_story_items(
                    extract_fn(chunk.get("labelled_text") or chunk["text"]),
                    chunk["text"],
                    chunk["cues"],
                    chunk.get("labelled_text") or "",
                )
            except Exception:  # noqa: BLE001
                continue
        return None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(run, chunks))
    if chunks and all(result is None for result in results):
        raise RuntimeError("Story extractor failed for every chunk; existing stories kept")

    kept: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    seen_excerpts: set[str] = set()
    for result in results:
        for item in result or []:
            digest = _story_hash(transcript_id, item["text"], item["excerpt"])
            excerpt_key = _excerpt_hash(item["excerpt"])
            if digest in seen_hashes or excerpt_key in seen_excerpts:
                continue
            seen_hashes.add(digest)
            seen_excerpts.add(excerpt_key)
            kept.append({**item, "text_hash": digest})

    embed = embed_fn or default_embed_texts
    vectors = (
        embed([f"{row['title']} {row['text']}".strip() for row in kept]) if kept else []
    )

    db.query(TranscriptStory).filter(TranscriptStory.style_transcript_id == transcript_id).delete()
    for index, (row, vector) in enumerate(zip(kept, vectors)):
        db.add(
            TranscriptStory(
                style_transcript_id=transcript_id,
                story_index=index,
                kind=row["kind"],
                title=row["title"],
                text=row["text"],
                excerpt=row["excerpt"],
                speaker=row["speaker"],
                start_sec=row["start_sec"],
                end_sec=row["end_sec"],
                module_ids=row["module_ids"],
                module_strengths_json=json.dumps(row["module_strengths"]),
                entities=row["entities"],
                figures=row["figures"],
                usable=row["usable"],
                source_name=transcript.name or "",
                source_url=getattr(transcript, "source_url", "") or "",
                embedding_json=json.dumps(vector),
                text_hash=row["text_hash"],
            )
        )
    db.commit()
    try:
        from backend.generation_cache import bump_content_version

        bump_content_version()
    except Exception:  # noqa: BLE001
        pass

    by_kind: Counter[str] = Counter(row["kind"] for row in kept if row["usable"])
    return {
        "stories": len(kept),
        "usable": sum(1 for row in kept if row["usable"]),
        "chunks": len(chunks),
        **dict(by_kind),
    }


def select_stories_for_topics(
    db: Session,
    *,
    topics: list[Any],
    embed_fn: EmbedFn | None = None,
    per_topic: int | None = None,
    word_cap: int | None = None,
) -> dict[str, Any]:
    per_topic = settings.transcript_stories_per_topic if per_topic is None else per_topic
    word_cap = settings.transcript_story_word_cap if word_cap is None else word_cap
    per_topic = max(1, int(per_topic))
    word_cap = max(0, int(word_cap))

    topic_specs: list[dict[str, Any]] = []
    for topic in topics or []:
        payload = _topic_payload(topic)
        try:
            topic_id = int(payload.get("topic_id") or 0)
        except (TypeError, ValueError):
            topic_id = 0
        if not topic_id:
            continue
        title = str(payload.get("title") or "")
        summary = str(payload.get("summary") or "")
        topic_specs.append(
            {
                "topic_id": topic_id,
                "title": title,
                "modules": _topic_modules(payload),
                "query": f"{title}. {summary}".strip(),
            }
        )
    empty = {
        "topics": [],
        "fed_words": 0,
        "fallback_topic_ids": [],
        "empty_topic_ids": [spec["topic_id"] for spec in topic_specs],
    }
    if not topic_specs:
        return {"topics": [], "fed_words": 0, "fallback_topic_ids": [], "empty_topic_ids": []}

    candidates: list[dict[str, Any]] = []
    for row in db.query(TranscriptStory).filter(TranscriptStory.usable.is_(True)).all():
        vector = parse_embedding(row.embedding_json)
        if not vector:
            continue
        try:
            strengths = json.loads(row.module_strengths_json or "{}")
        except json.JSONDecodeError:
            strengths = {}
        if not isinstance(strengths, dict):
            strengths = {}
        candidates.append(
            {
                "row": row,
                "vector": vector,
                "strengths": {str(k): str(v) for k, v in strengths.items()},
            }
        )
    if not candidates:
        return empty

    embed = embed_fn or default_embed_texts
    query_vectors = embed([spec["query"] for spec in topic_specs])

    scored: dict[int, list[dict[str, Any]]] = {}
    for spec, query_vector in zip(topic_specs, query_vectors):
        topic_id = spec["topic_id"]
        ranked: list[dict[str, Any]] = []
        for item in candidates:
            module_score = _module_match_score(spec["modules"], item["strengths"])
            cosine = cosine_similarity(query_vector, item["vector"])
            if module_score <= 0.0:
                if cosine < FALLBACK_COSINE:
                    continue
                match = "fallback"
                score = COSINE_WEIGHT * cosine
            else:
                match = "module"
                score = module_score + COSINE_WEIGHT * cosine
            ranked.append(
                {
                    "id": item["row"].id,
                    "kind": item["row"].kind,
                    "title": item["row"].title or "",
                    "text": item["row"].text,
                    "speaker": item["row"].speaker or "",
                    "source_name": item["row"].source_name or "",
                    "start_sec": item["row"].start_sec,
                    "modules": [
                        mid for mid in (item["row"].module_ids or "").split(",") if mid
                    ],
                    "match": match,
                    "score": score,
                    "word_count": len((item["row"].text or "").split()),
                    "vector": item["vector"],
                }
            )
        ranked.sort(key=lambda entry: entry["score"], reverse=True)
        scored[topic_id] = ranked

    used_ids: set[int] = set()
    picked_vectors: list[list[float]] = []
    fed_words = 0
    picks: dict[int, list[dict[str, Any]]] = {spec["topic_id"]: [] for spec in topic_specs}
    fallback_topic_ids: list[int] = []
    empty_topic_ids: list[int] = []

    for _pass_index in range(per_topic):
        for spec in topic_specs:
            if fed_words >= word_cap:
                break
            topic_id = spec["topic_id"]
            if len(picks[topic_id]) >= per_topic:
                continue
            for entry in scored.get(topic_id) or []:
                if entry["id"] in used_ids:
                    continue
                if any(
                    cosine_similarity(entry["vector"], picked) >= NEAR_DUP_COSINE
                    for picked in picked_vectors
                ):
                    continue
                if fed_words + entry["word_count"] > word_cap:
                    continue
                used_ids.add(entry["id"])
                picked_vectors.append(entry["vector"])
                fed_words += entry["word_count"]
                clean = {
                    "id": entry["id"],
                    "kind": entry["kind"],
                    "title": entry["title"],
                    "text": entry["text"],
                    "speaker": entry["speaker"],
                    "source_name": entry["source_name"],
                    "start_sec": entry["start_sec"],
                    "modules": entry["modules"],
                    "match": entry["match"],
                    "score": round(float(entry["score"]), 4),
                    "word_count": entry["word_count"],
                }
                picks[topic_id].append(clean)
                if entry["match"] == "fallback" and topic_id not in fallback_topic_ids:
                    fallback_topic_ids.append(topic_id)
                break

    for spec in topic_specs:
        if not picks[spec["topic_id"]]:
            empty_topic_ids.append(spec["topic_id"])

    return {
        "topics": [
            {
                "topic_id": spec["topic_id"],
                "title": spec["title"],
                "modules": spec["modules"],
                "stories": picks[spec["topic_id"]],
            }
            for spec in topic_specs
            if picks[spec["topic_id"]]
        ],
        "fed_words": fed_words,
        "fallback_topic_ids": fallback_topic_ids,
        "empty_topic_ids": empty_topic_ids,
    }


def format_stories_for_prompt(selection: dict[str, Any] | None) -> str:
    if not selection:
        return ""
    blocks: list[str] = []
    for topic in selection.get("topics") or []:
        stories = topic.get("stories") or []
        if not stories:
            continue
        modules = ", ".join(topic.get("modules") or []) or "—"
        header = (
            f"[topic_id={topic.get('topic_id')} | {topic.get('title') or ''} | {modules}]"
        )
        parts = [header]
        for story in stories:
            kind = story.get("kind") or "story"
            title = story.get("title") or ""
            text = story.get("text") or ""
            speaker = story.get("speaker") or "unknown"
            source = story.get("source_name") or "unknown"
            start = story.get("start_sec")
            stamp = format_timestamp(float(start)) if start is not None else "?:??"
            parts.append(
                f"- [{kind}] {title} — {text} ({speaker}, {source} @ {stamp})"
            )
        blocks.append("\n".join(parts))
    if not blocks:
        return ""
    return (
        "REAL STORIES & FACTS BY BEAT — from other Masters' Union sessions "
        "(unverified transcript; use as concrete examples; only state a number if it "
        "matches a LOCKED FACT, otherwise describe without the number):\n"
        + "\n\n".join(blocks)
    )


def coverage_report(db: Session) -> dict[str, dict[str, int]]:
    report: dict[str, dict[str, int]] = {
        module_id: {"strong": 0, "passing": 0, "items": 0}
        for module_id in sorted(VALID_MODULES)
    }
    for row in db.query(TranscriptStory).filter(TranscriptStory.usable.is_(True)).all():
        try:
            strengths = json.loads(row.module_strengths_json or "{}")
        except json.JSONDecodeError:
            strengths = {}
        if not isinstance(strengths, dict):
            continue
        for module_id, strength in strengths.items():
            if module_id not in report:
                continue
            key = "strong" if strength == "strong" else "passing"
            report[module_id][key] += 1
            report[module_id]["items"] += 1
    return report


def _has_non_pratham_speech(raw_text: str) -> bool:
    return bool(non_pratham_cues(raw_text or ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    index_p = sub.add_parser("index", help="extract stories/facts from non-Pratham speech")
    index_p.add_argument("--transcript-id", type=int, default=0)
    index_p.add_argument("--force", action="store_true")
    sub.add_parser("coverage", help="per-module strong/passing story counts")
    preview = sub.add_parser("preview", help="preview stories for a module/topic")
    preview.add_argument("--module", default="")
    preview.add_argument("--topic", default="Topic")
    args = parser.parse_args(argv)

    ensure_schema()
    db = SessionLocal()
    try:
        if args.command == "coverage":
            report = coverage_report(db)
            for module_id, stats in report.items():
                print(
                    f"{module_id}: strong={stats['strong']} passing={stats['passing']} "
                    f"items={stats['items']}"
                )
            return 0
        if args.command == "preview":
            from types import SimpleNamespace

            topic = SimpleNamespace(
                topic_id=1,
                title=args.topic,
                summary="",
                module_ids=[args.module] if args.module else [],
                recipe_modules=[args.module] if args.module else [],
                to_prompt_dict=lambda: {
                    "topic_id": 1,
                    "title": args.topic,
                    "summary": "",
                    "module_ids": [args.module] if args.module else [],
                    "recipe_modules": [args.module] if args.module else [],
                },
            )
            selection = select_stories_for_topics(db, topics=[topic])
            print(format_stories_for_prompt(selection) or "(nothing)")
            return 0
        query = db.query(StyleTranscript)
        if args.transcript_id:
            query = query.filter(StyleTranscript.id == args.transcript_id)
        for row in query.order_by(StyleTranscript.id.asc()).all():
            if not args.transcript_id and not _has_non_pratham_speech(row.raw_text or ""):
                continue
            has_rows = (
                db.query(TranscriptStory)
                .filter(TranscriptStory.style_transcript_id == row.id)
                .first()
            )
            if has_rows and not args.force:
                print(f"ST {row.id}: already has stories", flush=True)
                continue
            try:
                result = index_stories(db, row.id)
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
