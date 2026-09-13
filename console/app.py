"""
FastAPI application factory for the teacher console.

    create_app(settings=None, hub_client=None, accounts=None, secret=None)

Feature modules (students, status, files, backups, logs, branding) each expose
`router` (and optionally `setup(app)`); they are discovered by name so adding
one never requires editing this file.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import secrets
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, auth, errors, pages
from .accounts import AccountSettings, Accounts
from .hub_api import HubClient
from .settings import Settings

log = logging.getLogger("console")
HERE = os.path.dirname(os.path.abspath(__file__))
FEATURE_MODULES = ("students", "status", "files", "backups", "logs", "branding")
CSRF_EXEMPT_SUFFIXES = ("/logout", "/login", "/oauth_callback")


def _ensure_secret(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            data = fh.read().strip()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    data = secrets.token_hex(32).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data + b"\n")
    return data


def create_app(settings: Settings | None = None, hub_client: HubClient | None = None,
               accounts: Accounts | None = None, secret: bytes | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    prefix = settings.prefix

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Everything here is best-effort: a failure marks the app degraded but
        # the process keeps answering /health so the Hub does not give up on us.
        try:
            for d, mode in (
                (settings.state_dir, 0o700), (settings.backup_dir, 0o700), (settings.removed_dir, 0o700),
                (settings.tmp_dir, 0o700), (settings.log_dir, 0o755), (settings.branding_dir, 0o755),
            ):
                os.makedirs(d, mode=mode, exist_ok=True)
            if not os.path.isfile(settings.logo_file) and os.path.isfile(settings.default_logo):
                shutil.copyfile(settings.default_logo, settings.logo_file)
            if not os.path.isfile(settings.branding_json) and os.path.isfile(settings.default_branding):
                shutil.copyfile(settings.default_branding, settings.branding_json)
                os.chmod(settings.branding_json, 0o600)
        except OSError as e:
            log.error("cannot prepare state directories: %s", e)
            app.state.degraded = f"state folders unavailable: {e}"
        try:
            app.state.sessions = auth.Sessions(secret or _ensure_secret(settings.secret_path), settings)
        except OSError as e:
            log.error("cannot create the session secret: %s", e)
            app.state.degraded = f"session secret unavailable: {e}"
            app.state.sessions = auth.Sessions(secrets.token_bytes(32), settings)
        for name in FEATURE_MODULES:
            setup = getattr(app.state.features.get(name), "setup", None)
            if setup:
                try:
                    await setup(app) if _is_coro(setup) else setup(app)
                except Exception as e:  # noqa: BLE001
                    log.exception("feature %s failed to start", name)
                    app.state.degraded = f"{name} failed to start: {e}"
        log.info("teacher console %s ready at %s (image %s)", __version__, settings.service_prefix, settings.image_version)
        try:
            yield
        finally:
            for name in FEATURE_MODULES:
                teardown = getattr(app.state.features.get(name), "teardown", None)
                if teardown:
                    try:
                        await teardown(app) if _is_coro(teardown) else teardown(app)
                    except Exception:  # noqa: BLE001
                        log.exception("feature %s failed to stop", name)
            await app.state.hub.aclose()

    app = FastAPI(title="Teacher console", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.degraded = None
    app.state.hub = hub_client or HubClient(settings.hub_api_url, settings.hub_api_token, timeout=settings.hub_timeout)
    app.state.accounts = accounts or Accounts(
        AccountSettings(home_root=settings.home_root, shared_dir=settings.shared_dir, state_dir=settings.state_dir, kit_dir=settings.kit_dir)
    )
    app.state.admin_cache = auth.AdminCache(settings.user_cache_ttl)
    app.state.sessions = auth.Sessions(secret or secrets.token_bytes(32), settings)  # replaced in lifespan
    app.state.templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
    app.state.templates.env.globals.update({"prefix": prefix, "image_version": settings.image_version})
    app.state.render_error = pages.render_error
    app.state.features = {}

    errors.install(app, pages.render_error)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if request.method not in ("GET", "HEAD", "OPTIONS") and path.startswith(prefix) and not path.endswith(CSRF_EXEMPT_SUFFIXES):
            if not auth.csrf_ok(request):
                return JSONResponse({"error": "csrf_failed", "message": "Request blocked: missing console header."}, status_code=403)
        degraded = getattr(app.state, "degraded", None)
        if degraded and not (path.endswith("/health") or "/static/" in path):
            if errors.is_api(request):
                return JSONResponse({"error": "degraded", "message": f"The console is not fully started: {degraded}"}, status_code=503)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response

    app.mount(f"{prefix}/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    app.include_router(auth.router, prefix=prefix)
    app.include_router(pages.router, prefix=prefix)
    for name in FEATURE_MODULES:
        if importlib.util.find_spec(f"console.{name}") is None:
            continue
        module = importlib.import_module(f"console.{name}")
        app.state.features[name] = module
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router, prefix=prefix)
    return app


def _is_coro(fn) -> bool:
    import inspect

    return inspect.iscoroutinefunction(fn)


app = create_app()
