"""
End-to-end check of the teacher console, run INSIDE the container as root:

    docker compose exec -w /opt/classroom jupyterhub python tests/integration/console_check.py

It behaves like a browser: signs in to JupyterHub as the admin, follows the
OAuth hand-off into the console, adds a throw-away student through the
console API, checks the Linux account and home folder, proves the student can
sign in immediately (no restart), proves the student cannot open the console,
removes the student again and checks the archive. Exit code 1 on any failure.
Only the standard library is used so it also works on a bare server.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import pwd
import random
import re
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("CONSOLE_CHECK_BASE", "http://127.0.0.1:8000")
PREFIX = "/services/console"
ROSTER = os.environ.get("CONSOLE_CHECK_ROSTER", "/srv/jupyterhub/roster.json")
REMOVED_DIR = "/srv/jupyterhub/removed"
ADMIN = os.environ.get("CONSOLE_CHECK_ADMIN", os.environ.get("JUPYTERHUB_ADMIN_USERS", "teacher").split(",")[0].strip())

failures: list[str] = []


def ok(cond: bool, msg: str) -> bool:
    print(("  ok    " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)
    return cond


class Browser:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, data=None, headers=None, follow=True, json_body=None):
        url = path if path.startswith("http") else BASE + path
        body = None
        hdrs = {"Accept": "text/html,application/json"}
        hdrs.update(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        if not follow:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(self.jar))
        else:
            opener = self.opener
        try:
            resp = opener.open(req, timeout=30)
            return resp.status, resp.geturl(), resp.read().decode("utf-8", "replace"), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.geturl(), e.read().decode("utf-8", "replace"), dict(e.headers)

    def cookie(self, name: str):
        for c in self.jar:
            if c.name == name:
                return c.value
        return None

    def hub_login(self, username: str, password: str) -> int:
        self.request("GET", "/hub/login")
        xsrf = self.cookie("_xsrf")
        status, url, _, _ = self.request("POST", "/hub/login", data={"username": username, "password": password, "_xsrf": xsrf}, follow=False)
        return status


def api(b: Browser, method: str, path: str, json_body=None):
    status, _, text, headers = b.request(method, f"{PREFIX}/api/{path}", json_body=json_body, headers={"X-Console-Request": "1", "Accept": "application/json"})
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = text
    return status, data


def main() -> int:
    roster = json.load(open(ROSTER))
    admin_entry = next((u for u in roster["users"] if u["username"] == ADMIN), None)
    if not admin_entry:
        print(f"admin '{ADMIN}' not found in {ROSTER}")
        return 1
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    student = f"zzcheck{suffix}"
    print(f"== teacher '{ADMIN}' signs in to the Hub and opens the console")
    teacher = Browser()
    ok(teacher.hub_login(ADMIN, admin_entry["password"]) in (302, 303), "Hub login as admin")
    status, url, text, _ = teacher.request("GET", f"{PREFIX}/")
    ok(status == 200 and url.startswith(BASE + PREFIX) and "Teacher console" in text, f"OAuth hand-off lands on the console ({status} {url})")
    ok(teacher.cookie("console-session") is not None, "console session cookie set")
    status, who = api(teacher, "GET", "whoami")
    ok(status == 200 and isinstance(who, dict) and who.get("name") == ADMIN, "api/whoami answers for the admin")

    print(f"== add student '{student}' through the console")
    status, created = api(teacher, "POST", "students", {"display_name": "Check Student", "username": student})
    ok(status == 201 and isinstance(created, dict), f"POST api/students -> {status}")
    password = (created or {}).get("student", {}).get("password") if isinstance(created, dict) else None
    ok(bool(password), "generated password returned")
    try:
        pw = pwd.getpwnam(student)
        ok(pw.pw_uid >= 2000 and pw.pw_gid == 3000, f"Linux account exists (uid {pw.pw_uid}, gid {pw.pw_gid})")
    except KeyError:
        ok(False, "Linux account exists")
        pw = None
    home = f"/home/{student}"
    ok(os.path.isdir(home) and oct(os.stat(home).st_mode & 0o777) == "0o750", "home folder exists with mode 0750")
    ok(os.path.islink(f"{home}/shared") and os.path.isdir(f"{home}/submit"), "shared link and submit folder present")
    if os.path.isdir("/opt/classroom/skel") and os.listdir("/opt/classroom/skel"):
        ok(os.path.exists(f"{home}/Welcome.ipynb"), "welcome kit copied")
    roster = json.load(open(ROSTER))
    ok(any(u["username"] == student for u in roster["users"]), "student is in roster.json")
    status, listing = api(teacher, "GET", "students")
    ok(status == 200 and any(s["username"] == student for s in (listing or {}).get("students", [])), "student appears in GET api/students")

    print("== the new student can sign in right away, but not open the console")
    s_browser = Browser()
    ok(s_browser.hub_login(student, password or "") in (302, 303), "student Hub login succeeds without a restart")
    status, url, text, _ = s_browser.request("GET", f"{PREFIX}/")
    ok(status == 403, f"student is refused at the console ({status})")

    print("== reset password, then remove the student")
    status, reset = api(teacher, "POST", f"students/{student}/password", {"password": None})
    ok(status == 200 and reset.get("password") and reset["password"] != password, "password reset returns a new password")
    s2 = Browser()
    ok(s2.hub_login(student, reset.get("password", "")) in (302, 303), "student can sign in with the new password")
    status, removed = api(teacher, "DELETE", f"students/{student}?home=archive")
    ok(status == 200 and isinstance(removed, dict), f"DELETE api/students -> {status}")
    try:
        pwd.getpwnam(student)
        ok(False, "Linux account removed")
    except KeyError:
        ok(True, "Linux account removed")
    ok(not os.path.exists(home), "home folder removed")
    archive = (removed or {}).get("archived_to") if isinstance(removed, dict) else None
    if archive:
        archive = os.path.join(REMOVED_DIR, os.path.basename(archive))
    ok(bool(archive) and os.path.isfile(archive), f"archive created: {archive}")
    if archive and os.path.isfile(archive):
        listing = subprocess.run(["/usr/bin/tar", "-tzf", archive], capture_output=True, text=True).stdout
        ok(f"{student}/submit" in listing or f"{student}/" in listing, "archive lists the student's files")
        os.unlink(archive)
    roster = json.load(open(ROSTER))
    ok(not any(u["username"] == student for u in roster["users"]), "student removed from roster.json")
    s3 = Browser()
    ok(s3.hub_login(student, reset.get("password", "")) not in (302, 303), "removed student can no longer sign in")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print("  -", f)
        return 1
    print("All console checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
