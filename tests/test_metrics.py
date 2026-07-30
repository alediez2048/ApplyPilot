"""CRM-2 — counting what actually worked.

A CRM that generates but never counts is a very sophisticated mail merge. But a metric that
lies is worse than no metric, so two rules are pinned here rather than left to the UI:

  * every rate carries its `n` and knows whether that `n` means anything;
  * a BOUNCED email is out of the denominator. It never arrived, so counting it as "emailed,
    no reply" understates the true rate while hiding the real problem — a dead address. The
    live DB already had one Affirm address bouncing silently for two weeks.
"""

from __future__ import annotations

import pytest

from applypilot.domain import metrics as m
from applypilot.domain.timeutil import parse_ts


def _c(**over):
    c = {"id": "c1", "job_url": "http://j/1", "company": "Acme", "source": "apollo",
         "sent_message_id": "m1", "submitted_at": "2026-07-01T00:00:00+00:00",
         "email_status": "verified", "confidence": "high", "replied_at": None}
    c.update(over)
    return c


def _many(n, **over):
    return [_c(id=f"c{i}", **over) for i in range(n)]


# ── the small-sample rule ────────────────────────────────────────────────────────────────

def test_a_rate_below_the_threshold_knows_it_is_not_meaningful():
    """"1 of 3" is useful. "33%" from the same three is a lie with a decimal point."""
    r = m.Rate(hits=1, n=3)
    assert r.pct == pytest.approx(33.3, abs=0.1)
    assert r.meaningful is False


def test_a_rate_at_the_threshold_is_meaningful():
    assert m.Rate(hits=1, n=m.MIN_MEANINGFUL_N).meaningful is True


def test_an_empty_rate_does_not_divide_by_zero():
    r = m.Rate(hits=0, n=0)
    assert r.pct == 0.0 and r.meaningful is False


def test_every_rate_reports_its_n_so_the_ui_can_be_honest():
    d = m.Rate(hits=2, n=4, label="warm").as_dict()
    assert d["n"] == 4 and d["hits"] == 2 and d["meaningful"] is False and d["label"] == "warm"


# ── bounces are not failures to reply ────────────────────────────────────────────────────

def test_a_bounced_email_is_out_of_the_denominator():
    """The email never arrived. Counting it as an unanswered send makes outreach look worse
    than it is AND hides the dead address."""
    pool = [_c(id="a", replied_at="2026-07-02T00:00:00+00:00"),
            _c(id="b", email_status="bounced")]
    r = m.reply_rate(pool)
    assert r.n == 1, "the bounced contact stayed in the denominator"
    assert r.hits == 1 and r.pct == 100.0


def test_a_contact_that_was_never_emailed_is_not_in_the_denominator():
    assert m.reply_rate([_c(sent_message_id=None, submitted_at=None)]).n == 0


def test_the_funnel_shows_the_bounce_leak_rather_than_hiding_it():
    """A bounce is a real loss and belongs on the funnel — otherwise "emailed 33, replied 1"
    silently includes sends that never happened."""
    f = m.funnel([{"url": "j1", "applied_at": "x"}],
                 [_c(id="a"), _c(id="b", email_status="bounced")])
    assert f.emailed == 2 and f.bounced == 1
    assert f.as_dict()["bounced"] == 1


# ── the funnel ───────────────────────────────────────────────────────────────────────────

def test_the_funnel_counts_each_stage():
    jobs = [{"url": "j1", "applied_at": "2026-07-01"}, {"url": "j2", "applied_at": None}]
    contacts = [_c(id="a", job_url="j1", replied_at="2026-07-02T00:00:00+00:00"),
                _c(id="b", job_url="j1"), _c(id="c", job_url="j2")]
    f = m.funnel(jobs, contacts)
    assert f.discovered == 2 and f.applied == 1
    assert f.contacted == 2, "contacted counts JOBS with contacts, not contacts"
    assert f.emailed == 3 and f.replied == 1


def test_an_empty_database_produces_zeroes_not_a_crash():
    f = m.funnel([], [])
    assert f.as_dict()["discovered"] == 0
    assert m.summary([], [], [], parse_ts)["overall"]["n"] == 0


# ── the cuts ─────────────────────────────────────────────────────────────────────────────

def test_warm_and_cold_split_on_source():
    contacts = [_c(id="w", source="connection", replied_at="2026-07-02T00:00:00+00:00"),
                _c(id="c1", source="apollo"), _c(id="c2", source="apollo")]
    warm, cold = m.by_layer(contacts)
    assert warm.hits == 1 and warm.n == 1
    assert cold.hits == 0 and cold.n == 2


def test_the_legacy_hunter_source_is_folded_into_cold_not_dropped():
    """Those emails were really sent and really did or did not get answered. Excluding them
    would quietly shrink the only sample big enough to read."""
    warm, cold = m.by_layer([_c(id="h", source="hunter")])
    assert cold.n == 1 and warm.n == 0


def test_confidence_is_split_so_verification_can_be_tuned():
    """The one metric that changes CODE: if unconfirmed contacts never reply, verification
    should reject harder; if they reply as often, it is rejecting too much."""
    contacts = [_c(id="a", confidence="high", replied_at="2026-07-02T00:00:00+00:00"),
                _c(id="b", confidence=""), _c(id="c", confidence="")]
    rows = {r.label: r for r in m.by_confidence(contacts)}
    assert rows["high"].hits == 1 and rows["high"].n == 1
    assert rows["unverified"].hits == 0 and rows["unverified"].n == 2


def test_by_touch_buckets_on_follow_ups_actually_sent():
    """Answers "does the third message earn its place?" — the only way to justify
    FOLLOWUP_SCHEDULE with something other than instinct."""
    contacts = [_c(id="a"), _c(id="b", replied_at="2026-07-05T00:00:00+00:00")]
    touches = [
        {"contact_id": "b", "channel": "email", "sent_at": "2026-07-03"},
        {"contact_id": "b", "channel": "email", "sent_at": "2026-07-04"},
        {"contact_id": "b", "channel": "linkedin", "sent_at": "2026-07-04"},  # wrong channel
        {"contact_id": "a", "channel": "email", "sent_at": None},             # never sent
    ]
    rows = {r.label: r for r in m.by_touch(contacts, touches)}
    assert rows["first email only"].n == 1
    assert rows["+2 follow-ups"].hits == 1


def test_by_company_needs_more_than_one_unanswered_email_to_judge():
    """One unanswered email is not evidence that a company never responds."""
    rows = m.by_company([_c(id="a", company="Solo")])
    assert rows == []
    rows = m.by_company([_c(id="a", company="Duo"), _c(id="b", company="Duo")])
    assert rows and rows[0]["company"] == "Duo" and rows[0]["emailed"] == 2


def test_by_company_surfaces_a_bounce_even_on_a_single_address():
    """A bounce is a fact about the address, not a sample-size question."""
    rows = m.by_company([_c(id="a", company="Affirm", email_status="bounced")])
    assert rows and rows[0]["bounced"] == 1


# ── time to reply ────────────────────────────────────────────────────────────────────────

def test_time_to_reply_is_measured_in_hours():
    c = _c(submitted_at="2026-07-01T00:00:00+00:00", replied_at="2026-07-02T12:00:00+00:00")
    assert m.time_to_reply_hours([c], parse_ts) == [36.0]


def test_a_reply_that_predates_the_send_is_discarded():
    """Clock skew and hand-entered dates produce negative deltas; a negative median would
    quietly corrupt any schedule calibrated from it."""
    c = _c(submitted_at="2026-07-05T00:00:00+00:00", replied_at="2026-07-01T00:00:00+00:00")
    assert m.time_to_reply_hours([c], parse_ts) == []


def test_an_unparseable_timestamp_is_skipped_not_fatal():
    """Older rows are naive; some are junk. One bad row must not take the panel down."""
    good = _c(id="g", submitted_at="2026-07-01T00:00:00+00:00",
              replied_at="2026-07-01T06:00:00+00:00")
    bad = _c(id="b", submitted_at="not-a-date", replied_at="also-not")
    assert m.time_to_reply_hours([bad, good], parse_ts) == [6.0]


def test_median_of_nothing_is_none_not_zero():
    """0 hours would read as "everyone replies instantly"."""
    assert m.median([]) is None
    assert m.median([2.0]) == 2.0
    assert m.median([1.0, 3.0]) == 2.0


# ── the whole payload ────────────────────────────────────────────────────────────────────

def test_summary_is_json_safe_and_complete():
    import json
    contacts = _many(12) + [_c(id="r", replied_at="2026-07-02T00:00:00+00:00")]
    out = m.summary([{"url": "j", "applied_at": "x"}], contacts, [], parse_ts)
    json.dumps(out)  # must not raise
    assert out["overall"]["meaningful"] is True, "13 deliverable contacts should clear the bar"
    assert set(out) >= {"funnel", "overall", "by_layer", "by_confidence", "by_touch",
                        "by_company", "median_hours_to_reply", "min_meaningful_n"}
