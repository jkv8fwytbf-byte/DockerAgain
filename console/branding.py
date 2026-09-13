"""
Settings page backend: school and class branding, the sign-in page logo and
the "About this server" block.

The Hub reads branding.json and logo.png on every page render (see
_branding() in jupyterhub_config.py), so nothing here restarts anything: a
saved change is live on the sign-in page straight away.

    GET    /api/settings             current values + logo + about
    PUT    /api/settings             save the text fields (validated)
    POST   /api/settings/logo        multipart "file": PNG or JPEG -> logo.png
    DELETE /api/settings/logo        back to the built-in logo
    GET    /api/settings/detect-url  the address the teacher's browser is using

The logo upload is streamed through python-multipart with a hard byte cap, so
an oversized file is refused while it arrives and nothing is spooled to disk.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import io
import ipaddress
import logging
import os
import platform
import re
import socket
import threading
import time
from functools import lru_cache
from urllib.parse import urlsplit

import psutil
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import parse_options_header

from .auth import require_admin
from .errors import ApiError, HubError, HubUnavailable
from .util import (
    ACCENTS,
    BRANDING_DEFAULTS,
    BRANDING_LIMITS,
    atomic_write_bytes,
    atomic_write_json,
    iso_from_ts,
    iso_now,
    read_branding,
    read_json,
)

log = logging.getLogger("console.branding")
router = APIRouter()

TEXT_FIELDS = ("school_name", "class_name", "announcement", "class_url")
FIELD_LABELS = {
    "school_name": "School name",
    "class_name": "Class name",
    "accent": "Accent colour",
    "announcement": "Announcement",
    "class_url": "Class address",
}
MAX_LOGO_PX = 2000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
SVG_MESSAGE = "Please upload a PNG or JPEG (SVG cannot be shown by the login page)."
NOT_IMAGE_MESSAGE = "That file doesn't look like a PNG or JPEG image. Please upload a PNG or JPEG."
DAMAGED_MESSAGE = "That image can't be opened — it may be damaged. Try exporting it again as a PNG or JPEG."

# Hidden control characters (C0 except the whitespace we normalise, DEL, C1,
# line/paragraph separators) are refused; invisible formatting characters
# that can spoof text are silently dropped.
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029]")
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_HOST = re.compile(r"^(?:[A-Za-z0-9_](?:[A-Za-z0-9_\-]{0,62}[A-Za-z0-9_])?(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_\-]{0,62}[A-Za-z0-9_])?)*\.?|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")

_write_lock = threading.Lock()
_PYTHON_VERSION = platform.python_version()
try:
    _STARTED = iso_from_ts(psutil.Process().create_time())
except Exception:  # noqa: BLE001 - psutil quirks must never break the page
    _STARTED = iso_from_ts(time.time())


# --- validation -------------------------------------------------------------------

def clean_text(value: str, *, multiline: bool = False) -> str:
    """Normalise whitespace and drop invisible formatting characters. Raises ValueError on control characters."""
    value = _INVISIBLE.sub("", value or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    if not multiline:
        value = value.replace("\n", " ")
    if _CONTROL.search(value):
        raise ValueError("can't contain hidden control characters. Try typing it again instead of pasting.")
    lines = [re.sub(r" {2,}", " ", line.strip()) for line in value.split("\n")]
    value = "\n".join(lines).strip()
    while "\n\n\n" in value:
        value = value.replace("\n\n\n", "\n\n")
    return value


def clean_url(value: str) -> str:
    """'' or an absolute http(s) URL with a host. Raises ValueError with a friendly message."""
    value = clean_text(value)
    if not value:
        return ""
    if any(ch.isspace() for ch in value):
        raise ValueError("can't contain spaces. Example: http://192.168.1.10:8000")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("has a port that isn't valid. Example: http://192.168.1.10:8000") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("must start with http:// or https://. Example: http://192.168.1.10:8000")
    if not parts.netloc or parts.username is not None or parts.password is not None:
        raise ValueError("needs the computer's name or IP address after http://. Example: http://192.168.1.10:8000")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("has a port that isn't valid. Example: http://192.168.1.10:8000")
    if not _HOST.match(parts.netloc):
        raise ValueError("has characters that can't be part of an address. Example: http://192.168.1.10:8000")
    return value


def validate_branding(values: dict) -> tuple[dict, dict]:
    """(clean values, {field: message}) for the five editable fields."""
    clean: dict = {}
    errors: dict = {}
    for field in TEXT_FIELDS:
        raw = values.get(field, "")
        raw = "" if raw is None else str(raw)
        try:
            text = clean_url(raw) if field == "class_url" else clean_text(raw, multiline=field == "announcement")
        except ValueError as e:
            errors[field] = str(e)
            continue
        limit = BRANDING_LIMITS[field]
        if len(text) > limit:
            errors[field] = f"is too long — keep it under {limit} characters (you have {len(text)})."
            continue
        clean[field] = text
    accent = str(values.get("accent") or "").strip().lower()
    if accent not in ACCENTS:
        errors["accent"] = "must be one of the colours shown."
    else:
        clean["accent"] = accent
    return clean, errors


def raise_field_errors(errors: dict) -> None:
    first = next(iter(errors.items()))
    message = f"{FIELD_LABELS.get(first[0], first[0])} {first[1]}"
    if len(errors) > 1:
        message += f" (and {len(errors) - 1} more field{'s' if len(errors) > 2 else ''} to check)"
    raise ApiError(400, "validation_failed", message, {"fields": errors})


# --- branding.json -----------------------------------------------------------------

def _write_branding(path: str, values: dict, *, logo_custom: bool | None = None) -> dict:
    """Read-modify-write under a lock; only known keys are stored; 0600 and atomic."""
    with _write_lock:
        current = read_branding(path)
        data = {k: current[k] for k in BRANDING_DEFAULTS}
        data.update({k: v for k, v in values.items() if k in BRANDING_DEFAULTS})
        if logo_custom is not None:
            data["logo_custom"] = bool(logo_custom)
        data["updated_at"] = iso_now()
        atomic_write_json(path, data, mode=0o600)
        return data


def _logo_version(path: str) -> int:
    try:
        return int(os.stat(path).st_mtime)
    except OSError:
        return 0


def _install_logo(settings, data: bytes, *, custom: bool) -> None:
    """Replace logo.png atomically (0644) and make sure its version number moves on."""
    previous = _logo_version(settings.logo_file)
    atomic_write_bytes(settings.logo_file, data, 0o644)
    if _logo_version(settings.logo_file) <= previous:
        # Two changes inside one second would share a ?v= value and browsers would
        # keep the old picture; nudge the mtime so the cache-buster changes.
        os.utime(settings.logo_file, (time.time(), previous + 1))
    _write_branding(settings.branding_json, {}, logo_custom=custom)


def convert_logo(data: bytes, max_px: int = MAX_LOGO_PX) -> bytes:
    """Validate an uploaded PNG/JPEG and return it as RGBA PNG bytes. Raises ApiError."""
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<?xml", b"<svg", b"<!doctype svg")) or b"<svg" in head:
        raise ApiError(400, "unsupported_image", SVG_MESSAGE)
    if not (data.startswith(PNG_MAGIC) or data.startswith(JPEG_MAGIC)):
        raise ApiError(400, "unsupported_image", NOT_IMAGE_MESSAGE)
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            probe.verify()
    except Exception:  # noqa: BLE001 - Pillow raises many different things for bad files
        raise ApiError(400, "unsupported_image", DAMAGED_MESSAGE) from None
    if fmt not in ("PNG", "JPEG"):
        raise ApiError(400, "unsupported_image", NOT_IMAGE_MESSAGE)
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            if width < 1 or height < 1 or width > max_px or height > max_px:
                raise ApiError(
                    400, "unsupported_image",
                    f"That image is {width} × {height} pixels — please use one that is at most {max_px} × {max_px}. "
                    "A logo around 200–600 pixels tall is plenty.",
                )
            im = ImageOps.exif_transpose(im) or im
            rgba = im.convert("RGBA")
            out = io.BytesIO()
            rgba.save(out, format="PNG", optimize=True)
    except ApiError:
        raise
    except Exception:  # noqa: BLE001
        raise ApiError(400, "unsupported_image", DAMAGED_MESSAGE) from None
    return out.getvalue()


# --- about ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _jupyterlab_version() -> str | None:
    try:
        return importlib.metadata.version("jupyterlab")
    except importlib.metadata.PackageNotFoundError:
        return None


def _addresses() -> list[str]:
    """IPv4 addresses of this machine, loopback and link-local left out."""
    out: list[str] = []
    try:
        interfaces = psutil.net_if_addrs()
    except Exception:  # noqa: BLE001
        return out
    for _name, addrs in sorted(interfaces.items()):
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            try:
                ip = ipaddress.ip_address(a.address)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                continue
            if str(ip) not in out:
                out.append(str(ip))
    return out


async def _hub_version(request: Request) -> str | None:
    """JupyterHub version from /hub/api/info, remembered once it has been seen."""
    state = request.app.state
    cached = getattr(state, "branding_hub_version", None)
    if cached:
        return cached
    try:
        info = await state.hub.info()
    except (HubUnavailable, HubError) as e:
        log.debug("hub version unavailable: %s", e)
        return None
    version = info.get("version") if isinstance(info, dict) else None
    version = str(version) if version else None
    if version:
        state.branding_hub_version = version
    return version


def detected_origin(request: Request) -> str | None:
    """'<proto>://<host>/' as seen by the browser (through the proxy) or None."""
    proto = (request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme or "http").lower()
    host = (request.headers.get("x-forwarded-host", "") or request.headers.get("host", "")).split(",")[0].strip()
    if proto not in ("http", "https") or not host or not _HOST.match(host):
        return None
    return f"{proto}://{host}/"


# --- payload -----------------------------------------------------------------------

def _snapshot(settings) -> dict:
    """Everything that touches the disk, run in a worker thread."""
    raw = read_json(settings.branding_json, {}) or {}
    updated_at = raw.get("updated_at") if isinstance(raw, dict) else None
    return {
        "branding": read_branding(settings.branding_json),
        "updated_at": str(updated_at) if updated_at else None,
        "logo_version": _logo_version(settings.logo_file),
        "addresses": _addresses(),
    }


async def build_payload(request: Request) -> dict:
    settings = request.app.state.settings
    snap = await asyncio.to_thread(_snapshot, settings)
    hub_version = await _hub_version(request)
    branding = snap["branding"]
    return {
        "school_name": branding["school_name"],
        "class_name": branding["class_name"],
        "accent": branding["accent"],
        "accent_hex": branding["accent_hex"],
        "announcement": branding["announcement"],
        "class_url": branding["class_url"],
        "class_url_effective": branding["class_url"] or detected_origin(request) or "",
        "updated_at": snap["updated_at"],
        "logo": {
            "custom": bool(branding["logo_custom"]),
            "url": f"{settings.prefix}/public/logo?v={snap['logo_version']}",
            "version": snap["logo_version"],
        },
        "limits": {**BRANDING_LIMITS, "max_logo_bytes": settings.max_logo_bytes, "max_logo_px": MAX_LOGO_PX},
        "about": {
            "image_version": settings.image_version,
            "jupyterhub": hub_version,
            "jupyterlab": _jupyterlab_version(),
            "python": _PYTHON_VERSION,
            "addresses": snap["addresses"],
            "started": _STARTED,
            "accents": dict(ACCENTS),
        },
    }


def _respond(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


# --- routes ------------------------------------------------------------------------

class BrandingIn(BaseModel):
    """Fields left out keep their current value; nothing else is accepted as a non-string."""

    model_config = ConfigDict(extra="ignore")
    school_name: str | None = None
    class_name: str | None = None
    accent: str | None = None
    announcement: str | None = None
    class_url: str | None = None


@router.get("/api/settings")
async def get_settings(request: Request, user: str = Depends(require_admin)):
    return _respond(await build_payload(request))


@router.put("/api/settings")
async def put_settings(request: Request, body: BrandingIn, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    current = await asyncio.to_thread(read_branding, settings.branding_json)
    merged = {k: current[k] for k in (*TEXT_FIELDS, "accent")}
    merged.update({k: v for k, v in body.model_dump().items() if v is not None})
    clean, errors = validate_branding(merged)
    if errors:
        raise_field_errors(errors)
    await asyncio.to_thread(_write_branding, settings.branding_json, clean)
    log.info("branding updated by %s (accent=%s)", user, clean["accent"])
    return _respond(await build_payload(request))


class _TooLarge(Exception):
    pass


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


class _LogoReceiver:
    """
    Callbacks for python_multipart's streaming parser. Only the part named
    "file" is kept (in memory, at most `cap` bytes); every other part is
    skipped, so a stray extra field cannot grow the request either.
    """

    def __init__(self, cap: int):
        self.cap = cap
        self.data = bytearray()
        self.filename = ""
        self.seen_file = False
        self._collect = False
        self._hname = b""
        self._hval = b""
        self._headers: list[tuple[bytes, bytes]] = []

    def on_part_begin(self) -> None:
        self._collect = False
        self._headers = []

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._hname += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._hval += data[start:end]

    def on_header_end(self) -> None:
        self._headers.append((self._hname.lower(), bytes(self._hval)))
        self._hname = b""
        self._hval = b""

    def on_headers_finished(self) -> None:
        disp = next((v for k, v in self._headers if k == b"content-disposition"), b"")
        _, opts = parse_options_header(disp)
        if self.seen_file or _decode(opts.get(b"name")) != "file":
            return
        self.seen_file = True
        self.filename = _decode(opts.get(b"filename"))
        self._collect = True

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if not self._collect:
            return
        if len(self.data) + (end - start) > self.cap:
            raise _TooLarge()
        self.data += data[start:end]

    def on_part_end(self) -> None:
        self._collect = False


async def _receive_logo(request: Request, limit: int) -> bytes:
    """
    Stream the multipart body and return the bytes of its "file" part.
    Refuses before reading (Content-Length) and while reading (running total),
    so a huge upload never lands in memory or on disk.
    """
    friendly = f"{limit / 1024 / 1024:.0f} MB"
    too_large = ApiError(413, "logo_too_large", f"That image is bigger than {friendly}. Please use a smaller file.")
    slack = 64 * 1024                                   # boundaries, part headers, a small extra field
    declared = request.headers.get("content-length", "").strip()
    if declared.isdigit() and int(declared) > limit + slack:
        raise too_large
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise ApiError(400, "validation_failed", "Choose a PNG or JPEG image to upload.", {"fields": {"file": "field required"}})
    rx = _LogoReceiver(limit)
    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": rx.on_part_begin, "on_part_data": rx.on_part_data, "on_part_end": rx.on_part_end,
            "on_header_field": rx.on_header_field, "on_header_value": rx.on_header_value,
            "on_header_end": rx.on_header_end, "on_headers_finished": rx.on_headers_finished,
        },
        max_size=limit + slack,
    )
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit + slack:            # python-multipart would silently truncate; stop reading instead
                raise _TooLarge()
            parser.write(chunk)
        parser.finalize()
    except _TooLarge:
        raise too_large from None
    except FormParserError:
        raise ApiError(400, "bad_upload", "The upload was cut short or malformed. Please try again.") from None
    if not rx.seen_file:
        raise ApiError(400, "validation_failed", "Choose a PNG or JPEG image to upload.", {"fields": {"file": "field required"}})
    if not rx.data:
        raise ApiError(400, "unsupported_image", "That file is empty. Please choose a PNG or JPEG image.")
    return bytes(rx.data)


@router.post("/api/settings/logo")
async def upload_logo(request: Request, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    data = await _receive_logo(request, settings.max_logo_bytes)
    png = await asyncio.to_thread(convert_logo, data, MAX_LOGO_PX)
    await asyncio.to_thread(_install_logo, settings, png, custom=True)
    log.info("logo replaced by %s (%d bytes uploaded, %d bytes stored)", user, len(data), len(png))
    return _respond(await build_payload(request))


@router.delete("/api/settings/logo")
async def remove_logo(request: Request, user: str = Depends(require_admin)):
    settings = request.app.state.settings

    def restore() -> None:
        try:
            with open(settings.default_logo, "rb") as fh:
                data = fh.read()
        except OSError as e:
            raise ApiError(500, "default_logo_missing", "The built-in logo is missing from this server image, so the logo can't be reset right now.") from e
        _install_logo(settings, data, custom=False)

    await asyncio.to_thread(restore)
    log.info("logo reset to default by %s", user)
    return _respond(await build_payload(request))


@router.get("/api/settings/detect-url")
async def detect_url(request: Request, user: str = Depends(require_admin)):
    origin = detected_origin(request)
    if not origin:
        raise ApiError(400, "no_address", "Couldn't work out the address from your browser. Type it in by hand, for example http://192.168.1.10:8000")
    return _respond({"url": origin})


# --- app hooks ----------------------------------------------------------------------

def setup(app) -> None:
    """Share the accent list with the page templates so settings.html draws its swatches from it."""
    app.state.templates.env.globals.setdefault("accents", dict(ACCENTS))
    if not hasattr(app.state, "branding_hub_version"):
        app.state.branding_hub_version = None
