"""The reply drafter reads the WHOLE conversation, not the first email and the last message.

Reported as: "the draft an answer button is not using the context from the whole thread."

`conversation_transcript` took a `thread` argument and read exactly two fields off it — the last
inbound message's sender name and its date. Everything it actually rendered came from
`contacts.outreach_message`, `touches`, and ONE reply passed in separately. §Lessons 39 recorded
that precise shape for this precise function ("a function that takes a thread may not read the
thread"), and it was still true of every message except the newest.

Measured on the live database before the fix:

    Lee Ackerley       87 messages (45 inbound)  ->  transcript showed 2 entries
    Kevin Parakkattu   61 messages (36 inbound)  ->  2
    Diego Bodart       18 messages (12 inbound)  ->  2

So a reply advertised as "written from the whole sequence" was written from the opening email and
the newest message — with every answer WE had already given invisible, which is how a drafter
re-asks a question that was settled four messages ago.
"""

from __future__ import annotations

from applypilot.networking.outreach import conversation_transcript


def msg(direction, body, at, name="", mid="", subject="hello"):
    return {"message_id": mid or f"m-{at}", "direction": direction, "snippet": body,
            "sent_at": at, "from_name": name, "subject": subject,
            "from_addr": "them@x.test" if direction == "in" else "me@x.test",
            "to_addrs": [], "cc_addrs": []}


CONTACT = {"full_name": "Diego", "outreach_message": "my opening cold email",
           "outreach_subject": "quick question", "submitted_at": "2026-07-01T09:00",
           "sent_message_id": ""}


def test_every_inbound_message_is_in_the_transcript():
    """The report, in one assertion. Only the newest reply used to appear."""
    thread = [msg("in", "their first answer", "2026-07-02T09:00", "Diego"),
              msg("in", "their second answer", "2026-07-05T09:00", "Diego"),
              msg("in", "their third answer", "2026-07-09T09:00", "Diego")]
    got = conversation_transcript(CONTACT, thread, [], "")
    for n in ("first", "second", "third"):
        assert f"their {n} answer" in got, f"{n} inbound message is missing"


def test_our_own_REPLIES_are_in_it_too():
    """A drafter that cannot see what we already answered re-answers it. These live only in
    `messages` — `touches` holds follow-ups, not replies."""
    thread = [msg("in", "do you know Kubernetes?", "2026-07-02T09:00", "Diego"),
              msg("out", "yes, three years of it at T-Mobile", "2026-07-03T09:00"),
              msg("in", "great, and Terraform?", "2026-07-04T09:00", "Diego")]
    got = conversation_transcript(CONTACT, thread, [], "")
    assert "three years of it at T-Mobile" in got, "our own answer is invisible to the drafter"


def test_it_is_in_CHRONOLOGICAL_order():
    """Out of order, "you said X then I said Y" inverts and the model answers the wrong turn."""
    thread = [msg("in", "SECOND", "2026-07-05T09:00", "D"),
              msg("in", "FIRST", "2026-07-02T09:00", "D")]
    got = conversation_transcript(CONTACT, thread, [], "")
    assert got.index("my opening cold email") < got.index("FIRST") < got.index("SECOND")


def test_a_message_we_hold_in_FULL_is_not_also_shown_as_a_snippet():
    """`messages.snippet` is capped at 200 chars for synced mail while `contacts` and `touches`
    hold the text as typed. Shown twice, the model reads the repetition as emphasis."""
    c = dict(CONTACT, sent_message_id="gm1")
    thread = [msg("out", "my opening cold em", "2026-07-01T11:42", mid="gm1"),
              msg("in", "sure", "2026-07-02T09:00", "D")]
    got = conversation_transcript(c, thread, [], "")
    assert got.count("my opening cold em") == 1


def test_a_FOLLOW_UP_is_matched_by_timestamp_and_not_duplicated():
    """`touches` carries no message id, so an outbound thread copy is matched by minute."""
    touches = [{"body": "just circling back on this", "sent_at": "2026-07-04T09:00:11"}]
    thread = [msg("out", "just circling back", "2026-07-04T09:00:47"),
              msg("in", "got it", "2026-07-06T09:00", "D")]
    got = conversation_transcript(CONTACT, thread, touches, "")
    assert got.count("just circling back") == 1
    assert "just circling back on this" in got, "the fuller stored copy was the one dropped"


def test_a_PASTED_reply_replaces_the_stored_snippet_rather_than_repeating_it():
    """The operator's transcription is usually fuller than the 200-char snippet. Appended, the
    same message appears twice."""
    thread = [msg("in", "we should talk about the r", "2026-07-08T09:00", "Diego")]
    got = conversation_transcript(CONTACT, thread, [],
                                  "we should talk about the role next week, does Tuesday work?")
    assert "does Tuesday work?" in got
    assert got.count("we should talk about the r") == 1


def test_a_long_thread_is_TRIMMED_but_keeps_the_opening():
    """87 messages would blow the prompt and bury the part being answered. The first message says
    what the conversation IS and is what a late reply still implicitly answers, so it is never
    what gets dropped."""
    thread = [msg("in", f"message number {i} " + "x" * 400, f"2026-07-{i:02d}T09:00", "D")
              for i in range(1, 29)]
    got = conversation_transcript(CONTACT, thread, [], "")
    assert "my opening cold email" in got, "the opening was trimmed away"
    assert "message number 28" in got, "the most recent message was trimmed away"
    assert "omitted" in got, "the gap is silent — the model cannot know anything is missing"
    assert len(got) < 12000


def test_a_short_thread_is_NOT_trimmed():
    """The negative control: without it, "always trim" passes the test above."""
    thread = [msg("in", "hi", "2026-07-02T09:00", "D"), msg("out", "hello", "2026-07-03T09:00")]
    assert "omitted" not in conversation_transcript(CONTACT, thread, [], "")


def test_the_sender_of_each_inbound_message_is_NAMED():
    """Three people on a thread and every message labelled "THEY" is a transcript the model
    cannot reason about."""
    thread = [msg("in", "from kevin", "2026-07-02T09:00", "Kevin Parakkattu"),
              msg("in", "from john", "2026-07-03T09:00", "John Rohrer")]
    got = conversation_transcript(CONTACT, thread, [], "")
    assert "KEVIN PARAKKATTU REPLIED" in got and "JOHN ROHRER REPLIED" in got


def test_an_empty_thread_still_renders_what_we_sent():
    """A contact emailed once with no answer yet — `draft_reply` refuses elsewhere, but the
    transcript must not blow up on the way there."""
    got = conversation_transcript(CONTACT, [], [], "")
    assert "my opening cold email" in got


def test_nothing_at_all_is_an_empty_string():
    assert conversation_transcript({}, [], [], "") == ""
