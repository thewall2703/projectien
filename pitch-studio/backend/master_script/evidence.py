"""Engine 3 — Evidence over curated noun→card table."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from backend.config import DATA_DIR, settings
from backend.models import EvidenceCard, MasterNoun
from backend.pipeline.llm import chat_json
from backend.transcript_search import cosine_similarity, default_embed_texts

GAPS_PATH = DATA_DIR / "master_script" / "gaps.jsonl"

# Master Script exclusions enforced at card selection time.
_EXCLUSION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bblue\s*brew\b", re.I), "Blue Brew figures excluded"),
    (
        re.compile(
            r"\b(?:vip|venture\s+initiation\s+programme)\b.*\b(?:total|raised|valuation|startups)\b|"
            r"\b(?:total|raised|valuation|startups)\b.*\b(?:vip|venture\s+initiation\s+programme)\b",
            re.I,
        ),
        "VIP programme totals excluded",
    ),
    (re.compile(r"\bjobs?\s+created\b", re.I), "jobs-created figures excluded"),
]


def is_excluded_claim(text: str, figure_label: str = "") -> str:
    blob = f"{text} {figure_label}".strip()
    if re.search(r"\bblue\s*brew\b", blob, re.I):
        return "Blue Brew figures excluded"
    if re.search(r"\bjobs?\s+created\b", blob, re.I):
        return "jobs-created figures excluded"
    if re.search(r"\b(?:vip|venture\s+initiation)\b", blob, re.I) and re.search(
        r"\b(?:total|raised|valuation|startups?)\b", blob, re.I
    ):
        return "VIP programme totals excluded"
    # Unlabelled venture figures: has a money-ish figure but no label.
    if re.search(r"(?:\d|lakh|crore|\$|₹)", blob, re.I) and re.search(
        r"\b(?:venture|startup|raised|revenue|arr)\b", blob, re.I
    ):
        label = (figure_label or "").strip().lower()
        if label not in {"raised", "revenue", "arr"} and not re.search(
            r"\b(?:raised|revenue|arr)\b", blob, re.I
        ):
            return "unlabelled venture figures excluded"
    return ""


def append_gap(entry: dict[str, Any], path: Path | None = None) -> None:
    target = path or GAPS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    row = {
        **entry,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _parse_aliases(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        data = []
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    return []


def _card_to_dict(card: EvidenceCard) -> dict[str, Any]:
    return {
        "id": card.id,
        "noun_id": card.noun_id,
        "kind": card.kind,
        "claim": card.claim,
        "figure": card.figure,
        "figure_label": card.figure_label,
        "speaker": card.speaker,
        "source_type": card.source_type,
        "source_ref": card.source_ref,
        "source_date": card.source_date,
        "checkability": card.checkability,
        "consent_ok": bool(card.consent_ok),
        "strength": float(card.strength or 0),
        "excluded": bool(card.excluded),
        "exclusion_reason": card.exclusion_reason or "",
    }


def _keyword_hit(aliases: list[str], text: str) -> bool:
    lower = (text or "").lower()
    for alias in aliases:
        token = alias.lower().strip()
        if len(token) >= 3 and token in lower:
            return True
    return False


def retrieve_candidates(
    db: Session,
    request: dict[str, Any],
    *,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    noun = str(request.get("noun") or "").strip()
    need = str(request.get("need") or "").strip()
    query = f"{noun} {need}".strip()
    if not query:
        return []

    nouns = db.query(MasterNoun).filter(MasterNoun.approved.is_(True)).all()
    matched_ids: set[int] = set()
    for row in nouns:
        aliases = [row.canonical, *_parse_aliases(row.aliases_json)]
        if noun and any(noun.lower() == a.lower() for a in aliases if a):
            matched_ids.add(row.id)
        elif not noun and _keyword_hit(aliases, query):
            matched_ids.add(row.id)

    # A named request must never escape the user's approved noun list.
    if noun and not matched_ids:
        return []
    approved_ids = {row.id for row in nouns}
    if not approved_ids:
        return []
    q = db.query(EvidenceCard).filter(
        EvidenceCard.noun_id.in_(matched_ids or approved_ids),
        EvidenceCard.excluded.is_(False),
        EvidenceCard.consent_ok.is_(True),
    )
    if matched_ids:
        q = q.filter(EvidenceCard.noun_id.in_(matched_ids))
    cards = q.all()

    embed = embed_fn or default_embed_texts
    try:
        query_vec = embed([query])[0]
    except Exception:
        query_vec = []

    scored: list[tuple[float, EvidenceCard]] = []
    for card in cards:
        if not card.source_type or not card.source_ref or not card.checkability:
            continue
        reason = is_excluded_claim(card.claim or "", card.figure_label or "")
        if reason or card.excluded:
            continue
        score = float(card.strength or 0)
        if card.kind == "approved_story":
            score += 1.0
        aliases_blob = f"{card.claim} {card.figure} {card.figure_label}"
        if noun and noun.lower() in aliases_blob.lower():
            score += 0.2
        if need and any(tok in aliases_blob.lower() for tok in need.lower().split() if len(tok) > 3):
            score += 0.1
        if query_vec and card.embedding_json:
            try:
                vector = json.loads(card.embedding_json)
            except json.JSONDecodeError:
                vector = []
            if isinstance(vector, list) and vector:
                score += cosine_similarity(query_vec, vector)
        scored.append((score, card))

    scored.sort(key=lambda item: item[0], reverse=True)
    out: list[dict[str, Any]] = []
    for score, card in scored[:limit]:
        payload = _card_to_dict(card)
        payload["score"] = score
        out.append(payload)
    return out


RERANK_SYSTEM = """You rerank evidence cards for a Master Script pitch.
Return JSON: {"cards":[{"id":123,"verdict":"use|avoid|insufficient","reason":"..."}],"verdict":"use|avoid|insufficient"}
Enforce: no Blue Brew figures, no unlabelled venture figures, no VIP programme totals, no jobs-created figures.
Prefer labelled, checkable claims. If nothing is good enough, verdict=insufficient.
Prefer relevant approved_story cards: these are the user's approved pitch versions,
with original supporting material in checkability. Approval governs presentation,
not truth; still reject a story if it contradicts its sources or restrictions.
A supported explanation of what students can do, the resource or process enabling it,
and its practical usefulness is valid evidence. A named student or measured success
outcome is optional enrichment, not a condition for use. Do not invent a benefit,
outcome, or causal link; preserve source attribution and distinguish plans from results.
"""


def _rerank(
    request: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not candidates:
        return {
            "section_id": request.get("section_id") or "",
            "noun": request.get("noun") or "",
            "need": request.get("need") or "",
            "cards": [],
            "verdict": "insufficient",
        }
    if dry_run:
        cards = []
        for card in candidates[:3]:
            reason = is_excluded_claim(card.get("claim") or "", card.get("figure_label") or "")
            verdict = "avoid" if reason else "use"
            row = dict(card)
            row["verdict"] = verdict
            row["reason"] = reason or "top retrieval hit"
            cards.append(row)
        verdict = "use" if any(c["verdict"] == "use" for c in cards) else "insufficient"
        return {
            "section_id": request.get("section_id") or "",
            "noun": request.get("noun") or "",
            "need": request.get("need") or "",
            "cards": cards,
            "verdict": verdict,
        }

    try:
        result = chat_json(
            [
                {"role": "system", "content": RERANK_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"request": request, "candidates": candidates},
                        ensure_ascii=False,
                    ),
                },
            ],
            role="ms_evidence",
        )
    except Exception:
        return {**request, "cards": [], "verdict": "insufficient",
                "reason": "Evidence evaluation failed; candidates were not approved for use."}

    by_id = {
        int(item.get("id")): item
        for item in (result.get("cards") or [])
        if isinstance(item, dict) and str(item.get("id") or "").lstrip("-").isdigit()
    }
    cards: list[dict[str, Any]] = []
    for card in candidates:
        item = by_id.get(int(card["id"])) or {}
        reason = is_excluded_claim(card.get("claim") or "", card.get("figure_label") or "")
        verdict = str(item.get("verdict") or "insufficient")
        if verdict not in {"use", "avoid", "insufficient"}:
            verdict = "insufficient"
        if reason:
            verdict = "avoid"
        row = dict(card)
        row["verdict"] = verdict
        row["reason"] = reason or str(item.get("reason") or "")
        cards.append(row)
    overall = str(result.get("verdict") or "")
    if overall not in {"use", "avoid", "insufficient"}:
        overall = "use" if any(c["verdict"] == "use" for c in cards) else "insufficient"
    if overall != "use":
        cards = [{**card, "verdict": "insufficient"} for card in cards]
    elif not any(card["verdict"] == "use" for card in cards):
        overall = "insufficient"
    return {
        "section_id": request.get("section_id") or "",
        "noun": request.get("noun") or "",
        "need": request.get("need") or "",
        "cards": cards,
        "verdict": overall,
    }


def fulfil_requests(
    db: Session,
    requests: list[dict[str, Any]],
    *,
    room: str = "",
    dry_run: bool = False,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    max_workers: int = 4,
    follow_up: bool = True,
) -> list[dict[str, Any]]:
    """Batch pattern: one parallel fulfilment round, optional follow-up for insufficient."""

    # SQLAlchemy Sessions are not thread safe. Retrieve on the owning thread;
    # only the independent LLM evaluations run concurrently.
    def run_round(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        batches = [(req, retrieve_candidates(db, req, embed_fn=embed_fn)) for req in items]
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_rerank, req, cards, dry_run=dry_run) for req, cards in batches]
            return [future.result() for future in futures]

    results: list[dict[str, Any]] = []
    if not requests:
        return results

    results = run_round(requests)

    insufficient = [row for row in results if row.get("verdict") == "insufficient"]
    if follow_up and insufficient:
        # One bounded follow-up: broaden noun-less needs and retry.
        retries: list[dict[str, Any]] = []
        for row in insufficient:
            retries.append(
                {
                    "noun": row.get("noun") or "",
                    "need": str(row.get("need") or row.get("noun") or ""),
                    "section_id": row.get("section_id") or "",
                }
            )
        retry_results = run_round(retries)
        # Replace insufficient with retry when improved.
        by_section_need = {
            (str(r.get("section_id")), str(r.get("need")), str(r.get("noun"))): r for r in retry_results
        }
        merged: list[dict[str, Any]] = []
        for row in results:
            key = (str(row.get("section_id")), str(row.get("need")), str(row.get("noun")))
            alt = by_section_need.get(key)
            if row.get("verdict") == "insufficient" and alt and alt.get("verdict") == "use":
                merged.append(alt)
            else:
                merged.append(row)
        results = merged

    for row in results:
        if row.get("verdict") == "insufficient":
            append_gap(
                {
                    "room": room,
                    "section_id": row.get("section_id"),
                    "noun": row.get("noun"),
                    "need": row.get("need"),
                    "verdict": "insufficient",
                }
            )
    return results


def cards_for_plan(
    fulfilled: list[dict[str, Any]],
    plan: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Map section_id → chosen usable cards (full payloads)."""
    by_id: dict[int, dict[str, Any]] = {}
    for row in fulfilled:
        for card in row.get("cards") or []:
            try:
                by_id[int(card["id"])] = card
            except (KeyError, TypeError, ValueError):
                continue
    out: dict[str, list[dict[str, Any]]] = {}
    for section in plan.get("sections") or []:
        sid = str(section.get("section_id") or "")
        chosen: list[dict[str, Any]] = []
        for item in section.get("chosen_cards") or []:
            try:
                card_id = int(item.get("card_id"))
            except (TypeError, ValueError, AttributeError):
                continue
            card = by_id.get(card_id)
            if card and card.get("verdict") == "use" and any(
                str(row.get("section_id") or "") == sid
                and row.get("verdict") == "use"
                and any(c.get("id") == card_id for c in row.get("cards") or [])
                for row in fulfilled
            ):
                chosen.append(card)
        if not chosen:
            # Fall back to use-verdict cards returned for this section.
            for row in fulfilled:
                if str(row.get("section_id") or "") != sid:
                    continue
                if row.get("verdict") != "use":
                    continue
                for card in row.get("cards") or []:
                    if card.get("verdict") == "use":
                        chosen.append(card)
        out[sid] = chosen[:5]
    return out
