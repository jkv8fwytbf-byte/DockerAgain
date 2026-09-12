"""
The class roster: who the students are and how they log in.

Stored as /srv/jupyterhub/roster.json (root-only) with the shape

    {"version": 1, "users": [{"username": "priya", "password": "blue-tiger-27",
                              "display_name": "Priya Sharma", "created": "2026-09-13T08:00:00Z"}]}

Passwords are kept in plain text on purpose: the teacher prints login cards
from them. The file lives in a 0700 directory on a volume, mode 0600.

This module also owns the validation rules and the parsers for users.txt and
for the console's "bulk add" text box, so every entry point applies the same
rules.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
RESERVED_USERNAMES = {
    "root", "jovyan", "nobody", "daemon", "bin", "sys", "sync", "games", "man", "lp",
    "mail", "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "_apt",
    "admin", "administrator", "hub", "jupyterhub", "console",
}
PASSWORD_MIN, PASSWORD_MAX = 1, 72
DISPLAY_NAME_MAX = 64

# Simple, classroom-safe words for generated passwords ("blue-tiger-27").
WORDS = (
    "apple banana cherry mango lemon melon grape peach plum berry "
    "tiger lion zebra panda koala otter eagle falcon dolphin whale "
    "red blue green amber coral ivory olive violet silver golden "
    "river ocean forest meadow island canyon summit valley harbor desert "
    "rocket comet planet galaxy nebula meteor saturn orbit lunar solar "
    "maple cedar willow birch bamboo lotus tulip daisy clover fern"
).split()


class RosterError(Exception):
    """The roster file cannot be read or written."""


@dataclass
class Student:
    username: str
    password: str
    display_name: str = ""
    created: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Student":
        return cls(
            username=str(d.get("username", "")).strip().lower(),
            password=str(d.get("password", "")),
            display_name=str(d.get("display_name", "") or ""),
            created=str(d.get("created", "") or ""),
        )


# --- validation ----------------------------------------------------------------


def normalize_username(raw: str) -> str:
    return (raw or "").strip().lower()


def validate_username(raw: str) -> str:
    """Return the normalized username or raise ValueError with a teacher-readable reason."""
    name = normalize_username(raw)
    if not name:
        raise ValueError("username is empty")
    if not USERNAME_RE.match(name):
        raise ValueError(
            "usernames use lowercase letters, digits, - and _ only, start with a letter, max 32 characters"
        )
    if name in RESERVED_USERNAMES:
        raise ValueError(f"'{name}' is reserved")
    return name


def validate_password(raw: str) -> str:
    pw = raw if raw is not None else ""
    if len(pw) < PASSWORD_MIN:
        raise ValueError("password is empty")
    if len(pw) > PASSWORD_MAX:
        raise ValueError(f"password is longer than {PASSWORD_MAX} characters")
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in pw):
        raise ValueError("password may only contain printable ASCII characters (no newlines)")
    return pw


def validate_display_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if len(name) > DISPLAY_NAME_MAX:
        raise ValueError(f"name is longer than {DISPLAY_NAME_MAX} characters")
    if any(ord(ch) < 0x20 for ch in name):
        raise ValueError("name contains control characters")
    return name


def generate_password(rng=None) -> str:
    """Two simple words and a two-digit number, e.g. blue-tiger-27."""
    rng = rng or secrets.SystemRandom()
    a = rng.choice(WORDS)
    b = rng.choice(WORDS)
    while b == a:
        b = rng.choice(WORDS)
    return f"{a}-{b}-{rng.randrange(10, 100)}"


def _ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def suggest_username(display_name: str, taken: set[str] | None = None) -> str:
    """priya -> priyas -> priyas2 ... ; never returns an empty or reserved name."""
    taken = {t.lower() for t in (taken or set())}
    parts = [p for p in _ascii_fold(display_name or "").lower().split() if p]
    parts = [re.sub(r"[^a-z0-9_-]", "", p) for p in parts]
    parts = [p for p in parts if p]
    first = parts[0] if parts else "student"
    first = first[:24]
    if not re.match(r"^[a-z_]", first):
        first = "s" + first
    candidates = [first]
    if len(parts) > 1:
        candidates.append((first + parts[-1][0])[:32])
        candidates.append((first + parts[-1])[:32])
    for cand in candidates:
        if cand not in taken and cand not in RESERVED_USERNAMES and USERNAME_RE.match(cand):
            return cand
    n = 2
    while True:
        cand = f"{first[:28]}{n}"
        if cand not in taken and cand not in RESERVED_USERNAMES:
            return cand
        n += 1


# --- parsers -------------------------------------------------------------------


def parse_users_txt(text: str) -> tuple[list[Student], list[tuple[int, str]]]:
    """
    One "username:password" per line; '#' starts a comment; blank lines ignored.
    Returns (students, errors) where errors are (line number, reason).
    """
    students: list[Student] = []
    errors: list[tuple[int, str]] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].replace("\r", "").strip()
        if not line:
            continue
        if ":" not in line:
            errors.append((lineno, "expected username:password"))
            continue
        user_raw, pw = line.split(":", 1)
        try:
            username = validate_username(user_raw)
            password = validate_password(pw)
        except ValueError as e:
            errors.append((lineno, str(e)))
            continue
        if username in seen:
            errors.append((lineno, f"'{username}' listed twice; first entry kept"))
            continue
        seen.add(username)
        students.append(Student(username=username, password=password))
    return students, errors


def parse_bulk_text(text: str) -> tuple[list[dict], list[tuple[int, str]]]:
    """
    The console's paste box. Each line is one of
        Display Name
        Display Name, username
        Display Name, username, password
        username:password                (users.txt style)
    A header row starting with "name" is skipped. Missing usernames/passwords
    are left empty for the caller to generate. Returns (rows, errors).
    """
    rows: list[dict] = []
    errors: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.replace("\r", "").strip()
        if not line or line.startswith("#"):
            continue
        if lineno == 1 and line.lower().replace(" ", "").startswith(("name,", "displayname,", "display_name,")):
            continue
        if ":" in line and "," not in line:
            user_raw, pw = line.split(":", 1)
            rows.append({"display_name": "", "username": normalize_username(user_raw), "password": pw.strip(), "line": lineno})
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) > 3:
            errors.append((lineno, "too many commas (expected name, username, password)"))
            continue
        display, username, password = (parts + ["", "", ""])[:3]
        if not display and not username:
            errors.append((lineno, "no name or username"))
            continue
        rows.append(
            {"display_name": display, "username": normalize_username(username), "password": password, "line": lineno}
        )
    return rows, errors


# --- storage -------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Roster:
    """Load/save the roster with a cross-process file lock and atomic writes."""

    VERSION = 1

    def __init__(self, path: str, lock_path: str | None = None):
        self.path = path
        self.lock_path = lock_path or (path + ".lock")
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd = None

    # -- locking --
    @contextmanager
    def lock(self):
        """Re-entrant in-process lock plus a cross-process flock (taken once, at depth 0)."""
        with self._thread_lock:
            if self._depth == 0:
                os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
                self._fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fd is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                    os.close(self._fd)
                    self._fd = None

    # -- read --
    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def load(self) -> list[Student]:
        """Missing file -> []. Corrupt file -> .bak is tried; else RosterError."""
        if not os.path.exists(self.path):
            return []
        try:
            return self._read(self.path)
        except (ValueError, OSError, TypeError) as primary:
            bak = self.path + ".bak"
            if os.path.exists(bak):
                try:
                    students = self._read(bak)
                    return students
                except (ValueError, OSError, TypeError):
                    pass
            raise RosterError(f"cannot read {self.path}: {primary}") from primary

    @staticmethod
    def _read(path: str) -> list[Student]:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        users = data.get("users") if isinstance(data, dict) else data
        if not isinstance(users, list):
            raise ValueError("roster has no 'users' list")
        students = []
        seen = set()
        for entry in users:
            if not isinstance(entry, dict):
                continue
            s = Student.from_dict(entry)
            if not s.username or s.username in seen:
                continue
            seen.add(s.username)
            students.append(s)
        return students

    # -- write --
    def save(self, students: list[Student]) -> None:
        payload = {
            "version": self.VERSION,
            "updated": _utcnow_iso(),
            "users": [s.to_dict() for s in students],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        if os.path.exists(self.path):
            try:
                os.replace(self.path, self.path + ".bak")
            except OSError:
                pass
        tmp = f"{self.path}.tmp-{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise RosterError(f"cannot write {self.path}: {e}") from e

    # -- helpers --
    @staticmethod
    def find(students: list[Student], username: str) -> Student | None:
        username = normalize_username(username)
        for s in students:
            if s.username == username:
                return s
        return None

    @staticmethod
    def new_student(username: str, password: str, display_name: str = "") -> Student:
        return Student(
            username=validate_username(username),
            password=validate_password(password),
            display_name=validate_display_name(display_name),
            created=_utcnow_iso(),
        )
