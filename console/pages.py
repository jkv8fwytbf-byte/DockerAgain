"""
HTML page routes. Every page renders a template that extends base.html; the
data on the page is loaded by that page's JavaScript from the JSON API that
the feature module owns (students.py, status.py, ...).
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .auth import require_admin
from .util import read_branding

NAV = [
    ("dashboard", "", "Dashboard", "fa-gauge-high"),
    ("students", "students", "Students", "fa-user-graduate"),
    ("files", "files", "Handouts & Submissions", "fa-folder-open"),
    ("backups", "backups", "Backups", "fa-box-archive"),
    ("logs", "logs", "Logs", "fa-scroll"),
    ("settings", "settings", "Settings", "fa-sliders"),
]

router = APIRouter()
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
ASSET_VERSION = str(max(os.stat(os.path.join(_STATIC_DIR, name)).st_mtime_ns
                        for name in os.listdir(_STATIC_DIR) if name.endswith((".css", ".js"))))


def page_context(request: Request, page: str, title: str, subtitle: str = "", user: str | None = None) -> dict:
    settings = request.app.state.settings
    branding = read_branding(settings.branding_json)
    try:
        logo_version = int(os.stat(settings.logo_file).st_mtime)
    except OSError:
        logo_version = 0
    hub_base = settings.hub_base_url.rstrip("/")
    return {
        "request": request,
        "prefix": settings.prefix,
        "page": page,
        "title": title,
        "subtitle": subtitle,
        "user": user or getattr(request.state, "user", None),
        "nav": [{"id": i, "href": settings.url(p), "label": l, "icon": ic, "active": i == page} for i, p, l, ic in NAV],
        "branding": branding,
        "logo_url": f"{settings.prefix}/public/logo?v={logo_version}",
        "hub_static": f"{hub_base}/hub/static",
        "hub_home_url": f"{hub_base}/hub/home",
        "lab_url": f"{hub_base}/user/{user or getattr(request.state, 'user', '')}/lab",
        "image_version": settings.image_version,
        "asset_version": ASSET_VERSION,
        "degraded": getattr(request.app.state, "degraded", None),
    }


def render(request: Request, template: str, page: str, title: str, subtitle: str = "", status: int = 200, **extra):
    templates = request.app.state.templates
    ctx = page_context(request, page, title, subtitle)
    ctx.update(extra)
    response = templates.TemplateResponse(request, template, ctx, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def render_error(request: Request, status: int, title: str, text: str, retry_url: str | None = None, retry_label: str = "Try again") -> HTMLResponse:
    """Used by errors.py and auth.py; must work even before a session exists."""
    return render(request, "error.html", "error", title, "", status=status, error_status=status, error_text=text, retry_url=retry_url, retry_label=retry_label)


# --- public ----------------------------------------------------------------------

@router.get("/health")
async def health(request: Request):
    return JSONResponse({"status": "ok", "version": request.app.state.settings.image_version, "degraded": getattr(request.app.state, "degraded", None)})


@router.get("/logged-out")
async def logged_out(request: Request):
    settings = request.app.state.settings
    return render(request, "logged_out.html", "logged-out", "Signed out", hub_logout_url=settings.hub_base_url.rstrip("/") + "/hub/logout")


@router.get("/public/logo")
async def public_logo(request: Request):
    settings = request.app.state.settings
    path = settings.logo_file if os.path.isfile(settings.logo_file) else settings.default_logo
    try:
        etag = str(int(os.stat(path).st_mtime))
    except OSError:
        return Response(status_code=404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache", "ETag": etag})


# --- built-in API ---------------------------------------------------------------------

@router.get("/api/whoami")
async def whoami(request: Request, user: str = Depends(require_admin)):
    settings = request.app.state.settings
    return JSONResponse({"name": user, "admin": True, "version": settings.image_version, "prefix": settings.prefix})


# --- pages (admin only) -----------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(require_admin)):
    return render(request, "dashboard.html", "dashboard", "Dashboard", "Live view of the classroom server")


@router.get("/students", response_class=HTMLResponse)
async def students(request: Request, user: str = Depends(require_admin)):
    return render(request, "students.html", "students", "Students", "Accounts, passwords and login cards")


@router.get("/files", response_class=HTMLResponse)
async def files(request: Request, user: str = Depends(require_admin)):
    return render(request, "files.html", "files", "Handouts & Submissions", "Share files with the class and collect their work")


@router.get("/backups", response_class=HTMLResponse)
async def backups(request: Request, user: str = Depends(require_admin)):
    return render(request, "backups.html", "backups", "Backups", "One file with every student's work, the handouts and your settings")


@router.get("/logs", response_class=HTMLResponse)
async def logs(request: Request, user: str = Depends(require_admin)):
    return render(request, "logs.html", "logs", "Logs", "What the server has been doing")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: str = Depends(require_admin)):
    return render(request, "settings.html", "settings", "Settings", "School name, announcement, class address and logo")
