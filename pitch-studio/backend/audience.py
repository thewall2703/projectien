from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import (
    ListenerProfile,
    ListenerTurn,
    StyleTranscript,
    StyleTranscriptPersona,
)
from backend.pipeline.llm import LLMError, chat_json
from backend.pipeline.style_guide import _split_speaker, parse_webvtt
from backend.qa_extraction import lines_to_sentences, normalize_question
from backend.transcripts import chunk_sentences, normalize_whitespace

VALID_OUTCOMES = frozenset({"landed", "not_landed", "unclear"})

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Indian mobiles (10 digits) and common international / formatted variants.
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,5}\)?[-.\s]?)?\d{5,}[-.\s]?\d{4,6}(?!\d)"
    r"|(?<!\d)\d{10}(?!\d)"
)

EXTRACT_SYSTEM = (
    "You extract AUDIENCE LISTENER turns from a Masters' Union AMA transcript chunk. "
    "Focus on non-Pratham speakers (parents, students, applicants, guests). "
    "Skip hosts/moderators when they only facilitate; keep them only if they ask as a listener.\n"
    "For each listener turn return:\n"
    "- self_description: who they said they are (role/context), PII-stripped "
    "(e.g. 'parent of Class 12 student from a tier-2 city'). Empty if unknown.\n"
    "- concern: short normalized concern (a few words).\n"
    "- question_verbatim: their words, PII-stripped — remove personal names, phone numbers, "
    "emails, exact marks/scores of named kids, exact family income; use placeholders like "
    "[NAME], [PHONE], [EMAIL], [SCORE], [INCOME].\n"
    "- reaction_after_answer: what they did next if visible, else ''.\n"
    "- outcome: 'landed' | 'not_landed' | 'unclear'. Heuristic: follow-up on same point, "
    "pushback, or rephrased repeat ⇒ not_landed; acknowledgement ('okay', 'that's clear', "
    "thanks) and moving on, or next speaker changes topic ⇒ landed; otherwise unclear.\n"
    "- answer_summary: SHORT summary of the answer given (calibration only, not script evidence).\n"
    "- confidence: 0-1.\n"
    "Return strict JSON: "
    '{"turns":[{"self_description":"...","concern":"...","question_verbatim":"...",'
    '"reaction_after_answer":"...","outcome":"landed|not_landed|unclear",'
    '"answer_summary":"...","confidence":0.0}]}.'
)

PROFILE_SYSTEM = (
    "You write a listener PERSONA PROFILE from real AMA audience turns. "
    "Describe who these listeners are, their top worries ranked, how they phrase things, "
    "what they compare Masters' Union to, what evidence seems to reassure them, and what "
    "makes them tune out.\n"
    "CRITICAL: record LISTENER ATTITUDES only. Do NOT state facts about Masters' Union as "
    "truths. Do not invent programme details, numbers, or claims.\n"
    "Hard cap: about 400 words.\n"
    'Return JSON only: {"profile": "..."}'
)

CALIBRATE_SYSTEM = (
    "You predict whether a real AMA listener was satisfied by the answer they received. "
    "Use the persona profile and similar past turns for context. "
    "Return strict JSON: "
    '{"predicted_outcome":"landed|not_landed","confidence":0.0,"rationale":"..."}.'
)


def scrub_pii(text: str) -> str:
    cleaned = EMAIL_RE.sub("[EMAIL]", text or "")
    cleaned = PHONE_RE.sub("[PHONE]", cleaned)
    return cleaned


def _is_pratham(speaker: str | None) -> bool:
    return bool(speaker and "pratham" in speaker.lower())


def _clamp_confidence(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score != score:
        return 0.0
    return max(0.0, min(1.0, score))


def _normalize_outcome(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in {"landed", "satisfied", "resolved"}:
        return "landed"
    if raw in {"not_landed", "unsatisfied", "unresolved", "failed"}:
        return "not_landed"
    return "unclear"


def parse_listener_turns_payload(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        items = raw.get("turns") or raw.get("items") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    turns: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        question = scrub_pii(normalize_whitespace(str(item.get("question_verbatim") or "")))
        if not question:
            continue
        turns.append(
            {
                "self_description": scrub_pii(
                    normalize_whitespace(str(item.get("self_description") or ""))
                )[:500],
                "concern": scrub_pii(normalize_whitespace(str(item.get("concern") or "")))[:500],
                "question_verbatim": question,
                "reaction_after_answer": scrub_pii(
                    normalize_whitespace(str(item.get("reaction_after_answer") or ""))
                ),
                "outcome": _normalize_outcome(item.get("outcome")),
                "answer_summary": scrub_pii(
                    normalize_whitespace(str(item.get("answer_summary") or ""))
                ),
                "confidence": _clamp_confidence(item.get("confidence", 0.6)),
            }
        )
    return turns


def listener_labeled_lines(raw_text: str) -> list[str]:
    """Return speaker:utterance lines; drop unlabeled cues (speaker unknown)."""
    kept: list[str] = []
    for line in parse_webvtt(raw_text or ""):
        speaker, utterance = _split_speaker(line)
        if speaker is None or not utterance:
            continue
        kept.append(f"{speaker}: {utterance}")
    return kept


def _chunk_has_listener(text: str) -> bool:
    for line in (text or "").splitlines():
        speaker, _utterance = _split_speaker(line)
        if speaker and not _is_pratham(speaker):
            return True
    return False


def extract_turns_from_transcript(
    raw_text: str,
    curate: Callable[[str], Any] | None = None,
) -> list[dict[str, Any]]:
    lines = listener_labeled_lines(raw_text)
    if not lines:
        return []
    sentences = lines_to_sentences(lines)
    chunks = chunk_sentences(sentences, target_words=220, overlap=40)
    curate_fn = curate or (
        lambda text: chat_json(
            [
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": f"Transcript chunk:\n{text}"},
            ],
            timeout=45.0,
            reasoning=False,
            max_tokens=1800,
        )
    )
    collected: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        chunk_text = chunk.get("text") or ""
        if not _chunk_has_listener(chunk_text):
            continue
        try:
            raw = curate_fn(chunk_text)
        except (LLMError, TypeError, ValueError, json.JSONDecodeError):
            continue
        # Chunks overlap, so the same turn can be extracted twice; keep the most confident.
        for turn in parse_listener_turns_payload(raw):
            key = normalize_question(turn["question_verbatim"])
            previous = collected.get(key)
            if previous is None or turn["confidence"] > previous["confidence"]:
                collected[key] = turn
    return list(collected.values())


def extract_listener_turns(db: Session, style_transcript_id: int) -> list[ListenerTurn]:
    transcript = db.get(StyleTranscript, style_transcript_id)
    if transcript is None:
        raise ValueError("Style transcript not found")
    parsed = extract_turns_from_transcript(transcript.raw_text or "")
    existing = (
        db.query(ListenerTurn)
        .filter(ListenerTurn.style_transcript_id == style_transcript_id)
        .all()
    )
    for row in existing:
        db.delete(row)
    db.flush()
    rows: list[ListenerTurn] = []
    for item in parsed:
        row = ListenerTurn(
            style_transcript_id=style_transcript_id,
            self_description=item["self_description"],
            concern=item["concern"],
            question_verbatim=item["question_verbatim"],
            reaction_after_answer=item["reaction_after_answer"],
            outcome=item["outcome"],
            answer_summary=item["answer_summary"],
            confidence=item["confidence"],
        )
        db.add(row)
        rows.append(row)
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def list_listener_turns(
    db: Session,
    *,
    style_transcript_id: int | None = None,
    persona_label: str = "",
    limit: int = 200,
) -> list[ListenerTurn]:
    query = db.query(ListenerTurn)
    if style_transcript_id is not None:
        query = query.filter(ListenerTurn.style_transcript_id == style_transcript_id)
    label = (persona_label or "").strip()
    if label:
        query = query.join(
            StyleTranscriptPersona,
            StyleTranscriptPersona.style_transcript_id == ListenerTurn.style_transcript_id,
        ).filter(StyleTranscriptPersona.persona_label == label)
    return query.order_by(ListenerTurn.id.asc()).limit(max(1, limit)).all()


def _append_source_ids(previous: str, transcript_ids: list[int]) -> str:
    parts = [part.strip() for part in (previous or "").split(",") if part.strip()]
    for transcript_id in transcript_ids:
        token = str(transcript_id)
        if token not in parts:
            parts.append(token)
    return ",".join(parts)[:500]


def latest_listener_profile_row(db: Session, persona_label: str) -> ListenerProfile | None:
    label = (persona_label or "").strip()
    if not label:
        return None
    return (
        db.query(ListenerProfile)
        .filter(ListenerProfile.persona_label == label)
        .order_by(ListenerProfile.version.desc())
        .first()
    )


def latest_listener_profile(db: Session, persona_label: str) -> str:
    row = latest_listener_profile_row(db, persona_label)
    if row is None:
        return ""
    text = getattr(row, "profile_text", None)
    return text if isinstance(text, str) else ""


def _turns_for_persona(db: Session, persona_label: str) -> list[ListenerTurn]:
    label = (persona_label or "").strip()
    if not label:
        return []
    return (
        db.query(ListenerTurn)
        .join(
            StyleTranscriptPersona,
            StyleTranscriptPersona.style_transcript_id == ListenerTurn.style_transcript_id,
        )
        .filter(StyleTranscriptPersona.persona_label == label)
        .order_by(ListenerTurn.id.asc())
        .all()
    )


def _format_turns_for_profile(turns: list[ListenerTurn], limit: int = 40) -> str:
    blocks: list[str] = []
    for index, turn in enumerate(turns[:limit], start=1):
        blocks.append(
            f"{index}. self={turn.self_description or '(unknown)'} | "
            f"concern={turn.concern or '(none)'} | "
            f"q={turn.question_verbatim} | "
            f"reaction={turn.reaction_after_answer or '(none)'} | "
            f"outcome={turn.outcome}"
        )
    return "\n".join(blocks)


def build_listener_profile(db: Session, persona_label: str) -> ListenerProfile:
    label = (persona_label or "").strip()
    if not label:
        raise ValueError("persona_label is required")
    turns = _turns_for_persona(db, label)
    current = latest_listener_profile_row(db, label)
    transcript_ids = sorted({turn.style_transcript_id for turn in turns})
    if not turns:
        empty = ListenerProfile(
            version=(current.version if current is not None else 0) + 1,
            persona_label=label,
            profile_text="",
            source_transcript_ids="",
        )
        db.add(empty)
        db.commit()
        db.refresh(empty)
        return empty

    payload = chat_json(
        [
            {"role": "system", "content": PROFILE_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"PERSONA LABEL: {label}\n\n"
                    f"LISTENER TURNS:\n{_format_turns_for_profile(turns)}"
                ),
            },
        ],
        timeout=90.0,
        reasoning=False,
        max_tokens=1200,
    )
    profile_text = scrub_pii(normalize_whitespace(str(payload.get("profile") or "")))
    if not profile_text:
        raise RuntimeError("Listener profile distillation returned empty text")
    if len(profile_text.split()) > 450:
        profile_text = " ".join(profile_text.split()[:400])

    row = ListenerProfile(
        version=(current.version if current is not None else 0) + 1,
        persona_label=label,
        profile_text=profile_text,
        source_transcript_ids=_append_source_ids("", transcript_ids),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def listener_examples_for_persona(
    db: Session,
    persona_label: str,
    *,
    limit: int = 5,
    exclude_ids: set[int] | None = None,
) -> list[ListenerTurn]:
    turns = _turns_for_persona(db, persona_label)
    skip = exclude_ids or set()
    preferred = [
        turn
        for turn in turns
        if turn.id not in skip and turn.outcome in {"landed", "not_landed"}
    ]
    pool = preferred or [turn for turn in turns if turn.id not in skip]
    return pool[: max(0, limit)]


def calibrate_simulator(
    db: Session,
    persona_label: str,
    *,
    limit: int = 30,
    predict: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    label = (persona_label or "").strip()
    if not label:
        raise ValueError("persona_label is required")
    profile_text = latest_listener_profile(db, label)
    labeled = [
        turn
        for turn in _turns_for_persona(db, label)
        if turn.outcome in {"landed", "not_landed"}
    ][: max(1, limit)]

    details: list[dict[str, Any]] = []
    confusion = {
        "landed_landed": 0,
        "landed_not_landed": 0,
        "not_landed_landed": 0,
        "not_landed_not_landed": 0,
    }
    correct = 0

    def default_predict(
        *,
        question: str,
        answer_summary: str,
        examples: list[ListenerTurn],
    ) -> dict[str, Any]:
        example_block = "\n".join(
            (
                f"- q={ex.question_verbatim} | reaction={ex.reaction_after_answer} | "
                f"outcome={ex.outcome}"
            )
            for ex in examples
        ) or "(none)"
        return chat_json(
            [
                {"role": "system", "content": CALIBRATE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"PERSONA PROFILE:\n{profile_text or '(none)'}\n\n"
                        f"FEW-SHOT EXAMPLES (exclude the item under test):\n{example_block}\n\n"
                        f"QUESTION:\n{question}\n\n"
                        f"ANSWER SUMMARY:\n{answer_summary or '(none)'}\n\n"
                        "Predict whether the listener was satisfied (landed) or not."
                    ),
                },
            ],
            timeout=90.0,
            reasoning=True,
            max_tokens=4000,
            model=_listener_model(),
        )

    predict_fn = predict or default_predict
    for turn in labeled:
        examples = listener_examples_for_persona(
            db, label, limit=4, exclude_ids={turn.id}
        )
        try:
            raw = predict_fn(
                question=turn.question_verbatim,
                answer_summary=turn.answer_summary,
                examples=examples,
            )
        except (LLMError, TypeError, ValueError, json.JSONDecodeError) as exc:
            details.append(
                {
                    "turn_id": turn.id,
                    "actual": turn.outcome,
                    "predicted": "error",
                    "error": str(exc),
                }
            )
            continue
        predicted = _normalize_outcome(
            raw.get("predicted_outcome") if isinstance(raw, dict) else "unclear"
        )
        if predicted not in {"landed", "not_landed"}:
            details.append(
                {"turn_id": turn.id, "actual": turn.outcome, "predicted": "error", "error": "unclear prediction"}
            )
            continue
        key = f"{turn.outcome}_{predicted}"
        if key in confusion:
            confusion[key] += 1
        match = predicted == turn.outcome
        if match:
            correct += 1
        details.append(
            {
                "turn_id": turn.id,
                "actual": turn.outcome,
                "predicted": predicted,
                "correct": match,
                "confidence": _clamp_confidence(
                    raw.get("confidence") if isinstance(raw, dict) else 0
                ),
                "rationale": str(raw.get("rationale") or "") if isinstance(raw, dict) else "",
            }
        )

    total = len([item for item in details if item.get("predicted") != "error"])
    return {
        "persona_label": label,
        "total": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 0.0,
        "confusion_matrix": confusion,
        "details": details,
        "grounded": bool(profile_text),
    }


def _listener_model() -> str | None:
    from backend.config import settings

    model = (settings.listener_model or "").strip()
    return model or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract listener turns from AMA transcripts")
    parser.add_argument(
        "--transcript-id",
        type=int,
        default=0,
        help="Process one style transcript id (default: all processed)",
    )
    args = parser.parse_args(argv)
    db = SessionLocal()
    try:
        query = db.query(StyleTranscript).filter(StyleTranscript.status == "processed")
        if args.transcript_id:
            query = query.filter(StyleTranscript.id == args.transcript_id)
        rows = query.order_by(StyleTranscript.id.asc()).all()
        for transcript in rows:
            turns = extract_listener_turns(db, transcript.id)
            print(f"transcript={transcript.id} turns={len(turns)}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
