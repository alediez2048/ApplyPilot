"""SPACE-2: the panel is scoped to one Space, and says so when it could not be.

Two failure modes are worth more than the feature. The first is a `?space=` that does not
resolve rendering an empty table — indistinguishable from a Space with nothing in it, which is
§Lessons 15 and the reason `network_note` exists three tabs away. The second is a filter applied
to the rows but not the counts, so the strip above the table describes a different working set
than the table below it.

The frontend half runs the served script under the same Node stubs the other render tests use.
That matters here more than usual: the Space id is parsed at MODULE LOAD, and a throw there
blanks the entire dashboard exactly as a syntax error would (§Lessons 7). It did, on the first
run — 47 tests at once — because five of six stub files defined `location` only on `window`.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.networking import store
from applypilot.repo import spaces

from browser_stubs import BROWSER_GLOBALS


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    spaces.create_space("partnerships", "Partnerships", "outreach", conn=conn)
    for url, space in (("http://j/1", "job-search"), ("http://j/2", "job-search"),
                       ("target:partnerships:acme", "partnerships")):
        conn.execute("INSERT INTO jobs (url, title, site, strategy, space_id, "
                     "full_description, tailored_resume_path, discovered_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (url, url.split("/")[-1], "Greenhouse", "dashboard_upload", space,
                      "a posting with real words in it", "/tmp/r.pdf",
                      "2026-08-01T10:00:00+00:00"))
    conn.commit()
    return conn


def _urls(payload) -> set:
    return {j["url"] for j in payload["jobs"]}


# ── scoping ─────────────────────────────────────────────────────────────────

def test_the_panel_shows_only_the_requested_space(db):
    assert _urls(wd._status_payload("job-search")) == {"http://j/1", "http://j/2"}
    assert _urls(wd._status_payload("partnerships")) == {"target:partnerships:acme"}


def test_the_counts_are_scoped_with_the_rows(db):
    """A strip describing a different working set than the table under it is worse than none.

    The filter is two call sites — `dashboard_rows` and `queue_stats` — and applying it to one
    is §Lessons 49, which in this case would put "3 URL Jobs" above a two-row table.
    """
    p = wd._status_payload("job-search")
    assert p["stats"]["total"] == 2 == len(p["jobs"])
    q = wd._status_payload("partnerships")
    assert q["stats"]["total"] == 1 == len(q["jobs"])


def test_search_text_is_scoped_too(db):
    """UX-6's corpus follows the panel.

    Search reaching across Spaces returns rows the table cannot show, and a hit you cannot click
    is worse than no hit.
    """
    got = wd._all_job_descriptions({"space": "job-search"})["descriptions"]
    assert set(got) == {"http://j/1", "http://j/2"}


def test_lifetime_stats_stay_global(db):
    """Deliberately NOT scoped. "Lifetime Applied" answers a question about the operator, not
    about a panel, and silently rescoping it would make the number drop when you switch tabs."""
    assert wd._status_payload("partnerships")["stats"]["lifetime_total"] == 3


# ── the request that cannot be honoured ─────────────────────────────────────

def test_an_unknown_space_falls_back_AND_says_why(db):
    """§Lessons 15: a zero result must be as loud as an error.

    An empty table for a typo'd `?space=` looks exactly like a Space nobody has added anything
    to yet, and the operator has no way to tell which they are looking at.
    """
    p = wd._status_payload("partnerhsips")            # transposed, as a human would type it
    assert p["space"] == "job-search"
    assert "partnerhsips" in p["space_note"]
    assert p["space_note"], "fell back silently"
    assert _urls(p) == {"http://j/1", "http://j/2"}


def test_asking_for_nothing_is_not_an_error(db):
    """No `?space=` is the ordinary first load, not a failed request — no note."""
    p = wd._status_payload("")
    assert p["space"] == "job-search"
    assert p["space_note"] == ""


def test_the_nav_list_rides_on_the_payload(db):
    p = wd._status_payload("job-search")
    assert [s["id"] for s in p["spaces"]] == ["job-search", "partnerships"]
    assert p["spaces"][1]["shape"] == spaces.TARGETS_SHAPE


def test_a_database_with_no_registry_filters_nothing(tmp_path, monkeypatch):
    """The pre-Spaces meaning, preserved.

    `_resolve_space` returning `""` is "show everything", which is what every caller written
    before Spaces meant. Unlike the pipeline queues, this question has a safe empty answer —
    which is why they raise and this does not.
    """
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    conn.row_factory = sqlite3.Row
    assert wd._resolve_space("anything", conn) == ("", [], "")


# ── the browser half ────────────────────────────────────────────────────────

_DRIVER = """
const F = (new Function(SRC + `; return { renderSpaceNav, statusUrl, switchSpace,
  JOB_DESC, get SPACE_ID(){ return SPACE_ID; }, get LAST_JOBS(){ return LAST_JOBS; },
  get JOB_DESC_LOADED(){ return JOB_DESC_LOADED; } };`))();
const out = {};
const nav = document.getElementById('spaceNav');
const note = document.getElementById('spaceNote');

F.renderSpaceNav([{id:'job-search',name:'Job Search'}], 'job-search', '');
out.oneSpaceHidden = nav.hidden;
out.oneSpaceHtml = nav.innerHTML;

F.renderSpaceNav([{id:'job-search',name:'Job Search'},{id:'partnerships',name:'Partnerships'}],
                 'partnerships', '');
out.twoSpacesHidden = nav.hidden;
out.navHtml = nav.innerHTML;

F.renderSpaceNav([{id:'job-search',name:'Job Search'}], 'job-search', 'No Space called x.');
out.noteHidden = note.hidden;
out.noteText = note.textContent;

out.urlBefore = F.statusUrl();
F.JOB_DESC.set('http://j/1', 'text from the other space');
F.switchSpace('partnerships');
out.urlAfter = F.statusUrl();
out.descCleared = F.JOB_DESC.size === 0;
out.jobsCleared = F.LAST_JOBS.length === 0;
out.descReloadArmed = F.JOB_DESC_LOADED === false;
console.log(JSON.stringify(out));
"""


def _run_js(tmp_path, driver: str) -> dict:
    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "nav.mjs"
    script.write_text(
        BROWSER_GLOBALS
        # Two real nodes, because the nav's whole job is to be visible: an `el()` stub that
        # accepts every write would pass with `hidden` never set (§Lessons 41 — asserting on
        # copy rather than on the control existing is what shipped a tab showing nothing but
        # the right words).
        + """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, removeAttribute(){}, focus(){},
  scrollIntoView(){}, classList:{toggle(){},add(){},remove(){}},
  addEventListener(){}, appendChild(){}, dataset:{} });
// REAL nodes for the two this file asserts on. An `el()` that swallows every write would let
// `hidden` never be set and still pass — §Lessons 41, where a render test asserted on the copy
// of an empty state and happily passed for a tab that showed nothing but the right words.
const NODES = { spaceNav: el(), spaceNote: el() };
globalThis.document = { getElementById: (id) => NODES[id] || el(),
  querySelectorAll: ()=>[], querySelector: el, addEventListener(){},
  activeElement:null, body: el(), hasFocus: () => false };
const SRC = """ + json.dumps(src) + ";\n" + driver,
        encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr[:2000]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_the_nav_is_shown_from_one_space_up_because_the_plus_lives_there(tmp_path):
    """Revised by SPACE-3b. The first version hid the strip below two Spaces.

    A lone tab IS furniture — that reasoning was right — but the + button sits beside it, and
    hiding both meant a fresh install had one Space and nowhere to create another. §Lessons 43
    with a twist: the control could not be found because it was inside something that hid
    itself. With one Space the tab renders as a LABEL rather than a button, so nothing on
    screen offers a switch that does not exist.
    """
    out = _run_js(tmp_path, _DRIVER)
    assert out["oneSpaceHidden"] is False, "one Space meant no + button and no way to add one"
    assert "space-add" in out["oneSpaceHtml"], "the nav rendered without the + button"
    assert "switchSpace" not in out["oneSpaceHtml"], \
        "a lone Space offered a switch to itself"
    assert out["twoSpacesHidden"] is False, "the nav never appeared with two Spaces"
    assert "space-add" in out["navHtml"], "the + button vanished once there were two Spaces"
    assert "Partnerships" in out["navHtml"]
    assert "space-tab on" in out["navHtml"], "no tab is marked current"
    assert "switchSpace(&#39;partnerships&#39;)" in out["navHtml"] \
        or "switchSpace('partnerships')" in out["navHtml"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_the_note_shows_even_with_one_space(tmp_path):
    """One Space is exactly when a bad `?space=` is least explicable, so the note is not
    rendered inside the nav that hides itself."""
    out = _run_js(tmp_path, _DRIVER)
    assert out["noteHidden"] is False
    assert "No Space called x." in out["noteText"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_switching_carries_the_space_and_drops_the_old_caches(tmp_path):
    """A Space is a different working set, not a filter over the one already loaded.

    `JOB_DESC` is the ~130KB search corpus fetched ONCE PER SESSION (UX-6). Once per session was
    right with one Space and is a cross-Space leak now — the previous Space's postings would stay
    searchable, and clicking a hit would find no row.
    """
    out = _run_js(tmp_path, _DRIVER)
    assert out["urlBefore"] == "/api/status"
    assert out["urlAfter"] == "/api/status?space=partnerships"
    assert out["descCleared"], "the previous Space's search corpus survived the switch"
    assert out["jobsCleared"], "LAST_JOBS still holds the previous Space's rows"
    assert out["descReloadArmed"], "JOB_DESC_LOADED stayed true, so the new Space never loads"


_RACE_DRIVER = """
const F = (new Function(SRC + `; return { refresh, switchSpace, statusUrl,
  get SPACE_ID(){ return SPACE_ID; } };`))();

// Two responses in flight, resolving out of order — the real shape of a 2.5s poller plus a
// click. The FIRST request is for the Space we are leaving and comes back LAST.
let release;
const slow = new Promise(r => { release = r; });
let n = 0;
globalThis.fetch = async (url) => {
  n++;
  if (n === 1) { await slow; return { json: async () => ({space:'job-search', jobs:[], stats:{}}) }; }
  return { json: async () => ({space:'partnerships', jobs:[], stats:{}}) };
};

const first = F.refresh();          // the poller's tick, for job-search
F.switchSpace('partnerships');      // the operator clicks, mid-flight
release();                          // ...and the stale response lands afterwards
await first;
await new Promise(r => setImmediate(r));
console.log(JSON.stringify({ spaceId: F.SPACE_ID, url: F.statusUrl() }));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_a_stale_response_cannot_drag_the_panel_back(tmp_path):
    """The bug this shipped with, found by clicking the tab rather than by any test.

    `refresh()` adopts the server's `space` because the server may have fallen back from an id
    that does not resolve. A response already in flight for the PREVIOUS Space carries the
    previous id, so adopting it undid the switch — and undid it permanently, because
    `statusUrl()` then went on asking for the old Space. The URL said one thing and every number
    on screen said another.
    """
    out = _run_js(tmp_path, _RACE_DRIVER)
    assert out["spaceId"] == "partnerships", \
        "a stale response dragged the panel back to the Space we just left"
    assert out["url"] == "/api/status?space=partnerships"


def test_every_status_read_goes_through_statusUrl():
    """§Lessons 49: a rule applied to three of four call sites is not applied.

    The one that would get missed is the apply guard, which reads `stats.ready` to decide
    whether to launch — so a missed site launches an apply sized by another Space's counts.
    """
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    assert "fetch('/api/status')" not in js, "a bare /api/status fetch is back"
    assert js.count("fetch(statusUrl())") >= 4


# ── the Outcomes panel ──────────────────────────────────────────────────────

def test_outcomes_are_scoped_to_the_campaign(db):
    """Reported by the operator: a Partnerships Space showed the job search's reply rate.

    The funnel above it was already scoped — it is built from `dashboard_rows` — while the
    rates underneath came from every contact in the database. Two halves of one panel
    describing two different campaigns, which is worse than either being wrong: the numbers
    look consistent because they are both real.
    """
    from applypilot.networking import store
    store.upsert_contact({"job_url": "http://j/1", "space_id": "job-search",
                          "full_name": "Jo Blue", "email": "jo@x.test",
                          "sent_message_id": "gid1", "submitted_at": "2026-08-01T10:00:00+00:00",
                          "replied_at": "2026-08-02T10:00:00+00:00"}, db)
    store.upsert_contact({"job_url": "target:partnerships:acme", "space_id": "partnerships",
                          "full_name": "Ada Green", "email": "ada@y.test",
                          "sent_message_id": "gid2",
                          "submitted_at": "2026-08-01T10:00:00+00:00"}, db)

    # A SENT touch on the job-search contact. Without one the touch filter is untested — a
    # mutation removing it passed, because filtering an empty list changes nothing.
    from applypilot.networking import touches
    touches.init_touches(db)
    jo = [c for c in store.all_contacts_for_metrics(db, space_id="job-search")][0]["id"]
    touches.record_sent(jo, "email", conn=db)
    assert touches.all_sent_touches(db), "no touch recorded — the assertion below is vacuous"

    jobs = wd._status_payload("job-search")["metrics"]
    partners = wd._status_payload("partnerships")["metrics"]
    assert jobs and partners, "the panel returned nothing — the assertions below prove nothing"
    assert jobs != partners, "both Spaces reported the same numbers"

    # The job search has the only reply; Partnerships has none. If the scoping is dropped,
    # Partnerships inherits that reply and reads as a campaign that is working.
    assert json.dumps(partners).count('"replied": 1') == 0
    assert '"replied": 1' in json.dumps(jobs)

    # The touch aggregate follows the CONTACTS, structurally: `by_touch` buckets contacts and
    # looks each one's touches up by id, so scoping the contact list is what scopes it. There
    # is deliberately no filter on the touch list itself — one was written, and a mutation
    # deleting it changed nothing, which is what proved it was doing nothing.
    labels = {r["label"] for r in jobs.get("by_touch") or []}
    assert "+1 follow-up" in labels, f"the job search lost its follow-up bucket: {labels}"
    assert "+1 follow-up" not in {r["label"] for r in partners.get("by_touch") or []}, \
        "Partnerships inherited the job search's follow-up history"


def test_a_contact_in_another_space_does_not_count_here(db):
    """The narrower half: one contact, counted once, in one campaign."""
    from applypilot.networking import store
    store.upsert_contact({"job_url": "target:partnerships:acme", "space_id": "partnerships",
                          "full_name": "Ada Green", "email": "ada@y.test"}, db)
    from applypilot.networking import store as st
    assert len(st.all_contacts_for_metrics(db, space_id="partnerships")) == 1
    assert len(st.all_contacts_for_metrics(db, space_id="job-search")) == 0
    assert len(st.all_contacts_for_metrics(db)) == 1, "no space_id must still mean everything"


# ── creating a Space ────────────────────────────────────────────────────────

def test_the_plus_button_creates_a_space_from_a_template(db):
    r = wd._create_space({"name": "Contract work", "template": "outreach"})
    assert r["ok"] and r["id"] == "contract-work"
    made = spaces.load("contract-work", db)
    assert made.shape == spaces.TARGETS_SHAPE and made.terminal == "booked"
    assert made.tailor_docs is False


def test_a_new_space_starts_empty(db):
    """The whole point of a fresh template: no rows, no contacts, no inherited outcomes."""
    wd._create_space({"name": "Contract work", "template": "outreach"})
    p = wd._status_payload("contract-work")
    assert p["jobs"] == [] and p["stats"]["total"] == 0
    assert (p["metrics"].get("overall") or {}).get("n", 0) == 0
    assert p["space_offer"] == ""


def test_business_is_refused_and_says_why(db):
    """Not a generic rejection. The operator asked for three templates and needs to know this
    one is waiting on a mailbox rather than broken."""
    r = wd._create_space({"name": "Acme", "template": "business"})
    assert r["ok"] is False
    assert "mailbox" in r["message"] and "ID-1" in r["message"]
    assert spaces.get_space("acme", db) is None


def test_a_clashing_id_is_refused_not_disambiguated(db):
    """`partnerships-2` would be a permanent key that does not match its name, chosen silently.

    Unlike a deck slug there is nothing already in the world to preserve, so refusing costs
    nothing and guessing costs forever (§13.2).
    """
    r = wd._create_space({"name": "Partnerships", "template": "outreach"})
    assert r["ok"] is False and "already exists" in r["message"]
    assert len(spaces.all_spaces(conn=db)) == 2


@pytest.mark.parametrize("name", ["", "   ", "!!!", "、、"])
def test_a_name_with_no_id_in_it_is_refused(db, name):
    assert wd._create_space({"name": name, "template": "outreach"})["ok"] is False


def test_the_picker_is_described_by_the_module_that_builds_the_templates(db):
    """So the menu cannot describe a template differently from what it actually produces."""
    from applypilot.domain import space as sp
    offered = wd._status_payload("job-search")["space_templates"]
    assert [t["id"] for t in offered] == list(sp.OFFERED_TEMPLATES)
    assert "business" not in [t["id"] for t in offered]
    for t in offered:
        assert t["blurb"] and t["name"]
