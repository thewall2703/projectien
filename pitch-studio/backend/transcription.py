"""Audio extraction and OpenRouter speech-to-text for stored videos."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import httpx
import imageio_ffmpeg

from backend.config import settings
from backend.models import Asset
from backend.storage import download_file

OPENROUTER_STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
StageCallback = Callable[[str], None]


class TranscriptionError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.openrouter_api_key.strip()}",
        "HTTP-Referer": "http://127.0.0.1:5173",
        "X-Title": "Pitch Studio",
        "User-Agent": "pitch-studio/1.0",
    }


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return f"{response.status_code} {response.reason_phrase}: {response.text[:500]}"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"{response.status_code} {error.get('message') or error.get('code') or error}"
    if isinstance(error, str):
        return f"{response.status_code} {error}"
    return f"{response.status_code} {response.reason_phrase}: {response.text[:500]}"


def extract_audio(video_path: Path, audio_path: Path) -> None:
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        "-f",
        "mp3",
        "-y",
        str(audio_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip()
        raise TranscriptionError(f"Could not extract video audio: {detail[-1000:]}")
    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise TranscriptionError("Could not extract video audio: output was empty")


def transcribe_audio(audio_path: Path, timeout: float = 600.0) -> str:
    if not settings.openrouter_api_key:
        raise TranscriptionError("OPENROUTER_API_KEY is not set")
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with audio_path.open("rb") as audio, httpx.Client(timeout=timeout) as client:
                response = client.post(
                    OPENROUTER_STT_URL,
                    headers=_headers(),
                    data={
                        "model": settings.openrouter_stt_model,
                        "response_format": "json",
                    },
                    files={"file": (audio_path.name, audio, "audio/mpeg")},
                )
            if response.status_code >= 500:
                last_error = TranscriptionError(_error_message(response))
                time.sleep(1)
                continue
            if response.status_code >= 400:
                raise TranscriptionError(_error_message(response))
            try:
                body = response.json()
            except json.JSONDecodeError as exc:
                raise TranscriptionError(f"OpenRouter STT returned invalid JSON: {exc}") from exc
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise TranscriptionError("OpenRouter STT returned an empty transcript")
            return text.strip()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1)
                continue
            raise TranscriptionError(str(exc)) from exc
    raise TranscriptionError(str(last_error) if last_error else "OpenRouter STT request failed")


def transcribe_asset(asset: Asset, on_stage: StageCallback | None = None) -> str:
    if asset.file_status != "stored" or not asset.file_key:
        raise TranscriptionError("Video must be stored before it can be transcribed")
    suffix = Path(asset.file_key).suffix or ".mp4"
    with tempfile.TemporaryDirectory(prefix=f"pitch-video-{asset.id}-") as temp_dir:
        video_path = Path(temp_dir) / f"video{suffix}"
        audio_path = Path(temp_dir) / "audio.mp3"
        if on_stage:
            on_stage("Loading stored video…")
        download_file(asset.file_key, video_path)
        if on_stage:
            on_stage("Extracting audio…")
        extract_audio(video_path, audio_path)
        video_path.unlink(missing_ok=True)
        if on_stage:
            on_stage("Transcribing audio…")
        return transcribe_audio(audio_path)
