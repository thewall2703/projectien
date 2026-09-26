from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy.orm import Session

from backend.models import (
    FounderQuote,
    Recipe,
    StyleTranscript,
    StyleTranscriptPersona,
    VoiceStyleGuide,
)
from backend.pipeline.llm import chat_json
from backend.transcripts import chunk_sentences, curate_chunk, is_verbatim, quote_hash

TRANSCRIPT_CHAR_BUDGET = 60_000
TIMESTAMP_RE = re.compile(r"-->")
CUE_TIME_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)
CUE_NUMBER_RE = re.compile(r"^\d+$")
SPEAKER_RE = re.compile(r"^([^:]{1,80}):\s*(.*)$")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
PRATHAM_RUN_MERGE_WORDS = 40

DISTILL_SYSTEM = (
    "You are distilling how Pratham Mittal (founder, Masters' Union) structures and "
    "delivers live sessions, to guide scripts delivered by Masters' Union EMPLOYEES. "
    "Pratham is the style source, never the implied speaker.\n\n"
    "Produce a MERGED guide from CURRENT GUIDE and NEW TRANSCRIPT. Preserve useful "
    "transferable patterns, but actively remove old rules that confuse the employee with "
    "Pratham, treat transcript facts as authoritative, or overfit one AMA format.\n\n"
    "Write these sections:\n\n"
    "ALWAYS TRANSFERABLE\n"
    "- Spoken rhythm (short punchy lines mixed with longer ones), plain vocabulary, "
    "directness, concrete-to-point movement, tag questions, asides/self-interruptions, "
    "dry dares to the listener, honest objection handling, and avoiding corporate gloss.\n\n"
    "WHEN USEFUL\n"
    "- Optional devices such as humour, rhetorical questions, light Hindi/Hinglish "
    "(roughly one natural touch per ~150 spoken words — never whole Hindi sentences), "
    "and sparingly used direct address ('boss', 'guys', 'yaar'). Describe when they help; "
    "tone them down for corporate HR or formal parent audiences; never require them everywhere.\n\n"
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


class StyleTranscriptPersonaError(Exception):
    """Raised when persona labels are missing or invalid."""


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


def is_pratham_mittal_speaker(label: str) -> bool:
    """True only for the founder (needs both 'pratham' and 'mittal')."""
    tokens = {part for part in re.split(r"[^a-z0-9]+", (label or "").lower()) if part}
    return "pratham" in tokens and "mittal" in tokens


def _cue_seconds(groups: tuple[str, ...]) -> float:
    hours, minutes, seconds, millis = (int(part) for part in groups)
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def parse_webvtt_cues(text: str) -> list[dict[str, Any]]:
    """Parse VTT/plain text into cues with optional times and speaker labels."""
    raw = text or ""
    probe = raw.lstrip("\ufeff \t\r\n")
    if not probe.upper().startswith("WEBVTT"):
        return [
            {"start_sec": None, "end_sec": None, "speaker": None, "text": line.strip()}
            for line in raw.splitlines()
            if line.strip()
        ]

    cues: list[dict[str, Any]] = []
    start_sec: float | None = None
    end_sec: float | None = None
    for line in probe.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if CUE_NUMBER_RE.fullmatch(stripped):
            continue
        match = CUE_TIME_RE.match(stripped)
        if match:
            start_sec = _cue_seconds(match.groups()[:4])
            end_sec = _cue_seconds(match.groups()[4:])
            continue
        if TIMESTAMP_RE.search(stripped):
            start_sec = None
            end_sec = None
            continue
        upper = stripped.upper()
        if upper.startswith("WEBVTT") or upper.startswith("NOTE") or upper.startswith("STYLE") or upper.startswith(
            "REGION"
        ):
            continue
        speaker, utterance = _split_speaker(stripped)
        if not utterance:
            continue
        cues.append(
            {
                "start_sec": start_sec,
                "end_sec": end_sec,
                "speaker": speaker,
                "text": utterance,
            }
        )
        start_sec = None
        end_sec = None
    return cues


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
        if speaker and is_pratham_mittal_speaker(speaker) and utterance:
            utterances.append(utterance)
    return "\n".join(utterances)


def pratham_speech_runs(
    lines: list[str],
    *,
    min_merge_words: int = PRATHAM_RUN_MERGE_WORDS,
) -> list[str]:
    """Contiguous Pratham Mittal runs; tiny runs may merge with a neighbour."""
    runs: list[str] = []
    current: list[str] = []
    for line in lines:
        speaker, utterance = _split_speaker(line)
        if speaker and is_pratham_mittal_speaker(speaker) and utterance:
            current.append(utterance)
            continue
        if current:
            runs.append("\n".join(current))
            current = []
    if current:
        runs.append("\n".join(current))
    if not runs:
        return []

    merged: list[str] = [runs[0]]
    for run in runs[1:]:
        if len(merged[-1].split()) < min_merge_words or len(run.split()) < min_merge_words:
            merged[-1] = f"{merged[-1]}\n{run}"
        else:
            merged.append(run)
    return merged


def normalize_persona_labels(labels: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in labels or []:
        label = (raw or "").strip()
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(label)
    return cleaned


def known_persona_labels(db: Session) -> set[str]:
    rows = db.query(Recipe.audience_label).all()
    return {(row[0] or "").strip() for row in rows if (row[0] or "").strip()}


def validate_persona_labels(db: Session, labels: list[str] | None) -> list[str]:
    cleaned = normalize_persona_labels(labels)
    if not cleaned:
        raise StyleTranscriptPersonaError("Select at least one persona")
    known = known_persona_labels(db)
    if not known:
        raise StyleTranscriptPersonaError("No personas are configured yet")
    known_lookup = {label.casefold(): label for label in known}
    resolved: list[str] = []
    unknown: list[str] = []
    for label in cleaned:
        match = known_lookup.get(label.casefold())
        if match is None:
            unknown.append(label)
        else:
            resolved.append(match)
    if unknown:
        raise StyleTranscriptPersonaError(
            "Unknown persona(s): " + ", ".join(sorted(unknown))
        )
    return normalize_persona_labels(resolved)


def transcript_persona_labels(db: Session, transcript_id: int) -> list[str]:
    rows = (
        db.query(StyleTranscriptPersona.persona_label)
        .filter(StyleTranscriptPersona.style_transcript_id == transcript_id)
        .order_by(StyleTranscriptPersona.persona_label)
        .all()
    )
    return [row[0] for row in rows if row[0]]


def personas_for_transcripts(db: Session, transcript_ids: list[int]) -> dict[int, list[str]]:
    if not transcript_ids:
        return {}
    rows = (
        db.query(StyleTranscriptPersona)
        .filter(StyleTranscriptPersona.style_transcript_id.in_(transcript_ids))
        .order_by(StyleTranscriptPersona.persona_label)
        .all()
    )
    mapping: dict[int, list[str]] = {transcript_id: [] for transcript_id in transcript_ids}
    for row in rows:
        mapping.setdefault(row.style_transcript_id, []).append(row.persona_label)
    return mapping


def latest_style_guide_row(db: Session, persona_label: str = "") -> VoiceStyleGuide | None:
    label = (persona_label or "").strip()
    query = db.query(VoiceStyleGuide)
    if label:
        # Persona-scoped lookup never falls back to the legacy global guide.
        query = query.filter(VoiceStyleGuide.persona_label == label)
    else:
        query = query.filter(
            (VoiceStyleGuide.persona_label == "") | (VoiceStyleGuide.persona_label.is_(None))
        )
    return query.order_by(VoiceStyleGuide.version.desc()).first()


def latest_style_guide(db: Session, persona_label: str = "") -> str:
    row = latest_style_guide_row(db, persona_label=persona_label)
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


def _set_transcript_personas(db: Session, transcript_id: int, labels: list[str]) -> list[str]:
    cleaned = normalize_persona_labels(labels)
    existing = (
        db.query(StyleTranscriptPersona)
        .filter(StyleTranscriptPersona.style_transcript_id == transcript_id)
        .all()
    )
    existing_by_key = {(row.persona_label or "").casefold(): row for row in existing}
    keep_keys = {label.casefold() for label in cleaned}
    for key, row in existing_by_key.items():
        if key not in keep_keys:
            db.delete(row)
    for label in cleaned:
        if label.casefold() not in existing_by_key:
            db.add(
                StyleTranscriptPersona(
                    style_transcript_id=transcript_id,
                    persona_label=label,
                )
            )
    return cleaned


def _transcripts_for_persona(db: Session, persona_label: str) -> list[StyleTranscript]:
    label = (persona_label or "").strip()
    if not label:
        return []
    return (
        db.query(StyleTranscript)
        .join(
            StyleTranscriptPersona,
            StyleTranscriptPersona.style_transcript_id == StyleTranscript.id,
        )
        .filter(
            StyleTranscriptPersona.persona_label == label,
            StyleTranscript.status == "processed",
        )
        .order_by(StyleTranscript.id.asc())
        .all()
    )


def _rebuild_persona_guide(db: Session, persona_label: str) -> VoiceStyleGuide | None:
    label = (persona_label or "").strip()
    if not label:
        return None
    transcripts = _transcripts_for_persona(db, label)
    current = latest_style_guide_row(db, persona_label=label)
    if not transcripts:
        # Keep prior versions for history, but leave an empty latest marker so
        # generation no longer pulls an orphaned guide.
        empty = VoiceStyleGuide(
            version=(current.version if current is not None else 0) + 1,
            persona_label=label,
            guide_text="",
            source_transcript_ids="",
        )
        db.add(empty)
        db.flush()
        return empty

    guide_text = ""
    source_ids = ""
    for transcript in transcripts:
        lines = parse_webvtt(transcript.raw_text or "")
        body = "\n".join(lines) if lines else (transcript.raw_text or "")
        guide_text = distill_style(body, guide_text)
        source_ids = _append_source_ids(source_ids, transcript.id)

    guide = VoiceStyleGuide(
        version=(current.version if current is not None else 0) + 1,
        persona_label=label,
        guide_text=guide_text,
        source_transcript_ids=source_ids,
    )
    db.add(guide)
    db.flush()
    return guide


def _enrich_persona_guide(
    db: Session,
    *,
    persona_label: str,
    transcript: StyleTranscript,
    body: str,
) -> VoiceStyleGuide:
    label = (persona_label or "").strip()
    current = latest_style_guide_row(db, persona_label=label)
    current_guide = (
        current.guide_text
        if current is not None and isinstance(getattr(current, "guide_text", None), str)
        else ""
    )
    guide_text = distill_style(body, current_guide)
    guide = VoiceStyleGuide(
        version=(current.version if current is not None else 0) + 1,
        persona_label=label,
        guide_text=guide_text,
        source_transcript_ids=_append_source_ids(
            current.source_transcript_ids if current is not None else "",
            transcript.id,
        ),
    )
    db.add(guide)
    db.flush()
    return guide


def _harvest_quotes(
    db: Session,
    *,
    source_name: str,
    source_file_id: str,
    source_style_transcript_id: int,
    pratham_text: str,
    source_url: str = "",
) -> dict[str, int]:
    kept = 0
    skipped = 0
    if not pratham_text.strip():
        return {"quotes_kept": 0, "quotes_skipped": 0}
    sentences = [
        {"text": sentence, "start": 0.0, "end": 0.0}
        for line in pratham_text.splitlines()
        for sentence in SENTENCE_SPLIT_RE.split(line)
        if sentence.strip()
    ]
    chunks = chunk_sentences(sentences)
    pending: set[str] = set()
    for chunk in chunks:
        try:
            snippets = curate_chunk(chunk["text"])
        except Exception:
            continue
        if not snippets:
            continue
        for snippet in snippets:
            digest = quote_hash(source_file_id, snippet["text"])
            # Overlapping chunks can yield the same snippet before it is flushed.
            if digest in pending:
                skipped += 1
                continue
            existing = db.query(FounderQuote).filter(FounderQuote.text_hash == digest).first()
            if existing:
                if not getattr(existing, "source_style_transcript_id", 0):
                    existing.source_style_transcript_id = source_style_transcript_id
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
                    source_style_transcript_id=source_style_transcript_id,
                    source_url=source_url,
                    start_sec=float(chunk["start"]),
                    end_sec=float(chunk["end"]),
                    speaker="Pratham Mittal",
                    text_hash=digest,
                    status="approved",
                )
            )
            pending.add(digest)
            kept += 1
    return {"quotes_kept": kept, "quotes_skipped": skipped}


def ingest_style_transcript(
    db: Session,
    name: str,
    text: str,
    persona_labels: list[str] | None = None,
) -> dict[str, Any]:
    # Preserve the legacy one-step helper for tests and internal callers.
    validate_persona_labels(db, persona_labels)
    transcript = store_style_transcript(db, name, text)
    return index_style_transcript(db, transcript.id, persona_labels)


def store_style_transcript(
    db: Session,
    name: str,
    text: str,
    source_url: str = "",
) -> StyleTranscript:
    """Store a transcript in the library without indexing it."""
    title = (name or "").strip()
    raw = text or ""
    url = (source_url or "").strip()
    if not title or not raw.strip():
        raise ValueError("Name and transcript text are required")
    if url and not url.lower().startswith(("http://", "https://")):
        raise ValueError("Video link must start with http:// or https://")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if db.query(StyleTranscript).filter(StyleTranscript.text_hash == digest).first():
        raise DuplicateStyleTranscriptError("This transcript has already been uploaded")

    try:
        transcript = StyleTranscript(
            name=title,
            raw_text=raw,
            text_hash=digest,
            source_url=url,
            status="uploaded",
        )
        db.add(transcript)
        db.commit()
        db.refresh(transcript)
    except Exception:
        db.rollback()
        raise
    try:
        from backend.transcript_search import SOURCE_STYLE, safe_upsert_source

        safe_upsert_source(
            db=db,
            source_type=SOURCE_STYLE,
            source_id=str(transcript.id),
            source_name=transcript.name,
            text=transcript.raw_text or "",
        )
    except Exception:
        pass
    return transcript


def index_style_transcript(
    db: Session,
    transcript_id: int,
    persona_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Index one stored transcript for the selected personas."""
    transcript = db.get(StyleTranscript, transcript_id)
    if transcript is None:
        raise ValueError("Style transcript not found")
    if transcript.status == "processed":
        raise ValueError(f"“{transcript.name}” has already been indexed")
    labels = validate_persona_labels(db, persona_labels)
    lines = parse_webvtt(transcript.raw_text or "")
    body = "\n".join(lines) if lines else (transcript.raw_text or "")

    transcript.status = "indexing"
    db.commit()
    try:
        transcript = db.get(StyleTranscript, transcript_id)
        if transcript is None:
            raise ValueError("Style transcript not found")
        _set_transcript_personas(db, transcript.id, labels)

        guides_updated: list[str] = []
        max_version = 0
        for label in labels:
            guide = _enrich_persona_guide(
                db,
                persona_label=label,
                transcript=transcript,
                body=body,
            )
            guides_updated.append(label)
            max_version = max(max_version, guide.version)

        quotes = _harvest_quotes(
            db,
            source_name=transcript.name,
            source_file_id=f"style-{transcript.id}",
            source_style_transcript_id=transcript.id,
            pratham_text=pratham_lines(lines),
            source_url=getattr(transcript, "source_url", "") or "",
        )
        transcript.status = "processed"
        db.commit()
        db.refresh(transcript)
    except Exception:
        db.rollback()
        failed = db.get(StyleTranscript, transcript_id)
        if failed is not None:
            failed.status = "failed"
            db.commit()
        raise
    try:
        from backend.transcript_search import SOURCE_STYLE, safe_upsert_source

        safe_upsert_source(
            db=db,
            source_type=SOURCE_STYLE,
            source_id=str(transcript.id),
            source_name=transcript.name,
            text=transcript.raw_text or "",
        )
    except Exception:
        pass
    return {
        "guide_version": max_version,
        "quotes_kept": quotes["quotes_kept"],
        "quotes_skipped": quotes["quotes_skipped"],
        "transcript_id": transcript.id,
        "persona_labels": labels,
        "guides_updated": guides_updated,
    }


def update_style_transcript_personas(
    db: Session,
    transcript_id: int,
    persona_labels: list[str] | None,
) -> dict[str, Any]:
    transcript = db.get(StyleTranscript, transcript_id)
    if transcript is None:
        raise StyleTranscriptPersonaError("Style transcript not found")
    labels = validate_persona_labels(db, persona_labels)
    previous = set(transcript_persona_labels(db, transcript_id))
    next_labels = set(labels)
    affected = sorted(previous | next_labels)
    try:
        _set_transcript_personas(db, transcript_id, labels)
        db.flush()
        for label in affected:
            _rebuild_persona_guide(db, label)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "transcript_id": transcript_id,
        "persona_labels": labels,
        "guides_updated": affected,
    }


def save_style_guide(db: Session, guide_text: str, persona_label: str) -> VoiceStyleGuide:
    label = (persona_label or "").strip()
    if not label:
        raise StyleTranscriptPersonaError("persona_label is required")
    known = known_persona_labels(db)
    if label not in known:
        # Allow exact case-insensitive match against recipe labels.
        match = next((item for item in known if item.casefold() == label.casefold()), None)
        if match is None:
            raise StyleTranscriptPersonaError(f"Unknown persona: {label}")
        label = match
    current = latest_style_guide_row(db, persona_label=label)
    item = VoiceStyleGuide(
        version=(current.version if current is not None else 0) + 1,
        persona_label=label,
        guide_text=guide_text,
        source_transcript_ids=current.source_transcript_ids if current is not None else "",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item
