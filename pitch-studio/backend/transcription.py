"""Audio extraction and OpenRouter speech-to-text for stored videos."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
import imageio_ffmpeg

from backend.config import settings
from backend.models import Asset
from backend.pipeline.llm import chat_text_multimodal
from backend.storage import download_file

OPENROUTER_STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
StageCallback = Callable[[str], None]
MAX_VOLUME_RE = re.compile(r"max_volume:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dB", re.IGNORECASE)
SILENT_TRANSCRIPT = "[No spoken audio detected in this video.]"
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
FRAME_COUNT = 10


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoAnalysis:
    transcript: str
    visual_description: str


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


def max_volume_db(audio_path: Path) -> float | None:
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-i",
        str(audio_path),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
    match = MAX_VOLUME_RE.search(result.stderr or "")
    if not match:
        return None
    value = match.group(1).lower()
    return float("-inf") if value == "-inf" else float(value)


def video_duration_seconds(video_path: Path) -> float:
    result = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-i", str(video_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    match = DURATION_RE.search(result.stderr or "")
    if not match:
        raise TranscriptionError("Could not determine video duration")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def extract_video_frames(video_path: Path, frames_dir: Path, count: int = FRAME_COUNT) -> list[bytes]:
    duration = video_duration_seconds(video_path)
    if duration <= 0:
        raise TranscriptionError("Video duration is empty")
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames: list[bytes] = []
    for index in range(count):
        timestamp = duration * (index + 0.5) / count
        output = frames_dir / f"frame-{index:02d}.jpg"
        result = subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=960:-2",
                "-q:v",
                "3",
                "-y",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode == 0 and output.is_file() and output.stat().st_size:
            frames.append(output.read_bytes())
    if not frames:
        raise TranscriptionError("Could not extract any video frames")
    return frames


def describe_video_frames(frames: list[bytes], transcript: str) -> str:
    prompt = (
        "These are representative frames sampled in chronological order from a Masters' Union "
        "video. Explain what happens across the video, including the setting, people, actions, "
        "events, visible text or branding, emotional tone, and story arc. Then explain why the "
        "video is relevant as communications or marketing material and what audiences or messages "
        "it best supports. Combine the visuals with the audio transcript below; do not assume the "
        "transcript alone describes the video. Be concrete and concise.\n\n"
        f"Audio transcript:\n{transcript.strip() or SILENT_TRANSCRIPT}"
    )
    return chat_text_multimodal(prompt, frames, timeout=300.0)


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


def analyze_video_asset(
    asset: Asset,
    transcript: str = "",
    on_stage: StageCallback | None = None,
) -> VideoAnalysis:
    if asset.file_status != "stored" or not asset.file_key:
        raise TranscriptionError("Video must be stored before it can be analyzed")
    suffix = Path(asset.file_key).suffix or ".mp4"
    with tempfile.TemporaryDirectory(prefix=f"pitch-video-{asset.id}-") as temp_dir:
        video_path = Path(temp_dir) / f"video{suffix}"
        audio_path = Path(temp_dir) / "audio.mp3"
        if on_stage:
            on_stage("Loading stored video…")
        download_file(asset.file_key, video_path)
        resolved_transcript = transcript.strip()
        if not resolved_transcript:
            if on_stage:
                on_stage("Extracting audio…")
            try:
                extract_audio(video_path, audio_path)
                volume = max_volume_db(audio_path)
                if volume is not None and volume <= -60:
                    resolved_transcript = SILENT_TRANSCRIPT
                else:
                    if on_stage:
                        on_stage("Transcribing audio…")
                    resolved_transcript = transcribe_audio(audio_path)
            except TranscriptionError as exc:
                if "Could not extract video audio" not in str(exc):
                    raise
                resolved_transcript = SILENT_TRANSCRIPT
        if on_stage:
            on_stage("Analyzing video visuals…")
        frames = extract_video_frames(video_path, Path(temp_dir) / "frames")
        visual_description = describe_video_frames(frames, resolved_transcript)
        return VideoAnalysis(
            transcript=resolved_transcript,
            visual_description=visual_description,
        )
