"""Draft and send many email follow-ups in one click.

Fifty-seven were due and none had a draft, so clicking through them one at a time is what this
replaces. It is also the least reversible control in the app, and the design follows from that:

**It loops `_followup_action`.** Every guard the single-contact path has — the Space's
`can_autosend`, the channel's, the daily limit, the per-company cap, a terminal sequence, "no
draft yet" — is inherited rather than restated. A second send path would be §Lessons 49 pointed
at the one action that cannot be undone.

**The caller names the contacts.** The server never re-derives "everything due": the reply
poller runs every five minutes and moves that set, so a re-derivation could send a message the
operator was never shown. The ids sent are the ids listed.

**Email only.** LinkedIn and SMS are copy-paste by design (§Lessons 3), and offering them in a
bulk control implies an action that does not exist.
"""

from __future__ import annotations

import pytest

from applypilot import web_dashboard as wd


@pytest.fixture()
def calls(monkeypatch):
    """Record what the bulk path asks the single-contact path to do."""
    seen = []

    def fake(data):
        seen.append(dict(data))
        cid = data.get("contact_id")
        if cid.startswith("fail"):
            return {"ok": False, "message": "no follow-up draft — generate one first"}
        if cid.startswith("limit"):
            return {"ok": False, "message": "daily send limit reached (25)"}
        if cid.startswith("space"):
            return {"ok": False, "message": "“Gauntlet” has auto-send off — copy the draft"}
        if cid.startswith("boom"):
            raise RuntimeError("gmail exploded")
        return {"ok": True, "message": "sent"}

    monkeypatch.setattr(wd, "_followup_action", fake)
    return seen


# ── it delegates ────────────────────────────────────────────────────────────

def test_every_contact_goes_through_the_single_send_path(calls):
    """The whole design. If this ever stops being true, every guard has to be re-audited."""
    wd._bulk_followups({"action": "send", "contact_ids": ["a", "b", "c"]})
    assert [c["contact_id"] for c in calls] == ["a", "b", "c"]
    assert {c["action"] for c in calls} == {"send"}


def test_drafting_uses_the_same_path(calls):
    wd._bulk_followups({"action": "draft", "contact_ids": ["a", "b"]})
    assert {c["action"] for c in calls} == {"draft"}


def test_only_the_ids_given_are_touched(calls):
    """It must not re-derive the due set. The poller moves that set every five minutes, so a
    server-side re-derivation could send a message the operator never saw listed."""
    wd._bulk_followups({"action": "send", "contact_ids": ["only-this-one"]})
    assert [c["contact_id"] for c in calls] == ["only-this-one"]


# ── partial failure ─────────────────────────────────────────────────────────

def test_one_failure_does_not_abort_the_batch(calls):
    """A contact with no draft is skipped, not fatal — otherwise one stale row blocks fifty."""
    res = wd._bulk_followups({"action": "send", "contact_ids": ["a", "fail-1", "b"]})
    assert res["done"] == 2 and res["failed"] == 1
    assert [c["contact_id"] for c in calls] == ["a", "fail-1", "b"]


def test_an_exception_is_contained(calls):
    res = wd._bulk_followups({"action": "send", "contact_ids": ["a", "boom", "b"]})
    assert res["done"] == 2
    assert any("exploded" in r["message"] for r in res["results"])


def test_each_failure_says_which_contact(calls):
    """"12 sent, 5 failed" without naming the five leaves the operator to diff the board by
    hand — which on a send path means guessing what went out."""
    res = wd._bulk_followups({"action": "send", "contact_ids": ["fail-1", "fail-2"]})
    ids = {r["contact_id"] for r in res["results"] if r["contact_id"]}
    assert ids == {"fail-1", "fail-2"}


# ── stopping early ──────────────────────────────────────────────────────────

def test_the_daily_limit_stops_the_whole_batch(calls):
    """It applies to every remaining contact, so continuing is 56 more pointless Gmail calls —
    and each one is a real network round-trip that can fail in its own way."""
    wd._bulk_followups({"action": "send", "contact_ids": ["a", "limit-1", "b", "c"]})
    assert [c["contact_id"] for c in calls] == ["a", "limit-1"]


def test_a_space_with_autosend_off_stops_the_batch(calls):
    """`can_autosend=False` exists to stop exactly this: an operator doing something at 11pm
    that cannot be undone (`spaces-prd.md` §13.3)."""
    wd._bulk_followups({"action": "send", "contact_ids": ["a", "space-1", "b"]})
    assert [c["contact_id"] for c in calls] == ["a", "space-1"]


def test_a_per_contact_refusal_does_NOT_stop_the_batch(calls):
    """Guard the guard: stopping on every failure would make the two tests above pass while one
    contact without a draft silently cancelled the other fifty-six."""
    wd._bulk_followups({"action": "send", "contact_ids": ["fail-1", "a", "fail-2", "b"]})
    assert [c["contact_id"] for c in calls] == ["fail-1", "a", "fail-2", "b"]


# ── refusals ────────────────────────────────────────────────────────────────

def test_an_empty_selection_does_nothing(calls):
    assert wd._bulk_followups({"action": "send", "contact_ids": []})["ok"] is False
    assert not calls


def test_an_unknown_action_is_refused(calls):
    """`stop`, `replied` and `reopen` are real verbs on the single-contact endpoint. Bulk-
    stopping every sequence from one click is a different feature with different consequences,
    and it is not this one."""
    for verb in ("stop", "replied", "reopen", "sent", ""):
        assert wd._bulk_followups({"action": verb, "contact_ids": ["a"]})["ok"] is False
    assert not calls


def test_a_runaway_selection_is_capped(calls):
    """Bounds one click's blast radius. Not a policy about the right number to send — anything
    larger is simply a second click."""
    res = wd._bulk_followups({"action": "send",
                              "contact_ids": [f"c{i}" for i in range(wd._BULK_MAX + 1)]})
    assert res["ok"] is False
    assert not calls, "the cap has to refuse BEFORE sending anything"


def test_the_cap_allows_today_s_real_backlog(calls):
    """57 were due when this was built. A cap that refused the actual workload would be a
    feature that never runs."""
    assert wd._BULK_MAX >= 57
    wd._bulk_followups({"action": "send", "contact_ids": [f"c{i}" for i in range(57)]})
    assert len(calls) == 57


# ── the route is reachable ──────────────────────────────────────────────────

def test_the_endpoint_is_wired():
    """A handler nobody routes to is the same as no handler."""
    import inspect
    src = inspect.getsource(wd)
    assert '"/api/followup/bulk"' in src
    assert "_bulk_followups(data)" in src


# ── the control that is actually on the job ─────────────────────────────────
#
# The global console button does the same work across every application, and it was reported
# three times as not existing. It was there, and it was in the wrong room: the Follow-ups tab of
# a job is where each contact already has its own "✍ Draft follow-up", and that is where a bulk
# version of the same act belongs (§Lessons 43 — a control nobody finds is a broken feature, and
# "nobody finds it" is measured by the operator, not by whether it renders).

def _js() -> str:
    from pathlib import Path

    from applypilot import web_dashboard as wd
    return (Path(wd.__file__).parent / "static" / "dashboard.js").read_text()


def test_the_bulk_bar_is_rendered_on_the_followups_tab():
    js = _js()
    body = js[js.index("function followupBody"):]
    body = body[:body.index("\nfunction ", 10)]
    assert "fuBulkBar(j, f, byId)" in body, "the job's own Follow-ups tab has no bulk control"


def test_it_offers_both_draft_and_send():
    js = _js()
    bar = js[js.index("function fuBulkBar"):js.index("async function fuBulk(")]
    assert "'draft'" in bar and "'send'" in bar
    assert "Draft all" in bar and "Send all" in bar


def test_draft_and_send_act_on_DIFFERENT_people():
    """"Draft all" must skip anyone who already has a draft, or it silently discards edits the
    operator made by hand. "Send all" must act only on what is written — sending a contact with
    no draft is not a send, it is an error fifty times over."""
    js = _js()
    fn = js[js.index("async function fuBulk("):js.index("function fuFlash(")]
    assert "action === 'send'" in fn
    assert "? due.filter(c => (c.followup_message || '').trim())" in fn
    assert ": due.filter(c => !(c.followup_message || '').trim())" in fn


def test_a_disabled_button_says_why():
    """Hiding it would make "nothing to send yet" indistinguishable from a broken tab
    (§Lessons 41)."""
    bar = _js()
    bar = bar[bar.index("function fuBulkBar"):bar.index("async function fuBulk(")]
    assert "disabled title=" in bar
    assert "Draft them first" in bar


def test_it_does_not_appear_for_a_single_contact():
    """One person is just the button already on their card. A bulk bar above a single row is
    furniture."""
    bar = _js()
    bar = bar[bar.index("function fuBulkBar"):bar.index("async function fuBulk(")]
    assert "due.length < 2" in bar


def test_each_person_is_highlighted_as_the_batch_runs():
    """The operator asked to see WHICH person is being acted on. The cards are marked before the
    request goes out — so the set is visible BEFORE anything happens, not only after."""
    js = _js()
    fn = js[js.index("async function fuBulk("):js.index("function fuFlash(")]
    assert "due.forEach(c => fuFlash(c.id, 'working'));" in fn
    assert fn.index("fuFlash(c.id, 'working')") < fn.index("await post("), (
        "the highlight happens after the send — the operator cannot see who is included")
    assert "fuFlash(c.id, failed.has(c.id) ? 'failed' : 'ok')" in fn


def test_the_highlight_targets_that_contacts_card():
    js = _js()
    fn = js[js.index("function fuFlash("):]
    fn = fn[:fn.index("\n}") + 2]
    assert '.fu-card[data-cid="${cid}"]' in fn


def test_sending_confirms_with_the_names():
    """Eight names you can read, from the job you are looking at — the thing the global panel
    could not give, because it spanned every application at once."""
    js = _js()
    fn = js[js.index("async function fuBulk("):js.index("function fuFlash(")]
    assert "confirm(" in fn
    assert "due.map(c => c.full_name)" in fn
    assert "cannot be undone" in fn


def test_it_reuses_the_guarded_bulk_endpoint():
    """Not a second send path. Every guard — `can_autosend`, the daily limit, the company cap,
    "no draft yet" — is inherited by looping `_followup_action`, and a new one is inherited too."""
    js = _js()
    fn = js[js.index("async function fuBulk("):js.index("function fuFlash(")]
    assert "post('/api/followup/bulk'" in fn
