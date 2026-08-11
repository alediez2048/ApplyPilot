"""`Space.must_mention` — terms every message in a campaign has to name.

Asked for on the `gauntlet` Space: every email should mention the GauntletAI programme. Its
premise ALREADY named GauntletAI twice, and the measurement is the whole reason this is a new
field rather than a longer premise:

    zero of eight live drafts mentioned it, including the two that had the premise

Nothing was broken. `_premise_block` hands over facts and says so — *"say it in your own words,
or leave it out"*, *"if this role does not fit it, leave it out"* — so the model kept the decade
at T-Mobile and Verizon and dropped the rest, which is what it was told it could do. **A
requirement is not a fact**, and the fix is a field that says the other thing.

Two design choices carry it:

**Retry, never append.** `ensure_intro_deck` may append because a deck link is a URL: one correct
string, and repeating it costs nothing. A required mention has to be a SENTENCE, and a canned
sentence lands identically in every inbox at one company — §Lessons 42, where the SMS prompt's
own suggested wording came back in five of five drafts. Asking again gets a different sentence.

**The last attempt ships even if it still falls short.** Refusing to draft leaves the operator
with nothing to edit. A draft that misses the term is visible and fixable; no draft is neither.
"""

from __future__ import annotations

import pytest

from applypilot.domain import space as sp
from applypilot.networking import outreach

GAUNTLET = sp.from_template("gauntlet", "Gauntlet", "jobs").with_(must_mention=("GauntletAI",))
PLAIN = sp.from_template("job-search", "Job Search", "jobs")


# ── the manifest carries it, with no schema change ──────────────────────────

def test_it_round_trips_through_the_config_blob():
    """The point of the manifest design: a new field is a JSON key, not a migration."""
    row = {"id": "gauntlet", "name": "Gauntlet", "template": "jobs", "shape": "pipeline/jobs",
           "config": GAUNTLET.config_json()}
    assert sp.Space.from_row(row).must_mention == ("GauntletAI",)


def test_it_is_a_tuple_even_when_stored_as_a_list():
    """JSON has no tuples, so the blob round-trips a list. A manifest field that changes type
    depending on where it was loaded from is the kind of thing that works in tests and not live."""
    row = {"id": "g", "name": "G", "template": "jobs", "shape": "pipeline/jobs",
           "config": '{"must_mention": ["GauntletAI", "Austin"]}'}
    assert sp.Space.from_row(row).must_mention == ("GauntletAI", "Austin")


def test_a_space_without_it_is_unaffected():
    assert PLAIN.must_mention == ()
    assert outreach._must_mention_block(PLAIN) == ""


# ── the check ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body,missing", [
    ("I finished the GauntletAI programme last month.", []),
    ("I finished the Gauntlet AI programme last month.", []),      # spaced
    ("GauntletAI's curriculum was intense.", []),                  # possessive
    ("i went through gauntletai.", []),                            # case
    ("I spent a decade at T-Mobile and Verizon.", ["GauntletAI"]),
    ("", ["GauntletAI"]),
])
def test_missing_mentions_is_tolerant_of_spelling_but_not_of_absence(body, missing):
    """"GauntletAI", "Gauntlet AI" and "GauntletAI's" are the same mention. Refusing two of them
    would send the model into a retry it cannot win, and every retry is a real LLM call."""
    assert outreach.missing_mentions(body, GAUNTLET) == missing


def test_a_paraphrase_is_not_a_mention():
    """The term itself has to appear. "an intensive AI engineering bootcamp" is what the operator
    is trying to stop being written instead of the name."""
    body = "I completed an intensive AI engineering bootcamp and now build agents."
    assert outreach.missing_mentions(body, GAUNTLET) == ["GauntletAI"]


def test_nothing_required_means_nothing_missing():
    assert outreach.missing_mentions("anything at all", PLAIN) == []


# ── the retry ───────────────────────────────────────────────────────────────

class _Client:
    """Returns each scripted reply in turn and records what it was asked."""

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages, **_):
        self.seen.append(messages)
        return self.replies[min(len(self.seen) - 1, len(self.replies) - 1)]


def test_a_compliant_first_draft_costs_one_call():
    c = _Client(['{"subject":"s","body":"GauntletAI taught me this.","linkedin_note":"n"}'])
    out = outreach._chat_meeting_requirements(c, "sys", "user", GAUNTLET,
                                              max_tokens=400, temperature=0.8)
    assert len(c.seen) == 1
    assert "GauntletAI" in out


def test_a_non_compliant_draft_is_retried():
    c = _Client(['{"subject":"s","body":"A decade at Verizon.","linkedin_note":"n"}',
                 '{"subject":"s","body":"GauntletAI is where I built agents.","linkedin_note":"n"}'])
    out = outreach._chat_meeting_requirements(c, "sys", "user", GAUNTLET,
                                              max_tokens=400, temperature=0.8)
    assert len(c.seen) == 2
    assert "GauntletAI" in out


def test_the_retry_names_what_was_missing():
    """A correction that does not say what was wrong is a re-roll, not a correction."""
    c = _Client(['{"subject":"s","body":"A decade at Verizon.","linkedin_note":"n"}',
                 '{"subject":"s","body":"GauntletAI.","linkedin_note":"n"}'])
    outreach._chat_meeting_requirements(c, "sys", "user", GAUNTLET,
                                        max_tokens=400, temperature=0.8)
    correction = c.seen[1][-1]["content"]
    assert "GauntletAI" in correction
    assert "bolted on" in correction, "the retry does not warn against a tacked-on sentence"


def test_it_gives_up_rather_than_looping():
    """Every attempt is a real LLM call. Two tries, then the operator gets something to edit."""
    c = _Client(['{"subject":"s","body":"Never says it.","linkedin_note":"n"}'])
    out = outreach._chat_meeting_requirements(c, "sys", "user", GAUNTLET,
                                              max_tokens=400, temperature=0.8)
    assert len(c.seen) == 2
    assert "Never says it" in out, "the last attempt must still be returned"


def test_a_space_with_no_requirement_never_retries():
    """Guard the guard: retrying unconditionally would double the cost of every draft in every
    other Space, and `job-search` holds thirty jobs."""
    c = _Client(['{"subject":"s","body":"Nothing required here.","linkedin_note":"n"}'])
    outreach._chat_meeting_requirements(c, "sys", "user", PLAIN,
                                        max_tokens=400, temperature=0.8)
    assert len(c.seen) == 1


# ── the prompt ──────────────────────────────────────────────────────────────

def test_the_block_states_a_requirement_not_a_fact():
    """The distinction the whole feature rests on. `_premise_block` says the model may leave its
    contents out; this must not, or it is a second premise."""
    block = outreach._must_mention_block(GAUNTLET)
    assert "REQUIRED IN THIS MESSAGE" in block
    assert "not a fact to consider" in block
    for permission in ("leave it out", "if it does not fit"):
        assert permission not in block.lower()


def test_the_block_bans_a_stock_sentence():
    """Several people at one company get these. The same clause in two of them proves a machine
    wrote both — §Lessons 42, which fired on a single quoted phrasing appearing in 5 of 5."""
    block = outreach._must_mention_block(GAUNTLET)
    assert "YOUR OWN WORDS" in block
    assert "bolted onto the end" in block


def test_the_brief_form_keeps_the_requirement():
    """Follow-ups get the short version. It may shorten the GUIDANCE and must never drop the
    REQUIREMENT — a channel that silently loses a layer is the bug CTX-3 exists to close."""
    brief = outreach._must_mention_block(GAUNTLET, brief=True)
    assert "GauntletAI" in brief
    assert len(brief) < len(outreach._must_mention_block(GAUNTLET))


def test_the_requirement_reaches_the_jobs_prompt():
    seen = {}

    class _C:
        def chat(self, messages, **_):
            seen["user"] = messages[-1]["content"]
            return '{"subject":"s","body":"GauntletAI.","linkedin_note":"n"}'

    real = outreach.get_client
    outreach.get_client = lambda *_a, **_k: _C()
    try:
        outreach.draft_email({"personal": {"full_name": "A", "preferred_name": "A"}},
                             {"url": "https://x.example/acme/job/1", "title": "Engineer",
                              "company": "Acme", "site": "Greenhouse",
                              "full_description": "Build things."},
                             {"id": "c1", "full_name": "Sam", "title": "Recruiter",
                              "company": "Acme", "email": "s@x.test"},
                             space=GAUNTLET)
    finally:
        outreach.get_client = real
    assert "REQUIRED IN THIS MESSAGE" in seen["user"]
    assert "GauntletAI" in seen["user"]
