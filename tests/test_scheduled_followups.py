"""Scheduling a follow-up: the promise, the firing, and the two edges.

The interesting tests here are not "does it store a timestamp". They are the properties that
only matter because this is the ONE path in the app that sends with nobody watching:

  * a reply arriving between the promise and the firing must stop it
  * a promise must never fire twice, and must never retry itself into a loop
  * a promise too old to fire must not fire quietly a week late
  * the browser and the server must agree on which of those a given time IS
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from applypilot.domain import sendtime

JS = Path(__file__).resolve().parents[1] / "src" / "applypilot" / "static" / "dashboard.js"
CSS = Path(__file__).resolve().parents[1] / "src" / "applypilot" / "static" / "dashboard.css"


def _now():
    return datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


# ── domain/sendtime.py ──────────────────────────────────────────────────────

def test_a_time_in_the_past_is_refused_rather_than_sent_immediately():
    """Clamping to `now` would send at once — the one outcome a future time rules out."""
    at, err = sendtime.validate((_now() - timedelta(hours=1)).isoformat(), _now())
    assert at == ""
    assert "passed" in err


def test_a_time_beyond_the_horizon_is_refused():
    at, err = sendtime.validate(
        (_now() + timedelta(days=sendtime.MAX_AHEAD_DAYS + 1)).isoformat(), _now())
    assert at == ""
    assert str(sendtime.MAX_AHEAD_DAYS) in err


def test_the_horizon_is_the_default_not_something_the_caller_passes():
    """§Lessons 100: a test that supplies the bound cannot see the bound that ships."""
    at, err = sendtime.validate((_now() + timedelta(days=29)).isoformat(), _now())
    assert err == "" and at, "29 days must be inside a 30-day horizon"
    at, err = sendtime.validate((_now() + timedelta(days=31)).isoformat(), _now())
    assert at == "" and err


def test_unparseable_input_is_refused_and_says_so():
    at, err = sendtime.validate("next tuesday-ish", _now())
    assert at == "" and "time" in err.lower()


def test_a_naive_timestamp_is_read_as_utc_rather_than_raising():
    """Older rows have no timezone and subtracting an aware `now` raises (§Lessons 6)."""
    at, err = sendtime.validate("2026-09-01T09:00:00", _now())
    assert err == "" and at.endswith("+00:00")


@pytest.mark.parametrize("delta_h,want", [
    (5, sendtime.PENDING),      # not yet due
    (-1, sendtime.DUE),         # due, well inside the grace window
    (-23, sendtime.DUE),        # still inside 24h
    (-25, sendtime.MISSED),     # past it
    (-500, sendtime.MISSED),
])
def test_state_reads_pending_due_or_missed(delta_h, want):
    at = (_now() + timedelta(hours=delta_h)).isoformat()
    assert sendtime.state(at, _now(), grace_hours=24) == want


def test_nothing_scheduled_is_the_empty_string_not_a_state():
    assert sendtime.state("", _now()) == ""
    assert sendtime.state("   ", _now()) == ""


def test_the_grace_cutoff_and_the_state_function_agree():
    """`lapsed_before` feeds a SQL comparison and `state` feeds the card. If they disagreed the
    poller would fire something the screen called missed, or the reverse."""
    now = _now()
    cutoff = sendtime.lapsed_before(now, 24)
    for h in (-1, -23, -23.9, -24.1, -30):
        at = (now + timedelta(hours=h)).isoformat()
        claimable = at <= now.isoformat() and at > cutoff
        assert claimable == (sendtime.state(at, now, 24) == sendtime.DUE), h


def test_zero_grace_means_never_fire_late_not_the_default(monkeypatch):
    """§Lessons 50: 0 meant 'unlimited' in two settings and 'send nothing' in a third. Here it is
    the strictest available answer and folding it into the default with `or` inverts it."""
    assert sendtime.state((_now() - timedelta(minutes=1)).isoformat(), _now(),
                          grace_hours=0) == sendtime.MISSED
    monkeypatch.setattr("applypilot.settings.resolve",
                        lambda *a, **k: ({"SCHEDULED_SEND_GRACE_HOURS": 0}, {}))
    assert sendtime.grace_hours() == 0


def test_a_missing_setting_falls_back_to_the_declared_default(monkeypatch):
    monkeypatch.setattr("applypilot.settings.resolve", lambda *a, **k: ({}, {}))
    assert sendtime.grace_hours() == sendtime.GRACE_HOURS


# ── storage ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def db():
    """The ambient test database, emptied.

    Deliberately NOT a per-test `APPLYPILOT_DIR`: `get_connection` is thread-local and keyed on
    a module-level path resolved at import, so a fresh directory would give this fixture one
    database and `_followup_action` — which calls `get_connection()` itself — a different one.
    The tests that matter here drive the real endpoint, so they have to share its connection.
    The suite already runs under `APPLYPILOT_DIR=$(mktemp -d)`.
    """
    from applypilot.database import get_connection, init_db
    from applypilot.networking import store, touches
    init_db()
    conn = get_connection()
    store.init_contacts(conn)
    touches.init_touches(conn)
    # Only THIS file's rows. Wiping the tables would be tidier and would quietly break any other
    # test that shares the ambient database — the suite passes today in one order and there is
    # nothing enforcing that order.
    #
    # Scoped by JOB rather than by a list of contact ids, which is what it was and which leaked:
    # every test here files its people under `j1`, but a hardcoded ("c1","c2","c3") missed the
    # ones the throttle tests mint (`acr0`, `pAcrisure`, …), so those survived into the next test
    # and it counted four claims where it expected three. A cleanup keyed on the thing the tests
    # actually share cannot fall behind the tests.
    conn.execute("DELETE FROM touches WHERE contact_id IN "
                 "(SELECT id FROM contacts WHERE job_url = 'j1')")
    conn.execute("DELETE FROM sequences WHERE contact_id IN "
                 "(SELECT id FROM contacts WHERE job_url = 'j1')")
    conn.execute("DELETE FROM contacts WHERE job_url = 'j1'")
    conn.execute("DELETE FROM job_events WHERE job_url = 'j1'")
    conn.commit()
    store.upsert_contact({"id": "c1", "job_url": "j1", "full_name": "Dana",
                          "email": "dana@x.com", "sent_message_id": "m1"}, conn)
    return conn


def test_you_cannot_schedule_a_send_with_nothing_written(db):
    """The rule the whole feature rests on: a send you have not read is one you cannot judge."""
    from applypilot.networking import touches
    assert touches.schedule_send("c1", "email", "2026-09-01T09:00:00+00:00", db) is False


def test_an_empty_draft_body_does_not_count_as_written(db):
    from applypilot.networking import touches
    touches.set_draft("c1", "email", "subject only", "   ", db)
    assert touches.schedule_send("c1", "email", "2026-09-01T09:00:00+00:00", db) is False


def test_scheduling_reaches_the_payload_through_the_bulk_ladder_query(db):
    """It must ride `ladder_states`, which is ONE statement for every contact on the page."""
    from applypilot.networking import touches
    touches.set_draft("c1", "email", "s", "body", db)
    touches.schedule_send("c1", "email", "2026-09-01T09:00:00+00:00", db)
    assert touches.ladder_state("c1", "email", db)["scheduled_at"] == "2026-09-01T09:00:00+00:00"


def test_redrafting_keeps_the_appointment(db):
    """Revising the words and withdrawing the promise are different intentions, and only one has
    a button. Unscheduling on every redraft would silently cancel tomorrow's send."""
    from applypilot.networking import touches
    touches.set_draft("c1", "email", "s", "first", db)
    touches.schedule_send("c1", "email", "2026-09-01T09:00:00+00:00", db)
    touches.set_draft("c1", "email", "s", "revised", db)
    st = touches.ladder_state("c1", "email", db)
    assert st["scheduled_at"] == "2026-09-01T09:00:00+00:00"
    assert st["draft_body"] == "revised"


def test_cancelling_keeps_the_draft(db):
    """"Do not send this at 9am" is not "throw away what I wrote"."""
    from applypilot.networking import touches
    touches.set_draft("c1", "email", "s", "hand-edited body", db)
    touches.schedule_send("c1", "email", "2026-09-01T09:00:00+00:00", db)
    assert touches.cancel_schedule("c1", "email", db) is True
    st = touches.ladder_state("c1", "email", db)
    assert st["scheduled_at"] == "" and st["draft_body"] == "hand-edited body"


def test_sending_by_hand_clears_a_pending_appointment(db):
    """Otherwise a sent message carries a future send time."""
    from applypilot.networking import touches
    touches.set_draft("c1", "email", "s", "body", db)
    touches.schedule_send("c1", "email", "2026-09-01T09:00:00+00:00", db)
    touches.record_sent("c1", "email", conn=db)
    row = db.execute("SELECT scheduled_at FROM touches WHERE contact_id='c1'").fetchone()
    assert not (row["scheduled_at"] or "")


def _schedule(conn, at):
    from applypilot.networking import touches
    touches.set_draft("c1", "email", "s", "body", conn)
    touches.schedule_send("c1", "email", at, conn)


def test_a_promise_is_claimed_once_and_only_once(db):
    """The claim clears it in the same call, so a crash mid-send loses the promise rather than
    replaying the email every five minutes forever."""
    from applypilot.networking import touches
    now = _now()
    _schedule(db, (now - timedelta(minutes=5)).isoformat())
    first = touches.claim_due_scheduled(now.isoformat(), sendtime.lapsed_before(now, 24), conn=db)
    second = touches.claim_due_scheduled(now.isoformat(), sendtime.lapsed_before(now, 24), conn=db)
    assert len(first) == 1 and second == []


def test_a_promise_not_yet_due_is_left_alone(db):
    from applypilot.networking import touches
    now = _now()
    _schedule(db, (now + timedelta(hours=3)).isoformat())
    assert touches.claim_due_scheduled(now.isoformat(),
                                       sendtime.lapsed_before(now, 24), conn=db) == []


def test_a_lapsed_promise_is_neither_fired_nor_erased(db):
    """It has to stay visible as `missed`. Clearing it would make a send the operator asked for
    disappear with nothing on screen; firing it mails somebody days late, unattended."""
    from applypilot.networking import touches
    now = _now()
    at = (now - timedelta(hours=48)).isoformat()
    _schedule(db, at)
    assert touches.claim_due_scheduled(now.isoformat(),
                                       sendtime.lapsed_before(now, 24), conn=db) == []
    assert touches.ladder_state("c1", "email", db)["scheduled_at"] == at
    assert sendtime.state(at, now, 24) == sendtime.MISSED


def test_the_scheduled_column_is_added_to_a_table_that_already_exists(tmp_path):
    """The index on this column MUST be created after the additive column pass.

    On an existing install `CREATE TABLE IF NOT EXISTS` is a no-op, so indexing `scheduled_at`
    any earlier raises `no such column` — on every real database and on none of the fresh ones a
    test would otherwise build. This constructs the old shape deliberately.
    """
    path = tmp_path / "legacy.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE touches (id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, "
                "channel TEXT NOT NULL, seq INTEGER NOT NULL, status TEXT, subject TEXT, "
                "body TEXT, due_at TEXT, sent_at TEXT, error TEXT, created_at TEXT, "
                "updated_at TEXT)")
    old.execute("INSERT INTO touches (id, contact_id, channel, seq, status) "
                "VALUES ('t1','c1','email',1,'drafted')")
    old.commit()
    old.close()

    from applypilot.database import get_connection
    from applypilot.networking import touches
    conn = get_connection(str(path))          # this legacy file, not the ambient one
    touches.init_touches(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(touches)")}
    assert "scheduled_at" in cols
    assert conn.execute("SELECT COUNT(*) FROM touches").fetchone()[0] == 1, "row survived"
    names = {r[1] for r in conn.execute("PRAGMA index_list(touches)")}
    assert "idx_touches_scheduled" in names


# ── firing ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def wired(db, monkeypatch):
    """A live send path with only the TRANSPORT stubbed.

    Deliberately not stubbing `send_followup`: every guard this feature relies on lives inside
    it, so replacing it would test the poller against no guards at all and pass.
    """
    from applypilot.networking import gmail_oauth, store
    import applypilot.networking.gmail_send as gs
    sent: list[str] = []
    monkeypatch.setattr(gs, "transport", lambda: "oauth")
    monkeypatch.setattr(gs, "_from_address", lambda: "me@x.com")
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: "me@x.com")
    monkeypatch.setattr(gmail_oauth, "send",
                        lambda to, *a, **k: (sent.append(to), {"id": "x", "threadId": "t"})[1])
    for cid, name in (("c2", "Sam"), ("c3", "Ada")):
        store.upsert_contact({"id": cid, "job_url": "j1", "full_name": name,
                              "email": f"{cid}@x.com", "sent_message_id": "m" + cid}, db)
    return sent


def _promise(conn, cid, at):
    from applypilot.networking import touches
    touches.set_draft(cid, "email", "s", "body", conn)
    touches.schedule_send(cid, "email", at, conn)


def test_a_contact_who_replied_after_the_promise_is_not_emailed(db, wired):
    """The whole reason the poller runs AFTER reply detection, and the single worst thing this
    feature could do (§Lessons 27)."""
    from applypilot.networking import touches
    from applypilot.web_dashboard import ReplyPoller
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _promise(db, "c1", past)
    _promise(db, "c2", past)
    touches.set_sequence_status("c2", "email", "replied", "they answered", db)

    res = ReplyPoller._poll_scheduled()
    assert wired == ["dana@x.com"], wired
    assert res["sent"] == 1 and res["failed"] == 1


def test_a_stopped_sequence_is_not_emailed(db, wired):
    from applypilot.networking import touches
    from applypilot.web_dashboard import ReplyPoller
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _promise(db, "c3", past)
    touches.set_sequence_status("c3", "email", "stopped", "operator stopped", db)
    ReplyPoller._poll_scheduled()
    assert wired == []


def test_a_refused_send_keeps_its_draft_and_does_not_stay_scheduled(db, wired):
    """A promise is "try once at this time", never a standing order that retries into the same
    refusal every five minutes."""
    from applypilot.networking import touches
    from applypilot.web_dashboard import ReplyPoller
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _promise(db, "c2", past)
    touches.set_sequence_status("c2", "email", "stopped", "", db)
    ReplyPoller._poll_scheduled()
    st = touches.ladder_state("c2", "email", db)
    assert st["scheduled_at"] == "" and st["draft_body"] == "body"


def test_a_refusal_is_written_where_the_operator_will_meet_it(db, wired):
    """Nobody is present when this happens. If it is not logged on the contact, the only
    evidence is an email that never arrived."""
    from applypilot.networking import touches
    from applypilot.web_dashboard import ReplyPoller
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _promise(db, "c2", past)
    touches.set_sequence_status("c2", "email", "stopped", "", db)
    ReplyPoller._poll_scheduled()
    # Contact events land in the JOB's activity log — that is the surface the operator reads.
    text = " ".join(str(dict(e)) for e in
                    db.execute("SELECT * FROM job_events WHERE job_url='j1'"))
    assert "Scheduled follow-up did not send" in text


def test_the_poller_fires_scheduled_sends_after_reply_detection(db):
    """Order, read off the source. Reversed, a follow-up goes to somebody who answered this
    morning five minutes before the poll that would have noticed."""
    import inspect
    from applypilot.web_dashboard import ReplyPoller
    src = inspect.getsource(ReplyPoller.poll_now)
    assert src.index("reply_svc.poll") < src.index("_poll_scheduled")


def test_nothing_but_email_can_be_fired_unattended(db, wired):
    """SMS and LinkedIn are copy-paste by design (§Lessons 3). A channel reaching this path would
    be auto-sending something built for a human to paste.

    The email draft below is what makes this test able to FAIL. Without it, dropping the channel
    guard sends nothing anyway — `_followup_action` resolves a bare `send` to EMAIL, finds no
    email draft and refuses for an unrelated reason, so the assertion held with the guard
    deleted. A vacuous test in exactly the §Lessons 71 shape: it passes when the thing under
    test is emptied.
    """
    from applypilot.networking import touches
    from applypilot.web_dashboard import ReplyPoller
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    touches.set_draft("c1", "email", "s", "email body", db)     # written, NOT scheduled
    touches.set_draft("c1", "sms", "", "text body", db)
    touches.schedule_send("c1", "sms", past, db)
    res = ReplyPoller._poll_scheduled()
    assert res["sent"] == 0
    assert wired == [], "a scheduled SMS must not fire the email draft sitting beside it"


def test_a_scheduled_send_still_obeys_a_space_that_forbids_auto_send(db, wired, monkeypatch):
    """The gate lives in `_followup_action`, not in `send_followup` — so the poller has to go
    through it. Calling the sender directly would look identical and silently drop the one
    setting whose whole job is to stop an irreversible send happening unattended.

    The promise is written straight to storage here, deliberately: the endpoint refuses to make
    one for such a Space, so going through it would test the early check twice and this one
    never.
    """
    from applypilot import web_dashboard as wd

    class _Space:
        name, can_autosend = "Lead Sheet", False
    monkeypatch.setattr(wd, "_space_of_contact", lambda *a, **k: _Space())
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _promise(db, "c1", past)
    res = wd.ReplyPoller._poll_scheduled()
    assert wired == [] and res["sent"] == 0
    assert any("auto-send off" in w for w in res["why"]), res["why"]


# ── the endpoint ────────────────────────────────────────────────────────────

def test_the_bulk_cap_applies_to_scheduling_too(db):
    """Promising 500 sends for 9am is the same blast radius as sending 500 now, deferred."""
    from applypilot.web_dashboard import _BULK_MAX, _bulk_followups
    res = _bulk_followups({"action": "schedule", "at": "2026-09-01T09:00:00+00:00",
                           "contact_ids": [f"c{i}" for i in range(_BULK_MAX + 1)]})
    assert res["ok"] is False and str(_BULK_MAX) in res["message"]


def test_a_bad_time_is_refused_before_anything_is_promised(db):
    from applypilot.networking import touches
    from applypilot.web_dashboard import _bulk_followups
    touches.set_draft("c1", "email", "s", "body", db)
    res = _bulk_followups({"action": "schedule", "at": "yesterday", "contact_ids": ["c1"]})
    assert res["ok"] is False
    assert touches.ladder_state("c1", "email", db)["scheduled_at"] == ""


def test_a_space_with_autosend_off_cannot_have_a_scheduled_send(db, monkeypatch):
    """Not redundancy with the fire-time check: this Space will refuse forever, so accepting the
    promise puts a time on a card that is guaranteed never to arrive."""
    from applypilot.networking import touches
    from applypilot import web_dashboard as wd

    class _Space:
        name, can_autosend = "Lead Sheet", False
    monkeypatch.setattr(wd, "_space_of_contact", lambda *a, **k: _Space())
    touches.set_draft("c1", "email", "s", "body", db)
    res = wd._bulk_followups({"action": "schedule", "contact_ids": ["c1"],
                              "at": (datetime.now(timezone.utc)
                                     + timedelta(days=1)).isoformat()})
    assert res["ok"] is False and "auto-send off" in res["message"]
    assert touches.ladder_state("c1", "email", db)["scheduled_at"] == ""


def test_every_bulk_result_states_whether_it_worked(db):
    """The browser derives its red/green from `ok`. Omitted, `!x.ok` is true for every row and a
    batch where all eight sends succeeded flashes all eight cards red (§Lessons 103)."""
    from applypilot.networking import touches
    from applypilot.web_dashboard import _bulk_followups
    touches.set_draft("c1", "email", "s", "body", db)
    res = _bulk_followups({"action": "schedule", "contact_ids": ["c1", "nope"],
                           "at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
    assert [r["ok"] for r in res["results"]] == [True, False]


def test_unscheduling_one_contact_is_reachable_from_the_single_card(db):
    from applypilot.networking import touches
    from applypilot.web_dashboard import _followup_action
    touches.set_draft("c1", "email", "s", "body", db)
    touches.schedule_send("c1", "email", "2026-09-01T09:00:00+00:00", db)
    res = _followup_action({"contact_id": "c1", "action": "unschedule"})
    assert res["ok"] is True
    assert touches.ladder_state("c1", "email", db)["scheduled_at"] == ""


# ── the browser must agree with the server ──────────────────────────────────

def test_the_browser_and_the_server_draw_the_missed_line_in_the_same_place():
    """Two implementations of one rule. If the card called a promise missed while the poller
    still called it due, the operator would be told to send by hand at the same moment the
    machine sent it for them — §Lessons 49, in the two layers that cannot see each other."""
    js = JS.read_text(encoding="utf-8")
    m = re.search(r"function schedState\(iso\)\s*\{(.+?)\n\}", js, re.S)
    assert m, "schedState not found"
    body = m.group(1)
    assert "SCHED_GRACE_H * 3600 * 1000" in body, "grace must come from the payload"
    assert "'pending'" in body and "'due'" in body and "'missed'" in body
    # The same three words the domain uses, so a rename cannot leave one side behind.
    assert {sendtime.PENDING, sendtime.DUE, sendtime.MISSED} == {"pending", "due", "missed"}


def test_the_grace_window_is_served_never_written_twice_in_the_frontend():
    js = JS.read_text(encoding="utf-8")
    assert "data.scheduled_grace_h" in js
    # `|| 24` would turn the strictest setting (0) into the default (§Lessons 50).
    assert "data.scheduled_grace_h != null" in js
    assert not re.search(r"SCHED_GRACE_H\s*=\s*data\.scheduled_grace_h\s*\|\|", js)


def test_the_picker_converts_local_time_to_utc():
    """`<input type=datetime-local>` yields wall-clock with no zone. Posted raw, the server reads
    9am as 9am UTC — 3am in Austin, and the email is gone before breakfast."""
    js = JS.read_text(encoding="utf-8")
    m = re.search(r"function schedToUtc\(local\)\s*\{(.+?)\n\}", js, re.S)
    assert m and "toISOString()" in m.group(1)
    assert "body.at = schedToUtc(" in js


def test_the_operator_is_told_the_dashboard_has_to_be_running():
    """Nothing fires while it is closed. An operator who schedules five sends and shuts the
    laptop has to learn that before they walk away, not from five emails that never arrived."""
    js = JS.read_text(encoding="utf-8")
    m = re.search(r'fu-sendat-note">(.+?)</div>', js, re.S)
    assert m, "the note is gone"
    note = m.group(1).lower()
    assert "dashboard" in note and ("running" in note or "open" in note)
    assert "POLL_EVERY_S" in m.group(1), "state the real cadence, never a hardcoded guess"


def test_each_bulk_action_acts_on_a_different_set():
    """Drafting must not overwrite hand-edited drafts, and scheduling must not re-promise
    something already promised."""
    js = JS.read_text(encoding="utf-8")
    m = re.search(r"async function fuBulk\(url, action, btn\)\s*\{(.+?)\n\}", js, re.S)
    assert m
    body = m.group(1)
    assert "action === 'draft') due = due.filter(c => !written(c))" in body
    assert "!['pending', 'due'].includes(schedState(" in body, \
        "schedule skips what is already promised"


def test_a_missed_promise_can_be_rescheduled_and_cleared_in_BULK():
    """It will never fire on its own, so counting it as scheduled would leave a job with five of
    them needing five per-card clicks before the bulk control worked at all — §Lessons 99, the
    fix for a dead end that takes the way out with it.

    `due` sits with `pending` and not with `missed`: the poller is about to send it, so
    re-promising would silently move a send the operator already set.
    """
    js = JS.read_text(encoding="utf-8")
    bar = js[js.index("function fuBulkBar"):js.index("function fuSchedToggle")]
    assert "const schedulable = drafted.filter(c => !['pending', 'due'].includes(" in bar
    assert "const missed = drafted.filter(c => schedState(c.followup_scheduled_at) === 'missed')" in bar
    fn = js[js.index("async function fuBulk("):js.index("function fuFlash(")]
    assert "!['pending', 'due'].includes(schedState(c.followup_scheduled_at))" in fn
    assert "action === 'unschedule')\n    due = due.filter(c => !!schedState(" in fn


def test_schedule_and_unschedule_are_offered_TOGETHER_when_both_apply():
    """They act on disjoint sets. Either/or left one promise plus four unscheduled drafts showing
    only Unschedule — so scheduling the rest meant withdrawing the promise already made."""
    js = JS.read_text(encoding="utf-8")
    bar = js[js.index("function fuBulkBar"):js.index("function fuSchedToggle")]
    decl = bar[bar.index("const schedBtn"):bar.index("const picker")]
    assert "fuSchedToggle" in decl and "'unschedule'" in decl
    # A `?:` between the two is the shape that hides one of them; a `+` renders both.
    assert "\n    + (promised.length" in decl, "the two must concatenate, not choose"


def test_missed_is_counted_separately_from_scheduled_in_the_bar():
    """Folded together it reads as work in hand, when it is the exact opposite."""
    js = JS.read_text(encoding="utf-8")
    bar = js[js.index("function fuBulkBar"):js.index("function fuSchedToggle")]
    assert "missed.length" in bar and "fu-bulk-missed" in bar
    css = CSS.read_text(encoding="utf-8")
    m = re.search(r"\.fu-bulk-sched\.fu-bulk-missed\s*\{([^}]*)\}", css)
    assert m and "color:var(--" in m.group(1) and "background:var(--" in m.group(1)


def test_the_schedule_confirm_names_both_the_time_and_the_people():
    js = JS.read_text(encoding="utf-8")
    m = re.search(r"if \(action === 'schedule'\) \{(.+?)\n  \}", js, re.S)
    assert m
    block = m.group(1)
    assert "fmtSched(body.at)" in block, "the time, in the operator's own timezone"
    assert "${who}" in block, "the names, which a job-scoped batch is small enough to list"


def test_a_scheduled_send_is_visible_on_the_person_not_only_in_the_bulk_bar():
    """Otherwise opening the card shows an ordinary unsent draft, the operator sends it by hand,
    and the schedule looks like it silently failed."""
    js = JS.read_text(encoding="utf-8")
    m = re.search(r"function followupCard\(c, d, totalTouches\)\s*\{(.+?)\n\}", js, re.S)
    assert m
    body = m.group(1)
    assert "schedState(c.followup_scheduled_at)" in body
    assert "${schedRow}" in body, "computed and then never rendered is the classic version"


def test_a_missed_promise_says_why_and_offers_a_way_out():
    js = JS.read_text(encoding="utf-8")
    m = re.search(r"const schedRow = (.+?);\n  return", js, re.S)
    assert m
    block = m.group(1)
    assert "fu-sendat-missed" in block
    assert "dashboard was not running" in block
    assert "unschedule" in block, "a state you cannot clear is a dead end (§Lessons 99)"


def test_the_scheduled_banner_sets_a_background_and_a_foreground_together():
    """§Lessons 105: setting one without the other is the whole of how a banner becomes
    unreadable, and this stylesheet's tokens are the only thing that adapts with the app."""
    css = CSS.read_text(encoding="utf-8")
    for sel in (".fu-sendat", ".fu-sendat.fu-sendat-missed"):
        m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css)
        assert m, sel
        rule = m.group(1)
        assert "color:var(--" in rule and "background:var(--" in rule, sel


def test_no_scheduling_rule_sets_display_flex_on_a_table_cell():
    """§Lessons 101: a `display` on a td silently un-does colspan, and no markup test sees it."""
    css = CSS.read_text(encoding="utf-8")
    for sel, _rule in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if "fu-sendat" in sel and sel.strip().rstrip().endswith("td"):
            raise AssertionError(f"{sel} sets display on a table cell")


# ── one employer per pass ───────────────────────────────────────────────────

def _person(conn, cid, company):
    from applypilot.networking import store, touches
    store.upsert_contact({"id": cid, "job_url": "j1", "full_name": cid, "company": company,
                          "email": f"{cid}@x.com", "sent_message_id": "m" + cid}, conn)
    touches.set_draft(cid, "email", "s", "body", conn)
    touches.schedule_send(cid, "email", (_now() - timedelta(minutes=5)).isoformat(), conn)


def test_only_ONE_promise_per_employer_is_claimed_in_a_pass(db):
    """Five people at one company got five emails inside TWO SECONDS on the first real batch.
    The daily limit is global and the cooldown is per address, so neither could see it — and both
    are 0 on this machine anyway. The employer is the unit the RECIPIENT experiences."""
    from applypilot.networking import touches
    for i in range(5):
        _person(db, f"acr{i}", "Acrisure")
    now = _now()
    got = touches.claim_due_scheduled(now.isoformat(), sendtime.lapsed_before(now, 24), conn=db)
    assert len(got) == 1, [g["contact_id"] for g in got]


def test_the_ones_held_back_are_NOT_cleared_and_go_on_the_next_pass(db):
    """Throttling at the CLAIM is what makes this safe: an unclaimed promise is still a promise.
    Clearing them would silently drop four of five sends the operator scheduled."""
    from applypilot.networking import touches
    for i in range(3):
        _person(db, f"acr{i}", "Acrisure")
    now = _now()
    seen = []
    for _ in range(3):
        got = touches.claim_due_scheduled(now.isoformat(),
                                          sendtime.lapsed_before(now, 24), conn=db)
        assert len(got) == 1
        seen += [g["contact_id"] for g in got]
    assert sorted(seen) == ["acr0", "acr1", "acr2"], seen
    assert touches.claim_due_scheduled(now.isoformat(),
                                       sendtime.lapsed_before(now, 24), conn=db) == []


def test_DIFFERENT_employers_still_all_go_at_once(db):
    """The throttle is per company, not a global rate limit — spacing unrelated employers would
    delay work for no reason, since no recipient can see across them."""
    from applypilot.networking import touches
    for co in ("Acrisure", "Zapier", "Expedia", "Miro"):
        _person(db, f"p{co}", co)
    now = _now()
    got = touches.claim_due_scheduled(now.isoformat(), sendtime.lapsed_before(now, 24), conn=db)
    assert len(got) == 4, [g["contact_id"] for g in got]


def test_contacts_with_NO_employer_are_not_collapsed_into_one_bucket(db):
    """A target card or an unresolved employer has an empty company. Bucketed together they would
    throttle each other to one per pass despite being unrelated people."""
    from applypilot.networking import touches
    for i in range(3):
        _person(db, f"solo{i}", "")
    now = _now()
    got = touches.claim_due_scheduled(now.isoformat(), sendtime.lapsed_before(now, 24), conn=db)
    assert len(got) == 3, [g["contact_id"] for g in got]


def test_the_earliest_promise_at_an_employer_goes_first(db):
    from applypilot.networking import touches
    for i, mins in ((0, 5), (1, 30), (2, 60)):
        _person(db, f"acr{i}", "Acrisure")
        touches.schedule_send(f"acr{i}", "email",
                              (_now() - timedelta(minutes=mins)).isoformat(), db)
    now = _now()
    got = touches.claim_due_scheduled(now.isoformat(), sendtime.lapsed_before(now, 24), conn=db)
    assert [g["contact_id"] for g in got] == ["acr2"], got
