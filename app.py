"""Simple local Streamlit UI for streamed Google Drive transcription."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from transcription_pipeline import TranscriptionPipeline, parse_sheet

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "output" / "transcripts"
DEFAULT_TEMP = ROOT / ".tmp" / "audio"
CREDENTIALS_PATH = ROOT / "credentials.json"
TOKEN_PATH = ROOT / ".token.json"


def init_state() -> None:
    defaults = {
        "pipeline_thread": None,
        "pipeline": None,
        "progress": None,
        "error": None,
        "urls": [],
        "running": False,
        "zip_path": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def progress_callback(snapshot: dict) -> None:
    st.session_state["progress"] = snapshot
    st.session_state["running"] = bool(snapshot.get("running"))


def start_pipeline(
    urls: list[str],
    output_dir: Path,
    temp_dir: Path,
    stream_workers: int,
    access_mode: str,
) -> None:
    pipeline = TranscriptionPipeline(
        credentials_path=CREDENTIALS_PATH,
        token_path=TOKEN_PATH,
        output_root=output_dir,
        temp_root=temp_dir,
        stream_workers=stream_workers,
        access_mode=access_mode,
        on_progress=progress_callback,
    )
    st.session_state["pipeline"] = pipeline
    st.session_state["error"] = None
    st.session_state["zip_path"] = None
    st.session_state["running"] = True

    def worker() -> None:
        try:
            pipeline.run(urls)
            if not pipeline.state.stop_requested:
                zip_path = pipeline.zip_outputs()
                st.session_state["zip_path"] = str(zip_path)
        except Exception as exc:  # noqa: BLE001
            st.session_state["error"] = str(exc)
        finally:
            st.session_state["running"] = False

    thread = threading.Thread(target=worker, daemon=True)
    st.session_state["pipeline_thread"] = thread
    thread.start()


def status_color(status: str) -> str:
    return {
        "completed": "🟢",
        "skipped": "🔵",
        "failed": "🔴",
        "downloading": "🟡",
        "extracting": "🟠",
        "streaming": "🟡",
        "ready": "🟡",
        "transcribing": "🟠",
        "pending": "⚪",
        "stopped": "⚫",
    }.get(status, "⚪")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def elapsed_seconds(start_timestamp: str | None, end_timestamp: str | None = None) -> float:
    if not start_timestamp:
        return 0.0
    try:
        started = datetime.fromisoformat(start_timestamp.replace("Z", "+00:00"))
        ended = (
            datetime.fromisoformat(end_timestamp.replace("Z", "+00:00"))
            if end_timestamp
            else datetime.now(timezone.utc)
        )
        return max(0.0, (ended - started).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def manifest_urls(output_dir: Path) -> list[str]:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        jobs = sorted(
            (manifest.get("jobs") or {}).values(),
            key=lambda job: job.get("row") or 0,
        )
        return [job["url"] for job in jobs if job.get("url")]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def refresh_progress_from_manifest(
    output_dir: Path,
    progress: dict | None,
    thread_alive: bool,
) -> dict | None:
    """Use the on-disk manifest as a thread-safe live status source."""
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return progress
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        jobs = list((manifest.get("jobs") or {}).values())
    except (OSError, json.JSONDecodeError):
        return progress
    if not jobs:
        return progress

    snapshot = dict(progress or {})
    statuses = [job.get("status") for job in jobs]
    snapshot.update(
        {
            "jobs": jobs,
            "total": len(jobs),
            "completed": statuses.count("completed"),
            "skipped": statuses.count("skipped"),
            "failed": statuses.count("failed"),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "access_mode": manifest.get("access_mode"),
        }
    )
    active = next(
        (
            job
            for job in jobs
            if job.get("status")
            in {"downloading", "extracting", "ready", "transcribing", "streaming"}
        ),
        None,
    )
    incomplete = any(
        job.get("status") not in {"completed", "skipped"} for job in jobs
    )
    snapshot["resume_available"] = incomplete and not thread_alive
    if active:
        snapshot["current_phase"] = (
            active.get("status") if thread_alive else "interrupted"
        )
        snapshot["running"] = thread_alive
    elif thread_alive:
        snapshot["running"] = True
    else:
        snapshot["running"] = False
        if any(job.get("status") == "stopped" for job in jobs):
            snapshot["current_phase"] = "stopped"
        elif manifest.get("finished_at"):
            snapshot["current_phase"] = "finished"
    return snapshot


def main() -> None:
    st.set_page_config(page_title="Pratham Transcription", page_icon="🎧", layout="wide")
    init_state()

    st.title("Pratham Transcription")
    st.caption(
        "Upload a sheet of Google Drive links. Each source video is downloaded "
        "temporarily, converted to audio, and immediately deleted. Every video gets its own "
        "TXT / SRT / VTT / JSON transcript bundle."
    )

    with st.sidebar:
        st.header("Settings")
        output_dir = Path(
            st.text_input("Output folder", value=str(DEFAULT_OUTPUT))
        ).expanduser()
        temp_dir = Path(
            st.text_input("Temporary audio folder", value=str(DEFAULT_TEMP))
        ).expanduser()
        access_mode = st.radio(
            "Drive access",
            options=["public", "oauth"],
            index=1,  # default to OAuth since public quota fails for large files
            format_func=lambda v: (
                "Anyone with the link (no login)"
                if v == "public"
                else "Private files (Google sign-in)"
            ),
            help="Use Google sign-in when public downloads hit quota or SSL issues.",
        )
        stream_workers = st.slider(
            "Concurrent video downloads",
            min_value=1,
            max_value=3,
            value=1 if access_mode == "oauth" else 2,
            help="OAuth mode uses 1 for reliable resumable downloads.",
        )
        st.markdown("---")
        st.subheader("Google authorization (optional)")
        st.caption("Only needed if files are private / not link-shared.")
        if access_mode == "public":
            st.success("Public mode: credentials.json not required")
        elif CREDENTIALS_PATH.exists():
            st.success("credentials.json found")
        else:
            st.warning(
                "For private files, place a Desktop OAuth client JSON at "
                "`credentials.json` in the project root."
            )
        if TOKEN_PATH.exists():
            st.info("Already signed in (.token.json present)")
        if st.button(
            "Authorize Google Drive now",
            disabled=st.session_state["running"] or access_mode != "oauth",
        ):
            try:
                pipeline = TranscriptionPipeline(
                    credentials_path=CREDENTIALS_PATH,
                    token_path=TOKEN_PATH,
                    output_root=output_dir,
                    temp_root=temp_dir,
                    access_mode="oauth",
                )
                pipeline.ensure_auth()
                st.success("Google Drive authorized.")
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))

    uploaded = st.file_uploader(
        "Upload sheet (CSV or Excel) with one Google Drive URL per row",
        type=["csv", "xlsx", "xls"],
        disabled=st.session_state["running"],
    )

    if uploaded is not None:
        try:
            urls = parse_sheet(uploaded.getvalue(), filename=uploaded.name)
            st.session_state["urls"] = urls
            preview = pd.DataFrame({"drive_url": urls})
            st.write(f"Found **{len(urls)}** Drive link(s).")
            st.dataframe(preview, width="stretch", height=220)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not parse sheet: {exc}")

    urls = st.session_state.get("urls") or []
    resume_urls = manifest_urls(output_dir)
    if not urls and resume_urls:
        urls = resume_urls
        st.session_state["urls"] = resume_urls
        st.info(
            f"Found an interrupted run with {len(resume_urls)} video(s). "
            "The saved partial download will resume automatically."
        )

    pipeline_thread = st.session_state.get("pipeline_thread")
    thread_alive = bool(pipeline_thread and pipeline_thread.is_alive())
    existing_progress = refresh_progress_from_manifest(
        output_dir,
        st.session_state.get("progress"),
        thread_alive,
    )
    resume_available = bool(
        existing_progress and existing_progress.get("resume_available")
    )
    start_disabled = st.session_state["running"] or not urls
    if access_mode == "oauth" and not CREDENTIALS_PATH.exists():
        start_disabled = True

    col_start, col_stop, col_refresh = st.columns([1, 1, 1])
    with col_start:
        start = st.button(
            "Resume interrupted run" if resume_available else "Start transcription",
            type="primary",
            disabled=start_disabled,
            width="stretch",
        )
    with col_stop:
        stop = st.button(
            "Stop",
            disabled=not st.session_state["running"],
            width="stretch",
        )
    with col_refresh:
        st.button("Refresh status", width="stretch")

    if start:
        output_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        start_pipeline(urls, output_dir, temp_dir, stream_workers, access_mode)
        st.rerun()

    if stop and st.session_state.get("pipeline"):
        st.session_state["pipeline"].request_stop()
        st.warning("Stop requested. Finishing the current step…")

    pipeline_thread = st.session_state.get("pipeline_thread")
    thread_alive = bool(pipeline_thread and pipeline_thread.is_alive())
    progress = refresh_progress_from_manifest(
        output_dir,
        st.session_state.get("progress"),
        thread_alive,
    )
    if st.session_state.get("error"):
        st.error(st.session_state["error"])

    if progress:
        if progress.get("resume_available"):
            st.warning(
                "Previous run was interrupted. Click **Resume interrupted run** "
                "to continue its partial download."
            )
        total = max(1, int(progress.get("total") or 1))
        done = int(progress.get("completed") or 0) + int(progress.get("skipped") or 0)
        failed = int(progress.get("failed") or 0)
        phase = progress.get("current_phase") or "idle"
        mode = progress.get("access_mode") or access_mode
        jobs = progress.get("jobs") or []
        active_job = next(
            (
                job
                for job in jobs
                if job.get("status")
                in {"downloading", "extracting", "ready", "transcribing", "streaming"}
            ),
            None,
        )
        active_fraction = (
            float(active_job.get("download_progress") or 0)
            if active_job and active_job.get("status") == "downloading"
            else 0.0
        )
        overall_fraction = min(1.0, (done + active_fraction) / total)
        st.progress(
            overall_fraction,
            text=f"Phase: {phase} · mode: {mode} · {done}/{total} finished · {failed} failed",
        )

        elapsed = elapsed_seconds(
            progress.get("started_at"), progress.get("finished_at")
        )
        metrics = st.columns(6)
        metrics[0].metric("Total", progress.get("total", 0))
        metrics[1].metric("Completed", progress.get("completed", 0))
        metrics[2].metric("Skipped", progress.get("skipped", 0))
        metrics[3].metric("Failed", progress.get("failed", 0))
        metrics[4].metric("Elapsed", format_duration(elapsed))
        metrics[5].metric("Running", "Yes" if progress.get("running") else "No")

        if active_job:
            st.subheader("Current activity")
            active_cols = st.columns(5)
            active_cols[0].metric("Video", active_job.get("name") or "—")
            active_cols[1].metric(
                "Stage", str(active_job.get("status") or "—").title()
            )
            active_cols[2].metric(
                "Progress",
                (
                    f"{float(active_job.get('download_progress') or 0) * 100:.1f}%"
                    if active_job.get("status") == "downloading"
                    else f"{float(active_job.get('transcription_progress') or 0) * 100:.1f}%"
                    if active_job.get("status") == "transcribing"
                    else "—"
                ),
            )
            active_cols[3].metric(
                "Speed",
                f"{(active_job.get('download_speed_mbps') or 0):.1f} Mbps",
            )
            active_cols[4].metric(
                "ETA",
                format_duration(active_job.get("eta_seconds") or 0)
                if active_job.get("status") == "downloading"
                else "—",
            )
            if active_job.get("status") == "downloading":
                st.progress(
                    min(1.0, float(active_job.get("download_progress") or 0)),
                    text=(
                        f"{float(active_job.get('download_progress') or 0) * 100:.1f}% · "
                        f"{active_job.get('message') or ''}"
                    ),
                )
            elif active_job.get("status") == "transcribing":
                st.progress(
                    min(
                        1.0,
                        float(active_job.get("transcription_progress") or 0),
                    ),
                    text=active_job.get("message") or "Transcribing",
                )

        if jobs:
            rows = []
            for job in jobs:
                rows.append(
                    {
                        "#": job.get("row"),
                        "status": f"{status_color(job.get('status', ''))} {job.get('status', '')}",
                        "name": job.get("name") or job.get("safe_name") or "",
                        "progress": round(
                            float(job.get("download_progress") or 0) * 100, 1
                        ),
                        "transcription": round(
                            float(job.get("transcription_progress") or 0) * 100,
                            1,
                        ),
                        "downloaded_GB": round(
                            (job.get("downloaded_bytes") or 0) / 1_000_000_000, 2
                        ),
                        "total_GB": round(
                            (job.get("size_bytes") or 0) / 1_000_000_000, 2
                        ),
                        "speed_Mbps": round(
                            float(job.get("download_speed_mbps") or 0), 1
                        ),
                        "ETA": (
                            format_duration(job.get("eta_seconds") or 0)
                            if job.get("status") == "downloading"
                            else ""
                        ),
                        "message": job.get("message") or "",
                        "duration_min": round((job.get("duration_sec") or 0) / 60, 1),
                        "output": job.get("output_dir") or "",
                        "error": job.get("error") or "",
                    }
                )
            st.dataframe(
                pd.DataFrame(rows),
                width="stretch",
                height=420,
                column_config={
                    "progress": st.column_config.ProgressColumn(
                        "Download %",
                        min_value=0,
                        max_value=100,
                        format="%.1f%%",
                    ),
                    "transcription": st.column_config.ProgressColumn(
                        "Transcription %",
                        min_value=0,
                        max_value=100,
                        format="%.1f%%",
                    ),
                },
                hide_index=True,
            )

        st.caption(f"Outputs: `{output_dir}`")
        zip_path = st.session_state.get("zip_path")
        if zip_path and Path(zip_path).exists():
            st.download_button(
                "Download transcripts ZIP",
                data=Path(zip_path).read_bytes(),
                file_name="transcripts.zip",
                mime="application/zip",
            )
        elif not progress.get("running") and (progress.get("completed") or 0) > 0:
            if st.button("Create ZIP of outputs"):
                pipeline = st.session_state.get("pipeline")
                if pipeline:
                    path = pipeline.zip_outputs()
                    st.session_state["zip_path"] = str(path)
                    st.rerun()

    if st.session_state["running"]:
        time.sleep(1.5)
        st.rerun()


if __name__ == "__main__":
    main()
