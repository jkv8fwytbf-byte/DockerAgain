"""
Who is allowed in: the Hub's OAuth flow, a signed session cookie, and an
admin check on every request.

Flow
    GET  /login            -> signed "state" cookie, redirect to the Hub's authorize page
    GET  /oauth_callback   -> code -> access token (client_secret = our service token)
                              -> GET /hub/api/user -> must be admin -> session cookie
    POST /logout           -> clear cookie

The Hub itself only lets users with the `admin` role reach a service, so a
student is refused before we ever see them; we still check `admin` here.
"""
from __future__ import annotations

import hmac
import secrets
import time
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .errors import ApiError, HubUnavailable, NeedsLogin

SESSION_COOKIE = "console-session"
STATE_COOKIE = "console-oauth-state"
CSRF_HEADER = "x-console-request"


class Sessions:
    def __init__(self, secret: bytes, settings):
        self.settings = settings
        self._session = URLSafeTimedSerializer(secret, salt="console-session")
        self._state = URLSafeTimedSerializer(secret, salt="console-oauth-state")

    # -- session --
    def make_session(self, name: str) -> str:
        return self._session.dumps({"n": name, "t": int(time.time())})

    def read_session(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            data = self._session.loads(value, max_age=self.settings.session_max_age)
        except (BadSignature, SignatureExpired):
            return None
        name = data.get("n") if isinstance(data, dict) else None
        return name if isinstance(name, str) and name else None

    # -- oauth state --
    def make_state(self, state_id: str, next_path: str) -> str:
        return self._state.dumps({"s": state_id, "next": next_path})

    def read_state(self, value: str | None) -> dict | None:
        if not value:
            return None
        try:
            data = self._state.loads(value, max_age=self.settings.state_max_age)
        except (BadSignature, SignatureExpired):
            return None
        return data if isinstance(data, dict) else None


class AdminCache:
    """Remembers for a short while whether the Hub says a user is an admin."""

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._entries: dict[str, tuple[float, bool]] = {}

    async def is_admin(self, hub, name: str) -> bool:
        now = time.time()
        hit = self._entries.get(name)
        if hit and hit[0] > now:
            return hit[1]
        try:
            model = await hub.get_user(name)
        except HubUnavailable:
            if hit:                      # stale but better than locking the teacher out
                return hit[1]
            raise
        is_admin = bool(model and model.get("admin"))
        self._entries[name] = (now + self.ttl, is_admin)
        if len(self._entries) > 500:
            self._entries.clear()
        return is_admin

    def forget(self, name: str) -> None:
        self._entries.pop(name, None)


def is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return (proto or request.url.scheme) == "https"


def set_cookie(response: Response, request: Request, name: str, value: str, max_age: int) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        name, value, max_age=max_age, path=settings.service_prefix, httponly=True,
        samesite="lax", secure=is_https(request),
    )


def clear_cookie(response: Response, request: Request, name: str) -> None:
    settings = request.app.state.settings
    response.delete_cookie(name, path=settings.service_prefix)


def safe_next(request: Request, raw: str | None) -> str:
    settings = request.app.state.settings
    if raw and raw.startswith(settings.service_prefix) and "//" not in raw and "\\" not in raw and "\n" not in raw:
        return raw
    return settings.service_prefix


def current_user(request: Request) -> str | None:
    sessions: Sessions = request.app.state.sessions
    return sessions.read_session(request.cookies.get(SESSION_COOKIE))


async def require_admin(request: Request) -> str:
    """FastAPI dependency: the signed-in admin's username, or an error."""
    name = current_user(request)
    if not name:
        raise NeedsLogin(request.url.path + (("?" + request.url.query) if request.url.query else ""))
    request.state.user = name
    cache: AdminCache = request.app.state.admin_cache
    hub = request.app.state.hub
    if not await cache.is_admin(hub, name):
        raise ApiError(403, "forbidden", f"'{name}' is not a teacher account.")
    return name


def csrf_ok(request: Request) -> bool:
    """Mutating requests must carry our custom header and come from our own origin."""
    if request.headers.get(CSRF_HEADER) != "1":
        return False
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        origin_host = origin.split("://", 1)[-1].split("/", 1)[0]
        if not host or not hmac.compare_digest(origin_host.lower(), host.split(",")[0].strip().lower()):
            return False
    return True


router = APIRouter()


@router.get("/login")
async def login(request: Request, next: str | None = None):
    settings = request.app.state.settings
    sessions: Sessions = request.app.state.sessions
    state_id = secrets.token_urlsafe(16)
    params = {
        "client_id": settings.client_id,
        "redirect_uri": settings.oauth_callback_url,
        "response_type": "code",
        "state": state_id,
    }
    authorize = settings.hub_base_url.rstrip("/") + "/hub/api/oauth2/authorize?" + urlencode(params)
    response = RedirectResponse(authorize, status_code=302)
    set_cookie(response, request, STATE_COOKIE, sessions.make_state(state_id, safe_next(request, next)), settings.state_max_age)
    return response


@router.get("/oauth_callback")
async def oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    settings = request.app.state.settings
    sessions: Sessions = request.app.state.sessions
    render_error = request.app.state.render_error
    if error:
        return render_error(request, 400, "Sign-in was cancelled", f"JupyterHub said: {error}")
    if not code or not state:
        return render_error(request, 400, "Incomplete sign-in", "The sign-in link was missing information. Start again from the console.")
    saved = sessions.read_state(request.cookies.get(STATE_COOKIE))
    if not saved or not hmac.compare_digest(str(saved.get("s", "")), state):
        return render_error(
            request, 400, "Sign-in took too long",
            "Your browser lost the sign-in ticket (it lasts ten minutes) or cookies are blocked. Please try again.",
            retry_url=settings.url("login"),
        )
    hub = request.app.state.hub
    access_token = await hub.exchange_code(code, settings.client_id, settings.oauth_callback_url)
    model = await hub.whoami(access_token)
    name = str(model.get("name", "")).lower()
    response: Response
    if model.get("kind") != "user" or not model.get("admin") or not name:
        response = render_error(
            request, 403, "Teachers only",
            "This console is for your teacher. If you are a student, go back to your JupyterLab.",
            retry_url=settings.hub_base_url.rstrip("/") + "/hub/home", retry_label="Back to JupyterLab",
        )
        clear_cookie(response, request, STATE_COOKIE)
        return response
    request.app.state.admin_cache.forget(name)
    response = RedirectResponse(safe_next(request, saved.get("next")), status_code=303)
    set_cookie(response, request, SESSION_COOKIE, sessions.make_session(name), settings.session_max_age)
    clear_cookie(response, request, STATE_COOKIE)
    return response


@router.post("/logout")
async def logout(request: Request):
    settings = request.app.state.settings
    name = current_user(request)
    if name:
        request.app.state.admin_cache.forget(name)
    response = RedirectResponse(settings.url("logged-out"), status_code=303)
    clear_cookie(response, request, SESSION_COOKIE)
    return response
