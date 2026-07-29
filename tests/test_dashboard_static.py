"""ARCH-2: the page is three files on disk, served by the dashboard.

What can break once the template leaves the Python string:
  - the files don't ship with the package (works from the repo, 404s from a wheel)
  - index.html points at an asset name the server doesn't serve
  - the cache-buster token survives into the served HTML, so `?v=__ASSET_V__` never
    changes and the browser holds a stale bundle forever
  - the asset route becomes a file-read primitive on a machine-local server
"""

from __future__ import annotations

import re

import pytest

from applypilot import web_dashboard as w


def test_the_three_files_exist_inside_the_package():
    """They live under src/applypilot/, not next to it — that is what makes them ship."""
    for name in ("index.html", "dashboard.css", "dashboard.js"):
        assert (w._STATIC_DIR / name).is_file(), f"missing static asset: {name}"
    assert w._STATIC_DIR.parent.name == "applypilot"


def test_no_javascript_or_css_left_in_python():
    """The whole point of ARCH-2. A single leaked <script> block re-opens the debt."""
    src = (w._STATIC_DIR.parent / "web_dashboard.py").read_text(encoding="utf-8")
    # Closing tags, not opening ones — prose in a docstring may well name `<script>`,
    # but nothing writes `</script>` except an embedded template.
    for marker in ("</script>", "</style>", "<!doctype"):
        assert marker not in src.lower(), f"{marker} is still embedded in web_dashboard.py"


def test_index_references_only_assets_the_server_will_serve():
    html = (w._STATIC_DIR / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r'(?:href|src)="(/static/[^"?]+)', html))
    assert referenced, "index.html references no static assets at all"
    assert referenced <= set(w._STATIC_ASSETS), f"unservable: {referenced - set(w._STATIC_ASSETS)}"


def test_served_html_has_a_resolved_cache_buster():
    html = w._index_html()
    assert "__ASSET_V__" not in html, "cache-buster token was not substituted"
    assert f"?v={w._asset_version()}" in html


def test_cache_buster_changes_when_an_asset_changes(monkeypatch):
    """Version alone doesn't move during development; mtime does."""
    before = w._asset_version()
    js = w._STATIC_DIR / "dashboard.js"
    original = js.stat().st_mtime
    try:
        import os
        os.utime(js, (original + 60, original + 60))
        assert w._asset_version() != before
    finally:
        os.utime(js, (original, original))


def test_no_dead_functions():
    """ESLint can't do this one, and that is exactly why the debt accumulated.

    The script is a classic script on purpose — ~56 inline `onclick=` attributes call
    these functions by name, so to ESLint every top-level declaration looks like a used
    global. Reading the HTML too tells "called from an attribute" apart from "called from
    nowhere". Extracting the template found five dead functions and three dead Sets left
    over when the accordions became tabs; nothing had reported them in months.
    """
    js = (w._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    everywhere = js + "\n" + (w._STATIC_DIR / "index.html").read_text(encoding="utf-8")
    declared = re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", js, re.M)
    assert len(declared) > 50, "the declaration regex stopped matching — fix the test, not the code"
    dead = [n for n in declared if len(re.findall(rf"\b{re.escape(n)}\b", everywhere)) <= 1]
    assert not dead, "declared but never referenced: " + ", ".join(dead)


@pytest.mark.parametrize("path", [
    "/static/../web_dashboard.py",
    "/static/../../../../etc/passwd",
    "/static/",
    "/static/index.html",          # deliberately not servable: it goes through _index_html()
    "/api/status",
])
def test_only_the_allowlisted_assets_resolve(path):
    assert path not in w._STATIC_ASSETS
