from urllib.parse import parse_qs, urlparse

from console.auth import CSRF_HEADER, SESSION_COOKIE, STATE_COOKIE


def test_health_is_public(client, prefix):
    r = client.get(f"{prefix}/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_page_without_session_redirects_to_login(client, prefix):
    r = client.get(f"{prefix}/students")
    assert r.status_code == 302
    assert r.headers["location"].startswith(f"{prefix}/login?next=")


def test_api_without_session_is_401_json(client, prefix):
    r = client.get(f"{prefix}/api/whoami")
    assert r.status_code == 401 and r.json()["error"] == "not_authenticated"


def test_login_redirects_to_hub_authorize_with_state_cookie(client, prefix, settings):
    r = client.get(f"{prefix}/login?next={prefix}/students")
    assert r.status_code == 302
    url = urlparse(r.headers["location"])
    assert url.path == "/hub/api/oauth2/authorize"
    q = parse_qs(url.query)
    assert q["client_id"] == [settings.client_id]
    assert q["redirect_uri"] == [settings.oauth_callback_url]
    assert q["response_type"] == ["code"] and q["state"][0]
    assert STATE_COOKIE in r.cookies
    cookie_header = r.headers.get("set-cookie", "")
    assert f"Path={settings.service_prefix}" in cookie_header and "HttpOnly" in cookie_header


def _start_login(client, prefix, next_path=None):
    r = client.get(f"{prefix}/login" + (f"?next={next_path}" if next_path else ""))
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return state


def test_oauth_callback_admin_gets_session(client, prefix, hub):
    state = _start_login(client, prefix, f"{prefix}/backups")
    hub.codes["code-1"] = "teacher"
    r = client.get(f"{prefix}/oauth_callback?code=code-1&state={state}")
    assert r.status_code == 303 and r.headers["location"] == f"{prefix}/backups"
    assert SESSION_COOKIE in r.cookies
    assert ("POST", "/hub/api/oauth2/token") in hub.calls and ("GET", "/hub/api/user") in hub.calls
    # the session now opens pages
    r2 = client.get(f"{prefix}/")
    assert r2.status_code == 200 and "Dashboard" in r2.text and "teacher" in r2.text


def test_oauth_callback_non_admin_is_refused_without_cookie(client, prefix, hub):
    state = _start_login(client, prefix)
    hub.codes["code-2"] = "student01"
    r = client.get(f"{prefix}/oauth_callback?code=code-2&state={state}")
    assert r.status_code == 403 and "Teachers only" in r.text
    assert SESSION_COOKIE not in r.cookies


def test_oauth_callback_state_mismatch(client, prefix, hub):
    _start_login(client, prefix)
    hub.codes["code-3"] = "teacher"
    r = client.get(f"{prefix}/oauth_callback?code=code-3&state=wrong")
    assert r.status_code == 400 and "took too long" in r.text
    assert SESSION_COOKIE not in r.cookies


def test_oauth_callback_ignores_open_redirects(client, prefix, hub):
    state = _start_login(client, prefix, "https://evil.example/x")
    hub.codes["code-4"] = "teacher"
    r = client.get(f"{prefix}/oauth_callback?code=code-4&state={state}")
    assert r.status_code == 303 and r.headers["location"] == f"{prefix}/"


def test_tampered_session_is_anonymous(client, prefix, app):
    client.cookies.set(SESSION_COOKIE, app.state.sessions.make_session("teacher")[:-3] + "xyz")
    r = client.get(f"{prefix}/api/whoami")
    assert r.status_code == 401


def test_student_session_is_forbidden(as_student, prefix):
    r = as_student.get(f"{prefix}/api/whoami")
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    r = as_student.get(f"{prefix}/students")
    assert r.status_code == 403 and "not a teacher" in r.text


def test_admin_check_uses_hub_and_caches(as_teacher, prefix, hub):
    as_teacher.get(f"{prefix}/students")
    r = as_teacher.get(f"{prefix}/api/whoami")
    assert r.status_code == 200 and r.json()["name"] == "teacher"
    assert hub.calls.count(("GET", "/hub/api/users/teacher")) == 1


def test_csrf_header_required_on_mutations(as_teacher, prefix):
    del as_teacher.headers[CSRF_HEADER]
    r = as_teacher.post(f"{prefix}/api/servers/stop-all", json={})
    assert r.status_code == 403 and r.json()["error"] == "csrf_failed"


def test_cross_origin_mutation_is_rejected(as_teacher, prefix):
    r = as_teacher.post(f"{prefix}/api/servers/stop-all", json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_logout_clears_session(as_teacher, prefix):
    r = as_teacher.post(f"{prefix}/logout")
    assert r.status_code == 303 and r.headers["location"].endswith("/logged-out")
    assert "console-session=" in r.headers.get("set-cookie", "")
    as_teacher.cookies.clear()
    assert as_teacher.get(f"{prefix}/students").status_code == 302


def test_hub_down_with_cached_admin_still_works(as_teacher, prefix, hub):
    assert as_teacher.get(f"{prefix}/students").status_code == 200
    hub.down = True
    assert as_teacher.get(f"{prefix}/students").status_code == 200  # stale cache is fine for page access


def test_hub_down_without_cache_is_503(as_teacher, prefix, hub):
    hub.down = True
    r = as_teacher.get(f"{prefix}/api/whoami")
    assert r.status_code == 503 and r.json()["error"] == "hub_unreachable"


def test_placeholder_pages_render(as_teacher, prefix):
    for page in ("", "students", "files", "backups", "logs", "settings"):
        r = as_teacher.get(f"{prefix}/{page}")
        assert r.status_code == 200, page
        assert "Teacher console" in r.text and 'id="dark-theme-toggle"' in r.text
        assert "Test School" in r.text                         # branding defaults copied at startup
    assert as_teacher.get(f"{prefix}/static/console.css").status_code == 200


def test_public_logo_and_logged_out(client, prefix):
    assert client.get(f"{prefix}/public/logo").status_code == 200
    assert client.get(f"{prefix}/logged-out").status_code == 200


def test_unknown_page_is_friendly_404(as_teacher, prefix):
    r = as_teacher.get(f"{prefix}/nope")
    assert r.status_code == 404 and "Page not found" in r.text
    r = as_teacher.get(f"{prefix}/api/nope")
    assert r.status_code == 404 and r.json()["error"] == "http_error"
