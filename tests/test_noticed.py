"""The observation field — personalisation without scraping LinkedIn.

The idea was to crawl a contact's last five LinkedIn posts. Rejected, and the reasoning is
worth keeping: reading LinkedIn programmatically was abandoned twice in this codebase already
(§Lessons 3, ~2,900 lines deleted), it risks the account the entire outreach ladder runs on,
and it produces a WORSE answer than the alternative — because the "Copy note + open LinkedIn"
flow already puts a human on the profile. A scraper yields "posted about X three days ago";
five seconds of the operator's judgement yields the thing actually worth mentioning.

So: one field, filled in while you are already there, fed to the drafter.
"""

from __future__ import annotations

import pytest

from applypilot.networking import outreach

PROFILE = {"personal": {"name": "Alejandro Diez"}}
JOB = {"url": "http://j/1", "title": "Applied AI Engineer", "company": "Affirm",
       "full_description": "About the Role\nYou will build production AI systems. " * 30}

#: Every way a model has been observed to announce that it looked someone up. The prompt bans
#: the SHAPE rather than this list — the list is how the ban gets tested.
_ANNOUNCING = ("i noticed your", "i saw your", "i came across your", "your recent post",
               "noticed your post", "saw that you posted", "i read your post",
               "i stumbled upon", "having seen your")


@pytest.fixture()
def captured(monkeypatch):
    seen = {}

    class Fake:
        def chat(self, messages, **kw):
            seen["user"] = messages[1]["content"]
            return '{"subject": "hi", "body": "b", "linkedin_note": "n"}'

    monkeypatch.setattr(outreach, "get_client", lambda *_a, **_k: Fake())
    return seen


def _contact(**over):
    c = {"id": "c1", "full_name": "Ali Coppinger", "title": "HR Business Partner",
         "company": "Affirm", "email": "a@x.com"}
    c.update(over)
    return c


def test_an_observation_reaches_the_prompt(captured):
    outreach.draft_email(PROFILE, JOB, _contact(noticed="rebuilt their onboarding in Rust"))
    assert "rebuilt their onboarding in Rust" in captured["user"]


def test_no_observation_adds_no_block(captured):
    """An empty field must not leave an instruction talking about something that isn't there."""
    outreach.draft_email(PROFILE, JOB, _contact())
    assert "WHAT THE SENDER NOTICED" not in captured["user"]
    outreach.draft_email(PROFILE, JOB, _contact(noticed="   "))
    assert "WHAT THE SENDER NOTICED" not in captured["user"]


def test_the_prompt_bans_the_SHAPE_not_a_list_of_verbs(captured):
    """The first version listed "I saw your post" and "I came across your" as forbidden. The
    model wrote "I noticed your post about…" — same tell, a verb that was not on the list.
    Enumerating phrasings never covers the paraphrase space (§Lessons 42), so what is banned is
    any sentence whose job is to report that you looked."""
    outreach.draft_email(PROFILE, JOB, _contact(noticed="something"))
    user = captured["user"]
    assert "NEVER ANNOUNCE THE NOTICING" in user
    assert "It is the SHAPE that is banned, not a list of verbs" in user


def test_the_worked_example_is_off_domain(captured):
    """§Lessons 9, three times paid for. An example written in the candidate's own field comes
    back almost verbatim — a bullet example became his opening bullet, a summary example became
    his summary. The right-shape example here is about ferry timetables for that reason."""
    outreach.draft_email(PROFILE, JOB, _contact(noticed="something"))
    user = captured["user"]
    example = user[user.index("Wrong shape:"):user.index("- It is an ADDITION")]
    for term in ("recruit", "hiring", "engineer", "AI", "resume", "résumé", "candidate",
                 "role", "applied"):
        assert term.lower() not in example.lower(), (
            f"the worked example uses {term!r} — a domain word that will be parroted back")


def test_the_observation_must_not_replace_the_rest_of_the_email(captured):
    """Measured: the first version produced an email that was only the observation plus a
    calendar link. It had lost what the role involves AND the sender's background — a
    compliment, not an application."""
    outreach.draft_email(PROFILE, JOB, _contact(noticed="something"))
    assert "ADDITION, not a replacement" in captured["user"]


def test_it_is_capped_so_a_pasted_essay_cannot_take_over_the_prompt(captured):
    outreach.draft_email(PROFILE, JOB, _contact(noticed="x" * 5000))
    block = captured["user"][captured["user"].index("WHAT THE SENDER NOTICED"):]
    assert block.count("x") <= 400


def test_notes_are_not_used_for_drafting(captured):
    """`notes` is operator scratch — "called, no answer", "best reached Tuesdays". Feeding that
    to a drafting prompt is noise, which is why `noticed` is a separate column."""
    outreach.draft_email(PROFILE, JOB, _contact(notes="called twice, no answer, try Tuesday"))
    assert "no answer" not in captured["user"]


def test_it_is_stored_not_scraped():
    """The whole design decision, pinned. If something ever starts fetching this, that is the
    third attempt at reading LinkedIn programmatically and §Lessons 3 applies."""
    from applypilot.networking import store
    assert "noticed" in store._CONTACT_COLUMNS
    src = open(outreach.__file__, encoding="utf-8").read()
    block = src[src.index("noticed = (contact"):src.index("noticed = (contact") + 200]
    assert "contact.get" in block, "the observation is no longer read straight off the contact"
