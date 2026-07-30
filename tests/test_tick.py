"""CRM-3b — `applypilot tick`, the unattended heartbeat.

Three hard rules, and they are what these tests are really for. `tick` may run on a timer with
nobody watching, so the things it must NEVER do matter more than the things it does:

  * never send anything — every safeguard in `gmail_send.py` assumes a human initiated the
    action, and there is still no per-company cap;
  * never start an apply — co-pilot ends by handing a browser to a human, so an unattended
    apply fills a form nobody is there to review AND closes whatever review browser is open;
  * never touch `apply.pause` — writing it would pause a live application, clearing it would
    un-pause one the operator paused deliberately.

Plus the bug found while building it: `tick` reported ZERO follow-ups due while the dashboard
showed three, because `emailed` is a DERIVED field the dashboard adds and not a column. Raw DB
rows therefore read as "this channel was never used". It failed silently, which is the worst
way for a scheduled job to fail.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot import tick
from applypilot.networking import store, touches


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    return conn


@pytest.fixture(autouse=True)
def no_gmail(monkeypatch):
    """Default to Gmail being unavailable so no test can accidentally hit the network."""
    from applypilot.networking import gmail_read
    monkeypatch.setattr(gmail_read, "available", lambda: (False, "not connected in tests"))


# ── the three things it must never do ────────────────────────────────────────────────────

def test_tick_never_sends_anything(db, monkeypatch):
    """Drafting and queueing only. Sending stays a human click."""
    from applypilot.networking import gmail_send

    def boom(*a, **k):
        raise AssertionError("tick attempted to SEND an email")

    for name in ("send_followup", "send_outreach", "send"):
        if hasattr(gmail_send, name):
            monkeypatch.setattr(gmail_send, name, boom)
    tick.run(conn=db)


def test_tick_never_starts_an_apply(db, monkeypatch):
    """An unattended apply would fill a form nobody is there to review — and launching one
    closes whatever review browser is already open (§Lessons 8)."""
    import subprocess

    from applypilot.apply import launcher

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: pytest.fail("tick spawned a subprocess"))
    monkeypatch.setattr(launcher, "main",
                        lambda *a, **k: pytest.fail("tick started the apply pipeline"))
    tick.run(conn=db)


def test_tick_never_touches_the_pause_flag(db):
    """That flag is consumed by a running agent and cleared at start-up by `main()`. Writing it
    would pause a live application; clearing it would un-pause a deliberate pause."""
    from applypilot.apply import pause

    pause.request_pause()
    try:
        tick.run(conn=db)
        assert pause.pause_requested() is True, "tick cleared a pause the operator set"
    finally:
        pause.clear_pause()

    tick.run(conn=db)
    assert pause.pause_requested() is False, "tick created a pause flag out of nothing"


# ── isolation: one broken step must not take the rest down ───────────────────────────────

def test_a_failing_step_does_not_abort_the_others(db, monkeypatch):
    """A heartbeat that stops halfway is worse than none — the steps that DID run look
    identical to the steps that did not."""
    def explode(conn, dry_run):
        raise RuntimeError("boom")

    monkeypatch.setattr(tick, "STEPS", (("locks", explode),
                                        ("replies", tick._step_poll_replies),
                                        ("followups", tick._step_draft_followups)))
    out = tick.run(conn=db)
    assert "boom" in out["steps"]["locks"]["error"]
    assert "replies" in out["steps"] and "followups" in out["steps"]


def test_run_never_raises_even_when_everything_fails(db, monkeypatch):
    def explode(conn, dry_run):
        raise RuntimeError("nope")

    monkeypatch.setattr(tick, "STEPS", tuple((n, explode) for n, _ in tick.STEPS))
    out = tick.run(conn=db)
    assert all("error" in step for step in out["steps"].values())


# ── dry run ──────────────────────────────────────────────────────────────────────────────

def test_dry_run_changes_nothing(db):
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "PJ",
                                "email": "pj@x.com", "sent_message_id": "m1",
                                "submitted_at": "2026-01-01T00:00:00+00:00"}, db)
    before = store.get_contact(cid, db)
    out = tick.run(dry_run=True, conn=db)
    assert out["dry_run"] is True
    assert store.get_contact(cid, db) == before
    assert db.execute("SELECT COUNT(*) FROM touches").fetchone()[0] == 0


def test_dry_run_reports_what_it_would_do():
    out = tick.run(dry_run=True)
    assert "would_release" in out["steps"]["locks"]
    assert "detail" in out["steps"]["followups"]


# ── the silent-zero bug ──────────────────────────────────────────────────────────────────

def test_a_raw_db_row_is_ladder_ready_just_like_a_ui_payload():
    """`emailed` is DERIVED by the dashboard, not stored. Passing raw rows made every email
    ladder read "never used", so tick found 0 due while the dashboard showed 3 — silently."""
    from applypilot.domain import followup as fu

    raw = {"id": "c1", "email": "a@b.com", "sent_message_id": "m1"}
    assert fu.normalize_for_ladder(raw)["emailed"] is True
    assert fu._is_ready(fu.normalize_for_ladder(raw), fu.CHANNELS[0]) is True
    assert fu._is_ready(raw, fu.CHANNELS[0]) is False, (
        "a raw row is NOT ready without normalising — if this ever passes, the derived-field "
        "problem is gone and this guard is no longer testing anything")


def test_normalising_a_ui_payload_leaves_it_alone():
    """Idempotent: the dashboard already computes `emailed`, sometimes False on purpose."""
    from applypilot.domain import followup as fu

    payload = {"id": "c1", "email": "a@b.com", "emailed": False, "sent_message_id": "m1"}
    assert fu.normalize_for_ladder(payload)["emailed"] is False


def test_the_panel_normalises_so_every_caller_agrees(db):
    """The fix belongs at the ONE shared entry point, not at each call site — the dashboard
    passes payloads and tick passes raw rows, and they must never disagree about who is due."""
    from applypilot.domain import followup_panel

    # full_name included because `brief()` reads it with c["full_name"], not .get — a real DB
    # row always has the column, so omitting it here tested a shape that cannot occur.
    raw = {"id": "c1", "full_name": "PJ", "email": "a@b.com", "sent_message_id": "m1",
           "submitted_at": "2020-01-01T00:00:00+00:00"}
    panel = followup_panel([raw])
    assert panel["due_count"] == 1, "a long-overdue raw row was not seen as due"


# ── idempotence ──────────────────────────────────────────────────────────────────────────

def test_a_followup_already_drafted_is_not_redrafted(db, monkeypatch):
    """What makes hourly running safe: regenerating the same message every hour would spend
    real money and churn text the operator may have edited."""
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "PJ",
                                "email": "pj@x.com", "sent_message_id": "m1",
                                "submitted_at": "2020-01-01T00:00:00+00:00"}, db)
    assert len(tick._due_followups(db)) >= 1, "fixture is not actually due"

    touches.set_draft(cid, "email", "Re: hi", "already written", db)
    remaining = [d for d in tick._due_followups(db)
                 if d["contact"]["id"] == cid and d["channel"] == "email"]
    assert remaining == [], "a queued draft was offered for redrafting"


# ── the launchd schedule ─────────────────────────────────────────────────────────────────

def test_the_job_runs_the_venv_interpreter_not_system_python():
    """`launchd` inherits almost no environment. A bare "python" would be whichever one the
    system has, without applypilot installed — the job would fail silently every hour."""
    import sys

    from applypilot import schedule

    args = schedule.build_plist()["ProgramArguments"]
    assert args[0] == sys.executable
    assert args[1:] == ["-m", "applypilot.cli", "tick"]


def test_installing_the_schedule_does_not_immediately_run_it():
    """RunAtLoad would fire a tick the moment it is installed AND again at every login.
    Installing a schedule should schedule, not act."""
    from applypilot import schedule

    assert schedule.build_plist()["RunAtLoad"] is False


def test_the_schedule_is_hourly_within_working_hours():
    from applypilot import schedule

    hours = [i["Hour"] for i in schedule.build_plist()["StartCalendarInterval"]]
    assert hours == sorted(hours) and len(hours) == len(set(hours))
    assert min(hours) >= 6 and max(hours) <= 22, "runs in the middle of the night"


def test_the_plist_lives_where_launchd_looks_for_it():
    from applypilot import schedule

    p = schedule.plist_path()
    assert p.parent.name == "LaunchAgents"
    assert p.name.endswith(".plist") and schedule.LABEL in p.name


def test_a_custom_hour_list_is_honoured():
    from applypilot import schedule

    hours = [i["Hour"] for i in schedule.build_plist([9, 17])["StartCalendarInterval"]]
    assert hours == [9, 17]
