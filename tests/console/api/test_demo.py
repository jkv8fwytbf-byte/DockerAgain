"""Docker-free complete demo: seeded console, sign-in, student workspace."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from console.auth import CSRF_HEADER, SESSION_COOKIE
from demo.classroom import HANDOUTS
from demo.server import build_demo

HANDOUT_FILES = [
    "01-python-and-data.ipynb",
    "02-machine-learning.ipynb",
    "03-computer-vision.ipynb",
    "04-radio-signals.ipynb",
    "class-scores.csv",
    "week-1-homework.md",
]


@pytest.fixture
def demo(tmp_path):
    app, settings, hub, accounts = build_demo(tmp_path / "run", reset=True)
    return app, settings, hub, accounts


@pytest.fixture
def client(demo):
    app, settings, hub, accounts = demo
    with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as c:
        yield c, settings, hub, accounts


def test_handouts_exist_in_the_repo():
    for name in HANDOUT_FILES:
        path = HANDOUTS / name
        assert path.is_file(), path
        assert path.stat().st_size > 20


def test_landing_and_console_pages(client):
    c, settings, hub, accounts = client
    landing = c.get("/")
    assert landing.status_code == 200
    assert "Teacher console" in landing.text
    assert "Priya Sharma" in landing.text

    dash = c.get("/services/console/")
    assert dash.status_code == 200
    assert "Dashboard" in dash.text
    assert dash.cookies.get(SESSION_COOKIE)

    for path, needle in (
        ("/students", "Add student"),
        ("/files", "Handouts"),
        ("/backups", "Create backup now"),
        ("/logs", "JupyterHub log"),
        ("/settings", "School name"),
        ("/print/cards", "Login cards"),
    ):
        r = c.get(f"/services/console{path}")
        assert r.status_code == 200, path
        assert needle in r.text, path


def test_seeded_class_and_handouts(client):
    c, settings, hub, accounts = client
    r = c.get("/services/console/api/students")
    assert r.status_code == 200
    body = r.json()
    by = {s["username"]: s for s in body["students"]}
    assert set(by) >= {"teacher", "priya", "liam", "ana", "jordan", "sam", "nora"}
    assert by["priya"]["display_name"] == "Priya Sharma"
    assert by["priya"]["state"] == "running"
    assert by["ana"]["state"] == "stopped"
    assert by["teacher"]["is_admin"] is True

    shared = Path(settings.shared_dir)
    for name in HANDOUT_FILES:
        assert (shared / name).is_file(), name
    assert not (shared / "build.py").exists()
    assert (Path(settings.home_root) / "priya" / "submit" / "hello-from-priya.txt").is_file()
    assert (Path(settings.home_root) / "priya" / "Welcome.ipynb").is_file()
    assert list(Path(settings.removed_dir).glob("maya-*.tar.gz"))


def test_dashboard_status_shows_online_students(client):
    c, settings, hub, accounts = client
    r = c.get("/services/console/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["hub"]["ok"] is True
    assert body["students"]["online"] >= 4
    assert body["host"]["ncpu"] == 8
    priya = next(s for s in body["servers"] if s["username"] == "priya")
    assert priya["state"] == "running" and priya["mem_bytes"] > 0


def test_sign_in_as_student_and_as_teacher(client):
    c, settings, hub, accounts = client
    bad = c.post("/hub/login", data={"username": "priya", "password": "nope"})
    assert bad.status_code == 401
    assert "isn't right" in bad.text or "Invalid username" in bad.text

    ok = c.post("/hub/login", data={"username": "priya", "password": "coral-otter-12"})
    assert ok.status_code == 303
    assert ok.headers["location"] == "/user/priya/lab"

    c.cookies.set("demo-student", "priya")
    lab = c.get("/user/priya/lab")
    assert lab.status_code == 200
    assert "Priya Sharma" in lab.text
    assert "Welcome.ipynb" in lab.text
    shared = c.get("/user/priya/lab/tree/shared")
    assert shared.status_code == 200
    assert "01-python-and-data.ipynb" in shared.text

    teacher = c.post("/hub/login", data={"username": "teacher", "password": "change-me-teacher"})
    assert teacher.status_code == 303
    assert teacher.headers["location"] == "/services/console/"


def test_add_student_start_stop_and_backup(client):
    c, settings, hub, accounts = client
    headers = {CSRF_HEADER: "1"}
    added = c.post(
        "/services/console/api/students",
        json={"display_name": "Mei Zhang", "username": "mei", "password": "sunny-lotus-09"},
        headers=headers,
    )
    assert added.status_code == 201, added.text
    assert added.json()["student"]["username"] == "mei"
    assert "mei" in hub.users

    stop = c.post("/services/console/api/servers/priya/stop", headers=headers)
    assert stop.status_code == 200
    assert hub.users["priya"]["server"] is None

    start = c.post("/services/console/api/servers/ana/start", headers=headers)
    assert start.status_code == 200
    assert hub.users["ana"]["server"]

    files = c.get("/services/console/api/handouts")
    assert files.status_code == 200
    assert "01-python-and-data.ipynb" in str(files.json())

    subs = c.get("/services/console/api/submissions")
    assert subs.status_code == 200
    by = {row["username"]: row for row in subs.json()}
    assert by["priya"]["exists"] is True
    assert by["jordan"]["exists"] is True
    assert (Path(settings.home_root) / "priya" / "submit" / "hello-from-priya.txt").is_file()

    logs = c.get("/services/console/api/logs?source=hub&limit=50")
    assert logs.status_code == 200
    assert "priya" in logs.text

    backup = c.post("/services/console/api/backups", headers=headers)
    assert backup.status_code == 202, backup.text
    job_id = backup.json()["job"]["id"]
    job = backup.json()["job"]
    deadline = time.monotonic() + 8
    while job["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        job = c.get(f"/services/console/api/backups/jobs/{job_id}").json()
    assert job["state"] == "done", job
    listing = c.get("/services/console/api/backups")
    assert listing.status_code == 200
    body = listing.json()
    assert body["backups"]
    assert any("maya-" in a["name"] for a in body["archived"])


def test_branded_login_uses_lincoln_high(client):
    c, settings, hub, accounts = client
    page = c.get("/hub/login")
    assert page.status_code == 200
    assert "Lincoln High School" in page.text
    assert "Grade 10" in page.text
    assert "Homework 3 is due Friday" in page.text
