"""
Linux accounts for the classroom: the one implementation shared by the
container start-up script (`python -m console.accounts sync`) and the
teacher console (live changes).

Rules, unchanged from image 1.0's bash entrypoint:
- every student is a Linux user in group "students" (gid 3000), uid >= 2000;
- if /home/<user> already exists (files on the volume survive container
  replacement) its owner uid is re-used so the student keeps their files;
- passwords are (re)applied from the roster on every start because
  /etc/shadow lives in the container layer, not on a volume;
- ~/shared -> /srv/shared, ~/submit and ~/notebooks exist, home is 0750;
- admins (JUPYTERHUB_ADMIN_USERS) are also in group "admins" (gid 3001).

Everything goes through `System` (console/system.py) so tests can inject a
fake. All commands are argv lists; passwords travel over stdin only.
"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass, field

from .roster import (
    RESERVED_USERNAMES,
    Roster,
    RosterError,
    Student,
    parse_users_txt,
    validate_password,
    validate_username,
)
from .system import System

log = logging.getLogger("console.accounts")


class AccountError(Exception):
    """A Linux account operation failed; the message is safe to show a teacher."""


@dataclass
class AccountSettings:
    home_root: str = "/home"
    shared_dir: str = "/srv/shared"
    state_dir: str = "/srv/jupyterhub"
    students_group: str = "students"
    students_gid: int = 3000
    admins_group: str = "admins"
    admins_gid: int = 3001
    first_uid: int = 2000
    kit_dir: str = "/opt/classroom/skel"     # welcome kit source for backfill
    kit_version: int = 1
    useradd: str = "/usr/sbin/useradd"
    userdel: str = "/usr/sbin/userdel"
    usermod: str = "/usr/sbin/usermod"
    groupadd: str = "/usr/sbin/groupadd"
    chpasswd: str = "/usr/sbin/chpasswd"
    tar: str = "/usr/bin/tar"

    @property
    def removed_dir(self) -> str:
        return os.path.join(self.state_dir, "removed")

    @property
    def kit_marker_dir(self) -> str:
        """Root-only folder recording which kit version each home received."""
        return os.path.join(self.state_dir, "kit")


@dataclass
class CreateOutcome:
    username: str
    uid: int
    home_created: bool
    chowned: bool = False


@dataclass
class RemoveOutcome:
    username: str
    archive_path: str | None
    home_removed: bool
    killed: int = 0


@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)
    recreated: list[str] = field(default_factory=list)
    passwords_applied: int = 0
    kit_files_added: int = 0
    kit_homes: int = 0
    admins_ok: list[str] = field(default_factory=list)
    admins_missing: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


class Accounts:
    def __init__(self, settings: AccountSettings | None = None, system: System | None = None):
        self.s = settings or AccountSettings()
        self.sys = system or System()

    # --- helpers -------------------------------------------------------------
    def home(self, username: str) -> str:
        return os.path.join(self.s.home_root, username)

    def _run(self, argv, *, input_bytes=None, what="command"):
        res = self.sys.run(argv, input_bytes=input_bytes)
        if not res.ok:
            detail = (res.stderr or res.stdout).strip().splitlines()
            msg = detail[-1] if detail else f"exit code {res.returncode}"
            raise AccountError(f"{what} failed: {msg}")
        return res

    def user_exists(self, username: str) -> bool:
        try:
            self.sys.getpwnam(username)
            return True
        except KeyError:
            return False

    def uid_taken(self, uid: int) -> bool:
        try:
            self.sys.getpwuid(uid)
            return True
        except KeyError:
            return False

    def status(self, username: str) -> dict:
        uid = None
        try:
            uid = self.sys.getpwnam(username).pw_uid
        except KeyError:
            pass
        return {
            "uid": uid,
            "linux_ok": uid is not None,
            "home_ok": self.sys.isdir(self.home(username)),
        }

    def ensure_groups(self) -> None:
        for name, gid in ((self.s.students_group, self.s.students_gid), (self.s.admins_group, self.s.admins_gid)):
            try:
                self.sys.getgrnam(name)
            except KeyError:
                self._run([self.s.groupadd, "-g", str(gid), name], what=f"groupadd {name}")

    # --- uid choice --------------------------------------------------------------
    def pick_uid(self, username: str) -> int:
        """Re-use the owner uid of an orphaned existing home; else first free uid >= first_uid."""
        home = self.home(username)
        if self.sys.isdir(home):
            owner = self.sys.owner_uid(home)
            if owner >= self.s.first_uid and not self.uid_taken(owner):
                return owner
        home_owners = set(self.sys.scandir_owners(self.s.home_root).values())
        uid = self.s.first_uid
        while self.uid_taken(uid) or uid in home_owners:
            uid += 1
        return uid

    # --- create / update -------------------------------------------------------------
    def create(self, username: str, password: str) -> CreateOutcome:
        username = validate_username(username)
        password = validate_password(password)
        if self.user_exists(username):
            raise AccountError(f"account '{username}' already exists")
        home = self.home(username)
        uid = self.pick_uid(username)
        home_exists = self.sys.isdir(home)
        base = [self.s.useradd, "--uid", str(uid), "--gid", self.s.students_group,
                "--shell", "/bin/bash", "--home-dir", home]
        if home_exists:
            self._run(base + ["--no-create-home", username], what="useradd")
        else:
            self._run(base + ["--create-home", username], what="useradd")
        outcome = CreateOutcome(username=username, uid=uid, home_created=not home_exists)
        try:
            if home_exists and self.sys.owner_uid(home) != uid:
                self.sys.chown_tree(home, uid, self.s.students_gid)
                outcome.chowned = True
            self.sys.chmod(home, 0o750)
            self.set_password(username, password)
            self.ensure_home_extras(username, uid, new_home=not home_exists)
        except Exception:
            # Leave no half-made account behind. The home is kept if it pre-existed.
            self.sys.run([self.s.userdel, username])
            if not home_exists and self.sys.isdir(home):
                try:
                    self.sys.rmtree(home)
                except OSError:
                    pass
            raise
        return outcome

    def set_password(self, username: str, password: str) -> None:
        username = validate_username(username)
        password = validate_password(password)
        self._run([self.s.chpasswd], input_bytes=f"{username}:{password}\n".encode(), what="chpasswd")

    def ensure_home_extras(self, username: str, uid: int, *, new_home: bool = False) -> int:
        """~/shared symlink, ~/submit, ~/notebooks, welcome-kit backfill. Returns kit files added."""
        home = self.home(username)
        gid = self.s.students_gid
        link = os.path.join(home, "shared")
        if self.sys.islink(link) or not self.sys.exists(link):
            try:
                self.sys.symlink_replace(self.s.shared_dir, link)
            except OSError:
                log.warning("could not refresh shared link for %s; keeping the account", username)
        for sub in ("submit", "notebooks"):
            path = os.path.join(home, sub)
            if not self.sys.exists(path):
                try:
                    self.sys.mkdir(path, 0o750)
                    self.sys.lchown(path, uid, gid)
                except OSError:
                    log.warning("could not prepare %s for %s; continuing welcome kit", sub, username)
        return self._backfill_kit(username, home, uid, gid)

    def _kit_marker(self, username: str) -> str:
        return os.path.join(self.s.kit_marker_dir, username)

    def _backfill_kit(self, username: str, home: str, uid: int, gid: int) -> int:
        """
        Copy welcome-kit files that are missing, once per kit version. Never
        overwrites. The marker lives in the root-only state dir (not in the
        student's home), and every write goes through System.write_home_file,
        which never follows a link the student may have planted.
        """
        if not self.sys.isdir(self.s.kit_dir):
            return 0
        marker = self._kit_marker(username)
        try:
            if int(self.sys.read_text(marker).strip()) >= self.s.kit_version:
                return 0
        except (OSError, ValueError):
            pass
        added = 0
        for rel in sorted(self.sys.walk_files(self.s.kit_dir)):
            if any(part.startswith(".") for part in rel.split("/")):
                continue  # dotfiles (shell rc files, .DS_Store) are never part of the kit
            data = self.sys.read_bytes(os.path.join(self.s.kit_dir, rel))
            if self.sys.write_home_file(home, rel, data, uid, gid):
                added += 1
        self.sys.mkdir(self.s.kit_marker_dir, 0o700)
        self.sys.write_text(marker, f"{self.s.kit_version}\n", 0o600)
        return added

    def ensure_admin(self, username: str) -> None:
        self._run([self.s.usermod, "-aG", self.s.admins_group, username], what="usermod")

    # --- remove --------------------------------------------------------------------
    def remove(self, username: str, *, archive: bool = True, delete_home: bool = True) -> RemoveOutcome:
        username = validate_username(username)
        home = self.home(username)
        killed = 0
        uid = None
        try:
            uid = self.sys.getpwnam(username).pw_uid
        except KeyError:
            pass
        if uid is not None:
            killed = self.sys.kill_uid_processes(uid)
            res = self.sys.run([self.s.userdel, username])
            if not res.ok and "currently used by process" in (res.stderr + res.stdout):
                time.sleep(1)
                killed += self.sys.kill_uid_processes(uid)
                res = self.sys.run([self.s.userdel, username])
            if not res.ok and self.user_exists(username):
                raise AccountError(f"userdel failed: {(res.stderr or res.stdout).strip()}")
        archive_path = None
        home_removed = False
        self.sys.remove_file(self._kit_marker(username))
        if self.sys.isdir(home):
            if archive:
                self.sys.mkdir(self.s.removed_dir, 0o700)
                stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.sys.now()))
                archive_path = os.path.join(self.s.removed_dir, f"{username}-{stamp}.tar.gz")
                self._run(
                    [self.s.tar, "--create", "--gzip", "--file", archive_path, "-C", self.s.home_root, username],
                    what="archiving the home folder",
                )
                self.sys.chmod(archive_path, 0o600)
            if delete_home and (archive_path or not archive):
                self.sys.rmtree(home)
                home_removed = True
        return RemoveOutcome(username=username, archive_path=archive_path, home_removed=home_removed, killed=killed)

    # --- sync (boot and repair) ------------------------------------------------------
    def sync(self, students: list[Student], admins: set[str]) -> SyncReport:
        report = SyncReport()
        self.ensure_groups()
        for st in students:
            try:
                username = validate_username(st.username)
                password = validate_password(st.password)
            except ValueError as e:
                report.errors.append((st.username, str(e)))
                continue
            try:
                if not self.user_exists(username):
                    home_existed = self.sys.isdir(self.home(username))
                    out = self.create(username, password)
                    (report.recreated if home_existed else report.created).append(username)
                    report.passwords_applied += 1
                    added = 0  # create() already ran the extras
                else:
                    uid = self.sys.getpwnam(username).pw_uid
                    self.set_password(username, password)
                    report.passwords_applied += 1
                    added = self.ensure_home_extras(username, uid)
                if added:
                    report.kit_files_added += added
                    report.kit_homes += 1
            except (AccountError, OSError) as e:
                report.errors.append((username, str(e)))
        for admin in sorted(a.strip().lower() for a in admins if a.strip()):
            if self.user_exists(admin):
                try:
                    self.ensure_admin(admin)
                    report.admins_ok.append(admin)
                except AccountError as e:
                    report.errors.append((admin, str(e)))
            else:
                report.admins_missing.append(admin)
        roster_names = {s.username for s in students}
        for pw in self.sys.all_users():
            if pw.pw_uid >= self.s.first_uid and pw.pw_gid == self.s.students_gid and pw.pw_name not in roster_names:
                report.orphans.append(pw.pw_name)
        return report


# --- boot-time helpers -----------------------------------------------------------------


def seed_from_users_txt(roster: Roster, users_txt: str, admins: set[str]) -> tuple[list[Student], list[str]]:
    """Create roster.json from users.txt (or empty). Returns (students, messages)."""
    messages = []
    students: list[Student] = []
    try:
        text = open(users_txt, encoding="utf-8").read()
    except OSError:
        text = ""
        messages.append(f"no readable {users_txt}; starting with an empty roster")
    if text:
        students, errors = parse_users_txt(text)
        for lineno, reason in errors:
            messages.append(f"{users_txt} line {lineno}: {reason} (skipped)")
        messages.append(f"seeded {len(students)} account(s) from {users_txt}")
    students, extra = bootstrap_admins(students, admins)
    messages.extend(extra)
    roster.save(students)
    return students, messages


def bootstrap_admins(students: list[Student], admins: set[str]) -> tuple[list[Student], list[str]]:
    """Make sure every admin has an account, generating a password if needed."""
    messages = []
    names = {s.username for s in students}
    for admin in sorted(a.strip().lower() for a in admins if a.strip()):
        if admin in names:
            continue
        try:
            validate_username(admin)
        except ValueError as e:
            messages.append(f"admin '{admin}' skipped: {e}")
            continue
        password = "-".join(secrets.token_hex(2) for _ in range(3))
        students.append(Roster.new_student(admin, password, display_name=admin.capitalize()))
        banner = (
            "\n" + "=" * 68 + "\n"
            f"  Admin account '{admin}' was not in the roster and has been created.\n"
            f"  Username: {admin}\n"
            f"  Password: {password}\n"
            "  Log in and change it in the teacher console (Students > Reset password).\n"
            + "=" * 68
        )
        messages.append(banner)
    return students, messages


def _format_report(report: SyncReport) -> list[str]:
    lines = []
    if report.created:
        lines.append(f"created {len(report.created)} new account(s): {', '.join(report.created)}")
    if report.recreated:
        lines.append(f"re-created {len(report.recreated)} account(s) with existing files kept")
    lines.append(f"{report.passwords_applied} password(s) applied")
    if report.kit_homes:
        lines.append(f"welcome kit: {report.kit_files_added} file(s) added to {report.kit_homes} home(s)")
    for a in report.admins_ok:
        lines.append(f"admin '{a}' is in group admins")
    for a in report.admins_missing:
        lines.append(f"WARNING: admin '{a}' has no account; nobody can open the teacher console as '{a}'")
    for o in report.orphans:
        lines.append(f"note: account '{o}' exists but is not in the roster (remove or import it from the console)")
    for name, err in report.errors:
        lines.append(f"WARNING: {name}: {err}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m console.accounts", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--roster", default="/srv/jupyterhub/roster.json")
        p.add_argument("--admins", default=os.environ.get("JUPYTERHUB_ADMIN_USERS", "teacher"))

    p_sync = sub.add_parser("sync", help="create accounts from the roster (seeding it first if missing)")
    common(p_sync)
    p_sync.add_argument("--seed-from", default="/etc/jupyterhub/users.txt")
    p_seed = sub.add_parser("seed", help="create roster.json from users.txt if it does not exist")
    common(p_seed)
    p_seed.add_argument("--from", dest="seed_from", default="/etc/jupyterhub/users.txt")
    p_seed.add_argument("--force", action="store_true", help="overwrite an existing roster")
    p_list = sub.add_parser("list", help="print the roster usernames")
    common(p_list)
    args = parser.parse_args(argv)

    def out(msg: str):
        print(f"[accounts] {msg}", flush=True)

    admins = {a.strip().lower() for a in args.admins.split(",") if a.strip()}
    roster = Roster(args.roster)

    if args.cmd == "list":
        for s in roster.load():
            print(s.username)
        return 0

    if args.cmd == "seed":
        if roster.exists() and not args.force:
            out(f"{args.roster} already exists; nothing to do (use --force to overwrite)")
            return 0
        with roster.lock():
            _, messages = seed_from_users_txt(roster, args.seed_from, admins)
        for m in messages:
            out(m)
        return 0

    # sync
    if os.geteuid() != 0:
        out("ERROR: must run as root")
        return 2
    with roster.lock():
        if not roster.exists():
            students, messages = seed_from_users_txt(roster, args.seed_from, admins)
            for m in messages:
                out(m)
        else:
            try:
                students = roster.load()
            except RosterError as e:
                out(f"ERROR: {e}")
                return 2
            students, messages = bootstrap_admins(students, admins)
            if messages:
                roster.save(students)
                for m in messages:
                    out(m)
            try:
                if os.path.getmtime(args.seed_from) > os.path.getmtime(args.roster):
                    out(f"note: {args.seed_from} is newer than the roster; it is NOT applied automatically "
                        "- use the teacher console (Students > Import users.txt)")
            except OSError:
                pass
        report = Accounts().sync(students, admins)
    for line in _format_report(report):
        out(line)
    out(f"{len(students)} account(s) in the roster")
    return 0


if __name__ == "__main__":
    sys.exit(main())
