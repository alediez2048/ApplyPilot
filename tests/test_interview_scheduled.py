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

from browser_stubs import BROWSER_GLOBALS


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
""" + BROWSER_GLOBALS


def _js(driver, tmp_path, **payload):
    from applypilot import web_dashboard
    src = (web_dashboard._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "t.mjs"
    script.write_text(
        _STUBS + f"const SRC = {json.dumps(src)};\n"
        + "const F = (new Function(SRC + '; return { pendingActions, needsYou, nextAction, "
          "rowMenu, stepStrip, interviewButton, PANEL_OPEN };'))();\n"
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


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_the_button_is_on_the_row_not_only_in_the_menu(tmp_path):
    """It shipped inside the ⋯ menu and was reported as missing — the third control this
    session placed somewhere invisible. `restartButton` already carried the lesson in a comment
    ("burying it made it unfindable") and it was repeated anyway.

    Once set, the row offers the UNDO in the same place (UX-1). It used to render nothing there
    and put the undo in `⋯` — the same overflow menu the 🎯 button itself had to be dragged out
    of. Marking an interview halts every ladder on the job, so a misclick is expensive and its
    reversal is not "a second control saying the same thing".
    """
    out = _js("""
        const won = Object.assign({}, J, { interview_at: '2026-08-03T00:00:00+00:00' });
        console.log(JSON.stringify({
          strip: F.stepStrip(J), wonStrip: F.stepStrip(won),
          // The ROW control alone. Asserting on the whole strip cannot tell "on the row" from
          // "in the ⋯ menu", because the menu is INSIDE the strip — a mutation that moved the
          // undo back into the menu passed the first version of this test.
          rowBtn: F.interviewButton(J), wonRowBtn: F.interviewButton(won) }));
        """, tmp_path, J=_job())
    # `won-btn`, not `markInterview`: "unmarkInterview" CONTAINS "markInterview", so the
    # substring check reported the button as present on a job whose menu only offered undo.
    # §Lessons 1, in the test written to guard the placement.
    #
    # Match on `onclick="markInterview(` — the opening quote is what stops it matching
    # `unmarkInterview(`, which is the very substring bug this comment describes.
    assert 'onclick="markInterview(' in out["strip"], (
        "the success metric is only reachable through the ⋯ menu")
    assert 'onclick="markInterview(' not in out["wonStrip"], (
        "it still offers to mark an already-won job")
    assert "Interview scheduled" in out["wonStrip"]
    assert 'onclick="unmarkInterview(' in out["wonRowBtn"], (
        "the undo is not on the row — being somewhere in the strip includes the ⋯ menu")
    assert 'onclick="markInterview(' in out["rowBtn"], "the mark is not on the row"


def test_the_won_row_is_visibly_different():
    """The first attempt greyed the row with --surface2 (#f8f9fa) against a white row: a 2.7%
    difference on two channels, which is imperceptible. The button worked and the state saved,
    and it was reported as "the button does nothing" — because from the operator's side nothing
    about the row changed. A state change nobody can see has not happened.

    Asserts a real colour difference rather than "some rule exists", so a future tidy-up that
    swaps the value back for a token cannot pass.
    """
    import re

    from applypilot import web_dashboard
    css = (web_dashboard._STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    rule = re.search(r"tr\.row-won td \{([^}]*)\}", css)
    assert rule, "the won-row rule is gone"
    bg = re.search(r"background:\s*(#[0-9a-fA-F]{6})", rule.group(1))
    assert bg, f"the won row has no literal background colour: {rule.group(1)!r}"

    r, g, b = (int(bg.group(1)[i:i + 2], 16) for i in (1, 3, 5))
    # Against #ffffff. 2.7% was invisible; 5% per channel is the floor for "obviously changed".
    assert min(255 - r, 255 - g, 255 - b) >= 12, (
        f"{bg.group(1)} is {round(100 * (255 - max(r, g, b)) / 255, 1)}% off white — "
        "the same mistake as #f8f9fa, which shipped and read as a broken button")
    assert "var(--green)" in css[rule.end():rule.end() + 400], (
        "no green rail; the row reads as disabled rather than won")


def test_the_button_acknowledges_the_click_immediately():
    """The refresh takes a moment and the row may be off-screen. Without this the only feedback
    is a change the operator might not be looking at — which is exactly how the invisible-grey
    version came to look like a dead button."""
    from applypilot import web_dashboard
    js = (web_dashboard._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    fn = js[js.index("async function markInterview"):]
    fn = fn[:fn.index("\n}") + 2]
    assert "Saving" in fn and "Scheduled" in fn, "the button gives no immediate feedback"


def test_the_frontend_and_backend_agree_on_the_endpoints():
    """A half-applied rename here is silent: the button posts, nothing handles it, and the
    dashboard reports a generic failure the operator reads as a fluke."""
    js = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    py = open(wd.__file__, encoding="utf-8").read()
    for route in ("/api/mark-interview", "/api/unmark-interview"):
        assert route in js, f"{route} is never called by the frontend"
        assert f'path == "{route}"' in py, f"{route} has no handler"


# ── UX-1: the state has to REACH the browser, and the engine ────────────────

def test_interview_at_is_in_the_dashboard_payload(db):
    """The bug, reported three times as "the button does nothing".

    `dashboard_rows()` never selected `interview_at`, so the payload shipped "" forever and
    every downstream branch was dead: the row never greyed, the 🎯 chip never rendered, Next
    never said "Interview scheduled", and the ⋯ menu never flipped to offer the revert. The
    WRITE was correct the whole time — two jobs in the live DB carried a timestamp.

    Asserted on the payload, not on the SQL string: the point is what the browser receives.
    """
    db.execute("UPDATE jobs SET strategy = 'dashboard_upload' WHERE url = ?", ("http://j/1",))
    db.commit()
    jobsrepo.mark_interview("http://j/1", db)

    jobs = wd._status_payload()["jobs"]
    assert jobs, "empty payload — this test would measure nothing (§Lessons 13)"
    job = next(j for j in jobs if j["url"] == "http://j/1")
    assert job["interview_at"], "the interview state never reaches the browser"


def test_no_payload_key_is_silently_optional():
    """What hid it for two rounds of fixing the wrong layer:

        "interview_at": (row["interview_at"] if "interview_at" in row.keys() else "") or ""

    A guard that turns a KeyError into a plausible empty string. Without it this would have
    500'd on the first render. A column the payload needs belongs in the SELECT; if it is
    missing, the right behaviour is to crash.
    """
    import pathlib
    src = pathlib.Path(wd.__file__).read_text(encoding="utf-8")
    offenders = [ln.strip()[:90] for ln in src.splitlines()
                 if "in row.keys()" in ln and not ln.lstrip().startswith("#")]
    assert not offenders, (
        "a payload key is being read defensively instead of being SELECTed:\n  "
        + "\n  ".join(offenders))


def test_the_undo_is_on_the_row_not_only_in_the_overflow_menu(db):
    """§Lessons 43, fifth occurrence. The 🎯 button itself had to be dragged out of the ⋯ menu
    after being reported missing; its undo was left behind in the same menu."""
    import pathlib
    src = pathlib.Path(wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    block = src[src.index("function interviewButton"):]
    block = block[:block.index("\nfunction ")]
    assert "unmarkInterview" in block, "the undo is not on the row"


def test_the_undo_is_not_dimmed_into_invisibility():
    """It rendered correctly and was reported missing anyway.

    `tr.row-won + tr.job-foot .step-strip { opacity:.7 }` fades the whole strip on a won row,
    controls included — and the undo was --muted text on --surface, i.e. grey on a grey-green
    #eef2ee row at 70%. Present in the DOM and imperceptible, which is the same as absent.

    Second time on this one feature: the first won row was greyed #f8f9fa against white, a 2.7%
    difference, and was also reported as "the button does nothing". §Lessons 43.
    """
    from applypilot import web_dashboard as wd
    css = (wd._STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    assert "tr.row-won + tr.job-foot .step-strip button { opacity:1; }" in css, (
        "the won-row fade dims its own escape hatch")
    undo = next(ln for ln in css.splitlines() if ln.strip().startswith(".won-btn.undo {"))
    assert "background:#fff" in undo, (
        f"the undo has no contrasting ground against the #eef2ee won row: {undo.strip()}")


# ── "Mark as applied" (operator-asserted, distinct from Mark submitted ✓) ────

def test_marking_applied_by_hand_works_on_a_job_the_agent_never_touched(tmp_path, monkeypatch):
    """The case the OTHER button's guard exists to block, and the operator legitimately needs.

    `/api/mark-submitted` gates on `was_attempted` — right for a control that appears
    automatically in the co-pilot flow, where blessing an unopened job would be an accident.
    This one is for applying on the company's own site, where the app never ran at all.
    Refusing it leaves a true fact unrecordable, which is the corner §Lessons 19 describes.
    """
    import applypilot.database as database
    from applypilot import web_dashboard as wd
    from applypilot.repo import jobs as repo

    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    conn.execute("INSERT INTO jobs (url, title, site, strategy) VALUES (?,?,?,?)",
                 ("http://j/hand", "PM", "Greenhouse", "dashboard_upload"))
    conn.commit()

    assert repo.was_attempted("http://j/hand", conn) is False
    # The co-pilot confirmation refuses it, correctly.
    assert wd._mark_submitted("http://j/hand")["ok"] is False
    # The operator's assertion does not.
    assert wd._mark_applied_manually("http://j/hand")["ok"] is True
    assert repo.apply_status("http://j/hand", conn) == "applied"


def test_it_is_reversible_and_keeps_the_agent_run_history(tmp_path, monkeypatch):
    """A state change with no way back is a trap.

    And the undo must not be `reset_apply_state`, which also wipes attempts and errors —
    that history is the only account of what a real agent run did.
    """
    import applypilot.database as database
    from applypilot import web_dashboard as wd
    from applypilot.repo import jobs as repo

    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    conn.execute("INSERT INTO jobs (url, title, site, strategy, apply_attempts, apply_error) "
                 "VALUES (?,?,?,?,?,?)",
                 ("http://j/hand", "PM", "Greenhouse", "dashboard_upload", 2, "captcha"))
    conn.commit()

    wd._mark_applied_manually("http://j/hand")
    assert wd._unmark_applied("http://j/hand")["ok"] is True
    row = repo.get("http://j/hand", conn)
    assert row["applied_at"] is None and row["apply_status"] is None
    assert row["apply_attempts"] == 2, "the undo erased the agent's run history"
    # `apply_error` is cleared by mark_applied, not by the undo, and that is pre-existing and
    # right: once you have applied, "captcha" is a stale description of a state that no longer
    # obtains. `apply_attempts` is what carries the fact that an agent ran, and it survives.
    assert row["apply_error"] is None


def test_marking_it_twice_is_refused(tmp_path, monkeypatch):
    import applypilot.database as database
    from applypilot import web_dashboard as wd

    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    conn.execute("INSERT INTO jobs (url, title, site, strategy) VALUES (?,?,?,?)",
                 ("http://j/hand", "PM", "Greenhouse", "dashboard_upload"))
    conn.commit()
    assert wd._mark_applied_manually("http://j/hand")["ok"] is True
    assert wd._mark_applied_manually("http://j/hand")["ok"] is False
