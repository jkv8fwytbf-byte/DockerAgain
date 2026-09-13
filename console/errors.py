"""Uniform error handling: JSON for /api/ routes, a friendly page for the rest."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("console")


class ApiError(Exception):
    """An error the teacher can act on. `code` is machine-readable, `message` human."""

    def __init__(self, status: int, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}


class NeedsLogin(Exception):
    """Page request without a valid session: redirect to the login flow."""

    def __init__(self, next_path: str):
        self.next_path = next_path


class HubUnavailable(Exception):
    """The Hub API could not be reached."""


class HubError(Exception):
    """The Hub API answered with an error status."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Hub returned {status}: {message}")
        self.status = status
        self.message = message


def is_api(request: Request) -> bool:
    return "/api/" in request.url.path or request.url.path.endswith("/api")


def _json(status: int, code: str, message: str, detail: dict | None = None) -> JSONResponse:
    body = {"error": code, "message": message}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


def install(app: FastAPI, render_error) -> None:
    """render_error(request, status, title, text) -> HTMLResponse (provided by pages.py)."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        if is_api(request):
            return _json(exc.status, exc.code, exc.message, exc.detail)
        return render_error(request, exc.status, exc.message, "")

    @app.exception_handler(NeedsLogin)
    async def _needs_login(request: Request, exc: NeedsLogin):
        settings = request.app.state.settings
        if is_api(request):
            return _json(401, "not_authenticated", "Please sign in again.", {"login_url": settings.url("login")})
        from urllib.parse import quote

        return RedirectResponse(settings.url("login") + "?next=" + quote(exc.next_path, safe=""), status_code=302)

    @app.exception_handler(HubUnavailable)
    async def _hub_unavailable(request: Request, exc: HubUnavailable):
        log.warning("Hub unreachable: %s", exc)
        msg = "The JupyterHub server is not answering right now. Wait a moment and try again."
        if is_api(request):
            return _json(503, "hub_unreachable", msg)
        return render_error(request, 503, "JupyterHub is not answering", msg)

    @app.exception_handler(HubError)
    async def _hub_error(request: Request, exc: HubError):
        log.warning("Hub error: %s", exc)
        msg = f"JupyterHub answered with an error ({exc.status}): {exc.message}"
        if is_api(request):
            return _json(502, "hub_error", msg, {"hub_status": exc.status})
        return render_error(request, 502, "JupyterHub reported a problem", msg)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        fields = {}
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", []) if p not in ("body", "query", "path")]
            fields[".".join(loc) or "request"] = err.get("msg", "invalid value")
        msg = "; ".join(f"{k}: {v}" for k, v in fields.items()) or "Invalid request."
        if is_api(request):
            return _json(400, "validation_failed", msg, {"fields": fields})
        return render_error(request, 400, "That request was not valid", msg)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        text = str(exc.detail) if exc.detail else ""
        if is_api(request):
            return _json(exc.status_code, "http_error", text or "Request failed.")
        titles = {404: "Page not found", 403: "Teachers only", 405: "Not allowed"}
        return render_error(request, exc.status_code, titles.get(exc.status_code, "Something went wrong"), text)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        if is_api(request):
            return _json(500, "internal_error", "Something went wrong on the server. The details are in the console log.")
        return render_error(request, 500, "Something went wrong", "The details are in the console log (Logs page).")
