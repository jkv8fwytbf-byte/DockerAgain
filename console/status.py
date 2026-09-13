"""
Live status for the dashboard: a psutil sampler thread, GET /api/status and
the server start / stop / stop-all actions.

    Sampler        daemon thread; every `interval` seconds it aggregates
                   psutil process data per real uid and host figures into an
                   immutable snapshot dict (see `Sampler.sample`).
    GET  /api/status                 Hub user list (cached 2 s) joined with the
                                     snapshot and the roster's display names
    POST /api/servers/{u}/start      -> {"username", "state": "starting"|"running"}
    POST /api/servers/{u}/stop       -> {"username", "state": "stopping"|"stopped"}
    POST /api/servers/stop-all       -> {"stopped": [...], "failed": [{"username","error"}]}

The status endpoint never fails because of the Hub: when the Hub is down it
answers 200 with hub.ok == false and a roster-based server list.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pwd
import shutil
import threading
import time
import weakref
from datetime import datetime, timezone

import psutil
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .auth import require_admin
from .errors import ApiError, HubError, HubUnavailable
from .roster import Roster, RosterError, validate_username
from .util import iso_from_ts

log = logging.getLogger("console.status")
router = APIRouter()

SERVER_MARKER = "jupyterhub-singleuser"
SKIP_USERS = frozenset({"jovyan"})
HUB_CACHE_TTL = 2.0          # seconds; absorbs several tabs polling at once
INFO_CACHE_TTL = 600.0       # the Hub version hardly changes
INFO_RETRY_TTL = 60.0        # retry a failed version lookup after this long
NAME_CACHE_TTL = 60.0        # uid -> username lookups inside the sampler
STOP_ALL_CONCURRENCY = 4
NO_STORE = {"Cache-Control": "no-store"}
HOST_KEYS = (
    "cpu_pct", "ncpu", "load1", "mem_total", "mem_used", "mem_available", "swap_used",
    "disk_free", "disk_total", "disk_path", "disk_state_free", "disk_shared_free", "uptime_s",
)


# --- sampler -------------------------------------------------------------------


class Sampler:
    """
    Samples the host and every process once per `interval` seconds on a
    daemon thread. `snapshot()` returns the latest sample (treat it as
    read-only) or None before the first tick. Tests set `override` to a
    ready-made snapshot instead of starting the thread.
    """

    def __init__(self, interval: float, home_root: str, state_dir: str, shared_dir: str):
        self.interval = max(0.5, float(interval or 5.0))
        self.paths = {"home": home_root, "state": state_dir, "shared": shared_dir}
        self.override: dict | None = None
        self._snapshot: dict | None = None
        self._procs: dict[int, psutil.Process] = {}
        self._names: dict[int, tuple[float, str | None]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ticks = 0
        self.last_error: str | None = None

    # -- lifecycle --
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="console-sampler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self) -> dict | None:
        return self.override if self.override is not None else self._snapshot

    def invalidate_names(self) -> None:
        """Call after accounts change so uid -> name lookups are refreshed."""
        self._names.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._snapshot = self.sample()
                self.last_error = None
            except Exception as e:  # noqa: BLE001 - the thread must never die
                self.last_error = str(e)
                log.exception("status sampler tick failed")
            self.ticks += 1
            self._stop.wait(max(0.1, self.interval - (time.monotonic() - started)))

    # -- one tick --
    def sample(self) -> dict:
        now = time.time()
        users: dict[int, dict] = {}
        seen: set[int] = set()
        attrs = ["pid", "uids", "cmdline", "memory_info", "name"]
        for proc in psutil.process_iter(attrs=attrs, ad_value=None):
            pid = proc.pid
            seen.add(pid)
            kept = self._procs.get(pid)
            if kept is None or kept != proc:          # new pid, or the pid was reused
                self._procs[pid] = kept = proc
            try:
                cpu = kept.cpu_percent(None)          # since the previous tick on this object
            except psutil.Error:                      # NoSuchProcess, ZombieProcess, AccessDenied
                continue
            info = proc.info or {}
            uids = info.get("uids")
            if uids is None:
                continue
            uid = int(uids.real)
            mem = info.get("memory_info")
            rss = int(getattr(mem, "rss", 0) or 0)
            cmd = info.get("cmdline") or []
            name = info.get("name") or ""
            agg = users.get(uid)
            if agg is None:
                agg = users[uid] = {"cpu_sum": 0.0, "rss_bytes": 0, "nproc": 0, "has_server": False}
            agg["cpu_sum"] += float(cpu or 0.0)
            agg["rss_bytes"] += rss
            agg["nproc"] += 1
            if not agg["has_server"] and (SERVER_MARKER in name or any(SERVER_MARKER in str(part) for part in cmd)):
                agg["has_server"] = True
        for pid in list(self._procs):
            if pid not in seen:
                del self._procs[pid]
        for agg in users.values():
            agg["cpu_sum"] = round(agg["cpu_sum"], 2)

        ncpu = psutil.cpu_count() or 1
        try:
            load1 = float(os.getloadavg()[0])
        except (OSError, AttributeError):
            load1 = None
        vm = psutil.virtual_memory()
        try:
            swap_used = int(psutil.swap_memory().used)
        except (psutil.Error, OSError):
            swap_used = 0
        try:
            boot_time = float(psutil.boot_time())
        except (psutil.Error, OSError):
            boot_time = None
        disks = {}
        for key, path in self.paths.items():
            try:
                du = shutil.disk_usage(path)
                disks[key] = {"total": int(du.total), "used": int(du.used), "free": int(du.free)}
            except OSError:
                disks[key] = None
        host = {
            "cpu_pct": round(float(psutil.cpu_percent(None)), 1),
            "ncpu": int(ncpu),
            "load1": load1,
            "mem_total": int(vm.total),
            "mem_used": int(vm.used),
            "mem_available": int(vm.available),
            "swap_used": swap_used,
            "boot_time": boot_time,
            "uptime_s": int(now - boot_time) if boot_time else None,
            "disks": disks,
            "disk_path": self.paths["home"],
        }
        return {
            "sampled_at": iso_from_ts(now),
            "ts": now,
            "host": host,
            "users": users,
            "names": {uid: self._name_for(uid, now) for uid in users},
        }

    def _name_for(self, uid: int, now: float) -> str | None:
        hit = self._names.get(uid)
        if hit and hit[0] > now:
            return hit[1]
        try:
            name = pwd.getpwuid(uid).pw_name
        except (KeyError, OSError):
            name = None
        self._names[uid] = (now + NAME_CACHE_TTL, name)
        return name


def setup(app) -> None:
    """Start the sampler unless a test already installed one on app.state."""
    if getattr(app.state, "sampler", None) is not None:
        return
    s = app.state.settings
    sampler = Sampler(s.stats_interval, s.home_root, s.state_dir, s.shared_dir)
    sampler.start()
    app.state.sampler = sampler


def teardown(app) -> None:
    sampler = getattr(app.state, "sampler", None)
    stop = getattr(sampler, "stop", None)
    if stop:
        stop()


# --- per-app caches (Hub user list, Hub version) ----------------------------------------


class _AppCache:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.users_expires = 0.0
        self.users: list[dict] | None = None
        self.users_error: str | None = None
        self.info_expires = 0.0
        self.version: str | None = None
        self.names: dict[str, str] = {}      # last good username -> display name map


_caches: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _cache_for(app) -> _AppCache:
    cache = _caches.get(app)
    if cache is None:
        cache = _caches[app] = _AppCache()
    return cache


def invalidate(app) -> None:
    """Forget the cached Hub user list (after a start/stop the next poll must be fresh)."""
    cache = _caches.get(app)
    if cache is not None:
        cache.users_expires = 0.0


async def _hub_users(app) -> tuple[list[dict] | None, str | None]:
    """(users, None) or (None, error text). Cached for HUB_CACHE_TTL, single flight."""
    cache = _cache_for(app)
    now = time.monotonic()
    if cache.users_expires > now:
        return cache.users, cache.users_error
    async with cache.lock:
        now = time.monotonic()
        if cache.users_expires > now:
            return cache.users, cache.users_error
        try:
            users = await app.state.hub.list_users(include_stopped_servers=True)
            cache.users, cache.users_error = users, None
        except HubUnavailable as e:
            cache.users, cache.users_error = None, f"JupyterHub is not answering ({e})."
        except HubError as e:
            cache.users, cache.users_error = None, f"JupyterHub answered with an error ({e.status}): {e.message}"
        cache.users_expires = time.monotonic() + HUB_CACHE_TTL
        return cache.users, cache.users_error


async def _hub_version(app) -> str | None:
    cache = _cache_for(app)
    now = time.monotonic()
    if cache.info_expires > now:
        return cache.version
    try:
        info = await app.state.hub.info()
        cache.version = str(info.get("version") or "") or None
        cache.info_expires = now + INFO_CACHE_TTL
    except Exception as e:  # noqa: BLE001 - best effort only (HubUnavailable, HubError, bad JSON)
        log.debug("hub version lookup failed: %s", e)
        cache.info_expires = now + INFO_RETRY_TTL
    return cache.version


# --- joining Hub, roster and snapshot ---------------------------------------------------


def server_state(user: dict) -> str:
    """starting / stopping / running / stopped from a Hub user model."""
    servers = user.get("servers") or {}
    srv = servers.get("") if isinstance(servers, dict) else None
    pending = (srv or {}).get("pending") or user.get("pending")
    if pending == "spawn":
        return "starting"
    if pending == "stop":
        return "stopping"
    if srv is not None:
        return "running" if srv.get("ready") else "stopped"
    return "running" if user.get("server") else "stopped"


def parse_iso(value) -> float | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _display_names(roster_path: str) -> dict[str, str] | None:
    """
    username -> display name, read under the roster lock: Roster.save() moves
    roster.json to .bak before writing the new file, so an unlocked read in
    that window sees "no roster" and every name would vanish for one poll.
    Runs in a worker thread (the flock may block briefly). None when the
    roster cannot be read at all; the caller then keeps the last good map.
    """
    roster = Roster(roster_path)
    try:
        with roster.lock():
            students = roster.load()
    except RosterError as e:
        log.warning("roster unreadable for the dashboard: %s", e)
        return None
    except OSError as e:                      # the lock file itself could not be created
        log.warning("roster lock unavailable for the dashboard: %s", e)
        return None
    return {s.username: s.display_name for s in students}


def _lookup_uid(name: str, getpwnam, names_by_uid: dict) -> int | None:
    try:
        return int(getpwnam(name).pw_uid)
    except (KeyError, OSError, AttributeError, TypeError, ValueError):
        pass
    for uid, uname in names_by_uid.items():
        if uname == name:
            try:
                return int(uid)
            except (TypeError, ValueError):
                return None
    return None


def _host_payload(snapshot: dict | None, home_root: str, now: float) -> dict:
    host: dict = {k: None for k in HOST_KEYS}
    host["disk_path"] = home_root
    if not snapshot:
        return host
    h = snapshot.get("host") or {}
    disks = h.get("disks") or {}
    home = disks.get("home") or {}
    state = disks.get("state") or {}
    shared = disks.get("shared") or {}
    boot = h.get("boot_time")
    host.update({
        "cpu_pct": h.get("cpu_pct"),
        "ncpu": h.get("ncpu"),
        "load1": h.get("load1"),
        "mem_total": h.get("mem_total"),
        "mem_used": h.get("mem_used"),
        "mem_available": h.get("mem_available"),
        "swap_used": h.get("swap_used"),
        "disk_free": home.get("free"),
        "disk_total": home.get("total"),
        "disk_path": h.get("disk_path") or home_root,
        "disk_state_free": state.get("free"),
        "disk_shared_free": shared.get("free"),
        "uptime_s": int(now - boot) if boot else h.get("uptime_s"),
    })
    return host


def _usage_for(uid: int | None, snapshot: dict | None, ncpu: int) -> dict:
    agg = (snapshot or {}).get("users", {}).get(uid) if uid is not None else None
    if not agg:
        return {"cpu_pct": 0.0, "mem_bytes": 0, "nproc": 0, "has_server_process": False}
    cpu = float(agg.get("cpu_sum") or 0.0) / max(1, int(ncpu or 1))
    return {
        "cpu_pct": round(min(100.0, max(0.0, cpu)), 1),
        "mem_bytes": int(agg.get("rss_bytes") or 0),
        "nproc": int(agg.get("nproc") or 0),
        "has_server_process": bool(agg.get("has_server")),
    }


def _build_servers(app, hub_users: list[dict] | None, snapshot: dict | None, names: dict[str, str], now: float) -> list[dict]:
    settings = app.state.settings
    sysobj = getattr(app.state.accounts, "sys", None)
    getpwnam = getattr(sysobj, "getpwnam", None) or pwd.getpwnam
    names_by_uid = (snapshot or {}).get("names") or {}
    ncpu = int(((snapshot or {}).get("host") or {}).get("ncpu") or 1)
    rows: list[dict] = []
    if hub_users is not None:
        for u in hub_users:
            name = str(u.get("name") or "").strip()
            if not name or name in SKIP_USERS:
                continue
            state = server_state(u)
            srv = (u.get("servers") or {}).get("") if isinstance(u.get("servers"), dict) else None
            activity = (srv or {}).get("last_activity") or u.get("last_activity")
            ts = parse_iso(activity)
            url = (srv or {}).get("url") or u.get("server")
            row = {
                "username": name,
                "display_name": names.get(name, ""),
                "is_admin": bool(u.get("admin")),
                "state": state,
                "last_activity": activity,
                "idle_seconds": int(max(0.0, now - ts)) if ts is not None else None,
                "url": url if state in ("running", "starting", "stopping") and url else None,
                "error": None,
            }
            row.update(_usage_for(_lookup_uid(name, getpwnam, names_by_uid), snapshot, ncpu))
            rows.append(row)
        return rows
    for username, display in names.items():
        if username in SKIP_USERS:
            continue
        row = {
            "username": username,
            "display_name": display,
            "is_admin": username in settings.admin_users,
            "state": "unknown",
            "last_activity": None,
            "idle_seconds": None,
            "url": None,
            "error": None,
        }
        row.update(_usage_for(_lookup_uid(username, getpwnam, names_by_uid), snapshot, ncpu))
        rows.append(row)
    return rows


@router.get("/api/status")
async def get_status(request: Request, user: str = Depends(require_admin)):
    app = request.app
    settings = app.state.settings
    sampler = getattr(app.state, "sampler", None)
    snapshot = sampler.snapshot() if sampler is not None else None
    now = time.time()
    hub_users, hub_error = await _hub_users(app)
    version = await _hub_version(app) if hub_users is not None else _cache_for(app).version
    cache = _cache_for(app)
    names = await asyncio.to_thread(_display_names, settings.roster_path)
    if names is None:
        names = cache.names                   # unreadable right now: keep the last good names
    else:
        cache.names = names
    servers = _build_servers(app, hub_users, snapshot, names, now)
    students = [s for s in servers if not s["is_admin"]]
    body = {
        "sampled_at": (snapshot or {}).get("sampled_at"),
        "host": _host_payload(snapshot, settings.home_root, now),
        "hub": {
            "ok": hub_users is not None,
            "error": hub_error,
            "jupyterhub": version,
            "image_version": settings.image_version,
        },
        "students": {
            "online": sum(1 for s in students if s["state"] == "running"),
            "total": len(students),
        },
        "servers": servers,
    }
    return JSONResponse(body, headers=NO_STORE)


# --- server controls -----------------------------------------------------------------


def _clean_username(raw: str) -> str:
    try:
        return validate_username(raw)
    except ValueError as e:
        raise ApiError(400, "invalid_username", f"That is not a valid username: {e}.") from None


def _hub_action_error(name: str, e: HubError) -> ApiError:
    if e.status == 404:
        return ApiError(404, "unknown_user", f"JupyterHub does not know a student called '{name}'.")
    if e.status == 400:
        text = (e.message or "").lower()
        if "pending" in text or "in the process" in text or "please wait" in text:
            return ApiError(409, "server_busy", f"{name}'s server is still changing state. Wait a few seconds and try again.")
        return ApiError(409, "server_state", f"JupyterHub refused: {e.message}")
    return ApiError(502, "hub_error", f"JupyterHub answered with an error ({e.status}): {e.message}", {"hub_status": e.status})


@router.post("/api/servers/{username}/start")
async def start_server(request: Request, username: str, user: str = Depends(require_admin)):
    name = _clean_username(username)
    try:
        state = await request.app.state.hub.start_server(name)
    except HubError as e:
        raise _hub_action_error(name, e) from None
    invalidate(request.app)
    log.info("%s started the server of %s (%s)", user, name, state)
    return JSONResponse({"username": name, "state": state}, headers=NO_STORE)


@router.post("/api/servers/{username}/stop")
async def stop_server(request: Request, username: str, user: str = Depends(require_admin)):
    name = _clean_username(username)
    try:
        state = await request.app.state.hub.stop_server(name)
    except HubError as e:
        raise _hub_action_error(name, e) from None
    invalidate(request.app)
    log.info("%s stopped the server of %s (%s)", user, name, state)
    return JSONResponse({"username": name, "state": state}, headers=NO_STORE)


class StopAllBody(BaseModel):
    include_admins: bool = False


@router.post("/api/servers/stop-all")
async def stop_all(request: Request, body: StopAllBody | None = None, user: str = Depends(require_admin)):
    include_admins = bool(body and body.include_admins)
    hub = request.app.state.hub
    users = await hub.list_users(include_stopped_servers=True)      # HubUnavailable -> 503, HubError -> 502
    targets = []
    for u in users:
        name = str(u.get("name") or "").strip()
        if not name or name in SKIP_USERS:
            continue
        if u.get("admin") and not include_admins:
            continue
        if server_state(u) in ("stopped", "stopping"):
            continue
        targets.append(name)
    stopped: list[str] = []
    failed: list[dict] = []
    gate = asyncio.Semaphore(STOP_ALL_CONCURRENCY)

    async def one(name: str):
        async with gate:
            try:
                await hub.stop_server(name)
                stopped.append(name)
            except HubError as e:
                failed.append({"username": name, "error": _hub_action_error(name, e).message})
            except HubUnavailable as e:
                failed.append({"username": name, "error": f"JupyterHub stopped answering ({e})."})

    await asyncio.gather(*(one(n) for n in targets))
    invalidate(request.app)
    stopped.sort()
    failed.sort(key=lambda f: f["username"])
    log.info("%s stopped all servers: %d stopped, %d failed", user, len(stopped), len(failed))
    return JSONResponse({"stopped": stopped, "failed": failed}, headers=NO_STORE)
