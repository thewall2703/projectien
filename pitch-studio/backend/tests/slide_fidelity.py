"""Reproducible fidelity harness for generated slide templates.

Rebuilds specific brand-deck pages from a template + values (the fixtures in
``backend/tests/manifests/pNNN.json``) and pixel-diffs the render against the
cached page image.

Fidelity is measured two ways, because a faithful *structural* reconstruction
cannot yet be a pixel-perfect one:

* **Full-frame MAD** — mean absolute difference over the whole 1920x1080 frame,
  0-255 per channel. Reported as an informational baseline. Some source pages
  contain photography or optional cards the clean template intentionally does
  not reproduce, and the licensed display serif is not bundled.
* **Structural MAD** — the same metric restricted to a handful of background /
  grid *structural regions* that both images share (empty of brush, text and
  logo). This is the actionable gate: it verifies the frame, background,
  grid and rails line up, and is stable rather than flaky.

Run directly to (re)generate reconstructions, side-by-side comparisons and a
masked structural heatmap under ``.tmp/slide_fidelity`` and print the metrics::

    python -m backend.tests.slide_fidelity
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from backend.config import FILES_DIR
from backend.pipeline.slide_render import SLIDE_HEIGHT, SLIDE_WIDTH
from backend.pipeline.slide_templates import (
    campus_photo_manifest,
    programme_list_manifest,
    section_divider_manifest,
    stat_manifest,
)

MANIFESTS_DIR = Path(__file__).resolve().parent / "manifests"
FIXTURES = (
    "p019.json",
    "p052.json",
    "p058.json",
    "p074.json",
    "p084.json",
)


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((MANIFESTS_DIR / name).read_text())


def reference_path(fixture: dict[str, Any]) -> Path:
    return Path(FILES_DIR) / fixture["reference_image"]


def manifest_for(fixture: dict[str, Any]):
    template = fixture["template"]
    variant = fixture.get("variant", "light")
    if template == "section-divider":
        return section_divider_manifest(variant)
    if template == "stat":
        return stat_manifest(variant)
    if template == "campus-photo":
        return campus_photo_manifest()
    if template == "programme-list":
        return programme_list_manifest(variant)
    raise ValueError(f"unknown template: {template}")


def _photos_for(fixture: dict[str, Any]) -> dict[str, bytes] | None:
    """Resolve the fixture's photo slots to bytes.

    A fixture declares photos as ``{"slot": "reference"}`` (feed the cached page
    itself back into the full-bleed photo slot, so the harness measures the
    template's *furniture/scrim/text* fidelity against a photo we already trust)
    or ``{"slot": "<path-relative-to-FILES_DIR>"}`` for an explicit approved
    asset. No base64 lives in the fixture; bytes come from local files only.
    """
    spec = fixture.get("photos")
    if not spec:
        return None
    photos: dict[str, bytes] = {}
    for slot, ref in spec.items():
        if ref == "reference":
            photos[slot] = reference_path(fixture).read_bytes()
        else:
            photos[slot] = (Path(FILES_DIR) / ref).read_bytes()
    return photos


def _to_rgb_array(img: Image.Image) -> np.ndarray:
    if img.size != (SLIDE_WIDTH, SLIDE_HEIGHT):
        img = img.resize((SLIDE_WIDTH, SLIDE_HEIGHT))
    return np.asarray(img.convert("RGB"), dtype=np.int16)


def _region_mask(regions: list[list[float]]) -> np.ndarray:
    mask = np.zeros((SLIDE_HEIGHT, SLIDE_WIDTH), dtype=bool)
    for x, y, w, h in regions:
        x0, y0 = int(x), int(y)
        x1, y1 = int(x + w), int(y + h)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(SLIDE_WIDTH, x1), min(SLIDE_HEIGHT, y1)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = True
    return mask


@dataclass
class FidelityMetrics:
    page: int
    full_mad: float
    structural_mad: float
    structural_pixels: int
    structural_mad_max: float
    text_mad: float
    text_mad_max: float

    @property
    def passed(self) -> bool:
        # Only the structural frame is a hard gate; the text region is bounded
        # loosely (catches a blank/garbage render) but is not a fidelity gate
        # while the serif is a fallback.
        return (
            self.structural_mad <= self.structural_mad_max
            and self.text_mad <= self.text_mad_max
        )

    def as_row(self) -> str:
        flag = "PASS" if self.passed else "FAIL"
        return (
            f"p{self.page:<3d}  full_MAD={self.full_mad:6.2f}  "
            f"structural_MAD={self.structural_mad:6.2f} "
            f"(<= {self.structural_mad_max:.1f}, {self.structural_pixels} px)  "
            f"text_MAD={self.text_mad:6.2f} (<= {self.text_mad_max:.0f}, informational)  [{flag}]"
        )


def compare(reconstruction: bytes, fixture: dict[str, Any]) -> FidelityMetrics:
    """Compare a reconstruction JPEG against the fixture's cached page."""
    recon = _to_rgb_array(Image.open(io.BytesIO(reconstruction)))
    ref = _to_rgb_array(Image.open(reference_path(fixture)))

    diff = np.abs(recon - ref)
    full_mad = float(diff.mean())

    cmp = fixture["comparison"]
    mask = _region_mask(cmp["structural_regions"])
    struct_diff = diff[mask]
    structural_mad = float(struct_diff.mean()) if struct_diff.size else 0.0

    text_region = cmp.get("text_region")
    if text_region:
        tmask = _region_mask([text_region])
        text_mad = float(diff[tmask].mean()) if tmask.any() else 0.0
    else:
        text_mad = 0.0

    return FidelityMetrics(
        page=int(fixture["source_page"]),
        full_mad=full_mad,
        structural_mad=structural_mad,
        structural_pixels=int(mask.sum()),
        structural_mad_max=float(cmp.get("structural_mad_max", 10.0)),
        text_mad=text_mad,
        text_mad_max=float(cmp.get("text_region_mad_max", 255.0)),
    )


def _render(fixture: dict[str, Any]) -> bytes:
    # Imported lazily so the harness can be imported without a browser.
    from backend.pipeline.slide_render import get_renderer

    return (
        get_renderer()
        .render(manifest_for(fixture), fixture["values"], photos=_photos_for(fixture))
        .jpeg
    )


def main() -> int:
    from backend.pipeline.slide_render import shutdown_renderer

    out = Path(FILES_DIR).parent.parent.parent / ".tmp" / "slide_fidelity"
    out.mkdir(parents=True, exist_ok=True)
    print(f"Writing artifacts to {out}\n")
    ok = True
    try:
        for name in FIXTURES:
            fixture = load_fixture(name)
            recon = _render(fixture)
            metrics = compare(recon, fixture)
            ok = ok and metrics.passed

            page = metrics.page
            (out / f"p{page:03d}_reconstruction.jpg").write_bytes(recon)

            recon_img = Image.open(io.BytesIO(recon)).convert("RGB")
            ref_img = Image.open(reference_path(fixture)).convert("RGB")
            side = Image.new("RGB", (SLIDE_WIDTH, SLIDE_HEIGHT * 2), "white")
            side.paste(ref_img.resize((SLIDE_WIDTH, SLIDE_HEIGHT)), (0, 0))
            side.paste(recon_img, (0, SLIDE_HEIGHT))
            side.save(out / f"p{page:03d}_reference_vs_reconstruction.jpg", quality=88)

            # Structural heatmap: diff shown only inside compared regions.
            recon_a = _to_rgb_array(recon_img)
            ref_a = _to_rgb_array(ref_img)
            d = np.abs(recon_a - ref_a).mean(axis=2)
            mask = _region_mask(fixture["comparison"]["structural_regions"])
            heat = np.zeros((SLIDE_HEIGHT, SLIDE_WIDTH), dtype=np.uint8)
            heat[mask] = np.clip(d[mask] * 6, 0, 255).astype(np.uint8)
            Image.fromarray(heat).save(out / f"p{page:03d}_structural_diff.png")

            print(metrics.as_row())
    finally:
        shutdown_renderer()
    print("\nOverall:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
