"""ARCH-1: the follow-up ladder as a pure domain rule.

These import `applypilot.domain` and nothing else — no web server, no database, no clock
you can't control. Before ARCH-1 the same rules were three separate implementations inside
`web_dashboard.py` that had to agree with each other and had no test forcing them to.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from applypilot.domain import channel_schedule, followup_panel, job_checklist
from applypilot.domain.followup import EMAIL, LINKEDIN, CHANNELS, touch_state

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def emailed(**over) -> dict:
    c = {"id": "c1", "full_name": "Jane", "title": "Recruiter", "email": "j@x.com",
         "emailed": True, "submitted_at": ago(days=5), "followup_count": 0,
         "followup_status": "", "followed_up_at": "", "linkedin_url": "", "dm_status": ""}
    c.update(over)
    return c


def connected(**over) -> dict:
    c = {"id": "c2", "full_name": "Sumit", "title": "Recruiter", "email": "",
         "emailed": False, "linkedin_url": "https://l/in/s", "dm_status": "manual",
         "dm_sent_at": ago(days=7), "li_followup_count": 0, "li_followup_status": "",
         "li_followed_up_at": ""}
    c.update(over)
    return c


# ── the engine is one implementation, parameterised ─────────────────────────

def test_both_channels_use_the_same_engine():
    """If this ever needs a branch per channel, ARCH-1 has regressed."""
    for ch in CHANNELS:
        state, _ = touch_state(emailed() if ch is EMAIL else connected(),
                               ch, channel_schedule(ch), NOW)
        assert state == "due"


def test_email_and_linkedin_have_different_default_ladders():
    """LinkedIn is deliberately slower — nudging a new connection after 48h reads badly."""
    assert channel_schedule(EMAIL) == [48, 96, 168]
    assert channel_schedule(LINKEDIN) == [120, 288]


@pytest.mark.parametrize("channel,env", [(EMAIL, "FOLLOWUP_SCHEDULE"),
                                         (LINKEDIN, "LINKEDIN_FOLLOWUP_SCHEDULE")])
def test_schedule_is_configurable_and_survives_garbage(channel, env, monkeypatch):
    monkeypatch.setenv(env, "24, 48 ,72")
    assert channel_schedule(channel) == [24, 48, 72]
    monkeypatch.setenv(env, "not-a-number")          # falls back, never crashes
    assert channel_schedule(channel) == list(channel.default_schedule)


# ── due / waiting / finished / terminal ─────────────────────────────────────

def test_not_due_before_the_window():
    state, hours = touch_state(emailed(submitted_at=ago(hours=10)), EMAIL,
                               channel_schedule(EMAIL), NOW)
    assert state == "waiting" and hours == 38


def test_clock_runs_from_the_most_recent_touch_not_the_first():
    """After touch 1, the next is due 96h from THAT touch, not from the original email."""
    c = emailed(submitted_at=ago(days=20), followup_count=1, followed_up_at=ago(hours=10))
    state, hours = touch_state(c, EMAIL, channel_schedule(EMAIL), NOW)
    assert state == "waiting" and hours == 86


def test_ladder_finishes_after_the_last_touch():
    c = emailed(followup_count=3, followed_up_at=ago(days=30))
    assert touch_state(c, EMAIL, channel_schedule(EMAIL), NOW)[0] == "finished"


@pytest.mark.parametrize("status", ["replied", "stopped"])
def test_terminal_status_halts_the_ladder(status):
    c = emailed(followup_status=status, submitted_at=ago(days=90))
    assert touch_state(c, EMAIL, channel_schedule(EMAIL), NOW)[0] == status


def test_channel_needs_a_first_message_to_schedule_anything():
    # emailed=False -> the email ladder does not apply at all
    assert touch_state(emailed(emailed=False), EMAIL, channel_schedule(EMAIL), NOW)[0] == ""
    # a LinkedIn profile with no RECORDED invite has no anchor
    assert touch_state(connected(dm_status="", dm_sent_at=""), LINKEDIN,
                       channel_schedule(LINKEDIN), NOW)[0] == ""


def test_naive_timestamps_do_not_crash():
    """Older rows have no timezone; this once 500'd the whole /api/status endpoint."""
    c = emailed(submitted_at="2026-07-22 18:50:17")
    assert touch_state(c, EMAIL, channel_schedule(EMAIL), NOW)[0] in ("due", "waiting")


# ── the panel ───────────────────────────────────────────────────────────────

def test_panel_separates_the_two_channels():
    panel = followup_panel([emailed(), connected()], now=NOW)
    assert panel["due_count"] == 1 and panel["li_due_count"] == 1
    assert panel["due"][0]["full_name"] == "Jane"
    assert panel["li_due"][0]["full_name"] == "Sumit"


def test_ladders_are_independent():
    """One person can owe a LinkedIn message while their email ladder is done."""
    both = emailed(**{"linkedin_url": "https://l/in/j", "dm_status": "manual",
                      "dm_sent_at": ago(days=9), "li_followup_count": 0,
                      "li_followup_status": "", "li_followed_up_at": "",
                      "followup_status": "replied"})
    panel = followup_panel([both], now=NOW)
    assert panel["due_count"] == 0        # email stopped on reply
    assert panel["li_due_count"] == 1     # LinkedIn still owed


def test_panel_annotates_contacts_for_the_per_contact_buttons():
    c = emailed()
    followup_panel([c], now=NOW)
    assert c["followup_state"] == "due" and c["followup_touch"] == 1


def test_panel_does_not_own_followup_due():
    """`followup_due` belongs to the checklist, which uses a different rule.

    Setting it in the panel silently overwrote the checklist's answer for anyone already
    followed up once — caught only by the byte-identical /api/status check during ARCH-1.
    """
    c = emailed(followup_count=1, followed_up_at=ago(hours=1))
    job_checklist("applied", ago(days=6), [c], now=NOW)
    before = c["followup_due"]
    followup_panel([c], now=NOW)
    assert c["followup_due"] is before


# ── checklist ───────────────────────────────────────────────────────────────

def test_checklist_excludes_zero_denominator_steps():
    """A job with no emailable contacts must still be able to reach 100%."""
    cl = job_checklist("applied", ago(days=1), [connected()], now=NOW)
    states = {s["key"]: s["state"] for s in cl["steps"]}
    assert states["emailed"] == "na"
    assert cl["pct"] == 100 and cl["complete"] is True


def test_checklist_counts_a_due_followup_against_you():
    cl = job_checklist("applied", ago(days=6), [emailed()], now=NOW)
    assert cl["followups_due"] == 1 and cl["complete"] is False
