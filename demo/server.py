"""
Docker-free complete demo of Classroom JupyterHub.

Starts the real teacher console (FastAPI) on a fake Hub and fake Linux accounts,
seeds a Lincoln High School class, and serves the branded sign-in page plus a
lightweight student home-folder view.

    python -m demo
    make demo
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import httpx
import uvicorn
from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "console"))

from fakes import DEFAULT_HUB_TOKEN, FakeHub, FakeSystem  # noqa: E402

from console.accounts import AccountSettings, Accounts  # noqa: E402
from console.app import create_app  # noqa: E402
from console.auth import SESSION_COOKIE, set_cookie  # noqa: E402
from console.hub_api import HubClient  # noqa: E402
from console.settings import Settings  # noqa: E402
from demo.classroom import (  # noqa: E402
    FrozenSampler,
    copy_handouts,
    seed_accounts,
    seed_archived_student,
    seed_branding,
    seed_import_file,
    seed_logs,
    seed_roster,
    seed_submissions,
    snapshot_for,
)
from demo.hub_pages import (  # noqa: E402
    STUDENT_COOKIE,
    clear_student_cookie,
    hub_static_dir,
    landing_html,
    lookup_student,
    render_login,
    render_workspace,
    set_student_cookie,
    student_cookie_user,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099


def prepare_root(run_dir: Path, *, reset: bool = True) -> Path:
    run_dir = run_dir.resolve()
    if reset and run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "home").mkdir(parents=True, exist_ok=True)
    (run_dir / "shared").mkdir(parents=True, exist_ok=True)
    (run_dir / "state").mkdir(parents=True, exist_ok=True)
    return run_dir


def build_settings(run_dir: Path) -> Settings:
    branding_defaults = ROOT / "branding"
    return Settings(
        state_dir=str(run_dir / "state"),
        home_root=str(run_dir / "home"),
        shared_dir=str(run_dir / "shared"),
        users_txt=str(run_dir / "users.txt"),
        kit_dir=str(ROOT / "skel"),
        default_logo=str(branding_defaults / "default-logo.png"),
        default_branding=str(branding_defaults / "branding.default.json"),
        service_prefix="/services/console/",
        service_url=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}",
        hub_api_url="http://hub.demo/hub/api",
        hub_api_token=DEFAULT_HUB_TOKEN,
        hub_base_url="/",
        admin_users=frozenset({"teacher"}),
        classroom_url="http://192.168.1.10:8000",
        image_version=os.environ.get("CLASSROOM_IMAGE_VERSION", "1.1-demo"),
    )


def build_demo(run_dir: Path | None = None, *, reset: bool = True):
    """Build the FastAPI app and seed the class. Returns (app, settings, hub, accounts)."""
    run_dir = prepare_root(run_dir or (ROOT / "demo" / ".run"), reset=reset)
    settings = build_settings(run_dir)
    seed_import_file(settings.users_txt)
    copy_handouts(settings.shared_dir)
    seed_branding(settings)
    seed_logs(settings.log_dir)
    seed_archived_student(settings.removed_dir)

    hub = FakeHub(DEFAULT_HUB_TOKEN)
    fake = FakeSystem(settings.home_root, settings.kit_dir)
    accounts = Accounts(
        AccountSettings(
            home_root=settings.home_root, shared_dir=settings.shared_dir,
            state_dir=settings.state_dir, kit_dir=settings.kit_dir,
        ),
        fake,
    )
    seed_roster(settings.roster_path)
    seed_accounts(accounts, hub)
    seed_submissions(settings.home_root)

    hub_client = HubClient(settings.hub_api_url, DEFAULT_HUB_TOKEN, transport=httpx.MockTransport(hub.handler))
    app = create_app(settings, hub_client=hub_client, accounts=accounts, secret=b"demo-console-secret-0123456789ab")
    app.state.sampler = FrozenSampler(snapshot_for(accounts))
    app.state.demo_hub = hub
    app.state.demo_run_dir = str(run_dir)
    _install_demo_routes(app, settings)
    return app, settings, hub, accounts


class _TeacherAutoLogin(BaseHTTPMiddleware):
    """First visit to the console is already signed in as teacher (the Hub OAuth dance is skipped)."""

    async def dispatch(self, request: Request, call_next):
        settings = request.app.state.settings
        path = request.url.path
        if path.startswith(settings.prefix) and SESSION_COOKIE not in request.cookies:
            session = request.app.state.sessions.make_session("teacher")
            cookie = request.headers.get("cookie", "")
            extra = f"{SESSION_COOKIE}={session}"
            headers = [(k, v) for k, v in request.scope["headers"] if k != b"cookie"]
            headers.append((b"cookie", (f"{cookie}; {extra}" if cookie else extra).encode()))
            request.scope["headers"] = headers
            response = await call_next(request)
            set_cookie(response, request, SESSION_COOKIE, session, settings.session_max_age)
            return response
        return await call_next(request)


def _install_demo_routes(app, settings: Settings) -> None:
    static = hub_static_dir()
    if static:
        app.mount("/hub/static", StaticFiles(directory=static), name="hub-static")

    @app.get("/", include_in_schema=False)
    async def landing():
        return HTMLResponse(landing_html(settings))

    @app.get("/hub/login", include_in_schema=False)
    async def hub_login(request: Request, username: str = ""):
        return HTMLResponse(render_login(settings, username=username))

    @app.post("/hub/login", include_in_schema=False)
    async def hub_login_post(request: Request):
        form = await request.form()
        username = str(form.get("username") or "").strip().lower()
        password = str(form.get("password") or "")
        student = lookup_student(settings, username)
        if student is None or student.password != password:
            return HTMLResponse(
                render_login(settings, username=username, login_error="Invalid username or password"),
                status_code=401,
            )
        if username in settings.admin_users:
            response = RedirectResponse("/services/console/", status_code=303)
            session = request.app.state.sessions.make_session(username)
            set_cookie(response, request, SESSION_COOKIE, session, settings.session_max_age)
            return response
        response = RedirectResponse(f"/user/{username}/lab", status_code=303)
        set_student_cookie(response, username)
        return response

    @app.get("/hub/logout", include_in_schema=False)
    async def hub_logout():
        response = RedirectResponse("/", status_code=303)
        clear_student_cookie(response)
        response.delete_cookie(SESSION_COOKIE, path=settings.service_prefix)
        return response

    @app.get("/hub/home", include_in_schema=False)
    async def hub_home(request: Request):
        name = student_cookie_user(request)
        if name:
            return RedirectResponse(f"/user/{name}/lab", status_code=303)
        if request.cookies.get(SESSION_COOKIE):
            return RedirectResponse("/services/console/", status_code=303)
        return RedirectResponse("/hub/login", status_code=303)

    @app.get("/hub/logo", include_in_schema=False)
    async def hub_logo():
        path = settings.logo_file if os.path.isfile(settings.logo_file) else settings.default_logo
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})

    @app.get("/user/{username}/lab", include_in_schema=False)
    @app.get("/user/{username}/lab/", include_in_schema=False)
    async def student_lab(username: str, request: Request):
        return _student_tree(settings, request, username, "")

    @app.get("/user/{username}/lab/tree/{path:path}", include_in_schema=False)
    async def student_tree(username: str, path: str, request: Request):
        return _student_tree(settings, request, username, path)

    app.add_middleware(_TeacherAutoLogin)


def _student_tree(settings, request: Request, username: str, rel: str):
    who = student_cookie_user(request)
    if who != username and username not in settings.admin_users:
        # The teacher console "Open my JupyterLab" link is for the signed-in teacher.
        cookie = request.cookies.get(SESSION_COOKIE)
        teacher = request.app.state.sessions.read_session(cookie) if cookie else None
        if teacher != "teacher" and teacher != username:
            return RedirectResponse(f"/hub/login?username={username}", status_code=303)
    return render_workspace(settings, username, rel)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--run-dir", default=str(ROOT / "demo" / ".run"))
    parser.add_argument("--keep", action="store_true", help="do not wipe the previous demo run directory")
    args = parser.parse_args(argv)

    app, settings, _hub, _accounts = build_demo(Path(args.run_dir), reset=not args.keep)
    # Unprivileged demo: files in the temp tree are owned by us, not by FakeSystem uids.
    from console import files as files_mod

    files_mod._owner_ok = lambda st_uid, uid: True  # noqa: ARG005
    url = f"http://{args.host}:{args.port}"
    print(f"Classroom demo  {url}")
    print(f"  Student sign-in     {url}/hub/login")
    print(f"  Teacher console     {url}/services/console/")
    print(f"  Login cards         {url}/services/console/print/cards")
    print(f"  Demo data           {settings.state_dir}")
    print("  Example student     priya / coral-otter-12")
    print("  Teacher             teacher / change-me-teacher  (console is already signed in)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
