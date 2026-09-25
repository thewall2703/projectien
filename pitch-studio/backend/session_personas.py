"""Pick personas for Pratham-only style transcripts from their whole-session audio.

transcribe  -> whole-session Whisper text from output/.speaker_cache/<file_id>/audio.wav
interpret   -> OpenRouter model proposes personas (from recipe audience labels) per session,
               reading the whole-session transcript when one exists, else the Pratham-only text
index       -> index the Pratham-only StyleTranscript rows with a reviewed persona selection
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from backend.config import REPO_ROOT, settings
from backend.database import SessionLocal
from backend.models import Recipe, StyleTranscript
from backend.pipeline.llm import chat_json
from backend.pipeline.style_guide import index_style_transcript, parse_webvtt, validate_persona_labels

CACHE_DIR = REPO_ROOT / "output" / ".speaker_cache"
TRANSCRIPTS_DIR = REPO_ROOT / "output" / "transcripts"
SESSION_DIR = REPO_ROOT / "output" / "session_transcripts"
SELECTION_FILE = SESSION_DIR / "selection.json"
STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
CHUNK_SEC = 600
FILE_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)")
INTERPRET_CHAR_BUDGET = 400_000

INTERPRET_SYSTEM = (
    "You read the FULL transcript of a live Masters' Union session (all speakers: Pratham "
    "Mittal, hosts, audience, played clips). Masters' Union employees will later write pitch "
    "scripts for specific PERSONAS, and Pratham's delivery in this session will be used as a "
    "style reference for some of those personas.\n"
    "Decide which personas from the given list this session is a good style reference for: "
    "who is actually in the room or being addressed, what they care about, and whether the "
    "register (formality, stakes, objections, length) transfers to that persona.\n"
    "Only use labels exactly as written in the list. Prefer 1-4 strong matches; return an "
    "empty list if nothing fits. Do not pick a persona just because a topic is mentioned.\n"
    "Return JSON only: "
    '{"session_summary":"2-3 sentences","audience":"who is in the room",'
    '"personas":[{"label":"...","confidence":0.0,"reason":"one sentence grounded in the transcript"}]}'
)


def file_id_from_url(url: str) -> str:
    match = FILE_ID_RE.search(url or "")
    return match.group(1) if match else ""


def pratham_only_rows(db) -> list[StyleTranscript]:
    rows = (
        db.query(StyleTranscript)
        .filter(StyleTranscript.name.like("Pratham only%"))
        .order_by(StyleTranscript.id.asc())
        .all()
    )
    return [row for row in rows if file_id_from_url(row.source_url or "")]


def _stt_chunk(wav: Path, start: int, out: Path) -> str:
    if out.exists():
        return out.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        mp3 = Path(tmp) / "chunk.mp3"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-ss", str(start), "-t", str(CHUNK_SEC),
             "-i", str(wav), "-ac", "1", "-b:a", "48k", str(mp3)],
            check=True,
        )
        headers = {"Authorization": f"Bearer {settings.openrouter_api_key.strip()}", "X-Title": "Pitch Studio"}
        data = {"model": settings.openrouter_stt_model, "response_format": "json"}
        last = ""
        for attempt in range(4):
            try:
                with mp3.open("rb") as fh:
                    response = httpx.post(
                        STT_URL, headers=headers, data=data,
                        files={"file": ("chunk.mp3", fh, "audio/mpeg")}, timeout=600,
                    )
            except httpx.HTTPError as exc:
                last = str(exc)
                time.sleep(3 + attempt * 5)
                continue
            if response.status_code == 200:
                text = (response.json().get("text") or "").strip()
                out.write_text(text, encoding="utf-8")
                return text
            last = f"{response.status_code}: {response.text[:300]}"
            if response.status_code < 500 and response.status_code != 429:
                break
            time.sleep(3 + attempt * 5)
        raise RuntimeError(f"STT failed for {wav.parent.name}@{start}s: {last}")


def _duration(wav: Path) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)],
        check=True, capture_output=True, text=True,
    )
    return float(probe.stdout.strip())


def transcribe_session(file_id: str, workers: int = 6) -> Path:
    wav = CACHE_DIR / file_id / "audio.wav"
    if not wav.exists():
        raise FileNotFoundError(f"No cached audio for {file_id}")
    out_dir = SESSION_DIR / file_id
    out_dir.mkdir(parents=True, exist_ok=True)
    full = out_dir / "transcript.txt"
    if full.exists():
        return full
    starts = list(range(0, int(_duration(wav)) + 1, CHUNK_SEC))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        texts = list(pool.map(lambda s: _stt_chunk(wav, s, out_dir / f"chunk_{s:05d}.txt"), starts))
    blocks = [f"[{s // 3600}:{s % 3600 // 60:02d}:00]\n{t}" for s, t in zip(starts, texts) if t]
    full.write_text("\n\n".join(blocks), encoding="utf-8")
    return full


def persona_catalog(db) -> list[dict[str, str]]:
    rows = db.query(Recipe).order_by(Recipe.id.asc()).all()
    seen: set[str] = set()
    catalog = []
    for row in rows:
        label = (row.audience_label or "").strip()
        if label and label not in seen:
            seen.add(label)
            catalog.append({"label": label, "cluster": row.audience_cluster, "intent": row.intent,
                            "channel": row.channel, "duration": row.duration})
    return catalog


def interpret_session(name: str, transcript: str, catalog: list[dict[str, str]], model: str) -> dict[str, Any]:
    persona_lines = "\n".join(
        f"- {item['label']} (cluster={item['cluster']}, intent={item['intent']}, "
        f"channel={item['channel']}, duration={item['duration']})"
        for item in catalog
    )
    payload = chat_json(
        [
            {"role": "system", "content": INTERPRET_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"PERSONA LIST:\n{persona_lines}\n\nSESSION: {name}\n\n"
                    f"FULL SESSION TRANSCRIPT:\n{transcript[:INTERPRET_CHAR_BUDGET]}"
                ),
            },
        ],
        timeout=300.0,
        model=model,
    )
    known = {item["label"].casefold(): item["label"] for item in catalog}
    personas = []
    for item in payload.get("personas") or []:
        if not isinstance(item, dict):
            continue
        label = known.get(str(item.get("label") or "").strip().casefold())
        if label:
            personas.append({"label": label, "confidence": item.get("confidence"), "reason": item.get("reason", "")})
    return {
        "session_summary": payload.get("session_summary", ""),
        "audience": payload.get("audience", ""),
        "personas": personas,
    }


def cmd_transcribe(args) -> None:
    db = SessionLocal()
    try:
        rows = pratham_only_rows(db)
    finally:
        db.close()
    ids = sorted({file_id_from_url(row.source_url) for row in rows})
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        for file_id, result in zip(ids, pool.map(_safe_transcribe, ids)):
            print(f"{file_id}: {result}", flush=True)


def _safe_transcribe(file_id: str) -> str:
    try:
        path = transcribe_session(file_id)
        return f"ok {len(path.read_text(encoding='utf-8'))} chars"
    except Exception as exc:  # noqa: BLE001
        return f"FAILED {exc}"


def existing_session_transcripts() -> dict[str, Path]:
    """Whole-session (all speakers) transcripts from the earlier Drive batch, keyed by file id."""
    manifest = TRANSCRIPTS_DIR / "manifest.json"
    if not manifest.exists():
        return {}
    jobs = json.loads(manifest.read_text(encoding="utf-8")).get("jobs") or {}
    found: dict[str, Path] = {}
    for file_id, job in jobs.items():
        safe = job.get("safe_name") or ""
        path = TRANSCRIPTS_DIR / safe / f"{safe}.txt"
        if safe and path.exists():
            found[file_id] = path
    return found


def session_text(row: StyleTranscript, existing: dict[str, Path]) -> tuple[str, str]:
    file_id = file_id_from_url(row.source_url)
    for path in (SESSION_DIR / file_id / "transcript.txt", existing.get(file_id)):
        if path is not None and path.exists():
            return path.read_text(encoding="utf-8"), "whole_session"
    lines = parse_webvtt(row.raw_text or "")
    return "\n".join(lines) if lines else (row.raw_text or ""), "pratham_only"


def cmd_interpret(args) -> None:
    db = SessionLocal()
    try:
        rows = pratham_only_rows(db)
        catalog = persona_catalog(db)
    finally:
        db.close()
    model = args.model or settings.openrouter_model
    existing = existing_session_transcripts()

    def run(row: StyleTranscript) -> str:
        file_id = file_id_from_url(row.source_url)
        out = SESSION_DIR / file_id / "personas.json"
        if out.exists() and not args.force:
            return f"ST {row.id}: cached"
        text, source = session_text(row, existing)
        name = row.name if source == "whole_session" else f"{row.name} (only Pratham's lines available)"
        try:
            result = interpret_session(name, text, catalog, model)
        except Exception as exc:  # noqa: BLE001
            return f"ST {row.id}: FAILED {exc}"
        out.parent.mkdir(parents=True, exist_ok=True)
        result.update({"style_transcript_id": row.id, "name": row.name, "file_id": file_id,
                       "model": model, "source": source})
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        return f"ST {row.id} [{source}]: {[p['label'] for p in result['personas']]}"

    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        for line in pool.map(run, rows):
            print(line, flush=True)


def cmd_index(args) -> None:
    selection = json.loads(Path(args.selection).read_text(encoding="utf-8"))
    db = SessionLocal()
    try:
        for key, labels in selection.items():
            transcript_id = int(key)
            if not labels:
                print(f"ST {transcript_id}: skipped (no personas)", flush=True)
                continue
            row = db.get(StyleTranscript, transcript_id)
            if row is None or not row.name.startswith("Pratham only"):
                print(f"ST {transcript_id}: skipped (not a Pratham-only transcript)", flush=True)
                continue
            if row.status == "processed":
                print(f"ST {transcript_id}: already indexed", flush=True)
                continue
            validate_persona_labels(db, labels)
            try:
                result = index_style_transcript(db, transcript_id, labels)
            except Exception as exc:  # noqa: BLE001
                print(f"ST {transcript_id}: FAILED {exc}", flush=True)
                continue
            print(
                f"ST {transcript_id}: guides={result['guides_updated']} "
                f"quotes_kept={result['quotes_kept']} skipped={result['quotes_skipped']}",
                flush=True,
            )
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    transcribe = sub.add_parser("transcribe")
    transcribe.add_argument("--parallel", type=int, default=3)
    interpret = sub.add_parser("interpret")
    interpret.add_argument("--model", default="")
    interpret.add_argument("--parallel", type=int, default=4)
    interpret.add_argument("--force", action="store_true")
    index = sub.add_parser("index")
    index.add_argument("--selection", default=str(SELECTION_FILE))
    args = parser.parse_args(argv)
    {"transcribe": cmd_transcribe, "interpret": cmd_interpret, "index": cmd_index}[args.command](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
