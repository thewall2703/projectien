from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from backend.models import Job, Objection, QaCandidate, QaExtractionRun, StyleTranscript, utc_now
from backend.pipeline.llm import LLMError, chat_json
from backend.pipeline.style_guide import parse_webvtt
from backend.transcripts import chunk_sentences, normalize_whitespace

JOB_QA_EXTRACT = "qa_extract"
ACTIVE_JOB_STATUSES = {"queued", "running"}
StageCallback = Callable[[str], None]

EXTRACT_SYSTEM = (
    "You extract Masters' Union AMA question-and-answer pairs from a transcript chunk. "
    "Only keep explicit audience questions with a substantive answer from the host/founder "
    "(usually Pratham) or MU staff. Skip logistics, greetings, incomplete exchanges, and "
    "rhetorical questions without a real answer.\n"
    "Return strict JSON: "
    '{"pairs":[{"question_verbatim":"...","answer_verbatim":"...","proposed_question":"...",'
    '"proposed_who_asks":"...","proposed_move":"...","proposed_answer":"...","confidence":0.0,'
    '"keep":true}]}. '
    "question_verbatim and answer_verbatim must stay faithful to the transcript (founder wording OK). "
    "proposed_* fields are clean pitch-library wording for Masters' Union EMPLOYEES to use. "
    "proposed_answer MUST be collective employee voice: prefer 'we' / 'our' / 'at Masters\\' Union'. "
    "Never write I/me/my/mine/I'm as the speaker. Never impersonate Pratham. "
    "If a founder-specific point is needed, name Pratham in the third person "
    "('Pratham\\'s goal is…', 'Pratham has said…') — do not keep his first-person phrasing. "
    "proposed_move is the response strategy in one short sentence, also without his/he founder framing "
    "unless naming Pratham explicitly. "
    "confidence is 0-1 for how complete the exchange is."
)

REWRITE_ANSWER_SYSTEM = (
    "Rewrite this Masters' Union Q&A answer so a Masters' Union EMPLOYEE can say it aloud. "
    "Keep the substance. Do not invent facts.\n"
    "Hard voice rules:\n"
    "- Collective institutional voice: use we/our/us/at Masters' Union.\n"
    "- Never use speaker first-person singular: no I, me, my, mine, I'm, I've, I'll.\n"
    "- Never impersonate Pratham Mittal. If a founder-specific ambition or biography point is required, "
    "rewrite it in third person about Pratham or as an institutional 'we' goal.\n"
    "- Example: 'My goal is to build…' → 'Our goal is to build…' or "
    "'Pratham\\'s long-term goal is to build…'\n"
    "Prefer 'our' for shared institutional goals unless the point is uniquely personal to Pratham.\n"
    "Return strict JSON: {\"proposed_answer\": \"...\"}."
)

MATCH_SYSTEM = (
    "You match extracted AMA questions to an existing objection/Q&A library. "
    "For each candidate, pick the best matching existing objection id if it is the same "
    "underlying question, otherwise leave matched_objection_id as 0. "
    "Return strict JSON: "
    '{"matches":[{"candidate_index":0,"matched_objection_id":0,"confidence":0.0,'
    '"match_type":"new|enrich","rationale":"..."}]}.'
)

PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
SPACE_RE = re.compile(r"\s+")
FIRST_PERSON_RE = re.compile(
    r"\b(i|i'?m|i'?ve|i'?ll|i'?d|me|my|mine|myself)\b",
    re.IGNORECASE,
)
FOUNDER_HIS_RE = re.compile(r"\b(his|he'?s|he)\b", re.IGNORECASE)


class QaExtractionError(RuntimeError):
    """Raised when a Q&A extraction run cannot proceed."""


def normalize_question(text: str) -> str:
    cleaned = normalize_whitespace(text).lower()
    cleaned = PUNCT_RE.sub(" ", cleaned)
    return SPACE_RE.sub(" ", cleaned).strip()


def candidate_fingerprint(question: str) -> str:
    return hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()


def _clamp_confidence(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score != score:
        return 0.0
    return max(0.0, min(1.0, score))


def lines_to_sentences(lines: list[str]) -> list[dict[str, Any]]:
    sentences: list[dict[str, Any]] = []
    cursor = 0.0
    for line in lines:
        text = normalize_whitespace(line)
        if not text:
            continue
        # Approximate timing so chunk_sentences can overlap; AMA WebVTT usually
        # drops timestamps before this stage.
        words = max(1, len(text.split()))
        duration = max(2.0, words * 0.35)
        sentences.append({"text": text, "start": cursor, "end": cursor + duration})
        cursor += duration
    return sentences


def parse_extract_payload(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        items = raw.get("pairs") or raw.get("items") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    pairs: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        keep = item.get("keep", True)
        if keep is False or str(keep).lower() in {"false", "0", "no"}:
            continue
        question_verbatim = normalize_whitespace(str(item.get("question_verbatim") or ""))
        answer_verbatim = normalize_whitespace(str(item.get("answer_verbatim") or ""))
        proposed_question = normalize_whitespace(
            str(item.get("proposed_question") or question_verbatim)
        )
        proposed_answer = normalize_whitespace(str(item.get("proposed_answer") or answer_verbatim))
        if not proposed_question or not proposed_answer:
            continue
        pairs.append(
            {
                "question_verbatim": question_verbatim or proposed_question,
                "answer_verbatim": answer_verbatim or proposed_answer,
                "proposed_question": proposed_question,
                "proposed_who_asks": normalize_whitespace(str(item.get("proposed_who_asks") or ""))[:255],
                "proposed_move": normalize_whitespace(str(item.get("proposed_move") or "")),
                "proposed_answer": proposed_answer,
                "confidence": _clamp_confidence(item.get("confidence", 0.7)),
            }
        )
    return pairs


def has_speaker_first_person(text: str) -> bool:
    return bool(FIRST_PERSON_RE.search(text or ""))


def rewrite_answer_as_employee(
    question: str,
    answer: str,
    *,
    rewrite: Callable[[str, str], Any] | None = None,
) -> str:
    source = normalize_whitespace(answer)
    if not source:
        return ""

    def default_rewrite(q: str, a: str) -> Any:
        return chat_json(
            [
                {"role": "system", "content": REWRITE_ANSWER_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Question:\n{q}\n\nAnswer to rewrite:\n{a}\n\n"
                        "Remember: replace every I/me/my with we/our (or third-person Pratham if uniquely personal)."
                    ),
                },
            ],
            timeout=45.0,
            reasoning=False,
            max_tokens=900,
        )

    rewrite_fn = rewrite or default_rewrite
    try:
        raw = rewrite_fn(question, source)
    except (LLMError, TypeError, ValueError, json.JSONDecodeError):
        return source
    if isinstance(raw, dict):
        rewritten = normalize_whitespace(str(raw.get("proposed_answer") or ""))
        if rewritten and has_speaker_first_person(rewritten):
            # One forced retry if the model left I/my in place.
            try:
                raw = rewrite_fn(
                    question,
                    rewritten + "\n\n[Fix required: remove all I/me/my; use we/our.]",
                )
                if isinstance(raw, dict):
                    again = normalize_whitespace(str(raw.get("proposed_answer") or ""))
                    if again:
                        rewritten = again
            except (LLMError, TypeError, ValueError, json.JSONDecodeError):
                pass
        return rewritten or source
    return source


def normalize_employee_move(move: str) -> str:
    text = normalize_whitespace(move)
    if not text:
        return ""
    # Light deterministic cleanup for common founder framing in move lines.
    text = re.sub(r"\bShares his\b", "Share our", text, flags=re.IGNORECASE)
    text = re.sub(r"\bShare his\b", "Share our", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhis singular\b", "our shared", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhis long-term\b", "our long-term", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhis goal\b", "our goal", text, flags=re.IGNORECASE)
    return text


def rewrite_pending_candidates_as_employee(
    db: Session,
    *,
    rewrite: Callable[[str, str], Any] | None = None,
    limit: int | None = None,
    only_first_person: bool = False,
) -> int:
    query = (
        db.query(QaCandidate)
        .filter(QaCandidate.status == "pending")
        .order_by(QaCandidate.id.asc())
    )
    if limit is not None:
        query = query.limit(limit)
    rows = query.all()
    updated = 0
    for row in rows:
        answer = row.proposed_answer or row.answer_verbatim
        if only_first_person and not (
            has_speaker_first_person(answer) or has_speaker_first_person(row.proposed_move or "")
        ):
            continue
        rewritten = rewrite_answer_as_employee(
            row.proposed_question or row.question_verbatim,
            answer,
            rewrite=rewrite,
        )
        new_move = normalize_employee_move(row.proposed_move or "")
        changed = False
        if rewritten and rewritten != row.proposed_answer:
            row.proposed_answer = rewritten
            changed = True
        if new_move and new_move != row.proposed_move:
            row.proposed_move = new_move
            changed = True
        if changed:
            updated += 1
    if updated:
        db.commit()
    return updated


def merge_extracted_pairs(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_fingerprint: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        key = candidate_fingerprint(pair["proposed_question"])
        existing = by_fingerprint.get(key)
        if existing is None:
            row = dict(pair)
            row["fingerprint"] = key
            by_fingerprint[key] = row
            merged.append(row)
            continue
        if pair["confidence"] > existing["confidence"]:
            existing.update(pair)
            existing["fingerprint"] = key
        elif len(pair["proposed_answer"]) > len(existing["proposed_answer"]):
            existing["proposed_answer"] = pair["proposed_answer"]
            existing["answer_verbatim"] = pair["answer_verbatim"]
    return merged


def extract_pairs_from_transcript(raw_text: str, curate: Callable[[str], Any] | None = None) -> list[dict[str, Any]]:
    lines = parse_webvtt(raw_text)
    sentences = lines_to_sentences(lines)
    chunks = chunk_sentences(sentences, target_words=220, overlap=40)
    curate_fn = curate or (lambda text: chat_json(
        [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": f"Transcript chunk:\n{text}"},
        ],
        timeout=45.0,
        reasoning=False,
        max_tokens=1800,
    ))
    collected: list[dict[str, Any]] = []
    for chunk in chunks:
        try:
            raw = curate_fn(chunk["text"])
        except (LLMError, TypeError, ValueError, json.JSONDecodeError):
            continue
        for pair in parse_extract_payload(raw):
            pair["evidence"] = {
                "chunk_start": chunk.get("start", 0.0),
                "chunk_end": chunk.get("end", 0.0),
                "chunk_excerpt": chunk["text"][:600],
            }
            collected.append(pair)
    return merge_extracted_pairs(collected)


def deterministic_match(question: str, objections: list[Objection]) -> Objection | None:
    needle = normalize_question(question)
    if not needle:
        return None
    exact = [row for row in objections if normalize_question(row.question) == needle]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return exact[0]
    # Containment fallback for near-duplicates.
    contained = [
        row
        for row in objections
        if needle in normalize_question(row.question) or normalize_question(row.question) in needle
    ]
    if len(contained) == 1:
        return contained[0]
    return None


def parse_match_payload(raw: Any, candidate_count: int, known_ids: set[int]) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        items = raw.get("matches") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    results: list[dict[str, Any]] = [
        {"candidate_index": index, "matched_objection_id": 0, "confidence": 0.0, "match_type": "new"}
        for index in range(candidate_count)
    ]
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("candidate_index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= candidate_count:
            continue
        matched_id = 0
        try:
            matched_id = int(item.get("matched_objection_id") or 0)
        except (TypeError, ValueError):
            matched_id = 0
        if matched_id and matched_id not in known_ids:
            matched_id = 0
        match_type = str(item.get("match_type") or "").strip().lower()
        if matched_id and match_type not in {"enrich", "new"}:
            match_type = "enrich"
        if not matched_id:
            match_type = "new"
        results[index] = {
            "candidate_index": index,
            "matched_objection_id": matched_id,
            "confidence": _clamp_confidence(item.get("confidence", 0.0)),
            "match_type": "enrich" if matched_id else "new",
            "rationale": normalize_whitespace(str(item.get("rationale") or "")),
        }
    return results


def semantic_match_pairs(
    pairs: list[dict[str, Any]],
    objections: list[Objection],
    matcher: Callable[[list[dict[str, str]], list[dict[str, Any]]], Any] | None = None,
) -> list[dict[str, Any]]:
    if not pairs:
        return []
    by_id = {row.id: row for row in objections}
    unresolved: list[tuple[int, dict[str, Any]]] = []
    assigned: dict[int, dict[str, Any]] = {}

    for index, pair in enumerate(pairs):
        hit = deterministic_match(pair["proposed_question"], objections)
        if hit is not None:
            assigned[index] = {
                "candidate_index": index,
                "matched_objection_id": hit.id,
                "confidence": max(pair.get("confidence", 0.0), 0.9),
                "match_type": "enrich",
                "rationale": "Deterministic question match",
            }
        else:
            unresolved.append((index, pair))

    if unresolved and objections:
        catalog = [
            {"id": row.id, "question": row.question, "who_asks": row.who_asks}
            for row in objections
        ]
        payload_pairs = [
            {"candidate_index": index, "question": pair["proposed_question"], "answer": pair["proposed_answer"][:400]}
            for index, pair in unresolved
        ]

        def default_matcher(existing: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> Any:
            return chat_json(
                [
                    {"role": "system", "content": MATCH_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            "Existing objections:\n"
                            f"{json.dumps(existing, ensure_ascii=False)}\n\n"
                            "Candidates:\n"
                            f"{json.dumps(candidates, ensure_ascii=False)}"
                        ),
                    },
                ],
                timeout=45.0,
                reasoning=False,
                max_tokens=1500,
            )

        matcher_fn = matcher or default_matcher
        try:
            raw = matcher_fn(catalog, payload_pairs)
            matches = parse_match_payload(raw, len(pairs), set(by_id))
            for match in matches:
                index = match["candidate_index"]
                if index in assigned:
                    continue
                if match["matched_objection_id"] and match["confidence"] >= 0.7:
                    assigned[index] = match
                else:
                    assigned[index] = {
                        "candidate_index": index,
                        "matched_objection_id": 0,
                        "confidence": match.get("confidence", 0.0),
                        "match_type": "new",
                        "rationale": match.get("rationale", ""),
                    }
        except (LLMError, TypeError, ValueError, json.JSONDecodeError):
            for index, _pair in unresolved:
                assigned.setdefault(
                    index,
                    {
                        "candidate_index": index,
                        "matched_objection_id": 0,
                        "confidence": 0.0,
                        "match_type": "new",
                        "rationale": "Semantic match unavailable",
                    },
                )

    results: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs):
        match = assigned.get(
            index,
            {
                "candidate_index": index,
                "matched_objection_id": 0,
                "confidence": pair.get("confidence", 0.0),
                "match_type": "new",
                "rationale": "",
            },
        )
        row = dict(pair)
        matched_id = int(match.get("matched_objection_id") or 0)
        row["matched_objection_id"] = matched_id
        row["match_type"] = "enrich" if matched_id else "new"
        row["match_confidence"] = float(match.get("confidence") or 0.0)
        row["match_rationale"] = str(match.get("rationale") or "")
        if matched_id and matched_id in by_id:
            objection = by_id[matched_id]
            row["prior_question"] = objection.question
            row["prior_who_asks"] = objection.who_asks
            row["prior_move"] = objection.move
            row["prior_answer"] = objection.answer
        results.append(row)
    return results


def active_qa_job_for(db: Session, style_transcript_id: int) -> Job | None:
    if not style_transcript_id:
        return None
    return (
        db.query(Job)
        .filter(
            Job.job_type == JOB_QA_EXTRACT,
            Job.media_id == style_transcript_id,
            Job.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(Job.id.desc())
        .first()
    )


def enqueue_qa_extraction(db: Session, style_transcript_id: int, force: bool = False) -> tuple[QaExtractionRun, Job]:
    transcript = db.get(StyleTranscript, style_transcript_id)
    if transcript is None:
        raise QaExtractionError("Style transcript not found")

    if not force:
        existing_job = active_qa_job_for(db, style_transcript_id)
        if existing_job is not None:
            run = (
                db.query(QaExtractionRun)
                .filter(QaExtractionRun.job_id == existing_job.id)
                .order_by(QaExtractionRun.id.desc())
                .first()
            )
            if run is not None:
                return run, existing_job

    run = QaExtractionRun(
        style_transcript_id=transcript.id,
        transcript_hash=transcript.text_hash,
        status="queued",
        stage="Queued",
    )
    db.add(run)
    db.flush()

    job = Job(
        job_type=JOB_QA_EXTRACT,
        media_id=transcript.id,
        asset_id=run.id,
        status="queued",
        stage="Queued",
    )
    db.add(job)
    db.flush()
    run.job_id = job.id
    db.commit()
    db.refresh(run)
    db.refresh(job)
    return run, job


def _emit_stage(on_stage: StageCallback | None, text: str) -> None:
    if on_stage is not None:
        on_stage(text)


def persist_candidates(
    db: Session,
    run: QaExtractionRun,
    transcript: StyleTranscript,
    pairs: list[dict[str, Any]],
) -> int:
    created = 0
    for pair in pairs:
        fingerprint = pair.get("fingerprint") or candidate_fingerprint(pair["proposed_question"])
        existing = (
            db.query(QaCandidate)
            .filter(
                QaCandidate.style_transcript_id == transcript.id,
                QaCandidate.fingerprint == fingerprint,
            )
            .first()
        )
        if existing is not None:
            if existing.status == "pending":
                existing.run_id = run.id
                existing.match_type = pair.get("match_type", "new")
                existing.matched_objection_id = int(pair.get("matched_objection_id") or 0)
                existing.confidence = float(pair.get("confidence") or 0.0)
                existing.question_verbatim = pair.get("question_verbatim", "")
                existing.answer_verbatim = pair.get("answer_verbatim", "")
                existing.proposed_question = pair["proposed_question"]
                existing.proposed_who_asks = pair.get("proposed_who_asks", "")
                existing.proposed_move = pair.get("proposed_move", "")
                existing.proposed_answer = pair["proposed_answer"]
                existing.evidence_json = json.dumps(pair.get("evidence") or {}, ensure_ascii=False)
                existing.prior_question = pair.get("prior_question", "")
                existing.prior_who_asks = pair.get("prior_who_asks", "")
                existing.prior_move = pair.get("prior_move", "")
                existing.prior_answer = pair.get("prior_answer", "")
                existing.error = ""
            continue

        db.add(
            QaCandidate(
                run_id=run.id,
                style_transcript_id=transcript.id,
                fingerprint=fingerprint,
                match_type=pair.get("match_type", "new"),
                matched_objection_id=int(pair.get("matched_objection_id") or 0),
                confidence=float(pair.get("confidence") or 0.0),
                status="pending",
                question_verbatim=pair.get("question_verbatim", ""),
                answer_verbatim=pair.get("answer_verbatim", ""),
                proposed_question=pair["proposed_question"],
                proposed_who_asks=pair.get("proposed_who_asks", ""),
                proposed_move=pair.get("proposed_move", ""),
                proposed_answer=pair["proposed_answer"],
                evidence_json=json.dumps(pair.get("evidence") or {}, ensure_ascii=False),
                prior_question=pair.get("prior_question", ""),
                prior_who_asks=pair.get("prior_who_asks", ""),
                prior_move=pair.get("prior_move", ""),
                prior_answer=pair.get("prior_answer", ""),
            )
        )
        created += 1
    return created


def run_qa_extraction(
    db: Session,
    run_id: int,
    on_stage: StageCallback | None = None,
    curate: Callable[[str], Any] | None = None,
    matcher: Callable[[list[dict[str, Any]], list[dict[str, Any]]], Any] | None = None,
) -> QaExtractionRun:
    run = db.get(QaExtractionRun, run_id)
    if run is None:
        raise QaExtractionError("Extraction run not found")
    transcript = db.get(StyleTranscript, run.style_transcript_id)
    if transcript is None:
        raise QaExtractionError("Style transcript not found")

    run.status = "running"
    run.stage = "Extracting Q&A pairs"
    run.error = ""
    db.commit()
    _emit_stage(on_stage, run.stage)

    pairs = extract_pairs_from_transcript(transcript.raw_text, curate=curate)
    run.stage = f"Matching {len(pairs)} candidates"
    db.commit()
    _emit_stage(on_stage, run.stage)

    objections = db.query(Objection).order_by(Objection.id.asc()).all()
    matched = semantic_match_pairs(pairs, objections, matcher=matcher)
    created = persist_candidates(db, run, transcript, matched)
    run.candidate_count = (
        db.query(QaCandidate)
        .filter(QaCandidate.run_id == run.id)
        .count()
    )
    if created and run.candidate_count == 0:
        run.candidate_count = created
    run.status = "done"
    run.stage = "Done"
    run.finished_at = utc_now()
    db.commit()
    db.refresh(run)
    _emit_stage(on_stage, run.stage)
    return run


def approve_candidate(
    db: Session,
    candidate_id: int,
    *,
    question: str | None = None,
    who_asks: str | None = None,
    move: str | None = None,
    answer: str | None = None,
    review_note: str = "",
) -> QaCandidate:
    candidate = db.get(QaCandidate, candidate_id)
    if candidate is None:
        raise QaExtractionError("Candidate not found")
    if candidate.status != "pending":
        raise QaExtractionError("Only pending candidates can be approved")

    transcript = db.get(StyleTranscript, candidate.style_transcript_id)
    source_name = transcript.name if transcript else ""

    final_question = normalize_whitespace(question if question is not None else candidate.proposed_question)
    final_who = normalize_whitespace(who_asks if who_asks is not None else candidate.proposed_who_asks)[:255]
    final_move = normalize_whitespace(move if move is not None else candidate.proposed_move)
    final_answer = normalize_whitespace(answer if answer is not None else candidate.proposed_answer)
    if not final_question or not final_answer:
        raise QaExtractionError("Approved Q&A needs both a question and an answer")

    if candidate.match_type == "enrich" and candidate.matched_objection_id:
        objection = db.get(Objection, candidate.matched_objection_id)
        if objection is None:
            raise QaExtractionError("Matched objection no longer exists")
        objection.question = final_question
        objection.who_asks = final_who
        objection.move = final_move
        objection.answer = final_answer
        objection.status = "approved"
        objection.edited = True
        objection.source_name = source_name
        objection.source_transcript_id = candidate.style_transcript_id
        objection.source_candidate_id = candidate.id
        applied_id = objection.id
    else:
        objection = Objection(
            question=final_question,
            who_asks=final_who,
            move=final_move,
            answer=final_answer,
            status="approved",
            edited=True,
            source_name=source_name,
            source_transcript_id=candidate.style_transcript_id,
            source_candidate_id=candidate.id,
        )
        db.add(objection)
        db.flush()
        applied_id = objection.id

    candidate.proposed_question = final_question
    candidate.proposed_who_asks = final_who
    candidate.proposed_move = final_move
    candidate.proposed_answer = final_answer
    candidate.status = "approved"
    candidate.review_note = review_note
    candidate.applied_objection_id = applied_id
    candidate.reviewed_at = utc_now()
    db.commit()
    db.refresh(candidate)
    return candidate


def reject_candidate(db: Session, candidate_id: int, review_note: str = "") -> QaCandidate:
    candidate = db.get(QaCandidate, candidate_id)
    if candidate is None:
        raise QaExtractionError("Candidate not found")
    if candidate.status != "pending":
        raise QaExtractionError("Only pending candidates can be rejected")
    candidate.status = "rejected"
    candidate.review_note = review_note
    candidate.reviewed_at = utc_now()
    db.commit()
    db.refresh(candidate)
    return candidate


AUDIENCE_HINTS = {
    "A": (
        "student", "students", "parent", "parents", "ug", "pg",
        "aspirant", "aspirants", "school", "applicant", "applicants",
        "family", "admission", "admissions", "faculty", "degree",
        "vision", "brand", "eligibility",
    ),
    "B": ("faculty", "hire", "employee", "candidate", "team", "cxo", "teacher", "academic"),
    "C": ("investor", "vc", "bank", "lender", "donor", "fund", "capital", "vision", "governance"),
    "D": ("recruiter", "corporate", "l&d", "partner", "vendor", "enterprise", "hire", "placement", "skills"),
    "E": ("government", "regulator", "accreditation", "ranking", "university", "auditor", "degree", "psu"),
    "F": ("journalist", "press", "alumni", "creator", "public", "media", "social", "vision", "story"),
}

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "he", "in", "into", "is", "it", "its", "of", "on", "or",
    "that", "the", "to", "was", "were", "will", "with", "one", "two", "many",
}


def rank_objections_for_pitch(
    objections: list[Objection],
    *,
    audience_cluster: str = "",
    audience_label: str = "",
    intent: str = "",
    limit: int = 6,
) -> list[Objection]:
    if not objections:
        return []
    cluster = (audience_cluster or "").strip().upper()
    hints = set(AUDIENCE_HINTS.get(cluster, ()))
    raw_label_tokens = set(normalize_question(audience_label).split()) if audience_label else set()
    label_tokens = {t for t in raw_label_tokens if t not in _STOPWORDS and len(t) > 2}
    defend = intent == "I3"

    def score(row: Objection) -> tuple[int, int, int]:
        haystack = normalize_question(f"{row.who_asks} {row.question} {row.move}")
        tokens = set(haystack.split())
        hint_hits = sum(
            1
            for hint in hints
            if (hint in tokens if len(hint) <= 3 else (hint in tokens or hint in haystack))
        )
        label_hits = len(tokens & label_tokens)
        defend_boost = 1 if defend and any(word in haystack for word in ("object", "concern", "doubt", "why not", "roi")) else 0
        return (hint_hits + label_hits + defend_boost, label_hits, -row.id)

    ranked = sorted(objections, key=score, reverse=True)
    return ranked[: max(0, limit)]
