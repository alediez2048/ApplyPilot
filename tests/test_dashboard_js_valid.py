"""Guard: the dashboard's inline <script> must be valid JavaScript.

A stray quote/escape in a Python-embedded JS string (e.g. "I\\'ll") throws a SyntaxError that
breaks the ENTIRE script — refresh() never runs and the whole jobs table silently blanks. This
extracts the served page's <script> and syntax-checks it with node, so that class of bug fails
in CI instead of on the user.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from applypilot import web_dashboard


def _page_html() -> str:
    # DASHBOARD_HTML is the served template; find whatever attribute holds the full page.
    for name in dir(web_dashboard):
        val = getattr(web_dashboard, name)
        if isinstance(val, str) and "<script>" in val and "function refresh" in val:
            return val
    pytest.skip("dashboard HTML template not found as a module string")


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_served_dashboard_js_is_valid(tmp_path):
    html = _page_html()
    m = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert m, "no <script> block in the dashboard HTML"
    js = tmp_path / "dash.js"
    js.write_text(m.group(1), encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert proc.returncode == 0, f"dashboard JS has a syntax error:\n{proc.stderr}"
