"""CO-2 phase 2 — the button, the dialog, and the endpoints behind them.

Placement is the whole risk here, and it has cost this project ten reports. §Lessons 43 is "a
control nobody can find", §Lessons 88 its inverse (a `<span>` that looks like a button), and
§Lessons 89/97 the third form: findable is not the same as findable FROM WHERE THE WORK IS. So
this asserts WHERE the control renders, not only that the markup exists somewhere.

The server half is tested from what `_status_payload`-style handlers actually produce rather
than from a fixture written to match the frontend — a fixture you wrote to match your code
proves only that your code matches your fixture (§Lessons 103, where `m.at` and `sent_at`
agreed with each other and the live separators rendered no date at all).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import applypilot.database as database
from applypilot.networking import messages as _msg, store
from applypilot.repo import jobs as _jobs

DEAD = "https://boards.example.com/google/startups-performance-lead"
LIVE = "https://boards.example.com/google/ai-sales-specialist"


# ── the browser half ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def node():
    if not shutil.which("node"):
        pytest.skip("node not available")


def _run(tmp_path, tail):
    import applypilot.web_dashboard as wd
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


CLOSED = {"url": DEAD, "title": "Startups Performance Lead", "company": "Google",
          "status": "cancelled", "apply_status": "cancelled",
          "contacts": [{"id": "c1", "full_name": "Patrick", "hot": False}]}


def _people(tmp_path, job, setup=""):
    """The People tab as the operator sees it — through `peopleList`, not through `migrateBar`.

    Calling the renderer directly would prove the bar can be built and nothing about whether it
    lands on the page: fourteen tests of the employer-banding functions all passed against a
    version that banded the wrong rows, because none of them drove `renderJobsTable`
    (§Lessons 94/100).
    """
    return _run(tmp_path, f"const J = {json.dumps(job)};\n"
                "const F = (new Function(SRC + '; return { peopleList, migState, MIGRATE_UNDO,"
                " setUndo: v => { MIGRATE_UNDO = v; } };'))();\n"
                + setup +
                "console.log(JSON.stringify({html: F.peopleList(J)}));\n")["html"]


def test_the_button_is_on_the_PEOPLE_tab_of_the_closed_job(tmp_path):
    """Where the contacts are. The row menu was the obvious home and is the wrong one: it is
    not a destructive action, and burying one is how the interview button was reported three
    times as doing nothing."""
    html = _people(tmp_path, CLOSED)
    assert "Move contacts to another application" in html
    assert "toggleMigrate" in html
    assert html.index("mig-row") < html.index("contact"), \
        "the mover renders below the people it moves"


def test_it_is_a_real_BUTTON(tmp_path):
    """§Lessons 88: a `<span>` styled like a button spends the one click you get. A missing
    control is merely absent; a fake one is a promise the page does not keep."""
    html = _people(tmp_path, CLOSED)
    i = html.index("Move contacts to another application")
    assert "<button" in html[max(0, i - 300):i]


def test_a_LIVE_role_is_not_offered_the_button(tmp_path):
    """The situation does not exist: nobody is stranded on an application still running, and a
    control on every card is noise that trains you to stop seeing it."""
    assert "Move contacts to another application" not in _people(
        tmp_path, dict(CLOSED, status="applied", apply_status="applied"))


def test_a_closed_role_with_NOBODY_on_it_is_not_offered_it_either(tmp_path):
    assert "Move contacts to another application" not in _people(
        tmp_path, dict(CLOSED, contacts=[]))


def test_every_closed_state_offers_it_not_just_rejected(tmp_path):
    """`cancelled` and `ghost` are the states this feature is actually for — a req pulled or a
    listing that was never real. Naming only 'rejected' is the miss that was live for the whole
    life of `cancelled` (§Lessons 93)."""
    for status in ("rejected", "cancelled", "ghost"):
        html = _people(tmp_path, dict(CLOSED, status=status, apply_status=status))
        assert "Move contacts to another application" in html, status


def test_the_undo_renders_on_the_job_it_was_run_FROM(tmp_path):
    """Not in a global console two screens away — that is exactly what got the bulk follow-up
    bar reported three times as not existing."""
    setup = ("F.setUndo({token:'t1', moved:11, title:'AI Sales Specialist', backup:'b.db',"
             f" src:{json.dumps(DEAD)}}});\n")
    html = _people(tmp_path, CLOSED, setup)
    assert "Moved 11 people" in html and "undoMigrate" in html


def test_the_undo_does_NOT_render_on_a_different_job(tmp_path):
    setup = ("F.setUndo({token:'t1', moved:11, title:'x', backup:'', src:'http://j/other'});\n")
    assert "undoMigrate" not in _people(tmp_path, CLOSED, setup)


def test_the_undo_survives_moving_EVERYBODY(tmp_path):
    """Taking every contact lands on `peopleList`'s empty branch, which is precisely the moment
    the operator is most likely to want the move back."""
    setup = ("F.setUndo({token:'t1', moved:11, title:'x', backup:'',"
             f" src:{json.dumps(DEAD)}}});\n")
    assert "undoMigrate" in _people(tmp_path, dict(CLOSED, contacts=[]), setup)


# NOTE: there is deliberately no test here that the six `onclick` handlers resolve at global
# scope. `test_dashboard_static.py::test_every_inline_handler_resolves_at_global_scope` already
# scans EVERY inline handler in the file and has a negative control proving it can fail — a
# hand-listed copy would be a second, weaker source of truth that goes stale the moment a
# seventh handler is added. The first draft of it here was also wrong (`typeof` applied twice,
# so it compared "string" to "function"), which is the argument in miniature.


# ── the server half ─────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    _msg.init_messages(conn)
    _jobs.insert_imported(DEAD, "Startups Performance Lead", "Google", "Google", DEAD, conn)
    _jobs.insert_imported(LIVE, "AI Sales Specialist", "Google", "Google", LIVE, conn)
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: conn)
    return conn


def test_the_plan_endpoint_answers_with_targets_before_a_destination_is_chosen(db):
    from applypilot import web_dashboard as wd
    got = wd._migrate_plan({"src": DEAD})
    assert got["ok"] and [t["url"] for t in got["targets"]] == [LIVE]
    assert got["plan"] is None, "a preview was computed before anywhere was chosen"


def test_no_destination_anywhere_says_WHICH_condition_is_unmet(db):
    """Not silence and not a failure: "same employer, still open" is a rule the operator can
    act on, and §Lessons 15 is what a truthful zero that explains nothing costs."""
    db.execute("UPDATE jobs SET rejected_at = '2026-08-01', apply_status = 'ghost' "
               "WHERE url = ?", (LIVE,))
    db.commit()
    from applypilot import web_dashboard as wd
    got = wd._migrate_plan({"src": DEAD})
    assert got["ok"] is False and "no other open application" in got["error"]


def test_the_move_endpoint_logs_on_BOTH_jobs(db):
    """The old card is where "where did those people go" gets asked; the new one is where "why
    is there a conversation here I did not start" gets asked."""
    from applypilot import web_dashboard as wd
    from applypilot.database import get_job_events
    cid = store.upsert_contact({"job_url": DEAD, "full_name": "Patrick", "company": "Google",
                                "email": "p@google.test"}, db)
    got = wd._migrate_contacts({"src": DEAD, "dst": LIVE, "ids": [cid]})
    assert got["ok"] and got["moved"] == 1
    assert any("moved to" in (e["detail"] or "") for e in get_job_events(DEAD, conn=db))
    assert any("moved in from" in (e["detail"] or "") for e in get_job_events(LIVE, conn=db))


def test_a_refused_move_logs_NOTHING(db):
    from applypilot import web_dashboard as wd
    from applypilot.database import get_job_events
    store.upsert_contact({"job_url": DEAD, "full_name": "Nick", "company": "Google",
                          "email": ""}, db)
    before = len(get_job_events(DEAD, conn=db))
    assert wd._migrate_contacts({"src": DEAD, "dst": LIVE, "ids": ["nope"]})["ok"] is False
    assert len(get_job_events(DEAD, conn=db)) == before
