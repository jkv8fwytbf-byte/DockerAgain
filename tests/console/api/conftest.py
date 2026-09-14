"""
API test fixtures: the FastAPI app with a fake Hub (httpx.MockTransport), a
fake account layer, temp state directories, and helper sessions.
"""
from __future__ import annotations

import json
import os
import sys

import httpx
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "console"))

from fakes import DEFAULT_HUB_TOKEN, FakeHub, FakeSystem  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from console.accounts import AccountSettings, Accounts  # noqa: E402
from console.app import create_app  # noqa: E402
from console.auth import CSRF_HEADER, SESSION_COOKIE  # noqa: E402
from console.hub_api import HubClient  # noqa: E402
from console.settings import Settings  # noqa: E402

SERVICE_TOKEN = DEFAULT_HUB_TOKEN


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return str(d)


@pytest.fixture
def settings(tmp_path, state_dir):
    home = tmp_path / "home"; home.mkdir()
    shared = tmp_path / "shared"; shared.mkdir()
    skel = tmp_path / "skel"; skel.mkdir(); (skel / "Welcome.ipynb").write_text("{}")
    branding_defaults = tmp_path / "defaults"; branding_defaults.mkdir()
    (branding_defaults / "default-logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (branding_defaults / "branding.default.json").write_text(json.dumps({"school_name": "Test School", "accent": "blue"}))
    s = Settings(
        state_dir=state_dir, home_root=str(home), shared_dir=str(shared), users_txt=str(tmp_path / "users.txt"),
        kit_dir=str(skel), default_logo=str(branding_defaults / "default-logo.png"),
        default_branding=str(branding_defaults / "branding.default.json"),
        hub_api_token=SERVICE_TOKEN, admin_users=frozenset({"teacher"}), user_cache_ttl=60,
    )
    return s


@pytest.fixture
def hub():
    h = FakeHub()
    h.add("teacher", admin=True)
    h.add("student01")
    return h


@pytest.fixture
def fake_system(settings):
    fs = FakeSystem(settings.home_root, settings.kit_dir)
    fs.add_user("teacher", 2000)
    fs.add_user("student01", 2001)
    fs.make_home("teacher", 2000)
    fs.make_home("student01", 2001)
    return fs


@pytest.fixture
def app(settings, hub, fake_system):
    hub_client = HubClient(settings.hub_api_url, SERVICE_TOKEN, transport=httpx.MockTransport(hub.handler))
    accounts = Accounts(
        AccountSettings(home_root=settings.home_root, shared_dir=settings.shared_dir, state_dir=settings.state_dir, kit_dir=settings.kit_dir),
        fake_system,
    )
    return create_app(settings, hub_client=hub_client, accounts=accounts, secret=b"unit-test-secret-0123456789abcdef")


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://classroom.local", follow_redirects=False) as c:
        yield c


@pytest.fixture
def prefix(settings):
    return settings.prefix


def session_cookie(app, name: str) -> dict:
    return {SESSION_COOKIE: app.state.sessions.make_session(name)}


@pytest.fixture
def as_teacher(client, app):
    client.cookies.set(SESSION_COOKIE, app.state.sessions.make_session("teacher"))
    client.headers[CSRF_HEADER] = "1"
    return client


@pytest.fixture
def as_student(client, app):
    client.cookies.set(SESSION_COOKIE, app.state.sessions.make_session("student01"))
    client.headers[CSRF_HEADER] = "1"
    return client
