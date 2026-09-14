"""
Test doubles shared by the unit suite, the API suite and the Docker-free demo.

FakeSystem: emulates useradd/userdel/usermod/chpasswd/tar against an in-memory
passwd table while using a real temporary directory for home folders
(ownership is tracked in a dict because tests are not root).

FakeHub: in-memory JupyterHub REST API (users, servers, OAuth code exchange).
"""
from __future__ import annotations

import grp
import os
import pwd
import shutil
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx

from console.system import RunResult

DEFAULT_HUB_TOKEN = "service-token-xyz"


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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

    def read_bytes(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def remove_file(self, path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def write_home_file(self, home, rel, data, uid, gid, mode=0o644, dir_mode=0o750):
        # Use the real, fd-based implementation (it only calls os.*); just record ownership.
        from console.system import System

        written = System.write_home_file(self, home, rel, data, uid, gid, mode, dir_mode)
        if written:
            path = home
            for part in rel.split("/"):
                path = os.path.join(path, part)
                self.owners[path] = uid
        return written

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


class FakeHub:
    """In-memory JupyterHub API: users, servers, OAuth code exchange."""

    def __init__(self, service_token: str = DEFAULT_HUB_TOKEN):
        self.service_token = service_token
        self.users: dict[str, dict] = {}
        self.codes: dict[str, str] = {}          # code -> username
        self.tokens: dict[str, str] = {}         # access token -> username
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.pending_stop: set[str] = set()

    def add(self, name: str, admin: bool = False, running: bool = False, last_activity: str | None = None):
        self.users[name] = {
            "kind": "user", "name": name, "admin": admin, "groups": [], "roles": ["admin" if admin else "user"],
            "server": f"/user/{name}/" if running else None, "pending": None, "last_activity": last_activity,
            "servers": {"": {"name": "", "ready": running, "pending": None, "stopped": not running,
                              "started": last_activity if running else None, "last_activity": last_activity,
                              "url": f"/user/{name}/"}},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.down:
            raise httpx.ConnectError("hub down")
        path = request.url.path.replace("/hub/api", "", 1)
        auth = request.headers.get("Authorization", "")
        if path == "/oauth2/token" and request.method == "POST":
            form = parse_qs(request.content.decode())
            if form.get("client_secret", [""])[0] != self.service_token or form.get("grant_type") != ["authorization_code"]:
                return httpx.Response(400, json={"message": "bad client"})
            code = form.get("code", [""])[0]
            if code not in self.codes:
                return httpx.Response(400, json={"message": "invalid code"})
            token = f"tok-{code}"
            self.tokens[token] = self.codes.pop(code)
            return httpx.Response(200, json={"access_token": token, "token_type": "Bearer"})
        if path == "/user":
            name = self.tokens.get(auth.replace("token ", ""))
            if not name:
                return httpx.Response(403, json={"message": "invalid token"})
            return httpx.Response(200, json=dict(self.users[name], scopes=["identify"]))
        if auth != f"token {self.service_token}":
            return httpx.Response(403, json={"message": "service token required"})
        if path == "/users" and request.method == "GET":
            items = list(self.users.values())
            offset = int(request.url.params.get("offset", 0)); limit = int(request.url.params.get("limit", 50))
            page = items[offset:offset + limit]
            nxt = {"offset": offset + limit, "limit": limit} if offset + limit < len(items) else None
            return httpx.Response(200, json={"items": page, "_pagination": {"offset": offset, "limit": limit, "total": len(items), "next": nxt}})
        if path.startswith("/users/"):
            parts = path.split("/")[2:]
            name = parts[0]
            if len(parts) == 1:
                if request.method == "GET":
                    return httpx.Response(200, json=self.users[name]) if name in self.users else httpx.Response(404, json={"message": "not found"})
                if request.method == "POST":
                    if name in self.users:
                        return httpx.Response(409, json={"message": "exists"})
                    self.add(name)
                    return httpx.Response(201, json=self.users[name])
                if request.method == "DELETE":
                    if name not in self.users:
                        return httpx.Response(404, json={"message": "not found"})
                    if name in self.pending_stop:
                        self.pending_stop.discard(name)
                        return httpx.Response(400, json={"message": f"{name}'s server is in the process of stopping, please wait."})
                    del self.users[name]
                    return httpx.Response(204)
            if len(parts) == 2 and parts[1] == "server":
                if name not in self.users:
                    return httpx.Response(404, json={"message": "not found"})
                u = self.users[name]
                if request.method == "POST":
                    if u["server"]:
                        return httpx.Response(400, json={"message": f"{name} is already running"})
                    stamp = _iso_now()
                    u["server"] = f"/user/{name}/"
                    u["last_activity"] = stamp
                    u["pending"] = None
                    u["servers"][""].update(ready=True, stopped=False, pending=None, last_activity=stamp, started=stamp)
                    return httpx.Response(201)
                if request.method == "DELETE":
                    if not u["server"]:
                        return httpx.Response(400, json={"message": f"{name} is not running"})
                    u["server"] = None
                    u["pending"] = None
                    u["servers"][""].update(ready=False, stopped=True, pending=None)
                    return httpx.Response(204)
        if path == "/info":
            return httpx.Response(200, json={"version": "5.3.0"})
        return httpx.Response(404, json={"message": f"no route {path}"})
