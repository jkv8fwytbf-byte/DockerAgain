"""
Load /etc/jupyterhub/jupyterhub_config.py the way JupyterHub would and check
the parts that would otherwise only fail at container start-up.

Runs at image build time (Dockerfile) and via `make check-config` inside the
running container. Exit code 1 on any failure.

    python tests/check_config.py [path/to/jupyterhub_config.py]
"""
import os
import runpy
import sys
import traceback

CONFIG = sys.argv[1] if len(sys.argv) > 1 else "/etc/jupyterhub/jupyterhub_config.py"
failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"  FAIL  {msg}")
    else:
        print(f"  ok    {msg}")


def load(env_overrides):
    from traitlets.config import Config

    cfg = Config()
    saved = dict(os.environ)
    os.environ.update(env_overrides)
    try:
        ns = runpy.run_path(CONFIG, init_globals={"get_config": lambda: cfg})
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return cfg, ns


def service_keys_ok(services):
    from jupyterhub.services.service import Service

    allowed = set(Service().traits(input=True))
    bad = []
    for svc in services:
        for k in svc:
            if k not in allowed:
                bad.append(f"{svc.get('name')}: {k}")
    return bad


def scopes_ok(roles):
    from jupyterhub.scopes import scope_definitions

    bad = []
    for role in roles:
        for s in role.get("scopes", []):
            if s.split("!", 1)[0] not in scope_definitions:
                bad.append(f"{role['name']}: {s}")
    return bad


def main():
    print(f"checking {CONFIG}")
    for label, env in (
        ("console enabled", {"CONSOLE_ENABLED": "1", "IDLE_TIMEOUT_SECONDS": "7200"}),
        ("console disabled, culler off", {"CONSOLE_ENABLED": "0", "IDLE_TIMEOUT_SECONDS": "0"}),
    ):
        print(f"--- {label}")
        try:
            cfg, ns = load(env)
        except Exception:
            traceback.print_exc()
            failures.append(f"{label}: config raised")
            continue
        services = list(cfg.JupyterHub.get("services", []))
        roles = list(cfg.JupyterHub.get("load_roles", []))
        names = {s["name"] for s in services}
        check(all(isinstance(s, dict) and s.get("name") for s in services), "services are named dicts")
        check(not service_keys_ok(services), f"service keys valid ({service_keys_ok(services) or 'all good'})")
        check(
            all(svc in names for r in roles for svc in r.get("services", [])),
            "every role refers to a defined service",
        )
        check(not scopes_ok(roles), f"role scopes exist ({scopes_ok(roles) or 'all good'})")
        if env["CONSOLE_ENABLED"] == "1":
            console = next((s for s in services if s["name"] == "console"), None)
            check(console is not None, "console service declared")
            if console:
                check(console["url"].startswith("http://127.0.0.1:"), "console binds to loopback")
                check(console.get("oauth_no_confirm") is True, "console skips the OAuth confirm page")
                check("PATH" in console.get("environment", {}), "console environment sets PATH")
        else:
            check("console" not in names, "console absent when disabled")
            check(not roles and not services, "no services/roles when console and culler are off")

        check(not cfg.JupyterHub.get("extra_log_handlers"), "no handler objects in config (traitlets deep-copies config)")
        lc = cfg.JupyterHub.get("logging_config", {})
        check("file" in lc.get("handlers", {}), "logging_config defines the file handler")
        check("file" in lc.get("loggers", {}).get("JupyterHub", {}).get("handlers", []), "JupyterHub logger writes to the file handler")
        try:
            import copy, pickle
            copy.deepcopy(dict(cfg.JupyterHub))
            check(True, "JupyterHub config section is deep-copyable")
        except Exception as e:  # noqa: BLE001
            check(False, f"JupyterHub config section is deep-copyable ({e})")
        check(
            bool(cfg.Authenticator.get("allowed_users")) or cfg.Authenticator.get("allow_all") is True,
            "allowed_users populated or allow_all set",
        )
        tv = cfg.JupyterHub.get("template_vars", {})
        check(callable(tv.get("branding")), "template_vars.branding is callable")
        if callable(tv.get("branding")):
            try:
                b = tv["branding"](None)
                check(isinstance(b, dict) and "school_name" in b and "accent_hex" in b, "branding(None) returns a dict")
                check(str(b["accent_hex"]).startswith("#"), "accent_hex is a colour")
            except Exception as e:  # noqa: BLE001
                failures.append(f"branding callable raised: {e}")
                print(f"  FAIL  branding callable raised: {e}")
        if callable(tv.get("announcement_login")):
            try:
                a = tv["announcement_login"](None)
                check(isinstance(a, str), "announcement_login(None) returns a string")
            except Exception as e:  # noqa: BLE001
                failures.append(f"announcement callable raised: {e}")

        tdirs = cfg.JupyterHub.get("template_paths", [])
        check(bool(tdirs) and os.path.isdir(tdirs[0]), f"template dir exists: {tdirs}")
        if tdirs and os.path.isdir(tdirs[0]):
            for name in ("page.html", "login.html"):
                check(os.path.isfile(os.path.join(tdirs[0], name)), f"custom template present: {name}")
            try:
                from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader
                from jupyterhub._data import DATA_FILES_PATH

                base = os.path.join(DATA_FILES_PATH, "templates")
                loader = ChoiceLoader(
                    [
                        PrefixLoader({"templates": FileSystemLoader([base])}, "/"),
                        FileSystemLoader(list(tdirs) + [base]),
                    ]
                )
                env = Environment(loader=loader, autoescape=True)
                for name in sorted(os.listdir(tdirs[0])):
                    if name.endswith(".html"):
                        env.get_template(name)  # compiles: catches syntax errors
                        print(f"  ok    template compiles: {name}")
            except Exception as e:  # noqa: BLE001
                failures.append(f"template compile failed: {e}")
                print(f"  FAIL  template compile failed: {e}")

    print()
    if failures:
        print(f"{len(failures)} problem(s) found in {CONFIG}")
        sys.exit(1)
    print("config OK")


if __name__ == "__main__":
    main()
