"""
Data for the printable login cards: the class address, one sign-in link per
student and its QR code (segno, inline SVG).

The QR carries only "<class_url>/hub/login?username=<u>" - never the password.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

log = logging.getLogger("console.cards")

CARDS_PER_SHEET = 8          # 2 x 4 on A4 and Letter
QR_ERROR_LEVEL = "m"
QR_BORDER_MODULES = 2        # quiet zone inside the 30 mm box; the card adds more white space


def class_url_for(request, configured: str = "") -> str:
    """The address students type: the configured one, else what the browser used to reach us."""
    configured = (configured or "").strip().rstrip("/")
    if configured:
        return configured
    headers = request.headers
    proto = (headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = "http"
    host = (headers.get("x-forwarded-host") or headers.get("host") or request.url.netloc or "").split(",")[0].strip()
    host = "".join(ch for ch in host if ch.isalnum() or ch in ".-:[]_")
    return f"{proto}://{host}" if host else "http://localhost:8000"


def login_url(class_url: str, username: str, hub_base_url: str = "/") -> str:
    base = "/" + (hub_base_url or "/").strip("/")
    base = "" if base == "/" else base
    return f"{class_url.rstrip('/')}{base}/hub/login?username={quote(username, safe='')}"


def qr_svg(data: str, label: str = "") -> str:
    """Inline SVG (viewBox only, sized by CSS). Empty string if segno is unavailable."""
    try:
        import segno
    except ImportError:  # pragma: no cover - segno is in requirements-console.txt
        log.warning("segno is not installed; login cards have no QR code")
        return ""
    qr = segno.make(data, error=QR_ERROR_LEVEL)
    svg = qr.svg_inline(
        scale=1, border=QR_BORDER_MODULES, dark="#000", light=None, omitsize=True,
        svgclass="cc-qr", lineclass=None, title=label or None,
    )
    return svg.replace("<svg ", '<svg role="img" ', 1) if label else svg.replace("<svg ", '<svg aria-hidden="true" ', 1)


def build_cards(students, class_url: str, hub_base_url: str = "/") -> list[dict]:
    """One dict per student, sorted by display name (username as fallback)."""
    cards = []
    for s in students:
        url = login_url(class_url, s.username, hub_base_url)
        cards.append({
            "username": s.username,
            "display_name": s.display_name or s.username,
            "password": s.password,
            "login_url": url,
            "qr_svg": qr_svg(url, f"QR code with the sign-in link for {s.username}"),
        })
    cards.sort(key=lambda c: (c["display_name"].lower(), c["username"]))
    return cards


def chunk(items: list, size: int = CARDS_PER_SHEET) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]
