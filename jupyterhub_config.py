"""
JupyterHub configuration for the classroom image.

Knobs a teacher may want to turn are read from environment variables that
compose.yaml sets:
    JUPYTERHUB_ADMIN_USERS   comma-separated admin usernames   (default: teacher)
    IDLE_TIMEOUT_SECONDS     stop a student's server after this much idle time,
                             0 disables                        (default: 7200 = 2 h)
    SHUTDOWN_ON_LOGOUT       "true" stops a student's server when they log out
"""
import os
import sys

c = get_config()  # noqa: F821  (injected by JupyterHub when it loads this file)

# --- Network -----------------------------------------------------------------
# Listen on every interface so laptops on the LAN can reach the Hub.
c.JupyterHub.bind_url = "http://0.0.0.0:8000"
# Hub <-> student servers talk to each other inside the container only.
c.JupyterHub.hub_ip = "127.0.0.1"

# --- Persistence (everything under /srv/jupyterhub, which is a Docker volume) ---
c.JupyterHub.db_url = "sqlite:////srv/jupyterhub/jupyterhub.sqlite"
c.JupyterHub.cookie_secret_file = "/srv/jupyterhub/jupyterhub_cookie_secret"
c.ConfigurableHTTPProxy.pid_file = "/srv/jupyterhub/jupyterhub-proxy.pid"

# --- Who may log in ------------------------------------------------------------
# Accounts are ordinary Linux users created by entrypoint.sh from users.txt.
# PAM checks the password against those accounts.
c.JupyterHub.authenticator_class = "pam"
c.PAMAuthenticator.service = "jupyterhub"        # /etc/pam.d/jupyterhub
c.PAMAuthenticator.open_sessions = False


def _read_users(path="/etc/jupyterhub/users.txt"):
    """Usernames from users.txt, parsed with the same rules as entrypoint.sh."""
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


_users = _read_users()
if _users:
    # Listing the accounts here registers them with the Hub at start-up, so the
    # admin panel shows every student before their first login.
    c.Authenticator.allowed_users = _users
else:
    c.Authenticator.allow_all = True             # fallback: any Linux account

c.Authenticator.admin_users = {
    u.strip().lower()
    for u in os.environ.get("JUPYTERHUB_ADMIN_USERS", "teacher").split(",")
    if u.strip()
}

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

# --- Idle culler: free RAM by stopping servers nobody has touched in a while ---
idle_timeout = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "7200"))
if idle_timeout > 0:
    c.JupyterHub.load_roles = [
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
    ]
    c.JupyterHub.services = [
        {
            "name": "jupyterhub-idle-culler-service",
            "command": [
                sys.executable,
                "-m", "jupyterhub_idle_culler",
                f"--timeout={idle_timeout}",
            ],
        }
    ]

c.JupyterHub.log_level = "INFO"
