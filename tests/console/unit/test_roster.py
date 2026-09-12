import json
import os
import random
import stat

import pytest

from console.roster import (
    Roster,
    RosterError,
    Student,
    generate_password,
    parse_bulk_text,
    parse_users_txt,
    suggest_username,
    validate_password,
    validate_username,
)


# --- validation -------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [("Priya", "priya"), ("  student01 ", "student01"), ("a_b-c", "a_b-c"), ("_x", "_x")],
)
def test_validate_username_normalises(raw, expected):
    assert validate_username(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "1abc", "priya.sharma", "with space", "a" * 33, "root", "jovyan", "Nobody", "ünïcode"],
)
def test_validate_username_rejects(raw):
    with pytest.raises(ValueError):
        validate_username(raw)


def test_validate_password_rules():
    assert validate_password("blue-tiger-27") == "blue-tiger-27"
    assert validate_password("has:colon") == "has:colon"
    for bad in ["", "x" * 73, "new\nline", "tab\there", "ünïcode"]:
        with pytest.raises(ValueError):
            validate_password(bad)


def test_generate_password_shape():
    rng = random.Random(7)
    for _ in range(20):
        pw = generate_password(rng)
        a, b, n = pw.split("-")
        assert a != b and a.isalpha() and b.isalpha() and 10 <= int(n) <= 99
        validate_password(pw)


def test_suggest_username():
    assert suggest_username("Priya Sharma") == "priya"
    assert suggest_username("Priya Sharma", {"priya"}) == "priyas"
    assert suggest_username("Priya Sharma", {"priya", "priyas"}) == "priyasharma"
    assert suggest_username("Priya Sharma", {"priya", "priyas", "priyasharma"}) == "priya2"
    assert suggest_username("Zoë Ångström") == "zoe"
    assert suggest_username("123") == "s123"
    assert suggest_username("") == "student"
    assert suggest_username("Root") != "root"


# --- parsers ----------------------------------------------------------------------

def test_parse_users_txt_rules():
    text = (
        "# comment line\n"
        "teacher:change-me   # trailing comment\r\n"
        "student01:pass:with:colons\n"
        "\n"
        "Student02:UPPER\n"
        "badline\n"
        "root:x\n"
        "bad name:pw\n"
        "student01:again\n"
        "empty:\n"
    )
    students, errors = parse_users_txt(text)
    assert [(s.username, s.password) for s in students] == [
        ("teacher", "change-me"),
        ("student01", "pass:with:colons"),
        ("student02", "UPPER"),
    ]
    assert [ln for ln, _ in errors] == [6, 7, 8, 9, 10]


def test_parse_bulk_text_formats():
    text = (
        "Name, Username, Password\n"
        "Priya Sharma\n"
        "Liam Chen, liam\n"
        "Ana Silva, ana, sunny-day-7\n"
        "sam:secret\n"
        "a, b, c, d\n"
        "\n"
        ", \n"
    )
    rows, errors = parse_bulk_text(text)
    assert [(r["display_name"], r["username"], r["password"]) for r in rows] == [
        ("Priya Sharma", "", ""),
        ("Liam Chen", "liam", ""),
        ("Ana Silva", "ana", "sunny-day-7"),
        ("", "sam", "secret"),
    ]
    assert [ln for ln, _ in errors] == [6, 8]


# --- storage ----------------------------------------------------------------------

def test_roster_missing_file_is_empty(tmp_path):
    r = Roster(str(tmp_path / "roster.json"))
    assert r.load() == []
    assert not r.exists()


def test_roster_roundtrip_mode_and_backup(tmp_path):
    path = tmp_path / "roster.json"
    r = Roster(str(path))
    s1 = Roster.new_student("Priya", "blue-tiger-27", "Priya Sharma")
    assert s1.created.endswith("Z")
    r.save([s1])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["version"] == 1 and data["users"][0]["username"] == "priya"
    assert not (tmp_path / "roster.json.bak").exists()

    r.save([s1, Student("liam", "pw")])
    assert (tmp_path / "roster.json.bak").exists()
    assert [s.username for s in r.load()] == ["priya", "liam"]
    assert not list(tmp_path.glob("roster.json.tmp-*"))


def test_roster_corrupt_falls_back_to_backup(tmp_path):
    path = tmp_path / "roster.json"
    r = Roster(str(path))
    r.save([Student("priya", "pw")])
    r.save([Student("priya", "pw"), Student("liam", "pw")])
    path.write_text("{ not json")
    assert [s.username for s in r.load()] == ["priya"]  # from .bak
    (tmp_path / "roster.json.bak").write_text("also broken")
    with pytest.raises(RosterError):
        r.load()


def test_roster_dedupes_and_normalises_on_load(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"users": [
        {"username": " Priya ", "password": "a"},
        {"username": "priya", "password": "b"},
        {"username": "", "password": "c"},
        "garbage",
    ]}))
    students = Roster(str(path)).load()
    assert [(s.username, s.password) for s in students] == [("priya", "a")]


def test_roster_lock_is_reentrant(tmp_path):
    r = Roster(str(tmp_path / "roster.json"))
    with r.lock():
        with r.lock():
            r.save([])
    assert r.load() == []
