"""
All paths, environment inputs and limits for the console in one place.

The Hub injects the JUPYTERHUB_* variables when it starts the service. Tests
override anything with CONSOLE_<FIELD> environment variables or by building a
Settings object directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


def _read_version() -> str:
    v = os.environ.get("CLASSROOM_IMAGE_VERSION", "").strip()
    if v:
        return v
    try:
        with open("/etc/classroom-version", encoding="utf-8") as fh:
            return fh.read().strip() or "dev"
    except OSError:
        return "dev"


@dataclass
class Settings:
    # --- where things live (volume-backed) ---
    state_dir: str = "/srv/jupyterhub"
    home_root: str = "/home"
    shared_dir: str = "/srv/shared"
    users_txt: str = "/etc/jupyterhub/users.txt"
    kit_dir: str = "/opt/classroom/skel"
    default_logo: str = "/opt/classroom/branding-defaults/default-logo.png"
    default_branding: str = "/opt/classroom/branding-defaults/branding.default.json"

    # --- how the Hub reaches us / we reach the Hub ---
    service_prefix: str = "/services/console/"
    service_url: str = "http://127.0.0.1:8099"
    hub_api_url: str = "http://127.0.0.1:8081/hub/api"
    hub_api_token: str = ""
    client_id: str = "service-console"
    oauth_callback: str = ""            # defaults to <prefix>oauth_callback
    hub_base_url: str = "/"
    admin_users: frozenset = field(default_factory=lambda: frozenset({"teacher"}))
    classroom_url: str = ""
    image_version: str = "dev"

    # --- limits and timings ---
    session_max_age: int = 12 * 3600
    state_max_age: int = 600
    user_cache_ttl: int = 60
    hub_timeout: float = 10.0
    stats_interval: float = 5.0
    max_upload_bytes: int = 2 * 1024**3
    max_unzipped_bytes: int = 4 * 1024**3
    max_zip_entries: int = 20000
    max_submission_zip_bytes: int = 2 * 1024**3
    max_logo_bytes: int = 2 * 1024**2
    log_tail_max_bytes: int = 4 * 1024**2

    # --- derived paths ---
    @property
    def prefix(self) -> str:
        """Service prefix without the trailing slash, e.g. /services/console."""
        return self.service_prefix.rstrip("/")

    @property
    def roster_path(self) -> str:
        return os.path.join(self.state_dir, "roster.json")

    @property
    def secret_path(self) -> str:
        return os.path.join(self.state_dir, "console_secret")

    @property
    def backup_dir(self) -> str:
        return os.path.join(self.state_dir, "backups")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.state_dir, "logs")

    @property
    def branding_dir(self) -> str:
        return os.path.join(self.state_dir, "branding")

    @property
    def branding_json(self) -> str:
        return os.path.join(self.branding_dir, "branding.json")

    @property
    def logo_file(self) -> str:
        return os.path.join(self.branding_dir, "logo.png")

    @property
    def removed_dir(self) -> str:
        return os.path.join(self.state_dir, "removed")

    @property
    def tmp_dir(self) -> str:
        return os.path.join(self.state_dir, "tmp")

    @property
    def hub_log_file(self) -> str:
        return os.path.join(self.log_dir, "jupyterhub.log")

    @property
    def console_log_file(self) -> str:
        return os.path.join(self.log_dir, "console.log")

    @property
    def oauth_callback_url(self) -> str:
        return self.oauth_callback or (self.service_prefix + "oauth_callback")

    @property
    def bind_host_port(self) -> tuple[str, int]:
        from urllib.parse import urlparse

        u = urlparse(self.service_url)
        return (u.hostname or "127.0.0.1", u.port or 8099)

    def url(self, path: str = "") -> str:
        """Absolute-path URL under the service prefix ("students" -> /services/console/students)."""
        return self.prefix + "/" + path.lstrip("/")

    # --- construction ---
    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env
        s = cls()
        s.service_prefix = env.get("JUPYTERHUB_SERVICE_PREFIX", s.service_prefix) or s.service_prefix
        s.service_url = env.get("JUPYTERHUB_SERVICE_URL", s.service_url) or s.service_url
        s.hub_api_url = env.get("JUPYTERHUB_API_URL", s.hub_api_url) or s.hub_api_url
        s.hub_api_token = env.get("JUPYTERHUB_API_TOKEN", "")
        s.client_id = env.get("JUPYTERHUB_CLIENT_ID", s.client_id) or s.client_id
        s.oauth_callback = env.get("JUPYTERHUB_OAUTH_CALLBACK_URL", "")
        s.hub_base_url = env.get("JUPYTERHUB_BASE_URL", "/") or "/"
        s.admin_users = frozenset(
            a.strip().lower() for a in env.get("JUPYTERHUB_ADMIN_USERS", "teacher").split(",") if a.strip()
        )
        s.classroom_url = env.get("CLASSROOM_URL", "").strip()
        s.image_version = _read_version()
        # CONSOLE_<FIELD> overrides (tests, local runs)
        for f in fields(cls):
            key = "CONSOLE_" + f.name.upper()
            if key in env:
                raw = env[key]
                current = getattr(s, f.name)
                if isinstance(current, bool):
                    setattr(s, f.name, raw.lower() in ("1", "true", "yes"))
                elif isinstance(current, int):
                    setattr(s, f.name, int(raw))
                elif isinstance(current, float):
                    setattr(s, f.name, float(raw))
                elif isinstance(current, frozenset):
                    setattr(s, f.name, frozenset(x.strip().lower() for x in raw.split(",") if x.strip()))
                else:
                    setattr(s, f.name, raw)
        return s
