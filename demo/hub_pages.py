"""Branded Hub sign-in and a lightweight student workspace for the demo."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader
from markupsafe import Markup, escape

from console.roster import Roster
from console.util import read_branding
from demo.classroom import ROOT

REPO = ROOT.parent

CUSTOM_DIR = REPO / "hub-templates"
STUDENT_COOKIE = "demo-student"


def stock_templates_dir() -> str | None:
    candidates = [os.environ.get("HUB_STOCK_TEMPLATES") or "", "/opt/conda/share/jupyterhub/templates"]
    try:
        from jupyterhub._data import DATA_FILES_PATH  # type: ignore

        candidates.append(os.path.join(DATA_FILES_PATH, "templates"))
    except Exception:  # noqa: BLE001
        pass
    for d in candidates:
        if d and os.path.isfile(os.path.join(d, "page.html")) and os.path.isfile(os.path.join(d, "login.html")):
            return d
    return None


def hub_static_dir() -> str | None:
    env = os.environ.get("HUB_STOCK_STATIC") or ""
    candidates = [env, "/opt/conda/share/jupyterhub/static"]
    try:
        from jupyterhub._data import DATA_FILES_PATH  # type: ignore

        candidates.append(os.path.join(DATA_FILES_PATH, "static"))
    except Exception:  # noqa: BLE001
        pass
    for d in candidates:
        if d and os.path.isdir(d) and os.path.isfile(os.path.join(d, "css", "style.min.css")):
            return d
    return None


def _hub_env(stock: str) -> Environment:
    loader = ChoiceLoader(
        [
            PrefixLoader({"templates": FileSystemLoader([stock])}, "/"),
            FileSystemLoader([str(CUSTOM_DIR), stock]),
        ]
    )
    return Environment(loader=loader, autoescape=True)


def _branding_for_hub(settings) -> dict:
    raw = read_branding(settings.branding_json)
    try:
        logo_version = int(os.stat(settings.logo_file).st_mtime)
    except OSError:
        logo_version = 0
    announcement = raw.get("announcement") or ""
    announcement_html = Markup("<br>").join(escape(announcement).split("\n")) if announcement else ""
    return {
        "school_name": escape(raw.get("school_name") or "Classroom JupyterHub"),
        "class_name": escape(raw.get("class_name") or ""),
        "accent": escape(raw.get("accent") or "orange"),
        "accent_hex": raw.get("accent_hex") or "#f37524",
        "announcement": escape(announcement),
        "class_url": escape(raw.get("class_url") or ""),
        "logo_custom": bool(raw.get("logo_custom")),
        "logo_version": logo_version,
        "image_version": escape(settings.image_version),
        "announcement_html": announcement_html,
    }


class _Authenticator:
    request_otp = False
    otp_prompt = "OTP:"


def render_login(settings, *, username: str = "", login_error: str = "") -> str:
    stock = stock_templates_dir()
    branding = _branding_for_hub(settings)
    if stock is None:
        return _fallback_login(settings, branding, username=username, login_error=login_error)
    env = _hub_env(stock)
    announcement_login = branding["announcement_html"]
    ctx = {
        "base_url": "/hub/",
        "prefix": "/",
        "user": None,
        "login_url": "/hub/login",
        "logout_url": "/hub/logout",
        "login_service": "",
        "static_url": lambda path, **kw: "/hub/static/" + path,
        "version_hash": "demo",
        "services": [],
        "parsed_scopes": {},
        "expanded_scopes": set(),
        "xsrf": "demo",
        "xsrf_token": "demo",
        "branding": branding,
        "announcement_login": announcement_login,
        "announcement": "",
        "login_error": login_error,
        "username": username,
        "authenticator_login_url": "/hub/login",
        "authenticator": _Authenticator(),
        "custom_html": "",
        "next": "",
        "login_term_url": "",
        "logo_url": f"/hub/logo?v={branding['logo_version']}",
    }
    return env.get_template("login.html").render(ctx)


def _fallback_login(settings, branding, *, username: str, login_error: str) -> str:
    err = f'<div class="alert alert-danger" role="alert">{html.escape(login_error)}</div>' if login_error else ""
    announcement = ""
    if branding["announcement"]:
        announcement = (
            f'<div class="cc-login__announcement" role="note"><i class="fa fa-bullhorn"></i>'
            f'<div>{branding["announcement_html"]}</div></div>'
        )
    klass = f'<p class="cc-login__class">{branding["class_name"]}</p>' if branding["class_name"] else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · {branding["school_name"]}</title>
<link rel="stylesheet" href="/hub/static/css/style.min.css">
<style>
  :root {{ --cc-accent: {branding["accent_hex"]}; }}
  body {{ background: #f4f6f8; font-family: system-ui, sans-serif; }}
  .cc-login {{ max-width: 420px; margin: 8vh auto; padding: 0 1rem; }}
  .cc-login__brand {{ text-align: center; margin-bottom: 1.25rem; }}
  .cc-login__logo {{ height: 56px; }}
  .cc-login__card {{ background: #fff; border: 1px solid #dee2e6; border-top: 4px solid var(--cc-accent);
    border-radius: .75rem; padding: 1.5rem; }}
  .cc-btn-accent {{ background: var(--cc-accent); border-color: var(--cc-accent); color: #fff; }}
  .cc-login__hint {{ margin-top: 1rem; color: #6c757d; }}
</style></head>
<body>
<div class="cc-login">
  <div class="cc-login__brand">
    <img class="cc-login__logo" src="/hub/logo?v={branding["logo_version"]}" alt="">
    <h1>{branding["school_name"]}</h1>{klass}
  </div>
  <div class="cc-login__card">
    {announcement}{err}
    <form method="post" action="/hub/login" aria-label="Sign in">
      <label for="username_input">Username</label>
      <input id="username_input" class="form-control" name="username" value="{html.escape(username)}" required>
      <label for="password_input" class="mt-2">Password</label>
      <input id="password_input" class="form-control" type="password" name="password" required>
      <button class="btn btn-lg w-100 cc-btn-accent mt-3" type="submit">Sign in</button>
    </form>
    <p class="cc-login__hint">Forgot your password? Ask your teacher.</p>
  </div>
</div>
</body></html>"""


def lookup_student(settings, username: str):
    name = (username or "").strip().lower()
    if not name:
        return None
    try:
        students = Roster(settings.roster_path).load()
    except Exception:  # noqa: BLE001
        return None
    for s in students:
        if s.username == name:
            return s
    return None


def landing_html(settings) -> str:
    b = read_branding(settings.branding_json)
    school = html.escape(b.get("school_name") or "Classroom")
    klass = html.escape(b.get("class_name") or "")
    students = []
    try:
        students = [s for s in Roster(settings.roster_path).load() if s.username not in settings.admin_users]
    except Exception:  # noqa: BLE001
        pass
    rows = "".join(
        f"<tr><td>{html.escape(s.display_name or s.username)}</td>"
        f"<td><code>{html.escape(s.username)}</code></td>"
        f"<td><code>{html.escape(s.password)}</code></td></tr>"
        for s in students
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Complete demo · {school}</title>
<link rel="stylesheet" href="/hub/static/css/style.min.css">
<link rel="stylesheet" href="/services/console/static/console.css">
<style>
  :root {{ --cc-accent: {html.escape(b.get("accent_hex") or "#f37524")}; }}
  body {{ background: #f4f6f8; margin: 0; font-family: system-ui, sans-serif; color: #212529; }}
  .demo-wrap {{ max-width: 960px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }}
  .demo-hero {{ display: flex; gap: 1.25rem; align-items: center; margin-bottom: 2rem; }}
  .demo-hero img {{ height: 56px; }}
  .demo-hero h1 {{ margin: 0; font-size: 1.75rem; }}
  .demo-hero p {{ margin: .25rem 0 0; color: #6c757d; }}
  .demo-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
  @media (max-width: 800px) {{ .demo-grid {{ grid-template-columns: 1fr; }} }}
  .demo-card {{ background: #fff; border: 1px solid #dee2e6; border-radius: .75rem; padding: 1.25rem 1.4rem;
    border-top: 4px solid var(--cc-accent); }}
  .demo-card h2 {{ font-size: 1.15rem; margin: 0 0 .5rem; }}
  .demo-card p {{ color: #495057; }}
  table {{ width: 100%; }}
  .note {{ margin-top: 1.5rem; color: #6c757d; font-size: .9rem; }}
</style></head>
<body>
<main class="demo-wrap">
  <div class="demo-hero">
    <img src="/hub/logo" alt="">
    <div>
      <h1>{school}</h1>
      <p>{klass or "Classroom JupyterHub"} · complete demo (no Docker image required)</p>
    </div>
  </div>
  <div class="demo-grid">
    <section class="demo-card">
      <h2>Student sign-in</h2>
      <p>The branded JupyterHub login page. Sign in as any student below. JupyterLab itself lives in the classroom image; this demo shows the files that would be in that student's home.</p>
      <p><a class="btn btn-primary" href="/hub/login">Open the sign-in page</a></p>
      <table class="table table-sm"><thead><tr><th>Name</th><th>Username</th><th>Password</th></tr></thead>
      <tbody>{rows}</tbody></table>
    </section>
    <section class="demo-card">
      <h2>Teacher console</h2>
      <p>Every page the teacher uses in class: dashboard, students, login cards, handouts, submissions, backups, logs and settings. Signed in as <code>teacher</code>.</p>
      <p><a class="btn btn-primary" href="/services/console/">Open the teacher console</a></p>
      <p class="mb-0"><a href="/services/console/students">Students</a> ·
         <a href="/services/console/files">Handouts</a> ·
         <a href="/services/console/print/cards">Login cards</a></p>
    </section>
  </div>
  <p class="note">This run uses the project's test doubles (FakeHub / FakeSystem), so no Linux accounts are created on this computer. Sample lesson notebooks are in <code>demo/handouts/</code> and already sit in every student's <code>shared</code> folder.</p>
</main>
</body></html>"""


def student_cookie_user(request: Request) -> str | None:
    return request.cookies.get(STUDENT_COOKIE) or None


def set_student_cookie(response, username: str) -> None:
    response.set_cookie(STUDENT_COOKIE, username, max_age=12 * 3600, path="/", httponly=True, samesite="lax")


def clear_student_cookie(response) -> None:
    response.delete_cookie(STUDENT_COOKIE, path="/")


def _safe_rel(root: Path, rel: str) -> Path | None:
    """Stay under the student's home. Do not resolve() — ~/shared is a symlink out."""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        return root
    parts = Path(rel).parts
    if ".." in parts or any(p.startswith("/") for p in parts):
        return None
    return root.joinpath(*parts)


def render_workspace(settings, username: str, rel: str = "") -> HTMLResponse:
    student = lookup_student(settings, username)
    if student is None:
        return HTMLResponse("Unknown student.", status_code=404)
    home = Path(settings.home_root) / username
    target = _safe_rel(home, rel)
    if target is None:
        return HTMLResponse("Invalid path.", status_code=400)
    if target.is_file():
        return HTMLResponse(_file_view(settings, student, home, target))
    if not target.exists():
        return HTMLResponse("Not found.", status_code=404)
    return HTMLResponse(_folder_view(settings, student, home, target))


def _crumbs(username: str, home: Path, target: Path) -> str:
    rel = target.relative_to(home) if target != home else Path()
    parts = [f'<a href="/user/{username}/lab">home</a>']
    acc = Path()
    if rel != Path():
        for part in rel.parts:
            acc = acc / part
            parts.append(f'<a href="/user/{username}/lab/tree/{quote(str(acc))}">{html.escape(part)}</a>')
    return " / ".join(parts)


def _folder_view(settings, student, home: Path, target: Path) -> str:
    b = read_branding(settings.branding_json)
    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if item.name.startswith("."):
            continue
        rel = item.relative_to(home).as_posix()
        href = f"/user/{student.username}/lab/tree/{quote(rel)}"
        kind = "folder" if item.is_dir() else "file"
        icon = "fa-folder" if item.is_dir() else "fa-file"
        entries.append(
            f'<tr><td><i class="fa {icon}"></i> <a href="{href}">{html.escape(item.name)}</a></td>'
            f"<td>{kind}</td></tr>"
        )
    rows = "".join(entries) or '<tr><td colspan="2" class="text-muted">This folder is empty.</td></tr>'
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>{html.escape(student.display_name or student.username)} · JupyterLab (demo)</title>
<link rel="stylesheet" href="/hub/static/css/style.min.css">
<style>
  :root {{ --cc-accent: {html.escape(b.get("accent_hex") or "#f37524")}; }}
  body {{ margin: 0; font-family: system-ui, sans-serif; background: #fff; }}
  header {{ display: flex; align-items: center; gap: 1rem; padding: .75rem 1.25rem; border-bottom: 1px solid #dee2e6; }}
  header img {{ height: 36px; }}
  .banner {{ background: #fff3cd; color: #664d03; padding: .6rem 1.25rem; font-size: .9rem; }}
  main {{ padding: 1.25rem 1.5rem; }}
</style></head>
<body>
<header>
  <img src="/hub/logo" alt="">
  <div>
    <strong>{html.escape(student.display_name or student.username)}</strong>
    <div class="text-muted">JupyterLab workspace · demo</div>
  </div>
  <span class="ms-auto"><a href="/hub/logout">Sign out</a></span>
</header>
<div class="banner">This is a file view of the student's home. Full JupyterLab (kernels, notebooks, terminals) runs inside the classroom Docker image.</div>
<main>
  <p>{_crumbs(student.username, home, target)}</p>
  <table class="table"><thead><tr><th>Name</th><th>Type</th></tr></thead><tbody>{rows}</tbody></table>
</main>
</body></html>"""


def _file_view(settings, student, home: Path, target: Path) -> str:
    b = read_branding(settings.branding_json)
    rel = target.relative_to(home).as_posix()
    body: str
    if target.suffix == ".ipynb":
        body = _notebook_html(target)
    elif target.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        body = f'<p class="text-muted">{html.escape(rel)} is an image file.</p>'
    else:
        text = target.read_text(encoding="utf-8", errors="replace")
        body = f"<pre>{html.escape(text)}</pre>"
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>{html.escape(target.name)} · demo</title>
<link rel="stylesheet" href="/hub/static/css/style.min.css">
<style>
  body {{ margin: 0; font-family: system-ui, sans-serif; }}
  header {{ padding: .75rem 1.25rem; border-bottom: 1px solid #dee2e6; }}
  main {{ padding: 1.25rem 1.5rem; max-width: 900px; }}
  .nb-md, .nb-code {{ margin: 0 0 1rem; padding: 1rem; border-radius: .5rem; }}
  .nb-md {{ background: #f8f9fa; }}
  .nb-code {{ background: #212529; color: #f8f9fa; overflow: auto; }}
</style></head>
<body>
<header>
  <div>{_crumbs(student.username, home, target.parent)} / {html.escape(target.name)}</div>
</header>
<main>{body}</main>
</body></html>"""


def _notebook_html(path: Path) -> str:
    try:
        nb = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"<p>Could not read notebook: {html.escape(str(e))}</p>"
    chunks = []
    for cell in nb.get("cells") or []:
        src = cell.get("source") or ""
        if isinstance(src, list):
            src = "".join(src)
        if cell.get("cell_type") == "markdown":
            chunks.append(f'<div class="nb-md">{html.escape(src).replace(chr(10), "<br>")}</div>')
        else:
            chunks.append(f"<pre class=\"nb-code\">{html.escape(src)}</pre>")
    return "".join(chunks) or "<p>Empty notebook.</p>"
