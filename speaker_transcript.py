"""Extract a single speaker's transcript from Google Drive videos.

Pipeline per link:
  1. Audio: Drive API byte-range fetch of just the audio track (fast for huge camera
     files), falling back to ffmpeg over the Drive API, then public yt-dlp download.
  2. Text: Drive auto-captions (original + English translation) when present;
     otherwise local Parakeet (English) or OpenRouter Whisper (Hindi/any language).
  3. Speaker: SpeechBrain ECAPA voice embeddings, clustered; the target cluster is
     chosen by a saved voiceprint, a reference clip/time range, or largest-by-talk-time.

Run `python speaker_transcript.py --help` or see .cursor/skills/speaker-transcript/SKILL.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import requests
import soundfile as sf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
from drive_auth import extract_file_id  # noqa: E402

SR = 16000
CACHE_DIR = ROOT / "output" / ".speaker_cache"
DEFAULT_OUT = ROOT / "output" / "speaker_transcripts"
VOICEPRINT_DIR = ROOT / "voiceprints"
API_MEDIA = "https://www.googleapis.com/drive/v3/files/{fid}?alt=media&supportsAllDrives=true"
API_META = "https://www.googleapis.com/drive/v3/files/{fid}"
OPENROUTER_STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
RANGE_FETCH_MIN_BYTES = 300 * 1024 * 1024
DISPLAY_NAMES = {"pratham": "Pratham Mittal"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def vtt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d}.{ms % 1000:03d}"


def parse_hms(s: str) -> float:
    parts = [float(p) for p in s.strip().split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def slugify(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60] or "video"


@dataclass
class Segment:
    start: float
    end: float
    text: str = ""
    original: str = ""
    cluster: int = -1
    sim: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def dur(self) -> float:
        return self.end - self.start


# --------------------------------------------------------------------------- audio


def oauth_token() -> str | None:
    """Return a Drive access token only if a saved OAuth token exists (never opens a browser)."""
    token_path = Path(os.environ.get("GOOGLE_TOKEN", ROOT / ".token.json"))
    if not token_path.is_absolute():
        token_path = ROOT / token_path
    if not token_path.exists():
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(token_path), ["https://www.googleapis.com/auth/drive.readonly"])
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds.token if creds.valid else None


class RangeReader:
    def __init__(self, fid: str, token: str):
        self.url = API_MEDIA.format(fid=fid)
        self.headers = {"Authorization": f"Bearer {token}"}
        self.session = requests.Session()
        self.session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=32))

    def get(self, a: int, b: int) -> bytes:
        last = None
        for attempt in range(6):
            try:
                r = self.session.get(self.url, headers={**self.headers, "Range": f"bytes={a}-{b}"}, timeout=90)
                if r.status_code == 206 and len(r.content) == b - a + 1:
                    return r.content
                last = f"HTTP {r.status_code}"
            except requests.RequestException as exc:
                last = str(exc)
            time.sleep(1 + attempt)
        raise RuntimeError(f"range {a}-{b} failed: {last}")


def _children(buf: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        sz, typ = struct.unpack(">I4s", buf[i:i + 8])
        hl = 8
        if sz == 1:
            sz = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            hl = 16
        elif sz == 0:
            sz = end - i
        if sz < hl:
            return
        yield typ, i + hl, i + sz
        i += sz


def _find(buf: bytes, start: int, end: int, path: list[bytes]):
    for typ, s, e in _children(buf, start, end):
        if typ == path[0]:
            return (s, e) if len(path) == 1 else _find(buf, s, e, path[1:])
    return None


def _asc_from_esds(buf: bytes, s: int, e: int) -> bytes:
    i = s + 4

    def read_len(i: int):
        n = 0
        for _ in range(4):
            b = buf[i]
            i += 1
            n = (n << 7) | (b & 0x7F)
            if not b & 0x80:
                break
        return n, i

    while i < e:
        tag = buf[i]
        i += 1
        ln, i = read_len(i)
        if tag == 0x03:
            flags = buf[i + 2]
            i += 3
            if flags & 0x80:
                i += 2
            if flags & 0x40:
                i += 1 + buf[i]
            if flags & 0x20:
                i += 2
        elif tag == 0x04:
            i += 13
        elif tag == 0x05:
            return buf[i:i + ln]
        else:
            i += ln
    raise RuntimeError("AAC config not found")


def fetch_audio_ranges(fid: str, token: str, size: int, out_wav: Path) -> None:
    """Download only the AAC audio samples of an MP4/MOV via HTTP ranges and decode to 16k mono."""
    rr = RangeReader(fid, token)
    off, moov = 0, None
    while off < size:
        hdr = rr.get(off, min(off + 15, size - 1))
        sz, typ = struct.unpack(">I4s", hdr[:8])
        if sz == 1:
            sz = struct.unpack(">Q", hdr[8:16])[0]
        elif sz == 0:
            sz = size - off
        if sz < 8:
            raise RuntimeError("not an MP4/MOV file")
        if typ == b"moov":
            moov = (off, sz)
            break
        off += sz
    if not moov:
        raise RuntimeError("moov box not found")
    buf = rr.get(moov[0], moov[0] + moov[1] - 1)

    stbl = None
    for typ, s, e in _children(buf, 8, len(buf)):
        if typ != b"trak":
            continue
        hdlr = _find(buf, s, e, [b"mdia", b"hdlr"])
        if hdlr and buf[hdlr[0] + 8:hdlr[0] + 12] == b"soun":
            stbl = _find(buf, s, e, [b"mdia", b"minf", b"stbl"])
            break
    if not stbl:
        raise RuntimeError("no audio track")
    st = {t: (a, b) for t, a, b in _children(buf, *stbl)}

    a, _ = st[b"stsd"]
    entry = a + 8
    entry_size, fourcc = struct.unpack(">I4s", buf[entry:entry + 8])
    version = struct.unpack(">H", buf[entry + 16:entry + 18])[0]
    channels, bits = struct.unpack(">HH", buf[entry + 24:entry + 28])
    rate = struct.unpack(">I", buf[entry + 32:entry + 36])[0] >> 16
    body = entry + 8 + 28 + {0: 0, 1: 16, 2: 36}.get(version, 0)
    pcm_formats = {b"twos": "s16be", b"sowt": "s16le", b"in24": "s24be", b"in32": "s32be"}
    if fourcc == b"mp4a":
        esds = _find(buf, body, entry + entry_size, [b"esds"])
        if not esds:
            wave = _find(buf, body, entry + entry_size, [b"wave"])
            esds = wave and _find(buf, wave[0], wave[1], [b"esds"])
        if not esds:
            raise RuntimeError("esds not found")
        asc = _asc_from_esds(buf, *esds)
        obj = asc[0] >> 3
        sfi = ((asc[0] & 7) << 1) | (asc[1] >> 7)
        ch = (asc[1] >> 3) & 0xF
        if obj > 4 or sfi == 15 or ch == 0:
            raise RuntimeError("unsupported AAC config for ADTS")
        pcm = None
    elif fourcc in pcm_formats and version in (0, 1) and channels and rate:
        pcm = pcm_formats[fourcc]
        if fourcc == b"twos" and bits == 8:
            pcm = "s8"
        bytes_per_frame = channels * max(bits, 8) // 8
        if version == 1:
            bytes_per_frame = struct.unpack(">I", buf[entry + 44:entry + 48])[0] or bytes_per_frame
    else:
        raise RuntimeError(f"audio codec {fourcc!r} (v{version}) not supported for range fetch")

    a, _ = st[b"stsz"]
    fixed, n = struct.unpack(">II", buf[a + 4:a + 12])
    if pcm and fixed <= 1:
        fixed = bytes_per_frame
    sizes = [fixed] * n if fixed else list(struct.unpack(f">{n}I", buf[a + 12:a + 12 + 4 * n]))
    if b"co64" in st:
        a, _ = st[b"co64"]
        cn = struct.unpack(">I", buf[a + 4:a + 8])[0]
        offs = struct.unpack(f">{cn}Q", buf[a + 8:a + 8 + 8 * cn])
    else:
        a, _ = st[b"stco"]
        cn = struct.unpack(">I", buf[a + 4:a + 8])[0]
        offs = struct.unpack(f">{cn}I", buf[a + 8:a + 8 + 4 * cn])
    a, _ = st[b"stsc"]
    en = struct.unpack(">I", buf[a + 4:a + 8])[0]
    stsc = [struct.unpack(">III", buf[a + 8 + 12 * k:a + 20 + 12 * k]) for k in range(en)]

    chunks, si = [], 0
    for k, (first, spc, _) in enumerate(stsc):
        last = stsc[k + 1][0] - 1 if k + 1 < len(stsc) else cn
        for c in range(first, last + 1):
            chunks.append((offs[c - 1], sizes[si:si + spc]))
            si += spc
    log(f"range-fetching {len(chunks)} audio chunks ({sum(sizes) / 1e6:.0f} MB of {size / 1e9:.1f} GB)")

    def fetch(item):
        o, szs = item
        return rr.get(o, o + sum(szs) - 1), szs

    raw = out_wav.with_suffix(".raw" if pcm else ".aac")
    done = 0
    with open(raw, "wb") as f, ThreadPoolExecutor(24) as ex:
        for data, szs in ex.map(fetch, chunks):
            if pcm:
                f.write(data)
            else:
                p = 0
                for sz in szs:
                    fl = sz + 7
                    f.write(bytes([0xFF, 0xF1, ((obj - 1) << 6) | (sfi << 2) | (ch >> 2),
                                   ((ch & 3) << 6) | (fl >> 11), (fl >> 3) & 0xFF, ((fl & 7) << 5) | 0x1F, 0xFC]))
                    f.write(data[p:p + sz])
                    p += sz
            done += 1
            if done % 1000 == 0:
                log(f"  {done}/{len(chunks)} chunks")
    src = ["-f", pcm, "-ar", str(rate), "-ac", str(channels), "-i", str(raw)] if pcm else ["-i", str(raw)]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *src, "-ac", "1", "-ar", str(SR), str(out_wav)], check=True)
    raw.unlink(missing_ok=True)


def probe_duration(src: str, headers: str | None = None) -> float:
    cmd = ["ffprobe", "-v", "error"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-show_entries", "format=duration", "-of", "csv=p=0", src]
    try:
        return float(subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.strip() or 0)
    except (ValueError, subprocess.TimeoutExpired):
        return 0.0


def check_complete(out_wav: Path, expected: float) -> None:
    got = sf.info(str(out_wav)).duration
    if expected and got < expected * 0.97:
        out_wav.unlink(missing_ok=True)
        raise RuntimeError(f"audio incomplete: got {hms(got)} of {hms(expected)}")


def ffmpeg_to_wav(src: str, out_wav: Path, headers: str | None = None) -> None:
    cmd = ["ffmpeg", "-loglevel", "error", "-y"]
    if headers:
        cmd += ["-headers", headers]
    if src.startswith("http"):
        cmd += ["-reconnect", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "30"]
    cmd += ["-i", src, "-vn", "-map", "0:a:0", "-ac", "1", "-ar", str(SR), str(out_wav)]
    subprocess.run(cmd, check=True)
    check_complete(out_wav, probe_duration(src, headers))


def public_download_audio(fid: str, out_wav: Path) -> None:
    import yt_dlp

    with tempfile.TemporaryDirectory() as td:
        last = None
        for fmt in ("18", "22", "source", "best"):
            try:
                opts = {"quiet": True, "noprogress": True, "format": fmt, "http_chunk_size": 10 * 1024 * 1024,
                        "outtmpl": f"{td}/v.%(ext)s"}
                with yt_dlp.YoutubeDL(opts) as y:
                    y.download([f"https://drive.google.com/file/d/{fid}/view"])
                path = next(Path(td).glob("v.*"))
                ffmpeg_to_wav(str(path), out_wav)
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                for p in Path(td).glob("*"):
                    p.unlink()
                if "playbacks has been exceeded" in str(exc):
                    break
        raise RuntimeError(f"public download failed: {last}")


def get_audio(fid: str, work: Path) -> tuple[Path, dict]:
    out = work / "audio.wav"
    meta_path = work / "meta.json"
    if out.exists() and meta_path.exists():
        log("audio: using cache")
        return out, json.loads(meta_path.read_text())
    meta: dict = {"id": fid}
    token = None
    try:
        token = oauth_token()
    except Exception as exc:  # noqa: BLE001
        log(f"OAuth unavailable ({exc}); using public access")
    errors = []
    if token:
        r = requests.get(API_META.format(fid=fid), headers={"Authorization": f"Bearer {token}"},
                         params={"fields": "name,size,mimeType", "supportsAllDrives": "true"}, timeout=30)
        if r.ok:
            info = r.json()
            meta.update(name=info.get("name"), size=int(info.get("size") or 0), mime=info.get("mimeType"))
            log(f"Drive API: {meta['name']} ({meta['size'] / 1e9:.2f} GB, {meta['mime']})")
            is_mp4 = (meta["mime"] or "").startswith("video/") and str(meta["name"]).lower().endswith(
                (".mp4", ".mov", ".m4v", ".m4a"))
            if is_mp4 and meta["size"] >= RANGE_FETCH_MIN_BYTES:
                try:
                    fetch_audio_ranges(fid, token, meta["size"], out)
                    check_complete(out, probe_duration(API_MEDIA.format(fid=fid), f"Authorization: Bearer {token}\r\n"))
                    meta["audio_method"] = "drive-api-ranges"
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"ranges: {exc}")
                    log(f"range fetch failed ({exc}); falling back to ffmpeg stream")
            if not out.exists():
                try:
                    log("ffmpeg reading via Drive API (may take a while for large files)")
                    ffmpeg_to_wav(API_MEDIA.format(fid=fid), out, f"Authorization: Bearer {token}\r\n")
                    meta["audio_method"] = "drive-api-ffmpeg"
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"api-ffmpeg: {exc}")
        else:
            errors.append(f"Drive API {r.status_code}: {r.text[:200]}")
            log(f"Drive API metadata failed ({r.status_code}); trying public download")
    if not out.exists():
        log("public download via yt-dlp")
        try:
            public_download_audio(fid, out)
            meta["audio_method"] = "public-yt-dlp"
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            raise RuntimeError("could not get audio: " + " | ".join(errors)) from exc
    meta["duration"] = sf.info(str(out)).duration
    meta_path.write_text(json.dumps(meta, indent=1))
    return out, meta


# ------------------------------------------------------------------------ captions


def _parse_vtt(text: str) -> list[Segment]:
    def ts(s: str) -> float:
        h, m, rest = s.split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)

    out = []
    for block in text.split("\n\n"):
        lines = block.strip().split("\n")
        if not lines or "-->" not in lines[0]:
            continue
        start, end = [ts(x.strip().split(" ")[0]) for x in lines[0].split("-->")]
        tagged = [ln for ln in lines[1:] if "<c>" in ln]
        if tagged:
            body = tagged[0]
        elif any("<c>" in b for b in text[:20000].split("\n")):
            continue
        else:
            body = " ".join(lines[1:])
        body = re.sub(r"<[^>]+>", "", body).strip()
        if body and end > start:
            out.append(Segment(start, end, body))
    return out


def get_captions(fid: str, work: Path) -> tuple[list[Segment], str] | None:
    """Return caption segments (English text, original in .original) and language, or None."""
    cache = work / "captions.json"
    if cache.exists():
        data = json.loads(cache.read_text())
        if not data:
            return None
        return [Segment(**s) for s in data["segments"]], data["lang"]
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "writesubtitles": True}) as y:
            info = y.extract_info(f"https://drive.google.com/file/d/{fid}/view", download=False, process=False)
        subs = info.get("subtitles") or {}
    except Exception as exc:  # noqa: BLE001
        log(f"captions: video page not readable ({str(exc)[:120]})")
        return None
    result = None
    for lang, tracks in subs.items():
        vtt = next((t["url"] for t in tracks if t.get("ext") == "vtt"), None)
        if not vtt:
            continue

        def fetch(url: str) -> list[Segment]:
            resp = requests.get(url, timeout=60)
            return _parse_vtt(resp.text) if resp.ok else []

        original = fetch(vtt)
        if not original:
            continue
        english = original if lang.startswith("en") else fetch(vtt + "&tlang=en") or original
        if english is not original:
            for seg in english:
                seg.original = " ".join(o.text for o in original if o.start < seg.end and o.end > seg.start)
        result = (english, lang)
        log(f"captions: {len(english)} cues, language={lang}")
        break
    if not result:
        log("captions: none available")
    cache.write_text(json.dumps({"segments": [s.__dict__ for s in result[0]], "lang": result[1]} if result else {},
                                ensure_ascii=False))
    return result


# ------------------------------------------------------------------------ speakers


_ENCODER = None


def encoder():
    global _ENCODER
    if _ENCODER is None:
        import warnings

        warnings.filterwarnings("ignore")
        from speechbrain.inference.speaker import EncoderClassifier

        _ENCODER = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb",
                                                  savedir=str(CACHE_DIR / "ecapa"))
    return _ENCODER


def embed_spans(audio: np.ndarray, spans: list[tuple[float, float]], min_len: float = 0.8) -> tuple[np.ndarray, list[int]]:
    import torch

    enc = encoder()
    embs, keep = [], []
    for i, (a, b) in enumerate(spans):
        if b - a < min_len:
            continue
        clip = audio[int(a * SR):int(b * SR)]
        if len(clip) < int(min_len * SR):
            continue
        with torch.no_grad():
            e = enc.encode_batch(torch.from_numpy(clip).unsqueeze(0)).squeeze().numpy()
        embs.append(e / (np.linalg.norm(e) + 1e-9))
        keep.append(i)
    return (np.stack(embs) if embs else np.zeros((0, 192), dtype=np.float32)), keep


def cluster(X: np.ndarray, threshold: float) -> np.ndarray:
    from sklearn.cluster import AgglomerativeClustering

    if len(X) < 2:
        return np.zeros(len(X), dtype=int)
    return AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                   distance_threshold=threshold).fit_predict(X)


def speech_windows(audio: np.ndarray, win: float, hop: float) -> list[tuple[float, float]]:
    n = int(win * SR)
    h = int(hop * SR)
    starts = range(0, max(1, len(audio) - n), h)
    rms = np.array([np.sqrt(np.mean(audio[s:s + n] ** 2) + 1e-12) for s in starts])
    floor = np.percentile(rms, 20)
    gate = max(floor * 2.0, np.percentile(rms, 10) * 3.0, 1e-3)
    return [(s / SR, (s + n) / SR) for s, v in zip(starts, rms) if v >= gate]


@dataclass
class Identity:
    target: np.ndarray
    chosen: int
    clusters: list[dict]
    how: str


def identify(X: np.ndarray, labels: np.ndarray, durs: np.ndarray, *, voiceprint: np.ndarray | None,
             pick: int | None, texts: list[str] | None, starts: list[float]) -> Identity:
    stats = []
    for lab in sorted(set(labels.tolist())):
        m = labels == lab
        c = X[m].mean(0)
        c /= np.linalg.norm(c) + 1e-9
        idx = np.where(m)[0]
        stats.append({
            "cluster": int(lab), "minutes": round(float(durs[m].sum()) / 60, 2), "segments": int(m.sum()),
            "first": hms(starts[idx[0]]), "last": hms(starts[idx[-1]]),
            "voiceprint_sim": round(float(c @ voiceprint), 3) if voiceprint is not None else None,
            "samples": [texts[i] for i in idx[:6]] if texts else [hms(starts[i]) for i in idx[:6]],
            "_centroid": c,
        })
    stats.sort(key=lambda s: s["minutes"], reverse=True)
    if pick is not None:
        chosen = next(s for s in stats if s["cluster"] == pick)
        how = f"cluster {pick} picked manually"
    elif voiceprint is not None:
        big = [s for s in stats if s["minutes"] >= 0.5] or stats
        chosen = max(big, key=lambda s: s["voiceprint_sim"])
        how = f"closest cluster to voiceprint (sim {chosen['voiceprint_sim']})"
        if chosen["voiceprint_sim"] < 0.3:
            how += " — WARNING: weak match, speaker may not be in this video"
    else:
        chosen = stats[0]
        how = "largest cluster by speaking time (no voiceprint) — verify the samples"
    return Identity(chosen["_centroid"], chosen["cluster"], stats, how)


def load_voiceprint(args, audio: np.ndarray) -> np.ndarray | None:
    if args.reference:
        clip, sr = sf.read(args.reference, dtype="float32")
        if clip.ndim > 1:
            clip = clip.mean(1)
        if sr != SR:
            with tempfile.NamedTemporaryFile(suffix=".wav") as t:
                ffmpeg_to_wav(args.reference, Path(t.name))
                clip, _ = sf.read(t.name, dtype="float32")
        return _embed_clip(clip)
    if args.reference_range:
        a, b = [parse_hms(x) for x in args.reference_range.split("-")]
        return _embed_clip(audio[int(a * SR):int(b * SR)])
    if args.auto or not args.speaker:
        return None
    path = VOICEPRINT_DIR / f"{slugify(args.speaker).lower()}.npy"
    if path.exists():
        log(f"voiceprint: {path.relative_to(ROOT)}")
        return np.load(path)
    log(f"voiceprint: none saved for '{args.speaker}', using largest speaker")
    return None


def _embed_clip(clip: np.ndarray) -> np.ndarray:
    win = 3 * SR
    spans = [(s / SR, (s + win) / SR) for s in range(0, max(1, len(clip) - win), win // 2)] or [(0, len(clip) / SR)]
    X, _ = embed_spans(clip, spans)
    if not len(X):
        raise SystemExit("reference audio too short (need 3s+)")
    v = X.mean(0)
    return v / np.linalg.norm(v)


# ------------------------------------------------------------------------ STT


def openrouter_headers() -> dict:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not set (needed for --stt openrouter / translation)")
    return {"Authorization": f"Bearer {key}", "X-Title": "speaker-transcript"}


def whisper_region(audio: np.ndarray, spans: list[tuple[float, float]], language: str | None) -> str:
    gap = np.zeros(int(0.3 * SR), dtype=np.float32)
    pieces = []
    for a, b in spans:
        pieces += [audio[int(a * SR):int(b * SR)], gap]
    with tempfile.NamedTemporaryFile(suffix=".mp3") as t:
        wav = t.name.replace(".mp3", ".wav")
        sf.write(wav, np.concatenate(pieces), SR)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", wav, "-b:a", "64k", t.name], check=True)
        os.unlink(wav)
        data = {"model": os.environ.get("OPENROUTER_STT_MODEL", "openai/whisper-large-v3"), "response_format": "json"}
        if language:
            data["language"] = language
        for attempt in range(3):
            with open(t.name, "rb") as fh:
                r = requests.post(OPENROUTER_STT_URL, headers=openrouter_headers(), data=data,
                                  files={"file": ("region.mp3", fh, "audio/mpeg")}, timeout=600)
            if r.ok:
                return (r.json().get("text") or "").strip()
            if r.status_code < 500:
                raise RuntimeError(f"OpenRouter STT {r.status_code}: {r.text[:300]}")
            time.sleep(2 + attempt * 3)
        raise RuntimeError(f"OpenRouter STT failed: {r.status_code}")


def parakeet_segments(wav: Path) -> list[Segment]:
    from parakeet_mlx import from_pretrained

    model = from_pretrained(os.environ.get("MODEL_NAME") or "mlx-community/parakeet-tdt-0.6b-v3")
    result = model.transcribe(str(wav), chunk_duration=120.0, overlap_duration=15.0)
    return [Segment(float(s.start), float(s.end), s.text.strip()) for s in result.sentences if s.text.strip()]


def needs_translation(texts: list[str]) -> bool:
    joined = "".join(texts)
    letters = [c for c in joined if c.isalpha()]
    return bool(letters) and sum(not c.isascii() for c in letters) / len(letters) > 0.2


def translate(paragraphs: list[str]) -> list[str]:
    model = os.environ.get("OPENROUTER_TRANSLATE_MODEL") or os.environ.get("OPENROUTER_MODEL") or "openai/gpt-4o-mini"
    out: list[str] = []
    batch = 15
    for i in range(0, len(paragraphs), batch):
        chunk = paragraphs[i:i + batch]
        prompt = ("Translate each numbered paragraph of this spoken transcript into natural English. Keep meaning, "
                  "names and numbers; do not summarise. Reply ONLY with a JSON array of strings, same length and "
                  "order.\n\n" + "\n\n".join(f"{k + 1}. {p}" for k, p in enumerate(chunk)))
        r = requests.post(OPENROUTER_CHAT_URL, headers=openrouter_headers(), timeout=300,
                          json={"model": model, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\[.*\]", content, re.S)
        items = json.loads(m.group(0)) if m else []
        if len(items) != len(chunk):
            items = (items + chunk)[:len(chunk)]
        out += [str(x) for x in items]
    return out


# ------------------------------------------------------------------------ main flow


def paragraphs(segs: list[Segment], gap: float = 3.0, max_len: float = 90.0) -> list[dict]:
    paras: list[dict] = []
    for s in segs:
        if paras and s.start - paras[-1]["end"] < gap and s.end - paras[-1]["start"] < max_len:
            p = paras[-1]
            p["end"] = s.end
            p["segs"].append(s)
        else:
            paras.append({"start": s.start, "end": s.end, "segs": [s]})
    return paras


def process(link: str, args) -> Path:
    fid = extract_file_id(link)
    if not fid:
        raise SystemExit(f"not a Drive link: {link}")
    work = CACHE_DIR / fid
    work.mkdir(parents=True, exist_ok=True)
    log(f"=== {link}")
    wav, meta = get_audio(fid, work)
    audio, _ = sf.read(str(wav), dtype="float32")
    if args.limit_minutes:
        audio = audio[:int(args.limit_minutes * 60 * SR)]
    name = meta.get("name") or fid
    log(f"audio ready: {hms(meta['duration'])} ({meta.get('audio_method', 'cache')})")

    whisper_ok = args.stt == "openrouter" and bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
    use_captions = args.source == "captions" or (args.source == "auto" and not whisper_ok)
    caps = get_captions(fid, work) if use_captions else None
    if args.source == "captions" and not caps:
        raise SystemExit("captions unavailable: none exist or Drive blocked the lookup (see log); "
                         "use --source auto or stt")
    if args.source == "auto" and not caps and args.stt == "openrouter" and not whisper_ok:
        log("no captions and no OPENROUTER_API_KEY — falling back to local Parakeet (English only)")
        args.stt = "parakeet"
    voiceprint = load_voiceprint(args, audio)

    if caps:
        method = f"Drive auto-captions ({caps[1]})"
        segs = [s for s in caps[0] if s.end <= len(audio) / SR]
    elif args.stt == "parakeet":
        method = "Parakeet MLX (local)"
        log("transcribing full audio with Parakeet")
        segs = [s for s in parakeet_segments(wav) if s.end <= len(audio) / SR]
    else:
        method = "OpenRouter Whisper (target-speaker regions only)"
        segs = None

    if segs is not None:
        X, keep = embed_spans(audio, [(s.start, s.end) for s in segs])
        segs_k = [segs[i] for i in keep]
        labels = cluster(X, args.cluster_threshold)
        ident = identify(X, labels, np.array([s.dur for s in segs_k]), voiceprint=voiceprint, pick=args.pick_cluster,
                         texts=[s.text for s in segs_k], starts=[s.start for s in segs_k])
        sims = X @ ident.target
        for s, lab, sim in zip(segs_k, labels, sims):
            s.cluster, s.sim = int(lab), round(float(sim), 3)
        kept = [s for s in segs_k if s.sim >= args.threshold]
        kept_X = X[sims >= args.threshold]
    else:
        wins = speech_windows(audio, 1.5, 0.75)
        log(f"embedding {len(wins)} speech windows")
        X, keep = embed_spans(audio, wins)
        wins = [wins[i] for i in keep]
        labels = cluster(X, args.cluster_threshold)
        ident = identify(X, labels, np.full(len(wins), 0.75), voiceprint=voiceprint, pick=args.pick_cluster,
                         texts=None, starts=[w[0] for w in wins])
        sims = X @ ident.target
        mask = sims >= args.threshold
        smooth = np.array([np.median(mask[max(0, i - 2):i + 3]) >= 0.5 for i in range(len(mask))])
        regions: list[list[float]] = []
        for (a, b), on in zip(wins, smooth):
            if not on:
                continue
            if regions and a - regions[-1][1] < 1.0:
                regions[-1][1] = max(regions[-1][1], b)
            else:
                regions.append([a, b])
        regions = [r for r in regions if r[1] - r[0] >= 1.5]
        groups = paragraphs([Segment(a, b) for a, b in regions])
        log(f"transcribing {len(groups)} target-speaker passages "
            f"({sum(r[1] - r[0] for r in regions) / 60:.1f} min) with Whisper")
        lang = args.language
        if not lang:
            longest = sorted(groups, key=lambda g: g["end"] - g["start"], reverse=True)[:3]
            probe = [whisper_region(audio, [(s.start, s.end) for s in g["segs"]], None) for g in longest]
            lang = "hi" if needs_translation(probe) else "en"
            log(f"detected language: {lang}")

        def run(g):
            return whisper_region(audio, [(s.start, s.end) for s in g["segs"]], lang)

        with ThreadPoolExecutor(4) as ex:
            texts = list(ex.map(run, groups))
        kept = [Segment(g["start"], g["end"], t, t) for g, t in zip(groups, texts) if t]
        kept_X = X[mask]

    total = sum(s.dur for s in kept) / 60
    log(f"kept {len(kept)} segments, {total:.1f} min of target speaker — {ident.how}")
    if not kept:
        raise SystemExit("no segments matched the target speaker; inspect clusters with --pick-cluster")

    paras = paragraphs(kept)
    en_texts = [" ".join(s.text for s in p["segs"]) for p in paras]
    orig_texts = [" ".join((s.original or s.text) for s in p["segs"]) for p in paras]
    if not caps and not args.no_translate and needs_translation(en_texts):
        log("translating to English via OpenRouter")
        en_texts = translate(en_texts)

    speaker = args.speaker or "speaker"
    out_dir = Path(args.out) / f"{slugify(Path(name).stem)}_{slugify(speaker).lower()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    header = f"{name} — {speaker} only\nSource: {method}\nSpeaker match: {ident.how}\n\n"
    (out_dir / "transcript.en.txt").write_text(
        header + "".join(f"[{hms(p['start'])} - {hms(p['end'])}]\n{t}\n\n" for p, t in zip(paras, en_texts)),
        encoding="utf-8")
    if any(o != t for o, t in zip(orig_texts, en_texts)):
        (out_dir / "transcript.original.txt").write_text(
            header + "".join(f"[{hms(p['start'])} - {hms(p['end'])}]\n{t}\n\n" for p, t in zip(paras, orig_texts)),
            encoding="utf-8")
    display = args.display_name or DISPLAY_NAMES.get(speaker.lower(), speaker.title())
    vtt_rows = kept if caps or args.stt == "parakeet" else [
        Segment(p["start"], p["end"], t) for p, t in zip(paras, en_texts)]
    (out_dir / "transcript.vtt").write_text(
        "WEBVTT\n\n" + "".join(
            f"{vtt_ts(s.start)} --> {vtt_ts(s.end)}\n{display}: {s.text.strip()}\n\n" for s in vtt_rows if s.text.strip()),
        encoding="utf-8")
    (out_dir / "segments.json").write_text(json.dumps(
        [{"start": s.start, "end": s.end, "text": s.text, "original": s.original, "cluster": s.cluster,
          "sim": s.sim} for s in kept], ensure_ascii=False, indent=1), encoding="utf-8")
    report = {
        "link": link, "file": name, "duration": hms(meta["duration"]), "audio_method": meta.get("audio_method"),
        "text_source": method, "speaker": speaker, "match": ident.how, "chosen_cluster": ident.chosen,
        "kept_minutes": round(total, 1), "threshold": args.threshold,
        "clusters": [{k: v for k, v in c.items() if not k.startswith("_")} for c in ident.clusters[:10]],
    }
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.save_voiceprint and len(kept_X):
        v = kept_X.mean(0)
        v /= np.linalg.norm(v)
        VOICEPRINT_DIR.mkdir(exist_ok=True)
        vp = VOICEPRINT_DIR / f"{slugify(speaker).lower()}.npy"
        np.save(vp, v.astype(np.float32))
        log(f"saved voiceprint {vp.relative_to(ROOT)}")

    print("\nClusters (largest first):")
    for c in report["clusters"][:6]:
        mark = "  <== chosen" if c["cluster"] == ident.chosen else ""
        vp_sim = f" vp_sim={c['voiceprint_sim']}" if c["voiceprint_sim"] is not None else ""
        print(f"  cluster {c['cluster']}: {c['minutes']} min, {c['first']}-{c['last']}{vp_sim}{mark}")
        for smp in c["samples"][:3]:
            print(f"      {smp}")
    log(f"wrote {out_dir}")
    return out_dir


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("links", nargs="+", help="Drive links or file IDs (or a .txt/.csv file with one per line)")
    ap.add_argument("--speaker", help="speaker name; loads voiceprints/<name>.npy when it exists")
    ap.add_argument("--display-name", help="name used as the speaker label in transcript.vtt (default: Pratham Mittal for pratham)")
    ap.add_argument("--reference", help="audio/video file containing only the target speaker (10s+)")
    ap.add_argument("--reference-range", help="time range in the video where only the target speaks, e.g. 0:00:05-0:01:00")
    ap.add_argument("--auto", action="store_true", help="ignore saved voiceprint; pick the largest speaker")
    ap.add_argument("--pick-cluster", type=int, help="force a cluster id from a previous run's report")
    ap.add_argument("--save-voiceprint", action="store_true", help="save/overwrite voiceprints/<speaker>.npy from this run")
    ap.add_argument("--source", choices=["auto", "captions", "stt"], default="auto")
    ap.add_argument("--stt", choices=["openrouter", "parakeet"], default="openrouter",
                    help="used when no captions: openrouter=Whisper (Hindi/any), parakeet=local (English only)")
    ap.add_argument("--language", help="ISO code hint for Whisper, e.g. hi or en")
    ap.add_argument("--no-translate", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.5, help="min voice similarity to keep a segment")
    ap.add_argument("--cluster-threshold", type=float, default=0.75)
    ap.add_argument("--limit-minutes", type=float, help="only process the first N minutes (quick trial)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    links: list[str] = []
    for item in args.links:
        p = Path(item)
        if p.suffix in {".txt", ".csv"} and p.exists():
            links += re.findall(r"https?://drive\.google\.com/\S+?(?=[\s,\"']|$)", p.read_text())
        else:
            links.append(item)
    failures = []
    for link in links:
        try:
            process(link, args)
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            log(f"FAILED {link}: {exc}")
            failures.append(link)
    if failures:
        log(f"{len(failures)} of {len(links)} failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
