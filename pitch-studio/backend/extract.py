"""RunPod extraction client and report-passage selection for generation."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Asset
from backend.storage import get_url

DEFAULT_LIMIT = 4
DEFAULT_MAX_CHARS = 2800
DEFAULT_PASSAGE_CHARS = 900


def passages_from_extract(
    payload: dict[str, Any],
    *,
    asset_id: int,
    title: str,
    module_ids: str = "",
) -> list[dict[str, Any]]:
    passages: list[dict[str, Any]] = []
    chunks = payload.get("chunks") or []
    if chunks:
        for chunk in chunks:
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            passages.append(
                {
                    "asset_id": asset_id,
                    "title": title,
                    "module_ids": module_ids,
                    "start_page": chunk.get("start_page"),
                    "end_page": chunk.get("end_page"),
                    "text": text,
                }
            )
        return passages
    for page in payload.get("pages") or []:
        if page.get("duplicate"):
            continue
        text = (page.get("text") or "").strip()
        if not text:
            continue
        passages.append(
            {
                "asset_id": asset_id,
                "title": title,
                "module_ids": module_ids,
                "start_page": page.get("page"),
                "end_page": page.get("page"),
                "text": text,
            }
        )
    return passages


def pick_report_passages(
    assets: list[Asset],
    sequence: list[str],
    *,
    limit: int = DEFAULT_LIMIT,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[dict[str, Any]]:
    sequence_set = set(sequence)
    scored: list[tuple[int, dict[str, Any]]] = []
    for asset in assets:
        if asset.type != "report" or asset.extract_status != "ready" or not asset.extract_json:
            continue
        try:
            payload = json.loads(asset.extract_json)
        except json.JSONDecodeError:
            continue
        ids = {part.strip() for part in (asset.module_ids or "").split(",") if part.strip()}
        overlap = len(ids & sequence_set) if ids else 0
        rank = 0 if overlap else 1
        for passage in passages_from_extract(
            payload,
            asset_id=asset.id,
            title=asset.title,
            module_ids=asset.module_ids,
        ):
            scored.append((rank, passage))
    scored.sort(key=lambda item: item[0])
    selected: list[dict[str, Any]] = []
    used = 0
    for _, passage in scored:
        text = passage["text"].strip()
        if len(text) > DEFAULT_PASSAGE_CHARS:
            text = text[:DEFAULT_PASSAGE_CHARS].rsplit(" ", 1)[0]
            passage = dict(passage)
            passage["text"] = text
        if used + len(text) > max_chars and selected:
            remain = max_chars - used
            if remain > 240:
                trimmed = dict(passage)
                trimmed["text"] = text[:remain].rsplit(" ", 1)[0]
                selected.append(trimmed)
            break
        selected.append(passage)
        used += len(text)
        if len(selected) >= limit:
            break
    return selected


def _runpod_headers() -> dict[str, str]:
    if not settings.runpod_api_key or not settings.runpod_endpoint_id:
        raise RuntimeError("RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID are required")
    return {
        "Authorization": f"Bearer {settings.runpod_api_key}",
        "Content-Type": "application/json",
    }


def _runpod_url(path: str) -> str:
    return f"https://api.runpod.ai/v2/{settings.runpod_endpoint_id}/{path}"


def runpod_extract(
    *,
    file_url: str | None = None,
    spaces_key: str | None = None,
    max_pages: int | None = None,
    ocr: bool = True,
    chunks: bool = True,
    timeout: float = 300.0,
) -> dict[str, Any]:
    job_input: dict[str, Any] = {"ocr": ocr, "chunks": chunks}
    if file_url:
        job_input["file_url"] = file_url
    elif spaces_key:
        job_input["spaces_key"] = spaces_key
    else:
        raise ValueError("provide file_url or spaces_key")
    if max_pages:
        job_input["max_pages"] = max_pages

    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            _runpod_url("runsync"),
            headers=_runpod_headers(),
            json={"input": job_input},
        )
        response.raise_for_status()
        body = response.json()
        status = body.get("status")
        if status in {None, "COMPLETED"}:
            output = body.get("output") or {}
            if isinstance(output, dict) and output.get("error"):
                raise RuntimeError(output["error"])
            if isinstance(output, dict):
                return output
        if status in {"IN_QUEUE", "IN_PROGRESS"}:
            return _poll_job(client, body.get("id"), timeout)
        if body.get("error"):
            raise RuntimeError(str(body["error"]))
        output = body.get("output") or {}
        if isinstance(output, dict) and output.get("error"):
            raise RuntimeError(output["error"])
        if isinstance(output, dict) and output:
            return output
        raise RuntimeError(f"RunPod job did not complete: {status}")


def _poll_job(client: httpx.Client, job_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(_runpod_url(f"status/{job_id}"), headers=_runpod_headers())
        response.raise_for_status()
        body = response.json()
        status = body.get("status")
        if status == "COMPLETED":
            output = body.get("output") or {}
            if isinstance(output, dict) and output.get("error"):
                raise RuntimeError(output["error"])
            if not isinstance(output, dict):
                raise RuntimeError("RunPod returned a non-object output")
            return output
        if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
            raise RuntimeError(body.get("error") or f"RunPod job {status}")
        time.sleep(2)
    raise TimeoutError(f"RunPod job {job_id} timed out")


def extract_asset(
    db: Session,
    asset: Asset,
    *,
    max_pages: int | None = None,
    ocr: bool = True,
) -> Asset:
    if not asset.file_key and not asset.source_url:
        raise ValueError("asset has no file to extract")
    file_url = None
    spaces_key = None
    if settings.uses_spaces and asset.file_key:
        file_url = get_url(asset.file_key, expires=1800)
        spaces_key = None if file_url else asset.file_key
    elif asset.file_key and asset.source_url.startswith("http"):
        file_url = asset.source_url
    elif asset.source_url.startswith("http") and "drive.google.com" not in asset.source_url:
        file_url = asset.source_url
    elif settings.uses_spaces and asset.file_key:
        spaces_key = asset.file_key
    else:
        file_url = get_url(asset.file_key) if asset.file_key else asset.source_url

    try:
        payload = runpod_extract(
            file_url=file_url,
            spaces_key=spaces_key,
            max_pages=max_pages,
            ocr=ocr,
            chunks=True,
        )
    except Exception as exc:  # noqa: BLE001
        asset.extract_status = "error"
        asset.extract_error = str(exc)
        db.commit()
        db.refresh(asset)
        raise

    asset.extract_json = json.dumps(payload, ensure_ascii=False)
    asset.extract_status = "ready"
    asset.extract_error = ""
    asset.extracted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(asset)
    return asset
