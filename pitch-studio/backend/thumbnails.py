from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from io import BytesIO

from backend.database import SessionLocal, ensure_schema
from backend.models import Asset
from backend.storage import file_exists, read_file, save_file

THUMBNAIL_SIZE = (640, 480)
THUMBNAIL_QUALITY = 76


def thumbnail_key(asset: Asset) -> str:
    version = int(asset.synced_at.timestamp()) if asset.synced_at else 0
    return f"thumbnails/assets/{asset.id}-{version}.jpg"


def thumbnail_jpeg(data: bytes) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source)
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.thumbnail(THUMBNAIL_SIZE)
        output = BytesIO()
        image.save(output, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
        return output.getvalue()


def ensure_thumbnail(asset: Asset, force: bool = False) -> str:
    if not asset.file_key or not (asset.content_type or "").startswith("image/"):
        raise ValueError("Thumbnail source must be a stored image")
    key = thumbnail_key(asset)
    if not force and file_exists(key):
        return key
    data = thumbnail_jpeg(read_file(asset.file_key))
    save_file(key, data, "image/jpeg")
    return key


def ensure_thumbnails(
    assets: Iterable[Asset],
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, list[tuple[int, str]]]:
    rows = list(assets)
    created = 0
    errors: list[tuple[int, str]] = []
    for index, asset in enumerate(rows, start=1):
        try:
            key = thumbnail_key(asset)
            existed = file_exists(key)
            if not existed:
                ensure_thumbnail(asset, force=True)
            if not existed:
                created += 1
        except Exception as exc:  # one bad image must not fail the whole set
            errors.append((asset.id, str(exc)))
        if on_progress is not None:
            on_progress(index, len(rows))
    return created, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Create persistent previews for stored images")
    parser.add_argument("--force", action="store_true", help="Regenerate existing thumbnails")
    args = parser.parse_args()

    ensure_schema()
    db = SessionLocal()
    try:
        assets = (
            db.query(Asset)
            .filter(
                Asset.file_status == "stored",
                Asset.file_key != "",
                Asset.content_type.startswith("image/"),
            )
            .order_by(Asset.id)
            .all()
        )
        failures = 0
        for index, asset in enumerate(assets, start=1):
            try:
                ensure_thumbnail(asset, force=args.force)
                print(f"[{index}/{len(assets)}] asset {asset.id}: ready", flush=True)
            except Exception as exc:
                failures += 1
                print(f"[{index}/{len(assets)}] asset {asset.id}: {exc}", flush=True)
        print(f"Thumbnails complete: {len(assets) - failures} ready, {failures} failed")
        return 1 if failures else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
