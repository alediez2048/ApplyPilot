"""Our own sent message is stored whole, not re-capped by the call that had it in its hand.

`record_outbound` is the AUTHORITATIVE write for a follow-up or a reply: it runs at send time
holding the exact text that just left the mailbox, and trims it to `PASTED_MAX`. It then called
`upsert_messages(...)` without `full=True`, and `upsert_messages` defaults to `SNIPPET_MAX` — so
the body was cut to 200 characters by the one code path that could not have been mistaken about
what the message said.

Distinct from the sync-path bug §Lessons 111 records. That one re-capped text it was merely
PRESERVING. This capped text at first WRITE, so `_keep_longer` never got a chance to defend it.

It matters most for a REPLY. A first email survives in `contacts.outreach_message` and a
follow-up in `touches.body`, so the card can substitute a fuller copy for both. A reply we send
is stored NOWHERE else: if this write truncates it, the text is gone until somebody re-fetches
the thread from Gmail.
"""
from applypilot.networking import messages as msgs

LONG = ("This is the reply we actually sent, and it is comfortably longer than the two hundred "
        "characters that Gmail's automatic preview would have kept for us. " * 4).strip()


def test_a_sent_reply_is_stored_in_full(monkeypatch):
    """The behaviour, executed rather than grepped."""
    captured = {}

    def fake_upsert(rows, conn=None, full=False):
        captured["full"] = full
        captured["len"] = len(rows[0]["snippet"])
        return 1

    monkeypatch.setattr(msgs, "upsert_messages", fake_upsert)
    msgs.record_outbound(
        contact={"id": "c1", "job_url": "u", "thread_id": "t1"},
        # `record_outbound` keys on sent["id"], NOT "message_id". The first version of this
        # test used the wrong key, `mid` came back None, the function returned before writing
        # anything and the assertion failed on a correct fix (§Lessons 103: a fixture that
        # invents a field proves only that the fixture matches itself).
        sent={"id": "m1", "thread_id": "t1", "from_addr": "me@x.test",
              "rfc_message_id": "<r1>"},
        to_addr="them@x.test", cc=[], subject="Re: the role", body=LONG)
    assert captured["full"] is True, (
        "record_outbound dropped full=True, so upsert_messages re-capped our own sent text "
        "to SNIPPET_MAX at the moment it was written")
    assert captured["len"] == len(LONG), "the body was trimmed before it even reached the store"


def test_the_two_bounds_are_chosen_by_the_kind_of_read():
    """A deliberate write of our own words gets PASTED_MAX; an automatic sync of somebody
    else's gets SNIPPET_MAX. Never a number a caller invents."""
    assert msgs.PASTED_MAX > msgs.SNIPPET_MAX
    src = __import__("inspect").getsource(msgs.record_outbound)
    # Comments stripped: the comment beside the fix NAMES SNIPPET_MAX to explain which bound
    # was wrong and why, and a guard that cannot tell an explanation from the code it explains
    # fails on a correct file.
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "full=True" in code
    assert "PASTED_MAX" in code
    assert "SNIPPET_MAX" not in code, "the send path must not reach for the sync bound"
