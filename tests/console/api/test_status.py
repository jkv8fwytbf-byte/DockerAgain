"""
Dashboard status API and server controls. The psutil sampler is replaced by a
fake snapshot so nothing here depends on real process timing.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from console import status as status_mod
from console.errors import HubError
from console.status import Sampler, parse_iso, server_state

NOW = 1_789_000_000.0


def make_snapshot(users=None):
    """Snapshot with two Linux users unless overridden: 2001 (busy) and 2000 (teacher, idle)."""
    if not users:
        users = {
            2001: {"cpu_sum": 120.0, "rss_bytes": 600_000_000, "nproc": 3, "has_server": True},
            2000: {"cpu_sum": 10.0, "rss_bytes": 100_000_000, "nproc": 1, "has_server": False},
        }
    names = {2001: "student01", 2000: "teacher", 2002: "student02"}
    return {
        "sampled_at": "2026-09-13T08:00:05Z",
        "ts": NOW,
        "host": {
            "cpu_pct": 12.5, "ncpu": 4, "load1": 0.5, "mem_total": 16_000_000_000, "mem_used": 9_000_000_000,
            "mem_available": 7_000_000_000, "swap_used": 0, "boot_time": NOW - 11_520, "uptime_s": 11_520,
            "disks": {
                "home": {"total": 120_000_000_000, "used": 79_000_000_000, "free": 41_000_000_000},
                "state": {"total": 120_000_000_000, "used": 79_000_000_000, "free": 40_000_000_000},
                "shared": None,
            },
            "disk_path": "/home",
        },
        "users": users,
        "names": {uid: names.get(uid) for uid in users},
    }


class FakeSampler:
    def __init__(self, snapshot):
        self.snap = snapshot
        self.stopped = False

    def snapshot(self):
        return self.snap

    def stop(self):
        self.stopped = True


@pytest.fixture
def sampler(app):
    """Installed before the lifespan runs, so setup() never starts a real thread."""
    fs = FakeSampler(make_snapshot())
    app.state.sampler = fs
    return fs


@pytest.fixture
def roster(settings):
    data = {"version": 1, "users": [
        {"username": "student01", "password": "pw-1", "display_name": "Priya Sharma", "created": ""},
        {"username": "student02", "password": "pw-2", "display_name": "Omar Haddad", "created": ""},
        {"username": "teacher", "password": "pw-t", "display_name": "", "created": ""},
    ]}
    os.makedirs(settings.state_dir, exist_ok=True)
    with open(settings.roster_path, "w") as fh:
        json.dump(data, fh)
    return data


# --- GET /api/status ----------------------------------------------------------------


def test_status_shape_and_join(sampler, roster, hub, fake_system, as_teacher, prefix):
    hub.add("student02", running=True)
    hub.add("jovyan")
    fake_system.add_user("student02", 2002)
    sampler.snap = make_snapshot({
        2001: {"cpu_sum": 120.0, "rss_bytes": 600_000_000, "nproc": 3, "has_server": True},
        2002: {"cpu_sum": 480.0, "rss_bytes": 1_000, "nproc": 2, "has_server": True},
    })
    r = as_teacher.get(f"{prefix}/api/status")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    d = r.json()
    assert set(d) == {"sampled_at", "host", "hub", "students", "servers"}
    assert d["sampled_at"] == "2026-09-13T08:00:05Z"
    assert set(d["host"]) == set(status_mod.HOST_KEYS)
    assert d["host"]["cpu_pct"] == 12.5 and d["host"]["ncpu"] == 4 and d["host"]["load1"] == 0.5
    assert d["host"]["disk_free"] == 41_000_000_000 and d["host"]["disk_total"] == 120_000_000_000
    assert d["host"]["disk_path"] == "/home" and d["host"]["disk_state_free"] == 40_000_000_000
    assert d["host"]["disk_shared_free"] is None
    assert d["host"]["uptime_s"] >= 11_520
    assert d["hub"] == {"ok": True, "error": None, "jupyterhub": "5.3.0", "image_version": "dev"}
    assert d["students"] == {"online": 1, "total": 2}

    by_name = {s["username"]: s for s in d["servers"]}
    assert set(by_name) == {"teacher", "student01", "student02"}          # jovyan skipped
    s1, s2, t = by_name["student01"], by_name["student02"], by_name["teacher"]
    assert s1["display_name"] == "Priya Sharma" and s1["state"] == "stopped" and s1["is_admin"] is False
    assert s1["cpu_pct"] == 30.0 and s1["mem_bytes"] == 600_000_000 and s1["nproc"] == 3 and s1["has_server_process"] is True
    assert s1["url"] is None and s1["last_activity"] is None and s1["idle_seconds"] is None
    assert s2["state"] == "running" and s2["cpu_pct"] == 100.0 and s2["url"] == "/user/student02/"   # capped at 100
    assert t["is_admin"] is True and t["state"] == "stopped" and t["display_name"] == ""
    assert t["cpu_pct"] == 0.0 and t["nproc"] == 0                          # 2000 not in this snapshot
    for s in d["servers"]:
        assert set(s) == {"username", "display_name", "is_admin", "state", "last_activity", "idle_seconds",
                          "cpu_pct", "mem_bytes", "nproc", "has_server_process", "url", "error"}


def test_status_uid_falls_back_to_snapshot_names(sampler, roster, hub, as_teacher, prefix):
    """student02 is unknown to the (fake) passwd table but the sampler saw uid 2002 -> student02."""
    hub.add("student02")
    sampler.snap = make_snapshot({2002: {"cpu_sum": 40.0, "rss_bytes": 5_000, "nproc": 1, "has_server": False}})
    d = as_teacher.get(f"{prefix}/api/status").json()
    s2 = next(s for s in d["servers"] if s["username"] == "student02")
    assert s2["cpu_pct"] == 10.0 and s2["mem_bytes"] == 5_000


def test_status_state_mapping_and_activity(sampler, hub, as_teacher, prefix):
    hub.add("student02", running=True)
    hub.add("student03")
    hub.add("student04", running=True)
    hub.users["student02"]["servers"][""]["pending"] = "spawn"
    hub.users["student03"]["servers"][""]["pending"] = "stop"
    hub.users["student04"]["servers"][""]["last_activity"] = "2026-09-12T08:00:00.123456Z"
    d = as_teacher.get(f"{prefix}/api/status").json()
    states = {s["username"]: s["state"] for s in d["servers"]}
    assert states == {"teacher": "stopped", "student01": "stopped", "student02": "starting", "student03": "stopping", "student04": "running"}
    s4 = next(s for s in d["servers"] if s["username"] == "student04")
    assert s4["last_activity"] == "2026-09-12T08:00:00.123456Z" and s4["idle_seconds"] > 0
    assert d["students"]["online"] == 1 and d["students"]["total"] == 4


def test_server_state_helper():
    assert server_state({"servers": {"": {"ready": False, "pending": "spawn"}}}) == "starting"
    assert server_state({"servers": {"": {"ready": True, "pending": "stop"}}}) == "stopping"
    assert server_state({"servers": {"": {"ready": True, "pending": None}}}) == "running"
    assert server_state({"servers": {"": {"ready": False, "pending": None, "stopped": True}}}) == "stopped"
    assert server_state({"servers": {}, "server": "/user/x/", "pending": None}) == "running"   # old-style model
    assert server_state({"server": None, "pending": "spawn"}) == "starting"
    assert server_state({}) == "stopped"
    from datetime import datetime, timezone
    assert parse_iso("2026-09-13T08:00:00Z") == datetime(2026, 9, 13, 8, tzinfo=timezone.utc).timestamp()
    assert parse_iso("2026-09-13T08:00:00+00:00") == parse_iso("2026-09-13T08:00:00")
    assert parse_iso("nonsense") is None and parse_iso(None) is None


def test_status_hub_down_is_200_from_roster(sampler, roster, hub, as_teacher, prefix):
    as_teacher.get(f"{prefix}/api/whoami")          # warm the admin cache so the request is authorised offline
    hub.down = True
    status_mod.invalidate(as_teacher.app)
    r = as_teacher.get(f"{prefix}/api/status")
    assert r.status_code == 200
    d = r.json()
    assert d["hub"]["ok"] is False and "not answering" in d["hub"]["error"]
    assert d["hub"]["jupyterhub"] is None
    by_name = {s["username"]: s for s in d["servers"]}
    assert set(by_name) == {"student01", "student02", "teacher"}
    assert all(s["state"] == "unknown" for s in by_name.values())
    assert by_name["teacher"]["is_admin"] is True and by_name["student01"]["is_admin"] is False
    assert by_name["student01"]["display_name"] == "Priya Sharma"
    assert by_name["student01"]["cpu_pct"] == 30.0                    # usage still joined via the snapshot
    assert d["students"] == {"online": 0, "total": 2}
    assert d["host"]["cpu_pct"] == 12.5                               # host tiles keep working


def test_status_hub_error_is_200(sampler, hub, as_teacher, prefix, monkeypatch):
    async def boom(**kw):
        raise HubError(500, "database locked")
    monkeypatch.setattr(as_teacher.app.state.hub, "list_users", boom)
    d = as_teacher.get(f"{prefix}/api/status").json()
    assert d["hub"]["ok"] is False and "500" in d["hub"]["error"]


def test_status_without_snapshot_yet(sampler, hub, as_teacher, prefix):
    sampler.snap = None
    d = as_teacher.get(f"{prefix}/api/status").json()
    assert d["sampled_at"] is None
    assert d["host"]["cpu_pct"] is None and d["host"]["mem_total"] is None
    assert d["host"]["disk_path"] == as_teacher.app.state.settings.home_root
    s1 = next(s for s in d["servers"] if s["username"] == "student01")
    assert s1["cpu_pct"] == 0.0 and s1["mem_bytes"] == 0 and s1["state"] == "stopped"


def test_hub_user_list_is_cached_briefly(sampler, hub, as_teacher, prefix):
    as_teacher.get(f"{prefix}/api/status")
    as_teacher.get(f"{prefix}/api/status")
    assert hub.calls.count(("GET", "/hub/api/users")) == 1
    assert hub.calls.count(("GET", "/hub/api/info")) == 1
    status_mod.invalidate(as_teacher.app)
    as_teacher.get(f"{prefix}/api/status")
    assert hub.calls.count(("GET", "/hub/api/users")) == 2
    assert hub.calls.count(("GET", "/hub/api/info")) == 1          # version cached much longer


def test_status_version_lookup_is_best_effort(sampler, hub, as_teacher, prefix, monkeypatch):
    async def denied():
        raise HubError(403, "read:hub scope missing")
    monkeypatch.setattr(as_teacher.app.state.hub, "info", denied)
    d = as_teacher.get(f"{prefix}/api/status").json()
    assert d["hub"]["ok"] is True and d["hub"]["jupyterhub"] is None


def test_setup_keeps_injected_sampler_and_teardown_stops_it(settings, hub, fake_system):
    import httpx
    from fastapi.testclient import TestClient

    from console.accounts import AccountSettings, Accounts
    from console.app import create_app
    from console.hub_api import HubClient

    hub_client = HubClient(settings.hub_api_url, "service-token-xyz", transport=httpx.MockTransport(hub.handler))
    app = create_app(settings, hub_client=hub_client, accounts=Accounts(AccountSettings(), fake_system), secret=b"x" * 32)
    fs = FakeSampler(make_snapshot())
    app.state.sampler = fs
    with TestClient(app):
        assert app.state.sampler is fs
    assert fs.stopped is True


def test_setup_starts_a_real_sampler_by_default(app, client):
    sampler = app.state.sampler
    assert isinstance(sampler, Sampler) and sampler.running
    assert sampler.interval == app.state.settings.stats_interval
    assert sampler.paths["home"] == app.state.settings.home_root


# --- the real sampler (one tick, no timing assumptions) ---------------------------------


def test_sampler_tick_shape(tmp_path):
    s = Sampler(0.5, str(tmp_path), str(tmp_path), "/definitely/not/here")
    snap = s.sample()
    host = snap["host"]
    assert snap["sampled_at"].endswith("Z") and snap["ts"] <= time.time()
    assert host["ncpu"] >= 1 and host["mem_total"] > 0 and host["mem_used"] >= 0
    assert 0 <= host["cpu_pct"] <= 100
    assert host["disks"]["home"]["total"] > 0 and host["disks"]["shared"] is None
    assert host["disk_path"] == str(tmp_path)
    me = os.getuid()
    assert me in snap["users"], "the test process itself must be counted"
    mine = snap["users"][me]
    assert mine["nproc"] >= 1 and mine["rss_bytes"] > 0 and mine["cpu_sum"] >= 0 and mine["has_server"] is False
    assert all(isinstance(uid, int) for uid in snap["users"])
    assert snap["names"][me]
    # a second tick reuses Process objects (so cpu_percent has a baseline) and prunes gone pids
    before = dict(s._procs)
    snap2 = s.sample()
    assert me in snap2["users"]
    assert set(s._procs) & set(before), "process objects are kept across ticks"
    s.override = {"sampled_at": "x", "host": {}, "users": {}, "names": {}}
    assert s.snapshot()["sampled_at"] == "x"


def test_sampler_thread_starts_stops_and_survives_errors(tmp_path, monkeypatch):
    s = Sampler(0.5, str(tmp_path), str(tmp_path), str(tmp_path))
    calls = {"n": 0}
    real = s.sample

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("psutil hiccup")
        return real()

    monkeypatch.setattr(s, "sample", flaky)
    s.start()
    deadline = time.time() + 3
    while time.time() < deadline and s.ticks < 2:
        time.sleep(0.02)
    assert s.ticks >= 2 and s.running
    assert s.snapshot() is not None and s.last_error is None
    s.stop()
    assert not s.running
    s.start()                       # restartable
    assert s.running
    s.stop()


# --- server controls ---------------------------------------------------------------------


def test_start_and_stop_server(sampler, hub, as_teacher, prefix):
    r = as_teacher.post(f"{prefix}/api/servers/student01/start")
    assert r.status_code == 200 and r.json() == {"username": "student01", "state": "running"}
    assert r.headers["cache-control"] == "no-store"
    assert hub.users["student01"]["server"] == "/user/student01/"
    assert ("POST", "/hub/api/users/student01/server") in hub.calls
    # already running -> the Hub's 400 is mapped, not surfaced
    r = as_teacher.post(f"{prefix}/api/servers/Student01/start")
    assert r.status_code == 200 and r.json()["state"] == "running"
    # the next status poll is fresh, not the 2 s cache
    d = as_teacher.get(f"{prefix}/api/status").json()
    assert next(s for s in d["servers"] if s["username"] == "student01")["state"] == "running"

    r = as_teacher.post(f"{prefix}/api/servers/student01/stop")
    assert r.status_code == 200 and r.json() == {"username": "student01", "state": "stopped"}
    assert hub.users["student01"]["server"] is None
    r = as_teacher.post(f"{prefix}/api/servers/student01/stop")
    assert r.status_code == 200 and r.json()["state"] == "stopped"
    d = as_teacher.get(f"{prefix}/api/status").json()
    assert next(s for s in d["servers"] if s["username"] == "student01")["state"] == "stopped"


def test_start_unknown_user_is_404(sampler, hub, as_teacher, prefix):
    r = as_teacher.post(f"{prefix}/api/servers/ghost/start")
    assert r.status_code == 404 and r.json()["error"] == "unknown_user"
    r = as_teacher.post(f"{prefix}/api/servers/ghost/stop")
    assert r.status_code == 404 and r.json()["error"] == "unknown_user"


@pytest.mark.parametrize("bad", ["root", "jovyan", "Bad%20Name", "a%2Fb", "x" * 40, "1abc", "..", "%00"])
def test_invalid_usernames_never_reach_the_hub(sampler, hub, as_teacher, prefix, bad):
    before = len(hub.calls)
    for verb in ("start", "stop"):
        r = as_teacher.post(f"{prefix}/api/servers/{bad}/{verb}")
        assert r.status_code in (400, 404), (bad, verb, r.status_code)
        if r.status_code == 400:
            assert r.json()["error"] == "invalid_username"
    assert not [c for c in hub.calls[before:] if "/server" in c[1]]


def test_pending_server_is_409(sampler, hub, as_teacher, prefix, monkeypatch):
    async def pending(name):
        raise HubError(400, f"{name} is pending spawn, please wait")
    monkeypatch.setattr(as_teacher.app.state.hub, "stop_server", pending)
    r = as_teacher.post(f"{prefix}/api/servers/student01/stop")
    assert r.status_code == 409 and r.json()["error"] == "server_busy"
    assert "wait" in r.json()["message"].lower()


def test_hub_down_on_start_is_503(sampler, hub, as_teacher, prefix):
    as_teacher.get(f"{prefix}/api/whoami")
    hub.down = True
    r = as_teacher.post(f"{prefix}/api/servers/student01/start")
    assert r.status_code == 503 and r.json()["error"] == "hub_unreachable"


def test_stop_all(sampler, hub, as_teacher, prefix):
    hub.add("student02", running=True)
    hub.add("student03", running=True)
    hub.add("student04")                                        # nothing to stop
    hub.add("jovyan", running=True)                             # never touched
    hub.users["teacher"]["server"] = "/user/teacher/"; hub.users["teacher"]["servers"][""].update(ready=True, stopped=False)
    r = as_teacher.post(f"{prefix}/api/servers/stop-all", json={})
    assert r.status_code == 200
    assert r.json() == {"stopped": ["student02", "student03"], "failed": []}
    assert hub.users["student02"]["server"] is None and hub.users["student03"]["server"] is None
    assert hub.users["teacher"]["server"] and hub.users["jovyan"]["server"]
    assert ("DELETE", "/hub/api/users/student04/server") not in hub.calls
    # include admins on request; no body at all is fine too
    r = as_teacher.post(f"{prefix}/api/servers/stop-all", json={"include_admins": True})
    assert r.status_code == 200 and r.json()["stopped"] == ["teacher"]
    assert hub.users["teacher"]["server"] is None
    r = as_teacher.post(f"{prefix}/api/servers/stop-all")
    assert r.status_code == 200 and r.json() == {"stopped": [], "failed": []}


def test_stop_all_reports_failures(sampler, hub, as_teacher, prefix, monkeypatch):
    hub.add("student02", running=True)
    hub.add("student03", running=True)
    real = as_teacher.app.state.hub.stop_server

    async def flaky(name):
        if name == "student03":
            raise HubError(500, "spawner exploded")
        return await real(name)

    monkeypatch.setattr(as_teacher.app.state.hub, "stop_server", flaky)
    r = as_teacher.post(f"{prefix}/api/servers/stop-all", json={"include_admins": False})
    assert r.status_code == 200
    d = r.json()
    assert d["stopped"] == ["student02"]
    assert d["failed"] == [{"username": "student03", "error": d["failed"][0]["error"]}]
    assert "500" in d["failed"][0]["error"]


def test_stop_all_hub_down_is_503(sampler, hub, as_teacher, prefix):
    as_teacher.get(f"{prefix}/api/whoami")
    hub.down = True
    r = as_teacher.post(f"{prefix}/api/servers/stop-all", json={})
    assert r.status_code == 503 and r.json()["error"] == "hub_unreachable"


def test_stop_all_rejects_bad_body(sampler, hub, as_teacher, prefix):
    r = as_teacher.post(f"{prefix}/api/servers/stop-all", json={"include_admins": "maybe"})
    assert r.status_code == 400 and r.json()["error"] == "validation_failed"


# --- permissions ---------------------------------------------------------------------------


def test_student_is_forbidden(sampler, as_student, prefix):
    assert as_student.get(f"{prefix}/api/status").status_code == 403
    assert as_student.post(f"{prefix}/api/servers/student01/start").status_code == 403
    assert as_student.post(f"{prefix}/api/servers/student01/stop").status_code == 403
    assert as_student.post(f"{prefix}/api/servers/stop-all", json={}).status_code == 403


def test_anonymous_is_401(sampler, client, prefix):
    assert client.get(f"{prefix}/api/status").status_code == 401
    r = client.post(f"{prefix}/api/servers/student01/start", headers={"X-Console-Request": "1"})
    assert r.status_code == 401 and r.json()["error"] == "not_authenticated"


# --- page ------------------------------------------------------------------------------------


def test_dashboard_page_and_assets(sampler, as_teacher, prefix):
    r = as_teacher.get(f"{prefix}/")
    assert r.status_code == 200
    for needle in ('id="dashboard-table"', 'id="stop-all"', 'id="stat-online"', 'id="dash-empty"', "dashboard.js", "dashboard.css", 'data-search-for="dashboard-table"'):
        assert needle in r.text, needle
    assert as_teacher.get(f"{prefix}/static/dashboard.js").status_code == 200
    assert as_teacher.get(f"{prefix}/static/dashboard.css").status_code == 200
