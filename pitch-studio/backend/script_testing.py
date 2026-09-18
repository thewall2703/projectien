from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import ScriptTestRun, utc_now
from backend.pipeline.runner import generate_script_phase
from backend.pipeline.validator import split_spoken_sentences


def script_paragraphs(text: str) -> list[str]:
    """Match frontend scriptParagraphs() blank-line / newline / sentence grouping."""
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return []
    by_blank = [
        re.sub(r"[ \t]+", " ", part).strip()
        for part in re.split(r"\n\s*\n", raw)
    ]
    by_blank = [part for part in by_blank if part]
    if len(by_blank) > 1:
        return by_blank
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in raw.split("\n")
        if line.strip()
    ]
    if len(lines) > 1:
        return lines
    sentences = split_spoken_sentences(raw)
    if len(sentences) <= 2:
        return [raw]
    paragraphs: list[str] = []
    for index in range(0, len(sentences), 2):
        paragraphs.append(" ".join(sentences[index : index + 2]))
    return paragraphs


def build_review_document(script: dict[str, Any]) -> dict[str, Any]:
    """Derive stable sentence/paragraph targets from a final ScriptPayload dict."""
    sections_out: list[dict[str, Any]] = []
    for section_index, section in enumerate(script.get("sections") or []):
        text = str(section.get("text") or "")
        paragraphs_out: list[dict[str, Any]] = []
        for paragraph_index, paragraph_text in enumerate(script_paragraphs(text)):
            sentences = split_spoken_sentences(paragraph_text) or [paragraph_text]
            sentence_rows = []
            for sentence_index, sentence_text in enumerate(sentences):
                sentence_rows.append(
                    {
                        "id": (
                            f"section-{section_index}-paragraph-{paragraph_index}"
                            f"-sentence-{sentence_index}"
                        ),
                        "index": sentence_index,
                        "text": sentence_text,
                    }
                )
            paragraphs_out.append(
                {
                    "id": f"section-{section_index}-paragraph-{paragraph_index}",
                    "index": paragraph_index,
                    "text": paragraph_text,
                    "sentences": sentence_rows,
                }
            )
        sections_out.append(
            {
                "index": section_index,
                "module_id": str(section.get("module_id") or ""),
                "topic_id": int(section.get("topic_id") or 0),
                "topic_title": str(section.get("topic_title") or ""),
                "heading": str(section.get("heading") or ""),
                "paragraphs": paragraphs_out,
            }
        )
    return {
        "sections": sections_out,
        "cta": str(script.get("cta") or ""),
    }


def find_review_target(
    review_document: dict[str, Any],
    target_kind: str,
    target_id: str,
) -> dict[str, Any] | None:
    """Return coordinates and reference text for a validated review target."""
    for section in review_document.get("sections") or []:
        section_index = int(section.get("index") or 0)
        for paragraph in section.get("paragraphs") or []:
            paragraph_index = int(paragraph.get("index") or 0)
            if target_kind == "paragraph" and paragraph.get("id") == target_id:
                return {
                    "target_kind": "paragraph",
                    "target_id": target_id,
                    "section_index": section_index,
                    "paragraph_index": paragraph_index,
                    "sentence_index": None,
                    "reference_text": str(paragraph.get("text") or ""),
                }
            if target_kind == "sentence":
                for sentence in paragraph.get("sentences") or []:
                    if sentence.get("id") == target_id:
                        return {
                            "target_kind": "sentence",
                            "target_id": target_id,
                            "section_index": section_index,
                            "paragraph_index": paragraph_index,
                            "sentence_index": int(sentence.get("index") or 0),
                            "reference_text": str(sentence.get("text") or ""),
                        }
    return None


def run_script_test(script_test_id: int) -> None:
    """Background job: generate and validate a script only (no deck/media)."""
    db = SessionLocal()
    run = db.get(ScriptTestRun, script_test_id)
    if run is None:
        db.close()
        return

    def set_status(status: str) -> None:
        run.status = status
        db.commit()
        db.refresh(run)

    try:
        phase = generate_script_phase(db, run, set_status)
        if phase is None:
            run.finished_at = utc_now()
            db.commit()
            return
        run.review_document_json = json.dumps(
            build_review_document(phase.script),
            ensure_ascii=False,
        )
        run.error = ""
        run.finished_at = utc_now()
        set_status("done")
    except Exception as exc:  # noqa: BLE001
        run.error = f"{type(exc).__name__}: {exc}"
        run.status = "failed"
        run.finished_at = utc_now()
        db.commit()
    finally:
        db.close()
