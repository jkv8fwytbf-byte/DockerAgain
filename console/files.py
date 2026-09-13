"""
Handouts & Submissions.

Handouts live in settings.shared_dir (/srv/shared); every student sees that
folder read-only as ~/shared. Submissions are the ~/submit folder of each
student in the roster.

    GET    /api/handouts?path=              list one folder level
    POST   /api/handouts                    multipart upload (streamed, capped, optional unzip)
    POST   /api/handouts/mkdir              {"path": "week-3"} -> 201
    DELETE /api/handouts?path=              file or folder (never the root) -> 204
    GET    /api/handouts/download?path=     one file
    GET    /api/submissions                 per-student summary
    GET    /api/submissions/download-all    one zip with <username>/... folders
    GET    /api/submissions/{u}/download    zip of one student's submit folder

Security notes
- Every client path goes through util.safe_join with no_symlink_components,
  so a link planted in /srv/shared cannot redirect a write, delete or read.
- Uploads are streamed straight to a temp file next to their destination
  with a running byte cap (Content-Length is checked first but never
  trusted on its own); zips are pre-scanned and extracted with zip-slip,
  symlink-entry and size/ratio guards.
- Submission zips walk ~/submit with directory file descriptors and
  O_NOFOLLOW and only include regular files owned by the student.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
import zipfile
import zlib
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import parse_options_header
from starlette.background import BackgroundTask

from .auth import require_admin
from .errors import ApiError
from .roster import Roster, RosterError, Student, validate_username
from .util import UnsafePath, clean_filename, human_bytes, iso_from_ts, read_branding, safe_join

log = logging.getLogger("console.files")
router = APIRouter()

CHUNK = 1024 * 1024
ADMINS_GID = 3001
FILE_MODE = 0o664
DIR_MODE = 0o2775
MAX_FILES_PER_UPLOAD = 200
MAX_FIELD_BYTES = 4096
MAX_NAME_BYTES = 255                            # what ext4/overlayfs accept for one path component
ZIP_RATIO_LIMIT = 200
ZIP_RATIO_MIN_SIZE = 1024 * 1024
SUBMISSION_MAX_FILES = 5000
SUBMISSION_MAX_BYTES = 500 * 1024 * 1024
SUBMISSION_MAX_FILE_BYTES = 100 * 1024 * 1024
ZIP_SLOTS = threading.BoundedSemaphore(2)      # zip builds running at once; more get a 409

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# --- shared helpers ----------------------------------------------------------------


def _rel(raw: str | None) -> str:
    """
    Client path -> clean relative path ('' is the root). Raises 400 bad_path.
    Only paths relative to the Handouts folder are meaningful: absolute paths
    (anything but a bare "/", which the page uses for the root), '..' and NUL
    are refused outright rather than silently re-rooted.
    """
    raw = (raw or "").replace("\\", "/").strip()
    if "\x00" in raw or len(raw) > 1000:
        raise ApiError(400, "bad_path", "That path is not valid.")
    if raw.startswith("/") and raw.strip("/"):
        raise ApiError(400, "bad_path", "That path is not valid.")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ApiError(400, "bad_path", "That path is not valid.")
    return "/".join(parts)


def _resolve(root: str, rel: str) -> str:
    """
    Absolute path under root; every existing component must be a real folder
    (no links). safe_join lstat()s each component and lets ENOTDIR (a path
    through a file) and ENAMETOOLONG escape, so they are mapped here.
    """
    try:
        return safe_join(root, rel, allow_root=True, no_symlink_components=True)
    except UnsafePath:
        raise ApiError(400, "bad_path", "That path is not inside the Handouts folder.")
    except NotADirectoryError:
        raise ApiError(400, "bad_path", "That path goes through a file, not a folder.")
    except OSError:
        raise ApiError(400, "bad_path", "That path is not valid.")


def _fit_name(name: str) -> str:
    """
    clean_filename caps characters; the filesystem caps bytes (255). Shorten a
    very long name to fit, keeping its extension and never splitting a
    multi-byte character.
    """
    if len(name.encode("utf-8")) <= MAX_NAME_BYTES:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and stem and len(ext.encode("utf-8")) <= 16:
        ext = "." + ext
    else:
        stem, ext = name, ""
    budget = MAX_NAME_BYTES - len(ext.encode("utf-8"))
    short = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip()
    return (short or "file") + ext


def _resolve_entry(root: str, rel: str) -> str:
    """
    Like _resolve but the last component may be a link: its parents are checked,
    the entry itself is only ever lstat'ed / unlinked, never followed.
    """
    parent_rel, _, name = rel.rpartition("/")
    return os.path.join(_resolve(root, parent_rel), name)


def _type_of(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _unlink_quiet(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _admin_uid(request: Request) -> int | None:
    """uid of the first admin account; new handouts are owned by it so the teacher can edit them in JupyterLab."""
    settings = request.app.state.settings
    accounts = request.app.state.accounts
    for name in sorted(settings.admin_users):
        try:
            return int(accounts.sys.getpwnam(name).pw_uid)
        except (KeyError, OSError, AttributeError, TypeError, ValueError):
            continue
    return None


def _own(path: str, uid: int | None, mode: int) -> None:
    """<admin>:admins with the given mode. chown is best-effort: it needs root (tests are not)."""
    if uid is not None:
        try:
            os.lchown(path, uid, ADMINS_GID)
        except OSError:
            pass
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _ensure_dir(path: str, uid: int | None, depth: int = 0) -> None:
    """mkdir -p with 2775 folders; refuses to treat a link or file as a folder."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    except NotADirectoryError:
        raise ApiError(409, "exists", f"A file is in the way of the folder '{os.path.basename(path)}'.")
    if st is not None:
        if stat.S_ISDIR(st.st_mode):
            return
        raise ApiError(409, "exists", f"'{os.path.basename(path)}' already exists and is not a folder.")
    if depth > 64:
        raise ApiError(400, "bad_path", "That folder is nested too deeply.")
    _ensure_dir(os.path.dirname(path), uid, depth + 1)
    try:
        os.mkdir(path, DIR_MODE)
    except FileExistsError:
        return
    _own(path, uid, DIR_MODE)


def _disposition(filename: str) -> str:
    fallback = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip() or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def _wants_html(request: Request) -> bool:
    """A plain link click (navigation) rather than fetch(): show a friendly page on errors."""
    return request.headers.get("sec-fetch-mode") == "navigate" or "text/html" in request.headers.get("accept", "")


def _friendly(request: Request, exc: ApiError):
    settings = request.app.state.settings
    titles = {400: "That link was not valid", 404: "Not found", 409: "Please wait a moment", 413: "That download is too big"}
    return request.app.state.render_error(
        request, exc.status, titles.get(exc.status, "Download failed"), exc.message,
        retry_url=settings.url("files"), retry_label="Back to Handouts & Submissions",
    )


# --- handouts: listing -----------------------------------------------------------------


def _list_dir(path: str) -> list[dict]:
    """
    One folder level. Dot-entries are hidden, exactly as JupyterLab hides them
    from the students: they are never created from this page (clean_filename
    renames a leading dot) and they are noise a teacher cannot act on
    (.ipynb_checkpoints from editing in JupyterLab, .DS_Store, our own
    in-progress .part uploads).
    """
    out = []
    with os.scandir(path) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            t = _type_of(st.st_mode)
            out.append({"name": e.name, "type": t, "size": st.st_size if t == "file" else 0, "mtime": iso_from_ts(st.st_mtime)})
    out.sort(key=lambda d: (d["type"] != "dir", d["name"].lower()))
    return out


@router.get("/api/handouts")
async def list_handouts(request: Request, path: str = "", user: str = Depends(require_admin)):
    settings = request.app.state.settings
    rel = _rel(path)
    target = _resolve(settings.shared_dir, rel)

    def do():
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            if not rel:
                return []                        # shared folder not created yet: nothing to show
            raise ApiError(404, "not_found", "That folder does not exist any more.")
        except OSError:                          # ENAMETOOLONG, ENOTDIR on the last component
            raise ApiError(400, "bad_path", "That path is not valid.")
        if not stat.S_ISDIR(st.st_mode):
            raise ApiError(400, "bad_path", "That is not a folder.")
        return _list_dir(target)

    entries = await asyncio.to_thread(do)
    return {"path": rel, "entries": entries, "max_upload_bytes": settings.max_upload_bytes}


# --- handouts: upload ------------------------------------------------------------------


class _TooLarge(Exception):
    pass


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


class _Receiver:
    """
    Callbacks for python_multipart's streaming parser. Text fields are kept in
    memory (small), file parts go straight to a temp file in the destination
    folder while a running total enforces the cap. Disk writes are queued in
    `pending` and flushed in a worker thread by the request handler.
    """

    def __init__(self, root: str, cap: int):
        self.root = root
        self.cap = cap
        self.fields: dict[str, str] = {}
        self.files: list[dict] = []
        self.pending: list[tuple[int, bytes]] = []
        self.total = 0
        self._part: dict | None = None
        self._hname = b""
        self._hval = b""

    # -- parser callbacks --
    def on_part_begin(self) -> None:
        self._part = {"headers": [], "name": "", "filename": None, "data": bytearray(), "fd": None, "tmp": None, "size": 0, "skip": False}

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._hname += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._hval += data[start:end]

    def on_header_end(self) -> None:
        self._part["headers"].append((self._hname.lower(), bytes(self._hval)))
        self._hname = b""
        self._hval = b""

    def on_headers_finished(self) -> None:
        part = self._part
        disp = next((v for k, v in part["headers"] if k == b"content-disposition"), b"")
        _, opts = parse_options_header(disp)
        part["name"] = _decode(opts.get(b"name"))
        if b"filename" not in opts:
            return
        filename = _decode(opts.get(b"filename"))
        if not filename:
            part["skip"] = True                  # an empty <input type=file>
            return
        if len(self.files) >= MAX_FILES_PER_UPLOAD:
            raise ApiError(400, "too_many_files", f"Upload at most {MAX_FILES_PER_UPLOAD} files at a time.")
        tmp = os.path.join(self._dest_dir(), f".upload.part-{secrets.token_hex(8)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        part.update(filename=filename, fd=fd, tmp=tmp)
        self.files.append(part)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        part = self._part
        if part["skip"]:
            return
        chunk = data[start:end]
        if part["fd"] is not None:
            self.total += len(chunk)
            part["size"] += len(chunk)
            if self.total > self.cap:
                raise _TooLarge()
            self.pending.append((part["fd"], chunk))
        else:
            if len(part["data"]) + len(chunk) > MAX_FIELD_BYTES:
                raise ApiError(400, "bad_upload", "A form field in the upload was too long.")
            part["data"] += chunk

    def on_part_end(self) -> None:
        part = self._part
        if part["fd"] is None and not part["skip"] and part["name"]:
            self.fields[part["name"]] = part["data"].decode("utf-8", "replace")

    # -- helpers --
    def _dest_dir(self) -> str:
        """The destination folder; the client sends `path` before the files (ours does), else the root."""
        target = _resolve(self.root, _rel(self.fields.get("path", "")))
        if not os.path.isdir(target):
            raise ApiError(404, "not_found", "The destination folder does not exist any more.")
        return target

    def flush(self) -> None:
        """Runs in a worker thread: write every queued chunk."""
        for fd, chunk in self.pending:
            view = memoryview(chunk)
            while view:
                n = os.write(fd, view)
                view = view[n:]
        self.pending.clear()

    def close(self) -> None:
        for f in self.files:
            if f["fd"] is not None:
                try:
                    os.close(f["fd"])
                except OSError:
                    pass
                f["fd"] = None

    def discard(self) -> None:
        self.close()
        self.pending.clear()
        for f in self.files:
            _unlink_quiet(f["tmp"])
            f["tmp"] = None


def _place(tmp: str, final: str, overwrite: bool, uid: int | None) -> None:
    """Move a finished temp file onto its final name (409 unless overwrite)."""
    name = os.path.basename(final)
    try:
        st = os.lstat(final)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISDIR(st.st_mode):
            raise ApiError(409, "exists", f"A folder called '{name}' is already there. Rename the file or delete the folder first.", {"name": name, "kind": "dir"})
        if not overwrite:
            raise ApiError(409, "exists", f"'{name}' is already in Handouts.", {"name": name, "kind": "file"})
    _own(tmp, uid, FILE_MODE)
    os.replace(tmp, final)


def _bad_component(p: str) -> bool:
    return bool(_CONTROL_RE.search(p)) or len(p.encode("utf-8", "replace")) > 255


def _extract_zip(zip_path: str, dest: str, zip_name: str, *, overwrite: bool, settings, uid: int | None) -> tuple[int, list[dict], str]:
    """
    Unpack an uploaded zip under dest. Returns (files written, skipped entries, folder it went into).
    Guards: entry count, total size (declared and actual), per-entry ratio, symlink
    entries, absolute / drive-letter / '..' names, and safe_join for every target.
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError, ValueError):
        raise ApiError(400, "bad_zip", f"'{zip_name}' is not a zip file this console can open.")
    with zf:
        infos = zf.infolist()
        if len(infos) > settings.max_zip_entries:
            raise ApiError(413, "too_large", f"'{zip_name}' has more than {settings.max_zip_entries} entries. Unpack it on your computer and upload only what the class needs.")
        declared = 0
        for info in infos:
            if info.file_size > ZIP_RATIO_MIN_SIZE and info.file_size / max(info.compress_size, 1) > ZIP_RATIO_LIMIT:
                raise ApiError(400, "bad_zip", f"'{zip_name}' was not unpacked: one of its entries expands more than {ZIP_RATIO_LIMIT}x, which looks like a zip bomb.")
            declared += info.file_size
        if declared > settings.max_unzipped_bytes:
            raise ApiError(413, "too_large", f"'{zip_name}' unpacks to {human_bytes(declared)}, more than the limit of {human_bytes(settings.max_unzipped_bytes)}.")

        plan: list[tuple[zipfile.ZipInfo, list[str], bool]] = []
        skipped: list[dict] = []
        tops: set[str] = set()
        for info in infos:
            raw = info.filename.replace("\\", "/")
            shown = raw[:200]
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                skipped.append({"name": shown, "reason": "link"})
                continue
            parts = [p for p in raw.split("/") if p not in ("", ".")]
            if not parts:
                continue
            if raw.startswith("/") or _DRIVE_RE.match(raw) or ".." in parts or any(_bad_component(p) for p in parts):
                skipped.append({"name": shown, "reason": "unsafe path"})
                continue
            if parts[0] == "__MACOSX" or parts[-1] == ".DS_Store" or parts[-1].startswith("._"):
                continue                         # Finder metadata, never useful to students
            tops.add(parts[0])
            plan.append((info, parts, info.is_dir() or raw.endswith("/")))

        # Like Finder: an archive with a single top-level item (one folder or one
        # file) unpacks in place, anything else goes into a folder named after the
        # zip so loose files do not spill all over Handouts.
        base, into = dest, ""
        single = len(tops) == 1
        if plan and not single:
            try:
                into = clean_filename(zip_name[:-4] if zip_name.lower().endswith(".zip") else zip_name)
            except UnsafePath:
                into = "unpacked"
            base = os.path.join(dest, into)
            _ensure_dir(base, uid)

        written = 0
        count = 0
        for info, parts, is_dir in plan:
            relp = "/".join(parts)
            try:
                target = safe_join(base, relp, no_symlink_components=True)
                _ensure_dir(target if is_dir else os.path.dirname(target), uid)
            except UnsafePath:
                skipped.append({"name": relp[:200], "reason": "unsafe path"})
                continue
            except (NotADirectoryError, ApiError) as e:
                if isinstance(e, ApiError) and e.status != 409:
                    raise
                skipped.append({"name": relp[:200], "reason": "a file has that folder's name"})
                continue
            except OSError:
                skipped.append({"name": relp[:200], "reason": "cannot be written"})
                continue
            if is_dir:
                continue
            try:
                st = os.lstat(target)
            except FileNotFoundError:
                st = None
            if st is not None:
                if stat.S_ISDIR(st.st_mode):
                    skipped.append({"name": relp[:200], "reason": "a folder has that name"})
                    continue
                if not overwrite:
                    skipped.append({"name": relp[:200], "reason": "exists"})
                    continue
            tmp = os.path.join(os.path.dirname(target), f".{os.path.basename(target)}.part-{secrets.token_hex(8)}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as out, zf.open(info) as src:
                    while True:
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > settings.max_unzipped_bytes:
                            raise ApiError(413, "too_large", f"'{zip_name}' unpacks to more than {human_bytes(settings.max_unzipped_bytes)}; stopped.")
                        out.write(chunk)
            except ApiError:
                _unlink_quiet(tmp)
                raise
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error, ValueError) as e:
                _unlink_quiet(tmp)
                reason = "it is password-protected" if "password" in str(e).lower() else "it is damaged or uses an unsupported format"
                raise ApiError(400, "bad_zip", f"'{zip_name}' could not be unpacked: {reason}.")
            except BaseException:
                _unlink_quiet(tmp)
                raise
            _own(tmp, uid, FILE_MODE)
            os.replace(tmp, target)
            count += 1
    return count, skipped, into


def _finish_upload(rx: _Receiver, settings, uid: int | None) -> dict:
    """Runs in a worker thread once the whole request body has been received."""
    rel = _rel(rx.fields.get("path", ""))
    dest = _resolve(settings.shared_dir, rel)
    if not os.path.isdir(dest):
        raise ApiError(404, "not_found", "The destination folder does not exist any more.")
    unzip = rx.fields.get("unzip") == "1"
    overwrite = rx.fields.get("overwrite") == "1"
    keep_zip = rx.fields.get("keep_zip") == "1"
    if not rx.files:
        raise ApiError(400, "no_files", "Choose at least one file to upload.")
    saved: list[str] = []
    skipped: list[dict] = []
    unpacked: list[str] = []
    extracted = 0
    try:
        for f in rx.files:
            try:
                name = _fit_name(clean_filename(f["filename"]))
            except UnsafePath:
                skipped.append({"name": str(f["filename"])[:200], "reason": "invalid name"})
                continue
            rel_name = f"{rel}/{name}" if rel else name
            if unzip and name.lower().endswith(".zip"):
                n, sk, into = _extract_zip(f["tmp"], dest, name, overwrite=overwrite, settings=settings, uid=uid)
                extracted += n
                skipped.extend(sk)
                unpacked.append(f"{rel}/{into}" if rel and into else (into or rel))
                if keep_zip:
                    _place(f["tmp"], os.path.join(dest, name), overwrite, uid)
                    saved.append(rel_name)
                else:
                    _unlink_quiet(f["tmp"])
            else:
                _place(f["tmp"], os.path.join(dest, name), overwrite, uid)
                saved.append(rel_name)
            f["tmp"] = None
    except ApiError as e:
        e.detail = dict(e.detail, saved=saved, extracted=extracted)
        raise
    finally:
        for f in rx.files:
            _unlink_quiet(f["tmp"])
    log.info("handouts: %s saved, %s unpacked, %s skipped in '%s'", len(saved), extracted, len(skipped), rel or "/")
    return {"path": rel, "saved": saved, "extracted": extracted, "skipped": skipped, "unpacked_into": unpacked}


@router.post("/api/handouts")
async def upload_handouts(request: Request, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    cap = int(settings.max_upload_bytes)
    length = request.headers.get("content-length", "").strip()
    if length.isdigit() and int(length) > cap:
        raise ApiError(413, "too_large", f"That upload is bigger than the limit of {human_bytes(cap)}.", {"max_upload_bytes": cap})
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise ApiError(400, "bad_upload", "Expected a file upload (multipart form).")
    if not os.path.isdir(settings.shared_dir):
        raise ApiError(500, "no_shared_dir", "The Handouts folder is missing on the server. Restart the container to recreate it.")

    rx = _Receiver(settings.shared_dir, cap)
    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": rx.on_part_begin, "on_part_data": rx.on_part_data, "on_part_end": rx.on_part_end,
            "on_header_field": rx.on_header_field, "on_header_value": rx.on_header_value,
            "on_header_end": rx.on_header_end, "on_headers_finished": rx.on_headers_finished,
        },
        max_size=cap + 8 * 1024 * 1024,        # body = files + fields + boundaries; the file bytes are capped exactly
    )
    try:
        async for chunk in request.stream():
            parser.write(chunk)
            if rx.pending:
                await asyncio.to_thread(rx.flush)
        parser.finalize()
        if rx.pending:
            await asyncio.to_thread(rx.flush)
        rx.close()
        return await asyncio.to_thread(_finish_upload, rx, settings, _admin_uid(request))
    except _TooLarge:
        rx.discard()
        raise ApiError(413, "too_large", f"That upload is bigger than the limit of {human_bytes(cap)}.", {"max_upload_bytes": cap})
    except FormParserError:
        rx.discard()
        raise ApiError(400, "bad_upload", "The upload was cut short or malformed. Please try again.")
    except BaseException:
        rx.discard()
        raise


# --- handouts: folders, delete, download ---------------------------------------------------


class MkdirBody(BaseModel):
    path: str = Field(min_length=1, max_length=500)


@router.post("/api/handouts/mkdir", status_code=201)
async def make_folder(request: Request, body: MkdirBody, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    if "\\" in body.path:
        raise ApiError(400, "bad_name", "Folder names cannot contain a backslash.")
    rel = _rel(body.path)
    if not rel:
        raise ApiError(400, "bad_path", "Give the new folder a name.")
    name = rel.rsplit("/", 1)[-1]
    try:
        clean = clean_filename(name)
    except UnsafePath:
        clean = ""
    if clean != name or len(name) > 120 or len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ApiError(400, "bad_name", "Folder names cannot start with a dot, contain / or \\, or be longer than 120 characters.")
    target = _resolve(settings.shared_dir, rel)
    uid = _admin_uid(request)

    def do():
        if not os.path.isdir(os.path.dirname(target)):
            raise ApiError(404, "not_found", "The folder above it does not exist any more.")
        try:
            os.mkdir(target, DIR_MODE)
        except FileExistsError:
            raise ApiError(409, "exists", f"'{name}' already exists.", {"name": name})
        except (FileNotFoundError, NotADirectoryError):
            raise ApiError(404, "not_found", "The folder above it does not exist any more.")
        _own(target, uid, DIR_MODE)

    await asyncio.to_thread(do)
    return {"path": rel}


@router.delete("/api/handouts", status_code=204)
async def delete_handout(request: Request, path: str = "", user: str = Depends(require_admin)):
    settings = request.app.state.settings
    rel = _rel(path)
    if not rel:
        raise ApiError(400, "refuse_root", "The Handouts folder itself cannot be deleted, only the files inside it.")
    target = _resolve_entry(settings.shared_dir, rel)

    def do():
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            raise ApiError(404, "not_found", "That file is already gone.")
        except OSError:                          # ENAMETOOLONG, ENOTDIR on the last component
            raise ApiError(400, "bad_path", "That path is not valid.")
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(target)            # fd-based on Linux/macOS: links inside are removed, never followed
        else:
            os.unlink(target)                # a link is removed, its target untouched

    await asyncio.to_thread(do)
    log.info("handouts: deleted '%s'", rel)
    return Response(status_code=204)


def _stream_fd(fd: int, size: int, filename: str) -> StreamingResponse:
    fh = os.fdopen(fd, "rb", buffering=0)

    def body():
        remaining = size
        try:
            while remaining > 0:
                chunk = fh.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            fh.close()

    headers = {"Content-Length": str(size), "Content-Disposition": _disposition(filename), "Cache-Control": "no-store"}
    return StreamingResponse(body(), media_type="application/octet-stream", headers=headers)


@router.get("/api/handouts/download")
async def download_handout(request: Request, path: str = "", user: str = Depends(require_admin)):
    settings = request.app.state.settings
    try:
        rel = _rel(path)
        if not rel:
            raise ApiError(400, "bad_path", "Choose a file to download.")
        # Parents must be real folders; the file itself is opened O_NOFOLLOW below,
        # so a link gets the accurate "only files" answer instead of "bad path".
        target = _resolve_entry(settings.shared_dir, rel)

        def open_it():
            try:
                fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                raise ApiError(404, "not_found", "That file is not in Handouts any more.")
            except OSError as e:
                if e.errno == errno.ELOOP:   # O_NOFOLLOW on a link
                    raise ApiError(400, "not_a_file", "Only files can be downloaded here; that entry is a link.")
                raise ApiError(404, "not_found", "That file is not in Handouts any more.")
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise ApiError(400, "not_a_file", "Only files can be downloaded here. Open folders in JupyterLab instead.")
            except BaseException:
                os.close(fd)
                raise
            return fd, st.st_size

        fd, size = await asyncio.to_thread(open_it)
        return _stream_fd(fd, size, os.path.basename(target))
    except ApiError as e:
        if _wants_html(request):
            return _friendly(request, e)
        raise


# --- submissions ----------------------------------------------------------------------------


class _TooMuch(Exception):
    def __init__(self, username: str | None):
        super().__init__(username)
        self.username = username


def _owner_ok(st_uid: int, uid: int) -> bool:
    return st_uid == uid


def _open_dir(name: str, dir_fd: int | None = None) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def _walk_submit(submit_dir: str, uid: int):
    """
    Yield (relpath, fd, size, mtime) for every regular file owned by `uid`
    under submit_dir. Directories are opened with O_NOFOLLOW relative to
    their parent's descriptor and files with O_NOFOLLOW, then fstat'ed, so a
    link swapped in between two calls can never be followed. Symlinks,
    special files and files owned by someone else are skipped silently.
    The caller owns (and must close) each yielded fd.
    """
    try:
        cur = _open_dir(submit_dir)
    except OSError:
        return
    stack: list[tuple[int, str]] = []
    prefix = ""
    try:
        while True:
            try:
                with os.scandir(cur) as it:      # scandir dups the fd; `cur` stays ours to close
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                entries = []
            for e in entries:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if len(stack) >= 64:
                        continue
                    try:
                        stack.append((_open_dir(e.name, dir_fd=cur), prefix + e.name + "/"))
                    except OSError:
                        continue
                elif stat.S_ISREG(st.st_mode):
                    try:
                        fd = os.open(e.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=cur)
                    except OSError:
                        continue
                    try:
                        fst = os.fstat(fd)
                    except OSError:
                        os.close(fd)
                        continue
                    if not stat.S_ISREG(fst.st_mode) or not _owner_ok(fst.st_uid, uid):
                        os.close(fd)
                        continue
                    yield prefix + e.name, fd, fst.st_size, fst.st_mtime
            os.close(cur)
            cur = None
            if not stack:
                break
            cur, prefix = stack.pop()
    finally:
        if cur is not None:
            os.close(cur)
        for dfd, _ in stack:
            os.close(dfd)


def _summarize(submit_dir: str, uid: int) -> dict:
    count = total = large = 0
    latest = None
    capped = False
    gen = _walk_submit(submit_dir, uid)
    try:
        for _, fd, size, mtime in gen:
            os.close(fd)
            if size > SUBMISSION_MAX_FILE_BYTES:
                large += 1
                continue
            count += 1
            total += size
            latest = mtime if latest is None or mtime > latest else latest
            if count >= SUBMISSION_MAX_FILES or total >= SUBMISSION_MAX_BYTES:
                capped = True
                break
    finally:
        gen.close()
    return {"file_count": count, "total_bytes": total, "latest_mtime": iso_from_ts(latest) if latest else None,
            "latest_ts": latest or 0, "capped": capped, "large_files": large}


def _student_uid(accounts, username: str) -> int | None:
    try:
        return int(accounts.sys.getpwnam(username).pw_uid)
    except (KeyError, OSError, AttributeError, TypeError, ValueError):
        return None


def _roster_students(settings) -> list[Student]:
    try:
        return Roster(settings.roster_path).load()
    except RosterError as e:
        raise ApiError(500, "roster_unreadable", f"The class roster could not be read: {e}")


def _submit_dir(settings, username: str) -> str:
    return os.path.join(settings.home_root, username, "submit")


def _submit_exists(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


@router.get("/api/submissions")
async def list_submissions(request: Request, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    accounts = request.app.state.accounts

    def do():
        out = []
        for s in _roster_students(settings):
            submit = _submit_dir(settings, s.username)
            uid = _student_uid(accounts, s.username)
            entry = {
                "username": s.username, "display_name": s.display_name, "is_admin": s.username in settings.admin_users,
                "account": uid is not None, "exists": _submit_exists(submit),
                "file_count": 0, "total_bytes": 0, "latest_mtime": None, "latest_ts": 0, "capped": False, "large_files": 0,
            }
            if entry["exists"] and uid is not None:
                entry.update(_summarize(submit, uid))
            out.append(entry)
        out.sort(key=lambda e: (-e["latest_ts"], e["username"]))
        for e in out:
            del e["latest_ts"]
        return out

    return await asyncio.to_thread(do)


def _zip_dt(mtime: float) -> tuple:
    t = time.localtime(mtime)
    if t.tm_year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, min(t.tm_sec, 59))


def _add_student(zf: zipfile.ZipFile, username: str, submit_dir: str, uid: int | None, prefix: str, budget: dict) -> int:
    n = 0
    if uid is None or not _submit_exists(submit_dir):
        return 0
    gen = _walk_submit(submit_dir, uid)
    try:
        for rel, fd, size, mtime in gen:
            if size > SUBMISSION_MAX_FILE_BYTES:
                os.close(fd)
                budget["large"] += 1
                continue
            budget["files"] += 1
            budget["bytes"] += size
            if budget["files"] > SUBMISSION_MAX_FILES or budget["bytes"] > SUBMISSION_MAX_BYTES:
                os.close(fd)
                raise _TooMuch(username)
            info = zipfile.ZipInfo(prefix + rel, date_time=_zip_dt(mtime))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.file_size = size
            with os.fdopen(fd, "rb") as src, zf.open(info, "w") as dst:
                remaining = size
                while remaining > 0:
                    chunk = src.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    dst.write(chunk)
                    remaining -= len(chunk)
            n += 1
    finally:
        gen.close()
    return n


def _build_zip(path: str, students: list[tuple[str, str, str, int | None]], *, per_student_folders: bool, overall_cap: int) -> dict:
    """students: (username, display_name, submit_dir, uid). Raises _TooMuch when a cap is hit."""
    overall = 0
    large = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        if not students:
            zf.writestr("README.txt", "The roster is empty, so there are no submissions to collect yet.\n")
        for username, display, submit_dir, uid in students:
            prefix = f"{username}/" if per_student_folders else ""
            budget = {"files": 0, "bytes": 0, "large": 0}
            n = _add_student(zf, username, submit_dir, uid, prefix, budget)
            overall += budget["bytes"]
            large += budget["large"]
            if overall > overall_cap:
                raise _TooMuch(None)
            if n == 0:
                who = f"{display} ({username})" if display else username
                zf.writestr(prefix + "README.txt", f"{who} has not put any files in their submit folder yet.\n")
    return {"bytes": overall, "large": large}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]


async def _zip_response(request: Request, students: list, filename: str, *, per_student_folders: bool):
    settings = request.app.state.settings
    if not ZIP_SLOTS.acquire(blocking=False):
        raise ApiError(409, "busy", "Another download is still being prepared. Try again in a moment.")
    try:
        os.makedirs(settings.tmp_dir, mode=0o700, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="submissions-", suffix=".zip", dir=settings.tmp_dir)
        os.close(fd)
    except OSError as e:
        ZIP_SLOTS.release()
        raise ApiError(500, "tmp_unavailable", f"Could not create a temporary file on the server: {e}")
    try:
        result = await asyncio.to_thread(_build_zip, path, students, per_student_folders=per_student_folders,
                                         overall_cap=int(settings.max_submission_zip_bytes))
    except _TooMuch as e:
        _unlink_quiet(path)
        who = f"{e.username}'s submit folder" if e.username else "All the submissions together"
        raise ApiError(
            413, "too_large",
            f"{who} is over the download limit ({SUBMISSION_MAX_FILES:,} files or {human_bytes(SUBMISSION_MAX_BYTES)} per student). "
            "Make a backup instead: it includes every file.",
            {"hint": "backups", "backups_url": settings.url("backups")},
        )
    except BaseException:
        _unlink_quiet(path)
        raise
    finally:
        ZIP_SLOTS.release()
    if result["large"]:
        log.warning("submissions zip %s: %s file(s) over %s were left out", filename, result["large"], human_bytes(SUBMISSION_MAX_FILE_BYTES))
    return FileResponse(path, media_type="application/zip", filename=filename,
                        background=BackgroundTask(_unlink_quiet, path), headers={"Cache-Control": "no-store"})


@router.get("/api/submissions/download-all")
async def download_all_submissions(request: Request, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    accounts = request.app.state.accounts
    try:
        students = await asyncio.to_thread(_roster_students, settings)
        rows = [(s.username, s.display_name, _submit_dir(settings, s.username), _student_uid(accounts, s.username)) for s in students]
        branding = read_branding(settings.branding_json)
        name = f"submissions-{_slug(branding.get('class_name') or branding.get('school_name')) or 'class'}-{time.strftime('%Y%m%d')}.zip"
        return await _zip_response(request, rows, name, per_student_folders=True)
    except ApiError as e:
        if _wants_html(request):
            return _friendly(request, e)
        raise


@router.get("/api/submissions/{username}/download")
async def download_submission(request: Request, username: str, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    accounts = request.app.state.accounts
    try:
        try:
            username = validate_username(username)
        except ValueError as e:
            raise ApiError(400, "bad_username", str(e))
        students = await asyncio.to_thread(_roster_students, settings)
        student = Roster.find(students, username)
        if student is None:
            raise ApiError(404, "not_found", f"'{username}' is not in the roster.")
        rows = [(username, student.display_name, _submit_dir(settings, username), _student_uid(accounts, username))]
        return await _zip_response(request, rows, f"submit-{username}-{time.strftime('%Y%m%d')}.zip", per_student_folders=False)
    except ApiError as e:
        if _wants_html(request):
            return _friendly(request, e)
        raise
