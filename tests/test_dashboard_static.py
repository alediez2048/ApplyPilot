"""ARCH-2: the page is three files on disk, served by the dashboard.

What can break once the template leaves the Python string:
  - the files don't ship with the package (works from the repo, 404s from a wheel)
  - index.html points at an asset name the server doesn't serve
  - the cache-buster token survives into the served HTML, so `?v=__ASSET_V__` never
    changes and the browser holds a stale bundle forever
  - the asset route becomes a file-read primitive on a machine-local server
  - the script stops being a CLASSIC script, and all 56 inline `onclick=` handlers go dead
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

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


def test_assets_declare_the_content_types_browsers_require():
    """A stylesheet served as anything but text/css is silently ignored in standards mode.

    No console error, no failed request — just an unstyled page. Worth pinning rather
    than relying on `mimetypes` guessing the same thing on every machine.
    """
    assert w._STATIC_ASSETS["/static/dashboard.css"][1].startswith("text/css")
    assert "javascript" in w._STATIC_ASSETS["/static/dashboard.js"][1]


def test_served_html_has_a_resolved_cache_buster():
    html = w._index_html()
    assert "__ASSET_V__" not in html, "cache-buster token was not substituted"
    assert f"?v={w._asset_version()}" in html


def test_cache_buster_changes_when_an_asset_changes(monkeypatch):
    """Version alone doesn't move during development; mtime does.

    Bump past the NEWEST asset, not past this file's own mtime. `_asset_version` is a max over
    every asset, so bumping dashboard.js by 60s does nothing whenever dashboard.css happens to
    be more than 60s newer — which it is, every time the last edit was to the CSS. This test
    had been passing on the incidental fact that the JS was usually touched last, and started
    failing the first time a change was CSS-only.
    """
    import os

    before = w._asset_version()
    js = w._STATIC_DIR / "dashboard.js"
    original = js.stat().st_mtime
    newest = max((w._STATIC_DIR / name).stat().st_mtime for name, _ in w._STATIC_ASSETS.values())
    try:
        os.utime(js, (newest + 60, newest + 60))
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


_HANDLER_RE = r'\bon[a-z]+\s*=\s*"\s*([A-Za-z_$][\w$]*)\s*\('

# Runs dashboard.js at GLOBAL scope, the way a browser runs `<script src>`, then asks the
# same question the browser asks when you click: does this name resolve? A `new Function`
# wrapper (what test_dashboard_render.py uses) would answer "yes" even for a module, so
# this deliberately does not use one.
_SCOPE_PROBE = """
import vm from 'node:vm';
import fs from 'node:fs';
const src = fs.readFileSync(process.argv[2], 'utf8');
const html = fs.readFileSync(process.argv[3], 'utf8');
const names = new Set([...(src + html).matchAll(/__HANDLER_RE__/g)].map(m => m[1]));
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, removeAttribute(){}, focus(){},
  scrollIntoView(){},
  classList:{toggle(){},add(){},remove(){}}, addEventListener(){}, appendChild(){}, dataset:{} });
// A vm context starts EMPTY — it does not inherit Node's globals, so `URL` and
// `URLSearchParams` have to be handed in even though they exist in the outer process. The Space
// nav parses `?space=` at module load, and without these the script throws before a single
// handler is defined, which reads as every handler having gone missing at once.
const ctx = vm.createContext({
  document: { getElementById: el, querySelectorAll: ()=>[], querySelector: el,
              addEventListener(){}, activeElement:null, body: el() },
  location: { href:'http://localhost:8765/', search:'' },
  history: { replaceState(){}, pushState(){} },
  window: { open(){}, location:{href:''} },
  URL, URLSearchParams,
  navigator: { clipboard: { writeText(){} } },
  setInterval: () => 0, setTimeout: () => 0, clearTimeout: () => {},
  fetch: async () => ({ ok:true, json: async () => ({}) }),
  alert: () => {}, confirm: () => true, console,
});
vm.runInContext(src, ctx, { filename: 'dashboard.js' });
const missing = [...names].filter(n => vm.runInContext(`typeof ${n}`, ctx) !== 'function');
console.log(JSON.stringify({ checked: names.size, missing }));
""".replace("__HANDLER_RE__", _HANDLER_RE)  # the pattern is valid in both Python and JS as-is


def _probe_handler_scope(tmp_path, source: str) -> dict:
    script = tmp_path / "scope.mjs"
    script.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script), str(w._STATIC_DIR / "dashboard.js"), str(w._STATIC_DIR / "index.html")],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_every_inline_handler_resolves_at_global_scope(tmp_path):
    """The one regression this refactor could cause that every other test would miss.

    Nothing in Python or in the render test can tell you whether the browser can find
    `markSubmitted` when you click the button — the render test calls the functions
    directly. If dashboard.js ever becomes `type="module"`, or a function is renamed
    without updating the attribute, every button on the page goes dead SILENTLY and the
    whole suite stays green.
    """
    result = _probe_handler_scope(tmp_path, _SCOPE_PROBE)
    assert result["checked"] > 30, f"handler regex stopped matching: {result['checked']} found"
    assert not result["missing"], "inline handlers that resolve to nothing: " + ", ".join(result["missing"])


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_the_handler_probe_would_actually_catch_module_scoping(tmp_path):
    """Negative control — a passing test that can't fail is worse than no test.

    Wrapping the source in a function reproduces what `type="module"` does to scoping.
    If this ever finds nothing missing, the probe above has quietly stopped working.
    """
    scoped = _SCOPE_PROBE.replace(
        "vm.runInContext(src, ctx, { filename: 'dashboard.js' });",
        'vm.runInContext("(function(){" + src + "})()", ctx);',
    )
    result = _probe_handler_scope(tmp_path, scoped)
    assert len(result["missing"]) > 20, "module scoping did not break the handlers — probe is vacuous"


@pytest.mark.parametrize("path", [
    "/static/../web_dashboard.py",
    "/static/../../../../etc/passwd",
    "/static/",
    "/static/index.html",          # deliberately not servable: it goes through _index_html()
    "/api/status",
])
def test_only_the_allowlisted_assets_resolve(path):
    assert path not in w._STATIC_ASSETS
