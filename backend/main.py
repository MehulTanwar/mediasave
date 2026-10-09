"""
MediaSave API
=============
A small FastAPI service that lets a person download media they own or are
authorised to download, using yt-dlp for *public, unauthenticated* content only.

What this service deliberately does NOT do:
  - accept cookies, passwords, tokens or any login material
  - bypass DRM, paywalls, private/members-only/age-gated content
  - bypass geo-restrictions (geo_bypass is disabled)
  - fetch playlists, channels or live streams

Flow:  POST /api/resolve  ->  POST /api/prepare  ->  GET /api/files/{token}
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from yt_dlp.utils import DownloadError, ExtractorError

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mediasave")


# --------------------------------------------------------------------------- #
# Configuration (all via environment variables, safe defaults for free tiers)
# --------------------------------------------------------------------------- #
def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


MAX_FILESIZE_MB = _int("MAX_FILESIZE_MB", 200)
MAX_DURATION_MIN = _int("MAX_DURATION_MIN", 60)
RATE_LIMIT_PER_MIN = _int("RATE_LIMIT_PER_MIN", 10)
MAX_CONCURRENT_JOBS = _int("MAX_CONCURRENT_JOBS", 2)
FILE_TTL_SECONDS = _int("FILE_TTL_SECONDS", 300)
TRUST_PROXY = os.getenv("TRUST_PROXY", "true").lower() == "true"
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:8080,http://127.0.0.1:8080,http://localhost:5500,http://127.0.0.1:5500",
    ).split(",")
    if o.strip()
]

# Platforms shown on the site. Keep in sync with frontend/index.html.
PLATFORM_DOMAINS = [
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "dai.ly",
    "soundcloud.com", "reddit.com", "redd.it", "x.com", "twitter.com",
    "tiktok.com", "facebook.com", "fb.watch", "instagram.com", "twitch.tv",
    "bandcamp.com", "archive.org",
]
ALLOWED_DOMAINS = [
    d.strip().lower()
    for d in os.getenv("ALLOWED_DOMAINS", ",".join(PLATFORM_DOMAINS)).split(",")
    if d.strip()
]

ROOT = Path(tempfile.gettempdir()) / "mediasave"
SIZE_MARKER = "mediasave-size-limit"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class AppError(Exception):
    """An error with a stable machine code and a message safe to show users."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def error_response(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #
def validate_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url or len(url) > 2048:
        raise AppError("INVALID_URL", "Paste a full link that starts with http:// or https://.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise AppError("INVALID_URL", "Paste a full link that starts with http:// or https://.")
    if parsed.username or parsed.password:
        raise AppError("INVALID_URL", "Links that contain a username or password aren't accepted.")
    try:
        port = parsed.port
    except ValueError:
        raise AppError("INVALID_URL", "That link has an invalid port number.")
    if port not in (None, 80, 443):
        raise AppError("INVALID_URL", "Links with custom ports aren't accepted.")

    host = parsed.hostname.lower().rstrip(".")
    if not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
        raise AppError(
            "UNSUPPORTED_PLATFORM",
            "That website isn't on the supported list. Check the supported platforms section.",
        )
    _assert_public_host(host)
    return url


def _assert_public_host(host: str) -> None:
    """Defence in depth against SSRF: the host must resolve to public IPs only."""
    try:
        results = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise AppError("INVALID_URL", "We couldn't find that website. Check the link for typos.")
    for *_, sockaddr in results:
        if not ipaddress.ip_address(sockaddr[0]).is_global:
            raise AppError("INVALID_URL", "That link points to a non-public address.")


def require_consent(confirmed: bool) -> None:
    if not confirmed:
        raise AppError(
            "CONSENT_REQUIRED",
            "Confirm that you own this media or have permission to download it.",
        )


# --------------------------------------------------------------------------- #
# Rate limiting and concurrency (in-memory; fine for a single small instance)
# --------------------------------------------------------------------------- #
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # The right-most entry is the one appended by our own proxy.
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(request: Request) -> None:
    ip, now = client_ip(request), time.monotonic()
    with _hits_lock:
        window = _hits[ip]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= RATE_LIMIT_PER_MIN:
            raise AppError("RATE_LIMITED", "Too many requests. Wait a minute and try again.", 429)
        window.append(now)
        if len(_hits) > 5000:  # keep memory bounded
            for key in [k for k, v in _hits.items() if not v]:
                del _hits[key]


# --------------------------------------------------------------------------- #
# yt-dlp helpers
# --------------------------------------------------------------------------- #
def base_opts() -> dict:
    """Public, unauthenticated access only. No cookies, no geo bypass."""
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreconfig": True,          # ignore any user/system yt-dlp config (e.g. cookies)
        "geo_bypass": False,           # never spoof location
        "allow_unplayable_formats": False,  # never touch DRM-protected streams
        "cachedir": False,
        "socket_timeout": 15,
        "retries": 2,
        "fragment_retries": 2,
    }


def translate_error(message: str) -> AppError:
    m = message.lower()
    if SIZE_MARKER in m or "larger than max" in m:
        return AppError("TOO_LARGE", f"This file is larger than the {MAX_FILESIZE_MB} MB limit.", 413)
    if "requested format is not available" in m:
        return AppError(
            "FORMAT_UNAVAILABLE",
            f"No version of this media is available within the {MAX_FILESIZE_MB} MB limit.",
            422,
        )
    if "drm" in m:
        return AppError("DRM_PROTECTED", "This media is DRM-protected. MediaSave can't download it.", 403)
    if "not a bot" in m or "bot" in m and "sign in" in m:
        return AppError(
            "UPSTREAM_BLOCKED",
            "The platform is not letting our server read this link right now. Try again later.",
            502,
        )
    if any(s in m for s in ("sign in", "log in", "login", "cookies", "private", "members", "premium",
                            "subscriber", "confirm your age", "age-restricted", "age restricted",
                            "authentication", "requires")):
        return AppError(
            "AUTH_REQUIRED",
            "This media needs a login or special access. MediaSave only handles public media.",
            403,
        )
    if any(s in m for s in ("geo", "your country", "your location", "region")):
        return AppError("GEO_RESTRICTED", "This media isn't available in the region our server is in.", 403)
    if "unsupported url" in m:
        return AppError("UNSUPPORTED_URL", "That link doesn't point to a downloadable video or audio file.", 422)
    if any(s in m for s in ("unavailable", "removed", "does not exist", "not found", "404", "deleted")):
        return AppError("UNAVAILABLE", "This media is unavailable. It may be deleted or private.", 404)
    if "429" in m or "too many requests" in m:
        return AppError("UPSTREAM_RATE_LIMITED", "The platform is limiting requests. Try again in a few minutes.", 502)
    if "timed out" in m or "timeout" in m:
        return AppError("UPSTREAM_TIMEOUT", "The platform took too long to respond. Try again.", 504)
    return AppError("UPSTREAM_ERROR", "We couldn't read that link. Check it and try again.", 502)


def check_info(info: dict | None) -> dict:
    if not info:
        raise AppError("UNAVAILABLE", "This media is unavailable. It may be deleted or private.", 404)
    if info.get("_type") in ("playlist", "multi_video"):
        raise AppError("PLAYLIST_UNSUPPORTED", "Playlists and channels aren't supported. Paste a link to a single item.", 422)
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise AppError("LIVE_UNSUPPORTED", "Live streams can't be downloaded.", 422)
    if info.get("availability") in ("needs_auth", "subscriber_only", "premium_only", "private"):
        raise AppError("AUTH_REQUIRED", "This media needs a login or special access. MediaSave only handles public media.", 403)
    formats = info.get("formats") or []
    if formats and all(f.get("has_drm") is True for f in formats):
        raise AppError("DRM_PROTECTED", "This media is DRM-protected. MediaSave can't download it.", 403)
    duration = info.get("duration")
    if duration and duration > MAX_DURATION_MIN * 60:
        raise AppError("TOO_LONG", f"This media is longer than the {MAX_DURATION_MIN}-minute limit.", 413)
    return info


def fetch_info(url: str) -> dict:
    try:
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except (DownloadError, ExtractorError) as exc:
        raise translate_error(str(exc)) from exc
    except Exception:  # noqa: BLE001
        log.exception("Unexpected extractor failure")
        raise AppError("UPSTREAM_ERROR", "We couldn't read that link. Try again later.", 502)
    return check_info(info)


def summarize(info: dict) -> dict:
    formats = [f for f in (info.get("formats") or []) if f.get("has_drm") is not True]
    if formats:
        has_video = any(f.get("vcodec") not in (None, "none") for f in formats)
        has_audio = any(f.get("acodec") not in (None, "none") for f in formats)
    else:
        has_video = has_audio = True
    thumb = info.get("thumbnail")
    return {
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": thumb if isinstance(thumb, str) and thumb.startswith("https://") else None,
        "platform": info.get("extractor_key"),
        "has_video": has_video,
        "has_audio": has_audio,
        "max_filesize_mb": MAX_FILESIZE_MB,
    }


def run_download(url: str, mode: str) -> Path:
    """Download one file into a private temp dir and return its path."""
    workdir = Path(tempfile.mkdtemp(prefix="job_", dir=ROOT))
    limit = MAX_FILESIZE_MB * 1024 * 1024
    size = f"[filesize<?{MAX_FILESIZE_MB}M][filesize_approx<?{MAX_FILESIZE_MB}M]"
    # Single-file formats only, so ffmpeg is not required on the server.
    fmt = f"bestaudio[ext=m4a]{size}/bestaudio{size}" if mode == "audio" else f"best[ext=mp4]{size}/best{size}"

    def hook(d: dict) -> None:
        if d.get("status") == "downloading" and (d.get("downloaded_bytes") or 0) > limit:
            raise DownloadError(SIZE_MARKER)

    opts = base_opts() | {
        "format": fmt,
        "outtmpl": str(workdir / "media.%(ext)s"),
        "max_filesize": limit,
        "progress_hooks": [hook],
        "restrictfilenames": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [p for p in workdir.iterdir() if p.is_file() and p.suffix not in (".part", ".ytdl", ".temp")]
        if not files:
            raise AppError("TOO_LARGE", f"This file is larger than the {MAX_FILESIZE_MB} MB limit.", 413)
        return max(files, key=lambda p: p.stat().st_size)
    except (DownloadError, ExtractorError) as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise translate_error(str(exc)) from exc
    except AppError:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        log.exception("Unexpected download failure")
        raise AppError("INTERNAL", "Something went wrong while preparing your file.", 500)


def safe_filename(title: str | None, suffix: str) -> str:
    stem = re.sub(r"[^\w\s\-().]", "", title or "", flags=re.UNICODE).strip()[:80] or "media"
    return f"{stem}{suffix}"


# --------------------------------------------------------------------------- #
# Temporary file registry
# --------------------------------------------------------------------------- #
@dataclass
class Entry:
    path: Path
    name: str
    expires: float


FILES: dict[str, Entry] = {}
_files_lock = threading.Lock()


def purge_expired() -> None:
    now = time.time()
    with _files_lock:
        expired = [t for t, e in FILES.items() if e.expires < now]
        entries = [FILES.pop(t) for t in expired]
    for entry in entries:
        shutil.rmtree(entry.path.parent, ignore_errors=True)


async def janitor() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            purge_expired()
        except Exception:  # noqa: BLE001
            log.exception("Cleanup failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(janitor())
    yield
    task.cancel()
    shutil.rmtree(ROOT, ignore_errors=True)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = FastAPI(title="MediaSave API", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    expose_headers=["Content-Disposition"],
)


@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError):
    return error_response(exc.code, exc.message, exc.status)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, __: RequestValidationError):
    return error_response("INVALID_REQUEST", "The request was malformed. Reload the page and try again.", 422)


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception):
    log.exception("Unhandled error: %s", type(exc).__name__)
    return error_response("INTERNAL", "Something went wrong on our side. Try again in a moment.", 500)


class ResolveRequest(BaseModel):
    url: str = Field(max_length=2048)
    confirm_rights: bool = False


class PrepareRequest(ResolveRequest):
    mode: Literal["video", "audio"] = "video"


@app.get("/")
def root():
    return {"service": "MediaSave API", "status": "ok"}


@app.get("/api/health")
def health():
    return {"status": "ok", "yt_dlp": yt_dlp.version.__version__}


@app.post("/api/resolve")
def resolve(body: ResolveRequest, request: Request):
    """Read public metadata for a link. Nothing is downloaded."""
    enforce_rate_limit(request)
    require_consent(body.confirm_rights)
    url = validate_url(body.url)
    info = fetch_info(url)
    log.info("resolve ok host=%s", urlparse(url).hostname)
    return summarize(info)


@app.post("/api/prepare")
def prepare(body: PrepareRequest, request: Request):
    """Download to temporary storage and return a short-lived file link."""
    enforce_rate_limit(request)
    require_consent(body.confirm_rights)
    url = validate_url(body.url)

    if not _slots.acquire(blocking=False):
        raise AppError("BUSY", "The server is busy with other downloads. Try again in a minute.", 503)
    try:
        info = fetch_info(url)
        path = run_download(url, body.mode)
    finally:
        _slots.release()

    token = secrets.token_urlsafe(24)
    name = safe_filename(info.get("title"), path.suffix)
    with _files_lock:
        FILES[token] = Entry(path=path, name=name, expires=time.time() + FILE_TTL_SECONDS)
    log.info("prepare ok host=%s mode=%s bytes=%d", urlparse(url).hostname, body.mode, path.stat().st_size)
    return {
        "download_url": f"/api/files/{token}",
        "filename": name,
        "size_bytes": path.stat().st_size,
        "expires_in": FILE_TTL_SECONDS,
    }


@app.get("/api/files/{token}")
def get_file(token: str):
    with _files_lock:
        entry = FILES.get(token)
    if not entry or entry.expires < time.time() or not entry.path.exists():
        raise AppError("FILE_EXPIRED", "This download has expired. Go back and prepare it again.", 404)
    media_type = mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
    return FileResponse(entry.path, filename=entry.name, media_type=media_type, headers={"Cache-Control": "no-store"})
