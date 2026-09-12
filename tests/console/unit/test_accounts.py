import os
import stat

import pytest

from console.accounts import (
    AccountError,
    Accounts,
    Roster,
    Student,
    bootstrap_admins,
    main,
    seed_from_users_txt,
)


def useradd_calls(fake):
    return fake.argv_of("useradd")


# --- create --------------------------------------------------------------------

def test_create_new_account(accounts, fake, settings):
    out = accounts.create("Priya", "blue-tiger-27")
    assert out.uid == 2000 and out.home_created and not out.chowned
    home = os.path.join(settings.home_root, "priya")
    assert useradd_calls(fake) == [[
        settings.useradd, "--uid", "2000", "--gid", "students", "--shell", "/bin/bash",
        "--home-dir", home, "--create-home", "priya",
    ]]
    assert fake.passwords == {"priya": "blue-tiger-27"}
    assert fake.stdin == [b"priya:blue-tiger-27\n"]           # password only via stdin
    assert all("blue-tiger-27" not in " ".join(c) for c in fake.calls)
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o750
    assert os.readlink(os.path.join(home, "shared")) == settings.shared_dir
    for sub in ("submit", "notebooks"):
        assert os.path.isdir(os.path.join(home, sub))
        assert fake.owners[os.path.join(home, sub)] == 2000
    assert os.path.exists(os.path.join(home, "Welcome.ipynb"))  # skel copied by useradd


def test_create_reuses_orphan_home_uid(accounts, fake, settings):
    fake.make_home("priya", 2007, {"notes.txt": "keep me"})
    out = accounts.create("priya", "pw")
    assert out.uid == 2007 and not out.home_created and not out.chowned
    assert "--no-create-home" in useradd_calls(fake)[0]
    assert open(os.path.join(settings.home_root, "priya", "notes.txt")).read() == "keep me"
    assert not fake.argv_of("chown")  # ownership already right; chown_tree not needed


def test_create_chowns_home_with_low_uid_owner(accounts, fake, settings):
    fake.make_home("priya", 1000, {"old.txt": "x"})
    out = accounts.create("priya", "pw")
    assert out.uid == 2000 and out.chowned
    home = os.path.join(settings.home_root, "priya")
    assert fake.owners[home] == 2000 and fake.owners[os.path.join(home, "old.txt")] == 2000


def test_create_skips_taken_uids_and_foreign_home_owners(accounts, fake, settings):
    fake.add_user("teacher", 2000)
    fake.add_user("student01", 2001)
    fake.make_home("someone-else", 2002)  # orphan home for another name
    out = accounts.create("priya", "pw")
    assert out.uid == 2003


def test_create_rejects_existing_and_reserved(accounts, fake):
    fake.add_user("priya", 2000)
    with pytest.raises(AccountError):
        accounts.create("priya", "pw")
    with pytest.raises(ValueError):
        accounts.create("root", "pw")
    with pytest.raises(ValueError):
        accounts.create("ok", "")


def test_create_rolls_back_when_password_fails(accounts, fake, settings):
    fake.fail_next["chpasswd"] = (1, "chpasswd: PAM: boom")
    with pytest.raises(AccountError, match="chpasswd failed"):
        accounts.create("priya", "pw")
    assert "priya" not in fake.users
    assert fake.argv_of("userdel") == [[settings.userdel, "priya"]]
    assert not os.path.exists(os.path.join(settings.home_root, "priya"))  # new home removed


def test_create_keeps_preexisting_home_on_failure(accounts, fake, settings):
    fake.make_home("priya", 2005, {"keep.txt": "k"})
    fake.fail_next["chpasswd"] = (1, "boom")
    with pytest.raises(AccountError):
        accounts.create("priya", "pw")
    assert os.path.exists(os.path.join(settings.home_root, "priya", "keep.txt"))


# --- welcome kit ---------------------------------------------------------------

def test_kit_backfill_once_and_never_overwrites(accounts, fake, settings):
    fake.make_home("priya", 2001, {"submit/README.md": "my own readme"})
    accounts.create("priya", "pw")
    home = os.path.join(settings.home_root, "priya")
    assert open(os.path.join(home, "Welcome.ipynb")).read() == '{"cells": []}'
    assert open(os.path.join(home, "submit", "README.md")).read() == "my own readme"
    assert fake.owners[os.path.join(home, "Welcome.ipynb")] == 2001
    assert open(os.path.join(home, ".classroom", "kit-version")).read().strip() == "1"

    os.unlink(os.path.join(home, "Welcome.ipynb"))          # student deletes it
    assert accounts.ensure_home_extras("priya", 2001) == 0   # marker: not re-added
    assert not os.path.exists(os.path.join(home, "Welcome.ipynb"))

    settings.kit_version = 2                                 # new kit version re-seeds missing files
    assert accounts.ensure_home_extras("priya", 2001) == 1
    assert os.path.exists(os.path.join(home, "Welcome.ipynb"))
    assert open(os.path.join(home, "submit", "README.md")).read() == "my own readme"


def test_kit_missing_dir_is_noop(accounts, fake, settings):
    settings.kit_dir = os.path.join(settings.state_dir, "nope")
    fake.skel_dir = None
    accounts.create("priya", "pw")
    assert not os.path.exists(os.path.join(settings.home_root, "priya", ".classroom"))


# --- remove --------------------------------------------------------------------

def test_remove_archives_and_deletes_home(accounts, fake, settings):
    accounts.create("priya", "pw")
    out = accounts.remove("priya")
    assert fake.killed == [2000]
    assert fake.argv_of("userdel") == [[settings.userdel, "priya"]]  # never -r
    assert out.archive_path and out.archive_path.startswith(settings.removed_dir + "/priya-")
    assert out.archive_path.endswith(".tar.gz") and os.path.exists(out.archive_path)
    assert stat.S_IMODE(os.stat(out.archive_path).st_mode) == 0o600
    tar = fake.argv_of("tar")[0]
    assert tar[-3:] == ["-C", settings.home_root, "priya"] and "--file" in tar
    assert out.home_removed and not os.path.exists(os.path.join(settings.home_root, "priya"))
    assert "priya" not in fake.users


def test_remove_retries_userdel_when_busy(accounts, fake):
    accounts.create("priya", "pw")
    fake.busy_once.add("priya")
    out = accounts.remove("priya", archive=False)
    assert len(fake.argv_of("userdel")) == 2 and fake.killed == [2000, 2000]
    assert out.archive_path is None and out.home_removed


def test_remove_without_account_still_archives_home(accounts, fake, settings):
    fake.make_home("ghost", 2042, {"a.txt": "a"})
    out = accounts.remove("ghost")
    assert out.archive_path and out.home_removed and fake.killed == []


def test_remove_keeps_home_when_archive_fails(accounts, fake, settings):
    accounts.create("priya", "pw")
    fake.fail_next["tar"] = (2, "tar: disk full")
    with pytest.raises(AccountError, match="archiving"):
        accounts.remove("priya")
    assert os.path.isdir(os.path.join(settings.home_root, "priya"))


# --- sync ----------------------------------------------------------------------

def test_sync_creates_reapplies_and_reports(accounts, fake, settings):
    fake.make_home("student02", 2002)                 # survives container replacement
    fake.add_user("stray", 2050)                      # not in roster
    roster = [
        Student("teacher", "t-pw"),
        Student("student01", "pw1"),
        Student("student02", "pw2"),
        Student("bad name", "x"),
    ]
    rep = accounts.sync(roster, {"teacher", "ghost"})
    assert rep.created == ["teacher", "student01"] and rep.recreated == ["student02"]
    assert rep.passwords_applied == 3
    assert fake.users["student02"].pw_uid == 2002
    assert rep.admins_ok == ["teacher"] and rep.admins_missing == ["ghost"]
    assert fake.supplementary == {"teacher": {"admins"}}
    assert rep.orphans == ["stray"]
    assert rep.errors and rep.errors[0][0] == "bad name"
    assert fake.groups == {"students": 3000, "admins": 3001}

    fake.calls.clear()
    rep2 = accounts.sync(roster, {"teacher"})
    assert rep2.created == [] and rep2.recreated == [] and rep2.passwords_applied == 3
    assert not useradd_calls(fake) and len(fake.argv_of("chpasswd")) == 3
    assert rep2.kit_homes == 0


def test_sync_creates_missing_groups(accounts, fake, settings):
    fake.groups.clear()
    accounts.sync([], set())
    assert fake.groups == {"students": 3000, "admins": 3001}
    assert [c[1:] for c in fake.argv_of("groupadd")] == [["-g", "3000", "students"], ["-g", "3001", "admins"]]


# --- seeding and CLI -------------------------------------------------------------

def test_seed_from_users_txt_bootstraps_admin(tmp_path):
    users_txt = tmp_path / "users.txt"
    users_txt.write_text("student01:pw1\nstudent02:pw2\n")
    roster = Roster(str(tmp_path / "roster.json"))
    students, messages = seed_from_users_txt(roster, str(users_txt), {"teacher"})
    names = [s.username for s in students]
    assert names == ["student01", "student02", "teacher"]
    teacher = students[-1]
    assert teacher.password and teacher.display_name == "Teacher"
    assert any("Password: " + teacher.password in m for m in messages)
    assert [s.username for s in roster.load()] == names


def test_bootstrap_admins_is_noop_when_present():
    students = [Student("teacher", "pw")]
    out, messages = bootstrap_admins(students, {"teacher"})
    assert out == students and messages == []


def test_cli_seed_and_list(tmp_path, capsys):
    users_txt = tmp_path / "users.txt"
    users_txt.write_text("teacher:t\nstudent01:pw\n")
    roster = str(tmp_path / "roster.json")
    assert main(["seed", "--roster", roster, "--from", str(users_txt), "--admins", "teacher"]) == 0
    assert main(["seed", "--roster", roster, "--from", str(users_txt)]) == 0  # second run: no-op
    capsys.readouterr()
    assert main(["list", "--roster", roster]) == 0
    assert capsys.readouterr().out.split() == ["teacher", "student01"]
