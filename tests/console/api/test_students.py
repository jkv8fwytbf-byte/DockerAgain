"""
Students API: list, add (order: Linux before Hub), bulk preview/commit, import
users.txt, reset password, remove (Hub first, archive), repair, print cards,
and the access rules.
"""
from __future__ import annotations

import logging
import os
import re

import pytest

from console import students as students_mod
from console.auth import CSRF_HEADER
from console.roster import Roster, Student

PW_RE = re.compile(r"^[a-z]+-[a-z]+-\d{2}$")


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    monkeypatch.setattr(students_mod, "STOP_POLL_INTERVAL", 0)
    monkeypatch.setattr(students_mod, "STOP_POLL_TIMEOUT", 0.2)
    monkeypatch.setattr(students_mod, "DELETE_RETRY_INTERVAL", 0)
    monkeypatch.setattr(students_mod, "DELETE_RETRY_TIMEOUT", 0.2)


@pytest.fixture
def roster(settings):
    r = Roster(settings.roster_path)
    r.save([
        Student("teacher", "teach-pass-1", "Ms Teacher", "2026-09-01T00:00:00Z"),
        Student("student01", "changeme01", "Student One", "2026-09-01T00:00:00Z"),
    ])
    return r


def names(roster):
    return [s.username for s in roster.load()]


def warm_admin_cache(as_teacher, prefix):
    assert as_teacher.get(f"{prefix}/api/whoami").status_code == 200


# --- access -------------------------------------------------------------------------

def test_anonymous_is_refused(client, prefix, roster):
    assert client.get(f"{prefix}/api/students").status_code == 401
    assert client.post(f"{prefix}/api/students", json={"display_name": "X"}, headers={CSRF_HEADER: "1"}).status_code == 401
    assert client.get(f"{prefix}/print/cards").status_code == 302


def test_students_are_refused(as_student, prefix, roster):
    assert as_student.get(f"{prefix}/api/students").status_code == 403
    assert as_student.post(f"{prefix}/api/students", json={"display_name": "X"}).status_code == 403
    assert as_student.get(f"{prefix}/print/cards").status_code == 403


def test_csrf_header_required(as_teacher, prefix, roster):
    del as_teacher.headers[CSRF_HEADER]
    r = as_teacher.post(f"{prefix}/api/students", json={"display_name": "Priya Sharma"})
    assert r.status_code == 403 and r.json()["error"] == "csrf_failed"


# --- list ---------------------------------------------------------------------------

def test_list_students(as_teacher, prefix, roster, hub):
    hub.add("student01", running=True)
    r = as_teacher.get(f"{prefix}/api/students")
    assert r.status_code == 200
    body = r.json()
    by = {s["username"]: s for s in body["students"]}
    assert set(by) == {"teacher", "student01"}
    assert body["admins"] == ["teacher"] and body["hub_error"] is None
    assert by["teacher"]["is_admin"] is True and by["student01"]["is_admin"] is False
    assert by["student01"]["state"] == "running" and by["teacher"]["state"] == "stopped"
    assert by["student01"]["uid"] == 2001 and by["student01"]["linux_ok"] and by["student01"]["home_ok"] and by["student01"]["hub_ok"]
    assert by["student01"]["display_name"] == "Student One"
    assert "password" not in by["student01"]


def test_list_states_from_hub_pending(as_teacher, prefix, roster, hub):
    hub.users["student01"]["pending"] = "spawn"
    assert {s["username"]: s["state"] for s in as_teacher.get(f"{prefix}/api/students").json()["students"]}["student01"] == "starting"
    hub.users["student01"]["pending"] = "stop"
    assert {s["username"]: s["state"] for s in as_teacher.get(f"{prefix}/api/students").json()["students"]}["student01"] == "stopping"


def test_list_reveal_sets_no_store(as_teacher, prefix, roster):
    r = as_teacher.get(f"{prefix}/api/students?reveal=1")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    by = {s["username"]: s for s in r.json()["students"]}
    assert by["student01"]["password"] == "changeme01"


def test_list_marks_admins_declared_by_the_hub(as_teacher, prefix, roster, hub):
    hub.users["student01"]["admin"] = True
    body = as_teacher.get(f"{prefix}/api/students").json()
    assert {s["username"]: s["is_admin"] for s in body["students"]}["student01"] is True
    assert body["admins"] == ["student01", "teacher"]


def test_list_with_hub_down_reports_unknown(as_teacher, prefix, roster, hub):
    warm_admin_cache(as_teacher, prefix)
    hub.down = True
    r = as_teacher.get(f"{prefix}/api/students")
    assert r.status_code == 200
    body = r.json()
    assert body["hub_error"]
    assert all(s["state"] == "unknown" and s["hub_ok"] is None for s in body["students"])


# --- add ----------------------------------------------------------------------------

def test_add_generates_username_and_password_and_orders_linux_before_hub(as_teacher, prefix, roster, hub, fake_system, settings):
    hub_calls_at_useradd = []
    real_run = fake_system.run

    def spy(argv, **kw):
        if os.path.basename(argv[0]) == "useradd":
            hub_calls_at_useradd.append(list(hub.calls))
        return real_run(argv, **kw)

    fake_system.run = spy
    r = as_teacher.post(f"{prefix}/api/students", json={"display_name": "Priya Sharma"})
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    st = body["student"]
    assert st["username"] == "priya" and st["display_name"] == "Priya Sharma"
    assert PW_RE.match(st["password"]) and body["hub_registered"] is True and body["warnings"] == []
    assert st["linux_ok"] and st["home_ok"] and st["uid"] == 2002 and st["is_admin"] is False
    # Linux account first, then the Hub
    assert len(hub_calls_at_useradd) == 1
    assert ("POST", "/hub/api/users/priya") not in hub_calls_at_useradd[0]
    assert ("POST", "/hub/api/users/priya") in hub.calls
    assert "priya" in fake_system.users and fake_system.passwords["priya"] == st["password"]
    assert "priya" in hub.users
    assert "priya" in names(roster) and Roster.find(roster.load(), "priya").password == st["password"]
    home = os.path.join(settings.home_root, "priya")
    assert os.path.isdir(home) and os.path.isfile(os.path.join(home, "Welcome.ipynb")) and os.path.isdir(os.path.join(home, "submit"))
    # the password never travels on argv
    assert not any(st["password"] in " ".join(c) for c in fake_system.calls)


def test_add_with_explicit_username_and_password(as_teacher, prefix, roster, fake_system):
    r = as_teacher.post(f"{prefix}/api/students", json={"display_name": "Liam Chen", "username": " Liam ", "password": "tiger-42"})
    assert r.status_code == 201
    assert r.json()["student"]["username"] == "liam" and r.json()["student"]["password"] == "tiger-42"
    assert fake_system.passwords["liam"] == "tiger-42"


def test_add_username_collision_gets_a_suggestion(as_teacher, prefix, roster):
    r = as_teacher.post(f"{prefix}/api/students", json={"display_name": "Student Two"})
    assert r.status_code == 201 and r.json()["student"]["username"] == "student"
    r = as_teacher.post(f"{prefix}/api/students", json={"display_name": "Student Three"})
    assert r.status_code == 201 and r.json()["student"]["username"] == "studentt"


@pytest.mark.parametrize("body, code", [
    ({"display_name": "Priya", "username": "Bad Name"}, "invalid_username"),
    ({"display_name": "Priya", "username": "root"}, "invalid_username"),
    ({"display_name": "Priya", "password": "a\nb"}, "invalid_password"),
    ({"display_name": "Priya", "password": "x" * 73}, "invalid_password"),
    ({"display_name": "", "username": ""}, "invalid_name"),
    ({"display_name": "x" * 65}, "invalid_name"),
])
def test_add_validation_errors(as_teacher, prefix, roster, fake_system, body, code):
    before = list(fake_system.calls)
    r = as_teacher.post(f"{prefix}/api/students", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["error"] == code
    assert fake_system.calls == before                       # nothing was run


def test_add_conflicts(as_teacher, prefix, roster, hub, fake_system):
    r = as_teacher.post(f"{prefix}/api/students", json={"display_name": "Someone", "username": "student01"})
    assert r.status_code == 409 and r.json()["error"] == "exists" and r.json()["detail"]["where"] == "roster"
    fake_system.add_user("orphan", 2077)
    r = as_teacher.post(f"{prefix}/api/students", json={"display_name": "Orphan", "username": "orphan"})
    assert r.status_code == 409 and r.json()["detail"]["where"] == "linux"
    hub.add("ghost")
    r = as_teacher.post(f"{prefix}/api/students", json={"display_name": "Ghost", "username": "ghost"})
    assert r.status_code == 409 and r.json()["detail"]["where"] == "hub"
    assert "ghost" not in fake_system.users and "ghost" not in names(roster)


def test_add_with_hub_down_still_creates_account(as_teacher, prefix, roster, hub, fake_system):
    warm_admin_cache(as_teacher, prefix)
    hub.down = True
    r = as_teacher.post(f"{prefix}/api/students", json={"display_name": "Priya Sharma"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["hub_registered"] is False and any("Repair" in w for w in body["warnings"])
    assert "priya" in fake_system.users and "priya" in names(roster)
    assert "priya" not in hub.users


def test_suggest_username_and_generate_password(as_teacher, prefix, roster):
    r = as_teacher.get(f"{prefix}/api/students/suggest-username", params={"name": "Teacher Two"})
    assert r.status_code == 200 and r.json()["username"] == "teachert"
    r = as_teacher.get(f"{prefix}/api/students/suggest-username", params={"name": "Zoë Ångström"})
    assert r.json()["username"] == "zoe"
    r = as_teacher.get(f"{prefix}/api/students/generate-password")
    assert r.status_code == 200 and PW_RE.match(r.json()["password"]) and r.headers["cache-control"] == "no-store"


# --- bulk ---------------------------------------------------------------------------

BULK_TEXT = "name, username, password\nPriya Sharma\nStudent One, student01\nBad Person, bad name\nLiam Chen, , tiger-42\nPriya Sharma\nnothing,,,,\n"


def test_bulk_preview_from_text(as_teacher, prefix, roster):
    r = as_teacher.post(f"{prefix}/api/students/bulk/preview", json={"text": BULK_TEXT})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    rows = {row["line"]: row for row in r.json()["rows"]}
    assert set(rows) == {2, 3, 4, 5, 6, 7}
    assert rows[2]["result"] == "new" and rows[2]["username"] == "priya" and PW_RE.match(rows[2]["password"])
    assert rows[3]["result"] == "exists" and rows[3]["username"] == "student01"
    assert rows[4]["result"] == "invalid" and "username" in rows[4]["reason"]
    assert rows[5]["result"] == "new" and rows[5]["username"] == "liam" and rows[5]["password"] == "tiger-42"
    assert rows[6]["result"] == "new" and rows[6]["username"] == "priyas"       # unique within the batch
    assert rows[7]["result"] == "invalid" and "commas" in rows[7]["reason"]
    assert r.json()["summary"] == {"new": 3, "exists": 1, "invalid": 2}


def test_bulk_preview_from_uploaded_csv(as_teacher, prefix, roster):
    csv = b'\xef\xbb\xbfname,username,password\n"Sharma, Priya",priya,\nLiam Chen,liam,tiger-42\n'
    r = as_teacher.post(f"{prefix}/api/students/bulk/preview", files={"file": ("class.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [x["username"] for x in rows] == ["priya", "liam"]
    assert rows[0]["display_name"] == "Sharma Priya" and rows[1]["password"] == "tiger-42"


def test_bulk_preview_from_multipart_text_field(as_teacher, prefix, roster):
    r = as_teacher.post(f"{prefix}/api/students/bulk/preview", files={"text": (None, "Priya Sharma\nstudent01:changed")})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [(x["username"], x["result"]) for x in rows] == [("priya", "new"), ("student01", "exists")]
    assert rows[1]["password"] == "changed"


def test_bulk_preview_rejects_binary_and_huge_files(as_teacher, prefix, roster):
    r = as_teacher.post(f"{prefix}/api/students/bulk/preview", files={"file": ("x.txt", b"\xff\xfe\x00\x01bad", "text/plain")})
    assert r.status_code == 400 and r.json()["error"] == "bad_encoding"
    r = as_teacher.post(f"{prefix}/api/students/bulk/preview", files={"file": ("x.txt", b"a" * (1024 * 1024 + 1), "text/plain")})
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    r = as_teacher.post(f"{prefix}/api/students/bulk/preview", json={"nope": 1})
    assert r.status_code == 400


def test_bulk_commit_skip_conflicts(as_teacher, prefix, roster, hub, fake_system):
    rows = [
        {"display_name": "Priya Sharma", "username": "priya", "password": "blue-tiger-27", "line": 1},
        {"display_name": "Student One", "username": "student01", "password": "new-pass-9", "line": 2},
        {"display_name": "Bad", "username": "bad name", "password": "x", "line": 3},
        {"display_name": "Dup", "username": "priya", "password": "y", "line": 4},
        {"display_name": "Ana Silva", "username": "", "password": "", "line": 5},
    ]
    r = as_teacher.post(f"{prefix}/api/students/bulk", json={"rows": rows, "on_conflict": "skip"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == ["priya", "ana"] and body["skipped"] == ["student01"] and body["updated"] == [] and body["hub_failures"] == []
    assert sorted(e["line"] for e in body["errors"]) == [3, 4]
    assert fake_system.passwords["priya"] == "blue-tiger-27" and "student01" not in fake_system.passwords   # untouched
    assert "priya" in hub.users and "ana" in hub.users
    assert names(roster) == ["teacher", "student01", "priya", "ana"]


def test_bulk_commit_reset_password(as_teacher, prefix, roster, fake_system):
    rows = [{"display_name": "", "username": "student01", "password": "new-pass-9"}]
    r = as_teacher.post(f"{prefix}/api/students/bulk", json={"rows": rows, "on_conflict": "reset_password"})
    assert r.status_code == 200 and r.json()["updated"] == ["student01"] and r.json()["added"] == []
    assert fake_system.passwords["student01"] == "new-pass-9"
    assert Roster.find(roster.load(), "student01").password == "new-pass-9"
    assert Roster.find(roster.load(), "student01").display_name == "Student One"   # kept


def test_bulk_commit_with_hub_down_reports_hub_failures(as_teacher, prefix, roster, hub, fake_system):
    warm_admin_cache(as_teacher, prefix)
    hub.down = True
    rows = [{"display_name": "Priya Sharma", "username": "priya", "password": "pw-1"}, {"display_name": "Liam", "username": "liam", "password": "pw-2"}]
    r = as_teacher.post(f"{prefix}/api/students/bulk", json={"rows": rows})
    assert r.status_code == 200
    assert r.json()["added"] == ["priya", "liam"] and r.json()["hub_failures"] == ["priya", "liam"]
    assert "priya" in fake_system.users and "liam" in fake_system.users


def test_bulk_commit_empty_and_bad_conflict_mode(as_teacher, prefix, roster):
    assert as_teacher.post(f"{prefix}/api/students/bulk", json={"rows": []}).status_code == 400
    r = as_teacher.post(f"{prefix}/api/students/bulk", json={"rows": [{"username": "x"}], "on_conflict": "nuke"})
    assert r.status_code == 400 and r.json()["error"] == "validation_failed"


def test_import_users_txt(as_teacher, prefix, roster, settings, fake_system, hub):
    r = as_teacher.post(f"{prefix}/api/students/import-users-txt", json={"on_conflict": "skip"})
    assert r.status_code == 404 and r.json()["error"] == "users_txt_missing"
    with open(settings.users_txt, "w") as fh:
        fh.write("alice:pw-alice   # comment\nstudent01:changed\n\nbadline\n")
    r = as_teacher.post(f"{prefix}/api/students/import-users-txt", json={"on_conflict": "skip"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == ["alice"] and body["skipped"] == ["student01"]
    assert body["errors"] == [{"line": 4, "username": "", "reason": "expected username:password"}]
    assert fake_system.passwords["alice"] == "pw-alice" and "alice" in hub.users
    assert "student01" not in fake_system.passwords                 # skipped, untouched
    r = as_teacher.post(f"{prefix}/api/students/import-users-txt", json={"on_conflict": "reset_password"})
    assert r.json()["updated"] == ["alice", "student01"] and fake_system.passwords["student01"] == "changed"


# --- password -----------------------------------------------------------------------

def test_reset_password_generated_and_explicit(as_teacher, prefix, roster, fake_system):
    r = as_teacher.post(f"{prefix}/api/students/student01/password", json={"password": None})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    pw = r.json()["password"]
    assert PW_RE.match(pw) and fake_system.passwords["student01"] == pw
    assert Roster.find(roster.load(), "student01").password == pw
    r = as_teacher.post(f"{prefix}/api/students/student01/password", json={"password": "my-own-1"})
    assert r.status_code == 200 and fake_system.passwords["student01"] == "my-own-1"
    assert Roster.find(roster.load(), "student01").password == "my-own-1"
    assert fake_system.stdin[-1] == b"student01:my-own-1\n"


def test_reset_password_errors(as_teacher, prefix, roster, fake_system):
    r = as_teacher.post(f"{prefix}/api/students/ghost/password", json={})
    assert r.status_code == 404 and r.json()["error"] == "not_in_roster"
    r = as_teacher.post(f"{prefix}/api/students/student01/password", json={"password": "bad\npw"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_password"
    r = as_teacher.post(f"{prefix}/api/students/Bad%20Name/password", json={})
    assert r.status_code == 400 and r.json()["error"] == "invalid_username"
    r = as_teacher.post(f"{prefix}/api/students/..%2F..%2Fetc/password", json={})
    assert r.status_code in (400, 404)
    assert "student01" not in fake_system.passwords                 # nothing was changed


def test_reset_password_chpasswd_failure_leaves_roster(as_teacher, prefix, roster, fake_system):
    fake_system.fail_next["chpasswd"] = (1, "chpasswd: PAM failure")
    r = as_teacher.post(f"{prefix}/api/students/student01/password", json={"password": "new-1"})
    assert r.status_code == 500 and r.json()["error"] == "account_command_failed"
    assert Roster.find(roster.load(), "student01").password == "changeme01"


def test_passwords_never_appear_in_logs(as_teacher, prefix, roster, fake_system, caplog):
    caplog.set_level(logging.DEBUG)
    assert as_teacher.post(f"{prefix}/api/students", json={"display_name": "Priya Sharma", "password": "secret-pw-77"}).status_code == 201
    assert as_teacher.post(f"{prefix}/api/students/priya/password", json={"password": "another-pw-88"}).status_code == 200
    rows = [{"display_name": "Liam", "username": "liam", "password": "bulk-pw-99"}]
    assert as_teacher.post(f"{prefix}/api/students/bulk", json={"rows": rows}).status_code == 200
    fake_system.fail_next["chpasswd"] = (1, "chpasswd: PAM failure")       # the failure path logs too
    assert as_teacher.post(f"{prefix}/api/students/liam/password", json={"password": "failed-pw-11"}).status_code == 500
    for secret in ("secret-pw-77", "another-pw-88", "bulk-pw-99", "failed-pw-11"):
        assert secret not in caplog.text
    assert "priya" in caplog.text                                            # usernames are logged, passwords are not


# --- remove -------------------------------------------------------------------------

def test_remove_refuses_self_and_admins(as_teacher, prefix, roster, app, hub, fake_system):
    r = as_teacher.delete(f"{prefix}/api/students/teacher")
    assert r.status_code == 403 and r.json()["error"] == "is_self"
    app.state.settings.admin_users = frozenset({"teacher", "assistant"})
    roster.save(roster.load() + [Student("assistant", "pw", "Assistant", "")])
    r = as_teacher.delete(f"{prefix}/api/students/assistant")
    assert r.status_code == 403 and r.json()["error"] == "is_admin"
    hub.users["student01"]["admin"] = True                     # admin according to the Hub only
    r = as_teacher.delete(f"{prefix}/api/students/student01")
    assert r.status_code == 403 and r.json()["error"] == "is_admin"
    assert "student01" in fake_system.users and "student01" in names(roster)
    r = as_teacher.delete(f"{prefix}/api/students/ghost")
    assert r.status_code == 404 and r.json()["error"] == "not_in_roster"


def test_remove_archives_home_and_updates_everything(as_teacher, prefix, roster, hub, fake_system, settings):
    hub.add("student01", running=True)
    r = as_teacher.delete(f"{prefix}/api/students/student01")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "student01" and body["home"] == "archived" and body["hub_deleted"] is True
    assert body["archived_to"].startswith("student01-") and body["archived_to"].endswith(".tar.gz")
    assert os.path.isfile(os.path.join(settings.removed_dir, body["archived_to"]))
    # Hub: stop the server, wait, delete the user - before anything local changed
    stop_at = hub.calls.index(("DELETE", "/hub/api/users/student01/server"))
    delete_at = hub.calls.index(("DELETE", "/hub/api/users/student01"))
    assert stop_at < delete_at
    assert "student01" not in hub.users
    assert "student01" not in fake_system.users and fake_system.killed == [2001]
    assert not os.path.exists(os.path.join(settings.home_root, "student01"))
    assert names(roster) == ["teacher"]


def test_remove_retries_while_server_is_stopping(as_teacher, prefix, roster, hub, fake_system):
    hub.pending_stop.add("student01")
    r = as_teacher.delete(f"{prefix}/api/students/student01")
    assert r.status_code == 200, r.text
    assert hub.calls.count(("DELETE", "/hub/api/users/student01")) == 2
    assert "student01" not in hub.users and "student01" not in fake_system.users


def test_remove_keep_and_delete_home(as_teacher, prefix, roster, hub, fake_system, settings):
    roster.save(roster.load() + [Student("liam", "pw", "Liam", "")])
    fake_system.add_user("liam", 2002)
    fake_system.make_home("liam", 2002, {"notes.txt": "hi"})
    hub.add("liam")
    r = as_teacher.delete(f"{prefix}/api/students/liam?home=keep")
    assert r.status_code == 200 and r.json()["home"] == "kept" and r.json()["archived_to"] is None
    assert os.path.isfile(os.path.join(settings.home_root, "liam", "notes.txt"))
    assert "liam" not in fake_system.users and "liam" not in names(roster)

    r = as_teacher.delete(f"{prefix}/api/students/student01?home=delete")
    assert r.status_code == 200 and r.json()["home"] == "deleted" and r.json()["archived_to"] is None
    assert not os.path.exists(os.path.join(settings.home_root, "student01"))
    assert not os.path.isdir(settings.removed_dir) or not os.listdir(settings.removed_dir)

    r = as_teacher.delete(f"{prefix}/api/students/teacher?home=burn")
    assert r.status_code == 400


def test_remove_with_hub_down_changes_nothing(as_teacher, prefix, roster, hub, fake_system):
    warm_admin_cache(as_teacher, prefix)
    hub.down = True
    r = as_teacher.delete(f"{prefix}/api/students/student01")
    assert r.status_code == 503 and r.json()["error"] == "hub_unreachable"
    assert "student01" in fake_system.users and "student01" in names(roster) and fake_system.killed == []


def test_remove_student_unknown_to_hub(as_teacher, prefix, roster, hub, fake_system):
    del hub.users["student01"]
    r = as_teacher.delete(f"{prefix}/api/students/student01")
    assert r.status_code == 200 and r.json()["hub_deleted"] is False
    assert "student01" not in fake_system.users and "student01" not in names(roster)


def test_remove_userdel_failure_reports_hub_state(as_teacher, prefix, roster, hub, fake_system):
    fake_system.fail_next["userdel"] = (1, "userdel: cannot lock /etc/passwd")
    r = as_teacher.delete(f"{prefix}/api/students/student01")
    assert r.status_code == 500 and r.json()["error"] == "account_command_failed"
    assert r.json()["detail"]["hub_deleted"] is True
    assert "student01" in names(roster)                       # roster untouched, Repair can re-register


# --- repair -------------------------------------------------------------------------

def test_repair_recreates_missing_accounts_and_lists_orphans(as_teacher, prefix, roster, hub, fake_system):
    del fake_system.users["student01"]                         # Linux account lost, home kept (uid 2001)
    del hub.users["student01"]
    fake_system.add_user("orphan", 2050)
    hub.add("ghost")
    r = as_teacher.post(f"{prefix}/api/students/repair", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["linux_created"] == ["student01"] and body["hub_created"] == ["student01"]
    assert body["passwords_reapplied"] == 1 and body["errors"] == []
    assert body["linux_orphans"] == ["orphan"] and body["hub_orphans"] == ["ghost"]
    assert fake_system.users["student01"].pw_uid == 2001       # the old home's owner uid is re-used
    assert fake_system.passwords["student01"] == "changeme01" and fake_system.passwords["teacher"] == "teach-pass-1"
    assert "student01" in hub.users and "ghost" in hub.users   # never deletes


# --- print cards --------------------------------------------------------------------

def test_print_cards_page(as_teacher, prefix, roster):
    r = as_teacher.get(f"{prefix}/print/cards", headers={"X-Forwarded-Proto": "http", "X-Forwarded-Host": "192.168.1.10:8000"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    html = r.text
    assert "<svg" in html and "student01" in html and "changeme01" in html and "Ms Teacher" in html
    assert "http://192.168.1.10:8000" in html and "Scan to open" in html
    assert 'data-bs-theme="light"' in html and "darkmode.js" not in html
    assert "2 cards" in html and "1 page" in html
    assert "Test School" in html


def test_print_cards_selection_and_class_url(as_teacher, prefix, roster, settings):
    import json

    with open(settings.branding_json, "w") as fh:
        json.dump({"school_name": "Lincoln High", "class_name": "Grade 10", "accent": "blue", "class_url": "https://class.example.org/"}, fh)
    r = as_teacher.get(f"{prefix}/print/cards?students=student01,nope,Bad%20Name")
    assert r.status_code == 200
    assert "Student One" in r.text and "Ms Teacher" not in r.text and "teach-pass-1" not in r.text
    assert "https://class.example.org" in r.text and "Lincoln High" in r.text and "Grade 10" in r.text
    assert "1 card" in r.text
    r = as_teacher.get(f"{prefix}/print/cards?students=nope")
    assert r.status_code == 200 and "No cards to print" in r.text


def test_print_cards_escapes_names_and_passwords(as_teacher, prefix, roster):
    roster.save(roster.load() + [Student("evil", 'pw<b>"x', "<script>alert(1)</script>", "")])
    r = as_teacher.get(f"{prefix}/print/cards?students=evil")
    assert r.status_code == 200
    assert "<script>alert" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert 'pw<b>"x' not in r.text and "pw&lt;b&gt;" in r.text
    assert "1 card" in r.text


def test_cards_helpers(settings):
    from console import cards

    url = cards.login_url("http://192.168.1.10:8000/", "priya", "/")
    assert url == "http://192.168.1.10:8000/hub/login?username=priya"
    assert cards.login_url("http://h", "a_b", "/jupyter/") == "http://h/jupyter/hub/login?username=a_b"
    svg = cards.qr_svg(url, "QR for priya")
    assert svg.startswith("<svg") and 'viewBox="0 0' in svg and "QR for priya" in svg and "width=" not in svg
    cards_list = cards.build_cards([Student("zed", "pw-z", "Zed"), Student("amy", "pw-a", ""), Student("bob", "pw-b", "Bob")], "http://h")
    assert [c["display_name"] for c in cards_list] == ["amy", "Bob", "Zed"]
    assert cards.chunk(list(range(17)), 8) == [list(range(8)), list(range(8, 16)), [16]]


def test_students_page_renders(as_teacher, prefix, roster):
    r = as_teacher.get(f"{prefix}/students")
    assert r.status_code == 200
    assert 'id="modal-add"' in r.text and 'id="modal-bulk"' in r.text and "students.js" in r.text
    assert f'href="{prefix}/print/cards"' in r.text
    assert 'id="btn-import"' in r.text and 'id="btn-repair-menu"' in r.text
    assert r.headers["cache-control"] == "no-store"
    # Bootstrap's display utilities are !important and defeat the hidden attribute: never combine them.
    for tag in re.findall(r"<[^>]*\bhidden\b[^>]*>", r.text):
        assert not re.search(r'class="[^"]*\bd-(?:flex|block|inline|grid)', tag), tag
