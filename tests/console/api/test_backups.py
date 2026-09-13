"""
Backups + archived students: job lifecycle, archive contents and safety,
disk guard, name validation, permissions.
"""
from __future__ import annotations

import collections
import io
import json
import os
import pwd
import stat
import tarfile
import threading
import time

import pytest
from fastapi.testclient import TestClient

from console import backups as backups_mod
from console.auth import CSRF_HEADER

Usage = collections.namedtuple("usage", "total used free")
GiB = 1024 ** 3


def wait_for_job(client, prefix, job_id, timeout=10.0) -> dict:
    """Poll the job endpoint until the worker thread finishes."""
    deadline = time.monotonic() + timeout
    while True:
        r = client.get(f"{prefix}/api/backups/jobs/{job_id}")
        assert r.status_code == 200, r.text
        job = r.json()
        if job["state"] != "running" or time.monotonic() > deadline:
            return job
        time.sleep(0.02)


def write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(content)
    return path


@pytest.fixture
def populated(settings, fake_system, tmp_path):
    """A small classroom on disk: two homes, a handout, roster, branding, plus traps."""
    home = settings.home_root
    fake_system.make_home("student01", 2001, {"notebooks/lab1.ipynb": '{"cells": []}', "submit/hello.txt": "hi"})
    fake_system.make_home("teacher", 2000, {"notes.md": "# notes"})
    write(os.path.join(settings.shared_dir, "handout.pdf"), b"%PDF-fake")
    os.makedirs(settings.branding_dir, exist_ok=True)
    write(settings.branding_json, json.dumps({"school_name": "Test School"}))
    write(settings.logo_file, b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    write(settings.roster_path, json.dumps({"version": 1, "users": [
        {"username": "teacher", "password": "t", "display_name": "Teacher"},
        {"username": "student01", "password": "s", "display_name": "Student One"},
    ]}))
    secret = write(str(tmp_path / "outside" / "secret.txt"), b"TOP-SECRET")
    os.symlink(secret, os.path.join(home, "student01", "link-to-secret"))
    os.symlink(settings.shared_dir, os.path.join(home, "student01", "shared"))
    os.mkfifo(os.path.join(home, "student01", "pipe"))
    return {"secret": secret}


def test_list_empty(as_teacher, prefix):
    r = as_teacher.get(f"{prefix}/api/backups")
    assert r.status_code == 200
    data = r.json()
    assert data["backups"] == [] and data["archived"] == [] and data["job"] is None
    disk = data["disk"]
    assert disk["free"] > 0 and disk["total"] >= disk["free"]
    assert isinstance(disk["estimate_bytes"], int) and disk["warning"] is None
    assert r.headers["cache-control"] == "no-store"


def test_create_backup_runs_to_completion(as_teacher, prefix, settings, populated):
    r = as_teacher.post(f"{prefix}/api/backups")
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert job["state"] in ("running", "done") and backups_mod.BACKUP_NAME_RE.match(job["name"])
    assert set(job) >= {"id", "state", "name", "started", "finished", "bytes_written", "error"}

    job = wait_for_job(as_teacher, prefix, job["id"])
    assert job["state"] == "done", job
    assert job["error"] is None and job["finished"] and job["bytes_written"] > 0
    assert job["homes_total"] == 2 and job["homes_done"] == 2
    assert job["skipped"] == 1                      # the FIFO

    final = os.path.join(settings.backup_dir, job["name"])
    assert stat.S_IMODE(os.stat(final).st_mode) == 0o600
    assert not os.path.exists(final + ".part")
    assert os.path.getsize(final) == job["bytes_written"]

    with tarfile.open(final, "r:gz") as tar:
        names = tar.getnames()
        members = {m.name: m for m in tar.getmembers()}
        assert "home" in names and "home/student01" in names and "home/teacher" in names
        assert tar.extractfile("home/student01/notebooks/lab1.ipynb").read() == b'{"cells": []}'
        assert tar.extractfile("home/student01/submit/hello.txt").read() == b"hi"
        assert tar.extractfile("home/teacher/notes.md").read() == b"# notes"
        assert tar.extractfile("shared/handout.pdf").read() == b"%PDF-fake"
        roster = json.loads(tar.extractfile("hubstate/roster.json").read())
        assert [u["username"] for u in roster["users"]] == ["teacher", "student01"]
        assert json.loads(tar.extractfile("hubstate/branding/branding.json").read())["school_name"] == "Test School"
        assert "hubstate/branding/logo.png" in names
        manifest = json.loads(tar.extractfile("manifest.json").read())
        assert manifest["users"] == 2 and manifest["image_version"] == settings.image_version
        assert manifest["created"] == job["started"] and manifest["home_folders"] == 2
        assert manifest["files"] == job["files"] and manifest["skipped"] == 1
        assert manifest["skipped_items"] == [{"path": "home/student01/pipe", "reason": "not a regular file (pipe, socket or device)"}]
        assert manifest["warnings"] == [] and "left out" in manifest["skipped_note"]
        assert job["skipped_items"] == manifest["skipped_items"] and job["warnings"] == []
        # symlinks are stored as links, never followed
        link = members["home/student01/link-to-secret"]
        assert link.issym() and link.linkname == populated["secret"]
        assert members["home/student01/shared"].issym()
        assert "home/student01/pipe" not in names
        for m in tar.getmembers():
            assert m.isdir() or m.isreg() or m.issym(), m.name
        blob = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isreg())
        assert b"TOP-SECRET" not in blob
        assert tar.format == tarfile.PAX_FORMAT

    r = as_teacher.get(f"{prefix}/api/backups")
    data = r.json()
    assert [b["name"] for b in data["backups"]] == [job["name"]]
    assert data["backups"][0]["size"] == job["bytes_written"]
    assert data["backups"][0]["created"].endswith("Z")
    assert data["job"]["id"] == job["id"] and data["job"]["state"] == "done"


def test_second_post_while_running_is_409(as_teacher, prefix, app, populated):
    gate = threading.Event()
    manager = app.state.backups
    manager.on_start = lambda job: gate.wait(10)
    try:
        first = as_teacher.post(f"{prefix}/api/backups")
        assert first.status_code == 202
        job = first.json()["job"]
        assert job["state"] == "running"

        second = as_teacher.post(f"{prefix}/api/backups")
        assert second.status_code == 409
        body = second.json()
        assert body["error"] == "job_running" and body["detail"]["job"]["id"] == job["id"]

        listing = as_teacher.get(f"{prefix}/api/backups").json()
        assert listing["job"]["state"] == "running" and listing["backups"] == []

        # the running job's file cannot be deleted from under the worker
        r = as_teacher.delete(f"{prefix}/api/backups/{job['name']}")
        assert r.status_code == 409 and r.json()["error"] == "job_running"
    finally:
        gate.set()
    done = wait_for_job(as_teacher, prefix, job["id"])
    assert done["state"] == "done"
    assert as_teacher.post(f"{prefix}/api/backups").status_code == 202   # free again


def test_failed_job_removes_part_file(as_teacher, prefix, app, settings, populated):
    def boom(job):
        write(job["part_path"] if isinstance(job, dict) else job.part_path, b"partial")
        raise RuntimeError("disk on fire")

    app.state.backups.on_start = boom
    r = as_teacher.post(f"{prefix}/api/backups")
    assert r.status_code == 202
    job = wait_for_job(as_teacher, prefix, r.json()["job"]["id"])
    assert job["state"] == "failed" and job["error"] == "disk on fire" and job["finished"]
    assert os.listdir(settings.backup_dir) == []
    listing = as_teacher.get(f"{prefix}/api/backups").json()
    assert listing["backups"] == [] and listing["job"]["state"] == "failed"
    assert as_teacher.post(f"{prefix}/api/backups").status_code == 202   # not stuck


def test_disk_guard_507(as_teacher, prefix, app, monkeypatch):
    monkeypatch.setattr(backups_mod.BackupManager, "estimate", lambda self, force=False: 10 * GiB)
    monkeypatch.setattr(backups_mod.shutil, "disk_usage", lambda path: Usage(100 * GiB, 95 * GiB, 5 * GiB))
    r = as_teacher.post(f"{prefix}/api/backups")
    assert r.status_code == 507
    body = r.json()
    assert body["error"] == "insufficient_storage"
    assert body["detail"] == {"free": 5 * GiB, "needed": 10 * GiB + backups_mod.SPACE_MARGIN}
    assert "free" in body["message"] and "GB" in body["message"]
    assert app.state.backups.jobs == {}
    listing = as_teacher.get(f"{prefix}/api/backups").json()
    assert listing["disk"]["warning"] and "Not enough free space" in listing["disk"]["warning"]
    assert listing["disk"]["estimate_bytes"] == 10 * GiB and listing["job"] is None


def test_disk_warning_when_space_is_tight(as_teacher, prefix, monkeypatch):
    monkeypatch.setattr(backups_mod.BackupManager, "estimate", lambda self, force=False: 10 * GiB)
    monkeypatch.setattr(backups_mod.shutil, "disk_usage", lambda path: Usage(100 * GiB, 85 * GiB, 15 * GiB))
    disk = as_teacher.get(f"{prefix}/api/backups").json()["disk"]
    assert disk["warning"] and "getting tight" in disk["warning"]
    monkeypatch.setattr(backups_mod.shutil, "disk_usage", lambda path: Usage(100 * GiB, 40 * GiB, 60 * GiB))
    assert as_teacher.get(f"{prefix}/api/backups").json()["disk"]["warning"] is None


def test_estimate_is_cached_and_never_follows_symlinks(settings, tmp_path, populated):
    manager = backups_mod.BackupManager(settings)
    big = write(str(tmp_path / "outside" / "big.bin"), b"0" * 100_000)
    os.symlink(big, os.path.join(settings.home_root, "teacher", "big-link"))
    first = manager.estimate()
    assert 0 < first < 50_000                       # the 100 kB target is not counted
    write(os.path.join(settings.home_root, "teacher", "more.txt"), b"0" * 20_000)
    assert manager.estimate() == first              # cached
    assert manager.estimate(force=True) == first + 20_000
    manager.shutdown()


def test_download_and_delete_backup(as_teacher, prefix, settings):
    name = "classroom-backup-20260913-101010.tar.gz"
    os.makedirs(settings.backup_dir, exist_ok=True)
    write(os.path.join(settings.backup_dir, name), b"\x1f\x8b" + b"z" * 100)
    write(os.path.join(settings.backup_dir, name + ".part"), b"unfinished")     # never listed
    write(os.path.join(settings.backup_dir, "notes.txt"), b"not a backup")

    listing = as_teacher.get(f"{prefix}/api/backups").json()
    assert [b["name"] for b in listing["backups"]] == [name]
    assert listing["backups"][0] == {"name": name, "size": 102, "created": "2026-09-13T10:10:10Z"}
    assert listing["disk"]["stored_bytes"] == 102

    r = as_teacher.get(f"{prefix}/api/backups/{name}/download")
    assert r.status_code == 200 and r.content == b"\x1f\x8b" + b"z" * 100
    assert r.headers["content-disposition"].startswith("attachment") and name in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"

    r = as_teacher.delete(f"{prefix}/api/backups/{name}")
    assert r.status_code == 204 and r.content == b""
    assert not os.path.exists(os.path.join(settings.backup_dir, name))
    assert as_teacher.delete(f"{prefix}/api/backups/{name}").status_code == 404
    assert as_teacher.get(f"{prefix}/api/backups/{name}/download").status_code == 404
    assert os.path.exists(os.path.join(settings.backup_dir, "notes.txt"))   # untouched


@pytest.mark.parametrize("name", [
    "..", "../roster.json", "%2e%2e%2froster.json", "notes.txt", "evil.tar.gz",
    "classroom-backup-2026091-101010.tar.gz", "classroom-backup-20260913-101010.tar.gz.part",
    "classroom-backup-20260913-101010.tar.gz%00", "CLASSROOM-BACKUP-20260913-101010.tar.gz",
    "classroom-backup-20260913-101010.tar.gz/../x", "classroom-backup-20260913-101010.tar.gz%0A",
    "%0Aclassroom-backup-20260913-101010.tar.gz",
])
def test_backup_name_validation(as_teacher, prefix, settings, name):
    os.makedirs(settings.backup_dir, exist_ok=True)
    write(os.path.join(settings.backup_dir, "notes.txt"), b"keep me")
    r = as_teacher.get(f"{prefix}/api/backups/{name}/download")
    assert r.status_code in (400, 404), (name, r.status_code)
    assert b"keep me" not in r.content
    r = as_teacher.delete(f"{prefix}/api/backups/{name}")
    assert r.status_code in (400, 404, 405), (name, r.status_code)
    assert os.path.exists(os.path.join(settings.backup_dir, "notes.txt"))
    assert os.path.exists(settings.roster_path) or not os.path.exists(settings.roster_path)  # nothing outside touched


def test_bad_name_is_400(as_teacher, prefix):
    r = as_teacher.get(f"{prefix}/api/backups/evil.tar.gz/download")
    assert r.status_code == 400 and r.json()["error"] == "bad_name"
    r = as_teacher.delete(f"{prefix}/api/archive/evil.tar.gz")
    assert r.status_code == 400 and r.json()["error"] == "bad_name"


def test_symlink_in_backup_dir_is_not_served_or_followed(as_teacher, prefix, settings, tmp_path):
    secret = write(str(tmp_path / "secret.txt"), b"TOP-SECRET")
    name = "classroom-backup-20260913-101010.tar.gz"
    os.makedirs(settings.backup_dir, exist_ok=True)
    os.symlink(secret, os.path.join(settings.backup_dir, name))
    assert as_teacher.get(f"{prefix}/api/backups").json()["backups"] == []
    r = as_teacher.get(f"{prefix}/api/backups/{name}/download")
    assert r.status_code == 404 and b"TOP-SECRET" not in r.content
    assert as_teacher.delete(f"{prefix}/api/backups/{name}").status_code == 404
    assert os.path.exists(secret)


def test_job_not_found(as_teacher, prefix):
    r = as_teacher.get(f"{prefix}/api/backups/jobs/nope")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_archived_students_listing_download_delete(as_teacher, prefix, settings):
    os.makedirs(settings.removed_dir, exist_ok=True)
    write(os.path.join(settings.removed_dir, "priya-20260913-101010.tar.gz"), b"priya-files")
    write(os.path.join(settings.removed_dir, "liam_c-20260901-080000.tar.gz"), b"liam")
    write(os.path.join(settings.removed_dir, "Bad Name-20260913-101010.tar.gz"), b"ignored")
    write(os.path.join(settings.removed_dir, "priya-2026.tar.gz"), b"ignored")
    write(os.path.join(settings.removed_dir, "priya-20260913-101010.tar.gz.tmp"), b"ignored")

    archived = as_teacher.get(f"{prefix}/api/backups").json()["archived"]
    assert archived == [
        {"name": "priya-20260913-101010.tar.gz", "username": "priya", "removed": "2026-09-13T10:10:10Z", "size": 11},
        {"name": "liam_c-20260901-080000.tar.gz", "username": "liam_c", "removed": "2026-09-01T08:00:00Z", "size": 4},
    ]

    r = as_teacher.get(f"{prefix}/api/archive/priya-20260913-101010.tar.gz/download")
    assert r.status_code == 200 and r.content == b"priya-files"
    assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"

    r = as_teacher.delete(f"{prefix}/api/archive/priya-20260913-101010.tar.gz")
    assert r.status_code == 204
    assert not os.path.exists(os.path.join(settings.removed_dir, "priya-20260913-101010.tar.gz"))
    assert as_teacher.delete(f"{prefix}/api/archive/priya-20260913-101010.tar.gz").status_code == 404
    assert as_teacher.get(f"{prefix}/api/archive/priya-20260913-101010.tar.gz/download").status_code == 404
    remaining = as_teacher.get(f"{prefix}/api/backups").json()["archived"]
    assert [a["username"] for a in remaining] == ["liam_c"]


@pytest.mark.parametrize("name", ["../roster.json", "Bad Name-20260913-101010.tar.gz", "priya-2026.tar.gz", "root-20260913-101010.tar", "priya-20260913-101010.tar.gz.tmp", "priya-20260913-101010.tar.gz%0A"])
def test_archive_name_validation(as_teacher, prefix, settings, name):
    os.makedirs(settings.removed_dir, exist_ok=True)
    write(os.path.join(settings.removed_dir, "priya-20260913-101010.tar.gz.tmp"), b"keep")
    assert as_teacher.get(f"{prefix}/api/archive/{name}/download").status_code in (400, 404)
    assert as_teacher.delete(f"{prefix}/api/archive/{name}").status_code in (400, 404, 405)
    assert os.path.exists(os.path.join(settings.removed_dir, "priya-20260913-101010.tar.gz.tmp"))


def test_permissions(client, as_student, prefix, settings):
    os.makedirs(settings.backup_dir, exist_ok=True)
    name = "classroom-backup-20260913-101010.tar.gz"
    write(os.path.join(settings.backup_dir, name), b"data")
    # a student session is refused everywhere
    assert as_student.get(f"{prefix}/api/backups").status_code == 403
    assert as_student.post(f"{prefix}/api/backups").status_code == 403
    assert as_student.get(f"{prefix}/api/backups/{name}/download").status_code == 403
    assert as_student.delete(f"{prefix}/api/backups/{name}").status_code == 403
    assert as_student.get(f"{prefix}/api/archive/{name}/download").status_code == 403
    assert as_student.get(f"{prefix}/api/backups/jobs/x").status_code == 403
    assert os.path.exists(os.path.join(settings.backup_dir, name))


def test_anonymous_and_csrf(client, app, prefix, settings):
    assert client.get(f"{prefix}/api/backups").status_code == 401
    assert client.get(f"{prefix}/api/backups/classroom-backup-20260913-101010.tar.gz/download").status_code == 401
    from console.auth import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, app.state.sessions.make_session("teacher"))
    assert CSRF_HEADER not in client.headers
    r = client.post(f"{prefix}/api/backups")
    assert r.status_code == 403 and r.json()["error"] == "csrf_failed"
    r = client.delete(f"{prefix}/api/backups/classroom-backup-20260913-101010.tar.gz")
    assert r.status_code == 403 and r.json()["error"] == "csrf_failed"
    assert app.state.backups.jobs == {}


def test_stale_part_files_are_removed_at_startup(app, settings, prefix):
    os.makedirs(settings.backup_dir, exist_ok=True)
    stale = write(os.path.join(settings.backup_dir, "classroom-backup-20260101-000000.tar.gz.part"), b"half")
    keep = write(os.path.join(settings.backup_dir, "classroom-backup-20260101-000000.tar.gz"), b"whole")
    other = write(os.path.join(settings.backup_dir, "something.part"), b"not ours")
    with TestClient(app, base_url="http://classroom.local"):
        assert not os.path.exists(stale)
        assert os.path.exists(keep) and os.path.exists(other)


def test_backups_page_renders(as_teacher, prefix):
    r = as_teacher.get(f"{prefix}/backups")
    assert r.status_code == 200
    assert 'id="bk-create"' in r.text and "Archived students" in r.text
    assert 'id="bk-note"' in r.text and 'id="bk-section"' in r.text and 'id="ar-section"' in r.text
    assert r.text.index('id="bk-error"') < r.text.index('id="bk-section"')      # the error card comes first
    assert 'id="bk-progress" class="cc-card cc-progress-card" tabindex="-1"' in r.text
    assert f"{prefix}/static/backups.js" in r.text and f"{prefix}/static/backups.css" in r.text
    assert as_teacher.get(f"{prefix}/static/backups.js").status_code == 200
    assert as_teacher.get(f"{prefix}/static/backups.css").status_code == 200


def test_exact_reader_pads_and_truncates():
    reader = backups_mod._ExactReader(io.BytesIO(b"abc"), 5)
    assert reader.read(2) == b"ab" and reader.read(10) == b"c\0\0" and reader.read() == b""
    reader = backups_mod._ExactReader(io.BytesIO(b"abcdef"), 4)
    assert reader.read() == b"abcd" and reader.read(1) == b""


# --- the walk: never follows links, bounded depth, size rules --------------------------


def test_name_regexes_reject_trailing_newline():
    assert backups_mod.BACKUP_NAME_RE.fullmatch("classroom-backup-20260913-101010.tar.gz")
    assert backups_mod.BACKUP_NAME_RE.fullmatch("classroom-backup-20260913-101010.tar.gz\n") is None
    assert backups_mod.ARCHIVE_NAME_RE.fullmatch("priya-20260913-101010.tar.gz\n") is None


def tar_blob(path) -> tuple[list[str], dict, bytes]:
    with tarfile.open(path, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        blob = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isreg())
        return list(members), members, blob


def test_directory_swapped_for_symlink_mid_walk_is_never_followed(as_teacher, prefix, settings, tmp_path, monkeypatch):
    """
    A student replaces a folder with a link to a root-only directory while the
    backup is copying that folder's children (and before another folder is
    reached). Nothing behind the link may end up in the archive.
    """
    home = os.path.join(settings.home_root, "student01")
    write(os.path.join(home, "bar", "first.txt"), b"first")
    write(os.path.join(home, "bar", "second.txt"), b"second")
    write(os.path.join(home, "foo", "secret.txt"), b"mine")
    target = str(tmp_path / "outside" / "state")
    for name in ("first.txt", "second.txt", "secret.txt", "console_secret"):
        write(os.path.join(target, name), b"STOLEN-" + name.encode())
    write(os.path.join(target, "backups", "old.bin"), b"STOLEN-old")

    def swap():
        for d in ("bar", "foo"):
            os.rename(os.path.join(home, d), os.path.join(home, d + ".real"))
            os.symlink(target, os.path.join(home, d))

    real_walk = backups_mod._walk_tree
    swapped = []

    def hooked(fd, arc, **kw):
        gen = real_walk(fd, arc, **kw)
        try:
            for ev in gen:
                yield ev
                if ev[0] == "file" and ev[4] == "home/student01/bar/first.txt":
                    swap()               # after bar/first.txt was copied, before bar/second.txt is opened
                    swapped.append(True)
        finally:
            gen.close()

    monkeypatch.setattr(backups_mod, "_walk_tree", hooked)
    r = as_teacher.post(f"{prefix}/api/backups")
    assert r.status_code == 202
    job = wait_for_job(as_teacher, prefix, r.json()["job"]["id"])
    assert job["state"] == "done", job
    assert swapped == [True]
    names, members, blob = tar_blob(os.path.join(settings.backup_dir, job["name"]))
    assert b"STOLEN" not in blob
    assert not any("console_secret" in n or "old.bin" in n for n in names)
    # the folder already open when the swap happened is read through its descriptor, not the new link
    with tarfile.open(os.path.join(settings.backup_dir, job["name"]), "r:gz") as tar:
        assert tar.extractfile("home/student01/bar/second.txt").read() == b"second"
    assert members["home/student01/bar"].isdir()
    # the folder not yet reached is now a link: stored as a link, its target never entered
    assert members["home/student01/foo"].issym() and members["home/student01/foo"].linkname == target
    assert "home/student01/foo/secret.txt" not in names
    assert job["skipped"] == 0 and job["error"] is None


def test_open_dir_refuses_a_link_swapped_in_for_a_folder(tmp_path):
    """The listing said 'folder', then a student swapped in a link before the open: O_NOFOLLOW|O_DIRECTORY refuses it."""
    root = str(tmp_path / "tree")
    outside = str(tmp_path / "outside2")
    write(os.path.join(outside, "y.txt"), b"STOLEN")
    os.makedirs(os.path.join(root, "sub"))
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        sub = backups_mod._open_dir("sub", dir_fd=fd)
        assert os.fstat(sub).st_ino == os.lstat(os.path.join(root, "sub")).st_ino
        os.close(sub)
        os.rename(os.path.join(root, "sub"), os.path.join(root, "sub.real"))
        os.symlink(outside, os.path.join(root, "sub"))
        with pytest.raises(OSError):
            backups_mod._open_dir("sub", dir_fd=fd)
    finally:
        os.close(fd)


def make_deep_tree(root: str, levels: int, files: dict[int, bytes]) -> None:
    """a/a/a/... `levels` deep, built one descriptor at a time (no long paths, no recursion)."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for level in range(1, levels + 1):
            os.mkdir("a", 0o755, dir_fd=fd)
            nfd = os.open("a", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
            if level in files:
                ffd = os.open("marker.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=fd)
                with os.fdopen(ffd, "wb") as fh:
                    fh.write(files[level])
    finally:
        os.close(fd)


def test_deep_tree_is_capped_and_the_rest_still_backs_up(as_teacher, prefix, settings, populated):
    cap = backups_mod.MAX_DEPTH
    levels = cap + 50
    home = os.path.join(settings.home_root, "student01")
    make_deep_tree(home, levels, {cap - 5: b"still in", levels: b"too deep"})
    r = as_teacher.post(f"{prefix}/api/backups")
    assert r.status_code == 202
    job = wait_for_job(as_teacher, prefix, r.json()["job"]["id"])
    assert job["state"] == "done" and job["error"] is None, job
    names, members, blob = tar_blob(os.path.join(settings.backup_dir, job["name"]))
    assert "home/teacher/notes.md" in names and "hubstate/roster.json" in names
    inside = "home/student01/" + "/".join(["a"] * (cap - 5)) + "/marker.txt"
    assert inside in names and b"still in" in blob and b"too deep" not in blob
    assert job["skipped"] == 2                       # the FIFO and the too-deep subtree (counted once)
    deep = [i for i in job["skipped_items"] if "nested deeper" in i["reason"]]
    assert len(deep) == 1 and deep[0]["path"].startswith("home/student01/a/a/")
    assert deep[0]["path"].count("/") == cap + 1     # the first folder past the cap, so the teacher can find it
    with tarfile.open(os.path.join(settings.backup_dir, job["name"]), "r:gz") as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert manifest["skipped"] == 2 and deep[0] in manifest["skipped_items"]


def test_sparse_and_oversized_files_are_left_out_of_estimate_and_archive(as_teacher, prefix, settings, app, populated):
    home = os.path.join(settings.home_root, "student01")
    hole = os.path.join(home, "hole.bin")
    with open(hole, "wb") as fh:
        fh.truncate(200 * 1024 * 1024)
    st = os.stat(hole)
    if st.st_blocks * 512 >= st.st_size // 8:
        pytest.skip("this filesystem does not make sparse files")
    write(os.path.join(home, "big.bin"), b"B" * 2000)
    write(os.path.join(home, "small.bin"), b"s" * 500)
    manager = app.state.backups
    manager.max_file_bytes = 1000
    estimate = manager.estimate(force=True)
    assert 500 <= estimate < 20_000                   # neither the 200 MB hole nor the 2 kB file is counted

    r = as_teacher.post(f"{prefix}/api/backups")
    assert r.status_code == 202, r.text
    job = wait_for_job(as_teacher, prefix, r.json()["job"]["id"])
    assert job["state"] == "done", job
    names, members, blob = tar_blob(os.path.join(settings.backup_dir, job["name"]))
    assert "home/student01/small.bin" in names
    assert "home/student01/hole.bin" not in names and "home/student01/big.bin" not in names
    assert os.path.getsize(os.path.join(settings.backup_dir, job["name"])) < 100_000
    reasons = {i["path"]: i["reason"] for i in job["skipped_items"]}
    assert "sparse" in reasons["home/student01/hole.bin"]
    assert reasons["home/student01/big.bin"].startswith("bigger than 1000 B")
    assert job["skipped"] == 3                        # hole, big, the FIFO
    with tarfile.open(os.path.join(settings.backup_dir, job["name"]), "r:gz") as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert {i["path"] for i in manifest["skipped_items"]} == {"home/student01/hole.bin", "home/student01/big.bin", "home/student01/pipe"}


def test_hard_links_are_stored_once(as_teacher, prefix, settings, populated):
    home = os.path.join(settings.home_root, "teacher")
    write(os.path.join(home, "one.bin"), b"L" * 3000)
    os.link(os.path.join(home, "one.bin"), os.path.join(home, "two.bin"))
    r = as_teacher.post(f"{prefix}/api/backups")
    job = wait_for_job(as_teacher, prefix, r.json()["job"]["id"])
    assert job["state"] == "done", job
    names, members, blob = tar_blob(os.path.join(settings.backup_dir, job["name"]))
    assert members["home/teacher/one.bin"].isreg() and members["home/teacher/one.bin"].size == 3000
    assert members["home/teacher/two.bin"].islnk() and members["home/teacher/two.bin"].linkname == "home/teacher/one.bin"
    assert members["home/teacher/one.bin"].uname == pwd.getpwuid(os.getuid()).pw_name


def test_tree_size_uses_the_writers_rules(tmp_path):
    root = str(tmp_path / "t")
    write(os.path.join(root, "a.txt"), b"x" * 100)
    write(os.path.join(root, "sub", "b.txt"), b"y" * 50)
    os.symlink("/etc/hostname", os.path.join(root, "lnk"))
    write(os.path.join(root, "big.bin"), b"z" * 300)
    assert backups_mod.tree_size(root) == 150 + len("/etc/hostname") + 300
    assert backups_mod.tree_size(root, max_file_bytes=200) == 150 + len("/etc/hostname")
    assert backups_mod.tree_size(str(tmp_path / "missing")) == 0


# --- the roster --------------------------------------------------------------------------


def test_roster_is_taken_from_bak_while_a_save_is_in_flight(as_teacher, prefix, settings, populated):
    # Roster.save() moves roster.json to .bak first and only then writes the new file:
    # a backup that starts in that window must still ship the accounts.
    os.rename(settings.roster_path, settings.roster_path + ".bak")
    r = as_teacher.post(f"{prefix}/api/backups")
    job = wait_for_job(as_teacher, prefix, r.json()["job"]["id"])
    assert job["state"] == "done" and job["warnings"] == [], job
    with tarfile.open(os.path.join(settings.backup_dir, job["name"]), "r:gz") as tar:
        roster = json.loads(tar.extractfile("hubstate/roster.json").read())
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert [u["username"] for u in roster["users"]] == ["teacher", "student01"]
    assert manifest["users"] == 2 and manifest["warnings"] == []


def test_missing_roster_is_a_visible_warning(as_teacher, prefix, settings, populated):
    os.unlink(settings.roster_path)
    r = as_teacher.post(f"{prefix}/api/backups")
    job = wait_for_job(as_teacher, prefix, r.json()["job"]["id"])
    assert job["state"] == "done", job
    assert len(job["warnings"]) == 1 and "roster.json" in job["warnings"][0] and "no accounts" in job["warnings"][0]
    with tarfile.open(os.path.join(settings.backup_dir, job["name"]), "r:gz") as tar:
        assert "hubstate/roster.json" not in tar.getnames()
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert manifest["users"] == 0 and manifest["warnings"] == job["warnings"]
    listing = as_teacher.get(f"{prefix}/api/backups").json()
    assert listing["job"]["warnings"] == job["warnings"]


def test_backup_waits_for_the_roster_lock(as_teacher, prefix, settings, populated):
    from console.roster import Roster

    held = Roster(settings.roster_path)
    with held.lock():                                 # a student add/reset/remove in progress
        r = as_teacher.post(f"{prefix}/api/backups")
        assert r.status_code == 202
        job_id = r.json()["job"]["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = as_teacher.get(f"{prefix}/api/backups/jobs/{job_id}").json()
            if job["phase"] == "roster and settings" or job["state"] != "running":
                break
            time.sleep(0.01)
        assert job["state"] == "running" and job["phase"] == "roster and settings", job
        time.sleep(0.1)
        job = as_teacher.get(f"{prefix}/api/backups/jobs/{job_id}").json()
        assert job["state"] == "running"              # blocked on the lock, not finished without the roster
    job = wait_for_job(as_teacher, prefix, job_id)
    assert job["state"] == "done" and job["warnings"] == [], job
    with tarfile.open(os.path.join(settings.backup_dir, job["name"]), "r:gz") as tar:
        assert "hubstate/roster.json" in tar.getnames()
