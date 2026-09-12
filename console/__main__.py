"""
Process entry point used by the Hub:  python -m console

Binds uvicorn to the host/port from JUPYTERHUB_SERVICE_URL, trusts proxy
headers from the Hub's proxy only, logs to stdout and to console.log. A fatal
start-up error is logged and followed by a 30 s pause before exiting, so the
Hub's immediate-restart policy cannot turn into a tight loop.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time


def _setup_logging(log_dir: str) -> None:
    fmt = logging.Formatter("[%(levelname)1.1s %(asctime)s %(name)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "console.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("console.log unavailable: %s", e)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # request lines carry no value here


def main() -> int:
    from .settings import Settings

    settings = Settings.from_env()
    _setup_logging(settings.log_dir)
    log = logging.getLogger("console")
    try:
        import uvicorn

        from .app import create_app

        host, port = settings.bind_host_port
        app = create_app(settings)
        log.info("starting teacher console on %s:%s prefix %s", host, port, settings.service_prefix)
        uvicorn.run(
            app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
            log_config=None, server_header=False, timeout_graceful_shutdown=5, access_log=False,
        )
        return 0
    except Exception:  # noqa: BLE001
        log.exception("teacher console failed to start; pausing 30 s before exit")
        time.sleep(30)
        return 1


if __name__ == "__main__":
    sys.exit(main())
