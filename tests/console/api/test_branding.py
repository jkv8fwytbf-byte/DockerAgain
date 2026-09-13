"""Settings page API: branding text, logo upload/reset, address detection, permissions."""
from __future__ import annotations

import io
import json
import os
import stat

import pytest
from PIL import Image

from console.auth import CSRF_HEADER
from console.branding import MAX_LOGO_PX, clean_text, clean_url, validate_branding
from console.util import read_branding

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def make_image(fmt: str, size=(300, 120), mode="RGB", color=(200, 30, 30)) -> bytes:
    im = Image.new(mode, size, color)
    buf = io.BytesIO()
    im.save(buf, format=fmt)
    return buf.getvalue()


def upload(client, prefix, data: bytes, name="logo.png", ctype="image/png"):
    return client.post(f"{prefix}/api/settings/logo", files={"file": (name, data, ctype)})


def branding_file(settings) -> dict:
    with open(settings.branding_json, encoding="utf-8") as fh:
        return json.load(fh)


def logo_bytes(settings) -> bytes:
    with open(settings.logo_file, "rb") as fh:
        return fh.read()


VALID = {
    "school_name": "Lincoln High School",
    "class_name": "Grade 10 · Python & Radio Lab",
    "accent": "green",
    "announcement": "Homework 3 is due Friday.",
    "class_url": "http://192.168.1.10:8000",
}


# --- GET -------------------------------------------------------------------------------

def test_get_defaults_from_default_branding(as_teacher, prefix, settings):
    r = as_teacher.get(f"{prefix}/api/settings")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    s = r.json()
    assert s["school_name"] == "Test School"
    assert s["class_name"] == "" and s["announcement"] == "" and s["class_url"] == ""
    assert s["accent"] == "blue" and s["accent_hex"] == "#2c7bb6"
    # no class_url saved -> the address the browser is using
    assert s["class_url_effective"] == "http://classroom.local/"
    logo = s["logo"]
    assert logo["custom"] is False
    assert logo["url"].startswith(f"{prefix}/public/logo?v=") and isinstance(logo["version"], int)
    assert logo["version"] == int(os.stat(settings.logo_file).st_mtime)
    about = s["about"]
    assert about["image_version"] == settings.image_version
    assert about["jupyterhub"] == "5.3.0"                      # from the FakeHub /info
    assert about["python"].count(".") == 2
    assert isinstance(about["addresses"], list) and all("127." not in a for a in about["addresses"])
    assert about["started"].endswith("Z")
    assert about["accents"]["orange"] == "#f37524" and len(about["accents"]) == 6
    assert s["limits"]["school_name"] == 80 and s["limits"]["max_logo_bytes"] == settings.max_logo_bytes


def test_get_when_hub_is_down_still_answers(as_teacher, prefix, hub):
    as_teacher.get(f"{prefix}/api/whoami")          # caches the admin check
    hub.down = True
    r = as_teacher.get(f"{prefix}/api/settings")
    assert r.status_code == 200 and r.json()["about"]["jupyterhub"] is None
    hub.down = False
    assert as_teacher.get(f"{prefix}/api/settings").json()["about"]["jupyterhub"] == "5.3.0"
    hub.down = True                                 # remembered once seen
    assert as_teacher.get(f"{prefix}/api/settings").json()["about"]["jupyterhub"] == "5.3.0"


# --- PUT -------------------------------------------------------------------------------

def test_put_valid_writes_file_0600_and_round_trips(as_teacher, prefix, settings):
    r = as_teacher.put(f"{prefix}/api/settings", json=VALID)
    assert r.status_code == 200, r.text
    s = r.json()
    for k, v in VALID.items():
        assert s[k] == v
    assert s["accent_hex"] == "#1a8f4e" and s["class_url_effective"] == VALID["class_url"]
    assert s["updated_at"].endswith("Z")
    assert stat.S_IMODE(os.stat(settings.branding_json).st_mode) == 0o600
    data = branding_file(settings)
    assert data["school_name"] == VALID["school_name"] and data["logo_custom"] is False and data["updated_at"] == s["updated_at"]
    assert set(data) == {"school_name", "class_name", "accent", "announcement", "class_url", "logo_custom", "updated_at"}
    # what the Hub's reader sees
    assert read_branding(settings.branding_json)["accent_hex"] == "#1a8f4e"
    assert as_teacher.get(f"{prefix}/api/settings").json()["announcement"] == VALID["announcement"]
    assert not os.path.exists(settings.branding_json + ".tmp")


def test_put_normalises_whitespace_and_keeps_newlines_in_announcement(as_teacher, prefix):
    body = dict(VALID, school_name="  Lincoln \t High  ", announcement="Line one\r\nLine two\r\n\r\n\r\nLine three  ", class_url=" http://x.local:8000 ")
    s = as_teacher.put(f"{prefix}/api/settings", json=body).json()
    assert s["school_name"] == "Lincoln High"
    assert s["announcement"] == "Line one\nLine two\n\nLine three"
    assert s["class_url"] == "http://x.local:8000"


def test_put_partial_keeps_other_fields(as_teacher, prefix):
    as_teacher.put(f"{prefix}/api/settings", json=VALID)
    s = as_teacher.put(f"{prefix}/api/settings", json={"announcement": ""}).json()
    assert s["announcement"] == "" and s["school_name"] == VALID["school_name"] and s["accent"] == "green"


@pytest.mark.parametrize("field,value", [
    ("school_name", "Lincoln\x00High"),
    ("school_name", "Lincoln\x1bHigh"),
    ("class_name", "Grade\x7f10"),
    ("announcement", "Due\x08Friday"),
    ("announcement", "Para\u2028graph"),
])
def test_put_rejects_control_characters(as_teacher, prefix, settings, field, value):
    before = branding_file(settings)
    r = as_teacher.put(f"{prefix}/api/settings", json=dict(VALID, **{field: value}))
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "validation_failed" and field in body["detail"]["fields"]
    assert "control" in body["detail"]["fields"][field]
    assert branding_file(settings) == before


@pytest.mark.parametrize("field,value,limit", [
    ("school_name", "x" * 81, 80),
    ("class_name", "x" * 121, 120),
    ("announcement", "x" * 301, 300),
    ("class_url", "http://h.local/" + "a" * 200, 200),
])
def test_put_rejects_over_long_values(as_teacher, prefix, field, value, limit):
    r = as_teacher.put(f"{prefix}/api/settings", json=dict(VALID, **{field: value}))
    assert r.status_code == 400
    assert str(limit) in r.json()["detail"]["fields"][field]


@pytest.mark.parametrize("accent", ["red", "", "#f37524", "ORANGE!"])
def test_put_rejects_unknown_accent(as_teacher, prefix, accent):
    r = as_teacher.put(f"{prefix}/api/settings", json=dict(VALID, accent=accent))
    assert r.status_code == 400 and "accent" in r.json()["detail"]["fields"]


def test_put_accepts_accent_case_insensitively(as_teacher, prefix):
    assert as_teacher.put(f"{prefix}/api/settings", json=dict(VALID, accent="Purple")).json()["accent"] == "purple"


@pytest.mark.parametrize("url", [
    "192.168.1.10:8000",
    "ftp://school.local",
    "javascript:alert(1)",
    "http://",
    "http://user:pw@school.local",
    "http://school local:8000",
    "http://school.local:99999",
    "http://sch<ool>.local",
    "http://x.local\nhttp://y.local",
])
def test_put_rejects_bad_urls(as_teacher, prefix, url):
    r = as_teacher.put(f"{prefix}/api/settings", json=dict(VALID, class_url=url))
    assert r.status_code == 400, url
    assert "class_url" in r.json()["detail"]["fields"]


@pytest.mark.parametrize("url", ["", "http://192.168.1.10:8000", "https://hub.school.org", "HTTP://lab.local:8000/", "http://[fd00::1]:8000/"])
def test_put_accepts_good_urls(as_teacher, prefix, url):
    r = as_teacher.put(f"{prefix}/api/settings", json=dict(VALID, class_url=url))
    assert r.status_code == 200, r.text
    assert r.json()["class_url"] == url


def test_put_reports_every_bad_field_at_once(as_teacher, prefix):
    r = as_teacher.put(f"{prefix}/api/settings", json=dict(VALID, accent="pink", class_url="nope", school_name="x" * 90))
    assert r.status_code == 400
    body = r.json()
    assert set(body["detail"]["fields"]) == {"accent", "class_url", "school_name"}
    assert "School name" in body["message"] and "2 more" in body["message"]


def test_put_non_string_value_is_400(as_teacher, prefix):
    r = as_teacher.put(f"{prefix}/api/settings", json=dict(VALID, school_name=42))
    assert r.status_code == 400 and r.json()["error"] == "validation_failed"


def test_put_strips_invisible_formatting_characters(as_teacher, prefix):
    s = as_teacher.put(f"{prefix}/api/settings", json=dict(VALID, school_name="Lin\u200bcoln \u202eHigh\ufeff")).json()
    assert s["school_name"] == "Lincoln High"


# --- logo --------------------------------------------------------------------------------

def test_upload_png_replaces_logo_and_marks_custom(as_teacher, prefix, settings):
    before_version = int(os.stat(settings.logo_file).st_mtime)
    data = make_image("PNG", mode="RGBA", color=(0, 0, 0, 0))
    r = upload(as_teacher, prefix, data)
    assert r.status_code == 200, r.text
    logo = r.json()["logo"]
    assert logo["custom"] is True and logo["version"] > before_version
    assert logo["url"] == f"{prefix}/public/logo?v={logo['version']}"
    stored = logo_bytes(settings)
    assert stored.startswith(PNG_MAGIC)
    with Image.open(io.BytesIO(stored)) as im:
        assert im.format == "PNG" and im.mode == "RGBA" and im.size == (300, 120)
    assert stat.S_IMODE(os.stat(settings.logo_file).st_mode) == 0o644
    assert branding_file(settings)["logo_custom"] is True
    assert not any(n.startswith("logo.png.tmp") for n in os.listdir(settings.branding_dir))
    # the public route serves the new file
    pub = as_teacher.get(f"{prefix}/public/logo")
    assert pub.status_code == 200 and pub.content == stored and pub.headers["content-type"].startswith("image/png")
    # a later text save keeps logo_custom
    as_teacher.put(f"{prefix}/api/settings", json=VALID)
    assert branding_file(settings)["logo_custom"] is True
    assert as_teacher.get(f"{prefix}/api/settings").json()["logo"]["custom"] is True


def test_upload_jpeg_is_converted_to_png(as_teacher, prefix, settings):
    r = upload(as_teacher, prefix, make_image("JPEG", size=(640, 200)), name="photo.jpg", ctype="image/jpeg")
    assert r.status_code == 200, r.text
    stored = logo_bytes(settings)
    assert stored.startswith(PNG_MAGIC)
    with Image.open(io.BytesIO(stored)) as im:
        assert im.format == "PNG" and im.mode == "RGBA" and im.size == (640, 200)
    assert r.json()["logo"]["custom"] is True


def test_upload_version_changes_even_twice_in_one_second(as_teacher, prefix):
    v1 = upload(as_teacher, prefix, make_image("PNG")).json()["logo"]["version"]
    v2 = upload(as_teacher, prefix, make_image("PNG", color=(0, 0, 255))).json()["logo"]["version"]
    assert v2 > v1


def test_upload_oversized_file_is_refused_and_logo_unchanged(as_teacher, prefix, settings):
    settings.max_logo_bytes = 4096
    before = logo_bytes(settings)
    big = make_image("PNG", size=(1200, 1200), color=(1, 2, 3))
    # make sure it really is bigger than the cap and not compressible below it
    big = big + b"\0" * 8192
    assert len(big) > settings.max_logo_bytes
    r = upload(as_teacher, prefix, big)
    assert r.status_code == 413 and r.json()["error"] == "logo_too_large"
    assert logo_bytes(settings) == before and branding_file(settings).get("logo_custom", False) is False


def test_upload_svg_is_refused_with_explanation(as_teacher, prefix, settings):
    before = logo_bytes(settings)
    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'
    r = upload(as_teacher, prefix, svg, name="logo.svg", ctype="image/svg+xml")
    assert r.status_code == 400 and r.json()["error"] == "unsupported_image"
    assert "SVG" in r.json()["message"] and "PNG or JPEG" in r.json()["message"]
    assert logo_bytes(settings) == before
    # a bare <svg> with a .png name is still recognised
    r = upload(as_teacher, prefix, b"  <svg xmlns='http://www.w3.org/2000/svg'></svg>", name="sneaky.png")
    assert r.status_code == 400 and "SVG" in r.json()["message"]


@pytest.mark.parametrize("name,data", [
    ("garbage.png", b"this is not an image at all" * 10),
    ("empty.png", b""),
    ("truncated.png", make_image("PNG")[:60]),
    ("png-magic-only.png", PNG_MAGIC + b"\0" * 100),
    ("gif.gif", b"GIF89a" + b"\0" * 50),
])
def test_upload_garbage_is_refused_and_logo_unchanged(as_teacher, prefix, settings, name, data):
    before = logo_bytes(settings)
    r = upload(as_teacher, prefix, data, name=name)
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "unsupported_image"
    assert logo_bytes(settings) == before
    assert branding_file(settings).get("logo_custom", False) is False


def test_upload_too_many_pixels_is_refused(as_teacher, prefix, settings):
    before = logo_bytes(settings)
    r = upload(as_teacher, prefix, make_image("PNG", size=(MAX_LOGO_PX + 1, 10)))
    assert r.status_code == 400 and r.json()["error"] == "unsupported_image"
    assert str(MAX_LOGO_PX) in r.json()["message"]
    assert logo_bytes(settings) == before


def test_upload_without_file_field_is_400(as_teacher, prefix):
    r = as_teacher.post(f"{prefix}/api/settings/logo", files={"other": ("a.png", make_image("PNG"), "image/png")})
    assert r.status_code == 400 and r.json()["error"] == "validation_failed"


def test_delete_logo_restores_default(as_teacher, prefix, settings):
    with open(settings.default_logo, "rb") as fh:
        default = fh.read()
    upload(as_teacher, prefix, make_image("PNG"))
    assert logo_bytes(settings) != default
    r = as_teacher.delete(f"{prefix}/api/settings/logo")
    assert r.status_code == 200, r.text
    assert r.json()["logo"]["custom"] is False
    assert logo_bytes(settings) == default
    assert branding_file(settings)["logo_custom"] is False
    assert stat.S_IMODE(os.stat(settings.logo_file).st_mode) == 0o644
    # idempotent
    assert as_teacher.delete(f"{prefix}/api/settings/logo").status_code == 200


def test_delete_logo_when_default_is_missing_is_a_clear_error(as_teacher, prefix, settings):
    os.unlink(settings.default_logo)
    r = as_teacher.delete(f"{prefix}/api/settings/logo")
    assert r.status_code == 500 and r.json()["error"] == "default_logo_missing"


def test_logo_symlink_is_replaced_not_followed(as_teacher, prefix, settings, tmp_path):
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"do not touch")
    os.unlink(settings.logo_file)
    os.symlink(str(victim), settings.logo_file)
    r = upload(as_teacher, prefix, make_image("PNG"))
    assert r.status_code == 200
    assert victim.read_bytes() == b"do not touch"
    assert not os.path.islink(settings.logo_file) and logo_bytes(settings).startswith(PNG_MAGIC)


# --- detect-url ---------------------------------------------------------------------------

def test_detect_url_uses_forwarded_headers(as_teacher, prefix):
    r = as_teacher.get(f"{prefix}/api/settings/detect-url", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "hub.school.org:8443"})
    assert r.status_code == 200 and r.json() == {"url": "https://hub.school.org:8443/"}
    r = as_teacher.get(f"{prefix}/api/settings/detect-url", headers={"X-Forwarded-Proto": "https, http", "X-Forwarded-Host": "a.local, b.local"})
    assert r.json() == {"url": "https://a.local/"}


def test_detect_url_falls_back_to_host_header(as_teacher, prefix):
    r = as_teacher.get(f"{prefix}/api/settings/detect-url")
    assert r.json() == {"url": "http://classroom.local/"}
    r = as_teacher.get(f"{prefix}/api/settings/detect-url", headers={"Host": "192.168.1.10:8000"})
    assert r.json() == {"url": "http://192.168.1.10:8000/"}


def test_detect_url_refuses_odd_hosts(as_teacher, prefix):
    r = as_teacher.get(f"{prefix}/api/settings/detect-url", headers={"X-Forwarded-Host": "evil.example/<script>"})
    assert r.status_code == 400 and r.json()["error"] == "no_address"
    r = as_teacher.get(f"{prefix}/api/settings/detect-url", headers={"X-Forwarded-Proto": "gopher"})
    assert r.status_code == 400


# --- permissions ----------------------------------------------------------------------------

def test_anonymous_is_401(client, prefix):
    assert client.get(f"{prefix}/api/settings").status_code == 401
    assert client.get(f"{prefix}/api/settings/detect-url").status_code == 401
    assert client.put(f"{prefix}/api/settings", json=VALID, headers={CSRF_HEADER: "1"}).status_code == 401
    assert client.post(f"{prefix}/api/settings/logo", files={"file": ("a.png", make_image("PNG"), "image/png")}, headers={CSRF_HEADER: "1"}).status_code == 401
    assert client.delete(f"{prefix}/api/settings/logo", headers={CSRF_HEADER: "1"}).status_code == 401


def test_student_is_403_everywhere(as_student, prefix, settings):
    before = branding_file(settings)
    assert as_student.get(f"{prefix}/api/settings").status_code == 403
    assert as_student.get(f"{prefix}/api/settings/detect-url").status_code == 403
    assert as_student.put(f"{prefix}/api/settings", json=VALID).status_code == 403
    assert upload(as_student, prefix, make_image("PNG")).status_code == 403
    assert as_student.delete(f"{prefix}/api/settings/logo").status_code == 403
    assert branding_file(settings) == before


def test_mutations_without_console_header_are_blocked(as_teacher, prefix, settings):
    del as_teacher.headers[CSRF_HEADER]
    before = branding_file(settings)
    r = as_teacher.put(f"{prefix}/api/settings", json=VALID)
    assert r.status_code == 403 and r.json()["error"] == "csrf_failed"
    assert upload(as_teacher, prefix, make_image("PNG")).status_code == 403
    assert as_teacher.delete(f"{prefix}/api/settings/logo").status_code == 403
    assert branding_file(settings) == before


def test_cross_site_origin_is_blocked(as_teacher, prefix):
    r = as_teacher.put(f"{prefix}/api/settings", json=VALID, headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


# --- page -----------------------------------------------------------------------------------

def test_settings_page_renders_current_values(as_teacher, prefix):
    as_teacher.put(f"{prefix}/api/settings", json=dict(VALID, school_name="Lincoln <b>High</b>", announcement="Due & soon"))
    r = as_teacher.get(f"{prefix}/settings")
    assert r.status_code == 200
    html = r.text
    assert "Lincoln &lt;b&gt;High&lt;/b&gt;" in html and "<b>High</b>" not in html
    assert "Due &amp; soon" in html
    assert 'name="accent" value="green" checked' in html
    assert "settings.js" in html and "settings.css" in html
    assert "You have unsaved changes" in html and "Copy diagnostics" in html


# --- validators (direct) -------------------------------------------------------------------------

def test_clean_text_and_url_helpers():
    assert clean_text(" a \t b ") == "a b"
    assert clean_text("a\r\nb\n\n\n\nc", multiline=True) == "a\nb\n\nc"
    assert clean_text("a\nb") == "a b"
    with pytest.raises(ValueError):
        clean_text("a\x00b")
    assert clean_url("") == ""
    assert clean_url("http://lab.local:8000") == "http://lab.local:8000"
    with pytest.raises(ValueError):
        clean_url("lab.local:8000")
    clean, errors = validate_branding({"school_name": "S", "class_name": "", "announcement": "", "class_url": "", "accent": "teal"})
    assert not errors and clean["accent"] == "teal" and clean["class_url"] == ""


# --- contract with the template and the Hub reader ---------------------------------------------

def test_settings_page_swatches_match_the_shared_accent_list(as_teacher, prefix):
    from console.util import ACCENTS

    html = as_teacher.get(f"{prefix}/settings").text
    for name, hex_ in ACCENTS.items():
        assert f'name="accent" value="{name}"' in html
        assert f"background: {hex_}" in html
    assert html.count('name="accent"') == len(ACCENTS)
    assert 'role="radiogroup"' in html
    assert "data-logo-limit" in html


def test_put_cannot_flip_logo_custom_or_updated_at(as_teacher, prefix, settings):
    body = dict(VALID, logo_custom=True, updated_at="2000-01-01T00:00:00Z", logo={"custom": True})
    r = as_teacher.put(f"{prefix}/api/settings", json=body)
    assert r.status_code == 200, r.text
    data = branding_file(settings)
    assert data["logo_custom"] is False and data["updated_at"] != "2000-01-01T00:00:00Z"
    assert r.json()["logo"]["custom"] is False


def test_about_started_is_a_past_utc_timestamp(as_teacher, prefix):
    from datetime import datetime, timezone

    started = as_teacher.get(f"{prefix}/api/settings").json()["about"]["started"]
    when = datetime.fromisoformat(started.replace("Z", "+00:00"))
    assert when.tzinfo is not None and when <= datetime.now(timezone.utc)


def test_every_settings_response_is_no_store(as_teacher, prefix):
    assert as_teacher.put(f"{prefix}/api/settings", json=VALID).headers["cache-control"] == "no-store"
    assert upload(as_teacher, prefix, make_image("PNG")).headers["cache-control"] == "no-store"
    assert as_teacher.delete(f"{prefix}/api/settings/logo").headers["cache-control"] == "no-store"
    assert as_teacher.get(f"{prefix}/api/settings/detect-url").headers["cache-control"] == "no-store"


def test_hub_reader_sees_what_the_console_wrote(as_teacher, prefix, settings):
    """jupyterhub_config._branding() reads the same file; the console's reader must agree with it."""
    as_teacher.put(f"{prefix}/api/settings", json=dict(VALID, announcement="Line 1\nLine 2"))
    upload(as_teacher, prefix, make_image("PNG"))
    seen = read_branding(settings.branding_json)
    assert seen["school_name"] == VALID["school_name"] and seen["class_name"] == VALID["class_name"]
    assert seen["accent"] == "green" and seen["accent_hex"] == "#1a8f4e"
    assert seen["announcement"] == "Line 1\nLine 2" and seen["class_url"] == VALID["class_url"]
    assert seen["logo_custom"] is True


# --- streamed upload: caps and body shape -------------------------------------------------------

def multipart_body(parts, boundary="cc-test-boundary") -> bytes:
    """parts: list of (name, filename|None, content_type|None, data)."""
    out = bytearray()
    for name, filename, ctype, data in parts:
        out += f"--{boundary}\r\n".encode()
        disp = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disp += f'; filename="{filename}"'
        out += (disp + "\r\n").encode()
        if ctype:
            out += f"Content-Type: {ctype}\r\n".encode()
        out += b"\r\n" + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


def test_upload_is_capped_while_streaming_without_content_length(as_teacher, prefix, settings):
    settings.max_logo_bytes = 4096
    before = logo_bytes(settings)
    body = multipart_body([("file", "big.png", "image/png", PNG_MAGIC + b"\0" * 20000)])

    def chunks():
        for i in range(0, len(body), 1024):
            yield body[i:i + 1024]

    r = as_teacher.post(
        f"{prefix}/api/settings/logo", content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=cc-test-boundary", "Transfer-Encoding": "chunked"},
    )
    assert r.status_code == 413 and r.json()["error"] == "logo_too_large"
    assert logo_bytes(settings) == before


def test_upload_declared_too_big_is_refused_before_reading(as_teacher, prefix, settings):
    before = logo_bytes(settings)
    r = as_teacher.post(
        f"{prefix}/api/settings/logo", content=b"",
        headers={"Content-Type": "multipart/form-data; boundary=x", "Content-Length": str(settings.max_logo_bytes * 3)},
    )
    assert r.status_code == 413 and r.json()["error"] == "logo_too_large"
    assert logo_bytes(settings) == before


def test_upload_keeps_only_the_file_part(as_teacher, prefix, settings):
    """Extra fields and a second file are ignored; a huge extra field is not read into memory as the logo."""
    png = make_image("PNG", size=(40, 20), color=(9, 9, 9))
    other = make_image("PNG", size=(8, 8), color=(250, 250, 250))
    body = multipart_body([
        ("note", None, None, b"x" * 3000),
        ("file", "logo.png", "image/png", png),
        ("file", "second.png", "image/png", other),
        ("trailing", None, None, b"y"),
    ])
    r = as_teacher.post(f"{prefix}/api/settings/logo", content=body, headers={"Content-Type": "multipart/form-data; boundary=cc-test-boundary"})
    assert r.status_code == 200, r.text
    with Image.open(io.BytesIO(logo_bytes(settings))) as im:
        assert im.size == (40, 20)


def test_upload_malformed_multipart_is_400(as_teacher, prefix, settings):
    before = logo_bytes(settings)
    r = as_teacher.post(f"{prefix}/api/settings/logo", content=b"--nope\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.png\"\r\n\r\n" + PNG_MAGIC,
                        headers={"Content-Type": "multipart/form-data; boundary=nope"})
    assert r.status_code == 400 and r.json()["error"] in ("bad_upload", "unsupported_image")
    assert logo_bytes(settings) == before


def test_upload_not_multipart_is_400(as_teacher, prefix, settings):
    before = logo_bytes(settings)
    r = as_teacher.post(f"{prefix}/api/settings/logo", content=make_image("PNG"), headers={"Content-Type": "image/png"})
    assert r.status_code == 400 and r.json()["error"] == "validation_failed" and "file" in r.json()["detail"]["fields"]
    r = as_teacher.post(f"{prefix}/api/settings/logo", json={"file": "x"})
    assert r.status_code == 400 and r.json()["error"] == "validation_failed"
    assert logo_bytes(settings) == before


def test_upload_from_a_student_never_touches_the_logo(as_student, prefix, settings):
    before = logo_bytes(settings)
    r = upload(as_student, prefix, make_image("PNG"))
    assert r.status_code == 403
    assert logo_bytes(settings) == before and branding_file(settings).get("logo_custom", False) is False


def test_accents_are_shared_with_templates_at_startup(as_teacher, app):
    from console.util import ACCENTS

    assert app.state.templates.env.globals["accents"] == ACCENTS
