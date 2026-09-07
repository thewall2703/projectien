"""Google Drive helpers: public link streaming + optional OAuth."""

from __future__ import annotations

import html
import io
import re
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

DRIVE_ID_PATTERNS = [
    re.compile(r"/file/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]+)"),
    re.compile(r"/open\?id=([a-zA-Z0-9_-]+)"),
    re.compile(r"/uc\?id=([a-zA-Z0-9_-]+)"),
    re.compile(r"^([a-zA-Z0-9_-]{20,})$"),
]

CONFIRM_PATTERNS = [
    re.compile(r"confirm=([0-9A-Za-z_-]+)"),
    re.compile(r'"downloadWarning"\s*,\s*"([^"]+)"'),
    re.compile(r"name=\"confirm\"\s+value=\"([^\"]+)\""),
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def extract_file_id(url_or_id: str) -> Optional[str]:
    """Extract a Google Drive file ID from a URL or bare ID."""
    value = (url_or_id or "").strip()
    if not value:
        return None
    for pattern in DRIVE_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return None


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _filename_from_content_disposition(header: str | None) -> Optional[str]:
    if not header:
        return None
    match = re.search(r"filename\*=UTF-8''([^;]+)", header, flags=re.I)
    if match:
        from urllib.parse import unquote

        return unquote(match.group(1)).strip().strip('"')
    match = re.search(r'filename="([^"]+)"', header, flags=re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r"filename=([^;]+)", header, flags=re.I)
    if match:
        return match.group(1).strip().strip('"')
    return None


def _confirm_token(response: requests.Response) -> Optional[str]:
    for key, value in response.cookies.items():
        if key.startswith("download_warning") or key == "download_warning":
            return value
    # Large-file interstitial pages embed a confirm token in HTML.
    text_sample = ""
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "text/html" in content_type:
        # Only peek at the start; callers stream the rest separately.
        text_sample = response.text[:200_000]
    for pattern in CONFIRM_PATTERNS:
        match = pattern.search(text_sample)
        if match:
            return match.group(1)
    return None


def get_public_file_metadata(file_id: str, timeout: int = 45) -> dict:
    """Resolve filename for a file shared as 'anyone with the link'."""
    session = _session()
    view_url = f"https://drive.google.com/file/d/{file_id}/view"
    try:
        resp = session.get(view_url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            title_match = re.search(r"<title>(.*?)</title>", resp.text, flags=re.I | re.S)
            if title_match:
                title = html.unescape(title_match.group(1)).strip()
                title = re.sub(r"\s*-\s*Google Drive\s*$", "", title, flags=re.I).strip()
                if title and title.lower() not in {"google drive", "error 404 (not found)!!1"}:
                    return {"id": file_id, "name": title, "access": "public"}
    except requests.RequestException:
        pass

    # Fallback: probe the download endpoint headers (no full download).
    download_url = "https://drive.google.com/uc"
    try:
        with session.get(
            download_url,
            params={"id": file_id, "export": "download"},
            stream=True,
            timeout=timeout,
        ) as resp:
            name = _filename_from_content_disposition(resp.headers.get("Content-Disposition"))
            if name:
                resp.close()
                return {"id": file_id, "name": name, "access": "public"}
            token = _confirm_token(resp)
            resp.close()
            if token:
                with session.get(
                    download_url,
                    params={"id": file_id, "export": "download", "confirm": token},
                    stream=True,
                    timeout=timeout,
                ) as resp2:
                    name = _filename_from_content_disposition(resp2.headers.get("Content-Disposition"))
                    resp2.close()
                    if name:
                        return {"id": file_id, "name": name, "access": "public"}
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not access public Drive file {file_id}. "
            "Confirm sharing is 'Anyone with the link'."
        ) from exc

    return {"id": file_id, "name": f"video-{file_id}", "access": "public"}


def open_public_drive_stream(file_id: str, timeout: int = 120) -> tuple[requests.Response, requests.Session]:
    """
    Open a streaming HTTP response for a publicly shared Drive file.

    Caller must close the response (and preferably the session) when done.
    """
    session = _session()
    download_url = "https://drive.google.com/uc"
    params: dict[str, str] = {"id": file_id, "export": "download"}
    resp = session.get(download_url, params=params, stream=True, timeout=timeout)
    if resp.status_code >= 400:
        resp.close()
        raise RuntimeError(
            f"Public Drive download failed ({resp.status_code}). "
            "Share the file as 'Anyone with the link' (Viewer)."
        )

    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in content_type:
        # Large files show a virus-scan / confirm page. Parse form fields and retry.
        html_text = resp.text
        resp.close()
        lowered = html_text.lower()
        if "quota exceeded" in lowered or "too many users have viewed" in lowered:
            raise RuntimeError(
                "Google Drive anonymous download quota exceeded for this file. "
                "Use Private files (Google sign-in) mode instead."
            )

        inputs = dict(
            re.findall(
                r'name=["\']([^"\']+)["\']\s+value=["\']([^"\']*)["\']',
                html_text,
                flags=re.I,
            )
        )
        for value, name in re.findall(
            r'value=["\']([^"\']*)["\']\s+name=["\']([^"\']+)["\']',
            html_text,
            flags=re.I,
        ):
            inputs.setdefault(name, value)
        action_match = re.search(
            r'<form[^>]+action=["\']([^"\']+)["\']',
            html_text,
            flags=re.I,
        )
        action = action_match.group(1) if action_match else "https://drive.usercontent.google.com/download"
        params = {
            "id": file_id,
            "export": "download",
            "confirm": inputs.get("confirm") or "t",
        }
        if inputs.get("uuid"):
            params["uuid"] = inputs["uuid"]

        resp = session.get(action, params=params, stream=True, timeout=timeout)
        if resp.status_code >= 400:
            resp.close()
            raise RuntimeError(f"Public Drive download failed after confirm ({resp.status_code}).")
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type:
            peek = resp.text[:8000].lower()
            resp.close()
            if "quota exceeded" in peek or "too many users have viewed" in peek:
                raise RuntimeError(
                    "Google Drive anonymous download quota exceeded for this file. "
                    "Use Private files (Google sign-in) mode instead."
                )
            raise RuntimeError(
                "Still received HTML instead of video bytes. "
                "The link may be private — use optional Google authorization, "
                "or set sharing to Anyone with the link."
            )

    return resp, session


def open_oauth_drive_stream(
    creds,
    file_id: str,
    timeout: int = 300,
    retries: int = 4,
):
    """
    Stream Drive file bytes with OAuth via requests (avoids httplib2 SSL issues).

    Returns (response, session). Caller must close both.
    """
    from google.auth.transport.requests import AuthorizedSession

    last_error: Exception | None = None
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    params = {"alt": "media", "supportsAllDrives": "true"}

    for attempt in range(1, retries + 1):
        session = AuthorizedSession(creds)
        try:
            # Ensure token is fresh before each attempt.
            if not creds.valid:
                from google.auth.transport.requests import Request

                creds.refresh(Request())
            resp = session.get(url, params=params, stream=True, timeout=timeout)
            if resp.status_code >= 400:
                body = ""
                try:
                    body = resp.text[:500]
                except Exception:
                    body = ""
                resp.close()
                session.close()
                raise RuntimeError(
                    f"OAuth Drive download failed ({resp.status_code}) for {file_id}. {body}"
                )
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                # Unexpected non-media payload.
                peek = resp.text[:500]
                resp.close()
                session.close()
                raise RuntimeError(f"OAuth download returned non-media content: {peek}")
            return resp, session
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, OSError) as exc:
            last_error = exc
            try:
                session.close()
            except Exception:
                pass
            if attempt >= retries:
                break
            time.sleep(1.5 * attempt)
        except Exception:
            try:
                session.close()
            except Exception:
                pass
            raise

    raise RuntimeError(
        f"OAuth Drive download failed after {retries} attempts "
        f"(SSL/connection). Last error: {last_error}"
    )


def _content_range_start(response: requests.Response) -> Optional[int]:
    header = response.headers.get("Content-Range") or ""
    match = re.match(r"bytes\s+(\d+)-", header, flags=re.I)
    return int(match.group(1)) if match else None


def _truncate_partial(path: Path, size: int) -> None:
    with path.open("r+b") as handle:
        handle.truncate(size)


def _finish_partial(partial: Path, destination: Path, expected_size: int = 0) -> Path:
    if expected_size and partial.exists() and partial.stat().st_size > expected_size:
        _truncate_partial(partial, expected_size)
    partial.replace(destination)
    return destination


def download_oauth_file(
    creds,
    file_id: str,
    destination: Path,
    *,
    expected_size: int = 0,
    stop_requested: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    chunk_size: int = 4 * 1024 * 1024,
    retries: int = 12,
) -> Path:
    """Download a Drive file to a resumable temporary file using byte ranges."""
    from google.auth.transport.requests import AuthorizedSession, Request

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    params = {"alt": "media", "supportsAllDrives": "true"}
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        if stop_requested and stop_requested():
            raise RuntimeError("Stopped by user")

        offset = partial.stat().st_size if partial.exists() else 0
        if expected_size and offset > expected_size:
            # A previous resume appended overlapping bytes. Keep the valid prefix.
            _truncate_partial(partial, expected_size)
            offset = expected_size
        if expected_size and offset == expected_size and offset > 0:
            if on_progress:
                on_progress(offset, expected_size)
            return _finish_partial(partial, destination, expected_size)

        session = AuthorizedSession(creds)
        response = None
        try:
            if not creds.valid:
                creds.refresh(Request())

            headers = {"Range": f"bytes={offset}-"} if offset else {}
            response = session.get(
                url,
                params=params,
                headers=headers,
                stream=True,
                timeout=(30, 300),
            )
            if response.status_code == 416:
                # Asked past EOF. The partial is already complete or oversized.
                if expected_size and partial.exists() and partial.stat().st_size >= expected_size:
                    if on_progress:
                        on_progress(expected_size, expected_size)
                    return _finish_partial(partial, destination, expected_size)
                if partial.exists():
                    partial.unlink()
                raise requests.exceptions.ConnectionError(
                    f"Drive returned 416 for {file_id}; restarting download"
                )
            if response.status_code not in {200, 206}:
                body = response.text[:500]
                raise RuntimeError(
                    f"OAuth Drive download failed ({response.status_code}) "
                    f"for {file_id}. {body}"
                )

            range_start = _content_range_start(response)
            if offset == 0 or response.status_code == 200:
                # Full-body response: rewrite instead of appending duplicates.
                mode = "wb"
                offset = 0
            elif range_start is not None and range_start != offset:
                _truncate_partial(partial, range_start)
                offset = range_start
                mode = "ab"
            else:
                mode = "ab"

            with partial.open(mode) as output:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if stop_requested and stop_requested():
                        raise RuntimeError("Stopped by user")
                    if chunk:
                        output.write(chunk)
                        offset += len(chunk)
                        if on_progress:
                            on_progress(offset, expected_size)

            downloaded = partial.stat().st_size
            if expected_size and downloaded > expected_size:
                _truncate_partial(partial, expected_size)
                downloaded = expected_size
            if on_progress:
                on_progress(downloaded, expected_size)
            if expected_size and downloaded != expected_size:
                raise requests.exceptions.ConnectionError(
                    f"Incomplete Drive download: {downloaded}/{expected_size} bytes"
                )

            return _finish_partial(partial, destination, expected_size)
        except RuntimeError as exc:
            if str(exc) == "Stopped by user":
                raise
            # HTTP permission/not-found errors will not improve with retries.
            if "OAuth Drive download failed (" in str(exc):
                raise
            last_error = exc
        except (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
            requests.exceptions.SSLError,
            OSError,
        ) as exc:
            last_error = exc
        finally:
            if response is not None:
                response.close()
            session.close()

        if attempt < retries:
            time.sleep(min(15.0, 1.5 * attempt))

    downloaded = partial.stat().st_size if partial.exists() else 0
    raise RuntimeError(
        f"Drive download failed after {retries} attempts; "
        f"saved {downloaded}/{expected_size or '?'} bytes for resume. "
        f"Last error: {last_error}"
    )


def stream_public_bytes(file_id: str, chunk_size: int = 8 * 1024 * 1024) -> Iterable[bytes]:
    """Yield public Drive media bytes without writing the video to disk."""
    resp, session = open_public_drive_stream(file_id)
    try:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk
    finally:
        resp.close()
        session.close()


def get_credentials(
    credentials_path: Path,
    token_path: Path,
):
    """Load or create OAuth credentials for Drive readonly access (optional)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Missing Google OAuth client file: {credentials_path}. "
            "For 'Anyone with the link' videos you do not need this. "
            "Only required for private Drive files."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    # Fixed port so Google Cloud redirect URI can match exactly.
    # Add this under OAuth client → Authorized redirect URIs:
    #   http://localhost:8085/
    creds = flow.run_local_server(
        port=8085,
        prompt="consent",
        access_type="offline",
        open_browser=True,
    )
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_drive_service(creds):
    """Build a Drive API v3 service."""
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_file_metadata(service, file_id: str) -> dict:
    """Fetch original filename, mime type, and size for a Drive file (OAuth)."""
    return (
        service.files()
        .get(fileId=file_id, fields="id,name,mimeType,size", supportsAllDrives=True)
        .execute()
    )


def iter_drive_bytes(creds, file_id: str, chunk_size: int = 1024 * 1024) -> Iterable[bytes]:
    """Yield authenticated Drive media bytes without writing the video to disk."""
    resp, session = open_oauth_drive_stream(creds, file_id)
    try:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk
    finally:
        resp.close()
        session.close()
