"""How an application is DOING, as opposed to how far it has travelled.

The status strip counts distance — Found → Applied → Emailed 4/4 → Follow up 2/4 — and every
number in it counts work the OPERATOR did. Two jobs reading exactly that can be a live
conversation and a dead one.

**The trap is written down.** §Lessons 35: the first Interactions tab counted our own LinkedIn
invites as engagement, so three live jobs read "3/3 engaged" before anyone had done anything,
while the honest number across every job was 2 of 58. A temperature built on effort is that bug
with a colour on it, and worse, because a colour is read at a glance and trusted.

Only their actions raise it. Ours can only lower it. The first test is that rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from applypilot.domain import interactions as ix
from applypilot.domain import temperature as tmp

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _c(cid="c1", name="Sarah Chen", **over):
    c = {"id": cid, "full_name": name, "interactions": [], "submitted_at": "",
         "email_status": "verified"}
    c.update(over)
    return c


def _job(**over):
    j = {"interview_at": "", "applied_at": _at(30)}
    j.update(over)
    return j


def _band(job=None, contacts=None, ladders=None):
    return tmp.temperature(job or _job(), contacts or [], ladders or {}, now=NOW)["band"]


# ── the rule this module exists to enforce ─────────────────────────────────

def test_our_own_effort_never_raises_the_temperature():
    """The §Lessons 35 mutation, as a test. Twelve messages and silence must read COLDER than
    one message and an answer. Any design where these compare the other way round is wrong
    however the constants are tuned."""
    shouting = [_c(submitted_at=_at(25))]
    ladders = {("c1", "email"): {"count": 11}}
    answered = [_c(cid="c2", submitted_at=_at(25),
                   interactions=[{"kind": ix.REPLIED, "at": _at(2)}])]

    loud = _band(contacts=shouting, ladders=ladders)
    heard = _band(contacts=answered)
    order = [tmp.COLD, tmp.COOLING, tmp.ACTIVE, tmp.WARM]
    assert order.index(loud) < order.index(heard), (
        f"12 unanswered messages ({loud}) reads warmer than 1 reply ({heard})")


def test_more_unanswered_effort_only_ever_cools():
    quiet = _band(contacts=[_c(submitted_at=_at(20))], ladders={("c1", "email"): {"count": 0}})
    louder = _band(contacts=[_c(submitted_at=_at(20))], ladders={("c1", "email"): {"count": 4}})
    order = [tmp.COLD, tmp.COOLING, tmp.ACTIVE, tmp.WARM]
    assert order.index(louder) <= order.index(quiet)


# ── their actions ───────────────────────────────────────────────────────────

def test_a_recent_reply_is_warm():
    assert _band(contacts=[_c(interactions=[{"kind": ix.REPLIED, "at": _at(1)}])]) == tmp.WARM


def test_a_reply_beats_any_amount_of_silence_elsewhere():
    contacts = [_c(cid="a", submitted_at=_at(30)),
                _c(cid="b", submitted_at=_at(30),
                   interactions=[{"kind": ix.REPLIED, "at": _at(1)}])]
    assert _band(contacts=contacts, ladders={("a", "email"): {"count": 5}}) == tmp.WARM


def test_it_decays():
    """The same reply at 2, 15 and 40 days lands in three different bands. Without this the
    reading is a permanent badge for anything that ever got an answer."""
    def at(d):
        return _band(contacts=[_c(interactions=[{"kind": ix.REPLIED, "at": _at(d)}])])
    assert at(2) == tmp.WARM
    assert at(15) == tmp.ACTIVE
    assert at(40) == tmp.COOLING


def test_a_deck_open_counts_as_them():
    """A click, not an open. `domain/deck.py` is why that distinction is trustworthy."""
    assert _band(contacts=[_c(interactions=[{"kind": ix.DECK, "at": _at(1)}])]) == tmp.WARM


def test_our_own_send_is_not_an_action_of_theirs():
    """`SENT` and `CONNECTED` are in the timeline so it can be read, never as signals."""
    for kind in (ix.SENT, ix.CONNECTED, ix.LINKEDIN_OUT):
        band = _band(contacts=[_c(submitted_at=_at(20),
                                  interactions=[{"kind": kind, "at": _at(1)}])])
        assert band in (tmp.COOLING, tmp.COLD), f"{kind} warmed the reading"


def test_the_engagement_line_is_not_redefined_here():
    """A signal cannot be engagement in one module and our own action in another. A third list
    of "what counts as them" is the copy that falls behind."""
    for kind in ix.ENGAGEMENT:
        assert _band(contacts=[_c(interactions=[{"kind": kind, "at": _at(1)}])]) == tmp.WARM


# ── terminal states, which are not temperatures ─────────────────────────────

def test_an_interview_is_won_not_warm():
    assert _band(job=_job(interview_at=_at(1))) == tmp.WON


def test_a_bounce_is_undeliverable_not_cold():
    """Opposite fixes. "Nobody is answering" means write better email; "nothing is arriving"
    means fix the address. Calling the second one cold hides the thing to correct."""
    assert _band(contacts=[_c(submitted_at=_at(5), email_status="bounced")]) == tmp.UNDELIVERABLE


def test_one_bounce_among_several_is_not_undeliverable():
    """Someone else may still answer. Only when EVERY address is rejected is the job itself
    undeliverable."""
    contacts = [_c(cid="a", submitted_at=_at(5), email_status="bounced"),
                _c(cid="b", submitted_at=_at(5), email_status="verified")]
    assert _band(contacts=contacts) != tmp.UNDELIVERABLE


def test_nothing_sent_yet_is_new_not_cold():
    """A job imported this morning is not failing. Reading it as cold would light the whole
    table amber on day one and train the operator to ignore the colour."""
    assert _band(contacts=[]) == tmp.NEW
    assert _band(contacts=[_c()]) == tmp.NEW


# ── every band explains itself ──────────────────────────────────────────────

def test_every_band_states_its_reason():
    """A band with no explanation is a colour, and a colour nobody can interrogate stops being
    read within a week — §Lessons 43 applied to information rather than to controls."""
    cases = [
        (_job(interview_at=_at(1)), [], {}),
        (_job(), [_c(submitted_at=_at(5), email_status="bounced")], {}),
        (_job(), [_c(interactions=[{"kind": ix.REPLIED, "at": _at(1)}])], {}),
        (_job(), [_c(interactions=[{"kind": ix.REPLIED, "at": _at(15)}])], {}),
        (_job(), [_c(interactions=[{"kind": ix.REPLIED, "at": _at(40)}])], {}),
        (_job(), [_c(submitted_at=_at(30))], {("c1", "email"): {"count": 4}}),
        (_job(), [], {}),
    ]
    seen = set()
    for job, contacts, ladders in cases:
        out = tmp.temperature(job, contacts, ladders, now=NOW)
        seen.add(out["band"])
        assert out["reason"].strip(), f"{out['band']} has no reason"
        assert out["reason"].endswith("."), f"{out['band']} reason is not a sentence"
        assert out["icon"] and out["label"], "colour is the only channel"
    assert len(seen) >= 5, f"the cases collapse into {len(seen)} bands: {seen}"


def test_the_reason_names_the_person_who_acted():
    out = tmp.temperature(_job(), [_c(name="Sarah Chen",
                                      interactions=[{"kind": ix.REPLIED, "at": _at(1)}])],
                          {}, now=NOW)
    assert "Sarah" in out["reason"]


def test_a_naive_timestamp_does_not_raise():
    """§Lessons 6 — older rows have no timezone and subtracting from an aware now raises,
    500ing the whole dashboard."""
    out = tmp.temperature(_job(), [_c(interactions=[{"kind": ix.REPLIED, "at": "2026-08-01 10:00:00"}])],
                          {}, now=NOW)
    assert out["band"]


def test_it_is_pure():
    import inspect
    src = inspect.getsource(tmp)
    for forbidden in ("get_connection", "execute(", "sqlite3", "httpx", "requests"):
        assert forbidden not in src, f"{forbidden} — this must be pure"


def test_it_reaches_the_payload_and_a_rejected_job_has_none(tmp_path, monkeypatch):
    """A rejected job has left the pipeline. A reading on it would be permanently lit, which
    is the failure the 🔔 counter already had to design around."""
    import applypilot.database as database
    from applypilot import web_dashboard as wd

    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    conn.execute("INSERT INTO jobs (url, title, site, strategy, applied_at) VALUES (?,?,?,?,?)",
                 ("http://j/live", "PM", "Greenhouse", "dashboard_upload", "2026-07-20T10:00:00+00:00"))
    conn.execute("INSERT INTO jobs (url, title, site, strategy, rejected_at) VALUES (?,?,?,?,?)",
                 ("http://j/dead", "PM", "Greenhouse", "dashboard_upload", "2026-08-01T10:00:00+00:00"))
    conn.commit()

    jobs = {j["url"]: j for j in wd._status_payload()["jobs"]}
    assert jobs, "empty payload — this test would measure nothing (§Lessons 13)"
    assert jobs["http://j/live"]["temperature"]["band"], "no reading on a live job"
    assert jobs["http://j/dead"]["temperature"] is None, "a rejected job still carries a reading"
