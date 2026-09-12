"""Small shared helpers: safe paths, atomic writes, timestamps, branding read."""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone


class UnsafePath(ValueError):
    """A client-supplied path tried to leave its root."""


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o644) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj, mode: int = 0o600) -> None:
    atomic_write_bytes(path, (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8"), mode)


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


_NAME_BAD = re.compile(r"[\x00-\x1f\x7f/\\]")


def clean_filename(name: str, max_len: int = 200) -> str:
    """Basename only, no control characters or path separators, never '.'/'..'/empty."""
    name = os.path.basename((name or "").replace("\\", "/").strip())
    name = _NAME_BAD.sub("", name).strip()
    if name in ("", ".", ".."):
        raise UnsafePath("invalid file name")
    if name.startswith("."):
        name = "_" + name[1:]
    return name[:max_len]


def safe_join(root: str, rel: str, *, allow_root: bool = False, no_symlink_components: bool = False) -> str:
    """
    Join rel onto root and guarantee the result stays inside root.
    Rejects NUL, backslashes, absolute paths and any '..' component.
    With no_symlink_components, every *existing* component below root must not
    be a symlink (use for writes and deletes so a planted link cannot redirect them).
    """
    rel = (rel or "").replace("\\", "/")
    if "\x00" in rel:
        raise UnsafePath("invalid path")
    rel = rel.strip("/")
    if not rel:
        if allow_root:
            return os.path.realpath(root)
        raise UnsafePath("path required")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafePath("path may not contain '..'")
    root_real = os.path.realpath(root)
    target = os.path.join(root_real, *parts)
    if not os.path.realpath(target).startswith(root_real + os.sep) and os.path.realpath(target) != root_real:
        raise UnsafePath("path escapes its folder")
    if no_symlink_components:
        cur = root_real
        for p in parts:
            cur = os.path.join(cur, p)
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                break
            if stat.S_ISLNK(st.st_mode):
                raise UnsafePath("path goes through a link")
    return target


def human_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


ACCENTS = {
    "orange": "#f37524",
    "blue": "#2c7bb6",
    "green": "#1a8f4e",
    "purple": "#6f42c1",
    "teal": "#0d8a8a",
    "slate": "#495057",
}
BRANDING_DEFAULTS = {
    "school_name": "Classroom JupyterHub",
    "class_name": "",
    "accent": "orange",
    "announcement": "",
    "class_url": "",
    "logo_custom": False,
}
BRANDING_LIMITS = {"school_name": 80, "class_name": 120, "announcement": 300, "class_url": 200}


def read_branding(path: str) -> dict:
    """Sanitised branding values (plain strings; templates escape them)."""
    raw = read_json(path, {}) or {}
    out = {}
    for k, default in BRANDING_DEFAULTS.items():
        v = raw.get(k, default) if isinstance(raw, dict) else default
        if isinstance(default, bool):
            out[k] = bool(v)
        else:
            v = "" if v is None else str(v)
            out[k] = v[: BRANDING_LIMITS.get(k, 200)]
    if out["accent"] not in ACCENTS:
        out["accent"] = "orange"
    out["accent_hex"] = ACCENTS[out["accent"]]
    return out
