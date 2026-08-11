"""💡 sends a contact to the TOP of the People list, not to the top of its own group.

Asked for as *"if I click Highlight on a contact, I want it to go all the way up to the top"*.

`peopleList` already grouped people twice — `🔥 People you know here` and `🧊 New contacts` —
so the cheap reading is "sort flagged first within each group". That fails the request exactly
where it matters: a flagged COLD contact would still render below every hot one, which on a job
with fifteen connections is nowhere near the top. The flag is pulled out of both groups into a
third that renders first.

**It is a group, not a silent reorder.** Every other ordering on this list is derived — `hot`
is something the system worked out — and a row that moves for an unstated reason reads as a bug.
The header names the reason and carries the count, and removing flagged people from the other
two keeps *their* counts describing what is actually rendered beneath them.

**The move happens on the click.** `toggleFlag` paints optimistically and the next natural
refresh is up to 2.5s away, which is long enough to read as "the bulb lit and nothing happened".
`rerenderJobs()` renders from `LAST_JOBS`, so the flag is mirrored there first — otherwise the
row re-renders with its old value and drops straight back down (§Lessons 21).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from browser_stubs import BROWSER_GLOBALS

JS = Path(__file__).resolve().parents[1] / "src/applypilot/static/dashboard.js"
CSS = Path(__file__).resolve().parents[1] / "src/applypilot/static/dashboard.css"


def _run(body: str, tmp_path) -> dict:
    script = tmp_path / "flagtop.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, classList:{add(){},remove(){},
  toggle(){}}, dataset:{} });
globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),
  querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,
  activeElement:null };
globalThis.alert = () => {};
const SRC = """ + json.dumps(JS.read_text(encoding="utf-8")) + """;
// Accessors, not globals. The bundle declares its own top-level `LAST_JOBS` and
// `rerenderJobs`, which SHADOW anything on globalThis inside this Function scope — the same
// trap that made a `post` stub silently unreachable in test_contact_flag.py. Setting
// globalThis.LAST_JOBS and then asserting it was untouched passes for the wrong reason and
// proves nothing (§Lessons 13, 71). Reach the real bindings or do not test them.
const F = (new Function(SRC + `; return {
  peopleList, setFlagIn, toggleFlag, contactRow,
  getJobs: () => LAST_JOBS,
  setJobs: v => { LAST_JOBS = v; },
  setRerender: fn => { rerenderJobs = fn; }
};`))();
// The bundle ends with a bare `refresh();` (line ~3670). Building F therefore starts a real
// refresh that is parked on `await fetch(...)`, and it RESUMES at the first await in the test —
// setting LAST_JOBS = (data.jobs || []) and wiping whatever the test had just put there. It
// cost a debugging round: `setJobs` round-tripped fine, then the value was gone after
// `await toggleFlag(...)`, with nothing in either function touching it. Drain it first so every
// test starts from a settled state. `setImmediate` is Node's own — BROWSER_GLOBALS stubs
// setTimeout to a no-op, so a timer-based wait would never resolve.
await new Promise(r => setImmediate(r));
""" + body, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


#: A flagged COLD contact under hot ones. This shape is the whole test: sorting inside a group
#: leaves Zoe below Hana and Hugo, which is not the top.
_JOB = """{
  url:'http://j/1', contacts:[
    { id:'h1', full_name:'Hana', hot:true,  flagged:false, email:'h@x.test' },
    { id:'h2', full_name:'Hugo', hot:true,  flagged:false, email:'g@x.test' },
    { id:'c1', full_name:'Cleo', hot:false, flagged:false, email:'c@x.test' },
    { id:'z1', full_name:'Zoe',  hot:false, flagged:true,  email:'z@x.test' }
  ]}"""


def test_a_flagged_cold_contact_outranks_every_hot_one(tmp_path):
    """The request, stated as a position. Zoe is cold and flagged; Hana and Hugo are hot."""
    out = _run("""
const h = F.peopleList(%s);
const at = n => h.indexOf(n);
console.log(JSON.stringify({
  zoe: at('Zoe'), hana: at('Hana'), hugo: at('Hugo'), cleo: at('Cleo'),
  hdr: h.indexOf('Highlighted'),
}));
""" % _JOB, tmp_path)
    assert out["zoe"] > -1 and out["hana"] > -1
    assert out["zoe"] < out["hana"], "a flagged cold contact still renders below the hot group"
    assert out["zoe"] < out["hugo"] and out["zoe"] < out["cleo"]
    assert -1 < out["hdr"] < out["zoe"], "the Highlighted header must precede its rows"


def test_the_flagged_person_leaves_the_other_group(tmp_path):
    """Rendering Zoe twice — once highlighted, once in her original group — would satisfy the
    position test above and show the same person on the list twice."""
    out = _run("""
const h = F.peopleList(%s);
const n = (h.match(/>Zoe</g) || []).length;
console.log(JSON.stringify({ zoeRows: n }));
""" % _JOB, tmp_path)
    assert out["zoeRows"] == 1


def test_the_other_group_counts_describe_what_is_rendered(tmp_path):
    """Zoe is out of the cold group, so the cold count must be 1, not 2. A count that still
    includes a person rendered elsewhere is a number nobody can reconcile with the rows."""
    out = _run("""
const h = F.peopleList(%s);
const pick = cls => {
  const m = h.match(new RegExp('ppl-group ' + cls + '"[^]*?ppl-g-n">(\\\\d+)<'));
  return m ? Number(m[1]) : null;
};
console.log(JSON.stringify({ flagged: pick('flagged'), hot: pick('hot'), cold: pick('cold') }));
""" % _JOB, tmp_path)
    assert out == {"flagged": 1, "hot": 2, "cold": 1}


def test_no_flagged_contacts_means_no_extra_header(tmp_path):
    """An always-rendered "💡 Highlighted 0" is a permanent empty shelf on every job."""
    out = _run("""
const h = F.peopleList({ url:'http://j/2', contacts:[
  { id:'a', full_name:'Ann', hot:true, flagged:false, email:'a@x.test' }]});
console.log(JSON.stringify({ hasHdr: h.includes('Highlighted'), hasAnn: h.includes('Ann') }));
""", tmp_path)
    assert out == {"hasHdr": False, "hasAnn": True}


def test_unflagging_puts_them_back_where_they_belong(tmp_path):
    """The move has to be reversible, and back into the RIGHT group — a cold contact must not
    return as a hot one."""
    out = _run("""
const job = { url:'http://j/3', contacts:[
  { id:'h1', full_name:'Hana', hot:true,  flagged:false, email:'h@x.test' },
  { id:'z1', full_name:'Zoe',  hot:false, flagged:true,  email:'z@x.test' }]};
const on = F.peopleList(job);
job.contacts[1].flagged = false;
const off = F.peopleList(job);
console.log(JSON.stringify({
  onTop: on.indexOf('Zoe') < on.indexOf('Hana'),
  offTop: off.indexOf('Zoe') < off.indexOf('Hana'),
  backInCold: off.indexOf('New contacts') < off.indexOf('Zoe'),
  noHdr: !off.includes('Highlighted'),
}));
""", tmp_path)
    assert out == {"onTop": True, "offTop": False, "backInCold": True, "noHdr": True}


# ── the move happens on the click, not 2.5s later ───────────────────────────

def test_the_payload_is_updated_so_a_rerender_keeps_the_row_at_the_top(tmp_path):
    """`rerenderJobs()` reads LAST_JOBS. Without this mirror the row is re-rendered from the
    stale payload and drops back down — the click would look like it undid itself."""
    out = _run("""
const jobs = [{ url:'http://j/1', contacts:[{ id:'z1', full_name:'Zoe', flagged:false }] }];
F.setFlagIn(jobs, 'z1', true);
const after = jobs[0].contacts[0].flagged;
F.setFlagIn(jobs, 'z1', false);
console.log(JSON.stringify({ after, cleared: jobs[0].contacts[0].flagged }));
""", tmp_path)
    assert out == {"after": True, "cleared": False}


def test_setFlagIn_touches_only_the_named_contact(tmp_path):
    """It scans every job because ids are unique across them. A missing id check would flag the
    whole list."""
    out = _run("""
const jobs = [
  { url:'a', contacts:[{ id:'x', flagged:false }, { id:'y', flagged:false }] },
  { url:'b', contacts:[{ id:'z', flagged:false }] }];
F.setFlagIn(jobs, 'y', true);
console.log(JSON.stringify({
  x: jobs[0].contacts[0].flagged, y: jobs[0].contacts[1].flagged, z: jobs[1].contacts[0].flagged }));
""", tmp_path)
    assert out == {"x": False, "y": True, "z": False}


def test_the_click_writes_the_flag_into_the_payload(tmp_path):
    """The mirror RUNS. Without this, deleting `setFlagIn(LAST_JOBS, ...)` from `toggleFlag`
    passed every other test in this file — because the sibling below asserts the stored value is
    `false`, which is also what it starts as, so "mirrored correctly" and "never ran" are the
    same observation (§Lessons 71). The two tests are only sound together: this one proves the
    write happens, that one proves it copies the right value."""
    out = _run("""
F.setJobs([{ url:'j', contacts:[{ id:'c1', full_name:'Zoe', flagged:false }] }]);
F.setRerender(() => {});
const row = { classList:{ toggle(){} } };
const btn = { classList:{ _on:false, contains(){ return this._on; }, toggle(k,v){ this._on=v; } },
  textContent:'', setAttribute(){}, closest:()=>row };
globalThis.fetch = async () => ({ ok:true, json: async () => ({ ok:true, flagged:true }) });
await F.toggleFlag('c1', btn);
console.log(JSON.stringify({ stored: F.getJobs()[0].contacts[0].flagged }));
""", tmp_path)
    assert out["stored"] is True, (
        "the flag never reached LAST_JOBS — rerenderJobs() will redraw the row unflagged "
        "and it drops straight back down")


def test_toggle_mirrors_the_servers_readback_not_the_optimistic_guess(tmp_path):
    """The POST is what decided. If the server stored something else — a race with another
    click, a contact deleted mid-flight — the list must follow the database, not the click."""
    out = _run("""
F.setJobs([{ url:'j', contacts:[{ id:'c1', full_name:'Zoe', flagged:false }] }]);
let rerendered = 0;
F.setRerender(() => { rerendered++; });
const row = { classList:{ toggle(){} } };
const btn = { classList:{ _on:false, contains(){ return this._on; }, toggle(k,v){ this._on=v; } },
  textContent:'', setAttribute(){}, closest:()=>row };
globalThis.fetch = async () => ({ ok:true,
  json: async () => ({ ok:true, flagged:false }) });   // server disagrees with the click
await F.toggleFlag('c1', btn);
console.log(JSON.stringify({ stored: F.getJobs()[0].contacts[0].flagged, rerendered }));
""", tmp_path)
    assert out["stored"] is False, "the optimistic value overwrote the server's answer"
    assert out["rerendered"] == 1


def test_a_failed_save_does_not_move_the_row(tmp_path):
    """A row that jumps to the top and stays there after the write failed is a lie about what
    is stored."""
    out = _run("""
F.setJobs([{ url:'j', contacts:[{ id:'c1', full_name:'Zoe', flagged:false }] }]);
let rerendered = 0;
F.setRerender(() => { rerendered++; });
const row = { classList:{ toggle(){} } };
const btn = { classList:{ _on:false, contains(){ return this._on; }, toggle(k,v){ this._on=v; } },
  textContent:'', setAttribute(){}, closest:()=>row };
globalThis.fetch = async () => ({ ok:true,
  json: async () => ({ ok:false, message:'nope' }) });
await F.toggleFlag('c1', btn);
console.log(JSON.stringify({ stored: F.getJobs()[0].contacts[0].flagged, rerendered }));
""", tmp_path)
    assert out == {"stored": False, "rerendered": 0}


def test_the_header_is_styled(tmp_path):
    """§Lessons 43: the won row was greyed 2.7% against white and the click "did nothing". A
    group header with no rule of its own inherits the muted default and reads as a third
    indistinguishable band."""
    css = CSS.read_text(encoding="utf-8")
    assert ".ppl-group.flagged" in css
