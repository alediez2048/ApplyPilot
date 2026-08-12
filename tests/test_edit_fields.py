"""EDIT-1 — edit a row's descriptive fields, and rename a Space.

Asked for as "if I double click on job name I should be able to change it … job name, job
description, tags etc, global across templates, space tabs should also be editable".

Two things were missing entirely rather than partly: **`title` had no write path anywhere**, and
`repo.spaces.rename()` had existed since SPACE-2 reachable from no endpoint and no button
(§Lessons 31 — a function nobody can invoke is not a feature).

**Tags are not edited directly.** They are DERIVED — from `location`, `salary`, `fit_score`,
`company` and the applied date — and the invariant is stated in CLAUDE.md: *"Tags are DERIVED
from fields already on the wire, so a tag cannot drift from its job."* Editing the FIELDS keeps
that true and is worth more than it sounds: `location` and `salary` were empty on all 81 rows, so
two of the five tag types had never rendered once.

The whitelist is the design. What is NOT editable is chosen for how silently each would fail:

    url          the ANCHOR. `contact_id` hashes it, so changing it orphans every contact,
                 ladder and message on the row, and nothing raises.
    fit_score    the model's judgement. Editable it stops being a signal, and the score filter
                 and `queue_for_*` both read it as one.
    applied_at   the state machine, driven by the row menu, which also logs why it moved.
    space_id     moving a row between Spaces is a different feature with its own questions.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.networking import sheet_import, store
from applypilot.repo import jobs as repo
from applypilot.repo import spaces as _spaces

from browser_stubs import BROWSER_GLOBALS


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    conn.execute("INSERT INTO jobs (url, title, company, site, strategy, space_id, fit_score) "
                 "VALUES (?,?,?,?,?,?,?)",
                 ("http://j/1", "Enginer", "Acme", "Greenhouse", "dashboard_upload",
                  "job-search", 7))
    conn.commit()
    return conn


def _row(conn, url="http://j/1"):
    r = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    return dict(zip(r.keys(), r))


# ── the edit ────────────────────────────────────────────────────────────────

def test_a_title_can_be_corrected(db):
    """There was no write path for this at all before EDIT-1 — a typo in a title was permanent
    unless the row was deleted and re-imported."""
    repo.set_fields("http://j/1", {"title": "Engineer"}, db)
    assert _row(db)["title"] == "Engineer"


@pytest.mark.parametrize("field,value", [
    ("title", "Staff Engineer"), ("company", "Acme Corp"),
    ("location", "Austin, TX"), ("salary", "$180k"),
    ("full_description", "Builds the thing."),
])
def test_every_whitelisted_field_writes(db, field, value):
    repo.set_fields("http://j/1", {field: value}, db)
    assert _row(db)[field] == value


def test_only_what_is_sent_is_touched(db):
    """`None` means "this caller did not show that field"; it is not the operator clearing it.
    Conflating the two is §Lessons 75, which shipped as a one-directional guard whose untested
    half dropped a typed paragraph."""
    repo.set_fields("http://j/1", {"title": "Engineer"}, db)
    repo.set_fields("http://j/1", {"location": "Austin"}, db)
    r = _row(db)
    assert r["title"] == "Engineer" and r["location"] == "Austin"
    assert r["company"] == "Acme", "an unsent field was blanked"


def test_an_empty_string_really_clears(db):
    """The other direction, and the reason `None` and `""` have to differ. Unlike the sheet
    importer — where a missing column means "this sheet does not say" — a box the operator
    emptied by hand is an instruction."""
    repo.set_fields("http://j/1", {"company": "Acme Corp"}, db)
    repo.set_fields("http://j/1", {"company": ""}, db)
    assert _row(db)["company"] == ""


def test_values_are_capped_and_the_caller_is_told_what_was_stored(db):
    """The browser adopts the returned value rather than what was typed. A silently truncated
    field that the screen still shows in full is a disagreement nobody can see."""
    wrote = repo.set_fields("http://j/1", {"title": "x" * 500}, db)
    assert len(wrote["title"]) == repo.EDITABLE_FIELDS["title"]
    assert _row(db)["title"] == wrote["title"]


def test_whitespace_is_stripped(db):
    assert repo.set_fields("http://j/1", {"title": "  Engineer  "}, db)["title"] == "Engineer"


# ── what must never be editable ─────────────────────────────────────────────

@pytest.mark.parametrize("field", ["url", "space_id", "fit_score", "applied_at",
                                   "apply_status", "rejected_at"])
def test_the_dangerous_fields_are_refused_loudly(db, field):
    """RAISES rather than ignoring. A silently dropped key is an edit the operator watched
    succeed and which never happened — the worst outcome available here."""
    with pytest.raises(ValueError) as e:
        repo.set_fields("http://j/1", {field: "x"}, db)
    assert field in str(e.value)


def test_editing_can_never_move_the_anchor(db):
    """The one that would be destructive and silent. `store.contact_id()` hashes `job_url`, so
    a row whose url changed keeps none of its contacts, ladders or messages — and nothing
    raises. Asserted with real contacts attached, not just on the column."""
    _spaces.create_space("sheets", "Sheets", "sheet", conn=db)
    sheet_import.import_sheet("sheets", "Company\tName\nRidgeline\tDana Okafor", db)
    card = "target:sheets:ridgeline"
    before = [r["id"] for r in db.execute(
        "SELECT id FROM contacts WHERE job_url = ?", (card,))]
    assert before

    repo.set_fields(card, {"title": "Solutions Engineer", "company": "Ridgeline Logistics"}, db)

    assert db.execute("SELECT 1 FROM jobs WHERE url = ?", (card,)).fetchone()
    after = [r["id"] for r in db.execute("SELECT id FROM contacts WHERE job_url = ?", (card,))]
    assert after == before, "the anchor moved and the card lost its people"
    orphans = db.execute("SELECT COUNT(*) FROM contacts c LEFT JOIN jobs j ON c.job_url = j.url "
                         "WHERE j.url IS NULL").fetchone()[0]
    assert orphans == 0


def test_renaming_a_sheet_company_does_not_reslug_it(db):
    """A targets anchor was BUILT from the name, so this changes what the row is CALLED and
    deliberately not what it is keyed on. They can disagree afterwards, and that is the correct
    trade — the alternative is the orphaning above."""
    _spaces.create_space("sheets", "Sheets", "sheet", conn=db)
    sheet_import.import_sheet("sheets", "Company\tName\nRidgeline\tDana", db)
    repo.set_fields("target:sheets:ridgeline", {"company": "Ridgeline Logistics"}, db)
    urls = [r["url"] for r in db.execute("SELECT url FROM jobs WHERE space_id='sheets'")]
    assert urls == ["target:sheets:ridgeline"]


def test_an_unknown_row_raises(db):
    with pytest.raises(ValueError):
        repo.set_fields("http://nope", {"title": "x"}, db)


def test_a_typed_description_stops_the_row_reading_as_a_failed_scrape(db):
    """§Lessons 44: a row carrying `detail_error` while holding a real description renders like
    a failure while being fine, and `queue_needing_detail` will never revisit it."""
    db.execute("UPDATE jobs SET detail_error='no data extracted' WHERE url='http://j/1'")
    db.commit()
    repo.set_fields("http://j/1", {"full_description": "Real text."}, db)
    r = _row(db)
    assert not r["detail_error"] and r["detail_scraped_at"]


def test_a_real_scrape_time_is_not_overwritten_by_typing(db):
    """The page really was visited; claiming the typing was a fetch loses that."""
    db.execute("UPDATE jobs SET detail_scraped_at='2026-01-01T00:00:00+00:00' "
               "WHERE url='http://j/1'")
    db.commit()
    repo.set_fields("http://j/1", {"full_description": "Typed."}, db)
    assert _row(db)["detail_scraped_at"] == "2026-01-01T00:00:00+00:00"


# ── the endpoint ────────────────────────────────────────────────────────────

def test_the_endpoint_saves_and_reports_what_it_stored(db):
    out = wd._edit_job({"url": "http://j/1", "title": "Engineer"})
    assert out["ok"] is True
    assert out["changed"] == ["title"] and out["values"]["title"] == "Engineer"


def test_the_endpoint_ignores_keys_it_does_not_own(db):
    """The browser posts `{url, <field>}`; `url` is the row selector and must not be read as a
    field to write. Filtering happens before the repo sees it, or every save would raise."""
    out = wd._edit_job({"url": "http://j/1", "title": "Engineer", "nonsense": "x"})
    assert out["ok"] is True
    assert _row(db)["title"] == "Engineer"


def test_the_endpoint_refuses_an_empty_save(db):
    assert wd._edit_job({"url": "http://j/1"})["ok"] is False
    assert wd._edit_job({"title": "x"})["ok"] is False


def test_it_works_the_same_on_a_sheet_card(db):
    """"Global across templates" — a row is a `jobs` row whatever the Space's shape."""
    _spaces.create_space("sheets", "Sheets", "sheet", conn=db)
    sheet_import.import_sheet("sheets", "Company\tName\nRidgeline\tDana", db)
    out = wd._edit_job({"url": "target:sheets:ridgeline", "location": "Austin, TX"})
    assert out["ok"] is True
    assert _row(db, "target:sheets:ridgeline")["location"] == "Austin, TX"


# ── the Space rename ────────────────────────────────────────────────────────

def test_a_space_can_be_renamed(db):
    _spaces.create_space("sheets", "Sheets", "sheet", conn=db)
    out = wd._rename_space({"space": "sheets", "name": "Lead sheet"})
    assert out["ok"] is True
    assert _spaces.load("sheets", db).name == "Lead sheet"


def test_renaming_never_changes_the_id(db):
    """The id is hashed into every targets `contact_id`, which is why `Space.with_()` refuses to
    change it and why this endpoint does not accept one."""
    _spaces.create_space("sheets", "Sheets", "sheet", conn=db)
    sheet_import.import_sheet("sheets", "Company\tName\nRidgeline\tDana", db)
    before = [r["id"] for r in db.execute("SELECT id FROM contacts")]
    wd._rename_space({"space": "sheets", "name": "Lead sheet", "id": "something-else"})
    assert _spaces.load("sheets", db) is not None
    assert [r["id"] for r in db.execute("SELECT id FROM contacts")] == before
    assert db.execute("SELECT COUNT(*) FROM jobs WHERE space_id='sheets'").fetchone()[0] == 1


def test_an_empty_name_is_refused(db):
    """A Space with a blank tab is unreachable — there is nothing left to click."""
    _spaces.create_space("sheets", "Sheets", "sheet", conn=db)
    assert wd._rename_space({"space": "sheets", "name": "   "})["ok"] is False
    assert _spaces.load("sheets", db).name == "Sheets"


def test_an_unknown_space_is_refused(db):
    assert wd._rename_space({"space": "nope-does-not-exist", "name": "X"})["ok"] is False


# ── the frontend, EXECUTED ──────────────────────────────────────────────────

def _js() -> str:
    return (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")


def _run(body: str, tmp_path) -> dict:
    script = tmp_path / "edit.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = () => ({ tagName:'DIV', innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>null, querySelector:()=>null, querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, focus(){}, select(){},
  classList:{add(){},remove(){},toggle(){},contains:()=>false}, dataset:{} });
globalThis.document = { getElementById:()=>el(), querySelector:()=>null, querySelectorAll:()=>[],
  addEventListener(){}, body:el(), hasFocus:()=>false, activeElement:null };
globalThis.CSS = { escape: s => s };
globalThis.alert = () => {};
const SRC = """ + json.dumps(_js()) + """;
const F = (new Function(SRC + `; return {
  editable, startEdit, commitEdit, isEditingJobs, EDITABLE,
  getEditing: () => EDITING, setEditing: v => { EDITING = v; },
  setJobs: v => { LAST_JOBS = v; }, getJobs: () => LAST_JOBS,
  setRerender: fn => { rerenderJobs = fn; }
};`))();
await new Promise(r => setImmediate(r));
""" + body, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_field_renders_as_double_clickable_and_shows_when_it_is_empty(tmp_path):
    """An empty cell offers no target at all — §Lessons 43, where a control existed and was
    imperceptible. `location` and `salary` are empty on every real row, so this IS the case."""
    out = _run("""
const j = { url:'http://j/1', title:'Engineer', location:'' };
const filled = F.editable(j, 'title', j.title, 'job-title');
const empty  = F.editable(j, 'location', j.location, 'jd-edit');
console.log(JSON.stringify({
  dbl: filled.includes('ondblclick="startEdit('),
  cls: filled.includes('editable'),
  emptyMarked: empty.includes('is-empty'),
  emptyPrompts: empty.includes('Add location'),
}));
""", tmp_path)
    assert out == {"dbl": True, "cls": True, "emptyMarked": True, "emptyPrompts": True}


def test_a_field_the_server_would_refuse_is_not_offered(tmp_path):
    """Two lists that can drift is how a control ends up offering an edit that always fails."""
    out = _run("""
const j = { url:'http://j/1', fit_score:7 };
const h = F.editable(j, 'fit_score', 7, 'x');
console.log(JSON.stringify({ noHandler: !h.includes('startEdit'), editable: F.EDITABLE }));
""", tmp_path)
    assert out["noHandler"] is True
    assert "fit_score" not in out["editable"] and "url" not in out["editable"]


def test_the_whitelists_on_both_sides_match():
    """A field editable in the browser and refused by the server is a box that cannot save."""
    js = _js()
    listed = js[js.index("const EDITABLE = ["):]
    listed = listed[:listed.index("]")]
    for f in ("title", "company", "location", "salary"):
        assert f"'{f}'" in listed
    assert set(repo.EDITABLE_FIELDS) - {"full_description"} == {
        "title", "company", "location", "salary"}


def test_an_open_editor_holds_the_refresh(tmp_path):
    """`startEdit` re-renders and THEN focuses, so for one tick `activeElement` is still the old
    node — a refresh landing in that window replaces the input just opened. The focus guard alone
    does not cover it."""
    out = _run("""
const before = F.isEditingJobs();
F.setEditing({url:'http://j/1', field:'title'});
console.log(JSON.stringify({ before, during: F.isEditingJobs() }));
""", tmp_path)
    assert out == {"before": False, "during": True}


def test_committing_writes_through_and_adopts_the_servers_value(tmp_path):
    """The payload is updated before the request or the cell snaps back for up to 2.5s and the
    edit reads as rejected (§Lessons 21). The SERVER's value wins, because it caps length."""
    out = _run("""
F.setJobs([{ url:'http://j/1', title:'Enginer' }]);
F.setRerender(() => {});
globalThis.fetch = async () => ({ ok:true,
  json: async () => ({ ok:true, changed:['title'], values:{ title:'Engineer' } }) });
const stub = (u,f) => ({ getAttribute: k => (k === 'data-url' ? u : k === 'data-field' ? f : ''), value: 'Engineer  ' });
await F.commitEdit(stub('http://j/1','title'));
console.log(JSON.stringify({ stored: F.getJobs()[0].title, editing: F.getEditing() }));
""", tmp_path)
    assert out["stored"] == "Engineer", "the typed value beat the server's stored one"
    assert out["editing"] is None


def test_a_failed_save_puts_the_old_value_back(tmp_path):
    """A cell that keeps a new value after the write failed is a lie about what is stored."""
    out = _run("""
F.setJobs([{ url:'http://j/1', title:'Enginer' }]);
F.setRerender(() => {});
globalThis.fetch = async () => ({ ok:true, json: async () => ({ ok:false, message:'nope' }) });
await F.commitEdit({ getAttribute: k => (k === 'data-url' ? 'http://j/1' : k === 'data-field' ? 'title' : ''), value: 'Engineer' });
console.log(JSON.stringify({ stored: F.getJobs()[0].title }));
""", tmp_path)
    assert out["stored"] == "Enginer"


def test_the_space_tab_offers_a_rename():
    js = _js()
    assert "renameSpace" in js
    assert 'ondblclick="renameSpace(' in js


# ── the render that OPENS an editor must not be vetoed by the edit guard ────

_TABLE_HARNESS = """
globalThis.WROTE = [];
const el2 = () => ({ tagName:'DIV', innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>null, querySelector:()=>null, querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, focus(){}, select(){},
  classList:{add(){},remove(){},toggle(){},contains:()=>false}, dataset:{} });
const jobsNode = { tagName:'DIV', _h:'', style:{}, dataset:{}, closest(){return this;},
  querySelector:()=>null, querySelectorAll:()=>[], setAttribute(){}, getAttribute:()=>null,
  addEventListener(){}, appendChild(){}, focus(){}, select(){},
  classList:{add(){},remove(){},toggle(){},contains:()=>false},
  get innerHTML(){ return this._h; },
  set innerHTML(v){ this._h = v; globalThis.WROTE.push(v); } };
globalThis.document = { getElementById: id => id === 'jobs' ? jobsNode : el2(),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener(){}, body: el2(), hasFocus: () => false, activeElement: null };
globalThis.CSS = { escape: s => s };
globalThis.alert = () => {};
"""

_JOB_ROW = """{ url:'http://j/1', title:'Enginer', company:'Acme', description:'d',
  status:'imported', site:'GH', strategy:'dashboard_upload', contacts:[], followups:{},
  shape:'pipeline/jobs' }"""


def _table(body: str, tmp_path) -> dict:
    script = tmp_path / "table.mjs"
    script.write_text(
        BROWSER_GLOBALS + _TABLE_HARNESS + """
const SRC = """ + json.dumps(_js()) + """;
const F = (new Function(SRC + `; return {
  startEdit, editDesc, renderJobsTable, isEditingJobs, rerenderJobs, jobDetail,
  setJobs: v => { LAST_JOBS = v; },
  openPanel: u => { PANEL_OPEN.add(u); TAB_OPEN.set(u, 'job'); },
  writes: () => globalThis.WROTE, reset: () => { globalThis.WROTE = []; }
};`))();
await new Promise(r => setImmediate(r));
F.reset();
""" + body, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_double_clicking_actually_puts_an_input_on_the_page(tmp_path):
    """Reported as "I click on job name and nothing happens", and nothing did.

    The handler fired and `EDITING` was set correctly — but `isEditingJobs()` returns true
    BECAUSE `EDITING` is set, and `renderJobsTable` bails on that guard, so the <input> was never
    written. The guard exists to stop the 2.5s TIMER destroying an open editor; applied to the
    deliberate render that OPENS one it does the exact opposite.

    Every other test in this file called `editable()` or `commitEdit()` directly and passed
    throughout — §Lessons 48: none of them drove the render, so none of them could see it.
    """
    out = _table("""
const j = %s;
F.setJobs([j]);
F.renderJobsTable([j], false);
const opening = globalThis.WROTE.length;
F.startEdit('http://j/1', 'title');
const last = globalThis.WROTE[globalThis.WROTE.length - 1] || '';
console.log(JSON.stringify({
  wroteOnOpen: globalThis.WROTE.length > opening,
  hasInput: /class="cell-edit/.test(last),
  guardStillTrue: F.isEditingJobs(),
}));
""" % _JOB_ROW, tmp_path)
    assert out["wroteOnOpen"] is True, "startEdit re-rendered nothing — the editor never appears"
    assert out["hasInput"] is True
    # The guard must still be ON afterwards, or the next tick eats the input it just opened.
    assert out["guardStillTrue"] is True


def test_the_timer_still_cannot_destroy_an_open_editor(tmp_path):
    """Guard the guard. Forcing every render would make the editor openable and then instantly
    disposable — the bug this whole guard exists for."""
    out = _table("""
const j = %s;
F.setJobs([j]);
F.renderJobsTable([j], false);
F.startEdit('http://j/1', 'title');
const after = globalThis.WROTE.length;
// Every path that is NOT a deliberate open. `rerenderJobs()` with no argument is what the flag
// toggle, the conversation expander and the tag filter all call — mutating it to force
// unconditionally has to fail here, or "forced always" passes and the editor is disposable.
// The payload CHANGES, which is the only case that can actually destroy the editor: with
// identical data the skip-if-unchanged guard makes every render a no-op and hides the bug.
const fresh = JSON.parse(JSON.stringify(j)); fresh.title = 'Something the poller brought';
F.setJobs([fresh]);
F.rerenderJobs();                        // what a flag toggle or the poller reaches
F.renderJobsTable([fresh], F.isEditingJobs());
F.renderJobsTable([fresh], false);       // even with a STALE "not editing" flag
console.log(JSON.stringify({ writesFromTimer: globalThis.WROTE.length - after }));
""" % _JOB_ROW, tmp_path)
    assert out["writesFromTimer"] == 0, "the refresh overwrote an open editor"


def test_an_existing_description_can_be_put_into_edit_mode(tmp_path):
    """The paste box renders only when the description is EMPTY, so a job that scraped fine had
    no way to correct one — the other half of "I cannot edit the description"."""
    out = _table("""
const j = %s;
F.setJobs([j]);
F.openPanel('http://j/1');
const closed = F.jobDetail(j);
F.editDesc('http://j/1', true);
const open = F.jobDetail(j);
console.log(JSON.stringify({
  rendered: open !== closed,
  hasTextarea: /jd-paste-box/.test(open),
  hasSave: /saveJobDescription/.test(open),
  guard: F.isEditingJobs(),
}));
""" % _JOB_ROW, tmp_path)
    assert out["rendered"] is True and out["hasTextarea"] is True and out["hasSave"] is True
    assert out["guard"] is True, "an open description editor does not hold the refresh"


def test_a_description_that_exists_still_offers_the_edit_button(tmp_path):
    out = _table("""
const j = %s;
F.setJobs([j]);
F.openPanel('http://j/1');
const h = F.jobDetail(j);
// A real, VISIBLE button. Matching `editDesc(` alone passed against a `hidden` one — the
// §Lessons 88 shape, where the markup is present and the control is not.
const m = h.match(/<button[^>]*editDesc\\([^>]*>/);
console.log(JSON.stringify({
  offered: !!m,
  visible: !!m && !/\\bhidden\\b/.test(m[0]),
  isButton: !!m && /class="linklike"/.test(m[0]),
}));
""" % _JOB_ROW, tmp_path)
    assert out["offered"] is True
    assert out["visible"] is True, "the edit control is present in the markup and hidden"
    assert out["isButton"] is True
