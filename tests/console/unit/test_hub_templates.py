"""
Compile and render the branded JupyterHub templates in hub-templates/ exactly
the way the Hub loads them (jupyterhub/app.py, JupyterHub 5.3):

    ChoiceLoader([
        PrefixLoader({"templates": FileSystemLoader([stock])}, "/"),
        FileSystemLoader([custom_dir, stock]),
    ])

so `{% extends "templates/page.html" %}` resolves to the stock file and a stock
template that extends "page.html" / "error.html" picks up ours.

The stock templates come from (first hit wins):
    $HUB_STOCK_TEMPLATES           a copy of <jupyterhub data files>/templates
    /opt/conda/share/jupyterhub/templates   (inside the classroom image)
    jupyterhub._data.DATA_FILES_PATH/templates  (jupyterhub importable)
The module is skipped when none of them exists.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader
from markupsafe import Markup, escape

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CUSTOM_DIR = os.path.join(ROOT, "hub-templates")
if not os.path.isdir(CUSTOM_DIR) and ROOT == "/opt/classroom":
    CUSTOM_DIR = "/etc/jupyterhub/templates"
CUSTOM_TEMPLATES = (
    "page.html",
    "login.html",
    "logout.html",
    "error.html",
    "404.html",
    "spawn_pending.html",
    "not_running.html",
)
# Stock templates that extend page.html / error.html and must keep compiling
# with ours in front of them.
STOCK_CHILDREN = (
    "404.html",
    "admin.html",
    "home.html",
    "oauth.html",
    "spawn.html",
    "stop_pending.html",
    "token.html",
    "accept-share.html",
)


def _stock_dir():
    candidates = [os.environ.get("HUB_STOCK_TEMPLATES") or "", "/opt/conda/share/jupyterhub/templates"]
    try:
        from jupyterhub._data import DATA_FILES_PATH  # type: ignore

        candidates.append(os.path.join(DATA_FILES_PATH, "templates"))
    except Exception:  # noqa: BLE001 - not installed on the Mac
        pass
    for d in candidates:
        if d and os.path.isfile(os.path.join(d, "page.html")) and os.path.isfile(os.path.join(d, "login.html")):
            return d
    return None


STOCK_DIR = _stock_dir()
pytestmark = pytest.mark.skipif(
    STOCK_DIR is None,
    reason="stock JupyterHub templates not found (set HUB_STOCK_TEMPLATES=<dir>)",
)


# --- stubs standing in for the Hub's template namespace -------------------------
class _Spawner:
    options_form = None


class _User:
    def __init__(self, name):
        self.name = name
        self.json_escaped_name = name
        self.spawner = _Spawner()


class _Service:
    def __init__(self, name, href):
        self.name = name
        self.href = href


class _Authenticator:
    request_otp = False
    otp_prompt = "OTP:"


def make_env(**options):
    loader = ChoiceLoader(
        [
            PrefixLoader({"templates": FileSystemLoader([STOCK_DIR])}, "/"),
            FileSystemLoader([CUSTOM_DIR, STOCK_DIR]),
        ]
    )
    return Environment(loader=loader, autoescape=True, **options)


def branding(**over):
    b = {
        "school_name": escape("Lincoln High School"),
        "class_name": escape("Grade 10 · Python & Radio Lab"),
        "accent": escape("blue"),
        "accent_hex": "#2c7bb6",
        "announcement": escape(""),
        "class_url": escape("http://192.168.1.10:8000"),
        "logo_custom": False,
        "logo_version": 1726000000,
        "image_version": escape("1.1"),
    }
    b.update(over)
    return b


def context(**over):
    ctx = {
        "base_url": "/hub/",
        "prefix": "/",
        "user": None,
        "login_url": "/hub/login",
        "logout_url": "/hub/logout",
        "login_service": "",
        "static_url": lambda path, **kw: "/hub/static/" + path,
        "version_hash": "abc123",
        "services": [],
        "parsed_scopes": {},
        "expanded_scopes": set(),
        "xsrf": "x",
        "xsrf_token": "x",
        "branding": branding(),
        "announcement_login": "",
        "announcement": "",
        "login_error": "",
        "username": "",
        "authenticator_login_url": "/hub/login?next=",
        "authenticator": _Authenticator(),
        "custom_html": "",
        "next": "",
        "login_term_url": "",
        "logo_url": "",
    }
    ctx.update(over)
    return ctx


def admin_context(**over):
    base = {
        "user": _User("teacher"),
        "parsed_scopes": {"admin-ui": {}, "access:services": {}},
        "services": [_Service("console", "/services/console/")],
    }
    base.update(over)
    return context(**base)


@pytest.fixture(scope="module")
def env():
    return make_env()


def render(env, name, **over):
    return env.get_template(name).render(**context(**over))


# --- compile ---------------------------------------------------------------------
def test_every_custom_template_extends_the_stock_one_without_leading_slash():
    for name in CUSTOM_TEMPLATES:
        with open(os.path.join(CUSTOM_DIR, name), encoding="utf-8") as fh:
            src = fh.read()
        first = src.lstrip().split("\n", 1)[0]
        parent = "error.html" if name == "404.html" else f"templates/{name}"
        assert first == '{%% extends "%s" %%}' % parent, f"{name}: first line is {first!r}"
        assert '"/templates/' not in src, f"{name}: a leading slash does not resolve in JupyterHub 5.3"


@pytest.mark.parametrize("name", CUSTOM_TEMPLATES)
def test_custom_templates_compile_and_are_the_custom_copies(env, name):
    tmpl = env.get_template(name)
    assert tmpl.filename == os.path.join(CUSTOM_DIR, name)


@pytest.mark.parametrize("name", STOCK_CHILDREN)
def test_stock_children_still_compile_on_top_of_our_page(env, name):
    env.get_template(name)  # raises on a template/syntax problem


# --- page.html (the shell) ---------------------------------------------------------
def test_page_anonymous_shows_brand_and_versioned_logo(env):
    html = render(env, "page.html")
    assert "<title>Lincoln High School · JupyterHub</title>" in html.replace("\n", "")
    assert 'src="/hub/logo?v=1726000000"' in html
    assert 'class="cc-brand-text">Lincoln High School</span>' in html
    assert '<meta name="theme-color" content="#2c7bb6">' in html
    assert "--cc-accent: #2c7bb6;" in html
    assert "Powered by JupyterHub · Classroom image v1.1" in html
    # the stock stylesheet and dark-mode plumbing are untouched
    assert 'href="/hub/static/css/style.min.css"' in html
    assert 'src="/hub/static/js/darkmode.js"' in html
    assert 'id="dark-theme-toggle"' in html
    # no navigation for anonymous visitors, so no console link either
    assert "Teacher console" not in html


def test_page_student_nav_has_no_teacher_links(env):
    html = render(env, "page.html", user=_User("amy"), parsed_scopes={})
    assert 'href="/hub/home">Home</a>' in html
    assert "/hub/admin" not in html
    assert "Teacher console" not in html
    assert "/services/console/" not in html
    assert "/hub/token" not in html  # Token link dropped on purpose
    assert "Services" not in html
    assert "amy" in html


def test_page_admin_nav_has_admin_and_console_links_once(env):
    html = env.get_template("page.html").render(**admin_context())
    assert 'href="/hub/admin">Admin</a>' in html
    assert html.count('href="/services/console/"') == 1
    assert "fa-chalkboard-user" in html
    assert "Teacher console" in html
    # the console is the only service, so no empty "Services" dropdown
    assert "dropdown-item" not in html
    assert ">Services</a>" not in html
    assert "/hub/token" not in html


def test_page_services_dropdown_lists_other_services_but_not_the_console(env):
    ctx = admin_context(
        services=[_Service("console", "/services/console/"), _Service("grader", "/services/grader/")]
    )
    html = env.get_template("page.html").render(**ctx)
    assert ">Services</a>" in html
    assert 'class="dropdown-item" href="/services/grader/">grader</a>' in html
    assert html.count("dropdown-item") == 1
    assert html.count('href="/services/console/"') == 1  # the nav link only


def test_page_respects_hub_base_url_prefix(env):
    ctx = admin_context(base_url="/jhub/hub/", prefix="/jhub/")
    html = env.get_template("page.html").render(**ctx)
    assert 'href="/jhub/services/console/"' in html
    assert 'src="/jhub/hub/logo?v=1726000000"' in html
    assert 'href="/jhub/hub/admin"' in html


def test_page_custom_logo_gets_dark_mode_class(env):
    html = render(env, "page.html", branding=branding(logo_custom=True))
    assert 'class="jpy-logo cc-logo--custom"' in html
    html = render(env, "page.html", branding=branding(logo_custom=False))
    assert "cc-logo--custom" not in html.split("<body>", 1)[1]


def test_page_falls_back_when_school_name_is_empty(env):
    html = render(env, "page.html", branding=branding(school_name=escape("")))
    assert "<title>Classroom · JupyterHub</title>" in html.replace("\n", "")
    assert 'alt="JupyterHub logo"' in html
    assert "cc-brand-text" not in html.split("<body>", 1)[1]


def test_page_escapes_user_supplied_branding(env):
    evil = branding(school_name=escape("<b>X</b>"), image_version=escape('1.1"><script>'))
    html = render(env, "page.html", branding=evil)
    assert "&lt;b&gt;X&lt;/b&gt;" in html
    assert "<b>X</b>" not in html
    assert "1.1&#34;&gt;&lt;script&gt;" in html or "1.1&quot;&gt;&lt;script&gt;" in html
    assert '1.1"><script>' not in html


def test_page_exposes_accent_as_hex_and_rgb(env):
    html = render(env, "page.html", branding=branding(accent_hex="#1a8f4e"))
    assert "--cc-accent: #1a8f4e;" in html
    assert "--cc-accent-rgb: 26, 143, 78;" in html
    assert "243,117,36" not in html  # focus ring follows the accent, no hard-coded orange
    # a broken value never breaks the page: it degrades to a usable colour
    html = render(env, "page.html", branding=branding(accent_hex=""))
    assert "--cc-accent: #f37524;" in html and "--cc-accent-rgb: 243, 117, 36;" in html


def test_page_services_may_be_plain_dicts(env):
    # the Hub passes Service objects; dicts (older Hubs, tests) must work too
    ctx = admin_context(
        services=[{"name": "console", "href": "/services/console/"}, {"name": "grader", "href": "/services/grader/"}]
    )
    html = env.get_template("page.html").render(**ctx)
    assert 'class="dropdown-item" href="/services/grader/">grader</a>' in html
    assert html.count('href="/services/console/"') == 1


def test_page_admin_gate_accepts_a_scope_set(env):
    html = render(env, "page.html", user=_User("teacher"), parsed_scopes={"admin-ui"})
    assert "Teacher console" in html and 'href="/hub/admin">Admin</a>' in html
    html = render(env, "page.html", user=_User("amy"), parsed_scopes=set())
    assert "Teacher console" not in html and "/hub/admin" not in html


def test_page_brand_link_honours_logo_url(env):
    html = render(env, "page.html", logo_url="/hub/home")
    assert 'class="cc-brand__link" href="/hub/home"' in html
    html = render(env, "page.html", logo_url="")
    assert 'class="cc-brand__link" href="/hub/"' in html


# --- login.html --------------------------------------------------------------------
def test_login_page_is_the_branded_card_with_stock_ids(env):
    html = render(env, "login.html")
    # brand block
    assert "<h1>Lincoln High School</h1>" in html
    assert "Grade 10 · Python &amp; Radio Lab" in html
    assert 'class="cc-login__logo"' in html and 'src="/hub/logo?v=1726000000"' in html
    assert "<title>Sign in · Lincoln High School</title>" in html.replace("\n", "")
    # form the Hub expects
    assert 'action="/hub/login?next="' in html and 'method="post"' in html
    assert '<input type="hidden" name="_xsrf" value="x">' in html
    for needle in ('id="username_input"', 'name="username"', 'autocomplete="username"',
                   'autocapitalize="off"', 'autocorrect="off"', 'spellcheck="false"',
                   'id="password_input"', 'name="password"', 'autocomplete="current-password"',
                   'id="login_submit"', 'type="submit"', "cc-btn-accent"):
        assert needle in html, needle
    # eye toggle is a real button with state
    assert 'id="password_toggle"' in html and 'aria-pressed="false"' in html
    assert 'aria-label="Show password"' in html
    assert "Sign in" in html and "Forgot your password? Ask your teacher." in html
    # no announcement bar or callout when there is nothing to say (look past the inline CSS)
    body = html.split("<body>", 1)[1]
    assert "cc-login__announcement" not in body
    assert 'class="container text-center announcement' not in body
    assert 'role="alert"' not in body  # (the stock page keeps a hidden .ajax-error modal; that is fine)


def test_login_page_drops_the_insecure_http_warning_and_jquery_script(env):
    html = render(env, "login.html")
    assert "insecure-login-warning" not in html
    assert "unsecured HTTP" not in html
    assert "isSecureContext" not in html
    body = html.split("</head>", 1)[1]
    assert "$('form')" not in body and "$(\"form\")" not in body
    assert "password_toggle" in body and "pageshow" in body


def test_login_autofocus_follows_prefilled_username(env):
    html = render(env, "login.html")
    user_tag = re.search(r"<input id=\"username_input\".*?>", html, re.S).group(0)
    pw_tag = re.search(r"<input id=\"password_input\".*?>", html, re.S).group(0)
    assert "autofocus" in user_tag and "autofocus" not in pw_tag

    html = render(env, "login.html", username="amy")
    user_tag = re.search(r"<input id=\"username_input\".*?>", html, re.S).group(0)
    pw_tag = re.search(r"<input id=\"password_input\".*?>", html, re.S).group(0)
    assert 'value="amy"' in user_tag
    assert "autofocus" not in user_tag and "autofocus" in pw_tag


def test_login_announcement_rendered_once_inside_the_card(env):
    ann = Markup("<br>").join(escape("Homework 3 is due Friday.\nBring <headphones>.").split("\n"))
    html = render(env, "login.html", announcement_login=ann)
    assert html.count("Homework 3 is due Friday.<br>Bring &lt;headphones&gt;.") == 1
    assert "cc-login__announcement" in html and "fa-bullhorn" in html
    assert 'class="container text-center announcement' not in html


def test_login_error_copy(env):
    html = render(env, "login.html", login_error="Invalid username or password", username="amy")
    assert 'role="alert"' in html
    assert "That username or password isn't right. Check your login card and try again." in html
    assert "Invalid username or password" not in html

    html = render(env, "login.html", login_error="Login form invalid or expired. Try again.")
    assert "Login form invalid or expired. Try again." in html
    assert "isn't right" not in html


def test_login_escapes_everything_user_controlled(env):
    html = render(
        env,
        "login.html",
        username='amy" onfocus="alert(1)',
        login_error="<script>alert(1)</script>",
        branding=branding(school_name=escape("<b>X</b>"), class_name=escape("<i>Y</i>")),
    )
    assert "&lt;b&gt;X&lt;/b&gt;" in html and "<b>X</b>" not in html
    assert "&lt;i&gt;Y&lt;/i&gt;" in html and "<i>Y</i>" not in html
    assert "<script>alert(1)</script>" not in html
    assert 'onfocus="alert(1)' not in html
    assert "amy&#34; onfocus=" in html or "amy&quot; onfocus=" in html


def test_login_renders_in_async_environment_like_the_hub():
    aenv = make_env(enable_async=True)
    html = asyncio.run(aenv.get_template("login.html").render_async(**context(username="amy")))
    assert 'id="login_submit"' in html and 'value="amy"' in html


def test_login_keeps_terms_checkbox_and_otp_hooks(env):
    html = render(env, "login.html", login_term_url="/hub/terms")
    assert 'id="login_terms_checkbox"' in html and 'href="/hub/terms"' in html

    class Otp(_Authenticator):
        request_otp = True

    html = render(env, "login.html", authenticator=Otp())
    assert 'id="otp_input"' in html and 'name="otp"' in html


def test_login_custom_logo_gets_dark_mode_backing(env):
    html = render(env, "login.html", branding=branding(logo_custom=True))
    assert 'class="cc-login__logo cc-logo--custom"' in html
    html = render(env, "login.html", branding=branding(logo_custom=False))
    assert 'class="cc-login__logo"' in html


def test_login_falls_back_when_branding_is_empty(env):
    html = render(env, "login.html", branding=branding(school_name=escape(""), class_name=escape("")))
    assert "<h1>Classroom JupyterHub</h1>" in html
    assert "cc-login__class" not in html.split("<body>", 1)[1]
    assert "<title>Sign in · Classroom</title>" in html.replace("\n", "")


# --- error.html --------------------------------------------------------------------
def error_context(status, **over):
    base = {
        "status_code": status,
        "status_message": {403: "Forbidden", 404: "Not Found", 500: "Internal Server Error",
                           503: "Service Unavailable"}.get(status, "Error"),
        "message": "",
        "message_html": "",
        "extra_error_html": "",
        "exception": None,
    }
    base.update(over)
    return context(**base)


def test_error_403_is_teachers_only(env):
    html = env.get_template("error.html").render(
        **error_context(403, user=_User("amy"), message="You do not have permission to access <console>")
    )
    assert "Teachers only" in html
    assert "This page is for your teacher. If you're a student, go back to your JupyterLab." in html
    assert 'href="/hub/home"' in html and "Back to JupyterLab" in html
    assert "<details" in html and "403 Forbidden — You do not have permission to access &lt;console&gt;" in html
    assert "<console>" not in html
    assert "<title>Teachers only · Lincoln High School</title>" in html.replace("\n", "")


def test_error_403_for_a_visitor_who_is_not_signed_in(env):
    html = env.get_template("error.html").render(**error_context(403, user=None))
    assert "Please sign in" in html
    assert "You need to sign in before you can open this page." in html
    assert 'href="/hub/login"' in html and ">Sign in" not in html.split("cc-status__actions")[0]
    assert "Teachers only" not in html and "Back to JupyterLab" not in html
    assert "<title>Please sign in · Lincoln High School</title>" in html.replace("\n", "")


def test_error_403_from_an_expired_form_is_not_teachers_only(env):
    for msg in ("'_xsrf' argument missing from POST", "XSRF cookie does not match POST argument"):
        html = env.get_template("error.html").render(**error_context(403, user=_User("amy"), message=msg))
        assert "That page timed out" in html, msg
        assert "Go back and try again" in html
        assert "Teachers only" not in html
        assert "Back to JupyterLab" in html
        assert "&#39;_xsrf&#39; argument missing from POST" in html or "XSRF cookie does not match" in html


def test_error_404_through_the_stock_404_template(env):
    html = env.get_template("404.html").render(**error_context(404))
    assert "Page not found" in html
    assert "That link doesn't go anywhere. Check the address, or go back to your JupyterLab." in html
    assert "Back to JupyterLab" in html
    assert "cc-status" in html
    assert "moons" not in html  # the stock 404 joke is not placed in the friendly page
    assert html.count("<h1") == 1


def test_error_other_codes_are_friendly_and_keep_details(env):
    html = env.get_template("error.html").render(
        **error_context(500, message_html="<b>db</b> down", extra_error_html="<i>extra</i>")
    )
    assert "Something went wrong (500)" in html
    assert "Try again in a moment. If it keeps happening, tell your teacher." in html
    assert "<b>db</b> down" in html and "<i>extra</i>" in html  # trusted Hub html, rendered as-is
    assert "_remove_redirects_from_url" in html  # stock script kept

    html = env.get_template("error.html").render(**error_context(503))
    assert "busy right now" in html


# --- spawn_pending.html / not_running.html / logout.html -----------------------------
def test_spawn_pending_keeps_progress_plumbing(env):
    html = env.get_template("spawn_pending.html").render(
        **context(user=_User("amy"), progress_url="/hub/api/users/amy/server/progress?_xsrf=x")
    )
    assert "Getting your JupyterLab ready…" in html
    assert "This usually takes a few seconds. You'll be taken there automatically." in html
    for needle in ('id="progress-bar"', 'id="sr-progress"', 'id="progress-message"',
                   'id="progress-details"', 'id="progress-log"',
                   'new EventSource("/hub/api/users/amy/server/progress?_xsrf=x")'):
        assert needle in html, needle
    assert html.count("<h1") == 1


def test_not_running_start_button_and_copy(env):
    base = dict(user=_User("amy"), server_name="", spawn_url="/hub/spawn/amy", failed=False)
    html = env.get_template("not_running.html").render(**context(**base))
    assert "Your JupyterLab isn't running right now" in html
    assert "Click below to start it. Your files are exactly where you left them." in html
    assert re.search(r'<a id="start"\s+role="button"\s+class="btn btn-lg cc-btn-accent"\s+href="/hub/spawn/amy">', html)
    assert "Start my JupyterLab" in html
    assert 'require(["not_running"])' in html
    assert "Technical details" not in html

    html = env.get_template("not_running.html").render(
        **context(**dict(base, failed=True, failed_message="<boom> exit 1"))
    )
    assert "Your JupyterLab couldn't start" in html
    assert "tell your teacher" in html
    assert "Try again" in html and 'id="start"' in html
    assert "&lt;boom&gt; exit 1" in html and "<boom>" not in html

    html = env.get_template("not_running.html").render(**context(**dict(base, implicit_spawn_seconds=3)))
    assert "starting automatically" in html
    assert "var implicit_spawn_seconds = 3;" in html  # stock auto-redirect script kept

    html = env.get_template("not_running.html").render(**context(**dict(base, server_name="gpu")))
    assert "Start my JupyterLab (gpu)" in html


def test_logout_page(env):
    html = render(env, "logout.html")
    assert "You're signed out." in html and "See you next time." in html
    assert 'href="/hub/login"' in html and "Sign in again" in html
    assert 'id="logout-main"' in html


def test_all_pages_have_footer_and_exactly_one_h1(env):
    pages = {
        "login.html": context(),
        "logout.html": context(),
        "error.html": error_context(403),
        "not_running.html": context(user=_User("amy"), server_name="", spawn_url="/hub/spawn/amy", failed=False),
        "spawn_pending.html": context(user=_User("amy"), progress_url="/p"),
    }
    for name, ctx in pages.items():
        html = env.get_template(name).render(**ctx)
        assert "Classroom image v1.1" in html, name
        assert html.count("<h1") == 1, name
        assert 'id="dark-theme-toggle"' in html, name
