from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy.orm import Session

from backend.models import FounderQuote, StyleTranscript, VoiceStyleGuide
from backend.pipeline.llm import chat_json
from backend.transcripts import chunk_sentences, curate_chunk, is_verbatim, quote_hash

TRANSCRIPT_CHAR_BUDGET = 60_000
TIMESTAMP_RE = re.compile(r"-->")
CUE_NUMBER_RE = re.compile(r"^\d+$")
SPEAKER_RE = re.compile(r"^([^:]{1,80}):\s*(.*)$")

DISTILL_SYSTEM = (
    "You are distilling how Pratham Mittal (founder, Masters' Union) structures and "
    "delivers live sessions, to guide scripts delivered by Masters' Union EMPLOYEES. "
    "Pratham is the style source, never the implied speaker.\n\n"
    "Produce a MERGED guide from CURRENT GUIDE and NEW TRANSCRIPT. Preserve useful "
    "transferable patterns, but actively remove old rules that confuse the employee with "
    "Pratham, treat transcript facts as authoritative, or overfit one AMA format.\n\n"
    "Write these sections:\n\n"
    "ALWAYS TRANSFERABLE\n"
    "- Spoken rhythm, plain vocabulary, directness, concrete-to-point movement, honest "
    "objection handling, and avoiding corporate gloss.\n\n"
    "WHEN USEFUL\n"
    "- Optional devices such as humour, rhetorical questions, occasional Hindi, and "
    "addressing a listener by name. Describe when they help; never require them everywhere.\n\n"
    "FORMAT-SPECIFIC\n"
    "- Session segmentation, named parts, Q&A mechanics, audience participation, prizes, "
    "gamification, and question-quality frameworks belong only in compatible long, "
    "interactive formats. They are not requirements for ordinary pitches.\n\n"
    "IDENTITY AND SOURCE BOUNDARIES\n"
    "- The employee may use 'we' for Masters' Union and must use third-person attribution "
    "for founder-specific material. Never instruct an employee to claim Pratham's memories, "
    "education, relationships, actions, or achievements as their own.\n"
    "- Transcript anecdotes, numbers, rankings, programme details, and biographical claims "
    "are not verified facts and must never enter the guide as factual material. The transcript "
    "shows delivery style only; script facts come from separate approved sources.\n\n"
    "Every rule must be a reusable pattern or instruction. NEVER include verbatim sentences "
    "or close paraphrases for a script to copy.\n\n"
    "Hard cap: about 700 words.\n"
    'Return JSON only: {"guide": "..."}'
)


class DuplicateStyleTranscriptError(Exception):
    """Raised when a transcript with the same hash already exists."""


def _split_speaker(line: str) -> tuple[str | None, str]:
    stripped = (line or "").strip()
    if not stripped:
        return None, ""
    match = SPEAKER_RE.match(stripped)
    if not match:
        return None, stripped
    speaker = match.group(1).strip()
    utterance = match.group(2).strip()
    if not speaker or not utterance:
        return None, stripped
    return speaker, utterance


def parse_webvtt(text: str) -> list[str]:
    raw = text or ""
    probe = raw.lstrip("\ufeff \t\r\n")
    if not probe.upper().startswith("WEBVTT"):
        return [line.strip() for line in raw.splitlines() if line.strip()]

    cues: list[tuple[str | None, str]] = []
    for line in probe.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if CUE_NUMBER_RE.fullmatch(stripped):
            continue
        if TIMESTAMP_RE.search(stripped):
            continue
        upper = stripped.upper()
        if upper.startswith("WEBVTT") or upper.startswith("NOTE") or upper.startswith("STYLE") or upper.startswith(
            "REGION"
        ):
            continue
        cues.append(_split_speaker(stripped))

    merged: list[str] = []
    last_speaker: str | None = None
    last_key: str | None = None
    parts: list[str] = []

    def flush() -> None:
        if not parts:
            return
        utterance = " ".join(parts)
        if last_speaker:
            merged.append(f"{last_speaker}: {utterance}")
        else:
            merged.append(utterance)

    for speaker, utterance in cues:
        if not utterance:
            continue
        key = speaker.lower() if speaker else ""
        if parts and key == last_key:
            parts.append(utterance)
            continue
        flush()
        last_speaker = speaker
        last_key = key
        parts = [utterance]
    flush()
    return merged


def pratham_lines(lines: list[str]) -> str:
    utterances: list[str] = []
    for line in lines:
        speaker, utterance = _split_speaker(line)
        if speaker and "pratham" in speaker.lower() and utterance:
            utterances.append(utterance)
    return "\n".join(utterances)


def latest_style_guide_row(db: Session) -> VoiceStyleGuide | None:
    return db.query(VoiceStyleGuide).order_by(VoiceStyleGuide.version.desc()).first()


def latest_style_guide(db: Session) -> str:
    row = latest_style_guide_row(db)
    if row is None:
        return ""
    text = getattr(row, "guide_text", None)
    return text if isinstance(text, str) else ""


def distill_style(transcript_text: str, current_guide: str) -> str:
    excerpt = (transcript_text or "")[:TRANSCRIPT_CHAR_BUDGET]
    payload = chat_json(
        [
            {"role": "system", "content": DISTILL_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"CURRENT GUIDE:\n{current_guide or '(empty)'}\n\n"
                    f"NEW TRANSCRIPT:\n{excerpt}"
                ),
            },
        ]
    )
    guide = str(payload.get("guide") or "").strip()
    if not guide:
        raise RuntimeError("Style distillation returned an empty guide")
    return guide


def _append_source_ids(previous: str, transcript_id: int) -> str:
    parts = [part.strip() for part in (previous or "").split(",") if part.strip()]
    token = str(transcript_id)
    if token not in parts:
        parts.append(token)
    joined = ",".join(parts)
    return joined[:500]


def _harvest_quotes(
    db: Session,
    *,
    source_name: str,
    source_file_id: str,
    pratham_text: str,
) -> dict[str, int]:
    kept = 0
    skipped = 0
    if not pratham_text.strip():
        return {"quotes_kept": 0, "quotes_skipped": 0}
    sentences = [
        {"text": line, "start": 0.0, "end": 0.0}
        for line in pratham_text.splitlines()
        if line.strip()
    ]
    chunks = chunk_sentences(sentences)
    for chunk in chunks:
        try:
            snippets = curate_chunk(chunk["text"])
        except Exception:
            continue
        if not snippets:
            continue
        for snippet in snippets:
            digest = quote_hash(source_file_id, snippet["text"])
            existing = db.query(FounderQuote).filter(FounderQuote.text_hash == digest).first()
            if existing:
                skipped += 1
                continue
            db.add(
                FounderQuote(
                    text=snippet["text"],
                    verbatim=is_verbatim(snippet["text"], pratham_text or chunk["text"]),
                    topic=snippet["topic"],
                    module_ids=snippet["module_ids"],
                    source_name=source_name,
                    source_file_id=source_file_id,
                    source_url="",
                    start_sec=float(chunk["start"]),
                    end_sec=float(chunk["end"]),
                    speaker="Pratham Mittal",
                    text_hash=digest,
                    status="approved",
                )
            )
            kept += 1
    return {"quotes_kept": kept, "quotes_skipped": skipped}


def ingest_style_transcript(db: Session, name: str, text: str) -> dict[str, Any]:
    title = (name or "").strip()
    raw = text or ""
    if not title or not raw.strip():
        raise ValueError("Name and transcript text are required")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if db.query(StyleTranscript).filter(StyleTranscript.text_hash == digest).first():
        raise DuplicateStyleTranscriptError("This transcript has already been uploaded")

    lines = parse_webvtt(raw)
    current_row = latest_style_guide_row(db)
    current_guide = current_row.guide_text if current_row is not None and isinstance(
        getattr(current_row, "guide_text", None), str
    ) else ""
    guide_text = distill_style("\n".join(lines) if lines else raw, current_guide)

    try:
        transcript = StyleTranscript(
            name=title,
            raw_text=raw,
            text_hash=digest,
            status="processed",
        )
        db.add(transcript)
        db.flush()

        next_version = (current_row.version if current_row is not None else 0) + 1
        previous_ids = current_row.source_transcript_ids if current_row is not None else ""
        guide = VoiceStyleGuide(
            version=next_version,
            guide_text=guide_text,
            source_transcript_ids=_append_source_ids(previous_ids, transcript.id),
        )
        db.add(guide)

        quotes = _harvest_quotes(
            db,
            source_name=title,
            source_file_id=f"style-{transcript.id}",
            pratham_text=pratham_lines(lines),
        )
        db.commit()
        db.refresh(transcript)
        db.refresh(guide)
    except Exception:
        db.rollback()
        raise
    return {
        "guide_version": guide.version,
        "quotes_kept": quotes["quotes_kept"],
        "quotes_skipped": quotes["quotes_skipped"],
        "transcript_id": transcript.id,
    }


def save_style_guide(db: Session, guide_text: str) -> VoiceStyleGuide:
    current = latest_style_guide_row(db)
    item = VoiceStyleGuide(
        version=(current.version if current is not None else 0) + 1,
        guide_text=guide_text,
        source_transcript_ids=current.source_transcript_ids if current is not None else "",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item
