"""
Students: the roster, the Linux accounts and the Hub registrations behind one
JSON API, plus the printable login-cards page.

Fixed orders (see the plan):
    add      validate -> conflict check -> Linux account -> roster -> Hub user
    remove   refuse admins/self -> stop server -> wait -> delete Hub user
             -> Linux account + home archive -> roster
The roster is the source of truth; the Hub is derived state that the Hub
config rebuilds from the roster at every start, so a failed Hub call never
loses an account (it is reported as a warning and fixed by Repair).

Blocking work (accounts, roster, tar) runs in a worker thread that holds the
roster lock; Hub calls are awaited on the event loop before/after.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import time
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from . import cards
from .accounts import AccountError
from .auth import require_admin
from .errors import ApiError, HubError, HubUnavailable
from .roster import (
    Roster,
    RosterError,
    Student,
    generate_password,
    parse_bulk_text,
    parse_users_txt,
    suggest_username,
    validate_display_name,
    validate_password,
    validate_username,
)
from .util import read_branding

log = logging.getLogger("console.students")
router = APIRouter()

# Removal timings (seconds). Tests set the intervals/timeouts to ~0.
STOP_POLL_INTERVAL = 1.0
STOP_POLL_TIMEOUT = 30.0
DELETE_RETRY_INTERVAL = 2.0
DELETE_RETRY_TIMEOUT = 30.0

BULK_MAX_BYTES = 1024 * 1024       # pasted text or uploaded .txt/.csv
BULK_MAX_ROWS = 1000
PRINT_MAX_CARDS = 500

HUB_UNREACHABLE_MSG = "JupyterHub is not answering right now."


# --- wiring -------------------------------------------------------------------------

def setup(app) -> None:
    app.state.students_roster = Roster(app.state.settings.roster_path)


def _roster(request: Request) -> Roster:
    app = request.app
    roster = getattr(app.state, "students_roster", None)
    if roster is None or roster.path != app.state.settings.roster_path:
        roster = Roster(app.state.settings.roster_path)
        app.state.students_roster = roster
    return roster


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def _load(roster: Roster) -> list[Student]:
    try:
        return roster.load()
    except RosterError as e:
        log.error("roster unreadable: %s", e)
        raise ApiError(500, "roster_unreadable", "The class list file cannot be read. Check the Logs page.") from e


def _save(roster: Roster, students: list[Student]) -> None:
    try:
        roster.save(students)
    except RosterError as e:
        log.error("roster unwritable: %s", e)
        raise ApiError(500, "roster_write_failed", "The class list could not be saved. Check the Logs page.") from e


def _username(raw: str) -> str:
    try:
        return validate_username(raw)
    except ValueError as e:
        raise ApiError(400, "invalid_username", f"That is not a valid username: {e}.") from e


def _display_name(raw: str) -> str:
    try:
        return validate_display_name(raw or "")
    except ValueError as e:
        raise ApiError(400, "invalid_name", f"That name cannot be used: {e}.") from e


def _password(raw: str | None) -> str:
    pw = raw if raw is not None else ""
    if pw == "":
        return generate_password()
    try:
        return validate_password(pw)
    except ValueError as e:
        raise ApiError(400, "invalid_password", f"That password cannot be used: {e}.") from e


def _linux_names(accounts) -> set[str]:
    try:
        return {pw.pw_name for pw in accounts.sys.all_users()}
    except Exception:  # noqa: BLE001 - pwd lookups should never break the console
        return set()


def _has_active_server(model: dict | None) -> bool:
    if not model:
        return False
    if model.get("server") or model.get("pending"):
        return True
    for srv in (model.get("servers") or {}).values():
        if srv.get("ready") or srv.get("pending"):
            return True
    return False


def hub_state(model: dict | None) -> str:
    """starting / stopping / running / stopped from a Hub user model."""
    if not model:
        return "stopped"
    default = (model.get("servers") or {}).get("") or {}
    pending = model.get("pending") or default.get("pending")
    if pending == "spawn":
        return "starting"
    if pending == "stop":
        return "stopping"
    if model.get("server") or default.get("ready"):
        return "running"
    return "stopped"


def _student_out(st: Student, *, admins: set[str], status: dict, hub_model: dict | None, hub_known: bool | None,
                 reveal: bool, state: str | None = None) -> dict:
    out = {
        "username": st.username,
        "display_name": st.display_name,
        "created": st.created,
        "is_admin": st.username in admins or bool(hub_model and hub_model.get("admin")),
        "uid": status.get("uid"),
        "linux_ok": bool(status.get("linux_ok")),
        "home_ok": bool(status.get("home_ok")),
        "hub_ok": hub_known,
        "state": state if state is not None else ("unknown" if hub_known is None else hub_state(hub_model)),
    }
    if reveal:
        out["password"] = st.password
    return out


# --- models -------------------------------------------------------------------------

class StudentCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    display_name: str = Field(default="", max_length=1000)
    username: str | None = Field(default=None, max_length=1000)
    password: str | None = Field(default=None, max_length=1000)


class BulkRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    display_name: str = Field(default="", max_length=1000)
    username: str = Field(default="", max_length=1000)
    password: str = Field(default="", max_length=1000)
    line: int | None = None


class BulkRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rows: list[BulkRow] = Field(default_factory=list, max_length=BULK_MAX_ROWS)
    on_conflict: Literal["skip", "reset_password"] = "skip"


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    on_conflict: Literal["skip", "reset_password"] = "skip"


class PasswordBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    password: str | None = Field(default=None, max_length=1000)


# --- list -------------------------------------------------------------------------------

@router.get("/api/students")
async def list_students(request: Request, response: Response, reveal: int = 0, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    accounts = request.app.state.accounts
    hub = request.app.state.hub
    roster = _roster(request)

    def load():
        with roster.lock():
            students = _load(roster)
        return students, {s.username: accounts.status(s.username) for s in students}

    students, statuses = await asyncio.to_thread(load)
    hub_users: dict[str, dict] = {}
    hub_error = None
    try:
        hub_users = {u.get("name"): u for u in await hub.list_users() if u.get("name")}
    except HubUnavailable as e:
        log.warning("student list: Hub unreachable: %s", e)
        hub_error = HUB_UNREACHABLE_MSG + " Server statuses are unknown until it is back."
    except HubError as e:
        log.warning("student list: Hub error: %s", e)
        hub_error = f"JupyterHub answered with an error ({e.status}); server statuses are unknown."
    admins = set(settings.admin_users) | {n for n, m in hub_users.items() if m.get("admin")}
    out = []
    for st in students:
        model = hub_users.get(st.username)
        known = None if hub_error else (model is not None)
        out.append(_student_out(st, admins=admins, status=statuses[st.username], hub_model=model, hub_known=known, reveal=bool(reveal)))
    _no_store(response)
    return {"students": out, "admins": sorted(admins), "hub_error": hub_error}


# --- helpers for the Add modal ----------------------------------------------------------------

@router.get("/api/students/suggest-username")
async def suggest(request: Request, response: Response, name: str = "", user: str = Depends(require_admin)):
    accounts = request.app.state.accounts
    roster = _roster(request)
    name = name[:200]

    def work():
        with roster.lock():
            students = _load(roster)
        taken = {s.username for s in students} | _linux_names(accounts)
        return suggest_username(name, taken)

    _no_store(response)
    return {"username": await asyncio.to_thread(work)}


@router.get("/api/students/generate-password")
async def new_password(response: Response, user: str = Depends(require_admin)):
    _no_store(response)
    return {"password": generate_password()}


# --- add one ----------------------------------------------------------------------------------

@router.post("/api/students", status_code=201)
async def add_student(body: StudentCreate, request: Request, response: Response, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    accounts = request.app.state.accounts
    hub = request.app.state.hub
    roster = _roster(request)

    display_name = _display_name(body.display_name)
    username_raw = (body.username or "").strip()
    if not display_name and not username_raw:
        raise ApiError(400, "invalid_name", "Enter the student's name.")
    username = _username(username_raw) if username_raw else None
    password = _password(body.password)
    warnings: list[str] = []

    # 1. conflicts we can see locally (roster, Linux); also picks the username when none was given
    def check_local():
        nonlocal username
        with roster.lock():
            students = _load(roster)
        roster_names = {s.username for s in students}
        linux_names = _linux_names(accounts)
        if username is None:
            username = suggest_username(display_name, roster_names | linux_names)
        if username in roster_names:
            raise ApiError(409, "exists", f"'{username}' is already in the class list.", {"where": "roster", "username": username})
        if accounts.user_exists(username):
            raise ApiError(409, "exists", f"An account named '{username}' already exists on this server but is not in the class list. Use Repair or pick another username.", {"where": "linux", "username": username})

    await asyncio.to_thread(check_local)

    # 2. conflict on the Hub side (a Hub that is down is tolerated: the account still gets created)
    try:
        if await hub.get_user(username):
            raise ApiError(409, "exists", f"'{username}' is already registered with JupyterHub but not in the class list.", {"where": "hub", "username": username})
    except HubUnavailable as e:
        log.warning("add %s: Hub unreachable during conflict check: %s", username, e)
    except HubError as e:
        log.warning("add %s: Hub error during conflict check: %s", username, e)

    # 3. Linux account first (the Hub refuses users without one), then the roster
    def create_locked():
        with roster.lock():
            students = _load(roster)
            if Roster.find(students, username):
                raise ApiError(409, "exists", f"'{username}' is already in the class list.", {"where": "roster", "username": username})
            if accounts.user_exists(username):
                raise ApiError(409, "exists", f"An account named '{username}' already exists on this server.", {"where": "linux", "username": username})
            try:
                outcome = accounts.create(username, password)
            except AccountError as e:
                log.error("add %s: %s", username, e)
                raise ApiError(500, "account_command_failed", f"The account could not be created: {e}") from e
            if username in settings.admin_users:
                try:
                    accounts.ensure_admin(username)
                except AccountError as e:
                    warnings.append(f"Could not add '{username}' to the admins group: {e}")
            st = Roster.new_student(username, password, display_name)
            students.append(st)
            try:
                _save(roster, students)
            except ApiError:
                # Leave nothing half-done: drop the account again (the home only if we made it).
                try:
                    accounts.remove(username, archive=False, delete_home=outcome.home_created)
                except AccountError:
                    log.exception("add %s: roster save failed and the account could not be rolled back", username)
                raise
            return st, outcome

    st, outcome = await asyncio.to_thread(create_locked)

    # 4. Hub registration (409 = already there = fine)
    hub_registered = False
    try:
        await hub.create_user(username)
        hub_registered = True
    except HubUnavailable as e:
        log.warning("add %s: Hub unreachable, not registered yet: %s", username, e)
        warnings.append("JupyterHub is not answering, so the account is not registered with it yet. It will be registered at the next restart or via Repair.")
    except HubError as e:
        log.warning("add %s: Hub refused registration: %s", username, e)
        warnings.append(f"JupyterHub refused to register the account ({e.status}: {e.message}). It will be registered at the next restart or via Repair.")

    log.info("student '%s' added by %s (uid %s, hub=%s)", username, user, outcome.uid, hub_registered)
    _no_store(response)
    student = _student_out(
        st, admins=set(settings.admin_users), status={"uid": outcome.uid, "linux_ok": True, "home_ok": True},
        hub_model=None, hub_known=hub_registered, reveal=True, state="stopped",
    )
    return {"student": student, "hub_registered": hub_registered, "warnings": warnings}


# --- bulk add ---------------------------------------------------------------------------------

def _rows_from_csv(text: str) -> str:
    """Quoted CSV cells (e.g. "Sharma, Priya") -> plain 'name, username, password' lines."""
    if '"' not in text:
        return text
    lines = []
    try:
        for cells in csv.reader(io.StringIO(text)):
            cells = [c.strip().replace(",", " ") for c in cells]
            lines.append(", ".join(cells[:3]) if len(cells) <= 3 else ",".join(cells))
    except csv.Error:
        return text
    return "\n".join(lines)


async def _read_bulk_text(request: Request) -> str:
    ctype = request.headers.get("content-type", "").lower()
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            text = form.get("text")
            if isinstance(text, str):
                return text[:BULK_MAX_BYTES]
            raise ApiError(400, "no_file", "Choose a .txt or .csv file, or paste the list instead.")
        data = await upload.read(BULK_MAX_BYTES + 1)
        if len(data) > BULK_MAX_BYTES:
            raise ApiError(413, "too_large", "That file is larger than 1 MB. A class list should be a small text file.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise ApiError(400, "bad_encoding", "That file is not plain UTF-8 text. Save it as CSV (UTF-8) or paste the names instead.") from e
        name = (getattr(upload, "filename", "") or "").lower()
        return _rows_from_csv(text) if name.endswith(".csv") else text
    body = await request.body()
    if len(body) > BULK_MAX_BYTES + 4096:
        raise ApiError(413, "too_large", "That list is larger than 1 MB.")
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, UnicodeDecodeError) as e:
        raise ApiError(400, "validation_failed", 'Send JSON with a "text" field or upload a file.') from e
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str):
        raise ApiError(400, "validation_failed", 'Send JSON with a "text" field or upload a file.')
    return text


def _preview_rows(text: str, roster: Roster, accounts) -> dict:
    text = text.replace("\x00", "")
    rows, parse_errors = parse_bulk_text(text)
    with roster.lock():
        students = _load(roster)
    roster_names = {s.username for s in students}
    linux_names = _linux_names(accounts)
    seen: set[str] = set()
    out = []
    for r in rows[:BULK_MAX_ROWS]:
        display, uname, pw = r["display_name"], r["username"], r["password"]
        reason = None
        try:
            display = validate_display_name(display)
        except ValueError as e:
            reason = str(e)
        if not reason:
            if uname:
                try:
                    uname = validate_username(uname)
                except ValueError as e:
                    reason = f"username: {e}"
            else:
                uname = suggest_username(display, roster_names | linux_names | seen)
        if not reason and uname in seen:
            reason = f"'{uname}' appears more than once in this list"
        if not reason:
            if pw:
                try:
                    pw = validate_password(pw)
                except ValueError as e:
                    reason = f"password: {e}"
            else:
                pw = generate_password()
        if reason:
            result = "invalid"
        elif uname in roster_names:
            result, reason = "exists", "already in the class list"
        elif uname in linux_names:
            result, reason = "exists", "an account with this username exists on the server but is not in the class list"
        else:
            result = "new"
        if uname and result != "invalid":
            seen.add(uname)
        out.append({"line": r["line"], "display_name": display, "username": uname, "password": pw, "result": result, "reason": reason})
    lines = text.splitlines()
    for lineno, reason in parse_errors:
        raw = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
        out.append({"line": lineno, "display_name": raw[:64], "username": "", "password": "", "result": "invalid", "reason": reason})
    out.sort(key=lambda r: r["line"])
    summary = {k: sum(1 for r in out if r["result"] == k) for k in ("new", "exists", "invalid")}
    if len(rows) > BULK_MAX_ROWS:
        summary["truncated"] = len(rows) - BULK_MAX_ROWS
    return {"rows": out, "summary": summary}


@router.post("/api/students/bulk/preview")
async def bulk_preview(request: Request, response: Response, user: str = Depends(require_admin)):
    text = await _read_bulk_text(request)
    result = await asyncio.to_thread(_preview_rows, text, _roster(request), request.app.state.accounts)
    _no_store(response)
    return result


async def _commit_rows(request: Request, rows: list[dict], on_conflict: str, actor: str) -> dict:
    settings = request.app.state.settings
    accounts = request.app.state.accounts
    hub = request.app.state.hub
    roster = _roster(request)
    report: dict = {"added": [], "updated": [], "skipped": [], "errors": [], "hub_failures": []}
    to_register: list[str] = []

    def work():
        with roster.lock():
            students = _load(roster)
            by_name = {s.username: s for s in students}
            linux_names = _linux_names(accounts)
            seen: set[str] = set()
            changed = False

            def err(line, uname, reason):
                report["errors"].append({"line": line, "username": uname, "reason": reason})

            try:
                for r in rows:
                    line = r.get("line")
                    try:
                        display = validate_display_name(r.get("display_name") or "")
                    except ValueError as e:
                        err(line, "", str(e))
                        continue
                    uname_raw = (r.get("username") or "").strip()
                    if uname_raw:
                        try:
                            uname = validate_username(uname_raw)
                        except ValueError as e:
                            err(line, uname_raw[:32], f"username: {e}")
                            continue
                    elif display:
                        uname = suggest_username(display, set(by_name) | linux_names | seen)
                    else:
                        err(line, "", "no name or username")
                        continue
                    if uname in seen:
                        err(line, uname, "listed twice in this batch")
                        continue
                    seen.add(uname)
                    pw_raw = r.get("password") or ""
                    try:
                        pw = validate_password(pw_raw) if pw_raw else generate_password()
                    except ValueError as e:
                        err(line, uname, f"password: {e}")
                        continue
                    existing = by_name.get(uname)
                    linux_exists = accounts.user_exists(uname)
                    if existing is not None or linux_exists:
                        if on_conflict != "reset_password":
                            report["skipped"].append(uname)
                            continue
                        try:
                            if linux_exists:
                                accounts.set_password(uname, pw)
                            else:
                                accounts.create(uname, pw)
                        except AccountError as e:
                            err(line, uname, str(e))
                            continue
                        if existing is None:                       # adopt an account that was not in the list
                            existing = Roster.new_student(uname, pw, display)
                            students.append(existing)
                            by_name[uname] = existing
                        else:
                            existing.password = pw
                            if display and not existing.display_name:
                                existing.display_name = display
                        changed = True
                        report["updated"].append(uname)
                        to_register.append(uname)
                        continue
                    try:
                        accounts.create(uname, pw)
                    except AccountError as e:
                        err(line, uname, f"could not create the account: {e}")
                        continue
                    if uname in settings.admin_users:
                        try:
                            accounts.ensure_admin(uname)
                        except AccountError as e:
                            err(line, uname, f"added, but not to the admins group: {e}")
                    st = Roster.new_student(uname, pw, display)
                    students.append(st)
                    by_name[uname] = st
                    linux_names.add(uname)
                    changed = True
                    report["added"].append(uname)
                    to_register.append(uname)
            finally:
                if changed:
                    _save(roster, students)

    await asyncio.to_thread(work)

    hub_down = False
    for uname in to_register:
        if hub_down:
            report["hub_failures"].append(uname)
            continue
        try:
            await hub.create_user(uname)
        except HubUnavailable as e:
            log.warning("bulk: Hub unreachable while registering %s: %s", uname, e)
            hub_down = True
            report["hub_failures"].append(uname)
        except HubError as e:
            log.warning("bulk: Hub refused %s: %s", uname, e)
            report["hub_failures"].append(uname)
    log.info("bulk by %s: %d added, %d updated, %d skipped, %d errors, %d hub failures", actor,
             len(report["added"]), len(report["updated"]), len(report["skipped"]), len(report["errors"]), len(report["hub_failures"]))
    return report


@router.post("/api/students/bulk")
async def bulk_commit(body: BulkRequest, request: Request, response: Response, user: str = Depends(require_admin)):
    rows = [r.model_dump() for r in body.rows]
    if not rows:
        raise ApiError(400, "empty", "There is nothing to add: the list is empty.")
    result = await _commit_rows(request, rows, body.on_conflict, user)
    _no_store(response)
    return result


@router.post("/api/students/import-users-txt")
async def import_users_txt(request: Request, response: Response, body: ImportRequest | None = None, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    path = settings.users_txt

    def read():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read(BULK_MAX_BYTES)
        except OSError:
            return None

    text = await asyncio.to_thread(read)
    if text is None:
        raise ApiError(404, "users_txt_missing", "There is no users.txt on this server (it is optional). Use Bulk add instead.")
    parsed, parse_errors = parse_users_txt(text)
    rows = [{"display_name": "", "username": s.username, "password": s.password, "line": None} for s in parsed]
    on_conflict = body.on_conflict if body else "skip"
    result = await _commit_rows(request, rows, on_conflict, user) if rows else {"added": [], "updated": [], "skipped": [], "errors": [], "hub_failures": []}
    result["errors"] = [{"line": ln, "username": "", "reason": reason} for ln, reason in parse_errors] + result["errors"]
    result["file"] = os.path.basename(path)
    _no_store(response)
    return result


# --- one student ------------------------------------------------------------------------------

@router.post("/api/students/{username}/password")
async def reset_password(username: str, request: Request, response: Response, body: PasswordBody | None = None,
                         user: str = Depends(require_admin)):
    accounts = request.app.state.accounts
    roster = _roster(request)
    username = _username(username)
    password = _password(body.password if body else None)

    def work():
        with roster.lock():
            students = _load(roster)
            st = Roster.find(students, username)
            if st is None:
                raise ApiError(404, "not_in_roster", f"'{username}' is not in the class list.")
            try:
                if accounts.user_exists(username):
                    accounts.set_password(username, password)
                else:                                              # repair a missing Linux account on the way
                    accounts.create(username, password)
            except AccountError as e:
                log.error("password for %s: %s", username, e)
                raise ApiError(500, "account_command_failed", f"The password could not be changed: {e}") from e
            st.password = password
            _save(roster, students)

    await asyncio.to_thread(work)
    log.info("password for '%s' reset by %s", username, user)
    _no_store(response)
    return {"username": username, "password": password}


@router.delete("/api/students/{username}")
async def remove_student(username: str, request: Request, home: Literal["archive", "keep", "delete"] = "archive",
                         user: str = Depends(require_admin)):
    settings = request.app.state.settings
    accounts = request.app.state.accounts
    hub = request.app.state.hub
    roster = _roster(request)
    username = _username(username)
    if username == user:
        raise ApiError(403, "is_self", "You cannot remove the account you are signed in with.")
    if username in settings.admin_users:
        raise ApiError(403, "is_admin", "Teacher accounts are set in compose.yaml and cannot be removed here.")

    def in_roster():
        with roster.lock():
            return Roster.find(_load(roster), username) is not None

    if not await asyncio.to_thread(in_roster):
        raise ApiError(404, "not_in_roster", f"'{username}' is not in the class list.")

    # Hub first; HubUnavailable propagates as 503 and nothing has changed yet.
    model = await hub.get_user(username)
    hub_deleted = False
    if model is not None:
        if model.get("admin"):
            raise ApiError(403, "is_admin", "Teacher accounts cannot be removed here.")
        if _has_active_server(model):
            try:
                await hub.stop_server(username)
            except HubError as e:
                log.warning("remove %s: stop request refused (%s); continuing", username, e)
            deadline = time.monotonic() + STOP_POLL_TIMEOUT
            while True:
                model = await hub.get_user(username)
                if not _has_active_server(model) or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(STOP_POLL_INTERVAL)
        deadline = time.monotonic() + DELETE_RETRY_TIMEOUT
        while True:
            try:
                hub_deleted = await hub.delete_user(username)
                break
            except HubError as e:
                if e.status == 400 and "stopping" in (e.message or "").lower():
                    if time.monotonic() < deadline:
                        await asyncio.sleep(DELETE_RETRY_INTERVAL)
                        continue
                    raise ApiError(409, "server_stopping", f"{username}'s JupyterLab is still shutting down. Wait a moment and try again.") from e
                raise

    def work():
        with roster.lock():
            students = _load(roster)
            try:
                outcome = accounts.remove(username, archive=(home == "archive"), delete_home=(home != "keep"))
            except AccountError as e:
                log.error("remove %s: %s", username, e)
                raise ApiError(500, "account_command_failed", f"The account could not be removed: {e}", {"hub_deleted": hub_deleted}) from e
            _save(roster, [s for s in students if s.username != username])
            return outcome

    outcome = await asyncio.to_thread(work)
    if outcome.archive_path:
        home_result = "archived"
    elif outcome.home_removed:
        home_result = "deleted"
    else:
        home_result = "kept"
    log.info("student '%s' removed by %s (home %s, hub_deleted=%s)", username, user, home_result, hub_deleted)
    return {
        "username": username,
        "home": home_result,
        "archived_to": os.path.basename(outcome.archive_path) if outcome.archive_path else None,
        "hub_deleted": hub_deleted,
    }


# --- repair -------------------------------------------------------------------------------------

@router.post("/api/students/repair")
async def repair(request: Request, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    accounts = request.app.state.accounts
    hub = request.app.state.hub
    roster = _roster(request)
    report: dict = {"linux_created": [], "hub_created": [], "passwords_reapplied": 0, "hub_orphans": [], "linux_orphans": [], "errors": []}

    def work():
        with roster.lock():
            students = _load(roster)
            for st in students:
                try:
                    if accounts.user_exists(st.username):
                        accounts.set_password(st.username, st.password)
                        report["passwords_reapplied"] += 1
                        accounts.ensure_home_extras(st.username, accounts.sys.getpwnam(st.username).pw_uid)
                    else:
                        accounts.create(st.username, st.password)
                        report["linux_created"].append(st.username)
                    if st.username in settings.admin_users:
                        accounts.ensure_admin(st.username)
                except (AccountError, OSError, ValueError, KeyError) as e:
                    report["errors"].append(f"{st.username}: {e}")
            names = {s.username for s in students}
            for pw in accounts.sys.all_users():
                if pw.pw_uid >= accounts.s.first_uid and pw.pw_gid == accounts.s.students_gid and pw.pw_name not in names:
                    report["linux_orphans"].append(pw.pw_name)
            report["linux_orphans"].sort()
            return names

    names = await asyncio.to_thread(work)
    try:
        hub_names = {u.get("name") for u in await hub.list_users() if u.get("name")}
    except HubUnavailable:
        report["errors"].append(HUB_UNREACHABLE_MSG + " Hub registrations were not checked.")
        return report
    except HubError as e:
        report["errors"].append(f"JupyterHub answered with an error ({e.status}): {e.message}")
        return report
    for name in sorted(names - hub_names):
        try:
            await hub.create_user(name)
            report["hub_created"].append(name)
        except HubUnavailable:
            report["errors"].append(HUB_UNREACHABLE_MSG + f" '{name}' was not registered.")
            break
        except HubError as e:
            report["errors"].append(f"{name}: JupyterHub said {e.message}")
    report["hub_orphans"] = sorted(hub_names - names)
    log.info("repair by %s: %s", user, {k: (len(v) if isinstance(v, list) else v) for k, v in report.items()})
    return report


# --- print cards page ---------------------------------------------------------------------------

@router.get("/print/cards", response_class=HTMLResponse)
async def print_cards(request: Request, students: str | None = None, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    roster = _roster(request)
    wanted: set[str] | None = None
    if students is not None and students.strip():
        wanted = set()
        for raw in students.split(",")[:PRINT_MAX_CARDS]:
            try:
                wanted.add(validate_username(raw))
            except ValueError:
                continue

    def load():
        with roster.lock():
            return _load(roster)

    everyone = await asyncio.to_thread(load)
    selected = [s for s in everyone if wanted is None or s.username in wanted][:PRINT_MAX_CARDS]
    branding = read_branding(settings.branding_json)
    class_url = cards.class_url_for(request, branding.get("class_url") or settings.classroom_url)
    card_list = await asyncio.to_thread(cards.build_cards, selected, class_url, settings.hub_base_url)
    sheets = cards.chunk(card_list, cards.CARDS_PER_SHEET)
    hub_base = settings.hub_base_url.rstrip("/")
    ctx = {
        "request": request,
        "prefix": settings.prefix,
        "branding": branding,
        "class_url": class_url,
        "cards": card_list,
        "sheets": sheets,
        "count": len(card_list),
        "pages": len(sheets),
        "selected": wanted is not None,
        "back_url": settings.url("students"),
        "hub_static": f"{hub_base}/hub/static",
        "user": user,
    }
    response = request.app.state.templates.TemplateResponse(request, "print_cards.html", ctx)
    _no_store(response)
    return response
