"""
Thin, injectable wrapper around every operating-system call the account
module makes. Tests replace it with a fake so the account logic can be
exercised without root and without touching /etc/passwd.
"""
from __future__ import annotations

import grp
import os
import pwd
import shutil
import subprocess
import time
from dataclasses import dataclass


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class System:
    """Real implementation. Every method is small so the fake can mirror it."""

    # --- processes --------------------------------------------------------
    def run(self, argv, *, input_bytes: bytes | None = None, timeout: float = 30) -> RunResult:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell string
            list(argv), input=input_bytes, capture_output=True, timeout=timeout, check=False
        )
        return RunResult(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    def kill_uid_processes(self, uid: int, grace: float = 5.0) -> int:
        """SIGTERM then SIGKILL every process owned by uid. Returns count."""
        import psutil  # present in the image; imported lazily for the tests

        procs = []
        for p in psutil.process_iter(attrs=["uids"]):
            try:
                if p.info["uids"] and p.info["uids"].real == uid:
                    procs.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=grace)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        return len(procs)

    def geteuid(self) -> int:
        return os.geteuid()

    def now(self) -> float:
        return time.time()

    # --- accounts ---------------------------------------------------------
    def getpwnam(self, name: str):
        return pwd.getpwnam(name)

    def getpwuid(self, uid: int):
        return pwd.getpwuid(uid)

    def getgrnam(self, name: str):
        return grp.getgrnam(name)

    def all_users(self):
        return pwd.getpwall()

    # --- files ------------------------------------------------------------
    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    def isdir(self, path: str) -> bool:
        return os.path.isdir(path)

    def islink(self, path: str) -> bool:
        return os.path.islink(path)

    def listdir(self, path: str):
        return os.listdir(path)

    def owner_uid(self, path: str) -> int:
        return os.lstat(path).st_uid

    def scandir_owners(self, root: str) -> dict[str, int]:
        """{entry name: owner uid} for the top level of root (no recursion)."""
        out = {}
        try:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        out[entry.name] = entry.stat(follow_symlinks=False).st_uid
                    except OSError:
                        continue
        except OSError:
            pass
        return out

    def lchown(self, path: str, uid: int, gid: int) -> None:
        os.lchown(path, uid, gid)

    def chown_tree(self, path: str, uid: int, gid: int) -> None:
        os.lchown(path, uid, gid)
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            for name in dirnames + filenames:
                try:
                    os.lchown(os.path.join(dirpath, name), uid, gid)
                except OSError:
                    continue

    def chmod(self, path: str, mode: int) -> None:
        os.chmod(path, mode)

    def mkdir(self, path: str, mode: int = 0o755) -> None:
        os.makedirs(path, mode=mode, exist_ok=True)

    def symlink_replace(self, target: str, linkpath: str) -> None:
        """Atomically (re)point linkpath at target."""
        tmp = f"{linkpath}.tmp-{os.getpid()}"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(target, tmp)
        os.replace(tmp, linkpath)

    def copy_file(self, src: str, dst: str) -> None:
        shutil.copyfile(src, dst)

    def read_text(self, path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def write_text(self, path: str, text: str, mode: int = 0o644) -> None:
        tmp = f"{path}.tmp-{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)

    def rmtree(self, path: str) -> None:
        shutil.rmtree(path)

    def walk_files(self, root: str):
        """Relative paths of regular files under root (symlinks skipped)."""
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.isfile(full) and not os.path.islink(full):
                    yield os.path.relpath(full, root)
