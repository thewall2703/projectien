"""PDF text extraction with OCR fallback for the Pitch Studio document worker.

Pure-Python core so it can be unit tested without RunPod. The heavy deps
(PyMuPDF, pytesseract) are imported lazily so importing this module never fails
in environments that only need the helpers (hashing, chunking).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

WHITESPACE_RE = re.compile(r"\s+")

# A page with fewer than this many extractable characters is treated as
# image-only and sent to OCR.
DEFAULT_MIN_CHARS = 40
# Render DPI for OCR. 300 is a good balance of accuracy vs. memory.
DEFAULT_OCR_DPI = 300


def normalize_whitespace(text: str) -> str:
    return WHITESPACE_RE.sub(" ", (text or "").strip())


def page_hash(text: str) -> str:
    return hashlib.sha256(normalize_whitespace(text).lower().encode("utf-8")).hexdigest()


@dataclass
class PageResult:
    page: int
    text: str
    method: str  # "text" | "ocr" | "empty"
    char_count: int
    hash: str
    duplicate: bool = False


@dataclass
class ExtractResult:
    source_name: str
    page_count: int
    ocr_pages: int
    duplicate_pages: int
    char_count: int
    pages: list[PageResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def chunk_pages(
    pages: list[PageResult],
    target_words: int = 700,
    overlap_words: int = 100,
) -> list[dict[str, Any]]:
    """Group consecutive, non-duplicate page text into retrieval-sized chunks."""
    chunks: list[dict[str, Any]] = []
    words: list[str] = []
    start_page = None
    end_page = None

    def flush() -> None:
        nonlocal words, start_page, end_page
        if words and start_page is not None:
            text = " ".join(words)
            chunks.append(
                {
                    "text": text,
                    "start_page": start_page,
                    "end_page": end_page,
                    "hash": page_hash(text),
                }
            )
        keep = words[-overlap_words:] if overlap_words else []
        words = keep[:]
        start_page = end_page

    for page in pages:
        if page.duplicate or not page.text.strip():
            continue
        if start_page is None:
            start_page = page.page
        end_page = page.page
        words.extend(page.text.split())
        if len(words) >= target_words:
            flush()
    if words and start_page is not None:
        text = " ".join(words)
        chunks.append(
            {
                "text": text,
                "start_page": start_page,
                "end_page": end_page,
                "hash": page_hash(text),
            }
        )
    return chunks


def _ocr_page(page, dpi: int, lang: str) -> str:
    import pytesseract  # lazy
    from PIL import Image  # lazy

    zoom = dpi / 72.0
    import fitz  # lazy

    matrix = fitz.Matrix(zoom, zoom)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    return pytesseract.image_to_string(image, lang=lang)


def extract_pdf(
    path: str | Path,
    *,
    ocr_lang: str = "eng",
    min_chars: int = DEFAULT_MIN_CHARS,
    ocr_dpi: int = DEFAULT_OCR_DPI,
    max_pages: int | None = None,
    enable_ocr: bool = True,
) -> ExtractResult:
    import fitz  # lazy import so unit tests can skip it

    path = Path(path)
    document = fitz.open(path)
    seen: set[str] = set()
    pages: list[PageResult] = []
    ocr_pages = 0
    duplicate_pages = 0
    total_chars = 0
    try:
        for index, page in enumerate(document):
            if max_pages is not None and index >= max_pages:
                break
            raw = page.get_text("text") or ""
            text = normalize_whitespace(raw)
            method = "text"
            if len(text) < min_chars and enable_ocr:
                try:
                    ocr_text = normalize_whitespace(_ocr_page(page, ocr_dpi, ocr_lang))
                except Exception:  # noqa: BLE001 - OCR is best-effort
                    ocr_text = ""
                if len(ocr_text) > len(text):
                    text = ocr_text
                    method = "ocr"
                    ocr_pages += 1
            if not text:
                method = "empty"
            digest = page_hash(text) if text else ""
            duplicate = bool(digest) and digest in seen
            if digest:
                seen.add(digest)
            if duplicate:
                duplicate_pages += 1
            total_chars += len(text)
            pages.append(
                PageResult(
                    page=index + 1,
                    text=text,
                    method=method,
                    char_count=len(text),
                    hash=digest,
                    duplicate=duplicate,
                )
            )
        return ExtractResult(
            source_name=path.name,
            page_count=len(pages),
            ocr_pages=ocr_pages,
            duplicate_pages=duplicate_pages,
            char_count=total_chars,
            pages=pages,
        )
    finally:
        document.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract text (with OCR fallback) from a PDF")
    parser.add_argument("pdf", help="Path to a local PDF")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--chunks", action="store_true", help="Also print retrieval chunks")
    args = parser.parse_args()
    result = extract_pdf(
        args.pdf,
        enable_ocr=not args.no_ocr,
        max_pages=args.max_pages or None,
    )
    output = result.to_dict()
    if args.chunks:
        output["chunks"] = chunk_pages(result.pages)
    print(json.dumps(output, ensure_ascii=False)[:4000])


if __name__ == "__main__":
    main()
