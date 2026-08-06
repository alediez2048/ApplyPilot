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


# ── runway: the fix for "recent jobs are marked cooling" ────────────────────
#
# Reported 2026-08-04. COOLING was the FALLBACK for every job with any effort and no reply —
# there was no band between "nothing sent" and "cold" — so on the live board it caught 10 of
# 22 jobs, including Visa, applied that morning with 6 of 8 emails already out. A band that
# covers half the table and means "decaying" is worse than no band at all.

def _plan(emailed=(0, 0), followup=(0, 0), linkedin=(0, 0), due=0, touches=3):
    def step(key, pair):
        done, total = pair
        state = "na" if not total else ("done" if done >= total else ("partial" if done else "todo"))
        return {"key": key, "done": done, "total": total, "state": state}
    return {"due": due, "total_touches": touches,
            "steps": [step("emailed", emailed), step("linkedin", linkedin),
                      step("followup", followup)]}


def _people(total, emailed=None, touches=0, last=0.0, stopped=0):
    """`total` contacts WITH AN ADDRESS, `emailed` of whom have been written to, each carrying
    `touches` follow-ups already sent.

    Runway is derived from contacts and ladders now, not asserted in a plan tuple, so a fixture
    has to be internally consistent the way a real job is. The old helper let a test claim
    "6 of 8 emailed" while passing a single contact — harmless when the plan WAS the input, and
    meaningless once the contacts are.
    """
    emailed = total if emailed is None else emailed
    contacts, ladders = [], {}
    for i in range(total):
        cid = f"c{i + 1}"
        was_emailed = i < emailed
        contacts.append(_c(cid=cid, name=f"Person {i + 1}", email=f"p{i + 1}@acme.com",
                           submitted_at=_at(last) if was_emailed else ""))
        if was_emailed:
            ladder = {"count": touches, "last_sent_at": _at(last) if touches else ""}
            if i < stopped:
                ladder["sequence_status"] = "stopped"
            ladders[(cid, "email")] = ladder
    return contacts, ladders


def _read(job=None, contacts=None, ladders=None, plan=None):
    return tmp.temperature(job or _job(), contacts or [], ladders or {}, plan, now=NOW)


def test_a_job_worked_today_with_outreach_left_is_active_not_cooling():
    """The exact reported case: Visa, applied this morning, 6 of 8 emailed."""
    contacts, ladders = _people(8, emailed=6, last=0)
    got = _read(job=_job(applied_at=_at(0)), contacts=contacts, ladders=ladders,
                plan=_plan(emailed=(6, 8)))
    assert got["band"] == tmp.ACTIVE, got
    assert "still to go" in got["reason"] and "sent today" in got["reason"], got["reason"]


def test_the_ladder_has_not_started_is_not_the_ladder_is_spent():
    """**The bug this rebuild exists for.** Expedia, applied 12:57, reported at 13:04 reading
    `cooling` with the tooltip "Everything planned here is spent (5 emails)."

    Five emails went out that morning and the schedule is 48/96/168h, so nothing was DUE and
    the checklist's follow-up step read 0/0 — which is correct for a checklist and inverts as
    runway. Fifteen follow-ups were queued behind those five emails and the plan reported none.

    Measured on the live board: of ten jobs reading `cooling`, six were ≤38% through their
    plan, and Expedia at 25% read "spent" while Saronic at 32% — further along — read "active".
    """
    contacts, ladders = _people(5, last=0, touches=0)
    got = _read(job=_job(applied_at=_at(0)), contacts=contacts, ladders=ladders,
                plan=_plan(emailed=(5, 5), followup=(0, 0)))
    assert got["band"] == tmp.ACTIVE, got
    assert "spent" not in got["reason"], (
        f"a ladder that has not started must not read as one that has finished: {got['reason']}")
    assert "5 of 20" in got["reason"], got["reason"]


def test_a_spent_sequence_is_cooling():
    """Webai: every email and every follow-up sent, nobody answered."""
    contacts, ladders = _people(5, touches=3, last=3)
    got = _read(contacts=contacts, ladders=ladders,
                plan=_plan(emailed=(5, 5), followup=(5, 5)))
    assert got["band"] == tmp.COOLING, got
    assert "spent" in got["reason"] and "5 emails" in got["reason"], got["reason"]


def test_nearly_through_the_plan_with_no_answer_is_cooling_not_active():
    """The trap in the fix. Counting scheduled touches as runway makes "is anything left" true
    for almost every job, which would put Webai — a fortnight in, one trailing touch to go —
    back in the same band as a job emailed this morning. That is §Lessons 54 rebuilt from the
    other side, so the band reads the PROPORTION left rather than whether any is."""
    order = [tmp.COLD, tmp.COOLING, tmp.ACTIVE, tmp.WARM]
    early, early_l = _people(5, touches=0, last=0)          # 5/20 sent
    late, late_l = _people(5, touches=2, last=2)            # 15/20 sent
    fresh = _read(contacts=early, ladders=early_l, plan=_plan())
    worn = _read(contacts=late, ladders=late_l, plan=_plan())
    assert fresh["band"] == tmp.ACTIVE, fresh
    assert worn["band"] == tmp.COOLING, worn
    assert order.index(worn["band"]) < order.index(fresh["band"])
    assert "Only 5 left" in worn["reason"], worn["reason"]


def test_finishing_the_plan_moves_a_job_DOWN():
    """§Lessons 35, restated for the new input. Runway must not become a way for effort to buy
    a better reading: the job that has sent MORE and has nothing left is the colder one."""
    order = [tmp.COLD, tmp.COOLING, tmp.ACTIVE, tmp.WARM]
    started, started_l = _people(8, emailed=2, last=2)
    done, done_l = _people(8, touches=3, last=2)
    running = _read(contacts=started, ladders=started_l, plan=_plan(emailed=(2, 8)))["band"]
    finished = _read(contacts=done, ladders=done_l,
                     plan=_plan(emailed=(8, 8), followup=(8, 8)))["band"]
    assert order.index(finished) < order.index(running), (
        f"spending the whole sequence ({finished}) read warmer than barely starting ({running})")


def test_a_stopped_ladder_is_finished_not_runway():
    """Stopping a sequence must not make the job look like it has further to go. A terminal
    ladder has no remaining touches whatever the schedule still lists."""
    contacts, ladders = _people(4, touches=1, last=2, stopped=4)
    got = _read(contacts=contacts, ladders=ladders, plan=_plan(emailed=(4, 4)))
    assert got["band"] == tmp.COOLING, got
    assert "spent" in got["reason"], got["reason"]


def test_runway_nobody_is_using_is_not_active():
    """Unsent outreach on a job nobody has touched in a fortnight is an abandoned job, not a
    plan in progress. Calling that `active` is the same lie as calling a fresh job `cooling`,
    pointing the other way."""
    contacts, ladders = _people(9, emailed=1, last=30)
    got = _read(contacts=contacts, ladders=ladders, plan=_plan(emailed=(1, 9)))
    assert got["band"] == tmp.COOLING, got
    assert "nothing has been sent" in got["reason"], got["reason"]


def test_cold_measures_silence_from_the_last_message_not_from_applied_at():
    """Betterup: applied 15 days ago, sequence spent — but the last follow-up went yesterday.
    The old reason said "no answer from anyone in 15 days" while we had messaged them the day
    before, which is a different situation and the opposite instruction."""
    contacts, ladders = _people(4, touches=3, last=1)
    fresh = _read(job=_job(applied_at=_at(15)), contacts=contacts, ladders=ladders,
                  plan=_plan(emailed=(4, 4), followup=(4, 4)))
    assert fresh["band"] == tmp.COOLING, fresh
    assert "yesterday" in fresh["reason"], fresh["reason"]

    contacts, ladders = _people(4, touches=3, last=21)
    stale = _read(job=_job(applied_at=_at(40)), contacts=contacts, ladders=ladders,
                  plan=_plan(emailed=(4, 4), followup=(4, 4)))
    assert stale["band"] == tmp.COLD, stale
    assert "21 days" in stale["reason"], stale["reason"]


def test_an_unsent_ladder_touch_is_runway():
    """One touch of three sent, per contact, is a sequence still running — not a due count.
    `plan['due']` counts what has crossed its threshold THIS INSTANT and is 0 for most of every
    ladder's life; the remaining touches are the runway."""
    contacts, ladders = _people(4, touches=1, last=3)
    got = _read(contacts=contacts, ladders=ladders, plan=_plan(emailed=(4, 4), due=0))
    assert got["band"] == tmp.ACTIVE, got
    assert "8 of 16" in got["reason"], got["reason"]


def test_a_contact_with_no_address_is_not_email_runway():
    """You cannot email someone you have no address for, so they are not road left to travel.
    Counting them would give every job permanent runway — the §Lessons 35 shape that keeps
    LinkedIn out of this maths."""
    reachable, ladders = _people(2, touches=3, last=2)
    unreachable = [_c(cid="x1", name="No Address", email="", submitted_at="")]
    got = _read(contacts=reachable + unreachable, ladders=ladders, plan=_plan(emailed=(2, 2)))
    assert got["band"] == tmp.COOLING, got


def test_linkedin_invites_are_not_runway():
    """`dm_status` is 'sent'|'manual', both meaning WE sent one; no `accepted` state exists
    anywhere in the schema, so an uninvited contact is not a pending conversation (§Lessons 35).
    Counting them also makes the band useless: measured live, LinkedIn steps sit at 3/16, 5/10
    and 3/6, so every job on the board would have runway forever and nothing could read spent."""
    contacts, ladders = _people(2, touches=3, last=4)
    got = _read(contacts=contacts, ladders=ladders,
                plan=_plan(emailed=(2, 2), followup=(2, 2), linkedin=(3, 16)))
    assert got["band"] == tmp.COOLING, got


def test_the_spent_reason_never_contradicts_an_outstanding_invite():
    """§Lessons 56. The Expedia row said "Everything planned here is spent" while its own Next
    button read "1 LinkedIn invite left". LinkedIn stays outside the runway maths, but it may
    not license a sentence the same row contradicts an inch away."""
    contacts, ladders = _people(2, touches=3, last=4)
    got = _read(contacts=contacts, ladders=ladders,
                plan=_plan(emailed=(2, 2), followup=(2, 2), linkedin=(6, 7)))
    assert "Everything planned" not in got["reason"], got["reason"]
    assert "Every email planned" in got["reason"], got["reason"]


def test_their_engagement_still_outranks_any_amount_of_runway():
    """Runway can reach `active`. Only a person can reach `warm`."""
    contacts, ladders = _people(5, emailed=1, last=0)
    busy = _read(contacts=contacts, ladders=ladders, plan=_plan(emailed=(1, 5)))["band"]
    answered = _read(contacts=[_c(interactions=[{"kind": ix.REPLIED, "at": _at(2)}])])["band"]
    assert busy == tmp.ACTIVE and answered == tmp.WARM


def test_no_plan_at_all_still_produces_a_band():
    """`plan` is optional — `_temperature` catches everything and callers outside the dashboard
    (the eval harness, `tick`) pass none."""
    got = tmp.temperature(_job(), [_c(submitted_at=_at(3))], {}, None, now=NOW)
    assert got["band"] in (tmp.COOLING, tmp.COLD) and got["reason"]


def test_the_reason_never_says_1_days():
    """It sits on the row and is read. "1 days", "today ago" and "in yesterday" are all what
    ONE shared duration helper produces, which is why there are two.

    The boundary is not decoration: the first version of this test asserted `"1 days" not in
    reason` and failed on "21 days" — §Lessons 1 inside the test written to check the wording.
    """
    import re
    for days in (0, 1, 2, 15, 21):
        got = _read(job=_job(applied_at=_at(days)), contacts=[_c(submitted_at=_at(days))],
                    ladders={("c1", "email"): {"count": 3, "last_sent_at": _at(days)}},
                    plan=_plan(emailed=(3, 3), followup=(3, 3)))
        assert not re.search(r"\b1 days\b", got["reason"]), got["reason"]
        assert "today ago" not in got["reason"], got["reason"]
        assert "in yesterday" not in got["reason"], got["reason"]


# ── the wiring, end to end ──────────────────────────────────────────────────

def test_the_dashboard_actually_passes_the_ladder_length(tmp_path, monkeypatch):
    """§Lessons 47, pre-empted. `total_touches` is the EMAIL ladder's length and it travels
    from `followup_panel` through `_temperature` into `_plan_progress`. Drop it anywhere on
    that path and `per` is 0, every emailed contact's plan is exactly one message, every job
    reads as a spent plan — the original bug, restored by omission and with nothing to see.

    The unit tests above cannot catch that: they hand `_plan()` a `total_touches` themselves.
    This one builds a real payload from a real database and asserts on the band a job gets
    when its emails went out MINUTES ago, which is the reported case.
    """
    from datetime import datetime, timedelta, timezone

    import applypilot.database as database
    from applypilot.networking import connections, store, touches

    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    connections.init_connections(conn)

    just_now = (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat()
    conn.execute("INSERT INTO jobs (url, title, site, company, strategy, "
                 "tailored_resume_path, applied_at, discovered_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("http://j/expedia", "Senior AI Engineer", "Expediagroup", "Expediagroup",
                  "dashboard_upload", "/tmp/r.pdf", just_now, just_now))
    for i in range(5):
        store.upsert_contact({"job_url": "http://j/expedia", "full_name": f"Person {i}",
                              "email": f"p{i}@expedia.com", "sent_message_id": f"g{i}",
                              "submitted_at": just_now}, conn)
    conn.commit()

    from applypilot import web_dashboard as wd
    job = next(j for j in wd._status_payload()["jobs"] if "expedia" in j["url"])
    temp = job["temperature"]

    assert temp["band"] == tmp.ACTIVE, (
        f"five emails sent seven minutes ago read {temp['band']!r}: {temp['reason']!r}")
    assert "spent" not in temp["reason"], temp["reason"]
