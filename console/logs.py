"""
Logs: follow the Hub and console log files and download them.

    GET /api/logs?source=hub|console&after=<offset>&limit=200&filter=text
        No `after`: the last `limit` matching entries (read backwards in
        64 KiB blocks, at most settings.log_tail_max_bytes). With `after`:
        only the bytes appended since that offset. If the file got smaller
        (rotated) the tail is returned again with rotated=true.
    GET /api/logs/files?source=      the current and rotated files with sizes
    GET /api/logs/download?source=&file=jupyterhub.log.1   plain-text attachment

Log lines look like "[I 2026-09-13 08:00:00 JupyterHub app:123] message"
(level letter, timestamp, logger, optional module:line). Lines that do not
start that way (tracebacks) continue the previous entry.

When a poll lands between a header line and its traceback (the record was
flushed in two writes, or the byte budget cut inside it), the next response
starts with a continuation entry: it carries the ts/level/logger of the header
it belongs to (found by looking back in the file), `msg` holds only the new
lines, `raw` is that header line plus the new lines, and "continues": true
tells the page to append it to the entry it already shows for that header.

Timestamps are the server's local time (the container runs on UTC); every
response carries `utc_offset` (seconds east of UTC) so the page can show them
in the browser's local time.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from .auth import require_admin
from .errors import ApiError
from .util import UnsafePath, iso_from_ts, safe_join

router = APIRouter()

SOURCES = ("hub", "console")
SOURCE_BASENAMES = {"hub": "jupyterhub", "console": "console"}
FILE_RE = re.compile(r"(jupyterhub|console)\.log(?:\.(\d{1,2}))?")   # always used with fullmatch(): "$" would accept a trailing newline
LINE_RE = re.compile(
    r"^\[([A-Z])\s(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)\s([^\s\]]+)(?:\s([^\]]*))?\]\s?(.*)$"
)
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LEVELS = {"D": "DEBUG", "I": "INFO", "W": "WARNING", "E": "ERROR", "C": "CRITICAL"}
BLOCK = 64 * 1024
LIMIT_DEFAULT, LIMIT_MAX = 200, 5000
FILTER_MAX = 200


# --- parsing --------------------------------------------------------------------

def _clean(raw: bytes) -> str:
    return ANSI_RE.sub("", raw.rstrip(b"\r").decode("utf-8", "replace"))


def _entry(match: re.Match | None, header: str, cont: list[str]) -> dict:
    """One log entry from its header line and the continuation lines that follow it."""
    cont = list(cont)
    while cont and not cont[-1].strip():
        cont.pop()
    tail_text = "\n".join(cont)
    if match is None:                         # lines before any header (or an unknown format)
        msg = header if not tail_text else f"{header}\n{tail_text}"
        return {"ts": "", "level": "OTHER", "logger": "", "msg": msg, "raw": msg}
    letter, ts, logger, _location, msg = match.groups()
    raw = header if not tail_text else f"{header}\n{tail_text}"
    if tail_text:
        msg = f"{msg}\n{tail_text}"
    return {"ts": ts, "level": LEVELS.get(letter, "OTHER"), "logger": logger, "msg": msg, "raw": raw}


def _continuation(header: tuple[re.Match, str] | None, cont: list[str]) -> dict:
    """Continuation lines whose header was already returned by an earlier poll (see the module docstring)."""
    cont = list(cont)
    while cont and not cont[-1].strip():
        cont.pop()
    text = "\n".join(cont)
    if header is None or not text:            # nothing but header-less lines before them (or only blank lines)
        return {"ts": "", "level": "OTHER", "logger": "", "msg": text, "raw": text, "continues": True}
    entry = _entry(header[0], header[1], [])
    entry["msg"] = text
    entry["raw"] = f"{header[1]}\n{text}"
    entry["continues"] = True
    return entry


def _matches(entry: dict, needle: str) -> bool:
    return not needle or needle in entry["raw"].lower()


@dataclass
class LogChunk:
    lines: list[dict] = field(default_factory=list)
    cursor: int = 0
    truncated: bool = False
    size: int = 0
    rotated: bool = False


# --- readers (blocking; call through asyncio.to_thread) --------------------------------

def _end_of_last_line(fh, size: int, max_bytes: int, block: int) -> int:
    """Offset just after the last newline, so a half-written last line is never returned."""
    pos = size
    scanned = 0
    while pos > 0 and scanned < max_bytes:
        n = min(block, pos)
        fh.seek(pos - n)
        chunk = fh.read(n)
        scanned += n
        i = chunk.rfind(b"\n")
        if i >= 0:
            return pos - n + i + 1
        pos -= n
    return 0


def _header_before(fh, end: int, max_bytes: int, block: int) -> tuple[re.Match, str] | None:
    """The newest header line that ends at or before offset `end` (a line boundary), looking back at most max_bytes."""
    pos = end
    carry = b""
    scanned = 0
    while pos > 0 and scanned < max_bytes:
        n = min(block, pos)
        pos -= n
        fh.seek(pos)
        data = fh.read(n) + carry
        scanned += n
        parts = data.split(b"\n")
        carry = parts[0]
        for raw in reversed(parts[1:]):
            text = _clean(raw)
            m = LINE_RE.match(text)
            if m:
                return m, text
    if pos == 0 and carry:
        text = _clean(carry)
        m = LINE_RE.match(text)
        if m:
            return m, text
    return None


def tail(path: str, limit: int, filter_text: str = "", max_bytes: int = 4 * 1024 * 1024, *, block: int = BLOCK) -> LogChunk:
    """The last `limit` entries whose text contains filter_text, oldest first."""
    needle = filter_text.lower()
    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        return LogChunk()
    with fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        end = _end_of_last_line(fh, size, max_bytes, block)
        entries: list[dict] = []        # newest first while collecting
        cont: list[str] = []            # continuation lines (newest first) waiting for their header
        carry = b""
        pos = end
        spent = 0
        first = True
        done = False
        reached_start = False

        def handle(raw: bytes) -> bool:
            text = _clean(raw)
            m = LINE_RE.match(text)
            if not m:
                cont.append(text)
                return False
            entry = _entry(m, text, list(reversed(cont)))
            cont.clear()
            if _matches(entry, needle):
                entries.append(entry)
            return len(entries) >= limit

        while pos > 0 and not done:
            n = min(block, pos, max_bytes - spent)
            if n <= 0:
                break
            pos -= n
            fh.seek(pos)
            data = fh.read(n) + carry
            spent += n
            if first:
                first = False
                if data.endswith(b"\n"):
                    data = data[:-1]
            parts = data.split(b"\n")
            carry = parts[0]
            for raw in reversed(parts[1:]):
                if handle(raw):
                    done = True
                    break
        if not done and pos == 0:
            reached_start = True
            if carry:
                handle(carry)
        if cont and len(entries) < limit:           # header-less lines at the start of what we read
            entry = _entry(None, cont[-1], list(reversed(cont[:-1])))
            if entry["raw"].strip() and _matches(entry, needle):
                entries.append(entry)
        entries.reverse()
        return LogChunk(entries, end, not reached_start, size)


def read_since(path: str, after: int, limit: int, filter_text: str = "", max_bytes: int = 4 * 1024 * 1024, *, block: int = BLOCK) -> LogChunk:
    """Entries appended after byte offset `after`; the tail again (rotated=True) if the file shrank."""
    needle = filter_text.lower()
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        size = 0
    if size < after:
        chunk = tail(path, limit, filter_text, max_bytes, block=block)
        chunk.rotated = True
        return chunk
    if size == after:
        return LogChunk([], after, False, size)
    with open(path, "rb") as fh:
        fh.seek(after)
        data = fh.read(min(size - after, max_bytes))
        nl = data.rfind(b"\n")
        if nl < 0:
            if len(data) < max_bytes:                 # a line still being written: wait for its newline
                return LogChunk([], after, False, size)
            entry = _entry(None, _clean(data), [])     # a single line bigger than the budget: never get stuck on it
            return LogChunk([entry] if _matches(entry, needle) else [], after + len(data), True, size)
        complete = data[: nl + 1]
        lines = complete.split(b"\n")[:-1]
        # The cursor may sit between a header line and its traceback: the lines we start with then
        # belong to the entry the previous poll already returned, so hand them back as a continuation.
        continued: tuple[re.Match, str] | None = None
        continuing = after > 0 and not LINE_RE.match(_clean(lines[0]))
        if continuing:
            continued = _header_before(fh, after, max_bytes, block)
    hit_budget = (size - after) > len(data)
    entries: list[dict] = []
    header: tuple[re.Match | None, str] | None = None
    cont: list[str] = []
    offset = after
    truncated = False

    def flush() -> None:
        nonlocal continuing
        if continuing:
            continuing = False
            entry = _continuation(continued, cont)
        elif header is None:
            return
        else:
            entry = _entry(header[0], header[1], cont)
        if entry["raw"].strip() and _matches(entry, needle):
            entries.append(entry)

    for raw in lines:
        text = _clean(raw)
        m = LINE_RE.match(text)
        if m or (header is None and not continuing):
            flush()
            if len(entries) >= limit:            # this line starts the entry after our cut-off
                truncated = True
                break
            header, cont = (m, text), []
        else:
            cont.append(text)
        offset += len(raw) + 1
    else:
        flush()
    return LogChunk(entries, offset, truncated or hit_budget, size)


def list_files(log_dir: str, source: str) -> list[dict]:
    base = SOURCE_BASENAMES[source]
    found = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []
    for name in names:
        m = FILE_RE.fullmatch(name)
        if not m or m.group(1) != base:
            continue
        path = os.path.join(log_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        st = os.stat(path)
        found.append((int(m.group(2) or 0), {"name": name, "size": st.st_size, "modified": iso_from_ts(st.st_mtime), "current": not m.group(2)}))
    found.sort(key=lambda t: t[0])
    return [f for _, f in found]


def _download_name(path: str, name: str) -> str:
    m = FILE_RE.fullmatch(name)
    base, part = (m.group(1), m.group(2)) if m else ("log", None)
    try:
        stamp = datetime.fromtimestamp(os.stat(path).st_mtime)
    except OSError:
        stamp = datetime.fromtimestamp(time.time())
    suffix = f"-part{part}" if part else ""
    return f"{base}-{stamp:%Y%m%d-%H%M}{suffix}.log"


def utc_offset() -> int:
    """Seconds east of UTC of the clock that stamps the log lines (logging uses the server's local time)."""
    return int(time.localtime().tm_gmtoff)


def _source_path(settings, source: str) -> str:
    return settings.hub_log_file if source == "hub" else settings.console_log_file


# --- routes ---------------------------------------------------------------------

@router.get("/api/logs")
async def get_logs(
    request: Request,
    source: Literal["hub", "console"] = Query("hub"),
    after: int | None = Query(None, ge=0),
    limit: int = Query(LIMIT_DEFAULT, ge=1, le=LIMIT_MAX),
    filter_text: str = Query("", alias="filter", max_length=FILTER_MAX),
    user: str = Depends(require_admin),
):
    settings = request.app.state.settings
    path = _source_path(settings, source)
    budget = settings.log_tail_max_bytes
    try:
        if after is None:
            chunk = await asyncio.to_thread(tail, path, limit, filter_text, budget)
        else:
            chunk = await asyncio.to_thread(read_since, path, after, limit, filter_text, budget)
    except OSError as e:
        raise ApiError(500, "log_unreadable", "Can't read the log file right now.", {"reason": e.strerror or str(e)}) from e
    body = {
        "source": source,
        "file": os.path.basename(path),
        "lines": chunk.lines,
        "cursor": chunk.cursor,
        "truncated": chunk.truncated,
        "rotated": chunk.rotated,
        "size": chunk.size,
        "utc_offset": utc_offset(),
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.get("/api/logs/files")
async def get_log_files(
    request: Request,
    source: Literal["hub", "console"] = Query("hub"),
    user: str = Depends(require_admin),
):
    settings = request.app.state.settings
    files = await asyncio.to_thread(list_files, settings.log_dir, source)
    return JSONResponse({"source": source, "files": files}, headers={"Cache-Control": "no-store"})


@router.get("/api/logs/download")
async def download_log(
    request: Request,
    source: Literal["hub", "console"] = Query("hub"),
    file: str | None = Query(None, max_length=40),
    user: str = Depends(require_admin),
):
    settings = request.app.state.settings
    name = file or os.path.basename(_source_path(settings, source))
    if not FILE_RE.fullmatch(name):
        raise ApiError(400, "bad_name", "That is not one of the log files.")
    # The name is a plain file name by construction (regex above); a planted link is "not there".
    candidate = os.path.join(settings.log_dir, name)
    if os.path.islink(candidate) or not os.path.isfile(candidate):
        raise ApiError(404, "not_found", "That log file does not exist any more (older files are removed as the log rotates).")
    try:
        path = safe_join(settings.log_dir, name, no_symlink_components=True)
    except UnsafePath:
        raise ApiError(400, "bad_name", "That is not one of the log files.")
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        filename=_download_name(path, name),
        headers={"Cache-Control": "no-store"},
    )
