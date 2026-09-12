"""
Async client for the JupyterHub REST API, authenticated with the service's
own token. Every method raises HubUnavailable (cannot connect) or HubError
(non-2xx) so callers never see raw httpx exceptions.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .errors import HubError, HubUnavailable

log = logging.getLogger("console.hub")
PAGINATION_ACCEPT = "application/jupyterhub-pagination+json"


class HubClient:
    def __init__(self, api_url: str, token: str, *, timeout: float = 10.0, transport=None):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self._client = httpx.AsyncClient(
            base_url=self.api_url,
            headers={"Authorization": f"token {token}"} if token else {},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- low level ---------------------------------------------------------------
    async def _request(self, method: str, path: str, *, ok=(200, 201, 202, 204), **kw) -> httpx.Response:
        try:
            resp = await self._client.request(method, path, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            raise HubUnavailable(str(e)) from e
        except httpx.HTTPError as e:
            raise HubUnavailable(str(e)) from e
        if resp.status_code not in ok:
            message = ""
            try:
                message = resp.json().get("message", "")
            except ValueError:
                message = resp.text[:200]
            raise HubError(resp.status_code, message or resp.reason_phrase)
        return resp

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    # --- OAuth -------------------------------------------------------------------
    async def exchange_code(self, code: str, client_id: str, redirect_uri: str) -> str:
        """Authorization code -> access token for the user who just logged in."""
        resp = await self._request(
            "POST",
            "/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": self.token,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            ok=(200,),
        )
        data = self._json(resp) or {}
        token = data.get("access_token")
        if not token:
            raise HubError(resp.status_code, "no access_token in OAuth response")
        return token

    async def whoami(self, access_token: str) -> dict:
        """User model for a user's OAuth token (includes the `admin` flag)."""
        resp = await self._request("GET", "/user", headers={"Authorization": f"token {access_token}"}, ok=(200,))
        return self._json(resp) or {}

    # --- users ---------------------------------------------------------------------
    async def get_user(self, name: str) -> dict | None:
        """User model or None when the Hub does not know the user."""
        try:
            resp = await self._request("GET", f"/users/{name}", ok=(200,))
        except HubError as e:
            if e.status == 404:
                return None
            raise
        return self._json(resp)

    async def list_users(self, *, include_stopped_servers: bool = True, state: str | None = None) -> list[dict]:
        users: list[dict] = []
        offset, limit = 0, 200
        while True:
            params = {"offset": offset, "limit": limit}
            if include_stopped_servers:
                params["include_stopped_servers"] = "1"
            if state:
                params["state"] = state
            resp = await self._request("GET", "/users", params=params, headers={"Accept": PAGINATION_ACCEPT}, ok=(200,))
            data = self._json(resp)
            if isinstance(data, list):        # older Hub without pagination support
                users.extend(data)
                break
            users.extend(data.get("items", []))
            nxt = (data.get("_pagination") or {}).get("next")
            if not nxt:
                break
            offset = int(nxt.get("offset", offset + limit))
            limit = int(nxt.get("limit", limit))
        return users

    async def create_user(self, name: str) -> bool:
        """True if created, False if it already existed."""
        try:
            await self._request("POST", f"/users/{name}", ok=(201,))
            return True
        except HubError as e:
            if e.status == 409:
                return False
            raise

    async def delete_user(self, name: str) -> bool:
        """True if deleted, False if unknown. Raises HubError(400) while a stop is pending."""
        try:
            await self._request("DELETE", f"/users/{name}", ok=(204,))
            return True
        except HubError as e:
            if e.status == 404:
                return False
            raise

    # --- servers -------------------------------------------------------------------
    async def start_server(self, name: str) -> str:
        """'running' (201) or 'starting' (202)."""
        try:
            resp = await self._request("POST", f"/users/{name}/server", ok=(201, 202))
        except HubError as e:
            if e.status == 400 and "already running" in e.message.lower():
                return "running"
            raise
        return "running" if resp.status_code == 201 else "starting"

    async def stop_server(self, name: str) -> str:
        """'stopped' (204) or 'stopping' (202)."""
        try:
            resp = await self._request("DELETE", f"/users/{name}/server", ok=(202, 204))
        except HubError as e:
            if e.status == 400 and "not running" in e.message.lower():
                return "stopped"
            raise
        return "stopped" if resp.status_code == 204 else "stopping"

    async def info(self) -> dict:
        resp = await self._request("GET", "/info", ok=(200,))
        return self._json(resp) or {}
