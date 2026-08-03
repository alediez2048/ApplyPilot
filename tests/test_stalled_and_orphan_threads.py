"""Two blind spots found in live use.

1. A contact who wrote to us on a thread WE never started was invisible to the poller. It read
   threads by `thread_id`, captured at send time, and `continue`d past anyone without one — so
   David, introduced into a Writer thread by Victoria and then writing on a fresh one, could
   email forever and nothing would notice.

2. A conversation that stalled after a reply had no mechanism at all. `replied` is TERMINAL, so
   the cold ladder halts — correctly, you do not keep cold-chasing someone who answered — but
   nothing replaced it. Measured: Gina replied 7/31, was answered the same day, then received a
   SECOND unanswered message on 8/3, and neither was tracked. The system chased strangers and
   abandoned the people who had actually engaged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from applypilot.domain.conversations import conversation_state

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _m(direction, when, **over):
    m = {"direction": direction, "sent_at": when,
         "from_addr": "them@x.com" if direction == "in" else "me@x.com",
         "from_name": "Gina" if direction == "in" else "", "to_addrs": [], "cc_addrs": []}
    m.update(over)
    return m


# ── a live thread that went quiet ───────────────────────────────────────────

def test_a_thread_they_stopped_answering_is_stalled():
    msgs = [_m("out", ago(days=8)), _m("in", ago(days=6)), _m("out", ago(days=5))]
    st = conversation_state(msgs, NOW)
    assert st["state"] == "awaiting_them"
    assert st["stalled"] is True
    assert st["days"] == 5


def test_a_fresh_answer_is_not_stalled():
    """Answering someone an hour ago is not a lead going cold."""
    msgs = [_m("out", ago(days=3)), _m("in", ago(hours=2)), _m("out", ago(hours=1))]
    assert conversation_state(msgs, NOW)["stalled"] is False


def test_their_message_waiting_on_US_is_never_stalled():
    """`awaiting_us` is a different and more urgent state — they wrote, nobody answered. Marking
    it stalled would suggest nudging somebody who is waiting on you."""
    msgs = [_m("out", ago(days=9)), _m("in", ago(days=5))]
    st = conversation_state(msgs, NOW)
    assert st["state"] == "awaiting_us"
    assert st["stalled"] is False


def test_a_thread_of_only_our_own_messages_is_not_a_conversation():
    """Outreach with an unanswered follow-up ladder. Calling that "stalled" would put every cold
    email into the same bucket as a real live thread — the exact conflation conversation_state
    returns None to prevent."""
    assert conversation_state([_m("out", ago(days=9)), _m("out", ago(days=5))], NOW) is None


# ── how many nudges have already been spent ─────────────────────────────────

def test_it_counts_our_messages_since_they_last_spoke():
    """The Gina case exactly: replied, answered, then nudged again. Two outbound in a row means
    the nudge is already spent, and the honest next move is to stop rather than send a third."""
    msgs = [_m("out", ago(days=9)), _m("in", ago(days=6)),
            _m("out", ago(days=6)), _m("out", ago(days=4))]
    assert conversation_state(msgs, NOW)["unanswered"] == 2


def test_one_unanswered_message_is_the_normal_case():
    msgs = [_m("out", ago(days=9)), _m("in", ago(days=6)), _m("out", ago(days=5))]
    assert conversation_state(msgs, NOW)["unanswered"] == 1


def test_the_count_resets_when_they_write_again():
    msgs = [_m("out", ago(days=9)), _m("out", ago(days=8)), _m("in", ago(days=2))]
    assert conversation_state(msgs, NOW)["unanswered"] == 0


def test_the_threshold_is_configurable(monkeypatch):
    """72h is a default, not a law. See the next test for the tension it leaves with the email
    ladder's 48h first touch — recorded rather than quietly resolved."""
    from applypilot import settings
    msgs = [_m("out", ago(days=8)), _m("in", ago(days=6)), _m("out", ago(days=4))]
    assert conversation_state(msgs, NOW)["stalled"] is True
    monkeypatch.setattr(settings, "resolve", lambda *_a, **_k: ({"STALLED_AFTER_HOURS": 240}, []))
    assert conversation_state(msgs, NOW)["stalled"] is False
    assert settings._BY_NAME["STALLED_AFTER_HOURS"].default == 72


def test_it_is_not_slower_than_the_slowest_cold_ladder():
    """A live thread must never wait longer than a stranger does.

    Honest about what this does NOT assert: at the default 72h, the EMAIL ladder still nudges a
    stranger sooner, at 48h. That is a genuine tension and it is recorded rather than hidden —
    the fix is a number the operator picks, not something to quietly change under them. Lower
    STALLED_AFTER_HOURS to 48 to make a live thread strictly the most urgent thing in the
    system; the state only surfaces a prompt, it never sends.
    """
    from applypilot.domain.conversations import STALLED_AFTER_HOURS
    from applypilot.domain.followup import CHANNELS
    slowest = max(ch.default_schedule[0] for ch in CHANNELS)
    assert STALLED_AFTER_HOURS <= slowest, (
        "a live conversation waits longer to be flagged than the slowest cold ladder waits to "
        "nudge a stranger — the system is prioritising people who never answered")


# ── the poller must see threads it did not start ────────────────────────────

def test_the_poller_sweeps_addresses_for_contacts_with_no_thread():
    """The loop used to `continue` past every contact without a thread_id, which made anyone who
    wrote to us FIRST permanently invisible."""
    from applypilot.networking import replies
    src = open(replies.__file__, encoding="utf-8").read()
    body = src[src.index("def poll("):]
    assert "_adopt_threads_by_address(contacts, conn)" in body, (
        "the poller no longer sweeps for threads it did not start")
    assert body.index("_adopt_threads_by_address") < body.index('if not tid:\n            continue'), (
        "the sweep runs after the skip, so it cannot help on this poll")


def test_the_sweep_is_one_query_not_one_per_contact():
    """§Lessons 26 — a per-contact network call on a 5-minute timer is how a poller quietly
    becomes the slowest thing in the system. /api/status already paid for that lesson."""
    from applypilot.networking import replies
    src = open(replies.__file__, encoding="utf-8").read()
    fn = src[src.index("def _adopt_threads_by_address"):]
    fn = fn[:fn.index("\ndef ")]
    assert fn.count("search_threads(") == 1, "the sweep searches per contact"
    assert '" OR ".join' in fn, "addresses are not batched into a single query"


def test_the_sweep_only_looks_at_people_in_play():
    """Every discovered contact would otherwise be searched forever, spending an API call apiece
    to learn nothing about strangers we have never written to."""
    from applypilot.networking import replies
    src = open(replies.__file__, encoding="utf-8").read()
    fn = src[src.index("def _adopt_threads_by_address"):]
    fn = fn[:fn.index("\ndef ")]
    for signal in ("submitted", "replied_at", "introduc"):
        assert signal in fn, f"the sweep ignores {signal!r}, so it searches for everyone"


def test_a_matched_thread_is_verified_against_the_address():
    """A thread can match the OR query through ANY of the addresses, so which contact it belongs
    to has to be checked rather than assumed — otherwise the first waiting contact would adopt
    every thread the query returned."""
    from applypilot.networking import replies
    src = open(replies.__file__, encoding="utf-8").read()
    fn = src[src.index("def _adopt_threads_by_address"):]
    fn = fn[:fn.index("\ndef ")]
    assert "if addr in blob" in fn, "threads are attributed without checking who is in them"
