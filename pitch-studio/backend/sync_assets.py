from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy.exc import OperationalError, PendingRollbackError
from sqlalchemy.orm import Session

from backend.config import REPO_ROOT
from backend.database import SessionLocal, engine, ensure_schema
from backend.models import Asset, Base, utc_now
from backend.storage import save_file, save_file_from_path
from backend.youtube_apify import download_caption_text, download_youtube, is_youtube_url

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

USER_AGENT = "pitch-studio/1.0 (+https://mastersunion.org)"
FOLDER_RE = re.compile(r"/folders/([a-zA-Z0-9_-]+)")
FILE_RE = re.compile(r"/file/d/([a-zA-Z0-9_-]+)")
DOCUMENT_SUFFIXES = {".pdf", ".pptx", ".ppt", ".docx", ".doc"}
DOCUMENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}
FOLDER_CHILD_CAP = 80
FOLDER_MIME = "application/vnd.google-apps.folder"
SKIP_FOLDER_MARKERS = ("font",)
FOLDER_FILE_MAX_BYTES = 80 * 1024 * 1024
PREVIEW_FILE_MAX_BYTES = 700 * 1024 * 1024
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv"}
YOUTUBE_FILE_MAX_BYTES = 5 * 1024 * 1024 * 1024


def classify_link(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return "gap"
    lowered = text.lower()
    if "figma.com" in lowered:
        return "external"
    if "drive.google.com" in lowered or "docs.google.com" in lowered:
        if "/folders/" in lowered:
            return "drive_folder"
        return "drive_file"
    if is_youtube_url(text):
        return "youtube"
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "cdn"
    return "gap"


def extract_folder_id(url: str) -> str | None:
    match = FOLDER_RE.search(url or "")
    return match.group(1) if match else None


def is_downloadable_file(name: str, mime: str) -> bool:
    suffix = Path(name or "").suffix.lower()
    if mime.startswith("application/vnd.google-apps"):
        return False
    if mime in DOCUMENT_TYPES or suffix in DOCUMENT_SUFFIXES:
        return True
    if suffix in IMAGE_SUFFIXES or (mime or "").startswith("image/"):
        return True
    return False


def should_descend_folder(name: str) -> bool:
    lowered = (name or "").lower()
    return not any(marker in lowered for marker in SKIP_FOLDER_MARKERS)


def find_local_override(asset: Asset) -> Path | None:
    if "brand deck" in (asset.title or "").lower():
        matches = sorted(REPO_ROOT.glob("Brand Deck*.pdf"))
        if matches:
            return matches[0]
    return None


def ingest_local_file(asset: Asset, path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(path)
    data = path.read_bytes()
    _store_bytes(asset, data, path.name, _content_type(path.name, "", str(path)), str(path))
    asset.notes = f"Imported local file {path.name}"


def extract_drive_file_id(url: str) -> str | None:
    match = FILE_RE.search(url or "")
    if match:
        return match.group(1)
    from drive_auth import extract_file_id

    return extract_file_id(url)


def asset_slug(title: str, asset_id: int) -> str:
    cleaned = re.sub(r"[^\w\s\-]+", "", title, flags=re.UNICODE)
    cleaned = re.sub(r"[\s_]+", "-", cleaned).strip("-").lower()[:60] or "report"
    return f"{cleaned}-{asset_id}"


def _extension(name: str, content_type: str, url: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in DOCUMENT_SUFFIXES or suffix in IMAGE_SUFFIXES or suffix in VIDEO_SUFFIXES:
        return suffix
    if "pdf" in (content_type or "") or (url or "").lower().endswith(".pdf"):
        return ".pdf"
    if "presentation" in (content_type or "") or (url or "").lower().endswith(".pptx"):
        return ".pptx"
    if "mp4" in (content_type or "") or (url or "").lower().endswith(".mp4"):
        return ".mp4"
    return ".bin"


def _content_type(name: str, header: str, url: str) -> str:
    if header and header.split(";")[0].strip() in DOCUMENT_TYPES:
        return header.split(";")[0].strip()
    suffix = Path(name or urlparse(url).path).suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix, header.split(";")[0].strip() if header else "application/octet-stream")


def _store_bytes(asset: Asset, data: bytes, filename: str, content_type: str, source_url: str) -> None:
    ext = _extension(filename, content_type, source_url)
    key = f"library/{asset.type}s/{asset_slug(asset.title, asset.id)}{ext}"
    stored = save_file(key, data, content_type)
    _mark_stored(asset, stored, content_type)


def _store_path(asset: Asset, path: Path, filename: str, content_type: str) -> None:
    ext = _extension(filename, content_type, filename)
    key = f"library/{asset.type}s/{asset_slug(asset.title, asset.id)}{ext}"
    stored = save_file_from_path(key, path, content_type)
    _mark_stored(asset, stored, content_type)


def _mark_stored(asset: Asset, stored: str, content_type: str) -> None:
    asset.file_key = stored
    asset.content_type = content_type
    asset.file_status = "stored"
    asset.sync_error = ""
    asset.synced_at = utc_now()
    asset.url = f"/api/assets/{asset.id}/file"
    asset.status = "exists"


def _mark(asset: Asset, status: str, error: str = "") -> None:
    asset.file_status = status
    asset.sync_error = error
    asset.synced_at = utc_now()
    if status == "gap":
        asset.status = "gap"
    if status == "external" and asset.source_url:
        asset.url = asset.source_url


def _download_url_to_path(url: str, dest: Path, limit: int = YOUTUBE_FILE_MAX_BYTES) -> str:
    headers = {"User-Agent": USER_AGENT}
    if settings_token := _apify_media_headers():
        headers.update(settings_token)
    with httpx.Client(timeout=httpx.Timeout(30.0, read=600.0), follow_redirects=True, headers=headers) as client:
        with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise RuntimeError(f"Media download failed ({response.status_code}) for {url}")
            total = 0
            with dest.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise RuntimeError(f"YouTube file exceeds {limit // (1024 * 1024)} MB")
                    handle.write(chunk)
            if total == 0:
                raise RuntimeError("Empty YouTube download")
            return response.headers.get("content-type", "video/mp4")


def _apify_media_headers() -> dict[str, str]:
    from backend.config import settings

    token = (settings.apify_token or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def sync_youtube(asset: Asset) -> str:
    result = download_youtube(asset.source_url)
    filename = Path(result["media_key"]).name or f"{asset_slug(asset.title, asset.id)}.mp4"
    suffix = Path(filename).suffix.lower() or ".mp4"
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / filename
        header = _download_url_to_path(result["media_url"], dest)
        content_type = _content_type(filename, header, filename)
        if suffix not in VIDEO_SUFFIXES:
            filename = f"{Path(filename).stem}.mp4"
            content_type = "video/mp4"
        _store_path(asset, dest, filename, content_type)
    transcript = download_caption_text(result.get("subtitles") or [])
    resolution = result.get("resolution") or "highest available"
    notice = result.get("quality_notice") or ""
    asset.notes = f"Downloaded via Apify at {resolution}"
    if transcript:
        asset.notes = f"{asset.notes}; transcript captured from captions"
    if notice:
        asset.notes = f"{asset.notes}. {notice}"
    return transcript


def sync_cdn(asset: Asset) -> None:
    url = asset.source_url
    with httpx.Client(timeout=60.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(url)
    if response.status_code >= 400:
        raise RuntimeError(f"CDN download failed ({response.status_code}) for {url}")
    content_type = _content_type(Path(urlparse(url).path).name, response.headers.get("content-type", ""), url)
    _store_bytes(asset, response.content, Path(urlparse(url).path).name, content_type, url)


def _drive_creds():
    """Load an existing Drive token. Never open a browser from the sync job."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from drive_auth import SCOPES

    token_path = REPO_ROOT / ".token.json"
    if not token_path.exists():
        raise RuntimeError("Drive OAuth token missing (.token.json). Sign in via the transcription app first.")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds
    raise RuntimeError("Drive OAuth token expired. Refresh it via the transcription app.")


def _read_limited(response, name: str, limit: int = 700 * 1024 * 1024) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise RuntimeError(f"{name} exceeds {limit // (1024 * 1024)} MB")
    if total == 0:
        raise RuntimeError(f"Empty download for {name}")
    return b"".join(chunks)


def _download_public_drive(file_id: str) -> tuple[bytes, str]:
    from drive_auth import get_public_file_metadata, open_public_drive_stream

    meta = get_public_file_metadata(file_id, timeout=15)
    name = meta.get("name") or f"{file_id}.pdf"
    response, session = open_public_drive_stream(file_id, timeout=30)
    try:
        return _read_limited(response, name), name
    finally:
        response.close()
        session.close()


def _download_oauth_drive(file_id: str) -> tuple[bytes, str, str]:
    from google.auth.transport.requests import AuthorizedSession

    from drive_auth import build_drive_service, get_file_metadata

    creds = _drive_creds()
    service = build_drive_service(creds)
    meta = get_file_metadata(service, file_id)
    name = meta.get("name") or f"{file_id}.pdf"
    mime = meta.get("mimeType") or ""
    if mime.startswith("application/vnd.google-apps"):
        raise RuntimeError(f"Skipping Google-native file {name}")
    session = AuthorizedSession(creds)
    try:
        response = session.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
            stream=True,
            timeout=(30, 600),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OAuth Drive download failed ({response.status_code}) for {name}")
        return _read_limited(response, name), name, mime
    finally:
        session.close()


def sync_drive_file(asset: Asset, file_id: str | None = None) -> None:
    file_id = file_id or extract_drive_file_id(asset.source_url)
    if not file_id:
        raise RuntimeError(f"Could not parse Drive file id from {asset.source_url}")
    errors: list[str] = []
    try:
        data, name = _download_public_drive(file_id)
        _store_bytes(asset, data, name, _content_type(name, "", asset.source_url), asset.source_url)
        return
    except Exception as exc:  # noqa: BLE001
        errors.append(f"public: {exc}")
        print(f"  public download failed for {asset.title}: {exc}", flush=True)
    print(f"  trying OAuth download for {asset.title}", flush=True)
    try:
        data, name, mime = _download_oauth_drive(file_id)
        _store_bytes(asset, data, name, _content_type(name, mime, asset.source_url), asset.source_url)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"oauth: {exc}")
        raise RuntimeError("; ".join(errors)) from exc


def _list_folder_page(service, folder_id: str) -> list[dict]:
    files: list[dict] = []
    page_token = None
    while True:
        result = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken,files(id,name,mimeType,size)",
                pageSize=100,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files.extend(result.get("files") or [])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return files


def list_drive_documents(service, folder_id: str, cap: int = FOLDER_CHILD_CAP) -> list[dict]:
    documents: list[dict] = []
    queue: list[tuple[str, str]] = [(folder_id, "")]
    seen: set[str] = set()
    while queue and len(documents) < cap:
        current_id, prefix = queue.pop(0)
        if current_id in seen:
            continue
        seen.add(current_id)
        for child in _list_folder_page(service, current_id):
            name = child.get("name") or ""
            mime = child.get("mimeType") or ""
            rel = f"{prefix}/{name}" if prefix else name
            if mime == FOLDER_MIME:
                if should_descend_folder(name):
                    queue.append((child["id"], rel))
                continue
            if not is_downloadable_file(name, mime):
                continue
            documents.append({**child, "relpath": rel})
            if len(documents) >= cap:
                break
    return documents


def sync_drive_folder(db: Session, asset: Asset) -> int:
    from drive_auth import build_drive_service

    folder_id = extract_folder_id(asset.source_url)
    if not folder_id:
        raise RuntimeError(f"Could not parse Drive folder id from {asset.source_url}")
    creds = _drive_creds()
    service = build_drive_service(creds)
    children = list_drive_documents(service, folder_id)
    created = 0
    stored_children = 0
    for child in children:
        name = child.get("relpath") or child.get("name") or ""
        title = f"{asset.title} — {name}"
        existing = db.query(Asset).filter(Asset.type == asset.type, Asset.title == title).first()
        if existing is None:
            existing = Asset(
                type=asset.type,
                title=title,
                source_url=f"https://drive.google.com/file/d/{child['id']}/view",
                url=f"https://drive.google.com/file/d/{child['id']}/view",
                module_ids=asset.module_ids,
                matrix_ref=asset.matrix_ref,
                audiences=asset.audiences,
                status="exists",
                file_status="pending",
                notes=f"From folder {asset.title}",
            )
            db.add(existing)
            db.flush()
            created += 1
        if existing.file_status == "stored" and existing.file_key:
            stored_children += 1
            continue
        size = int(child.get("size") or 0)
        size_cap = PREVIEW_FILE_MAX_BYTES if "preview" in name.lower() else FOLDER_FILE_MAX_BYTES
        if size > size_cap:
            print(f"  skip {name} ({size // (1024 * 1024)} MB)", flush=True)
            _mark(existing, "error", f"Skipped {name}: {size // (1024 * 1024)} MB exceeds folder cap")
            existing.status = "exists"
            continue
        print(f"  downloading {name}", flush=True)
        sync_drive_file(existing, child["id"])
        stored_children += 1
    if stored_children:
        _mark(asset, "stored")
        asset.notes = f"Folder with {stored_children} stored file(s)"
        asset.status = "exists"
    else:
        _mark(asset, "gap", "Folder had no downloadable documents")
    return created


def sync_one(db: Session, asset: Asset, force: bool = False) -> str:
    if asset.file_status == "stored" and asset.file_key and not force:
        return "skipped"
    kind = classify_link(asset.source_url)
    try:
        local = find_local_override(asset)
        if local:
            ingest_local_file(asset, local)
            return "stored"
        if kind == "gap":
            _mark(asset, "gap")
            return "gap"
        if kind == "external":
            _mark(asset, "external")
            return "external"
        if kind == "youtube":
            sync_youtube(asset)
            return "stored"
        if kind == "cdn":
            sync_cdn(asset)
            return "stored"
        if kind == "drive_file":
            sync_drive_file(asset)
            return "stored"
        if kind == "drive_folder":
            sync_drive_folder(db, asset)
            return asset.file_status
        _mark(asset, "gap", f"Unsupported link type: {kind}")
        return "gap"
    except Exception as exc:  # noqa: BLE001
        _mark(asset, "error", str(exc))
        return "error"


def run_sync(
    types: list[str] | None = None,
    force: bool = False,
    db: Session | None = None,
    titles: list[str] | None = None,
) -> dict[str, int]:
    close = False
    if db is None:
        Base.metadata.create_all(bind=engine)
        ensure_schema()
        db = SessionLocal()
        close = True
    query = db.query(Asset)
    if types:
        query = query.filter(Asset.type.in_(types))
    assets = query.order_by(Asset.id).all()
    if titles:
        needles = [title.lower() for title in titles]
        assets = [
            asset
            for asset in assets
            if any(needle in asset.title.lower() for needle in needles)
        ]
    counts: Counter[str] = Counter()
    try:
        for asset in assets:
            print(f"Syncing {asset.type} {asset.id}: {asset.title}", flush=True)
            result = sync_one(db, asset, force=force)
            last: Exception | None = None
            for attempt in range(8):
                try:
                    db.commit()
                    counts[result] += 1
                    print(f"  -> {result}", flush=True)
                    last = None
                    break
                except (OperationalError, PendingRollbackError) as exc:
                    last = exc
                    db.rollback()
                    time.sleep(0.5 * (attempt + 1))
                    asset = db.get(Asset, asset.id) or asset
                    if result != "skipped":
                        result = sync_one(db, asset, force=force)
            if last:
                raise last
        return dict(counts)
    except Exception:
        db.rollback()
        raise
    finally:
        if close:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Download and store Pitch Studio library files")
    parser.add_argument("--types", default="report", help="Comma-separated asset types")
    parser.add_argument("--titles", default="", help="Comma-separated title substrings")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    types = [part.strip() for part in args.types.split(",") if part.strip()]
    titles = [part.strip() for part in args.titles.split(",") if part.strip()]
    counts = run_sync(types=types, force=args.force, titles=titles or None)
    print("Sync complete:", ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "nothing")


if __name__ == "__main__":
    main()
