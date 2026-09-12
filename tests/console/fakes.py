"""
FakeSystem: emulates useradd/userdel/usermod/chpasswd/tar against an in-memory
passwd table while using a real temporary directory for home folders
(ownership is tracked in a dict because tests are not root).
Shared by the unit and API test suites.
"""
from __future__ import annotations

import grp
import os
import pwd
import shutil

from console.system import RunResult


class FakeSystem:
    def __init__(self, home_root: str, skel_dir: str | None = None):
        self.home_root = home_root
        self.skel_dir = skel_dir
        self.users: dict[str, pwd.struct_passwd] = {}
        self.groups: dict[str, int] = {"students": 3000, "admins": 3001}
        self.supplementary: dict[str, set[str]] = {}
        self.passwords: dict[str, str] = {}
        self.owners: dict[str, int] = {}
        self.calls: list[list[str]] = []
        self.stdin: list[bytes] = []
        self.killed: list[int] = []
        self.busy_once: set[str] = set()
        self.fail_next: dict[str, tuple[int, str]] = {}
        self.clock = 1_757_700_000.0  # 2025-09-12T18:40:00Z

    # --- helpers for tests ---
    def add_user(self, name: str, uid: int, gid: int = 3000, home: str | None = None):
        home = home or os.path.join(self.home_root, name)
        self.users[name] = pwd.struct_passwd((name, "x", uid, gid, "", home, "/bin/bash"))

    def make_home(self, name: str, uid: int, files: dict[str, str] | None = None):
        home = os.path.join(self.home_root, name)
        os.makedirs(home, exist_ok=True)
        self.owners[home] = uid
        for rel, content in (files or {}).items():
            path = os.path.join(home, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(content)
            self.owners[path] = uid
        return home

    def argv_of(self, cmd: str) -> list[list[str]]:
        return [c for c in self.calls if os.path.basename(c[0]) == cmd]

    # --- System API ---
    def run(self, argv, *, input_bytes=None, timeout=30) -> RunResult:
        argv = list(argv)
        self.calls.append(argv)
        cmd = os.path.basename(argv[0])
        if cmd in self.fail_next:
            rc, err = self.fail_next.pop(cmd)
            return RunResult(rc, "", err)
        if cmd == "groupadd":
            self.groups[argv[-1]] = int(argv[argv.index("-g") + 1])
            return RunResult(0, "", "")
        if cmd == "useradd":
            name = argv[-1]
            if name in self.users:
                return RunResult(9, "", f"useradd: user '{name}' already exists")
            uid = int(argv[argv.index("--uid") + 1])
            gid = self.groups[argv[argv.index("--gid") + 1]]
            home = argv[argv.index("--home-dir") + 1]
            shell = argv[argv.index("--shell") + 1]
            self.users[name] = pwd.struct_passwd((name, "x", uid, gid, "", home, shell))
            if "--create-home" in argv:
                os.makedirs(home, exist_ok=True)
                self.owners[home] = uid
                if self.skel_dir and os.path.isdir(self.skel_dir):
                    for dirpath, _, filenames in os.walk(self.skel_dir):
                        for f in filenames:
                            src = os.path.join(dirpath, f)
                            rel = os.path.relpath(src, self.skel_dir)
                            dst = os.path.join(home, rel)
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copyfile(src, dst)
                    # useradd chowns everything it copied, directories included
                    for dirpath, dirnames, filenames in os.walk(home):
                        for n in dirnames + filenames:
                            self.owners[os.path.join(dirpath, n)] = uid
            return RunResult(0, "", "")
        if cmd == "userdel":
            name = argv[-1]
            if name in self.busy_once:
                self.busy_once.discard(name)
                return RunResult(8, "", f"userdel: user {name} is currently used by process 4242")
            if name not in self.users:
                return RunResult(6, "", f"userdel: user '{name}' does not exist")
            del self.users[name]
            return RunResult(0, "", "")
        if cmd == "usermod":
            self.supplementary.setdefault(argv[-1], set()).add(argv[argv.index("-aG") + 1])
            return RunResult(0, "", "")
        if cmd == "chpasswd":
            self.stdin.append(input_bytes or b"")
            for line in (input_bytes or b"").decode().splitlines():
                if ":" in line:
                    u, p = line.split(":", 1)
                    self.passwords[u] = p
            return RunResult(0, "", "")
        if cmd == "tar":
            path = argv[argv.index("--file") + 1]
            with open(path, "wb") as fh:
                fh.write(b"fake-tarball")
            return RunResult(0, "", "")
        return RunResult(127, "", f"{cmd}: not emulated")

    def kill_uid_processes(self, uid, grace=5.0):
        self.killed.append(uid)
        return 0

    def geteuid(self):
        return 0

    def now(self):
        return self.clock

    def getpwnam(self, name):
        if name in self.users:
            return self.users[name]
        raise KeyError(name)

    def getpwuid(self, uid):
        for u in self.users.values():
            if u.pw_uid == uid:
                return u
        raise KeyError(uid)

    def getgrnam(self, name):
        if name in self.groups:
            return grp.struct_group((name, "x", self.groups[name], []))
        raise KeyError(name)

    def all_users(self):
        return list(self.users.values())

    def exists(self, path):
        return os.path.lexists(path)

    def isdir(self, path):
        return os.path.isdir(path)

    def islink(self, path):
        return os.path.islink(path)

    def listdir(self, path):
        return os.listdir(path)

    def owner_uid(self, path):
        if path in self.owners:
            return self.owners[path]
        return os.lstat(path).st_uid

    def scandir_owners(self, root):
        out = {}
        for name in os.listdir(root):
            out[name] = self.owner_uid(os.path.join(root, name))
        return out

    def lchown(self, path, uid, gid):
        self.owners[path] = uid

    def chown_tree(self, path, uid, gid):
        self.owners[path] = uid
        for dirpath, dirnames, filenames in os.walk(path):
            for n in dirnames + filenames:
                self.owners[os.path.join(dirpath, n)] = uid

    def chmod(self, path, mode):
        os.chmod(path, mode)

    def mkdir(self, path, mode=0o755):
        os.makedirs(path, mode=mode, exist_ok=True)

    def symlink_replace(self, target, linkpath):
        tmp = linkpath + ".tmp"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(target, tmp)
        os.replace(tmp, linkpath)

    def copy_file(self, src, dst):
        shutil.copyfile(src, dst)

    def read_text(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def write_text(self, path, text, mode=0o644):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def rmtree(self, path):
        shutil.rmtree(path)

    def walk_files(self, root):
        for dirpath, _, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.isfile(full) and not os.path.islink(full):
                    yield os.path.relpath(full, root)
