from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from backend.models import GeneratedSlide
from backend.storage import read_file, save_file

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256_of(payload: Any) -> str:
    """Deterministic SHA-256 of a JSON-canonicalised payload."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _canonical_text(text: str) -> str:
    """Whitespace/case-insensitive canonical form for claim fingerprinting."""
    return " ".join((text or "").lower().split())


def fact_fingerprint(fact: Any) -> str:
    """Content fingerprint for one locked fact.

    Includes the fact's content *and* verification status so any edit to a
    locked fact — a corrected number, a re-verification — changes the claim
    hash and naturally invalidates a cached slide built on the old content.
    """
    return _sha256_of(
        {
            "fact": _canonical_text(getattr(fact, "fact", "") or ""),
            "value": _canonical_text(getattr(fact, "value", "") or ""),
            "status": (getattr(fact, "status", "") or "").strip().lower(),
        }
    )


def passage_fingerprint(passage: Mapping[str, Any]) -> str:
    """Content fingerprint for one report passage (source content, not id)."""
    return _sha256_of(
        {
            "asset_id": int(passage.get("asset_id") or 0),
            "start_page": passage.get("start_page"),
            "end_page": passage.get("end_page"),
            "text": _canonical_text(str(passage.get("text") or "")),
        }
    )


def compute_claim_hash(
    *,
    claim: str,
    module_id: str,
    template_id: str,
    tone: str,
    source_facts: Sequence[Any] = (),
    source_passages: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Deterministic, content-based identity of a *claim* to be rendered.

    The hash binds the canonical claim, its module, the chosen template/tone,
    the ids of the locked facts and report passages it draws on, *and* a
    content fingerprint of each of those sources. Keying on fingerprints (not
    just ids) means that when a locked fact's content changes the hash changes
    with it, so the shared claim cache invalidates naturally instead of serving
    a slide built from stale numbers. Two users asking for the same claim
    against the same source content get the same hash and therefore share one
    rendered slide.
    """
    facts_payload = sorted(
        (
            {
                "id": int(getattr(fact, "id", 0) or 0),
                "fingerprint": fact_fingerprint(fact),
            }
            for fact in source_facts
        ),
        key=lambda item: (item["id"], item["fingerprint"]),
    )
    passages_payload = sorted(
        (
            {
                "id": int(passage.get("asset_id") or 0),
                "start_page": passage.get("start_page"),
                "end_page": passage.get("end_page"),
                "fingerprint": passage_fingerprint(passage),
            }
            for passage in source_passages
        ),
        key=lambda item: (
            item["id"],
            str(item["start_page"]),
            str(item["end_page"]),
            item["fingerprint"],
        ),
    )
    return _sha256_of(
        {
            "v": 1,
            "claim": _canonical_text(claim),
            "module_id": (module_id or "").strip(),
            "template_id": template_id,
            "tone": tone,
            "facts": facts_payload,
            "passages": passages_payload,
        }
    )


def compute_render_hash(
    *,
    template_id: str,
    template_version: str,
    slot_values: Mapping[str, str],
    font_bundle_version: str,
    photo: Mapping[str, Any] | None = None,
) -> str:
    """Deterministic identity of the *pixels* a render will produce.

    Two renders share a hash iff they would produce the same image: same
    template version, same resolved slot copy, same photo asset/crop (none for
    the text-only templates today), and the same embedded font bundle. This is
    the content-addressed key the JPEG is stored under, so an identical render
    is never recomputed or re-uploaded.
    """
    return _sha256_of(
        {
            "v": 1,
            "template_id": template_id,
            "template_version": template_version,
            "slots": {key: slot_values[key] for key in sorted(slot_values)},
            "photo": dict(photo) if photo else {},
            "font_bundle_version": font_bundle_version,
        }
    )


def generated_slide_key(render_hash: str) -> str:
    """Stable, globally-unique, URL-safe slide key for a content digest.

    The planner's placeholder key is only unique *within one plan*; the shared
    generated-slide row (and its authenticated image route) needs a key that is
    the same for the same rendered content across users and stable over time.
    Deriving it from the (unique) render hash gives exactly that — one key per
    distinct render — and keeps the key free of the ``:`` the brand keys use so
    it drops cleanly into a URL path.
    """
    normalized = (render_hash or "").strip().lower()
    if not _HASH_RE.fullmatch(normalized):
        raise ValueError("render_hash must be a 64-character lowercase SHA-256 digest")
    return f"generated-{normalized[:24]}"


def generated_slide_file_key(render_hash: str) -> str:
    """Return the private Spaces key for a content-addressed slide render."""
    normalized = (render_hash or "").strip().lower()
    if not _HASH_RE.fullmatch(normalized):
        raise ValueError("render_hash must be a 64-character lowercase SHA-256 digest")
    return f"generated-slides/{normalized}.jpg"


def save_generated_slide_image(render_hash: str, data: bytes) -> str:
    if not data:
        raise ValueError("generated slide image is empty")
    key = generated_slide_file_key(render_hash)
    return save_file(key, data, "image/jpeg")


def generated_slide_attempt_file_key(
    generation_id: int,
    placeholder_key: str,
    attempt_number: int,
    render_hash: str,
) -> str:
    """Private, generation-scoped key for one rendered audit attempt."""
    if generation_id <= 0:
        raise ValueError("generation_id must be positive")
    if attempt_number <= 0:
        raise ValueError("attempt_number must be positive")
    normalized = (render_hash or "").strip().lower()
    if not _HASH_RE.fullmatch(normalized):
        raise ValueError("render_hash must be a 64-character lowercase SHA-256 digest")
    placeholder_digest = hashlib.sha256((placeholder_key or "").encode("utf-8")).hexdigest()[:16]
    return (
        f"generated-slide-attempts/{generation_id}/"
        f"{placeholder_digest}/attempt-{attempt_number}-{normalized[:16]}.jpg"
    )


def save_generated_slide_attempt_image(
    generation_id: int,
    placeholder_key: str,
    attempt_number: int,
    render_hash: str,
    data: bytes,
) -> str:
    if not data:
        raise ValueError("generated slide attempt image is empty")
    key = generated_slide_attempt_file_key(
        generation_id, placeholder_key, attempt_number, render_hash
    )
    return save_file(key, data, "image/jpeg")


def read_generated_slide_image(slide: GeneratedSlide) -> bytes:
    if not slide.file_key:
        raise FileNotFoundError("generated slide has no rendered image")
    return read_file(slide.file_key)
