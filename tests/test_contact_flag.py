"""💡 — the operator marking a person they care about.

Every other signal on a contact row is DERIVED: replied, due, exhausted, opened the deck. They
say what the system observed. This one says what the human decided, and nothing else in the app
can express that — which is why "they're all just people at this company" was the state before it.

Three things carry it, each one a bug this repo has already paid for:

**A timestamp, not a boolean.** `NULL` is a complete answer and the value records when, the same
shape as `replied_at` and `deck_viewed_at`. A `0/1` throws that away for nothing.

**The additive dict, never a migration.** `get_connection()` does not call `init_db`, so
`ensure_contacts_columns` can run first; a migration touching a column declared there is a
duplicate-column error one way round and a missing-column error the other (§Spaces).

**The BROWSER decides the state, the server stores what it is told.** A server-side toggle races
the 2.5s refresh: two clicks either side of a re-render leave the button and the database
disagreeing, with nothing to say which is right.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from browser_stubs import BROWSER_GLOBALS

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.networking import store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    store.upsert_contact({"id": "c1", "job_url": "http://j/1", "full_name": "Sam",
                          "email": "sam@x.test", "company": "Acme"}, conn)
    return conn


# ── storage ─────────────────────────────────────────────────────────────────

def test_the_column_arrives_with_the_table_not_a_migration(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(contacts)")]
    assert "flagged_at" in cols
    assert "flagged_at" in store._CONTACT_COLUMNS, (
        "declared somewhere other than the additive dict — it will race ensure_contacts_columns")


def test_flagging_and_unflagging(db):
    assert store.set_flagged("c1", True, db) is True
    assert store.set_flagged("c1", False, db) is False


def test_it_stores_WHEN(db):
    """A boolean would answer "is this flagged". The column also answers "since when", which is
    the question anything later built on this will ask first."""
    store.set_flagged("c1", True, db)
    at = db.execute("SELECT flagged_at FROM contacts WHERE id='c1'").fetchone()[0]
    assert at and at.startswith("20")


def test_unflagging_clears_the_timestamp(db):
    """Not "sets it to a falsey string". `flagged_at` must be NULL, or every future query has to
    know which empty value this particular writer chose."""
    store.set_flagged("c1", True, db)
    store.set_flagged("c1", False, db)
    assert db.execute("SELECT flagged_at FROM contacts WHERE id='c1'").fetchone()[0] is None


def test_the_state_is_read_BACK_not_echoed(db):
    """A setter returning its own argument reports success for a write that never landed, and the
    button then paints a state nothing stored."""
    assert store.set_flagged("nobody-by-that-id", True, db) is None


def test_an_unknown_contact_is_distinguishable_from_an_unflagged_one(db):
    """`None` vs `False`. Collapsing them makes "that person is gone" render as "not flagged",
    which looks like the button silently doing nothing."""
    assert store.set_flagged("ghost", True, db) is None
    assert store.set_flagged("c1", False, db) is False


def test_flagging_does_not_re_key_the_contact(db):
    """`contact_id` hashes (job_url, linkedin_url, name). If this ever entered the identity,
    flagging somebody would mint a new id and detach their touches, sequences and messages —
    the same reason `space_id` stays out of the hash."""
    before = store.contact_id("http://j/1", None, "Sam")
    store.set_flagged("c1", True, db)
    assert store.contact_id("http://j/1", None, "Sam") == before
    assert db.execute("SELECT count(*) FROM contacts WHERE id='c1'").fetchone()[0] == 1


def test_it_does_not_disturb_the_rest_of_the_row(db):
    store.set_flagged("c1", True, db)
    r = db.execute("SELECT full_name, email, company FROM contacts WHERE id='c1'").fetchone()
    assert tuple(r) == ("Sam", "sam@x.test", "Acme")


# ── the endpoint ────────────────────────────────────────────────────────────

def test_the_endpoint_flags_and_unflags(db):
    assert wd._flag_contact({"contact_id": "c1", "on": 1})["flagged"] is True
    assert wd._flag_contact({"contact_id": "c1", "on": ""})["flagged"] is False


def test_the_endpoint_refuses_an_empty_id(db):
    assert wd._flag_contact({"contact_id": "", "on": 1})["ok"] is False


def test_the_endpoint_refuses_an_unknown_contact(db):
    """Silently succeeding would leave the button lit against a row that does not exist."""
    res = wd._flag_contact({"contact_id": "ghost", "on": 1})
    assert res["ok"] is False and "not found" in res["message"]


def test_the_endpoint_takes_the_state_rather_than_toggling(db):
    """The race this avoids: the browser knows what it just showed, the server does not. Sending
    the same state twice must be idempotent, which a toggle is not."""
    wd._flag_contact({"contact_id": "c1", "on": 1})
    assert wd._flag_contact({"contact_id": "c1", "on": 1})["flagged"] is True


def test_the_route_is_wired():
    import inspect
    src = inspect.getsource(wd)
    assert '"/api/contact/flag"' in src
    assert "_flag_contact(data)" in src


def test_the_payload_carries_it():
    import inspect
    assert '"flagged"' in inspect.getsource(wd)


# ── the control ─────────────────────────────────────────────────────────────

def _js() -> str:
    return (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")


def test_it_is_a_real_button():
    """§Lessons 88: the attachment toggle shipped as a `<span>` styled like a button, and the
    first thing it received was a click and a bug report. A fake control is worse than none —
    it spends the one click you get."""
    js = _js()
    row = js[js.index("function contactRow(c)"):js.index("async function toggleFlag(")]
    assert '<button class="flagbtn' in row
    assert 'aria-pressed=' in row


def test_it_sits_on_the_collapsed_row():
    """A marker you must expand a contact to see is a marker nobody sees. Same argument that put
    the reply pill on the collapsed row (§Lessons 27)."""
    js = _js()
    row = js[js.index("function contactRow(c)"):js.index("async function toggleFlag(")]
    prow = row[row.index('<div class="prow'):]
    assert "${flag}" in prow[:260], "the button is not on the collapsed row"


def test_pressing_it_does_not_open_the_contact():
    """The whole row is a click target. Without this the flag also expands the panel, which reads
    as the button doing the wrong thing."""
    js = _js()
    row = js[js.index("function contactRow(c)"):js.index("async function toggleFlag(")]
    assert "event.stopPropagation();toggleFlag(" in row


def test_the_unflagged_state_is_visible():
    """Rendered as ○, not as nothing. A control that only appears on hover is one nobody knows
    exists — §Lessons 43, seven occurrences."""
    js = _js()
    row = js[js.index("function contactRow(c)"):js.index("async function toggleFlag(")]
    assert "'💡' : '○'" in row


def test_the_whole_row_is_accented_not_just_the_icon():
    css = (wd._STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    assert ".prow.is-flagged" in css
    js = _js()
    row = js[js.index("function contactRow(c)"):js.index("async function toggleFlag(")]
    assert "is-flagged" in row


# ── it is EXECUTED, not grepped ─────────────────────────────────────────────

def _run(body, tmp_path):
    src = _js()
    script = tmp_path / "flag.mjs"
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
const SRC = """ + json.dumps(src) + """;
const F = (new Function(SRC + '; return { contactRow, toggleFlag };'))();
""" + body, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_row_renders_both_states(tmp_path):
    out = _run("""
const base = { id:'c1', full_name:'Sam', title:'Recruiter', email:'s@x.test' };
const off = F.contactRow({ ...base, flagged:false });
const on  = F.contactRow({ ...base, flagged:true  });
console.log(JSON.stringify({
  offLit: off.includes('flagbtn on'), onLit: on.includes('flagbtn on'),
  offRow: off.includes('is-flagged'), onRow: on.includes('is-flagged'),
}));
""", tmp_path)
    assert out["onLit"] is True and out["onRow"] is True
    # Guard the guard: a version that always lights satisfies the two above and marks everybody.
    assert out["offLit"] is False and out["offRow"] is False


#: Stubbed at FETCH, not at `post`. The first version replaced `globalThis.post`, which the
#: bundle's own top-level `post` shadows inside the `new Function` scope — so the stub was never
#: called, the real one tried a network request, and both tests failed for a reason that had
#: nothing to do with the flag. Stub the seam the code actually reaches.
_BTN = """
const btn = { classList: { _on:false,
    contains(){ return this._on; }, toggle(c, v){ this._on = v; }, add(){}, remove(){} },
  textContent:'○', setAttribute(){}, closest: () => ({ classList:{ toggle(){} } }) };
"""


def test_the_paint_is_reverted_when_the_write_fails(tmp_path):
    """Optimistic, because a marker that waits up to 2.5s to appear reads as a dead control and
    gets clicked again. An optimistic paint that is never undone is just a lie with better
    timing — the operator would believe a flag that no restart will bring back."""
    out = _run("""
let sent = null;
globalThis.fetch = async (path, opts) => { sent = { path, body: JSON.parse(opts.body) };
  return { ok:true, json: async () => ({ ok:false, message:'nope' }) }; };
""" + _BTN + """
await F.toggleFlag('c1', btn);
console.log(JSON.stringify({ path: sent.path, asked: !!sent.body.on,
                             cid: sent.body.contact_id, litAfterFailure: btn.classList._on }));
""", tmp_path)
    assert out["path"] == "/api/contact/flag"
    assert out["asked"] is True
    assert out["cid"] == "c1"
    assert out["litAfterFailure"] is False, "a failed write left the flag lit"


def test_a_successful_write_leaves_it_lit(tmp_path):
    out = _run("""
globalThis.fetch = async () => ({ ok:true, json: async () => ({ ok:true, flagged:true }) });
""" + _BTN + """
await F.toggleFlag('c1', btn);
console.log(JSON.stringify({ lit: btn.classList._on }));
""", tmp_path)
    assert out["lit"] is True
