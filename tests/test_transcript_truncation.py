"""The drafter must never be lied to about where a message ends.

Reported as *"when I try drafting a response the drafting is not accurate"*, with a screenshot
that is the whole ticket: Yukiko's reply was stored as Gmail's ~200-character PREVIEW, ending
*"Could you kindly let me know"*, the transcript presented it as her complete message, and the
model answered *"your message got cut off at the end — what were you about to ask?"* — a sentence
about ApplyPilot's own storage, addressed to a recruiter whose email was intact, sitting in a
composer one click from Send.

Two fixes, and they are independent on purpose:

  * drafting now READS the conversation in full from Gmail first, so the previews stop being what
    the model sees at all;
  * anything still truncated is MARKED, so the residual case is harmless rather than invisible.

The second is the one that has to hold when the first cannot run — no scope, no network, a
subject-keyed thread with no Gmail id.
"""

from __future__ import annotations

from applypilot.networking import outreach
from applypilot.networking.messages import PASTED_MAX, SNIPPET_MAX

MARK = "TRUNCATED BY APPLYPILOT"


# ── which messages are cut ──────────────────────────────────────────────────

def test_a_preview_that_stops_mid_sentence_is_clipped():
    assert outreach._is_clipped("Hi Alejandro, thanks for reaching out. Could you kindly let me know")


def test_a_message_ending_in_terminal_punctuation_is_not_clipped():
    for ending in ("Thanks for the note.", "Are you free Friday?", "Works for me!",
                   "he said “no thanks”", "call me (any time)", "here it is:"):
        assert not outreach._is_clipped(ending), ending


def test_length_alone_CANNOT_answer_this():
    """The first version compared length to `SNIPPET_MAX` and that is wrong: Gmail's snippet ends
    on a WORD boundary, not at exactly 200 characters. 236 live rows sat between 150 and 195, so
    the cap test missed nearly all of them — including a message in the very transcript used to
    check the fix, cut at 194 characters ending "On a"."""
    short_cut = "x" * 150 + " and then he said On a"
    assert len(short_cut) < SNIPPET_MAX
    assert outreach._is_clipped(short_cut), "a cut well short of the cap must still be caught"


def test_a_deliberate_fetch_that_FILLS_its_bound_is_clipped():
    """`PASTED_MAX` reached means there was more; the ending is irrelevant."""
    assert outreach._is_clipped("a" * PASTED_MAX + ".")


def test_a_long_message_that_ends_cleanly_is_not_clipped():
    body = "a" * (SNIPPET_MAX + 50) + "."
    assert not outreach._is_clipped(body)


def test_empty_is_not_clipped():
    assert not outreach._is_clipped("")
    assert not outreach._is_clipped("   ")


def test_over_reporting_is_the_SAFE_direction_on_AMBIGUOUS_text():
    """A false positive only tells the model not to lean on the last sentence; a false negative
    invites it to answer OUR cut as though the sender wrote it. So genuinely ambiguous endings
    err toward marked.

    This test asserted `_is_clipped("Best, Gina")` when it was first written, and that was wrong
    in a way worth recording: over-reporting being the safe DIRECTION is not a licence to state
    something known to be untrue. "Best, Liz" is a finished message, and a marker reading "this
    message continues" about it puts a false claim in the prompt — the thing §Lessons 40 and 42
    are both about. Live, it fired on 2 of 3 messages on the Sentilink card.

    Rewritten around the new decision rather than deleted, because a test that pins the old one
    holds the bug in place more firmly than the code does (§Lessons 85, 99).
    """
    # A trailing fragment with no sign-off and no punctuation stays marked.
    assert outreach._is_clipped("and then I spoke to the hiring manager about")
    # A recognised sign-off does not.
    assert not outreach._is_clipped("I will be in touch. Best, Gina")


# ── the marker reaches the transcript ───────────────────────────────────────

def _contact(**kw):
    return {"id": "c1", "job_url": "j1", "full_name": "Gina",
            "outreach_message": "", "outreach_subject": "", "submitted_at": "", **kw}


def _msg(direction, snippet, at="2026-08-06T10:00:00+00:00"):
    return {"message_id": f"m-{at}-{direction}", "thread_id": "t1", "direction": direction,
            "from_name": "Gina", "sent_at": at, "subject": "Re: the role", "snippet": snippet}


def test_a_clipped_message_carries_the_marker_in_the_transcript():
    t = outreach.conversation_transcript(
        _contact(), thread=[_msg("in", "Thanks for reaching out. Could you kindly let me know")])
    assert MARK in t


def test_the_marker_says_the_cut_is_OURS_and_forbids_mentioning_it():
    """The model's failure was reasonable given its input, so the instruction has to name the
    three things it actually did: comment on the cut, ask what was coming, treat the sentence as
    unfinished."""
    t = outreach.conversation_transcript(
        _contact(), thread=[_msg("in", "Could you kindly let me know")])
    line = next(ln for ln in t.splitlines() if MARK in ln)
    assert "OURS" in line and "not the sender" in line
    assert "Do NOT mention it" in line
    assert "about to say" in line and "unfinished" in line


def test_the_marker_sits_INSIDE_the_message_it_describes():
    """§Lessons 40: a rule in the standing instructions is about the transcript in general, and
    the heading wins. This is a fact about one sentence, so it goes on the line where that
    sentence stops."""
    t = outreach.conversation_transcript(
        _contact(),
        thread=[_msg("in", "Cut here and now", at="2026-08-06T10:00:00+00:00"),
                _msg("out", "A complete answer.", at="2026-08-07T10:00:00+00:00")])
    body = t[t.index("Cut here and now"):]
    assert body.index(MARK) < body.index("A complete answer."), \
        "the marker must attach to the clipped message, not float to the end"


def test_a_complete_message_gets_NO_marker():
    t = outreach.conversation_transcript(
        _contact(), thread=[_msg("in", "Thanks — I will get back to you on Friday.")])
    assert MARK not in t


def test_every_clipped_message_is_marked_not_just_the_first():
    """The live Miro card had two: their reply AND our own follow-up, both previews."""
    t = outreach.conversation_transcript(
        _contact(),
        thread=[_msg("in", "Their message cut off here", at="2026-08-06T10:00:00+00:00"),
                _msg("out", "Ours cut off here too", at="2026-08-07T10:00:00+00:00")])
    assert t.count(MARK) == 2


# ── drafting reads the conversation first ───────────────────────────────────

def test_drafting_pulls_the_thread_text_before_building_the_transcript():
    """Order matters and nothing else can check it: fetching AFTER the transcript is built leaves
    the model reading the previews the fetch just replaced."""
    import inspect
    from applypilot.web_dashboard import _draft_reply
    src = inspect.getsource(_draft_reply)
    assert "_pull_thread_text" in src
    assert src.index("_pull_thread_text") < src.index("_msgs.thread_for_contact"), \
        "the fetch must happen before the thread is read back"


def test_the_pull_is_scoped_to_the_conversation_the_composer_names():
    """`contacts.thread_id` is the thread captured at SEND time, so falling back to it on a
    multi-thread contact fetches the one we started rather than the one on screen."""
    import inspect
    from applypilot.web_dashboard import _pull_thread_text
    src = inspect.getsource(_pull_thread_text)
    assert "thread_id=key" in src


def test_a_subject_keyed_thread_is_SKIPPED_rather_than_guessed_at():
    """`thread_key` is a Gmail id or `subj:<normalised>`. Only the first can be fetched, and
    handing the second to Gmail as an id would be a request for a thread that does not exist."""
    from applypilot.web_dashboard import _pull_thread_text
    assert _pull_thread_text({"id": "c1"}, "subj:re the role", None) == 0


def test_a_failed_pull_never_breaks_the_draft(monkeypatch):
    """No scope, no network or a Gmail hiccup must not turn "draft me an answer" into an error —
    the previews are still there and the truncation is marked either way."""
    from applypilot import web_dashboard as wd
    from applypilot.networking import replies as _replies
    monkeypatch.setattr(_replies, "fetch_thread_text",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network")))
    assert wd._pull_thread_text({"id": "c1"}, "19ff0fc0be0647c1", None) == 0


def test_the_automatic_poller_still_stores_no_text():
    """The narrowing is unchanged and this is where it would quietly go. Reading a body on the
    five-minute poll is what the whole design refuses; reading one when the operator asks for a
    draft of ONE conversation is the request `⤓ Fetch from Gmail` already answers."""
    import inspect
    from applypilot.networking import replies
    src = inspect.getsource(replies._sync_thread)
    assert '"snippet": ""' in src
    assert "message_body" not in src
