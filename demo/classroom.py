"""A seeded Lincoln High School class used by the Docker-free demo."""
from __future__ import annotations

import io
import os
import shutil
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from console.roster import Roster, Student
from console.util import atomic_write_json, iso_from_ts

ROOT = Path(__file__).resolve().parent
HANDOUTS = ROOT / "handouts"

# Display names and passwords are classroom-safe examples (same style as the
# welcome kit and the console's bulk-add placeholder).
CLASS = (
    {"username": "teacher", "password": "change-me-teacher", "display_name": "Ms Rivera",
     "admin": True, "running": True, "idle_s": 90, "cpu": 8.0, "mem_mb": 280, "nproc": 4},
    {"username": "priya", "password": "coral-otter-12", "display_name": "Priya Sharma",
     "running": True, "idle_s": 25, "cpu": 22.0, "mem_mb": 740, "nproc": 6,
     "submit": {"hello-from-priya.txt": "Hello from priya, written on 2026-09-14 09:12:04\n",
                "01-python-and-data-priya.ipynb": ""}},
    {"username": "liam", "password": "maple-river-07", "display_name": "Liam Chen",
     "running": True, "idle_s": 180, "cpu": 41.0, "mem_mb": 1100, "nproc": 9,
     "submit": {"03-loops-liam.ipynb": ""}},
    {"username": "ana", "password": "golden-cedar-33", "display_name": "Ana Silva",
     "running": False, "idle_s": 5400, "cpu": 0.0, "mem_mb": 0, "nproc": 0,
     "submit": {"week-1-ana.md": "I finished the sine-wave cell in Welcome.ipynb.\n"}},
    {"username": "jordan", "password": "blue-tiger-21", "display_name": "Jordan Blake",
     "running": True, "idle_s": 70, "cpu": 15.0, "mem_mb": 520, "nproc": 5,
     "submit": {"hello-from-jordan.txt": "Hello from jordan, written on 2026-09-14 10:03:11\n"}},
    {"username": "sam", "password": "olive-falcon-44", "display_name": "Sam Okonkwo",
     "running": False, "idle_s": 86000, "cpu": 0.0, "mem_mb": 0, "nproc": 0},
    {"username": "nora", "password": "silver-willow-18", "display_name": "Nora Patel",
     "running": True, "idle_s": 12, "cpu": 33.0, "mem_mb": 890, "nproc": 7,
     "submit": {"radio-spectrum-nora.md": "The 1 kHz peak showed up. No dongle plugged in.\n"}},
)

BRANDING = {
    "school_name": "Lincoln High School",
    "class_name": "Grade 10 · Python & Radio Lab",
    "accent": "orange",
    "announcement": "Homework 3 is due Friday. Copy it from shared/ into notebooks/, then save your work in submit/.",
    "class_url": "http://192.168.1.10:8000",
    "logo_custom": False,
}

IMPORT_USERS_TXT = """\
# Extra students for Students → Import users.txt
mei:sunny-lotus-09
omar:amber-canyon-15
"""

NOW = datetime(2026, 9, 14, 12, 30, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def copy_handouts(shared_dir: str) -> list[str]:
    """Copy demo/handouts into the shared folder every student sees as ~/shared."""
    dest = Path(shared_dir)
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for src in sorted(HANDOUTS.iterdir()):
        if (
            not src.is_file()
            or src.name.startswith(("_", "."))
            or src.suffix not in {".ipynb", ".md", ".csv"}
        ):
            continue
        target = dest / src.name
        shutil.copyfile(src, target)
        copied.append(src.name)
    return copied


def seed_roster(roster_path: str) -> list[Student]:
    created = _iso(NOW - timedelta(days=12))
    students = [
        Student(c["username"], c["password"], c["display_name"], created)
        for c in CLASS
    ]
    Roster(roster_path).save(students)
    return students


def seed_accounts(accounts, hub) -> None:
    accounts.ensure_groups()
    for person in CLASS:
        accounts.create(person["username"], person["password"])
        if person.get("admin"):
            accounts.ensure_admin(person["username"])
        idle = int(person.get("idle_s") or 0)
        stamp = _iso(NOW - timedelta(seconds=idle)) if person.get("running") else None
        hub.add(person["username"], admin=bool(person.get("admin")), running=bool(person.get("running")),
                last_activity=stamp)


def seed_submissions(home_root: str) -> None:
    for person in CLASS:
        files = person.get("submit") or {}
        submit = Path(home_root) / person["username"] / "submit"
        submit.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            path = submit / name
            if name.endswith(".ipynb") and not content:
                src = HANDOUTS / "01-python-and-data.ipynb"
                if src.is_file() and "python" in name:
                    shutil.copyfile(src, path)
                    continue
                path.write_text(_tiny_notebook(person["display_name"], name), encoding="utf-8")
            else:
                path.write_text(content, encoding="utf-8")


def _tiny_notebook(display_name: str, filename: str) -> str:
    """A one-cell notebook so a submission zip is a real .ipynb, not an empty file."""
    import json

    title = filename.rsplit(".", 1)[0].replace("-", " ")
    nb = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3 (ipykernel)", "language": "python"}},
        "cells": [
            {"cell_type": "markdown", "id": "title", "metadata": {},
             "source": [f"# {title}\n", "\n", f"Handed in by {display_name}.\n"]},
            {"cell_type": "code", "id": "hello", "metadata": {}, "outputs": [], "execution_count": None,
             "source": [f'print("Handed in by {display_name}")\n']},
        ],
    }
    return json.dumps(nb, indent=1) + "\n"


def seed_branding(settings) -> None:
    os.makedirs(settings.branding_dir, mode=0o755, exist_ok=True)
    shutil.copyfile(settings.default_logo, settings.logo_file)
    os.chmod(settings.logo_file, 0o644)
    atomic_write_json(settings.branding_json, BRANDING, 0o600)


def seed_logs(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    hub = Path(log_dir) / "jupyterhub.log"
    console = Path(log_dir) / "console.log"
    hub.write_text("\n".join(_hub_log_lines()) + "\n", encoding="utf-8")
    console.write_text("\n".join(_console_log_lines()) + "\n", encoding="utf-8")


def _hub_log_lines() -> list[str]:
    t0 = NOW - timedelta(hours=2)
    rows = [
        (0, "I", "JupyterHub app:2234", "JupyterHub 5.3.0 is running at http://0.0.0.0:8000"),
        (2, "I", "JupyterHub proxy:780", "Adding user priya to proxy /user/priya/ => http://127.0.0.1:54501"),
        (3, "I", "JupyterHub log:192", "200 GET /hub/api/users (console@127.0.0.1)"),
        (8, "I", "JupyterHub spawner:1644", "liam's server is ready"),
        (12, "W", "JupyterHub spawner:1401", "sam's server has not been used in 23 hours; idle culler will stop it"),
        (40, "I", "JupyterHub login:88", "User logged in: nora"),
        (41, "I", "JupyterHub spawner:1644", "nora's server is ready"),
        (55, "E", "JupyterHub spawner:1788", "Spawn failed for ghost01: No such user"),
        (90, "I", "JupyterHub log:192", "200 GET /hub/home (teacher@10.0.0.14)"),
        (110, "I", "JupyterHub proxy:780", "Adding user jordan to proxy /user/jordan/ => http://127.0.0.1:54544"),
        (118, "W", "JupyterHub palogin:41", "PAM authenticate failed for username 'student99'"),
        (125, "I", "JupyterHub login:88", "User logged in: priya"),
    ]
    out = []
    for offset, level, logger, msg in rows:
        stamp = (t0 + timedelta(minutes=offset)).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"[{level} {stamp} {logger}] {msg}")
    return out


def _console_log_lines() -> list[str]:
    t0 = NOW - timedelta(hours=2)
    rows = [
        (0, "I", "console", "starting teacher console on 127.0.0.1:8099 prefix /services/console/"),
        (0, "I", "console", "teacher console 1.1.0 ready at /services/console/ (image 1.1)"),
        (15, "I", "console.students", "added student nora by teacher"),
        (80, "I", "console.files", "uploaded week-1-homework.md into shared by teacher"),
        (100, "I", "console.branding", "saved branding by teacher"),
    ]
    out = []
    for offset, level, logger, msg in rows:
        stamp = (t0 + timedelta(minutes=offset)).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"[{level} {stamp} {logger}] {msg}")
    return out


def seed_archived_student(removed_dir: str) -> str:
    """A student removed last week, so Backups → Archived students is not empty."""
    os.makedirs(removed_dir, mode=0o700, exist_ok=True)
    name = "maya-20260907-154800.tar.gz"
    path = os.path.join(removed_dir, name)
    payload = (
        "Maya Alvarez left the class on 7 September 2026.\n"
        "This archive is her home folder at the moment she was removed.\n"
    ).encode()
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name="home/maya/submit/final-maya.md")
        info.size = len(payload)
        info.mtime = int(datetime(2026, 9, 7, 15, 48, tzinfo=timezone.utc).timestamp())
        tar.addfile(info, fileobj=io.BytesIO(payload))
    os.chmod(path, 0o600)
    mtime = datetime(2026, 9, 7, 15, 48, tzinfo=timezone.utc).timestamp()
    os.utime(path, (mtime, mtime))
    return name


def seed_import_file(path: str) -> None:
    Path(path).write_text(IMPORT_USERS_TXT, encoding="utf-8")


def snapshot_for(accounts) -> dict:
    """A dashboard snapshot with per-student CPU/RAM so the live tiles look occupied."""
    users = {}
    names = {}
    ncpu = 8
    for person in CLASS:
        try:
            uid = accounts.sys.getpwnam(person["username"]).pw_uid
        except KeyError:
            continue
        names[uid] = person["username"]
        if person.get("running"):
            users[uid] = {
                "cpu_sum": float(person.get("cpu") or 0) * ncpu,
                "rss_bytes": int(person.get("mem_mb") or 0) * 1024 * 1024,
                "nproc": int(person.get("nproc") or 1),
                "has_server": True,
            }
    ts = NOW.timestamp()
    return {
        "sampled_at": _iso(NOW),
        "ts": ts,
        "host": {
            "cpu_pct": 28.0, "ncpu": ncpu, "load1": 1.8,
            "mem_total": 32 * 1024 ** 3, "mem_used": 11 * 1024 ** 3, "mem_available": 21 * 1024 ** 3,
            "swap_used": 0, "boot_time": ts - 3 * 86400, "uptime_s": 3 * 86400,
            "disks": {
                "home": {"total": 500 * 1024 ** 3, "used": 48 * 1024 ** 3, "free": 452 * 1024 ** 3},
                "state": {"total": 500 * 1024 ** 3, "used": 4 * 1024 ** 3, "free": 496 * 1024 ** 3},
                "shared": {"total": 500 * 1024 ** 3, "used": 80 * 1024 ** 2, "free": 500 * 1024 ** 3},
            },
            "disk_path": accounts.s.home_root,
        },
        "users": users,
        "names": names,
    }


class FrozenSampler:
    """Dashboard sampler that holds one snapshot still, so the demo is deterministic."""

    def __init__(self, snap: dict):
        self.snap = snap

    def snapshot(self):
        # Keep timestamps fresh so "last activity" ages while the demo runs.
        now = time.time()
        host = dict(self.snap.get("host") or {})
        boot = host.get("boot_time") or (now - 3 * 86400)
        host = dict(host, uptime_s=int(now - boot))
        return {**self.snap, "sampled_at": iso_from_ts(now), "ts": now, "host": host}

    def start(self):
        return None

    def stop(self, timeout: float = 1.0):
        return None

    def invalidate_names(self):
        return None
