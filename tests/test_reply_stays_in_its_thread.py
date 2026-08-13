"""A reply answers ONE conversation, not everything stored for a person.

`thread_for_contact` returns every message we hold for a contact, merged and sorted by date, and
`reply_target` read the whole list. Two consequences, both measured on the live database before
this was written:

    contact   msgs  threads   References sent
    a2b30…      61       17   61 ids across 17 unrelated conversations
    b71b6…      87       25   87 ids across 25
    fe176…      18        7   18 ids across 7

Every one of the 7 contacts with more than one thread. A `References` header spanning 25 threads
tells the recipient's mail client that a calendar invite, an introduction and two separate deals
are one conversation — and it is invisible from our side, because we render the merge we caused.

And on one contact the newest inbound overall was an out-of-office auto-reply that Gmail had put
on its OWN thread, so the composer under the live conversation offered to answer a different
message entirely. §Lessons 29: the dangerous half of a feature is the half that looks identical
when it is wrong.
"""

from __future__ import annotations

import pytest

from applypilot.domain import conversations as cv

ME = "me@work.test"


def m(direction, frm, *, tid="", subject="X", at="2026-08-01T10:00", mid="", to=None, cc=None):
    return {"direction": direction, "from_addr": frm, "from_name": "", "thread_id": tid,
            "subject": subject, "sent_at": at, "rfc_message_id": mid or f"<{frm}-{at}>",
            "to_addrs": to or [ME], "cc_addrs": cc or []}


#: Two conversations with one person. The DEAL is live; the INVITE is older and finished.
DEAL = [
    m("out", ME, tid="deal", subject="Ormus <> AMSYS", at="2026-08-01T10:00"),
    m("in", "kevin@amsys.test", tid="deal", subject="Re: Ormus <> AMSYS", at="2026-08-02T10:00",
      cc=["laura@amsys.test"]),
]
INVITE = [
    m("in", "calendar@amsys.test", tid="inv", subject="Accepted: intro", at="2026-08-03T10:00"),
]
BOTH = DEAL + INVITE


# ── the References corruption, which hit every multi-thread contact ─────────

def test_references_never_leaves_the_thread_being_answered():
    """The 87-id header. `References` chains THIS conversation and stops."""
    t = cv.reply_target(BOTH, ME, thread="deal")
    refs = t["references"].split()
    assert refs == [x["rfc_message_id"] for x in DEAL], \
        "the reply chained messages from another conversation"
    assert all("calendar" not in r for r in refs)


def test_the_merged_list_alone_used_to_chain_everything():
    """The negative case, so the test above cannot pass by returning an empty References."""
    assert len(cv.reply_target(BOTH, ME, thread="deal")["references"].split()) == 2
    assert len(BOTH) == 3, "fixture no longer has a message outside the deal thread"


# ── the misaddressing ───────────────────────────────────────────────────────

def test_a_reply_goes_to_the_person_on_the_thread_you_named():
    assert cv.reply_target(BOTH, ME, thread="deal")["to_addr"] == "kevin@amsys.test"
    assert cv.reply_target(BOTH, ME, thread="inv")["to_addr"] == "calendar@amsys.test"


def test_the_cc_comes_from_that_thread_too():
    """A Cc carried across threads adds somebody to a conversation they were never on."""
    assert cv.reply_target(BOTH, ME, thread="deal")["cc"] == ["laura@amsys.test"]
    assert cv.reply_target(BOTH, ME, thread="inv")["cc"] == []


def test_an_unknown_thread_REFUSES_rather_than_falling_back():
    """The fallback is the bug. A key that resolves to nothing must not quietly answer the
    newest inbound across everything — that is exactly how a reply reaches a stranger."""
    assert cv.reply_target(BOTH, ME, thread="no-such-thread") is None


def test_naming_a_thread_with_no_inbound_message_is_also_a_refusal():
    """A thread where only we have written is a follow-up, not a reply — and silently turning
    one into the other bypasses the ladder, its schedule and its stop conditions."""
    ours = [m("out", ME, tid="solo", subject="hello", at="2026-08-04T10:00")]
    assert cv.reply_target(BOTH + ours, ME, thread="solo") is None


# ── the default, for every caller that names no thread ──────────────────────

def test_with_no_thread_named_it_still_answers_ONE_conversation():
    """A CLI caller knows nothing about threads and must still not splice two together."""
    t = cv.reply_target(BOTH, ME)
    assert t["to_addr"] == "calendar@amsys.test"          # the newest inbound
    assert t["references"].split() == [INVITE[0]["rfc_message_id"]]


def test_the_default_thread_is_the_one_holding_the_NEWEST_inbound():
    """Not simply the newest thread: a conversation where only we have spoken has nothing to
    reply to, and skipping past it is what keeps the old behaviour for existing callers."""
    later_ours = BOTH + [m("out", ME, tid="new", subject="ping", at="2026-08-09T10:00")]
    assert cv.reply_target(later_ours, ME)["to_addr"] == "calendar@amsys.test"


def test_a_bounce_does_not_become_the_default_thread():
    """A MAILER-DAEMON notification is an inbound message on its own thread. Offering to reply
    to one is the point at which a CRM stops being trustworthy."""
    bounced = BOTH + [m("in", "mailer-daemon@googlemail.com", tid="bnc",
                        subject="Delivery Status Notification", at="2026-08-08T10:00")]
    assert cv.reply_target(bounced, ME)["to_addr"] == "calendar@amsys.test"


# ── what the composer needs to SAY ──────────────────────────────────────────

def test_the_target_names_the_conversation_it_answers():
    """A person with several threads gets one reply box. Without this the operator cannot tell
    which conversation the box is pointed at, and the two render identically."""
    t = cv.reply_target(BOTH, ME, thread="deal")
    assert t["thread_key"] == "deal"
    assert t["thread_subject"] == "Ormus <> AMSYS", "the Re: prefix leaked into the label"


def test_the_key_it_reports_is_one_you_can_reply_with():
    """A round trip. A key the caller cannot feed back in is a label, not a selector."""
    first = cv.reply_target(BOTH, ME)
    again = cv.reply_target(BOTH, ME, thread=first["thread_key"])
    assert again["to_addr"] == first["to_addr"]
    assert again["references"] == first["references"]


# ── messages with no thread_id at all ───────────────────────────────────────

def test_threads_without_an_id_are_still_separable_by_subject():
    """Pasted messages and anything synced before threading carry no id. Bucketing them under
    "" rebuilds the merge; they group by normalised subject instead."""
    pasted = [
        m("in", "a@x.test", tid="", subject="Invoice", at="2026-08-05T10:00"),
        m("in", "b@x.test", tid="", subject="Fwd: Re: Something else", at="2026-08-06T10:00"),
    ]
    keys = {cv.thread_key(x) for x in pasted}
    assert len(keys) == 2
    got = cv.reply_target(pasted, ME, thread=cv.thread_key(pasted[0]))
    assert got["to_addr"] == "a@x.test"


# ── and the shapes that must not regress ────────────────────────────────────

@pytest.mark.parametrize("bad", [None, [], [{"direction": "in"}], ["not a dict"]])
def test_junk_in_never_raises(bad):
    assert cv.reply_target(bad, ME) is None
    assert cv.reply_target(bad, ME, thread="anything") is None


def test_group_threads_orders_the_live_conversation_last():
    """The composer anchors under the last group, so this order IS the default thread."""
    groups = cv.group_threads(BOTH)
    assert [g["key"] for g in groups] == ["deal", "inv"]


def test_group_threads_is_not_merely_returning_insertion_order():
    """A test whose input is already in the answer's order cannot see the sort (§Lessons 103)."""
    shuffled = [INVITE[0]] + DEAL
    assert [g["key"] for g in cv.group_threads(shuffled)] == ["deal", "inv"]
