"""RunPod serverless handler for PDF text extraction + OCR.

Input event (job["input"]) supports:
  {
    "file_url":   "https://...",       # presigned URL (Spaces/S3/Drive) OR
    "spaces_key": "reports/foo.pdf",   # object key in the configured bucket
    "result_key": "extracted/foo.json",# optional: upload JSON result to Spaces
    "ocr": true,                        # default true
    "ocr_lang": "eng",                 # default "eng"
    "min_chars": 40,                    # OCR threshold
    "max_pages": null,                  # cap pages (debug)
    "chunks": false,                    # also return retrieval chunks
    "max_bytes": 2147483648             # download cap (default 2 GB)
  }

Storage config comes from env (matches the DigitalOcean Spaces setup):
  SPACES_ENDPOINT, SPACES_REGION, SPACES_BUCKET,
  SPACES_KEY / SPACES_SECRET  (or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

from extractor import chunk_pages, extract_pdf

DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


def _spaces_client():
    import boto3  # lazy

    endpoint = os.environ["SPACES_ENDPOINT"]
    region = os.environ.get("SPACES_REGION", "us-east-1")
    key = os.environ.get("SPACES_KEY") or os.environ.get("AWS_ACCESS_KEY_ID")
    secret = os.environ.get("SPACES_SECRET") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
    )


def _download_url(url: str, dest: Path, max_bytes: int) -> None:
    written = 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=300.0) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"file exceeds max_bytes ({max_bytes})")
                handle.write(chunk)


def _download_spaces(key: str, dest: Path) -> None:
    client = _spaces_client()
    bucket = os.environ["SPACES_BUCKET"]
    client.download_file(bucket, key, str(dest))


def _upload_spaces(key: str, payload: dict[str, Any]) -> str:
    client = _spaces_client()
    bucket = os.environ["SPACES_BUCKET"]
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    return key


def process(job_input: dict[str, Any]) -> dict[str, Any]:
    file_url = job_input.get("file_url")
    spaces_key = job_input.get("spaces_key")
    if not file_url and not spaces_key:
        return {"error": "provide 'file_url' or 'spaces_key'"}

    max_bytes = int(job_input.get("max_bytes") or DEFAULT_MAX_BYTES)
    enable_ocr = bool(job_input.get("ocr", True))
    ocr_lang = job_input.get("ocr_lang", "eng")
    min_chars = int(job_input.get("min_chars", 40))
    max_pages = job_input.get("max_pages") or None
    want_chunks = bool(job_input.get("chunks", False))

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "input.pdf"
        try:
            if file_url:
                _download_url(file_url, pdf_path, max_bytes)
            else:
                _download_spaces(spaces_key, pdf_path)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"download failed: {exc}"}

        try:
            result = extract_pdf(
                pdf_path,
                ocr_lang=ocr_lang,
                min_chars=min_chars,
                max_pages=max_pages,
                enable_ocr=enable_ocr,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"extraction failed: {exc}"}

    payload = result.to_dict()
    if spaces_key:
        payload["source_name"] = spaces_key
    if want_chunks:
        payload["chunks"] = chunk_pages(result.pages)

    result_key = job_input.get("result_key")
    if result_key:
        try:
            payload["result_key"] = _upload_spaces(result_key, payload)
        except Exception as exc:  # noqa: BLE001
            payload["upload_error"] = str(exc)

    return payload


def handler(job: dict[str, Any]) -> dict[str, Any]:
    return process(job.get("input") or {})


if __name__ == "__main__":
    import runpod  # lazy so local imports/tests don't require it

    runpod.serverless.start({"handler": handler})
