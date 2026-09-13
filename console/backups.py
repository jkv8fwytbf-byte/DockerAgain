"""
Backups and archived students.

A backup is one tar.gz holding every home folder, the shared handouts and
the hub state (roster + branding). It is written by a single background
worker, so only one job runs at a time:

    GET    /api/backups                  backups, archived students, disk figures, latest job
    POST   /api/backups                  start a job -> 202; 409 while one runs; 507 disk too full
    GET    /api/backups/jobs/{id}        progress (the page polls this every 2 s)
    GET    /api/backups/{name}/download  DELETE /api/backups/{name}
    GET    /api/archive/{name}/download  DELETE /api/archive/{name}   (homes of removed students)

Archive layout:  home/<user>/...   shared/...   hubstate/roster.json
                 hubstate/branding/...   manifest.json

Safety: the walk is descriptor-based and iterative. Every folder is opened
relative to its parent's descriptor with O_NOFOLLOW, every file likewise and
then fstat'ed, so a link a student swaps in for a folder while the backup is
running is never followed (not even for the children still to be copied).
Symlinks are stored as links, special files are skipped, files are read
through a padding reader so one that shrinks mid-copy cannot corrupt the
archive. Files over MAX_FILE_BYTES or that are mostly holes are left out by
both the size estimate and the writer, so a sparse file cannot block backups;
folders nested deeper than MAX_DEPTH are left out too. Everything left out is
counted, listed in manifest.json and shown to the teacher.
Every name that reaches the disk matches a strict pattern. Backups contain
the roster with plain-text passwords, so they are 0600 and served no-store.
Restoring is manual (tar -xzf into the volumes); the manifest explains how.
"""
from __future__ import annotations

import asyncio
import grp
import io
import json
import logging
import os
import pwd
import re
import secrets
import shutil
import stat
import tarfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .auth import require_admin
from .errors import ApiError
from .roster import Roster
from .util import human_bytes, iso_from_ts, iso_now

log = logging.getLogger("console.backups")

BACKUP_NAME_RE = re.compile(r"classroom-backup-(\d{8})-(\d{6})\.tar\.gz")     # used with fullmatch
ARCHIVE_NAME_RE = re.compile(r"([a-z_][a-z0-9_-]{0,31})-(\d{8}-\d{6})\.tar\.gz")
PART_SUFFIX = ".part"
STAMP_FMT = "%Y%m%d-%H%M%S"
SPACE_MARGIN = 256 * 1024 * 1024      # free space we insist on keeping after a backup
ESTIMATE_TTL = 600                    # seconds the source-size estimate is trusted
MAX_JOBS = 20                         # finished jobs remembered for GET /jobs/{id}
MAX_DEPTH = 128                       # folders nested deeper than this are left out (one open fd per level)
MAX_FILE_BYTES = 4 * 1024 ** 3        # a single file bigger than this is left out (Settings.max_backup_file_bytes overrides)
SPARSE_MIN_BYTES = 64 * 1024 ** 2     # above this size a file with < 1/8 of its bytes allocated counts as a hole
SKIPPED_LISTED = 25                   # skipped paths remembered per job (job dict + manifest)
SKIPPED_LOGGED = 50                   # skipped paths written to the log per job
MANIFEST_FORMAT = 1
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

router = APIRouter()


# --- errors raised by the manager (mapped to HTTP in the routes) ------------------


class BackupBusy(Exception):
    def __init__(self, job: "BackupJob"):
        super().__init__("a backup is already running")
        self.job = job


class NotEnoughSpace(Exception):
    def __init__(self, free: int, needed: int):
        super().__init__("not enough free space")
        self.free = free
        self.needed = needed


# --- helpers ---------------------------------------------------------------------


def _stamp_to_iso(stamp: str) -> str | None:
    """'20260913-143210' -> '2026-09-13T14:32:10Z' (stamps are UTC), None if not a date."""
    try:
        dt = datetime.strptime(stamp, STAMP_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _is_regular(path: str) -> bool:
    """True for an existing regular file that is not a symlink."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


class _ExactReader:
    """
    Serves exactly `size` bytes from fh. tarfile writes the header before the
    data, so a file that shrinks while we copy it would otherwise leave a
    short member and corrupt everything after it; we pad with NULs instead.
    """

    def __init__(self, fh, size: int):
        self.fh = fh
        self.remaining = size

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0 or n > self.remaining:
            n = self.remaining
        if n <= 0:
            return b""
        chunks = []
        got = 0
        while got < n:
            buf = self.fh.read(n - got)
            if not buf:
                break
            chunks.append(buf)
            got += len(buf)
        if got < n:
            chunks.append(b"\0" * (n - got))
        self.remaining -= n
        return b"".join(chunks)


def _open_dir(name: str, dir_fd: int | None = None) -> int:
    """Open a directory relative to dir_fd; a symlink in its place is refused, never followed."""
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _O_CLOEXEC, dir_fd=dir_fd)


def _open_root(path: str) -> int:
    """The roots (home_root, shared_dir, branding_dir) are trusted config paths, opened once by path."""
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC)


def _skip_reason(st: os.stat_result, max_file_bytes: int = MAX_FILE_BYTES) -> str | None:
    """Why a regular file is left out of the backup, or None. Used by the estimate and the writer alike."""
    blocks = getattr(st, "st_blocks", None)
    if blocks is not None and st.st_size > SPARSE_MIN_BYTES and blocks * 512 < st.st_size // 8:
        return f"mostly empty space (a {human_bytes(st.st_size)} sparse file with {human_bytes(blocks * 512)} on disk)"
    if st.st_size > max_file_bytes:
        return f"bigger than {human_bytes(max_file_bytes)} ({human_bytes(st.st_size)})"
    return None


class _Frame:
    __slots__ = ("fd", "arc", "depth", "entries", "i")

    def __init__(self, fd: int, arc: str, depth: int):
        self.fd, self.arc, self.depth, self.entries, self.i = fd, arc, depth, None, 0


def _walk_tree(root_fd: int, root_arc: str, *, max_depth: int | None = None, max_file_bytes: int | None = None):
    """
    Iterative, descriptor-based walk of the directory open at root_fd, which the
    walk owns and closes. Yields, in sorted order per folder,
        ("dir",  st, arc, depth)              a folder (the root first), already opened
        ("file", dir_fd, name, st, arc)       a regular file; open it with dir_fd=dir_fd while suspended here
        ("link", st, arc, target)             a symlink and what it points to (never followed)
        ("skip", arc, reason)                 anything left out and why
    Every folder is opened relative to its parent's descriptor with O_NOFOLLOW
    and checked to be the very inode the listing saw, so no absolute path below
    the root ever reaches a syscall and a link swapped in mid-walk is never
    followed. Only the ancestors' descriptors are open at any time.
    """
    max_depth = MAX_DEPTH if max_depth is None else max_depth
    max_file_bytes = MAX_FILE_BYTES if max_file_bytes is None else max_file_bytes
    frames: list[_Frame] = [_Frame(root_fd, root_arc, 0)]     # owned from here on: closed even if the consumer stops early
    try:
        yield ("dir", os.fstat(root_fd), root_arc, 0)
        while frames:
            f = frames[-1]
            if f.entries is None:
                try:
                    with os.scandir(f.fd) as it:            # scandir dups the fd; f.fd stays ours to close
                        f.entries = sorted(it, key=lambda e: e.name)
                except OSError as e:
                    f.entries = []
                    yield ("skip", f.arc, f"folder could not be listed: {e.strerror or e}")
            if f.i >= len(f.entries):
                frames.pop()
                os.close(f.fd)
                continue
            e = f.entries[f.i]
            f.i += 1
            arc = f.arc + "/" + e.name
            try:
                st = e.stat(follow_symlinks=False)
            except OSError as err:
                yield ("skip", arc, f"could not be read: {err.strerror or err}")
                continue
            if stat.S_ISDIR(st.st_mode):
                if f.depth >= max_depth:
                    yield ("skip", arc, f"folders nested deeper than {max_depth} levels")
                    continue
                try:
                    cfd = _open_dir(e.name, dir_fd=f.fd)
                except OSError as err:
                    yield ("skip", arc, f"folder could not be opened: {err.strerror or err}")
                    continue
                try:
                    cst = os.fstat(cfd)
                except OSError as err:
                    os.close(cfd)
                    yield ("skip", arc, f"folder could not be read: {err.strerror or err}")
                    continue
                if (cst.st_dev, cst.st_ino) != (st.st_dev, st.st_ino):
                    os.close(cfd)
                    yield ("skip", arc, "folder changed while the backup was running")
                    continue
                yield ("dir", st, arc, f.depth + 1)
                frames.append(_Frame(cfd, arc, f.depth + 1))
            elif stat.S_ISREG(st.st_mode):
                reason = _skip_reason(st, max_file_bytes)
                if reason:
                    yield ("skip", arc, reason)
                else:
                    yield ("file", f.fd, e.name, st, arc)
            elif stat.S_ISLNK(st.st_mode):
                try:
                    target = os.readlink(e.name, dir_fd=f.fd)
                except OSError as err:
                    yield ("skip", arc, f"link could not be read: {err.strerror or err}")
                    continue
                yield ("link", st, arc, target)
            else:                                            # fifo, socket, device
                yield ("skip", arc, "not a regular file (pipe, socket or device)")
    finally:
        for f in frames:
            try:
                os.close(f.fd)
            except OSError:
                pass


def tree_size(root: str, max_file_bytes: int = MAX_FILE_BYTES) -> int:
    """
    Bytes the backup will copy from below root: regular files that pass
    _skip_reason plus symlink targets, walked exactly like the writer walks
    (never follows symlinks, same depth cap, never raises).
    """
    try:
        fd = _open_root(root)
    except OSError:
        return 0
    total = 0
    with closing(_walk_tree(fd, "", max_file_bytes=max_file_bytes)) as events:
        for ev in events:
            if ev[0] == "file":
                total += ev[3].st_size
            elif ev[0] == "link":
                total += ev[1].st_size
    return total


# --- job -------------------------------------------------------------------------


@dataclass
class BackupJob:
    id: str
    name: str
    part_path: str
    final_path: str
    state: str = "running"                  # running | done | failed
    started: str = field(default_factory=iso_now)
    finished: str | None = None
    error: str | None = None
    phase: str = "starting"
    homes_done: int = 0
    homes_total: int | None = None
    files: int = 0
    skipped: int = 0
    users: int = 0
    size: int | None = None                 # final size once done
    skipped_items: list = field(default_factory=list)   # first SKIPPED_LISTED {"path", "reason"}
    warnings: list = field(default_factory=list)        # teacher-readable notes about what is missing

    def skip(self, arc: str, reason: str) -> None:
        self.skipped += 1
        if len(self.skipped_items) < SKIPPED_LISTED:
            self.skipped_items.append({"path": arc, "reason": reason})
        if self.skipped <= SKIPPED_LOGGED:
            log.warning("backup %s: left out %s: %s", self.name, arc, reason)
        elif self.skipped == SKIPPED_LOGGED + 1:
            log.warning("backup %s: more items left out; only the first %d are logged", self.name, SKIPPED_LOGGED)

    def bytes_written(self) -> int:
        if self.size is not None:
            return self.size
        try:
            return os.stat(self.part_path).st_size
        except OSError:
            return 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "name": self.name,
            "started": self.started,
            "finished": self.finished,
            "bytes_written": self.bytes_written(),
            "error": self.error,
            "phase": self.phase,
            "homes_done": self.homes_done,
            "homes_total": self.homes_total,
            "files": self.files,
            "skipped": self.skipped,
            "skipped_items": list(self.skipped_items),
            "warnings": list(self.warnings),
        }


# --- archive writer --------------------------------------------------------------


class _Archiver:
    """Writes one job's archive. Holds the per-job caches (hard-link inodes, owner names)."""

    def __init__(self, settings, tar: tarfile.TarFile, job: BackupJob, max_file_bytes: int):
        self.s = settings
        self.tar = tar
        self.job = job
        self.max_file_bytes = max_file_bytes
        self.inodes: dict[tuple[int, int], str] = {}
        self._unames: dict[int, str] = {}
        self._gnames: dict[int, str] = {}

    # -- tar headers built from stat results (never from a path) --
    def _info(self, st: os.stat_result, arc: str, kind: bytes) -> tarfile.TarInfo:
        ti = tarfile.TarInfo(arc)
        ti.type = kind
        ti.mode = stat.S_IMODE(st.st_mode)
        ti.uid, ti.gid = st.st_uid, st.st_gid
        ti.mtime = int(st.st_mtime)
        ti.size = 0
        ti.uname, ti.gname = self._uname(st.st_uid), self._gname(st.st_gid)
        return ti

    def _uname(self, uid: int) -> str:
        if uid not in self._unames:
            try:
                self._unames[uid] = pwd.getpwuid(uid).pw_name
            except (KeyError, OverflowError):
                self._unames[uid] = ""
        return self._unames[uid]

    def _gname(self, gid: int) -> str:
        if gid not in self._gnames:
            try:
                self._gnames[gid] = grp.getgrgid(gid).gr_name
            except (KeyError, OverflowError):
                self._gnames[gid] = ""
        return self._gnames[gid]

    # -- trees --
    def add_homes(self) -> None:
        s, job = self.s, self.job
        try:
            fd = _open_root(s.home_root)
        except OSError as e:
            raise RuntimeError(f"the home folder {s.home_root} is missing or unreadable ({e.strerror or e})") from e
        homes = 0
        try:
            with os.scandir(s.home_root) as it:
                for entry in it:
                    try:
                        if stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode):
                            homes += 1
                    except OSError:
                        continue
        except OSError:
            pass
        job.homes_total = homes
        job.phase = "home folders"
        started = 0

        def on_dir(depth: int) -> None:
            nonlocal started
            if depth == 1:
                job.homes_done = started
                started += 1

        self._consume(fd, "home", on_dir)
        job.homes_done = homes

    def add_tree(self, root_path: str, arc: str) -> None:
        """Optional roots (shared handouts, branding): a missing folder is simply absent from the archive."""
        try:
            fd = _open_root(root_path)
        except FileNotFoundError:
            return
        except OSError as e:
            self.job.skip(arc, f"folder could not be opened: {e.strerror or e}")
            return
        self._consume(fd, arc, None)

    def _consume(self, fd: int, arc: str, on_dir) -> None:
        job = self.job
        with closing(_walk_tree(fd, arc, max_file_bytes=self.max_file_bytes)) as events:
            for ev in events:
                kind = ev[0]
                if kind == "file":
                    self._add_file(ev[1], ev[2], ev[3], ev[4])
                elif kind == "dir":
                    self.tar.addfile(self._info(ev[1], ev[2], tarfile.DIRTYPE))
                    if on_dir:
                        on_dir(ev[3])
                elif kind == "link":
                    ti = self._info(ev[1], ev[2], tarfile.SYMTYPE)
                    ti.linkname = ev[3]
                    self.tar.addfile(ti)
                    job.files += 1
                else:
                    job.skip(ev[1], ev[2])

    def _add_file(self, dir_fd: int, name: str, st: os.stat_result, arc: str) -> None:
        job = self.job
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except OSError as e:
            job.skip(arc, f"could not be opened: {e.strerror or e}")
            return
        with os.fdopen(fd, "rb") as fh:
            fst = os.fstat(fd)
            # A FIFO/device swapped in, or a different file renamed into place, between the listing and the open.
            if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                job.skip(arc, "changed while the backup was running")
                return
            reason = _skip_reason(fst, self.max_file_bytes)   # the size at open time is what counts
            if reason:
                job.skip(arc, reason)
                return
            inode = (fst.st_dev, fst.st_ino)
            if fst.st_nlink > 1 and inode in self.inodes and self.inodes[inode] != arc:
                ti = self._info(fst, arc, tarfile.LNKTYPE)       # hard link to a member already stored
                ti.linkname = self.inodes[inode]
                self.tar.addfile(ti)
                job.files += 1
                return
            ti = self._info(fst, arc, tarfile.REGTYPE)
            ti.size = fst.st_size
            self.tar.addfile(ti, _ExactReader(fh, fst.st_size))
            job.files += 1
            if fst.st_nlink > 1:
                self.inodes[inode] = arc

    # -- hub state --
    def _read_roster(self) -> tuple[bytes | None, int]:
        """
        The roster bytes and mtime, read under the roster lock so a save in
        progress (roster.json -> .bak, tmp -> roster.json) cannot leave us with
        no file; the .bak is the fallback exactly as Roster.load() does it.
        """
        s = self.s
        with Roster(s.roster_path).lock():
            for path in (s.roster_path, s.roster_path + ".bak"):
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | _O_CLOEXEC)
                except FileNotFoundError:
                    continue
                with os.fdopen(fd, "rb") as fh:
                    return fh.read(), int(os.fstat(fd).st_mtime)
        return None, 0

    def add_hubstate(self) -> None:
        s, job = self.s, self.job
        now = int(time.time())
        d = tarfile.TarInfo("hubstate")
        d.type, d.mode, d.mtime = tarfile.DIRTYPE, 0o700, now
        self.tar.addfile(d)
        data, mtime = self._read_roster()
        if data is None:
            job.warnings.append(
                "The class roster (roster.json) was not found, so this backup holds no accounts or passwords. "
                "After a restore the students would have to be added again."
            )
        else:
            ti = tarfile.TarInfo("hubstate/roster.json")
            ti.size, ti.mode, ti.mtime = len(data), 0o600, mtime or now
            self.tar.addfile(ti, io.BytesIO(data))
            job.files += 1
            try:
                users = json.loads(data.decode("utf-8")).get("users", [])
                job.users = len(users) if isinstance(users, list) else 0
            except (ValueError, AttributeError):
                job.users = 0
                job.warnings.append("The class roster (roster.json) could not be read as JSON; it is included as-is.")
        self.add_tree(s.branding_dir, "hubstate/branding")

    def add_manifest(self) -> None:
        s, job = self.s, self.job
        job.files += 1                                   # the manifest counts itself
        manifest = {
            "format": MANIFEST_FORMAT,
            "backup": job.name,
            "created": job.started,
            "finished": iso_now(),
            "image_version": s.image_version,
            "users": job.users,
            "home_folders": job.homes_total or 0,
            "files": job.files,
            "skipped": job.skipped,
            "skipped_items": list(job.skipped_items),
            "skipped_note": (
                f"{job.skipped} item(s) were left out: files bigger than {human_bytes(self.max_file_bytes)}, "
                f"sparse files, pipes/sockets, folders nested deeper than {MAX_DEPTH} levels, or things that changed "
                f"while the backup ran. The first {SKIPPED_LISTED} are listed in skipped_items."
                if job.skipped else "Nothing was left out."
            ),
            "warnings": list(job.warnings),
            "contents": {
                "home/": f"every home folder from {s.home_root}",
                "shared/": f"the shared handouts from {s.shared_dir}",
                "hubstate/roster.json": "student accounts and passwords (plain text - keep this file private)",
                "hubstate/branding/": "school name, announcement and logo",
            },
            "restore": (
                "Stop the container. Extract with `tar -xzf <backup> -C /tmp/restore`, then copy home/* into the "
                f"/home volume, shared/* into {s.shared_dir} and hubstate/* into {s.state_dir}. Start the container "
                "again: accounts are recreated from roster.json on boot."
            ),
        }
        data = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        ti = tarfile.TarInfo("manifest.json")
        ti.size, ti.mode, ti.mtime = len(data), 0o600, int(time.time())
        self.tar.addfile(ti, io.BytesIO(data))


# --- manager ---------------------------------------------------------------------


class BackupManager:
    """One worker thread, one job at a time, the last MAX_JOBS jobs remembered."""

    def __init__(self, settings, *, executor: ThreadPoolExecutor | None = None):
        self.settings = settings
        self.max_file_bytes = int(getattr(settings, "max_backup_file_bytes", MAX_FILE_BYTES) or MAX_FILE_BYTES)
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="backup")
        self.jobs: OrderedDict[str, BackupJob] = OrderedDict()
        self.current: BackupJob | None = None
        # Test hook: called in the worker before anything is written. Tests use it
        # to hold a job open (threading.Event) or to make it fail.
        self.on_start = None
        self._lock = threading.Lock()
        self._estimate_lock = threading.Lock()
        self._estimate: tuple[float, int] | None = None

    # -- names and paths --
    def backup_path(self, name: str) -> str:
        if not BACKUP_NAME_RE.fullmatch(name or ""):
            raise ApiError(400, "bad_name", "That is not a backup file name.")
        return os.path.join(self.settings.backup_dir, name)

    def archive_path(self, name: str) -> str:
        if not ARCHIVE_NAME_RE.fullmatch(name or ""):
            raise ApiError(400, "bad_name", "That is not an archived-student file name.")
        return os.path.join(self.settings.removed_dir, name)

    def _new_name(self) -> str:
        """classroom-backup-YYYYmmdd-HHMMSS.tar.gz, bumped by a second if that name is taken."""
        ts = int(time.time())
        for _ in range(60):
            name = f"classroom-backup-{time.strftime(STAMP_FMT, time.gmtime(ts))}.tar.gz"
            final = os.path.join(self.settings.backup_dir, name)
            if not os.path.lexists(final) and not os.path.lexists(final + PART_SUFFIX):
                return name
            ts += 1
        raise RuntimeError("could not find a free backup file name")

    # -- listings --
    def list_backups(self) -> list[dict]:
        out = []
        try:
            entries = list(os.scandir(self.settings.backup_dir))
        except OSError:
            return out
        for entry in entries:
            m = BACKUP_NAME_RE.fullmatch(entry.name)
            if not m or not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
            created = _stamp_to_iso(f"{m.group(1)}-{m.group(2)}") or iso_from_ts(st.st_mtime)
            out.append({"name": entry.name, "size": st.st_size, "created": created})
        out.sort(key=lambda b: (b["created"], b["name"]), reverse=True)
        return out

    def list_archived(self) -> list[dict]:
        out = []
        try:
            entries = list(os.scandir(self.settings.removed_dir))
        except OSError:
            return out
        for entry in entries:
            m = ARCHIVE_NAME_RE.fullmatch(entry.name)
            if not m or not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
            removed = _stamp_to_iso(m.group(2)) or iso_from_ts(st.st_mtime)
            out.append({"name": entry.name, "username": m.group(1), "removed": removed, "size": st.st_size})
        out.sort(key=lambda a: (a["removed"], a["name"]), reverse=True)
        return out

    # -- disk --
    def estimate(self, force: bool = False) -> int:
        """Bytes the writer will copy (same walk and skip rules), cached ESTIMATE_TTL seconds."""
        with self._estimate_lock:
            now = time.monotonic()
            if not force and self._estimate and now - self._estimate[0] < ESTIMATE_TTL:
                return self._estimate[1]
            s = self.settings
            total = sum(tree_size(p, self.max_file_bytes) for p in (s.home_root, s.shared_dir, s.branding_dir))
            try:
                total += os.lstat(s.roster_path).st_size
            except OSError:
                pass
            self._estimate = (time.monotonic(), total)
            return total

    def cached_estimate(self) -> int | None:
        est = self._estimate
        return est[1] if est else None

    def _usage(self) -> tuple[int, int]:
        for path in (self.settings.backup_dir, self.settings.state_dir, os.sep):
            try:
                du = shutil.disk_usage(path)
                return du.free, du.total
            except OSError:
                continue
        return 0, 0

    def disk(self, estimate: int | None = None) -> dict:
        free, total = self._usage()
        est = self.cached_estimate() if estimate is None else estimate
        needed = (est + SPACE_MARGIN) if est is not None else None
        stored = sum(b["size"] for b in self.list_backups()) + sum(a["size"] for a in self.list_archived())
        warning = None
        if needed is not None and free < needed:
            warning = (
                f"Not enough free space for a new backup: {human_bytes(free)} free, about {human_bytes(needed)} needed. "
                "Download old backups to another computer, then delete them here to make room."
            )
        elif est is not None and free < 2 * est:
            warning = (
                f"Space is getting tight: {human_bytes(free)} free and a backup needs about {human_bytes(est)}. "
                "Download and delete old backups to keep room for the students' work."
            )
        elif total and free < total * 0.05:
            warning = (
                f"This server's disk is almost full ({human_bytes(free)} of {human_bytes(total)} free). "
                "Delete old backups you have already downloaded."
            )
        return {"free": free, "total": total, "estimate_bytes": est, "needed": needed, "stored_bytes": stored, "warning": warning}

    # -- jobs --
    def get(self, job_id: str) -> BackupJob | None:
        return self.jobs.get(job_id)

    def running(self) -> BackupJob | None:
        job = self.current
        return job if job and job.state == "running" else None

    def latest(self) -> BackupJob | None:
        return next(reversed(self.jobs.values()), None) if self.jobs else None

    def start(self, estimate: int) -> BackupJob:
        """Create and submit a job. Raises BackupBusy or NotEnoughSpace."""
        with self._lock:
            running = self.running()
            if running:
                raise BackupBusy(running)
            free, _ = self._usage()
            needed = estimate + SPACE_MARGIN
            if free < needed:
                raise NotEnoughSpace(free, needed)
            os.makedirs(self.settings.backup_dir, mode=0o700, exist_ok=True)
            name = self._new_name()
            final = os.path.join(self.settings.backup_dir, name)
            job = BackupJob(id=secrets.token_hex(8), name=name, part_path=final + PART_SUFFIX, final_path=final)
            self.jobs[job.id] = job
            while len(self.jobs) > MAX_JOBS:
                oldest = next(iter(self.jobs))
                if self.jobs[oldest].state == "running":
                    break
                del self.jobs[oldest]
            self.current = job
            try:
                self.executor.submit(self._run, job)
            except RuntimeError as e:          # executor already shut down
                job.state, job.error, job.finished = "failed", f"backup worker unavailable: {e}", iso_now()
                self.current = None
                raise
            return job

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def cleanup_stale_parts(self) -> int:
        """Remove .part files left by a backup that was interrupted by a restart."""
        removed = 0
        try:
            entries = list(os.scandir(self.settings.backup_dir))
        except OSError:
            return 0
        for entry in entries:
            if entry.name.endswith(PART_SUFFIX) and BACKUP_NAME_RE.fullmatch(entry.name[: -len(PART_SUFFIX)]):
                if not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    os.unlink(entry.path)
                    removed += 1
                    log.warning("removed unfinished backup %s", entry.name)
                except OSError as e:
                    log.warning("cannot remove unfinished backup %s: %s", entry.name, e)
        return removed

    # -- worker --
    def _run(self, job: BackupJob) -> None:
        s = self.settings
        started = time.monotonic()
        try:
            if self.on_start:
                self.on_start(job)
            fd = os.open(job.part_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_CLOEXEC, 0o600)
            with os.fdopen(fd, "wb") as raw:
                with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6, format=tarfile.PAX_FORMAT) as tar:
                    writer = _Archiver(s, tar, job, self.max_file_bytes)
                    writer.add_homes()
                    job.phase = "shared handouts"
                    writer.add_tree(s.shared_dir, "shared")
                    job.phase = "roster and settings"
                    writer.add_hubstate()
                    job.phase = "finishing"
                    writer.add_manifest()
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(job.part_path, job.final_path)
            os.chmod(job.final_path, 0o600)
            job.size = os.stat(job.final_path).st_size
            job.finished = iso_now()
            job.state = "done"                           # state is written last: it is what pollers key on
            log.info("backup %s written: %s, %d files, %d left out, %d warning(s), %.1f s",
                     job.name, human_bytes(job.size), job.files, job.skipped, len(job.warnings), time.monotonic() - started)
        except Exception as e:  # noqa: BLE001 - anything means "failed", the teacher sees the message
            log.exception("backup %s failed", job.name)
            try:
                os.unlink(job.part_path)
            except OSError:
                pass
            job.error = str(e) or e.__class__.__name__
            job.finished = iso_now()
            job.state = "failed"
        finally:
            with self._lock:
                if self.current is job:
                    self.current = None


# --- app wiring ------------------------------------------------------------------


def setup(app) -> None:
    manager = BackupManager(app.state.settings)
    manager.cleanup_stale_parts()
    app.state.backups = manager


def teardown(app) -> None:
    manager = getattr(app.state, "backups", None)
    if manager:
        manager.shutdown()


def _manager(request: Request) -> BackupManager:
    manager = getattr(request.app.state, "backups", None)
    if manager is None:
        raise ApiError(503, "backups_unavailable", "Backups are not available: the console did not start completely.")
    return manager


# --- routes ----------------------------------------------------------------------


@router.get("/api/backups")
async def list_backups(request: Request, user: str = Depends(require_admin)):
    m = _manager(request)
    estimate = await asyncio.to_thread(m.estimate)

    def gather():
        return {"backups": m.list_backups(), "archived": m.list_archived(), "disk": m.disk(estimate)}

    data = await asyncio.to_thread(gather)
    job = m.latest()
    data["job"] = job.to_dict() if job else None
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.post("/api/backups")
async def create_backup(request: Request, user: str = Depends(require_admin)):
    m = _manager(request)
    running = m.running()
    if running:
        raise _busy(running)
    estimate = await asyncio.to_thread(m.estimate)
    try:
        job = m.start(estimate)
    except BackupBusy as e:
        raise _busy(e.job) from None
    except NotEnoughSpace as e:
        raise ApiError(
            507, "insufficient_storage",
            f"Not enough free space for a backup: {human_bytes(e.free)} free, about {human_bytes(e.needed)} needed. "
            "Download old backups to another computer and delete them here, or free space on the server.",
            {"free": e.free, "needed": e.needed},
        ) from None
    except RuntimeError as e:
        raise ApiError(503, "backups_unavailable", f"The backup worker is not available: {e}") from None
    log.info("backup %s started by %s", job.name, user)
    return JSONResponse({"job": job.to_dict()}, status_code=202, headers={"Cache-Control": "no-store"})


def _busy(job: BackupJob) -> ApiError:
    return ApiError(409, "job_running", "A backup is already being created. Wait for it to finish.", {"job": job.to_dict()})


@router.get("/api/backups/jobs/{job_id}")
async def backup_job(job_id: str, request: Request, user: str = Depends(require_admin)):
    job = _manager(request).get(job_id)
    if job is None:
        raise ApiError(404, "not_found", "That backup job is not known (the console may have restarted).")
    return JSONResponse(job.to_dict(), headers={"Cache-Control": "no-store"})


@router.get("/api/backups/{name}/download")
async def download_backup(name: str, request: Request, user: str = Depends(require_admin)):
    path = _manager(request).backup_path(name)
    if not _is_regular(path):
        raise ApiError(404, "not_found", "That backup does not exist any more.")
    return FileResponse(path, media_type="application/gzip", filename=name, headers=NO_STORE)


@router.delete("/api/backups/{name}")
async def delete_backup(name: str, request: Request, user: str = Depends(require_admin)):
    m = _manager(request)
    path = m.backup_path(name)
    running = m.running()
    if running and running.name == name:
        raise ApiError(409, "job_running", "That backup is still being created. Wait for it to finish before deleting it.")
    if not _is_regular(path):
        raise ApiError(404, "not_found", "That backup does not exist any more.")
    await asyncio.to_thread(os.unlink, path)
    log.info("backup %s deleted by %s", name, user)
    return Response(status_code=204)


@router.get("/api/archive/{name}/download")
async def download_archive(name: str, request: Request, user: str = Depends(require_admin)):
    path = _manager(request).archive_path(name)
    if not _is_regular(path):
        raise ApiError(404, "not_found", "That archive does not exist any more.")
    return FileResponse(path, media_type="application/gzip", filename=name, headers=NO_STORE)


@router.delete("/api/archive/{name}")
async def delete_archive(name: str, request: Request, user: str = Depends(require_admin)):
    path = _manager(request).archive_path(name)
    if not _is_regular(path):
        raise ApiError(404, "not_found", "That archive does not exist any more.")
    await asyncio.to_thread(os.unlink, path)
    log.info("archived student %s deleted by %s", name, user)
    return Response(status_code=204)
