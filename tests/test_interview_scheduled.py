"""Interview scheduled: the only outcome in a system that otherwise counts effort.

Jobs found, applications sent, emails out, follow-ups due — every number the dashboard shows is
a measure of work done, and none of them is the thing the work was for. A funnel that ends at
"replied" says how much talking happened, not whether any of it worked.

It is also the only state that means STOP. Chasing somebody after they have agreed to meet you
is the one follow-up guaranteed to cost something, so marking it halts every sequence on the
job, greys the row, and takes it out of the counter.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.domain import metrics
from applypilot.networking import store, touches
from applypilot.repo import jobs as jobsrepo


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    conn.execute("INSERT INTO jobs (url, title, site, applied_at, apply_status) "
                 "VALUES (?,?,?,?,?)",
                 ("http://j/1", "AI Engineer", "Greenhouse", "2026-07-20T10:00:00+00:00",
                  "applied"))
    conn.commit()
    return conn


# ── the state ───────────────────────────────────────────────────────────────

def test_marking_it_records_a_timestamp(db):
    assert jobsrepo.mark_interview("http://j/1", db)
    row = db.execute("SELECT interview_at FROM jobs WHERE url = ?", ("http://j/1",)).fetchone()
    assert row[0]


def test_it_does_not_overwrite_apply_status(db):
    """Rejection overwrites the status because a rejected job has LEFT the pipeline. An
    interviewing job has arrived: you still applied, the materials still exist, and the strip
    should still read "Applied". Interview is a fact on top of the state, not a replacement —
    which is also what makes undo trivial instead of having to remember the prior status.
    """
    jobsrepo.mark_interview("http://j/1", db)
    row = db.execute("SELECT apply_status, applied_at FROM jobs WHERE url = ?",
                     ("http://j/1",)).fetchone()
    assert row[0] == "applied" and row[1], "marking an interview destroyed the apply state"


def test_undo_clears_it(db):
    jobsrepo.mark_interview("http://j/1", db)
    jobsrepo.unmark_interview("http://j/1", db)
    row = db.execute("SELECT interview_at FROM jobs WHERE url = ?", ("http://j/1",)).fetchone()
    assert not row[0]


# ── it stops the outreach ───────────────────────────────────────────────────

def test_marking_it_halts_every_sequence(db):
    """Without this the ladders keep coming due and the operator has to remember, per contact,
    not to send them — which is exactly what nobody remembers at the moment they have just
    booked an interview."""
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Ada",
                                "email": "a@x.com", "company": "Acme"}, db)
    touches.record_sent(cid, "email", conn=db)
    touches.set_draft(cid, "linkedin", "", "a pending nudge")

    assert wd._mark_interview("http://j/1")["ok"]
    assert touches.ladder_state(cid, "email", db)["sequence_status"] == "stopped"
    assert touches.ladder_state(cid, "linkedin", db)["sequence_status"] == "stopped"


def test_an_untouched_channel_is_left_alone(db):
    """Only channels with a real ladder are stopped. Marking every channel stopped would make a
    contact look deliberately closed on a channel nobody ever used."""
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Ada",
                                "email": "a@x.com"}, db)
    touches.record_sent(cid, "email", conn=db)
    wd._mark_interview("http://j/1")
    assert touches.ladder_state(cid, "sms", db)["sequence_status"] == ""


def test_undo_does_not_silently_restart_outreach(db):
    """They were stopped deliberately. Resuming sends to people who were told nothing is worse
    than making the operator reopen the ones they actually want back."""
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Ada",
                                "email": "a@x.com"}, db)
    touches.record_sent(cid, "email", conn=db)
    wd._mark_interview("http://j/1")
    wd._unmark_interview("http://j/1")
    assert touches.ladder_state(cid, "email", db)["sequence_status"] == "stopped"


def test_it_reaches_the_activity_log(db):
    """§Lessons 15. A state change that leaves no trace is indistinguishable from a click that
    did nothing, and this one silently stops outreach."""
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Ada",
                                "email": "a@x.com"}, db)
    touches.record_sent(cid, "email", conn=db)
    wd._mark_interview("http://j/1")
    details = [r[0] for r in db.execute(
        "SELECT detail FROM job_events WHERE job_url = ?", ("http://j/1",)).fetchall()]
    assert any("Interview scheduled" in d for d in details)
    assert any("sequence" in d for d in details), "the log does not say outreach was stopped"


def test_a_missing_job_is_rejected(db):
    assert wd._mark_interview("http://nope/")["ok"] is False
    assert wd._mark_interview("")["ok"] is False


# ── it is the success metric ────────────────────────────────────────────────

def test_the_funnel_ends_at_interviews():
    jobs = [{"applied_at": "x", "interview_at": "2026-08-03T00:00:00+00:00"},
            {"applied_at": "x", "interview_at": ""},
            {"applied_at": "x"}]
    f = metrics.funnel(jobs, [])
    assert f.interviews == 1
    assert f.steps[-1]["key"] == "interviews", (
        "the funnel does not end at the only outcome it measures")
    assert f.as_dict()["interviews"] == 1


# ── it stops asking for attention ───────────────────────────────────────────

_STUBS = """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[],
  setAttribute(){}, getAttribute:()=>null, focus(){}, scrollIntoView(){},
  classList:{toggle(){},add(){},remove(){}}, addEventListener(){}, appendChild(){}, dataset:{} });
globalThis.document = { getElementById: el, querySelectorAll: ()=>[], querySelector: el,
  addEventListener(){}, activeElement:null, body: el(), hasFocus: () => false };
globalThis.window = { open(){}, location:{href:''} };
Object.defineProperty(globalThis,"navigator",{value:{clipboard:{writeText(){}}},configurable:true});
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0;
globalThis.fetch = async () => ({ json: async () => ({}) });
globalThis.alert = () => {}; globalThis.confirm = () => true;
"""


def _js(driver, tmp_path, **payload):
    from applypilot import web_dashboard
    src = (web_dashboard._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "t.mjs"
    script.write_text(
        _STUBS + f"const SRC = {json.dumps(src)};\n"
        + "const F = (new Function(SRC + '; return { pendingActions, needsYou, nextAction, "
          "rowMenu, PANEL_OPEN };'))();\n"
        + "".join(f"const {k} = {json.dumps(v)};\n" for k, v in payload.items()) + driver)
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:1500]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _job(**over):
    j = {"url": "http://j/1", "title": "PM", "company": "Acme", "status": "applied",
         "contacts": [{"id": "c1"}], "awaiting_reply": [], "interview_at": "",
         "followups": {"due_count": 3, "li_due_count": 0, "sms_due_count": 0},
         "checklist": {"steps": []}}
    j.update(over)
    return j


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_it_leaves_the_counter_and_the_badge(tmp_path):
    """A job that has arrived must stop asking for work, or the badge is permanently lit and
    trains you to ignore it — the same reason the tab badge counts only what is NEW (CRM-3a)."""
    out = _js("""
        const before = { pending: F.pendingActions([J]).total, needs: F.needsYou(J) };
        const won = Object.assign({}, J, { interview_at: '2026-08-03T00:00:00+00:00' });
        const after = { pending: F.pendingActions([won]).total, needs: F.needsYou(won) };
        console.log(JSON.stringify({ before, after }));
        """, tmp_path, J=_job())
    assert out["before"]["pending"] == 3 and out["before"]["needs"] is True
    assert out["after"]["pending"] == 0, "an interviewing job still demanded work"
    assert out["after"]["needs"] is False


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_next_action_says_it_is_done_rather_than_offering_a_ladder(tmp_path):
    out = _js("""
        const won = Object.assign({}, J, { interview_at: '2026-08-03T00:00:00+00:00' });
        console.log(JSON.stringify(F.nextAction(won)));
        """, tmp_path, J=_job())
    assert "Interview scheduled" in out
    assert "followups" not in out, "it still offers to chase somebody who agreed to meet"


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_the_menu_offers_undo_once_it_is_set(tmp_path):
    out = _js("""
        const won = Object.assign({}, J, { interview_at: '2026-08-03T00:00:00+00:00' });
        console.log(JSON.stringify({ off: F.rowMenu(J), on: F.rowMenu(won) }));
        """, tmp_path, J=_job())
    assert "markInterview" in out["off"] and "unmarkInterview" not in out["off"]
    assert "unmarkInterview" in out["on"]


def test_the_frontend_and_backend_agree_on_the_endpoints():
    """A half-applied rename here is silent: the button posts, nothing handles it, and the
    dashboard reports a generic failure the operator reads as a fluke."""
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    py = open(wd.__file__, encoding="utf-8").read()
    for route in ("/api/mark-interview", "/api/unmark-interview"):
        assert route in js, f"{route} is never called by the frontend"
        assert f'path == "{route}"' in py, f"{route} has no handler"
