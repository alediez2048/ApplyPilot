"""The 120-word cap, enforced in code rather than asked for in the prompt.

All three first-contact voices state the cap. Measured against the live model with it stated
three ways at once (a hard cap, "count the words before you return", and an explicit order of
what to cut first), 2 of 5 drafts still came back at 131 and 160 words. Models do not count
reliably, so the prompt is the request and this is the guarantee — §Lessons 9 and 12, the same
split that gave `ensure_intro_deck` and `ensure_requisition`.

A RETRY, never a truncation. Cutting an email to a word count leaves it ending mid-sentence,
and a message that stops mid-thought reads as broken in a way a slightly long one does not.
`ensure_intro_deck` may append because a URL is one correct string; prose is not (§Lessons 87).
"""
import json

import pytest

from applypilot.networking import outreach


class _Client:
    """Returns each scripted reply in turn and records what it was asked."""

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages, **_):
        self.seen.append(messages)
        return self.replies[min(len(self.seen) - 1, len(self.replies) - 1)]


def draft(words: int) -> str:
    return json.dumps({"subject": "s", "body": " ".join(["word"] * words), "linkedin_note": "n"})


def test_the_prompt_and_the_code_agree_on_the_cap():
    """A bound written in two places is two bounds. That is how the intro-deck PDF rode along on
    34 real emails while `doctor --config` reported it off."""
    for name in ("_SYSTEM", "_PITCH_SYSTEM", "_PREMISE_SYSTEM"):
        assert f"{outreach._BODY_WORD_CAP} words" in getattr(outreach, name), (
            f"{name} states a different cap from _BODY_WORD_CAP")


@pytest.mark.parametrize("words,expected", [
    (10, 0),
    (outreach._BODY_WORD_CAP, 0),          # exactly at the cap is not over it
    (outreach._BODY_WORD_CAP + 1, outreach._BODY_WORD_CAP + 1),
    (300, 300),
])
def test_too_long_reports_the_count_only_when_over(words, expected):
    assert outreach._too_long(draft(words)) == expected


def test_unparseable_output_is_not_a_length_problem():
    """It raises later with a better message than a length retry would produce."""
    assert outreach._too_long("not json at all") == 0


def test_a_short_draft_costs_one_call():
    c = _Client([draft(60)])
    outreach._chat_meeting_requirements(c, "sys", "user", None, max_tokens=400, temperature=0.8)
    assert len(c.seen) == 1, "a compliant draft must not be re-rolled"


def test_an_overlong_draft_is_retried():
    c = _Client([draft(160), draft(80)])
    out = outreach._chat_meeting_requirements(c, "sys", "user", None,
                                              max_tokens=400, temperature=0.8)
    assert len(c.seen) == 2
    assert len(json.loads(out)["body"].split()) == 80


def test_the_retry_gives_back_the_count_and_the_cut_order():
    """The model cannot measure the overrun, so the correction states it. The cut order repeats
    the prompt's own, or the retry contradicts the standing rule (§Lessons 40)."""
    c = _Client([draft(160), draft(80)])
    outreach._chat_meeting_requirements(c, "sys", "user", None, max_tokens=400, temperature=0.8)
    correction = c.seen[1][-1]["content"]
    assert "160 words" in correction, "the retry does not say how long the draft actually was"
    assert "background" in correction, "the retry does not say what to cut first"
    assert "out" in correction and "question" in correction, "the retry does not protect the ask"


def test_a_draft_that_stays_long_is_returned_rather_than_truncated():
    """Two tries, then the operator gets something to edit. Never a body cut mid-sentence."""
    c = _Client([draft(200), draft(190)])
    out = outreach._chat_meeting_requirements(c, "sys", "user", None,
                                              max_tokens=400, temperature=0.8)
    assert len(c.seen) == 2, "every attempt is a real LLM call; it must not loop"
    assert len(json.loads(out)["body"].split()) == 190, "the last attempt was altered"
