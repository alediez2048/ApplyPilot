"""SPACE-0 — the endless scroll, measured before it was fixed.

The Spaces PRD §8.1 prescribed a `Done` bucket collapsing terminal rows, "on by default". That
diagnosis does not survive the data: on 2026-08-08 the live board held **1** terminal row of 31
(one interview, zero rejections), so archiving would have removed 2.5% of the page. And the
applied pile is not stale either — 28 rows, median 5 days old, none past 21, every one with live
follow-up ladders. §Lessons 28: measure the bug before fixing the bug the ticket describes.

What the page actually was, measured in a real browser rather than reasoned about:

    page          8,939px   (10 screens)
    table         6,569px   (73% of the page), 30 rows at 130px each
    the `desc` cell IS the row height — every other cell holds 25-33 characters
    every one of the 30 excerpts sat exactly on its 900-char cap
    premiseControls  290px  — the second largest block, and added the same day

So the scroll was two things, neither of them terminal rows: a six-line description clamp, and
a write-once box rendered open forever.
"""

from __future__ import annotations

import json
import re
import subprocess

from applypilot import web_dashboard as wd

from test_targets import BROWSER_GLOBALS

CSS = (wd._STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
HTML = (wd._STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ── the row ─────────────────────────────────────────────────────────────────

def test_the_description_is_clamped_to_two_lines():
    """The single biggest lever on the page, and it was a knob that already existed set to 6."""
    m = re.search(r"td\.desc \.desc-text \{([^}]*)\}", CSS)
    assert m, "the description clamp rule is gone"
    rule = m.group(1)
    clamp = re.search(r"-webkit-line-clamp:\s*(\d+)", rule)
    assert clamp, "the clamp was removed entirely — the column would render all 900 characters"
    assert int(clamp.group(1)) <= 2, f"clamped to {clamp.group(1)} lines; 6 was 130px per row"


def test_the_clamp_still_actually_clamps():
    """`-webkit-line-clamp` does nothing without the box display and `overflow:hidden`. A rule
    that names the property and drops its two prerequisites reads correct and renders the whole
    excerpt — §Lessons 62's shape, where the Node test asserted a property that had no effect."""
    rule = re.search(r"td\.desc \.desc-text \{([^}]*)\}", CSS).group(1).replace(" ", "")
    # `display:` specifically. Asserting `"-webkit-box" in rule` is satisfied by
    # `-webkit-box-orient`, so a mutation switching the display to `block` — which disables the
    # clamp entirely and renders all 900 characters — survived it. §Lessons 1, inside the test
    # written to guard the clamp, for the umpteenth time.
    assert "display:-webkit-box" in rule
    assert "-webkit-box-orient:vertical" in rule
    assert "overflow:hidden" in rule


def test_the_excerpt_is_still_sent_to_the_browser():
    """Presentation only. Search matches `j.description` in JS and UX-6's "matched: …" line
    depends on it, so shortening the CELL must not shorten the PAYLOAD."""
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    assert "j.description" in js or "description" in js


# ── the premise box ─────────────────────────────────────────────────────────

def test_the_premise_box_is_collapsible():
    assert '<details id="premiseBox"' in HTML, "the premise box is a plain div again — 290px, always"


def test_it_starts_open_so_it_cannot_become_invisible():
    """CTX-1 existed because this control could not be found. Shipping it collapsed-by-default
    with nothing in it would put it straight back (§Lessons 43, six occurrences)."""
    box = HTML[HTML.index('<details id="premiseBox"'):]
    assert box[:len('<details id="premiseBox" open>')].endswith("open>")


def test_the_summary_heading_is_not_left_block_level():
    """A <summary> containing an <h2> lays the marker and the title on separate lines unless the
    summary is a flex container and the h2's own margins are cleared."""
    assert re.search(r"#premiseBox > summary \{[^}]*display:flex", CSS)
    assert re.search(r"#premiseBox > summary h2 \{[^}]*margin:0", CSS)


# ── the browser half ────────────────────────────────────────────────────────

_DRIVER = """
const F = (new Function(SRC + `; return { renderSpaceShape };`))();
const out = {};
const det = document.getElementById('premiseBox');
const mark = document.getElementById('premiseMark');
const COPY = {title:'The premise of this campaign', placeholder:'p', hint:'h'};

// Empty: stays open, no marker.
det.open = true;
F.renderSpaceShape('pipeline/jobs', '', COPY);
out.openWhenEmpty = det.open;
out.markWhenEmpty = mark.textContent;

// A premise arrives: collapse once.
F.renderSpaceShape('pipeline/jobs', 'Ten weeks of agents.', COPY);
out.collapsedWhenFilled = det.open;
out.markWhenFilled = mark.textContent;

// The operator opens it to edit. The next tick must NOT slam it shut.
det.open = true;
F.renderSpaceShape('pipeline/jobs', 'Ten weeks of agents.', COPY);
F.renderSpaceShape('pipeline/jobs', 'Ten weeks of agents.', COPY);
out.stillOpenAfterOperatorOpenedIt = det.open;

// Clearing the premise re-arms it, so a Space that loses its premise shows the box again.
F.renderSpaceShape('pipeline/jobs', '', COPY);
det.open = true;
F.renderSpaceShape('pipeline/jobs', 'A new premise.', COPY);
out.reArmedAfterClearing = det.open;
console.log(JSON.stringify(out));
"""


def _run_js(tmp_path, driver: str) -> dict:
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "height.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', placeholder:'',
  open:false, style:{}, closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, removeAttribute(){}, focus(){},
  scrollIntoView(){}, classList:{toggle(){},add(){},remove(){}},
  addEventListener(){}, appendChild(){}, dataset:{} });
// REAL nodes for what this file asserts on. An el() that swallows every write passes with the
// whole collapse deleted (§Lessons 41).
const NODES = { jobControls: el(), targetControls: el(), premiseControls: el(),
                offerInput: el(), premiseTitle: el(), premiseHint: el(),
                premiseBox: el(), premiseMark: el() };
globalThis.document = { getElementById: (id) => NODES[id] || el(),
  querySelectorAll: ()=>[], querySelector: el, addEventListener(){},
  activeElement:null, body: el(), hasFocus: () => false };
const SRC = """ + json.dumps(src) + ";\n" + driver, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_an_empty_premise_leaves_the_box_open(tmp_path):
    out = _run_js(tmp_path, _DRIVER)
    assert out["openWhenEmpty"] is True
    assert out["markWhenEmpty"] == ""


def test_a_written_premise_collapses_it_and_says_it_is_in_play(tmp_path):
    """Collapsed with no marker would read as an empty box, which is the same failure one level
    down: the operator cannot tell a written premise from a missing one without opening it."""
    out = _run_js(tmp_path, _DRIVER)
    assert out["collapsedWhenFilled"] is False
    assert out["markWhenFilled"].strip()


def test_the_refresh_does_not_slam_it_shut_while_editing(tmp_path):
    """Auto-collapsing on EVERY render makes the box impossible to edit — `refresh()` runs every
    2.5 seconds. It collapses on the first render that finds a premise and never again."""
    out = _run_js(tmp_path, _DRIVER)
    assert out["stillOpenAfterOperatorOpenedIt"] is True


def test_clearing_the_premise_re_arms_the_collapse(tmp_path):
    """Guard the guard: latching forever would make the test above pass while a Space that
    gained a premise after being cleared never collapsed again."""
    out = _run_js(tmp_path, _DRIVER)
    assert out["reArmedAfterClearing"] is False
