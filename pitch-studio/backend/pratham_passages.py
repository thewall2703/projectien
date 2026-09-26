"""Pratham passages: long contiguous speech tagged by module, retrieved per beat.

index   -> split Pratham-only style transcripts into passages, tag modules, embed
select  -> for each script topic, pick 1–2 passages about that beat's modules
          (persona is a ranking boost, never a gate)
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
from backend.models import FounderQuote, Module, PrathamPassage, StyleTranscript
from backend.pipeline.llm import chat_json
from backend.pipeline.style_guide import SENTENCE_SPLIT_RE
from backend.pipeline.validator import split_spoken_sentences
from backend.pratham_playbook import _is_pratham_only, persona_transcript_ids, pratham_text
from backend.transcript_search import (
    EmbedFn,
    cosine_similarity,
    default_embed_texts,
    parse_embedding,
)
from backend.transcripts import VALID_MODULES, is_pratham_quote, normalize_module_ids, normalize_whitespace

TARGET_WORDS = 320
MIN_WORDS = 160
MAX_WORDS = 450
FALLBACK_COSINE = 0.3
STRENGTH_STRONG = 1.0
STRENGTH_PASSING = 0.55
PERSONA_BOOST = 0.2
COSINE_WEIGHT = 0.35
# Pratham retells the same story across sessions; on the real corpus, passage
# pairs at or above this similarity are retellings (max observed ~0.80).
NEAR_DUP_COSINE = 0.75
LINK_OPENERS = frozenset(
    {
        "so",
        "and",
        "but",
        "because",
        "now",
        "that",
        "this",
        "it",
        "then",
        "which",
        "those",
        "these",
        "they",
        "or",
        "yet",
        "also",
        "still",
    }
)
TAG_SYSTEM = (
    "You tag a contiguous passage of Pratham Mittal's live speech (founder, Masters' Union).\n"
    "Decide which Masters' Union modules (M01–M14) this passage is ABOUT.\n"
    "- strong: the passage is mainly about that module.\n"
    "- passing: it touches the module briefly.\n"
    "Use [] if it is not about any module.\n"
    "audience: who he is addressing (short free text).\n"
    "summary: one line.\n"
    "usable=false for logistics, banter, crowd management, garbled transcription, "
    "or no transferable point.\n"
    "Return JSON only:\n"
    '{"modules":[{"id":"M__","strength":"strong|passing"}],'
    '"audience":"...","summary":"...","usable":true}'
)


def split_passages(
    text: str,
    target_words: int = TARGET_WORDS,
    min_words: int = MIN_WORDS,
    max_words: int = MAX_WORDS,
) -> list[str]:
    """Pack contiguous sentences into passages. No overlap; never split a sentence."""
    sentences = [
        sentence.strip()
        for line in (text or "").splitlines()
        for sentence in SENTENCE_SPLIT_RE.split(line)
        if sentence.strip()
    ]
    if not sentences:
        return []

    passages: list[str] = []
    current: list[str] = []
    current_words = 0

    def flush() -> None:
        nonlocal current, current_words
        if current:
            passages.append(" ".join(current))
            current = []
            current_words = 0

    for sentence in sentences:
        words = len(sentence.split())
        if current and current_words + words > max_words:
            flush()
        current.append(sentence)
        current_words += words
        if current_words >= target_words:
            flush()

    if current:
        if passages and current_words < min_words:
            passages[-1] = f"{passages[-1]} {' '.join(current)}".strip()
        else:
            passages.append(" ".join(current))
    return passages


def parse_tag_payload(raw: Any, valid_ids: set[str] | frozenset[str]) -> dict[str, Any]:
    """Normalise a tagger response. Pure; unit-tested."""
    if not isinstance(raw, dict):
        raw = {}
    modules_raw = raw.get("modules") if isinstance(raw.get("modules"), list) else []
    strengths: dict[str, str] = {}
    for item in modules_raw:
        if not isinstance(item, dict):
            continue
        module_id = normalize_module_ids(item.get("id") or item.get("module_id") or "")
        if not module_id or module_id not in valid_ids:
            # normalize_module_ids may return csv; take first valid
            parts = [part for part in module_id.split(",") if part in valid_ids]
            if not parts:
                continue
            module_id = parts[0]
        if module_id in strengths:
            continue
        strength = str(item.get("strength") or "passing").strip().lower()
        if strength not in {"strong", "passing"}:
            strength = "passing"
        strengths[module_id] = strength
        if len(strengths) >= 3:
            break
    usable_raw = raw.get("usable", True)
    usable = usable_raw is True or str(usable_raw).lower() in {"true", "1", "yes"}
    return {
        "module_ids": ",".join(strengths.keys()),
        "module_strengths": strengths,
        "audience": normalize_whitespace(str(raw.get("audience") or ""))[:200],
        "summary": normalize_whitespace(str(raw.get("summary") or ""))[:400],
        "usable": usable,
    }


def _catalog_block(db: Session) -> str:
    rows = db.query(Module).order_by(Module.sort_order.asc(), Module.id.asc()).all()
    lines: list[str] = []
    for row in rows:
        core = normalize_whitespace(row.core_content or "")[:200]
        lines.append(f"{row.id} — {row.name} — {row.job} — {core}")
    return "\n".join(lines) or "(no modules)"


def _default_tag(text: str, catalog: str) -> Any:
    model = (settings.passage_tagger_model or "").strip() or settings.openrouter_interpret_model
    return chat_json(
        [
            {"role": "system", "content": TAG_SYSTEM},
            {
                "role": "user",
                "content": f"MODULE CATALOG:\n{catalog}\n\nPASSAGE:\n{text}",
            },
        ],
        model=model,
        timeout=120.0,
    )


def _passage_hash(transcript_id: int, index: int, text: str) -> str:
    return hashlib.sha256(f"{transcript_id}|{index}|{normalize_whitespace(text)}".encode("utf-8")).hexdigest()


def index_passages(
    db: Session,
    transcript_id: int,
    *,
    tag: Callable[[str], Any] | None = None,
    embed_fn: EmbedFn | None = None,
    workers: int = 6,
) -> dict[str, Any]:
    """Replace passages for one processed Pratham-only style transcript."""
    transcript = db.get(StyleTranscript, transcript_id)
    if transcript is None:
        raise ValueError("Style transcript not found")
    if not _is_pratham_only(transcript.raw_text or ""):
        return {"passages": 0, "usable": 0, "by_module": {}}
    text = pratham_text(transcript.raw_text or "")
    bodies = split_passages(text) if text.strip() else []
    catalog = _catalog_block(db)
    tag_fn = tag or (lambda body: _default_tag(body, catalog))

    def run(body: str) -> dict[str, Any] | None:
        # The tagger model intermittently returns an empty response.
        for _attempt in range(3):
            try:
                return parse_tag_payload(tag_fn(body), VALID_MODULES)
            except Exception:  # noqa: BLE001
                continue
        return None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(run, bodies))
    if bodies and all(result is None for result in results):
        raise RuntimeError("Passage tagger failed for every passage; existing passages kept")
    tags = [
        result
        or {"module_ids": "", "module_strengths": {}, "audience": "", "summary": "", "usable": False}
        for result in results
    ]

    rows: list[dict[str, Any]] = []
    for index, (body, tagged) in enumerate(zip(bodies, tags)):
        rows.append(
            {
                "passage_index": index,
                "text": body,
                "word_count": len(body.split()),
                "module_ids": tagged["module_ids"],
                "module_strengths": tagged["module_strengths"],
                "audience": tagged["audience"],
                "summary": tagged["summary"],
                "usable": bool(tagged["usable"]),
                "text_hash": _passage_hash(transcript_id, index, body),
            }
        )

    embed = embed_fn or default_embed_texts
    vectors = (
        embed([f"{row['summary']} {row['text']}".strip() for row in rows]) if rows else []
    )

    db.query(PrathamPassage).filter(PrathamPassage.style_transcript_id == transcript_id).delete()
    for row, vector in zip(rows, vectors):
        db.add(
            PrathamPassage(
                style_transcript_id=transcript_id,
                passage_index=row["passage_index"],
                text=row["text"],
                word_count=row["word_count"],
                module_ids=row["module_ids"],
                module_strengths_json=json.dumps(row["module_strengths"]),
                audience=row["audience"],
                summary=row["summary"],
                usable=row["usable"],
                source_name=transcript.name or "",
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

    by_module: Counter[str] = Counter()
    usable_n = 0
    for row in rows:
        if row["usable"]:
            usable_n += 1
        for module_id in (row["module_ids"] or "").split(","):
            if module_id:
                by_module[module_id] += 1
    return {"passages": len(rows), "usable": usable_n, "by_module": dict(by_module)}


def _topic_payload(topic: Any) -> dict[str, Any]:
    if hasattr(topic, "to_prompt_dict"):
        payload = topic.to_prompt_dict()
        return payload if isinstance(payload, dict) else {}
    return topic if isinstance(topic, dict) else {}


def _topic_modules(payload: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for key in ("module_ids", "recipe_modules"):
        raw = payload.get(key) or []
        if isinstance(raw, str):
            parts = [part for part in normalize_module_ids(raw).split(",") if part]
        else:
            parts = []
            for item in raw:
                mid = normalize_module_ids(item)
                if mid:
                    parts.extend(mid.split(","))
        for module_id in parts:
            if module_id and module_id not in seen:
                seen.add(module_id)
                ordered.append(module_id)
    return ordered


def _module_match_score(topic_modules: list[str], strengths: dict[str, str]) -> float:
    best = 0.0
    for module_id in topic_modules:
        strength = strengths.get(module_id)
        if strength == "strong":
            best = max(best, STRENGTH_STRONG)
        elif strength == "passing":
            best = max(best, STRENGTH_PASSING)
    return best


def select_passages_for_topics(
    db: Session,
    *,
    topics: list[Any],
    persona_label: str,
    context_note: str = "",
    embed_fn: EmbedFn | None = None,
    per_topic: int | None = None,
    word_cap: int | None = None,
) -> dict[str, Any]:
    """Pick 1–2 long passages per beat. Persona boosts score; never gates."""
    del context_note  # reserved for future query enrichment
    per_topic = settings.pratham_passages_per_topic if per_topic is None else per_topic
    word_cap = settings.pratham_passage_word_cap if word_cap is None else word_cap
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

    tagged_ids = set(persona_transcript_ids(db, persona_label))
    transcripts = (
        db.query(StyleTranscript)
        .filter(StyleTranscript.status == "processed")
        .all()
    )
    pratham_only_ids = {
        row.id for row in transcripts if _is_pratham_only(row.raw_text or "")
    }
    if not pratham_only_ids:
        return empty

    candidates: list[dict[str, Any]] = []
    for row in (
        db.query(PrathamPassage)
        .filter(
            PrathamPassage.style_transcript_id.in_(pratham_only_ids),
            PrathamPassage.usable.is_(True),
        )
        .all()
    ):
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
                "persona_boost": PERSONA_BOOST if row.style_transcript_id in tagged_ids else 0.0,
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
                score = COSINE_WEIGHT * cosine + item["persona_boost"]
            else:
                match = "module"
                score = module_score + COSINE_WEIGHT * cosine + item["persona_boost"]
            ranked.append(
                {
                    "id": item["row"].id,
                    "text": item["row"].text,
                    "source_name": item["row"].source_name or "",
                    "modules": [
                        mid
                        for mid in (item["row"].module_ids or "").split(",")
                        if mid
                    ],
                    "match": match,
                    "score": score,
                    "word_count": int(item["row"].word_count or len((item["row"].text or "").split())),
                    "module_score": module_score,
                    "cosine": cosine,
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

    for pass_index in range(per_topic):
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
                    "text": entry["text"],
                    "source_name": entry["source_name"],
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
                "passages": picks[spec["topic_id"]],
            }
            for spec in topic_specs
            if picks[spec["topic_id"]]
        ],
        "fed_words": fed_words,
        "fallback_topic_ids": fallback_topic_ids,
        "empty_topic_ids": empty_topic_ids,
    }


def format_passages_for_prompt(selection: dict[str, Any] | None) -> str:
    if not selection:
        return ""
    blocks: list[str] = []
    for topic in selection.get("topics") or []:
        passages = topic.get("passages") or []
        if not passages:
            continue
        modules = ", ".join(topic.get("modules") or []) or "—"
        header = (
            f"[topic_id={topic.get('topic_id')} | {topic.get('title') or ''} | {modules}]"
        )
        parts = [header]
        for passage in passages:
            source = passage.get("source_name") or "unknown"
            parts.append(f"(from {source})\n{passage.get('text') or ''}")
        blocks.append("\n".join(parts))
    if not blocks:
        return ""
    return (
        "PRATHAM BY BEAT — his real speech about each beat's module "
        "(verbatim transcript, not verified facts):\n"
        + "\n\n".join(blocks)
    )


def link_rate(text: str) -> float:
    """Share of consecutive sentence pairs where the second opens with a link/pointer."""
    sentences = [
        sentence
        for sentence in split_spoken_sentences(text or "")
        if len(sentence.split()) >= 3
    ]
    if len(sentences) < 2:
        return 0.0
    linked = 0
    pairs = 0
    for previous, current in zip(sentences, sentences[1:]):
        pairs += 1
        words = current.split()
        first = words[0].lower().strip("\",'“”‘’")
        prev_keys = {w.lower().strip("\",.'“”‘’") for w in previous.split() if len(w) > 3}
        if first in LINK_OPENERS or first in prev_keys:
            linked += 1
            continue
        # "That's" / "And that" style openers
        if first.rstrip("'s") in LINK_OPENERS:
            linked += 1
            continue
        if len(words) >= 2:
            second = words[1].lower().strip("\",'“”‘’")
            if first in {"and", "but", "so", "because", "now"} and (
                second in LINK_OPENERS or second in prev_keys
            ):
                linked += 1
    return linked / pairs if pairs else 0.0


def phrase_reuse_rate(script_text: str, source_text: str) -> float:
    """Share of the script's 4-word phrases that appear in the fed source text."""
    def phrases(text: str) -> list[str]:
        words = re.findall(r"[a-z0-9']+", (text or "").lower())
        if len(words) < 4:
            return []
        return [" ".join(words[i : i + 4]) for i in range(len(words) - 3)]

    script_phrases = phrases(script_text)
    if not script_phrases:
        return 0.0
    source_set = set(phrases(source_text))
    if not source_set:
        return 0.0
    hits = sum(1 for phrase in script_phrases if phrase in source_set)
    return hits / len(script_phrases)


def coverage_report(db: Session) -> dict[str, dict[str, int]]:
    report: dict[str, dict[str, int]] = {
        module_id: {"strong": 0, "passing": 0, "words": 0} for module_id in sorted(VALID_MODULES)
    }
    for row in db.query(PrathamPassage).filter(PrathamPassage.usable.is_(True)).all():
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
            report[module_id]["words"] += int(row.word_count or 0)
    return report


def retag_quotes(
    db: Session,
    *,
    tag: Callable[[str], Any] | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Re-tag approved Pratham FounderQuotes' module_ids (skips edited=True)."""
    catalog = _catalog_block(db)
    tag_fn = tag or (lambda body: _default_tag(body, catalog))
    quotes = (
        db.query(FounderQuote)
        .filter(FounderQuote.status == "approved", FounderQuote.edited.is_(False))
        .all()
    )
    updated = 0
    skipped = 0
    for quote in quotes:
        if not is_pratham_quote(quote):
            skipped += 1
            continue
        try:
            tagged = parse_tag_payload(tag_fn(quote.text or ""), VALID_MODULES)
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        if quote.module_ids == tagged["module_ids"]:
            skipped += 1
            continue
        if not dry_run:
            quote.module_ids = tagged["module_ids"]
        updated += 1
    if not dry_run and updated:
        db.commit()
        try:
            from backend.generation_cache import bump_content_version

            bump_content_version()
        except Exception:  # noqa: BLE001
            pass
    return {"updated": updated, "skipped": skipped, "total": len(quotes)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    index_p = sub.add_parser("index", help="index passages for processed Pratham-only style transcripts")
    index_p.add_argument("--transcript-id", type=int, default=0)
    index_p.add_argument("--force", action="store_true")
    sub.add_parser("coverage", help="per-module strong/passing passage counts")
    preview = sub.add_parser("preview", help="preview passages for a persona/module/topic")
    preview.add_argument("--persona", default="")
    preview.add_argument("--module", default="")
    preview.add_argument("--topic", default="Topic")
    retag = sub.add_parser("retag-quotes", help="re-tag approved Pratham founder quotes' module_ids")
    retag.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    ensure_schema()
    db = SessionLocal()
    try:
        if args.command == "coverage":
            report = coverage_report(db)
            for module_id, stats in report.items():
                print(
                    f"{module_id}: strong={stats['strong']} passing={stats['passing']} "
                    f"words={stats['words']}"
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
            selection = select_passages_for_topics(
                db, topics=[topic], persona_label=args.persona
            )
            print(format_passages_for_prompt(selection) or "(nothing)")
            return 0
        if args.command == "retag-quotes":
            result = retag_quotes(db, dry_run=args.dry_run)
            print(result)
            return 0
        query = db.query(StyleTranscript).filter(StyleTranscript.status == "processed")
        if args.transcript_id:
            query = query.filter(StyleTranscript.id == args.transcript_id)
        for row in query.order_by(StyleTranscript.id.asc()).all():
            if not args.transcript_id and not _is_pratham_only(row.raw_text or ""):
                continue
            has_rows = (
                db.query(PrathamPassage)
                .filter(PrathamPassage.style_transcript_id == row.id)
                .first()
            )
            if has_rows and not args.force:
                print(f"ST {row.id}: already has passages", flush=True)
                continue
            try:
                result = index_passages(db, row.id)
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
