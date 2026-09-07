"""Unit tests for the extraction helpers.

The heavy PyMuPDF/OCR path is exercised only when `fitz` is importable; the
pure-Python helpers (hashing, whitespace, chunking) always run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extractor import (  # noqa: E402
    PageResult,
    chunk_pages,
    extract_pdf,
    normalize_whitespace,
    page_hash,
)


def test_normalize_whitespace_collapses_runs():
    assert normalize_whitespace("  a\n\t b   c ") == "a b c"


def test_page_hash_is_whitespace_and_case_insensitive():
    assert page_hash("Hello   World") == page_hash("hello world")
    assert page_hash("hello") != page_hash("world")


def _page(num: int, text: str, duplicate: bool = False) -> PageResult:
    return PageResult(
        page=num,
        text=text,
        method="text",
        char_count=len(text),
        hash=page_hash(text),
        duplicate=duplicate,
    )


def test_chunk_pages_groups_by_target_words():
    pages = [_page(1, "word " * 400), _page(2, "word " * 400)]
    chunks = chunk_pages(pages, target_words=700, overlap_words=50)
    assert chunks
    assert chunks[0]["start_page"] == 1
    assert all("hash" in c for c in chunks)


def test_chunk_pages_skips_duplicates_and_empties():
    pages = [_page(1, "alpha beta"), _page(2, "", ), _page(3, "gamma", duplicate=True)]
    chunks = chunk_pages(pages, target_words=700)
    assert len(chunks) == 1
    assert "gamma" not in chunks[0]["text"]


def test_extract_pdf_reads_text_when_fitz_available(tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "sample.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Masters Union placement report 2025")
    doc.save(pdf_path)
    doc.close()

    result = extract_pdf(pdf_path, enable_ocr=False)
    assert result.page_count == 1
    assert "placement" in result.pages[0].text.lower()
    assert result.pages[0].method == "text"
