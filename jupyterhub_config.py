"""
JupyterHub configuration for the classroom image.

Knobs a teacher may want to turn are read from environment variables that
compose.yaml sets:
    JUPYTERHUB_ADMIN_USERS   comma-separated admin usernames   (default: teacher)
    IDLE_TIMEOUT_SECONDS     stop a student's server after this much idle time,
                             0 disables                        (default: 7200 = 2 h)
    SHUTDOWN_ON_LOGOUT       "true" stops a student's server when they log out
    CONSOLE_ENABLED          "1" runs the teacher console as a Hub service
    CLASSROOM_URL            public address printed on login cards (optional)

This file deliberately does NOT import the console package: a bug in the
console must never stop the Hub from loading its configuration.
"""
import json
import os
import sys

from markupsafe import Markup, escape

c = get_config()  # noqa: F821  (injected by JupyterHub when it loads this file)

STATE_DIR = "/srv/jupyterhub"                 # Docker volume "hubstate"
LOG_DIR = f"{STATE_DIR}/logs"
ROSTER_PATH = f"{STATE_DIR}/roster.json"      # source of truth for accounts (console + entrypoint)
USERS_TXT = "/etc/jupyterhub/users.txt"       # seed file, only used before roster.json exists
BRANDING_JSON = f"{STATE_DIR}/branding/branding.json"
LOGO_FILE = f"{STATE_DIR}/branding/logo.png"
TEMPLATE_DIR = "/etc/jupyterhub/templates"
CONSOLE_PORT = 8099

# --- Network -----------------------------------------------------------------
# Listen on every interface so laptops on the LAN can reach the Hub.
c.JupyterHub.bind_url = "http://0.0.0.0:8000"
# Hub <-> student servers (and the console) talk inside the container only.
c.JupyterHub.hub_ip = "127.0.0.1"

# --- Persistence (everything under /srv/jupyterhub, which is a Docker volume) ---
c.JupyterHub.db_url = f"sqlite:///{STATE_DIR}/jupyterhub.sqlite"
c.JupyterHub.cookie_secret_file = f"{STATE_DIR}/jupyterhub_cookie_secret"
c.ConfigurableHTTPProxy.pid_file = f"{STATE_DIR}/jupyterhub-proxy.pid"

# --- Hub log to a file as well (the teacher console shows it) -------------------
# Config values are deep-copied by traitlets, so a handler *object* cannot go in
# the config; instead extend the standard logging dictConfig. traitlets merges
# this into its defaults (which define the "console" handler).
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    c.JupyterHub.logging_config = {
        "formatters": {
            "file": {
                "format": "[%(levelname)1.1s %(asctime)s %(name)s %(module)s:%(lineno)d] %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            }
        },
        "handlers": {
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": f"{LOG_DIR}/jupyterhub.log",
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
                "encoding": "utf-8",
                "formatter": "file",
            }
        },
        "loggers": {"JupyterHub": {"handlers": ["console", "file"]}},
    }
except OSError:
    pass  # read-only or missing state dir: stdout only

# --- Who may log in ------------------------------------------------------------
# Accounts are ordinary Linux users created at start-up from roster.json (or,
# on the very first start, from users.txt). PAM checks the password.
c.JupyterHub.authenticator_class = "pam"
c.PAMAuthenticator.service = "jupyterhub"        # /etc/pam.d/jupyterhub
c.PAMAuthenticator.open_sessions = False


def _roster_usernames(path=ROSTER_PATH):
    """Usernames from roster.json; empty set if the file is missing or broken."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        users = data.get("users", []) if isinstance(data, dict) else data
        return {
            str(u["username"]).strip().lower()
            for u in users
            if isinstance(u, dict) and u.get("username")
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


def _users_txt_usernames(path=USERS_TXT):
    """Usernames from users.txt, parsed with the same rules as the account sync."""
    names = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if ":" in line:
                    name = line.split(":", 1)[0].strip().lower()
                    if name:
                        names.add(name)
    except OSError:
        pass
    return names


_admins = {
    u.strip().lower()
    for u in os.environ.get("JUPYTERHUB_ADMIN_USERS", "teacher").split(",")
    if u.strip()
}
c.Authenticator.admin_users = _admins

_users = _roster_usernames() or _users_txt_usernames()
if _users:
    # Registering the accounts here makes every student show up in the admin
    # panel before their first login. Users the console adds later are allowed
    # automatically (allow_existing_users) without a Hub restart.
    c.Authenticator.allowed_users = _users | _admins
    c.Authenticator.allow_existing_users = True
else:
    c.Authenticator.allow_all = True             # fallback: any Linux account

# --- How each student's JupyterLab is started -------------------------------
# LocalProcessSpawner = run "jupyterhub-singleuser" as that Linux user, in the
# same container. Simple, no Docker socket needed, works offline.
c.JupyterHub.spawner_class = "localprocess"
c.Spawner.default_url = "/lab"                   # open JupyterLab, not the classic UI
c.Spawner.notebook_dir = "~"                     # each student sees their own home
c.Spawner.environment = {
    "PATH": os.environ.get("PATH", "/opt/conda/bin:/usr/local/bin:/usr/bin:/bin"),
}
c.Spawner.start_timeout = 120
c.Spawner.http_timeout = 60

c.JupyterHub.shutdown_on_logout = (
    os.environ.get("SHUTDOWN_ON_LOGOUT", "false").lower() == "true"
)

# --- Branding (login page text, logo) ------------------------------------------
# The console writes branding.json + logo.png on the volume. The callables
# below are evaluated on every page render, so changes show up without a
# restart. Every string is escaped here because some Hub templates render
# these values with "| safe".
c.JupyterHub.template_paths = [TEMPLATE_DIR]
c.JupyterHub.logo_file = LOGO_FILE

ACCENTS = {
    "orange": "#f37524",
    "blue": "#2c7bb6",
    "green": "#1a8f4e",
    "purple": "#6f42c1",
    "teal": "#0d8a8a",
    "slate": "#495057",
}
_BRANDING_DEFAULTS = {
    "school_name": "Classroom JupyterHub",
    "class_name": "",
    "accent": "orange",
    "announcement": "",
    "class_url": "",
    "logo_custom": False,
}
_BRANDING_LIMITS = {"school_name": 80, "class_name": 120, "announcement": 300, "class_url": 200}
_branding_cache = {"key": None, "value": None}


def _image_version():
    v = os.environ.get("CLASSROOM_IMAGE_VERSION", "").strip()
    if not v:
        try:
            with open("/etc/classroom-version", encoding="utf-8") as fh:
                v = fh.read().strip()
        except OSError:
            v = "dev"
    return escape(v or "dev")


def _branding(user=None):
    """Sanitised branding dict for templates. Never raises."""
    try:
        json_key = os.stat(BRANDING_JSON).st_mtime_ns
    except OSError:
        json_key = None
    try:
        logo_version = int(os.stat(LOGO_FILE).st_mtime)
    except OSError:
        logo_version = 0
    key = (json_key, logo_version)
    if key == _branding_cache["key"]:
        return _branding_cache["value"]

    raw = {}
    if json_key is not None:
        try:
            with open(BRANDING_JSON, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, ValueError):
            raw = {}

    value = {}
    for field, default in _BRANDING_DEFAULTS.items():
        v = raw.get(field, default)
        if isinstance(default, bool):
            value[field] = bool(v)
        else:
            v = "" if v is None else str(v)
            value[field] = escape(v[: _BRANDING_LIMITS.get(field, 200)])
    if str(value["accent"]) not in ACCENTS:
        value["accent"] = escape("orange")
    value["accent_hex"] = ACCENTS[str(value["accent"])]
    value["logo_version"] = logo_version
    value["image_version"] = _image_version()
    _branding_cache["key"] = key
    _branding_cache["value"] = value
    return value


def _announcement_login(user=None):
    """Login-page announcement as safe HTML (escaped, newlines -> <br>)."""
    text = _branding()["announcement"]
    if not text:
        return ""
    return Markup("<br>").join(escape(str(text)).split("\n"))


c.JupyterHub.template_vars = {
    "branding": _branding,
    "announcement_login": _announcement_login,
}

# --- Services and roles ---------------------------------------------------------
_services = []
_roles = []

if os.environ.get("CONSOLE_ENABLED", "1") == "1":
    _services.append(
        {
            "name": "console",
            "url": f"http://127.0.0.1:{CONSOLE_PORT}",
            "command": [sys.executable, "-m", "console"],
            "cwd": "/opt/classroom",
            "oauth_no_confirm": True,            # no "authorize?" page for the teacher
            "environment": {
                "PYTHONPATH": "/opt/classroom",
                "PATH": "/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME": "/root",
                "USER": "root",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                "JUPYTERHUB_ADMIN_USERS": os.environ.get("JUPYTERHUB_ADMIN_USERS", "teacher"),
                "CLASSROOM_URL": os.environ.get("CLASSROOM_URL", ""),
                "CLASSROOM_IMAGE_VERSION": str(_image_version()),
            },
        }
    )
    _roles.append(
        {
            "name": "console-role",
            "description": "Teacher console: manage students and their servers",
            "scopes": [
                "list:users",
                "read:users",
                "read:users:activity",
                "read:servers",
                "servers",
                "delete:servers",
                "admin:users",
                "delete:users",
            ],
            "services": ["console"],
        }
    )

# Idle culler: free RAM by stopping servers nobody has touched in a while.
idle_timeout = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "7200"))
if idle_timeout > 0:
    _services.append(
        {
            "name": "jupyterhub-idle-culler-service",
            "command": [
                sys.executable,
                "-m", "jupyterhub_idle_culler",
                f"--timeout={idle_timeout}",
            ],
        }
    )
    _roles.append(
        {
            "name": "jupyterhub-idle-culler-role",
            "scopes": [
                "list:users",
                "read:users:activity",
                "read:servers",
                "delete:servers",
            ],
            "services": ["jupyterhub-idle-culler-service"],
        }
    )

c.JupyterHub.services = _services
c.JupyterHub.load_roles = _roles

c.JupyterHub.log_level = "INFO"
