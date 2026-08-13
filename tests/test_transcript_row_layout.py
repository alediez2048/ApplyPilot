"""The meeting row reads as a list item, and the borrowed banner is legible.

Two things reported by looking at them.

**The transcript row was six columns on one line**, with the title in a `minmax(0,1fr)` and no
overflow rule — so a real meeting name ("Waheed Ale Post NVDA Hackathon and Project Manager at
ARM connect") wrapped into five stacked words in a narrow column while the summary beside it was
clipped to nothing. Titles are whatever the operator pasted, so they are long by default; the
one-line grid only ever worked for a short fixture. And the size read **0k** for anything under
500 characters, which says nothing about a meeting and reads as an error.

**The borrowed-contact banner rendered as a near-black bar with dark text on it.** It hardcoded
a cream background plus a `@media (prefers-color-scheme: dark)` override, and this stylesheet
has NO dark theme — that block was the only one in the file. On a dark-OS machine it fired
alone: the banner went dark, its text did not, and every other element stayed light.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import applypilot.web_dashboard as wd

CSS = (Path(wd.__file__).parent / "static" / "dashboard.css").read_text(encoding="utf-8")


def _run(tmp_path, tail):
    from browser_stubs import BROWSER_GLOBALS
    src = (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "probe.mjs"
    script.write_text(
        BROWSER_GLOBALS
        + "const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},"
          " closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},"
          " getAttribute:()=>null, addEventListener(){}, appendChild(){},"
          " classList:{add(){},remove(){},toggle(){}}, dataset:{} });\n"
          "globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),"
          " querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,"
          " activeElement:null };\n"
        + f"const SRC = {json.dumps(src)};\n" + tail, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(autouse=True)
def node():
    if not shutil.which("node"):
        pytest.skip("node not available")


# ── the size, which read 0k on a real meeting ───────────────────────────────

def test_a_short_transcript_does_not_read_as_zero(tmp_path):
    """`Math.round(len/1000)+'k'` printed 0k for everything under 500 characters."""
    out = _run(tmp_path, "const F = (new Function(SRC + '; return { transcriptSize };'))();\n"
               "console.log(JSON.stringify({v: [0, 1, 480, 999, 2400, 45000].map(F.transcriptSize)}));\n")
    assert out["v"] == ["no text stored", "1 chars", "480 chars", "999 chars", "2.4k", "45k"]


# ── the row ─────────────────────────────────────────────────────────────────

def test_a_long_title_cannot_wrap_into_a_column(tmp_path):
    """The reported look. A title is one line, ellipsised, with the full text on hover."""
    rule = re.search(r"\.tr-title\s*\{([^}]*)\}", CSS)
    assert rule, ".tr-title has no rule"
    body = rule.group(1)
    assert "white-space:nowrap" in body.replace(" ", "")
    assert "text-overflow:ellipsis" in body.replace(" ", "")


def test_the_summary_gets_a_line_of_its_own(tmp_path):
    """Six columns on one line is what squeezed the title AND clipped the summary to nothing."""
    rule = re.search(r"\.tr-meta\s*\{([^}]*)\}", CSS)
    assert rule, "there is no second line"
    assert "grid-row:2" in rule.group(1).replace(" ", "")


def test_the_row_renders_title_actions_and_a_meta_line(tmp_path):
    from applypilot.networking import transcripts as _t  # noqa: F401
    out = _run(tmp_path, """
const F = (new Function(SRC + '; return { transcriptSection };'))();
const C = {id:'c1', full_name:'Waheed', transcripts:[{id:'t1',
  title:'Waheed Ale Post NVDA Hackathon and Project Manager at ARM connect',
  started_at:'2026-08-13T10:00', summary:'I had a really interesting conversation with...',
  body_len: 420}]};
const h = F.transcriptSection(C);
console.log(JSON.stringify({
  title: h.includes('tr-title'), meta: h.includes('tr-meta'),
  size: h.includes('420 chars'), zero: h.includes('0k'),
  hover: h.includes('title="Waheed Ale Post'),
}));
""")
    assert out["title"] and out["meta"]
    assert out["size"] and not out["zero"], "a 420-character meeting still reads as 0k"
    assert out["hover"], "the full title is unreachable once it is ellipsised"


def test_a_transcript_with_no_summary_says_so(tmp_path):
    """An empty cell beside a date reads as a broken row (§Lessons 96)."""
    assert ".tr-sum:empty::before" in CSS


# ── the banner ──────────────────────────────────────────────────────────────

def test_the_stylesheet_has_no_lone_dark_mode_override():
    """This app has no dark theme. ONE element overriding for dark mode is not a nicety — it is
    a guarantee that element disagrees with the page, which is exactly what happened: a
    near-black bar with unchanged dark text on it."""
    assert "prefers-color-scheme" not in CSS.split("*/")[0] or True
    rules = re.findall(r"@media\s*\(prefers-color-scheme[^)]*\)\s*\{", CSS)
    assert not rules, f"a dark-mode block is back in a stylesheet with no dark theme: {rules}"


def test_the_banner_uses_the_palette_and_sets_its_own_text_colour():
    """Setting a background without a foreground is how it became illegible."""
    rule = re.search(r"\.borrowed\s*\{([^}]*)\}", CSS)
    assert rule, ".borrowed has no rule"
    body = rule.group(1).replace(" ", "")
    assert "var(--yellow-soft)" in body and "var(--yellow)" in body
    assert "color:var(--text)" in body, "background themed, text left to chance"
    assert "#2a2314" not in body and "#fff8e6" not in body, "still hardcoding colours"


def test_the_title_is_on_the_FIRST_line():
    """Auto-placement put it on row THREE: the buttons are pinned to row 1 and the meta line to
    row 2, so the only slot left for the title was under its own date and summary. The markup is
    identical either way — only a browser shows it (§Lessons 101)."""
    rule = re.search(r"\.tr-title\s*\{([^}]*)\}", CSS)
    assert "grid-row:1" in rule.group(1).replace(" ", ""), \
        "the meeting name is auto-placed and lands below its own metadata"


def test_every_cell_in_the_row_has_an_explicit_row():
    """The general form of the same bug: one unplaced child in a grid whose siblings are all
    pinned gets pushed past them, silently."""
    for cls, want in [("tr-title", "1"), ("tr-meta", "2")]:
        rule = re.search(rf"\.{cls}\s*\{{([^}}]*)\}}", CSS)
        assert f"grid-row:{want}" in rule.group(1).replace(" ", ""), f".{cls} is unplaced"
    assert "grid-row:1" in re.search(r"\.tr-row\s*>\s*button\s*\{([^}]*)\}",
                                     CSS).group(1).replace(" ", "")
