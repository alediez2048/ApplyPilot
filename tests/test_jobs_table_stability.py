"""The 2.5s poll must not rebuild #jobs when nothing changed.

Reported as two symptoms that turned out to be one cause:

    "it does not let me scroll down smoothly, it keeps taking me back up when i scroll"
    "i CANT EASILY COPY AND PASTE the text, this is the same across all text boxes"

`renderJobsTable` ends in `document.getElementById('jobs').innerHTML = ...`, which destroys and
rebuilds every node beneath it. That resets each textarea's `scrollTop` to 0 and collapses any
selection — every 2.5 seconds, forever. The focus guard could not see either one: **scrolling a
textarea does not move `document.activeElement`**, and a selection dragged across a description
or a transcript is not an input at all.

Measured on the live dashboard before writing any of this: two `/api/status` bodies three
seconds apart were identical across **1.65 MB** except two `due_in_h` countdowns on one contact,
which change hourly. So the steady state was rebuilding a byte-identical tree ~1,440 times an
hour, and every one of those was a chance to interrupt whatever the operator was doing.

Three fixes, all here:

    1. skip the write when the generated HTML is unchanged   <- the scroll and the copy
    2. re-check the edit guard AT the write, not before the fetch that precedes it
    3. count a live selection as "busy", not only focus
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from browser_stubs import BROWSER_GLOBALS

JS = Path(__file__).resolve().parents[1] / "src/applypilot/static/dashboard.js"


#: A tracked #jobs node. Every other id gets a throwaway stub, so the only innerHTML writes this
#: counts are the ones under test.
#:
#: `WRITES` lives on globalThis deliberately. A module-level `let` is invisible inside the
#: `new Function` scope the bundle is built in, so an accessor declared in there reads a global
#: that does not exist — which is how the first version of this harness reported 0 writes for a
#: render that had demonstrably happened (the HTML cache was populated). Same family as the
#: shadowed `post` stub in test_contact_flag.py: the seam has to be reachable from BOTH scopes.
_HARNESS = """
globalThis.WRITES = [];
const el = (tag) => ({ tagName: tag || 'DIV', innerHTML:'', textContent:'', hidden:false,
  value:'', style:{}, closest:()=>null, querySelector:()=>null, querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, addEventListener(){}, appendChild(){},
  classList:{add(){},remove(){},toggle(){},contains:()=>false}, dataset:{} });
const jobsNode = { tagName:'DIV', _html:'', textContent:'', style:{}, dataset:{},
  closest(){ return this; }, querySelector:()=>null, querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, addEventListener(){}, appendChild(){},
  classList:{add(){},remove(){},toggle(){},contains:()=>false},
  get innerHTML(){ return this._html; },
  set innerHTML(v){ this._html = v; globalThis.WRITES.push(v); } };
globalThis.document = {
  getElementById: id => id === 'jobs' ? jobsNode : el(),
  querySelector: () => el(), querySelectorAll: () => [],
  addEventListener(){}, body: el(), hasFocus: () => false, activeElement: null };
globalThis.alert = () => {};
globalThis.getSelection = () => ({ isCollapsed:true, rangeCount:0,
  anchorNode:null, focusNode:null });
"""

#: One job, complete enough for `jobRows` + `stepStrip` to render without throwing.
_JOB = """{
  url:'http://j/1', title:'Forward Deployed AI Engineer', company:'Acme', description:'d',
  status:'applied', applied_at:'2026-08-01T10:00:00', site:'Acme', strategy:'dashboard_upload',
  contacts:[], followups:{}, shape:'pipeline/jobs' }"""


def _run(body: str, tmp_path) -> dict:
    script = tmp_path / "stab.mjs"
    script.write_text(
        BROWSER_GLOBALS + _HARNESS + """
const SRC = """ + json.dumps(JS.read_text(encoding="utf-8")) + """;
const F = (new Function(SRC + `; return {
  renderJobsTable, isEditingJobs, hasSelectionInJobs, switchSpace,
  getHtmlCache: () => LAST_JOBS_HTML, setHtmlCache: v => { LAST_JOBS_HTML = v; },
  writes: () => globalThis.WRITES, resetWrites: () => { globalThis.WRITES = []; }
};`))();
await new Promise(r => setImmediate(r));   // drain the bundle's own startup refresh()
F.resetWrites();
""" + body, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── 1. the write is skipped when nothing changed ────────────────────────────

def test_an_unchanged_render_does_not_touch_the_dom(tmp_path):
    """The whole fix. Three identical renders must produce ONE write — the other two are what
    were resetting scrollTop and collapsing the selection."""
    out = _run("""
const jobs = [%s];
F.renderJobsTable(jobs, false);
F.renderJobsTable(jobs, false);
F.renderJobsTable(jobs, false);
console.log(JSON.stringify({ writes: F.writes().length }));
""" % _JOB, tmp_path)
    assert out["writes"] == 1, (
        "#jobs was rebuilt for an identical payload — this is the scroll reset and the lost "
        "selection")


def test_a_real_change_still_writes_immediately(tmp_path):
    """Guard the guard. Never writing also passes the test above, and would freeze the table."""
    out = _run("""
const a = [%s];
const b = JSON.parse(JSON.stringify(a)); b[0].title = 'Something else';
F.renderJobsTable(a, false);
F.renderJobsTable(b, false);
const two = F.writes().length;
console.log(JSON.stringify({ two, changed: F.writes()[1].includes('Something else') }));
""" % _JOB, tmp_path)
    assert out["two"] == 2 and out["changed"] is True


def test_the_cache_holds_exactly_what_was_written(tmp_path):
    """A fingerprint computed from the payload rather than the markup can disagree with the
    screen. The cache is the rendered string itself, so it cannot."""
    out = _run("""
F.renderJobsTable([%s], false);
console.log(JSON.stringify({ same: F.getHtmlCache() === F.writes()[0] }));
""" % _JOB, tmp_path)
    assert out["same"] is True


def test_switching_space_drops_the_cache(tmp_path):
    """Otherwise the compare is against the previous Space's markup. Two empty Spaces rendering
    the same empty string is not far-fetched, and the failure is a tab that renders nothing."""
    out = _run("""
F.renderJobsTable([%s], false);
const before = F.getHtmlCache() !== null;
globalThis.location = { href:'http://x/' };
globalThis.history = { replaceState(){} };
try { F.switchSpace('another-space'); } catch (e) {}
console.log(JSON.stringify({ before, after: F.getHtmlCache() }));
""" % _JOB, tmp_path)
    assert out["before"] is True
    assert out["after"] is None


# ── 2. the guard is re-checked at the write ─────────────────────────────────

def test_focus_arriving_during_the_fetch_still_blocks_the_write(tmp_path):
    """`refresh()` computes `editing` BEFORE `await fetch(...)`, so it describes the page ~100ms
    ago. Clicking into a draft inside that window used to land the write anyway and take the
    focus, the selection and the scroll with it."""
    out = _run("""
const jobs = [%s];
// `editing` was false when refresh() sampled it; by the time the write runs, a textarea in
// #jobs has focus.
globalThis.document.activeElement = { tagName:'TEXTAREA', closest: s => s === '#jobs' ? {} : null };
F.renderJobsTable(jobs, false);
console.log(JSON.stringify({ writes: F.writes().length }));
""" % _JOB, tmp_path)
    assert out["writes"] == 0, "a stale 'not editing' flag let the write destroy a live field"


def test_the_stale_flag_is_still_honoured_when_it_says_editing(tmp_path):
    """Belt and braces: the passed flag must not be ignored either."""
    out = _run("""
F.renderJobsTable([%s], true);
console.log(JSON.stringify({ writes: F.writes().length }));
""" % _JOB, tmp_path)
    assert out["writes"] == 0


# ── 3. a selection counts as busy ───────────────────────────────────────────

def test_a_selection_inside_jobs_blocks_the_rebuild(tmp_path):
    """The copy half. A range dragged across a description or a transcript is not an input, so
    the focus guard returned false and the tick rebuilt the nodes mid-drag."""
    out = _run("""
const node = { nodeType:1, closest: s => s === '#jobs' ? {} : null };
globalThis.getSelection = () => ({ isCollapsed:false, rangeCount:1,
  anchorNode: node, focusNode: node });
const busy = F.isEditingJobs();
F.renderJobsTable([%s], false);
console.log(JSON.stringify({ busy, writes: F.writes().length }));
""" % _JOB, tmp_path)
    assert out["busy"] is True and out["writes"] == 0


def test_a_bare_caret_does_not_block_anything(tmp_path):
    """`isCollapsed` is a click, not a selection. Treating it as busy freezes the table for
    anyone who merely clicked once."""
    out = _run("""
globalThis.getSelection = () => ({ isCollapsed:true, rangeCount:1,
  anchorNode:{ nodeType:1, closest: () => ({}) }, focusNode:null });
console.log(JSON.stringify({ busy: F.hasSelectionInJobs() }));
""", tmp_path)
    assert out["busy"] is False


def test_a_selection_elsewhere_on_the_page_does_not_block(tmp_path):
    """Selecting the app-dir line at the top of the page must not stop the table updating."""
    out = _run("""
const outside = { nodeType:1, closest: () => null };
globalThis.getSelection = () => ({ isCollapsed:false, rangeCount:1,
  anchorNode: outside, focusNode: outside });
const busy = F.hasSelectionInJobs();
F.renderJobsTable([%s], false);
console.log(JSON.stringify({ busy, writes: F.writes().length }));
""" % _JOB, tmp_path)
    assert out["busy"] is False and out["writes"] == 1


def test_a_selection_anchored_in_a_text_node_is_found(tmp_path):
    """A real range's endpoints are TEXT nodes (nodeType 3), which have no `closest` of their
    own — the check has to climb to parentElement or it never fires in a browser while passing
    every element-node test written for it."""
    out = _run("""
const textNode = { nodeType:3, parentElement:{ closest: s => s === '#jobs' ? {} : null } };
globalThis.getSelection = () => ({ isCollapsed:false, rangeCount:1,
  anchorNode: textNode, focusNode: textNode });
console.log(JSON.stringify({ busy: F.hasSelectionInJobs() }));
""", tmp_path)
    assert out["busy"] is True


def test_focus_in_a_field_still_blocks(tmp_path):
    """The behaviour that already existed, pinned so broadening the guard did not replace it."""
    out = _run("""
globalThis.document.activeElement = { tagName:'INPUT', closest: s => s === '#jobs' ? {} : null };
console.log(JSON.stringify({ busy: F.isEditingJobs() }));
""", tmp_path)
    assert out["busy"] is True
