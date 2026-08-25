"""The contact card must show our own messages at the fullest length we hold.

Reported as "incomplete email sequences displaying on the contact cards", with two screenshots
of emails stopping mid-sentence ("...generative AI solutions really"). Nothing was lost, and
that is the whole diagnosis: for one live contact `contacts.outreach_message` held the real
698-character email while the `messages` row the card rendered held Gmail's 194-character
preview of that SAME email. 488 of 606 outbound rows were in that state.

Two separate defects, both on the same two lines of `dashboard.js`:

  1. `m.snippet || (isFirstOut ? c.outreach_message : '')` — the snippet is non-empty, so the
     194-char preview WON over the 698-char ground truth. The fallback only ever fired when the
     snippet was completely empty.
  2. `(m.snippet||'').length >= CONV_SNIPPET_MAX` — the marker predicate §Lessons 116 already
     disproved. Gmail's snippet ends on a WORD boundary, so 194 >= 200 is false and no marker
     rendered. Meanwhile `outreach._is_clipped` had the CORRECT predicate the whole time, on
     the drafter path only: the screen and the prompt disagreed about the same message.

The fix puts ONE rule in `domain/conversations` and has both callers use it.
"""
import pytest

from applypilot.domain import conversations as cv

CUT = "x" * 180 + " solutions really"          # 197 chars, stops mid-sentence
DONE = "All good, talk then."


# ── is_clipped: both failure modes §Lessons 116 records ─────────────────────

@pytest.mark.parametrize("text,expected,why", [
    (CUT, True, "a word-boundary cut short of the cap is the reported bug"),
    ("x" * 194, True, "no terminal punctuation, so it was cut"),
    (DONE, False, "ends on a full stop"),
    ("Best, Liz", False, "a sign-off is a complete ending even with no full stop"),
    ("Thanks!", False, "sign-off with punctuation"),
    ("", False, "nothing held is not a truncation"),
    ("x" * 2000, True, "a deliberate fetch filling PASTED_MAX is holding more"),
    ("x" * 400 + ".", False, "past the snippet cap and ending cleanly"),
])
def test_is_clipped(text, expected, why):
    assert cv.is_clipped(text) is expected, why


def test_the_length_comparison_that_was_disproved_would_miss_the_reported_case():
    """The guard on the guard. If someone reinstates `len >= SNIPPET_MAX`, this states plainly
    what that rule fails to see — the exact 194-character message from the screenshots."""
    assert len(CUT) < 200, "the fixture must sit BELOW the cap or it proves nothing"
    assert cv.is_clipped(CUT) is True


# ── fuller_outbound ─────────────────────────────────────────────────────────

def _thread(**kw):
    row = {"message_id": "m1", "direction": "out", "snippet": CUT, "sent_at": "2026-08-19T09:02"}
    row.update(kw)
    return [row]


def test_the_first_email_is_matched_by_message_id_and_shown_in_full():
    full = ("the complete 698 character email we actually sent. " * 5).strip()
    out = cv.fuller_outbound(_thread(), outreach_message=full, sent_message_id="m1")
    assert out[0]["snippet"] == full
    assert out[0]["clipped"] is False, "ground truth cannot be clipped"


def test_without_a_message_id_the_first_email_is_matched_by_minute():
    """Older rows carry no `sent_message_id`; the drafter falls back to the minute and so does
    this, because two rules for 'which stored copy is this' is how the surfaces drift."""
    full = ("the complete email. " * 20).strip()
    out = cv.fuller_outbound(_thread(), outreach_message=full, sent_message_id="",
                             submitted_at="2026-08-19T09:02:41")
    assert out[0]["snippet"] == full


def test_a_follow_up_is_matched_by_minute_from_touches():
    full = ("the complete follow-up we sent. " * 10).strip()
    out = cv.fuller_outbound(_thread(message_id="m9"), outreach_message="", sent_message_id="",
                             touch_bodies={"2026-08-19T09:02": full})
    assert out[0]["snippet"] == full


def test_a_shorter_fuller_copy_is_ignored():
    """Never downgrade what we already hold. That rule has been repaired twice in this codebase
    after being applied in one direction only."""
    out = cv.fuller_outbound(_thread(), outreach_message="tiny", sent_message_id="m1")
    assert out[0]["snippet"] == CUT, "a shorter copy overwrote the longer stored text"
    assert out[0]["clipped"] is True, "and it must still be marked as cut"


def test_an_inbound_message_is_never_rewritten():
    """We hold no fuller copy of what somebody else wrote, and inventing one is not available."""
    out = cv.fuller_outbound(_thread(direction="in"), outreach_message="x" * 900,
                             sent_message_id="m1")
    assert out[0]["snippet"] == CUT
    assert out[0]["clipped"] is True, "inbound still gets the marker, which is all we can give"


def test_an_unmatched_outbound_row_keeps_its_text_and_is_marked():
    out = cv.fuller_outbound(_thread(message_id="other"), outreach_message="x" * 900,
                             sent_message_id="m1")
    assert out[0]["snippet"] == CUT
    assert out[0]["clipped"] is True


def test_the_thread_is_not_reordered_or_dropped():
    """The thread stays the SPINE: it decides order and grouping, only the text is upgraded."""
    rows = [{"message_id": "a", "direction": "out", "snippet": "one.", "sent_at": "1"},
            {"message_id": "b", "direction": "in", "snippet": "two.", "sent_at": "2"},
            {"message_id": "c", "direction": "out", "snippet": "three.", "sent_at": "3"}]
    out = cv.fuller_outbound(rows, outreach_message="", sent_message_id="")
    assert [m["message_id"] for m in out] == ["a", "b", "c"]
    assert len(out) == 3


def test_the_input_rows_are_not_mutated():
    """The payload builder passes rows that other panels also read."""
    rows = _thread()
    cv.fuller_outbound(rows, outreach_message="x" * 900, sent_message_id="m1")
    assert rows[0]["snippet"] == CUT, "the caller's own row was rewritten underneath it"


# ── the browser must not grow a second predicate ────────────────────────────

def test_the_card_renders_the_servers_flag_rather_than_measuring_length():
    """The whole bug was two predicates for one question.

    `dashboard.js` asked `(m.snippet||'').length >= CONV_SNIPPET_MAX` while
    `outreach._is_clipped` asked whether the text ends like a finished message. They disagreed
    on every word-boundary cut, which is most of them, so the prompt knew a message was cut and
    the screen did not. The browser now renders a flag it is given and decides nothing.

    Grep-shaped on purpose, and its limits are real (§Lessons 48: grep proves where a string is,
    not what the code does). It is here to fail loudly if someone reinstates the comparison.
    """
    import pathlib

    from applypilot import web_dashboard
    js = (pathlib.Path(web_dashboard.__file__).parent / "static" / "dashboard.js").read_text()
    body = js[js.index("function convMessage"):]
    body = body[:body.index("\nfunction ")]
    # COMMENTS STRIPPED FIRST. The comment above the fixed line quotes the old predicate to say
    # why it was wrong, and the first version of this test matched that quote and failed on a
    # correct file — a guard that cannot tell an explanation from the code it explains.
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("//"))
    assert "m.clipped" in code, "the card no longer reads the server's clipped flag"
    assert "length >= CONV_SNIPPET_MAX" not in code, (
        "the disproved length comparison is back in convMessage")


def test_the_server_actually_sends_the_flag():
    """A frontend test that supplies its own input can only prove the frontend is consistent
    with itself (§Lessons 93, 103). At least one assertion has to start from what the SERVER
    produces, so this asserts the key exists on the payload shape the browser is handed."""
    out = cv.fuller_outbound(_thread(), outreach_message="", sent_message_id="")
    assert "clipped" in out[0], "the payload row carries no `clipped` key for the browser to read"


def test_ground_truth_is_never_marked_clipped_even_when_it_ends_untidily():
    """What we SENT is complete by definition, whatever it ends on.

    `is_clipped` is a heuristic for text we only hold a preview of. Applying it to the full
    body we wrote ourselves puts a "…truncated" marker on a complete message — the exact false
    statement §Lessons 116 records as the one direction over-reporting may NOT go. A mutation
    that ran the heuristic over the substituted copy survived until this test existed, because
    every other fixture happened to end on a full stop.
    """
    # Deliberately SHORT and ending mid-word: under the snippet cap is the only range where the
    # heuristic actually fires, so a longer fixture proves nothing (the first version of this
    # test used one and the mutation survived it).
    full = "a short note we really did send, ending on the word really"
    assert len(full) < 200 and cv.is_clipped(full) is True, (
        "the fixture must sit under the cap AND be the kind of text the heuristic flags")
    thread = [{"message_id": "m1", "direction": "out", "snippet": "a short note we",
               "sent_at": "2026-08-19T09:02"}]
    out = cv.fuller_outbound(thread, outreach_message=full, sent_message_id="m1")
    assert out[0]["snippet"] == full
    assert out[0]["clipped"] is False, "ground truth was run through the preview heuristic"
