"""The ordered plan: 3 emails → text + call → text + call again.

Asked for as three things that turn out to be one computation — the sequence, the Summary tab,
and "I should not close out a job application without having texted and called some of the
people whose contacts we found".

Pure domain: dicts in, dicts out, no database and no clock you cannot control.

The measurement that shaped the close guard, taken on the live database before it was written:
only **8 of 373 contacts carry a phone number**, so 24 of 36 jobs warn — and every one of them
warns truthfully, because nobody was texted for want of a number to text. A sentence that names
no fix is one the operator trains themselves past, so that case names the step (copy a direct
dial out of Apollo's own UI, which is the only place it exists — §Lessons 4).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from applypilot.domain.coverage import close_warning, contact_coverage, job_coverage
from applypilot.domain.followup import EMPTY_LADDER

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def ladder(count=0, last="", status=""):
    return {**EMPTY_LADDER, "count": count, "last_sent_at": last, "sequence_status": status}


def person(**over):
    c = {"id": "c1", "full_name": "Diego Bodart", "email": "d@bigco.test",
         "phone": "+1 512 555 0100", "linkedin_url": "", "dm_status": "",
         "emailed": False, "submitted_at": "", "sms_sent_at": "", "call_made_at": "",
         "replied_at": ""}
    c.update(over)
    return c


def cov(c, **ladders):
    return contact_coverage(c, {("c1", k): v for k, v in ladders.items()}, now=NOW)


# ── the order, which is the whole request ───────────────────────────────────

def test_a_fresh_contact_starts_at_EMAILS(tmp_path=None):
    got = cov(person())
    assert got["stage"]["key"] == "email"
    assert got["next"]["what"] == "send the first email"


def test_text_and_call_do_NOT_light_up_while_the_email_ladder_is_running():
    """The point of an ordered plan. Today each channel runs its own ladder unaware of the
    others, so a person can show three things due at once with nothing saying which is first."""
    got = cov(person(emailed=True, submitted_at=ago(days=1)), email=ladder(count=0))
    assert got["stage"]["key"] == "email"
    assert got["next"] is None, "the plan jumped ahead while emails were still in flight"


def test_the_reach_stage_opens_once_the_emails_are_SPENT():
    got = cov(person(emailed=True, submitted_at=ago(days=30)),
              email=ladder(count=3, last=ago(days=10)))       # ladder finished
    assert got["stage"]["key"] == "reach"
    assert got["next"]["what"] == "text and call them"


def test_a_started_pair_asks_for_the_MISSING_half():
    got = cov(person(emailed=True, submitted_at=ago(days=30), sms_sent_at=ago(days=1)),
              email=ladder(count=3, last=ago(days=10)))
    assert got["next"]["what"] == "call them"


def test_the_SECOND_text_and_call_come_due_after_three_days():
    """"There should be at least a highlight for a second text/call after 3 days if we don't get
    a response." Both ladders are 72h, so this is the stage the request named."""
    both = person(emailed=True, submitted_at=ago(days=30),
                  sms_sent_at=ago(days=4), call_made_at=ago(days=4))
    got = cov(both, email=ladder(count=3, last=ago(days=20)))
    assert got["stage"]["key"] == "close"
    assert got["next"] is not None and "again" in got["next"]["what"]


def test_two_days_later_is_NOT_yet_due():
    """The negative control: without it, a stage that always reads `due` passes the test above."""
    both = person(emailed=True, submitted_at=ago(days=30),
                  sms_sent_at=ago(days=2), call_made_at=ago(days=2))
    got = cov(both, email=ladder(count=3, last=ago(days=20)))
    assert got["next"] is None


def test_a_REPLY_ends_the_plan_rather_than_advancing_it():
    """Not a stage — an ending. Chasing somebody who answered is the one follow-up guaranteed to
    cost something, and a counter full of work you have decided not to do is one you stop
    reading (CRM-3a)."""
    got = cov(person(emailed=True, submitted_at=ago(days=30), replied_at=ago(days=1)),
              email=ladder(count=1))
    assert got["stage"]["key"] == "replied"
    assert got["next"] is None


def test_somebody_with_only_a_PHONE_starts_at_text_and_call():
    """A stage that cannot run must not block the ones after it, or a person with no address
    waits forever on an email that can never be sent."""
    got = cov(person(email=""))
    assert got["stage"]["key"] == "reach"


def test_somebody_with_no_number_is_BLOCKED_and_says_what_is_missing():
    got = cov(person(phone="", emailed=True, submitted_at=ago(days=30)),
              email=ladder(count=3, last=ago(days=10)))
    assert got["stage"]["key"] == "blocked"
    assert "phone number" in got["next"]["what"]


# ── the counts the Summary tab shows ────────────────────────────────────────

def test_the_first_message_counts_as_one():
    """`sent` is the anchor plus its touches. Counting only `touches` reports ZERO for somebody
    who has been emailed once, which is most of the live database."""
    got = cov(person(emailed=True, submitted_at=ago(days=10)), email=ladder(count=2))
    assert got["emails"] == 3
    got2 = cov(person(sms_sent_at=ago(days=10)), sms=ladder(count=1))
    assert got2["texts"] == 2


def test_a_channel_never_started_counts_zero_not_one():
    assert cov(person())["emails"] == 0
    assert cov(person())["calls"] == 0


# ── the close guard ─────────────────────────────────────────────────────────

def _job(people, **ladders):
    return job_coverage(people, ladders, now=NOW)


def test_it_names_who_is_unspent_per_channel():
    """"You have not finished working this" is not actionable; "4 of 6 never texted" is."""
    ppl = [person(id=f"c{i}", full_name=f"P{i}", emailed=True, submitted_at=ago(days=30))
           for i in range(4)]
    ppl[0]["sms_sent_at"] = ago(days=2)
    w = close_warning(_job(ppl))
    assert w["warn"]
    assert "3 of 4 never texted" in w["lines"]
    assert "4 of 4 never called" in w["lines"]


def test_people_with_NO_NUMBER_are_not_counted_as_untexted():
    """Somebody unreachable is not an unworked person, and folding the two together puts a
    permanent warning on every job — the shape §Lessons 98 caught in the sheet import."""
    ppl = [person(id="c1", phone=""), person(id="c2", phone="")]
    cv = _job(ppl)
    assert cv["with_phone"] == 0
    assert cv["unworked"]["texted"] == []


def test_the_no_number_case_names_the_FIX():
    """Live: 8 of 373 contacts have a number, so this fires on 24 of 36 jobs. True every time,
    and useless unless it says what to do about it."""
    w = close_warning(_job([person(id="c1", phone=""), person(id="c2", phone="")]))
    assert w["warn"]
    assert "Apollo" in w["lines"][0], "the commonest warning names no way out of itself"


def test_a_REPLY_silences_the_close_guard():
    """The outreach was for a conversation and one happened. Nagging about an unmade phone call
    on a job where somebody wrote back is the alarm that teaches you to ignore alarms."""
    ppl = [person(id="c1", emailed=True, replied_at=ago(days=1)), person(id="c2")]
    assert close_warning(_job(ppl))["warn"] is False


def test_a_job_with_nobody_on_it_never_warns():
    assert close_warning(_job([]))["warn"] is False


def test_a_fully_worked_job_is_silent():
    ppl = [person(id="c1", emailed=True, submitted_at=ago(days=30),
                  sms_sent_at=ago(days=5), call_made_at=ago(days=5))]
    assert close_warning(_job(ppl))["warn"] is False


def test_the_totals_are_summed_across_people():
    ppl = [person(id="c1", emailed=True, submitted_at=ago(days=9)),
           person(id="c2", emailed=True, submitted_at=ago(days=9), call_made_at=ago(days=1))]
    cv = _job(ppl)
    assert (cv["people"], cv["emails"], cv["calls"]) == (2, 2, 1)
