from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from backend.config import settings

APIFY_API = "https://api.apify.com/v2"
YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be")
VIDEO_ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})")
FAILED_RUN_STATUSES = {"FAILED", "ABORTED", "TIMED-OUT"}
TERMINAL_RUN_STATUSES = {"SUCCEEDED"} | FAILED_RUN_STATUSES


class YouTubeDownloadError(RuntimeError):
    pass


def is_youtube_url(url: str) -> bool:
    text = (url or "").strip()
    if not text:
        return False
    try:
        host = (urlparse(text).hostname or "").lower()
    except ValueError:
        return False
    return any(host == item or host.endswith(f".{item}") for item in YOUTUBE_HOSTS)


def canonical_youtube_url(url: str) -> str:
    text = (url or "").strip()
    match = VIDEO_ID_RE.search(text)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"
    return text


def _headers() -> dict[str, str]:
    token = (settings.apify_token or "").strip()
    if not token:
        raise YouTubeDownloadError("APIFY_TOKEN is not set")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _actor_path() -> str:
    actor = (settings.apify_youtube_actor or "dami_studio/youtube-video-downloader").strip()
    return actor.replace("/", "~")


# Actor docs say it falls back, but a missing exact height still errors.
# After the requested quality, retry formats YouTube almost always has.
# 720p is retried because the actor intermittently fails format discovery.
QUALITY_LADDER = ("4320", "2160", "1080", "720", "360")
RETRY_QUALITIES = ("720", "720", "720", "360", "360")


def _quality() -> str:
    return (settings.apify_youtube_quality or "4320").strip()


def _quality_attempts(preferred: str | None = None) -> list[str]:
    start = preferred or _quality()
    if start not in QUALITY_LADDER:
        first = [start, *QUALITY_LADDER]
    else:
        first = list(QUALITY_LADDER[QUALITY_LADDER.index(start) :])
    return first + list(RETRY_QUALITIES)


def _memory_mbytes(quality: str) -> int:
    if quality in {"4320", "2160", "1440"}:
        return 8192
    return 4096


def _run_input(url: str, quality: str) -> dict[str, Any]:
    watch_url = canonical_youtube_url(url)
    return {
        "urls": [watch_url],
        "videoUrl": watch_url,
        "quality": quality,
        "audioOnly": False,
        "subtitles": True,
        "subLangs": "en",
        "includeMetadata": True,
        "maxMegabytes": settings.apify_youtube_max_mb,
    }


def parse_download_item(item: dict[str, Any]) -> dict[str, Any]:
    if not item.get("ok", True):
        raise YouTubeDownloadError(str(item.get("error") or item.get("message") or "Apify download failed"))
    media_url = str(item.get("mediaUrl") or item.get("downloadUrl") or item.get("fileUrl") or "")
    if not media_url:
        raise YouTubeDownloadError("Apify finished but returned no media URL")
    title = str(item.get("title") or "youtube-video")
    resolution = str(item.get("resolution") or "")
    requested = str(item.get("requestedQuality") or _quality())
    media_key = str(item.get("mediaKey") or f"{title}.mp4")
    return {
        "media_url": media_url,
        "title": title,
        "resolution": resolution,
        "requested_quality": requested,
        "media_key": media_key,
        "quality_notice": str(item.get("qualityNotice") or ""),
        "file_size_bytes": int(item.get("fileSizeBytes") or 0),
        "subtitles": _subtitle_entries(item.get("subtitles")),
    }


def _subtitle_entries(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url:
            continue
        entries.append({"key": str(item.get("key") or ""), "url": url})
    return entries


def srt_to_text(raw: str) -> str:
    lines: list[str] = []
    for line in (raw or "").replace("\r\n", "\n").split("\n"):
        text = line.strip()
        if not text or text.isdigit() or "-->" in text:
            continue
        text = re.sub(r"<[^>]+>", "", text).strip()
        if text:
            lines.append(text)
    return " ".join(lines)


def pick_subtitle_url(subtitles: list[dict[str, str]]) -> str:
    if not subtitles:
        return ""
    for item in subtitles:
        blob = f"{item.get('key', '')} {item.get('url', '')}".lower()
        if "en" in blob:
            return item.get("url") or ""
    return subtitles[0].get("url") or ""


def _extract_run(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        return data
    if isinstance(payload, dict) and payload.get("id"):
        return payload
    raise YouTubeDownloadError("Apify did not return a run id")


def start_download_run(url: str, quality: str | None = None, timeout: float = 30.0) -> dict[str, Any]:
    chosen = quality or _quality()
    actor = _actor_path()
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{APIFY_API}/acts/{actor}/runs",
            headers=_headers(),
            params={"memory": _memory_mbytes(chosen), "timeout": 1800},
            json=_run_input(url, chosen),
        )
    if response.status_code >= 400:
        raise YouTubeDownloadError(f"Apify run failed to start ({response.status_code}): {response.text[:400]}")
    return _extract_run(response.json())


def wait_for_run(run_id: str, timeout: float = 1800.0, poll: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(f"{APIFY_API}/actor-runs/{run_id}", headers=_headers())
        if response.status_code >= 400:
            raise YouTubeDownloadError(f"Apify run poll failed ({response.status_code}): {response.text[:400]}")
        last = _extract_run(response.json())
        status = str(last.get("status") or "")
        if status in TERMINAL_RUN_STATUSES:
            if status != "SUCCEEDED":
                raise YouTubeDownloadError(f"Apify run {status.lower()}")
            return last
        time.sleep(poll)
    raise YouTubeDownloadError("Apify run timed out while downloading the YouTube video")


def fetch_run_errors(store_id: str) -> list[dict[str, Any]]:
    if not store_id:
        return []
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{APIFY_API}/key-value-stores/{store_id}/records/ERRORS",
            headers=_headers(),
        )
    if response.status_code >= 400:
        return []
    payload = response.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def fetch_dataset_items(dataset_id: str) -> list[dict[str, Any]]:
    with httpx.Client(timeout=60.0) as client:
        response = client.get(
            f"{APIFY_API}/datasets/{dataset_id}/items",
            headers=_headers(),
            params={"clean": 1},
        )
    if response.status_code >= 400:
        raise YouTubeDownloadError(f"Apify dataset failed ({response.status_code}): {response.text[:400]}")
    payload = response.json()
    if not isinstance(payload, list):
        raise YouTubeDownloadError("Apify dataset was not a list")
    return [item for item in payload if isinstance(item, dict)]


def download_caption_text(subtitles: list[dict[str, str]]) -> str:
    url = pick_subtitle_url(subtitles)
    if not url:
        return ""
    headers = _headers()
    with httpx.Client(timeout=60.0, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
    if response.status_code >= 400:
        return ""
    return srt_to_text(response.text)


def _run_error_message(run: dict[str, Any], finished: dict[str, Any]) -> str:
    errors = fetch_run_errors(str(finished.get("defaultKeyValueStoreId") or run.get("defaultKeyValueStoreId") or ""))
    if errors:
        first = errors[0]
        return str(first.get("error") or first.get("message") or "Apify download failed")
    return "Apify returned no download results"


def _format_unavailable(error: str) -> bool:
    lowered = error.lower()
    return "format is not available" in lowered or "requested format" in lowered


def download_youtube(url: str, qualities: list[str] | None = None) -> dict[str, Any]:
    last_error = "Apify returned no download results"
    watch_url = canonical_youtube_url(url)
    for index, quality in enumerate(qualities or _quality_attempts()):
        if index:
            time.sleep(2)
        print(f"  Apify YouTube download at {quality}p ({watch_url})", flush=True)
        run = start_download_run(watch_url, quality=quality)
        finished = wait_for_run(str(run.get("id") or ""))
        dataset_id = str(finished.get("defaultDatasetId") or run.get("defaultDatasetId") or "")
        items = fetch_dataset_items(dataset_id) if dataset_id else []
        if items:
            parsed = parse_download_item(items[0])
            if quality != _quality():
                parsed["quality_notice"] = (
                    f"{parsed.get('quality_notice') + ' ' if parsed.get('quality_notice') else ''}"
                    f"Requested {_quality()}p was unavailable; stored {quality}p."
                ).strip()
            return parsed
        last_error = _run_error_message(run, finished)
        if not _format_unavailable(last_error):
            raise YouTubeDownloadError(last_error)
        print(f"  format unavailable at {quality}p; retrying", flush=True)
    raise YouTubeDownloadError(last_error)
