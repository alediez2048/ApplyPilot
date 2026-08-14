"""CAL-1 — sending a Google Calendar invite from a contact card.

The most outward-facing thing this app does: it puts an entry on a stranger's calendar and mails
them about it, with no draft state to review afterwards. So the tests here are mostly about
refusing, and about the two properties that are easy to get wrong invisibly.

**The scope must stay opt-in.** `calendar.events` is the first non-Gmail scope this project has
asked for, and the ordinary `--gmail-connect` must never quietly start requesting it — the same
rule CRM-4b established for `gmail.readonly`.

**The send guards must be checked HERE.** `sendUpdates=all` means GOOGLE mails the invitation,
so this never passes through `gmail_send` and the daily limit, per-company cap and address
cooldown would never see it. §Lessons 77 is that shape exactly: `sent_today()` counted first
contacts only while gating three separate doors.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import applypilot.database as database
from applypilot.domain import invite as inv
from applypilot.networking import gmail_oauth, store

NOW = datetime(2026, 8, 14, 9, 0)


def when(days=3, t="14:00"):
    return (NOW + timedelta(days=days)).strftime("%Y-%m-%d"), t


def ok_args(**over):
    d, t = when()
    a = {"title": "Alejandro × Dana", "day": d, "time": t, "duration": 30,
         "attendees": ["dana@bigco.test"], "tz": "America/Chicago", "now": NOW}
    a.update(over)
    return a


# ── the scope stays opt-in ──────────────────────────────────────────────────

def test_calendar_is_NOT_in_the_ordinary_scope_list():
    """The whole guarantee. In SCOPES, every future `--gmail-connect` silently asks a Google
    consent screen for calendar write access."""
    assert gmail_oauth.CALENDAR_SCOPE not in gmail_oauth.SCOPES
    assert gmail_oauth.CONTENT_SCOPE not in gmail_oauth.SCOPES


def test_it_is_the_NARROW_calendar_scope():
    """Full `calendar` grants READ of every event on every calendar the account can see, which
    for a work account is the organisation's meeting habits. `events` can create and delete and
    cannot browse."""
    assert gmail_oauth.CALENDAR_SCOPE.endswith("/auth/calendar.events")


def test_reconnecting_for_calendar_does_not_DROP_reply_reading(monkeypatch):
    """Re-running connect REPLACES the token. Without carrying held scopes forward,
    `--with-calendar` alone would silently revoke `gmail.readonly` and every conversation would
    quietly stop showing text, with nothing saying why."""
    monkeypatch.setattr(gmail_oauth, "granted_scopes", lambda: [gmail_oauth.CONTENT_SCOPE])
    seen = {}

    class _Flow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            seen["scopes"] = scopes
            raise RuntimeError("stop here — the scope list is what is under test")

    monkeypatch.setattr(gmail_oauth, "_libs", lambda: (None, None, _Flow, None))
    monkeypatch.setattr(type(gmail_oauth.CLIENT_SECRET_PATH), "exists", lambda self: True)
    gmail_oauth.connect(with_calendar=True)
    assert gmail_oauth.CONTENT_SCOPE in seen["scopes"], "reconnecting dropped gmail.readonly"
    assert gmail_oauth.CALENDAR_SCOPE in seen["scopes"]


# ── what the domain refuses ─────────────────────────────────────────────────

def test_a_time_in_the_past_is_refused():
    """Almost always a mistyped year, and unrecoverable: the invitation goes out the instant the
    event is created."""
    d, t = when(days=-2)
    got = inv.validate(**ok_args(day=d, time=t))
    assert got["ok"] is False and "past" in got["error"]


def test_a_year_out_is_refused_as_a_typo():
    d, _ = when(days=500)
    got = inv.validate(**ok_args(day=d))
    assert got["ok"] is False and "check the year" in got["error"]


def test_no_attendee_is_refused():
    got = inv.validate(**ok_args(attendees=[]))
    assert got["ok"] is False and "nobody to invite" in got["error"]


@pytest.mark.parametrize("bad", ["", "   "])
def test_an_empty_title_is_refused(bad):
    assert inv.validate(**ok_args(title=bad))["ok"] is False


def test_an_offset_is_refused_as_a_TIME_ZONE():
    """"-05:00" is correct today and wrong after a DST change. Google wants IANA, and a meeting
    an hour out is worse than no meeting."""
    got = inv.validate(**ok_args(tz="-05:00"))
    assert got["ok"] is False and "IANA" in got["error"]


def test_a_valid_invite_computes_its_own_END():
    got = inv.validate(**ok_args(duration=45))
    assert got["ok"]
    assert got["start"].endswith("T14:00:00") and got["end"].endswith("T14:45:00")
    assert got["tz"] == "America/Chicago"


def test_the_start_carries_NO_offset():
    """`dateTime` + `timeZone` is how Google wants a local time. Building an offset ourselves
    bakes in today's DST."""
    got = inv.validate(**ok_args())
    assert "+" not in got["start"] and "Z" not in got["start"]


# ── the event body ──────────────────────────────────────────────────────────

def test_a_meet_link_is_REQUESTED_when_asked_for():
    b = inv.event_body(title="t", start="2026-08-17T14:00:00", end="2026-08-17T14:30:00",
                       tz="America/Chicago", attendees=["a@b.test"], meet=True)
    assert b["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"
    assert b["conferenceData"]["createRequest"]["requestId"]


def test_no_meet_means_no_conference_block():
    b = inv.event_body(title="t", start="2026-08-17T14:00:00", end="2026-08-17T14:30:00",
                       tz="America/Chicago", attendees=["a@b.test"], meet=False)
    assert "conferenceData" not in b


def test_conferenceDataVersion_is_sent_whenever_a_conference_is_requested(monkeypatch):
    """The single easiest thing to omit. Without it Google ACCEPTS `conferenceData` and silently
    drops it: the event is created, the invitation goes out, and there is no Meet link — a
    success response for a half-made meeting."""
    from applypilot.networking import calendar_send
    seen = {}

    class _Events:
        def insert(self, **kw):
            seen.update(kw)
            return type("R", (), {"execute": lambda s: {"id": "e1", "hangoutLink": "https://m"}})()

    monkeypatch.setattr(calendar_send, "_service",
                        lambda: (type("S", (), {"events": lambda s: _Events()})(), ""))
    body = inv.event_body(title="t", start="2026-08-17T14:00:00", end="2026-08-17T14:30:00",
                          tz="America/Chicago", attendees=["a@b.test"], meet=True)
    calendar_send.create(body)
    assert seen["conferenceDataVersion"] == 1
    assert seen["sendUpdates"] == "all", "Google is not being asked to email the invitation"


def test_the_agenda_reaches_the_event():
    b = inv.event_body(title="t", start="2026-08-17T14:00:00", end="2026-08-17T14:30:00",
                       tz="America/Chicago", attendees=["a@b.test"], agenda="what to cover")
    assert b["description"] == "what to cover"


# ── the endpoint: guards, and what it stores ────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    # Created UP FRONT so "nothing was stored" is a real assertion. Left absent, the failed-send
    # test passes because the table does not exist, whatever the code does — an assertion that
    # cannot fail when the thing under test is emptied (§Lessons 71).
    from applypilot.networking import interactions_store
    interactions_store.init_interactions(conn)
    from applypilot import web_dashboard as wd
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: conn)
    return conn


def _contact(conn, **kw):
    row = {"job_url": "http://j/1", "full_name": "Dana Okafor", "email": "dana@bigco.test",
           "company": "BigCo", "email_status": "verified"}
    row.update(kw)
    return store.upsert_contact(row, conn)


def _stub_calendar(monkeypatch, result=None):
    from applypilot.networking import calendar_send
    calls = []

    def _create(body, notify=True):
        calls.append(body)
        return result or {"ok": True, "error": "", "id": "ev1",
                          "link": "https://cal/ev1", "meet": "https://meet/xyz"}
    monkeypatch.setattr(calendar_send, "create", _create)
    return calls


def test_the_daily_limit_is_checked_even_though_GOOGLE_sends_the_mail(db, monkeypatch):
    """§Lessons 77's shape. `sendUpdates=all` bypasses `gmail_send` entirely, so a guard that is
    merely inherited is a guard that never runs on this path."""
    from applypilot import web_dashboard as wd
    from applypilot.networking import gmail_send
    calls = _stub_calendar(monkeypatch)
    cid = _contact(db)
    monkeypatch.setattr(gmail_send, "can_send",
                        lambda c, confirm_unverified=False: (False, "daily send limit reached (20)"))
    d, t = when()
    got = wd._send_invite({"contact_id": cid, "title": "x", "day": d, "time": t, "duration": 30})
    assert got["ok"] is False and "daily send limit" in got["message"]
    assert calls == [], "an event was created despite the send limit"


def test_the_two_refusals_that_do_NOT_apply_are_let_through(db, monkeypatch):
    """"Already sent to this contact" and the cross-role cooldown are about COLD outreach. An
    invite goes to somebody mid-conversation — refusing there would block the exact person this
    feature exists for."""
    from applypilot import web_dashboard as wd
    from applypilot.networking import gmail_send
    _stub_calendar(monkeypatch)
    cid = _contact(db)
    d, t = when()
    for why in ("already sent to this contact", "already emailed dana@bigco.test for another role"):
        monkeypatch.setattr(gmail_send, "can_send", lambda c, confirm_unverified=False, w=why: (False, w))
        got = wd._send_invite({"contact_id": cid, "title": "x", "day": d, "time": t,
                               "duration": 30})
        assert got["ok"] is True, f"{why!r} blocked an invite: {got['message']}"


def test_the_attendee_comes_from_the_STORED_contact_not_the_page(db, monkeypatch):
    """§Lessons 29: the dangerous half of an outward-facing action is the half that looks
    identical when it is wrong. A `to` accepted from the browser is how an invite reaches
    somebody nobody chose."""
    from applypilot import web_dashboard as wd
    from applypilot.networking import gmail_send
    monkeypatch.setattr(gmail_send, "can_send", lambda c, confirm_unverified=False: (True, "ok"))
    calls = _stub_calendar(monkeypatch)
    cid = _contact(db)
    d, t = when()
    wd._send_invite({"contact_id": cid, "title": "x", "day": d, "time": t, "duration": 30,
                     "attendees": ["attacker@evil.test"], "to": "attacker@evil.test"})
    assert [a["email"] for a in calls[0]["attendees"]] == ["dana@bigco.test"]


def test_the_event_id_is_STORED_so_it_can_be_cancelled(db, monkeypatch):
    """An invite you can send and not withdraw is half a feature, and the half you need in a
    hurry. Cancelling needs the id, so it is written before anything else can go wrong."""
    from applypilot import web_dashboard as wd
    from applypilot.networking import gmail_send
    monkeypatch.setattr(gmail_send, "can_send", lambda c, confirm_unverified=False: (True, "ok"))
    _stub_calendar(monkeypatch)
    cid = _contact(db)
    d, t = when()
    assert wd._send_invite({"contact_id": cid, "title": "Intro", "day": d, "time": t,
                            "duration": 30})["ok"]
    row = db.execute("SELECT detail FROM interactions WHERE contact_id=? AND kind='invited'",
                     (cid,)).fetchone()
    assert row and "ev1" in row["detail"]
    got = wd._upcoming_invites([cid], db)
    assert got[cid]["event_id"] == "ev1" and got[cid]["meet"] == "https://meet/xyz"


def test_a_failed_create_stores_NOTHING(db, monkeypatch):
    """Otherwise the card claims a meeting exists and offers to cancel one that does not."""
    from applypilot import web_dashboard as wd
    from applypilot.networking import gmail_send
    monkeypatch.setattr(gmail_send, "can_send", lambda c, confirm_unverified=False: (True, "ok"))
    _stub_calendar(monkeypatch, {"ok": False, "error": "Google said no", "id": "", "link": "",
                                 "meet": ""})
    cid = _contact(db)
    d, t = when()
    assert wd._send_invite({"contact_id": cid, "title": "x", "day": d, "time": t,
                            "duration": 30})["ok"] is False
    assert db.execute("SELECT COUNT(*) FROM interactions WHERE contact_id=?",
                      (cid,)).fetchone()[0] == 0


def test_a_bad_TIME_never_reaches_google(db, monkeypatch):
    from applypilot import web_dashboard as wd
    from applypilot.networking import gmail_send
    monkeypatch.setattr(gmail_send, "can_send", lambda c, confirm_unverified=False: (True, "ok"))
    calls = _stub_calendar(monkeypatch)
    cid = _contact(db)
    got = wd._send_invite({"contact_id": cid, "title": "x", "day": "2020-01-01",
                           "time": "09:00", "duration": 30})
    assert got["ok"] is False and calls == []


def test_cancelling_an_event_google_already_lost_is_a_SUCCESS(monkeypatch):
    """An event the operator deleted in Google's own UI is gone, which is the state they asked
    for. Reporting failure leaves a stale row on the card that nothing can clear."""
    from applypilot.networking import calendar_send

    class _Err(Exception):
        resp = type("R", (), {"status": 410})()

    class _Events:
        def delete(self, **kw):
            raise _Err()

    monkeypatch.setattr(calendar_send, "_service",
                        lambda: (type("S", (), {"events": lambda s: _Events()})(), ""))
    got = calendar_send.cancel("ev1")
    assert got["ok"] is True and "already gone" in got["note"]


def test_without_the_scope_it_refuses_with_the_COMMAND(monkeypatch):
    """Naming the fix is the whole point: the alternative is filling in a meeting and hitting a
    403 nobody can read."""
    from applypilot.networking import calendar_send
    monkeypatch.setattr(gmail_oauth, "available", lambda: True)
    monkeypatch.setattr(gmail_oauth, "can_send_invites", lambda: False)
    svc, why = calendar_send._service()
    assert svc is None
    assert "--with-calendar" in why
