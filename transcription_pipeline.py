"""Core streaming transcription pipeline.

Streams Google Drive videos into FFmpeg (no video storage), keeps compact
temporary audio, transcribes with Parakeet MLX, and writes one output bundle
per video.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from drive_auth import (
    build_drive_service,
    download_oauth_file,
    extract_file_id,
    get_credentials,
    get_file_metadata,
    get_public_file_metadata,
    open_public_drive_stream,
)

ProgressCallback = Callable[[dict], None]


@dataclass
class VideoJob:
    row: int
    url: str
    file_id: str
    name: str = ""
    safe_name: str = ""
    status: str = "pending"
    message: str = ""
    duration_sec: float = 0.0
    size_bytes: int = 0
    downloaded_bytes: int = 0
    download_progress: float = 0.0
    download_speed_mbps: float = 0.0
    eta_seconds: float = 0.0
    transcription_progress: float = 0.0
    output_dir: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: str = ""


@dataclass
class PipelineState:
    jobs: list[VideoJob] = field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    stop_requested: bool = False
    running: bool = False
    current_phase: str = "idle"
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(name: str, fallback: str = "video") -> str:
    base = Path(name).stem if name else fallback
    cleaned = re.sub(r"[^\w\s\-]+", "", base, flags=re.UNICODE)
    cleaned = re.sub(r"[\s_]+", "-", cleaned).strip("-").lower()
    return cleaned[:80] or fallback


def parse_sheet(path_or_bytes, filename: str = "") -> list[str]:
    """Parse a CSV/XLSX sheet and return Drive URLs/IDs found in each row."""
    name = filename.lower()
    if hasattr(path_or_bytes, "read"):
        data = path_or_bytes.read()
        path_or_bytes = data

    if isinstance(path_or_bytes, (bytes, bytearray)):
        from io import BytesIO

        bio = BytesIO(path_or_bytes)
        if name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(bio)
        else:
            df = pd.read_csv(bio)
    else:
        path = Path(path_or_bytes)
        if path.suffix.lower() in {".xlsx", ".xls"}:
            df = pd.read_excel(path)
        else:
            df = pd.read_csv(path)

    urls: list[str] = []
    for _, row in df.iterrows():
        for value in row.tolist():
            if value is None or (isinstance(value, float) and pd.isna(value)):
                continue
            text = str(value).strip()
            if extract_file_id(text):
                urls.append(text)
                break
    return urls


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"jobs": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def job_complete(output_dir: Path, safe_name: str) -> bool:
    required = [".txt", ".srt", ".vtt", ".json"]
    return all((output_dir / f"{safe_name}{ext}").exists() and (output_dir / f"{safe_name}{ext}").stat().st_size > 0 for ext in required)


def validate_outputs(output_dir: Path, safe_name: str) -> None:
    txt = output_dir / f"{safe_name}.txt"
    srt = output_dir / f"{safe_name}.srt"
    vtt = output_dir / f"{safe_name}.vtt"
    js = output_dir / f"{safe_name}.json"
    for path in (txt, srt, vtt, js):
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f"Missing or empty output: {path.name}")
    text = txt.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError("Transcript text is empty")
    srt_text = srt.read_text(encoding="utf-8")
    if "-->" not in srt_text:
        raise RuntimeError("SRT missing timestamps")
    vtt_text = vtt.read_text(encoding="utf-8")
    if "WEBVTT" not in vtt_text and "-->" not in vtt_text:
        raise RuntimeError("VTT missing timestamps")
    json.loads(js.read_text(encoding="utf-8"))


def format_timestamp(seconds: float, vtt: bool = False) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    sep = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{ms:03d}"


def write_srt(sentences: list[Any], path: Path) -> None:
    lines: list[str] = []
    for idx, sentence in enumerate(sentences, start=1):
        start = getattr(sentence, "start", 0.0)
        end = getattr(sentence, "end", start)
        text = getattr(sentence, "text", str(sentence)).strip()
        if not text:
            continue
        lines.append(str(idx))
        lines.append(f"{format_timestamp(start)} --> {format_timestamp(end)}")
        lines.append(text)
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_vtt(sentences: list[Any], path: Path) -> None:
    lines = ["WEBVTT", ""]
    for sentence in sentences:
        start = getattr(sentence, "start", 0.0)
        end = getattr(sentence, "end", start)
        text = getattr(sentence, "text", str(sentence)).strip()
        if not text:
            continue
        lines.append(f"{format_timestamp(start, vtt=True)} --> {format_timestamp(end, vtt=True)}")
        lines.append(text)
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_json_result(result: Any, path: Path, meta: dict) -> None:
    sentences = []
    for sentence in getattr(result, "sentences", []) or []:
        tokens = []
        for token in getattr(sentence, "tokens", []) or []:
            tokens.append(
                {
                    "text": getattr(token, "text", ""),
                    "start": getattr(token, "start", 0.0),
                    "end": getattr(token, "end", 0.0),
                }
            )
        sentences.append(
            {
                "text": getattr(sentence, "text", ""),
                "start": getattr(sentence, "start", 0.0),
                "end": getattr(sentence, "end", 0.0),
                "tokens": tokens,
            }
        )
    payload = {
        **meta,
        "text": getattr(result, "text", ""),
        "sentences": sentences,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def probe_duration(audio_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    try:
        out = subprocess.check_output(cmd, text=True).strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def download_public_file(
    file_id: str,
    destination: Path,
    stop_event: threading.Event,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Download a public Drive file to a temporary path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    response, session = open_public_drive_stream(file_id)
    total = int(response.headers.get("Content-Length") or 0)
    downloaded = 0
    try:
        with partial.open("wb") as output:
            for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                if stop_event.is_set():
                    raise RuntimeError("Stopped by user")
                if chunk:
                    output.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        on_progress(downloaded, total)
        partial.replace(destination)
        if on_progress:
            on_progress(downloaded, total)
        return destination
    finally:
        response.close()
        session.close()


def extract_audio_from_video(
    video_path: Path,
    audio_path: Path,
    stop_event: threading.Event,
) -> None:
    """Extract compact speech audio from a seekable local video file."""
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.unlink(missing_ok=True)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        str(audio_path),
    ]
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
    try:
        while proc.poll() is None:
            if stop_event.wait(0.25):
                proc.kill()
                raise RuntimeError("Stopped by user")
        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        if proc.stderr:
            proc.stderr.close()
        code = proc.returncode
        if code != 0:
            raise RuntimeError(f"FFmpeg failed ({code}): {stderr.strip()}")
        if not audio_path.exists() or audio_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced empty audio")
    except Exception:
        try:
            proc.kill()
            proc.wait()
        except Exception:
            pass
        if proc.stderr and not proc.stderr.closed:
            proc.stderr.close()
        if audio_path.exists():
            audio_path.unlink(missing_ok=True)
        raise


class TranscriptionPipeline:
    def __init__(
        self,
        credentials_path: Path,
        token_path: Path,
        output_root: Path,
        temp_root: Path,
        model_name: str = "mlx-community/parakeet-tdt-0.6b-v3",
        stream_workers: int = 2,
        access_mode: str = "public",
        on_progress: Optional[ProgressCallback] = None,
    ):
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.output_root = Path(output_root)
        self.temp_root = Path(temp_root)
        self.model_name = model_name
        self.stream_workers = max(1, stream_workers)
        self.access_mode = access_mode if access_mode in {"public", "oauth"} else "public"
        self.on_progress = on_progress or (lambda _state: None)
        self.state = PipelineState()
        self._stop = threading.Event()
        self._model = None
        self._lock = threading.Lock()
        self.manifest_path = self.output_root / "manifest.json"
        self._service = None
        self._creds = None
        self._download_lock = threading.Lock()
        # OAuth + concurrent httplib2 downloads caused SSL failures; keep OAuth serial.
        if self.access_mode == "oauth":
            self.stream_workers = 1

    def request_stop(self) -> None:
        self._stop.set()
        self.state.stop_requested = True
        self.state.current_phase = "stopping"
        self._emit()

    def ensure_auth(self):
        self._creds = get_credentials(self.credentials_path, self.token_path)
        self._service = build_drive_service(self._creds)
        return self._service

    def _emit(self) -> None:
        snapshot = {
            "running": self.state.running,
            "stop_requested": self.state.stop_requested,
            "current_phase": self.state.current_phase,
            "completed": self.state.completed,
            "failed": self.state.failed,
            "skipped": self.state.skipped,
            "total": self.state.total,
            "started_at": self.state.started_at,
            "finished_at": self.state.finished_at,
            "access_mode": self.access_mode,
            "jobs": [asdict(job) for job in self.state.jobs],
        }
        self.on_progress(snapshot)

    def _load_model(self):
        if self._model is None:
            from parakeet_mlx import from_pretrained

            self.state.current_phase = "loading_model"
            self._emit()
            self._model = from_pretrained(self.model_name)
        return self._model

    def prepare_jobs(self, urls: list[str], service=None) -> list[VideoJob]:
        jobs: list[VideoJob] = []
        used_names: dict[str, int] = {}
        for idx, url in enumerate(urls, start=1):
            file_id = extract_file_id(url)
            if not file_id:
                jobs.append(
                    VideoJob(
                        row=idx,
                        url=url,
                        file_id="",
                        status="failed",
                        error="Could not parse Google Drive file ID",
                        message="Invalid URL",
                    )
                )
                continue
            try:
                if service is not None:
                    meta = get_file_metadata(service, file_id)
                else:
                    meta = get_public_file_metadata(file_id)
                name = meta.get("name") or f"video-{file_id}"
                size_bytes = int(meta.get("size") or 0)
            except Exception as exc:
                jobs.append(
                    VideoJob(
                        row=idx,
                        url=url,
                        file_id=file_id,
                        status="failed",
                        error=str(exc),
                        message="Metadata lookup failed",
                    )
                )
                continue

            safe = slugify(name, fallback=f"video-{idx}")
            if safe in used_names:
                used_names[safe] += 1
                safe = f"{safe}-{used_names[safe]}"
            else:
                used_names[safe] = 1

            jobs.append(
                VideoJob(
                    row=idx,
                    url=url,
                    file_id=file_id,
                    name=name,
                    safe_name=safe,
                    size_bytes=size_bytes,
                    status="pending",
                    message="Queued",
                )
            )
        return jobs

    def _unique_output_dir(self, safe_name: str) -> Path:
        path = self.output_root / safe_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _update_job(self, job: VideoJob, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(job, key, value)
            self._emit()
            self._persist_manifest()

    def _persist_manifest(self) -> None:
        data = {
            "updated_at": utc_now(),
            "started_at": self.state.started_at,
            "finished_at": self.state.finished_at,
            "access_mode": self.access_mode,
            "jobs": {job.file_id or f"row-{job.row}": asdict(job) for job in self.state.jobs},
        }
        save_manifest(self.manifest_path, data)

    def _source_path(self, job: VideoJob) -> Path:
        source_suffix = Path(job.name).suffix or ".video"
        return self.temp_root / "source" / f"{job.file_id}{source_suffix}"

    def _extract_one(self, service, job: VideoJob) -> Path:
        audio_path = self.temp_root / f"{job.safe_name}.opus"
        existing_duration = probe_duration(audio_path) if audio_path.exists() else 0.0
        if existing_duration > 0:
            self._update_job(
                job,
                status="ready",
                message="Reusing previously extracted audio",
                duration_sec=existing_duration,
            )
            return audio_path

        audio_path.unlink(missing_ok=True)
        source_path = self._source_path(job)
        mode_label = "OAuth" if (service is not None or self._creds is not None) else "public link"
        self._update_job(
            job,
            status="downloading",
            message=f"Downloading temporary video ({mode_label})",
            started_at=utc_now(),
        )
        download_started = time.monotonic()
        last_update = 0.0

        def report_download(downloaded: int, total: int) -> None:
            nonlocal last_update
            now = time.monotonic()
            if now - last_update < 1.0 and (not total or downloaded < total):
                return
            elapsed = max(0.001, now - download_started)
            bytes_per_second = downloaded / elapsed
            effective_total = total or job.size_bytes
            progress = downloaded / effective_total if effective_total else 0.0
            eta = (
                (effective_total - downloaded) / bytes_per_second
                if effective_total > downloaded and bytes_per_second > 0
                else 0.0
            )
            last_update = now
            self._update_job(
                job,
                downloaded_bytes=downloaded,
                size_bytes=effective_total,
                download_progress=min(1.0, max(0.0, progress)),
                download_speed_mbps=(bytes_per_second * 8) / 1_000_000,
                eta_seconds=max(0.0, eta),
                message=(
                    f"Downloading {downloaded / 1_000_000_000:.2f} / "
                    f"{effective_total / 1_000_000_000:.2f} GB"
                    if effective_total
                    else f"Downloading {downloaded / 1_000_000_000:.2f} GB"
                ),
            )

        try:
            if self._creds is not None:
                download_oauth_file(
                    self._creds,
                    job.file_id,
                    source_path,
                    expected_size=job.size_bytes,
                    stop_requested=self._stop.is_set,
                    on_progress=report_download,
                )
            else:
                download_public_file(
                    job.file_id,
                    source_path,
                    self._stop,
                    on_progress=report_download,
                )

            self._update_job(
                job,
                status="extracting",
                message="Extracting audio, then deleting temporary video",
            )
            extract_audio_from_video(source_path, audio_path, self._stop)
            duration = probe_duration(audio_path)
            self._update_job(job, status="ready", message="Audio ready", duration_sec=duration)
            return audio_path
        finally:
            # Never retain a complete source video after extraction or failure.
            source_path.unlink(missing_ok=True)

    def _handle_transcription(self, job: VideoJob, audio_path: Path) -> None:
        try:
            self._transcribe_one(job, audio_path)
            self.state.completed += 1
        except Exception as exc:  # noqa: BLE001
            self._update_job(
                job,
                status="failed",
                message="Transcription failed",
                error=str(exc),
                finished_at=utc_now(),
            )
            self.state.failed += 1
        self._emit()

    def _process_pending(self, service, pending: list[VideoJob]) -> None:
        """Stream with N workers; transcribe serially on one MLX worker."""
        import queue

        audio_q: queue.Queue = queue.Queue()
        extract_errors: list[tuple[VideoJob, Exception]] = []

        def extract_worker(job: VideoJob) -> None:
            if self._stop.is_set():
                self._update_job(job, status="stopped", message="Stopped by user", finished_at=utc_now())
                audio_q.put(None)
                return
            try:
                audio_path = self._extract_one(service, job)
                audio_q.put((job, audio_path))
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set() or "Stopped by user" in str(exc):
                    self._update_job(job, status="stopped", message="Stopped by user", finished_at=utc_now())
                else:
                    extract_errors.append((job, exc))
                    self._update_job(
                        job,
                        status="failed",
                        message="Streaming/extraction failed",
                        error=str(exc),
                        finished_at=utc_now(),
                    )
                    self.state.failed += 1
                    self._emit()
                audio_q.put(None)

        with ThreadPoolExecutor(max_workers=self.stream_workers) as extractor:
            extract_futures = [extractor.submit(extract_worker, job) for job in pending]
            finished_extracts = 0
            total = len(pending)

            while finished_extracts < total:
                if self._stop.is_set() and audio_q.empty():
                    # Wait remaining extractors to notice stop
                    time.sleep(0.2)

                try:
                    item = audio_q.get(timeout=0.5)
                except queue.Empty:
                    # Check if all extract futures are done and queue drained
                    if all(f.done() for f in extract_futures) and audio_q.empty():
                        finished_extracts = total
                    continue

                finished_extracts += 1
                if item is None:
                    continue
                if self._stop.is_set():
                    job, audio_path = item
                    audio_path.unlink(missing_ok=True)
                    self._update_job(job, status="stopped", message="Stopped by user", finished_at=utc_now())
                    continue
                job, audio_path = item
                self._handle_transcription(job, audio_path)

            for fut in extract_futures:
                try:
                    fut.result()
                except Exception:
                    pass

    def _transcribe_one(self, job: VideoJob, audio_path: Path) -> None:
        from parakeet_mlx.cli import to_json as parakeet_to_json
        from parakeet_mlx.cli import to_srt as parakeet_to_srt
        from parakeet_mlx.cli import to_txt as parakeet_to_txt
        from parakeet_mlx.cli import to_vtt as parakeet_to_vtt

        model = self._load_model()
        self._update_job(job, status="transcribing", message="Transcribing with Parakeet MLX")

        def report_chunk(current: int, total: int) -> None:
            progress = current / total if total else 0.0
            self._update_job(
                job,
                status="transcribing",
                transcription_progress=min(1.0, max(0.0, progress)),
                message=f"Transcribing {progress * 100:.1f}%",
            )

        # Parakeet's Python API defaults to whole-file inference. Long recordings
        # then exceed Metal's maximum single-buffer size. Match the CLI's safe
        # long-audio defaults: 2-minute chunks with 15 seconds of overlap.
        result = model.transcribe(
            str(audio_path),
            chunk_duration=120.0,
            overlap_duration=15.0,
            chunk_callback=report_chunk,
        )

        out_dir = self._unique_output_dir(job.safe_name)
        txt_path = out_dir / f"{job.safe_name}.txt"
        srt_path = out_dir / f"{job.safe_name}.srt"
        vtt_path = out_dir / f"{job.safe_name}.vtt"
        json_path = out_dir / f"{job.safe_name}.json"

        txt_path.write_text(parakeet_to_txt(result).strip() + "\n", encoding="utf-8")
        srt_path.write_text(parakeet_to_srt(result).rstrip() + "\n", encoding="utf-8")
        vtt_path.write_text(parakeet_to_vtt(result).rstrip() + "\n", encoding="utf-8")

        # Enrich JSON with source metadata while keeping Parakeet sentence structure.
        payload = json.loads(parakeet_to_json(result))
        payload.update(
            {
                "file_id": job.file_id,
                "source_url": job.url,
                "source_name": job.name,
                "duration_sec": job.duration_sec,
                "model": self.model_name,
                "created_at": utc_now(),
            }
        )
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        validate_outputs(out_dir, job.safe_name)
        audio_path.unlink(missing_ok=True)
        self._update_job(
            job,
            status="completed",
            message="Done",
            transcription_progress=1.0,
            output_dir=str(out_dir),
            finished_at=utc_now(),
            error="",
        )

    def run(self, urls: list[str]) -> PipelineState:
        self._stop.clear()
        self.state = PipelineState(
            running=True,
            started_at=utc_now(),
            current_phase="starting",
            total=len(urls),
        )
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self._emit()

        try:
            service = None
            if self.access_mode == "oauth":
                self.state.current_phase = "authorizing"
                self._emit()
                service = self.ensure_auth()
            else:
                self.state.current_phase = "public_access"
                self._emit()

            self.state.current_phase = "resolving"
            self._emit()
            jobs = self.prepare_jobs(urls, service)
            self.state.jobs = jobs
            self.state.total = len(jobs)
            self._emit()
            self._persist_manifest()

            # Resume: mark already-complete jobs as skipped.
            pending: list[VideoJob] = []
            for job in jobs:
                if job.status == "failed":
                    self.state.failed += 1
                    continue
                out_dir = self.output_root / job.safe_name
                if job.safe_name and job_complete(out_dir, job.safe_name):
                    self._update_job(
                        job,
                        status="skipped",
                        message="Already completed",
                        output_dir=str(out_dir),
                        finished_at=utc_now(),
                    )
                    self.state.skipped += 1
                    continue
                pending.append(job)

            # Resume useful partial work first: extracted audio, then partial video.
            def resume_priority(job: VideoJob) -> tuple[int, int]:
                audio = self.temp_root / f"{job.safe_name}.opus"
                source = self._source_path(job)
                partial = source.with_suffix(source.suffix + ".part")
                if audio.exists() and audio.stat().st_size > 0:
                    return (0, job.row)
                if partial.exists() and partial.stat().st_size > 0:
                    return (1, job.row)
                return (2, job.row)

            pending.sort(key=resume_priority)

            self.state.current_phase = "processing"
            self._emit()
            self._process_pending(service, pending)

            if self._stop.is_set():
                self.state.current_phase = "stopped"
            else:
                self.state.current_phase = "done"
        except Exception as exc:
            self.state.current_phase = "error"
            self.state.finished_at = utc_now()
            self.state.running = False
            self._emit()
            raise RuntimeError(str(exc)) from exc
        finally:
            self.state.running = False
            self.state.finished_at = utc_now()
            self._persist_manifest()
            self._emit()

        return self.state

    def zip_outputs(self, zip_path: Optional[Path] = None) -> Path:
        zip_path = Path(zip_path or (self.output_root / "transcripts.zip"))
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in self.output_root.rglob("*"):
                if path.is_file() and path.name not in {"manifest.json", zip_path.name}:
                    zf.write(path, path.relative_to(self.output_root).as_posix())
        return zip_path
