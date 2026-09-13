"""Exercise the actual Hub config callables, including escaping before template rendering."""
import json
import runpy
from pathlib import Path

from markupsafe import Markup
from traitlets.config import Config


def test_announcement_is_escaped_once_and_preserves_line_breaks(tmp_path):
    config = Path(__file__).resolve().parents[3] / "jupyterhub_config.py"
    if not config.exists():
        config = Path("/etc/jupyterhub/jupyterhub_config.py")
    namespace = runpy.run_path(str(config), init_globals={"get_config": Config})
    announcement = namespace["_announcement_login"]
    state = announcement.__globals__
    branding = tmp_path / "branding.json"
    branding.write_text(json.dumps({"announcement": "<Keep learning> & save\n<script>alert(1)</script>"}))
    state["BRANDING_JSON"] = str(branding)
    state["LOGO_FILE"] = str(tmp_path / "logo.png")
    state["_branding_cache"] = {"key": None, "value": None}
    result = announcement()
    assert isinstance(result, Markup)
    assert result == "&lt;Keep learning&gt; &amp; save<br>&lt;script&gt;alert(1)&lt;/script&gt;"
