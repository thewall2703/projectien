"""Vision modules (VM01–VM15): taxonomy, content index, and CLI.

Separate from the classic M01–M14 recipe modules and from ``VISION_HEADINGS``
in ``vision_deck.py`` (classic mode keeps that table byte-for-byte).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from typing import Any, Callable

from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal, ensure_schema
from backend.extract import passages_from_extract
from backend.models import (
    Asset,
    DeckTopic,
    LockedFact,
    PrathamPassage,
    TranscriptStory,
    VisionModuleContent,
)
from backend.pipeline.brand_deck import PAGE_LABELS, PAGE_MODULES
from backend.pipeline.llm import chat_json
from backend.transcript_search import (
    EmbedFn,
    cosine_similarity,
    default_embed_texts,
    parse_embedding,
)
from backend.transcripts import normalize_whitespace

# Ordered fine headings with page ranges. Page 51 (five Outclass challenges
# recap) lives in Funded Ventures, not Immersions.
VISION_MODULES: tuple[tuple[str, str, int, int], ...] = (
    ("VM01", "The Founding Story", 1, 8),
    ("VM02", "Purpose, Vision and Governance", 9, 11),
    ("VM03", "Why Learn by Doing", 12, 18),
    ("VM04", "Learning Model", 19, 20),
    ("VM05", "Inclass", 21, 28),
    ("VM06", "Outclass: C&C", 29, 38),
    ("VM07", "Outclass: Labs", 39, 42),
    ("VM08", "Funded Ventures", 43, 51),
    ("VM09", "Immersions", 52, 56),
    ("VM10", "Outcomes", 57, 64),
    ("VM11", "Student Life", 65, 72),
    ("VM12", "Campus", 73, 82),
    ("VM13", "Programmes", 83, 87),
    ("VM14", "Recognition", 88, 90),
    ("VM15", "Vision Ahead", 91, 92),
)

VM_IDS = frozenset(row[0] for row in VISION_MODULES)
VM_BY_ID = {row[0]: row for row in VISION_MODULES}
SOURCE_TYPES = frozenset(
    {"pratham_passage", "transcript_story", "locked_fact", "report_passage"}
)
STRENGTH_STRONG = "strong"
STRENGTH_PASSING = "passing"
FORBIDDEN_FACT_STATUSES = frozenset(
    {"conflict", "do_not_use", "needs_source", "needs_decision"}
)

TAG_SYSTEM = (
    "You tag a content item to Masters' Union vision modules (VM01–VM15).\n"
    "Decide which vision modules this item is ABOUT (at most 3).\n"
    "- strong: mainly about that module.\n"
    "- passing: touches it briefly.\n"
    "page_hints: brand-deck page numbers (1–92) the item best supports, if any.\n"
    "Return JSON only:\n"
    '{"modules":[{"id":"VM08","strength":"strong"}],"page_hints":[45]}'
)

TagFn = Callable[[str, str], Any]


def vision_module_for_page(page: int) -> str | None:
    try:
        page_num = int(page)
    except (TypeError, ValueError):
        return None
    for vm_id, _title, start, end in VISION_MODULES:
        if start <= page_num <= end:
            return vm_id
    return None


def vm_title(vm_id: str) -> str:
    row = VM_BY_ID.get(vm_id)
    return row[1] if row else vm_id


def pages_for_vm(vm_id: str) -> list[int]:
    row = VM_BY_ID.get(vm_id)
    if not row:
        return []
    return list(range(row[2], row[3] + 1))


def _page_labels_for_vm(vm_id: str) -> list[str]:
    labels: list[str] = []
    for page in pages_for_vm(vm_id):
        label = PAGE_LABELS.get(page, "")
        if label:
            labels.append(f"p{page}: {label}")
    return labels


def build_vm_descriptions(db: Session | None = None) -> dict[str, str]:
    """One description per VM from page labels + DeckTopic summaries."""
    topic_bits: dict[str, list[str]] = defaultdict(list)
    if db is not None:
        from backend.deck_topic_index import parse_pages

        for topic in db.query(DeckTopic).all():
            summary = normalize_whitespace(getattr(topic, "summary", "") or "")
            vision = normalize_whitespace(getattr(topic, "vision", "") or "")
            bit = " ".join(part for part in (summary, vision) if part).strip()
            if not bit:
                continue
            for page in parse_pages(getattr(topic, "pages_json", "") or "[]"):
                vm_id = vision_module_for_page(page)
                if vm_id:
                    topic_bits[vm_id].append(bit)
    descriptions: dict[str, str] = {}
    for vm_id, title, start, end in VISION_MODULES:
        labels = _page_labels_for_vm(vm_id)
        extras = list(dict.fromkeys(topic_bits.get(vm_id) or []))[:4]
        parts = [f"{vm_id} {title} (pages {start}–{end})"]
        if labels:
            parts.append("Pages: " + "; ".join(labels))
        if extras:
            parts.append("Deck notes: " + " | ".join(extras))
        descriptions[vm_id] = "\n".join(parts)
    return descriptions


def _catalog_block(db: Session) -> str:
    descriptions = build_vm_descriptions(db)
    lines: list[str] = []
    for vm_id, title, start, end in VISION_MODULES:
        labels = ", ".join(
            PAGE_LABELS.get(page, f"Page {page}") for page in range(start, min(end, start + 4) + 1)
        )
        lines.append(
            f"{vm_id} — {title} (p{start}–{end}) — {labels}. "
            f"{(descriptions.get(vm_id) or '')[:280]}"
        )
    return "\n".join(lines)


def parse_vm_tag_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    modules_raw = raw.get("modules") if isinstance(raw.get("modules"), list) else []
    strengths: dict[str, str] = {}
    for item in modules_raw:
        if not isinstance(item, dict):
            continue
        vm_id = str(item.get("id") or item.get("vm_id") or "").strip().upper()
        if vm_id not in VM_IDS or vm_id in strengths:
            continue
        strength = str(item.get("strength") or STRENGTH_PASSING).strip().lower()
        if strength not in {STRENGTH_STRONG, STRENGTH_PASSING}:
            strength = STRENGTH_PASSING
        strengths[vm_id] = strength
        if len(strengths) >= 3:
            break
    hints: list[int] = []
    for item in raw.get("page_hints") or []:
        try:
            page = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= page <= 92 and page not in hints:
            hints.append(page)
        if len(hints) >= 8:
            break
    return {"modules": strengths, "page_hints": hints}


def _default_tag(text: str, catalog: str) -> Any:
    model = (settings.passage_tagger_model or "").strip() or settings.openrouter_interpret_model
    last_error: Exception | None = None
    for _ in range(3):
        try:
            return chat_json(
                [
                    {"role": "system", "content": TAG_SYSTEM},
                    {
                        "role": "user",
                        "content": f"VISION MODULE CATALOG:\n{catalog}\n\nCONTENT:\n{text}",
                    },
                ],
                model=model,
                timeout=120.0,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if last_error:
        raise last_error
    return {}


def _modules_from_m_tags(module_ids: str) -> dict[str, str]:
    """Map classic M01–M14 tags onto VMs via PAGE_MODULES overlap."""
    m_ids = {part.strip() for part in (module_ids or "").split(",") if part.strip()}
    if not m_ids:
        return {}
    hits: Counter[str] = Counter()
    for page, mid in PAGE_MODULES.items():
        if mid in m_ids:
            vm_id = vision_module_for_page(page)
            if vm_id:
                hits[vm_id] += 1
    if not hits:
        return {}
    ordered = [vm_id for vm_id, _count in hits.most_common(3)]
    strengths: dict[str, str] = {}
    for index, vm_id in enumerate(ordered):
        strengths[vm_id] = STRENGTH_STRONG if index == 0 else STRENGTH_PASSING
    return strengths


def _modules_from_cosine(
    text: str,
    descriptions: dict[str, str],
    vectors: dict[str, list[float]],
    text_vector: list[float] | None,
) -> dict[str, str]:
    if not text_vector:
        return {}
    scored: list[tuple[float, str]] = []
    for vm_id, desc_vec in vectors.items():
        if not desc_vec:
            continue
        scored.append((cosine_similarity(text_vector, desc_vec), vm_id))
    scored.sort(reverse=True)
    strengths: dict[str, str] = {}
    for score, vm_id in scored[:3]:
        if score < 0.2:
            continue
        strengths[vm_id] = STRENGTH_STRONG if score >= 0.35 else STRENGTH_PASSING
    return strengths


def _text_hash(*parts: str) -> str:
    body = "|".join(normalize_whitespace(part) for part in parts)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _fact_text(fact: LockedFact) -> str:
    return normalize_whitespace(f"{fact.fact}: {fact.value}. {fact.note or ''}")


def _collect_candidates(db: Session) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in db.query(PrathamPassage).filter(PrathamPassage.usable.is_(True)).all():
        text = normalize_whitespace(row.text or "")
        if not text:
            continue
        items.append(
            {
                "source_type": "pratham_passage",
                "source_ref": str(row.id),
                "text": text,
                "module_ids": row.module_ids or "",
                "embedding_json": row.embedding_json or "",
                "text_hash": _text_hash("pratham_passage", str(row.id), text),
            }
        )
    for row in db.query(TranscriptStory).filter(TranscriptStory.usable.is_(True)).all():
        text = normalize_whitespace(row.text or row.excerpt or "")
        if not text:
            continue
        items.append(
            {
                "source_type": "transcript_story",
                "source_ref": str(row.id),
                "text": text,
                "module_ids": row.module_ids or "",
                "embedding_json": row.embedding_json or "",
                "text_hash": _text_hash("transcript_story", str(row.id), text),
            }
        )
    for fact in db.query(LockedFact).all():
        if (fact.status or "") != "verified":
            continue
        text = _fact_text(fact)
        if not text:
            continue
        items.append(
            {
                "source_type": "locked_fact",
                "source_ref": str(fact.id),
                "text": text,
                "module_ids": fact.module_ids or "",
                "embedding_json": "",
                "text_hash": _text_hash("locked_fact", str(fact.id), text),
            }
        )
    for asset in db.query(Asset).filter(Asset.type == "report").all():
        if (asset.extract_status or "") != "ready" or not asset.extract_json:
            continue
        try:
            payload = json.loads(asset.extract_json)
        except json.JSONDecodeError:
            continue
        for index, passage in enumerate(
            passages_from_extract(
                payload,
                asset_id=asset.id,
                title=asset.title or "",
                module_ids=asset.module_ids or "",
            )
        ):
            text = normalize_whitespace(passage.get("text") or "")
            if not text:
                continue
            if len(text) > 1200:
                text = text[:1200].rsplit(" ", 1)[0]
            source_ref = f"{asset.id}:{index}"
            items.append(
                {
                    "source_type": "report_passage",
                    "source_ref": source_ref,
                    "text": text,
                    "module_ids": asset.module_ids or "",
                    "embedding_json": "",
                    "text_hash": _text_hash("report_passage", source_ref, text),
                }
            )
    return items


def _existing_map(db: Session) -> dict[tuple[str, str, str], VisionModuleContent]:
    mapping: dict[tuple[str, str, str], VisionModuleContent] = {}
    for row in db.query(VisionModuleContent).all():
        mapping[(row.vm_id, row.source_type, row.source_ref)] = row
    return mapping


def index_vision_module_content(
    db: Session,
    *,
    force: bool = False,
    tag: TagFn | None = None,
    embed_fn: EmbedFn | None = None,
) -> dict[str, Any]:
    """Tag usable content onto VMs. Never wipe rows if the run fails mid-way."""
    catalog = _catalog_block(db)
    descriptions = build_vm_descriptions(db)
    embed = embed_fn or default_embed_texts
    tag_fn = tag or (lambda body, cat=catalog: _default_tag(body, cat))

    desc_texts = [descriptions[vm_id] for vm_id, *_ in VISION_MODULES]
    desc_vectors_list = embed(desc_texts) if desc_texts else []
    desc_vectors = {
        vm_id: (desc_vectors_list[index] if index < len(desc_vectors_list) else [])
        for index, (vm_id, *_rest) in enumerate(VISION_MODULES)
    }

    candidates = _collect_candidates(db)
    existing = _existing_map(db)
    written = 0
    skipped = 0
    tagged_keys: set[tuple[str, str, str]] = set()
    need_embed: list[dict[str, Any]] = []
    prepared: list[dict[str, Any]] = []

    for item in candidates:
        key_preview = (item["source_type"], item["source_ref"], item["text_hash"])
        if not force:
            already = [
                row
                for (vm_id, st, sr), row in existing.items()
                if st == item["source_type"]
                and sr == item["source_ref"]
                and row.text_hash == item["text_hash"]
            ]
            if already:
                for row in already:
                    tagged_keys.add((row.vm_id, row.source_type, row.source_ref))
                skipped += 1
                continue
        prepared.append(item)

    for item in prepared:
        vector = parse_embedding(item.get("embedding_json") or "")
        if not vector:
            need_embed.append(item)
        else:
            item["vector"] = vector

    if need_embed:
        vectors = embed([row["text"] for row in need_embed])
        for row, vector in zip(need_embed, vectors):
            row["vector"] = vector

    try:
        for item in prepared:
            try:
                tagged = parse_vm_tag_payload(tag_fn(item["text"], catalog))
            except Exception:  # noqa: BLE001
                tagged = {"modules": {}, "page_hints": []}
            strengths = tagged.get("modules") or {}
            if not strengths:
                strengths = _modules_from_m_tags(item.get("module_ids") or "")
            if not strengths:
                strengths = _modules_from_cosine(
                    item["text"],
                    descriptions,
                    desc_vectors,
                    item.get("vector"),
                )
            if not strengths:
                skipped += 1
                continue
            page_hints = tagged.get("page_hints") or []
            hint_csv = ",".join(str(page) for page in page_hints)
            vector = item.get("vector") or []
            score_base = 1.0
            for vm_id, strength in strengths.items():
                key = (vm_id, item["source_type"], item["source_ref"])
                tagged_keys.add(key)
                row = existing.get(key)
                if row is None:
                    row = VisionModuleContent(
                        vm_id=vm_id,
                        source_type=item["source_type"],
                        source_ref=item["source_ref"],
                    )
                    db.add(row)
                    existing[key] = row
                row.text = item["text"]
                row.strength = strength
                row.score = score_base if strength == STRENGTH_STRONG else 0.55
                row.page_hints = hint_csv
                row.embedding_json = json.dumps(vector) if vector else ""
                row.text_hash = item["text_hash"]
                written += 1
        if force:
            for key, row in list(existing.items()):
                if key not in tagged_keys:
                    db.delete(row)
        db.commit()
    except Exception:
        db.rollback()
        raise

    try:
        from backend.generation_cache import bump_content_version

        if written:
            bump_content_version()
    except Exception:  # noqa: BLE001
        pass

    return {
        "candidates": len(candidates),
        "written": written,
        "skipped": skipped,
        "rows": db.query(VisionModuleContent).count(),
    }


def coverage_report(db: Session) -> dict[str, dict[str, int]]:
    report: dict[str, dict[str, int]] = {
        vm_id: {"strong": 0, "passing": 0, "total": 0} for vm_id, *_ in VISION_MODULES
    }
    for row in db.query(VisionModuleContent).all():
        bucket = report.setdefault(
            row.vm_id, {"strong": 0, "passing": 0, "total": 0}
        )
        if row.strength == STRENGTH_STRONG:
            bucket["strong"] += 1
        else:
            bucket["passing"] += 1
        bucket["total"] += 1
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    index_p = sub.add_parser("index", help="tag content onto vision modules")
    index_p.add_argument("--force", action="store_true")
    sub.add_parser("coverage", help="per-VM strong/passing content counts")
    args = parser.parse_args(argv)

    ensure_schema()
    db = SessionLocal()
    try:
        if args.command == "coverage":
            report = coverage_report(db)
            for vm_id, title, start, end in VISION_MODULES:
                stats = report.get(vm_id) or {"strong": 0, "passing": 0, "total": 0}
                print(
                    f"{vm_id} {title} (p{start}–{end}): "
                    f"strong={stats['strong']} passing={stats['passing']} "
                    f"total={stats['total']}"
                )
            return 0
        result = index_vision_module_content(db, force=bool(args.force))
        print(result)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
