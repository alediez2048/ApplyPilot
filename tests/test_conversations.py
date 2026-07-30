"""CRM-4a — knowing what a conversation IS, not just that one happened.

The motivating case is real, and is the first reply CRM-1 ever detected:

    1. me        ->  victoria.shearer@writer.com
    2. Victoria  ->  me       CC: David Loveless <david@writer.com>   <- a handoff
    3. me        ->  Victoria CC: David

Victoria answered by introducing a colleague. Recording that as `replied=True` stopped her
ladder, marked the job finished, and lost the only live conversation in the database — David
did not exist anywhere in the system.

All of this works on `gmail.metadata`. No message body is read or stored anywhere here.
"""

from __future__ import annotations

import pytest

from applypilot.domain import conversations as cv

ME = "jorgealejandrodiezm@gmail.com"

WRITER_THREAD = [
    {"id": "m1", "internalDate": "1000", "from": f"<{ME}>",
     "to": "victoria.shearer@writer.com", "cc": "", "subject": "quick q about the Writer role"},
    {"id": "m2", "internalDate": "2000", "from": "Victoria Shearer <victoria.shearer@writer.com>",
     "to": f"<{ME}>", "cc": "David Loveless <david@writer.com>",
     "subject": "Re: quick q about the Writer role"},
    {"id": "m3", "internalDate": "3000", "from": f"Alejandro Diez <{ME}>",
     "to": "Victoria Shearer <victoria.shearer@writer.com>",
     "cc": "David Loveless <david@writer.com>", "subject": "Re: quick q about the Writer role"},
]


# ── address handling: getting this wrong is how a system emails itself ───────────────────

@pytest.mark.parametrize("raw,want", [
    ("David Loveless <david@writer.com>", "david@writer.com"),
    ("<DAVID@Writer.com>", "david@writer.com"),
    ("david@writer.com", "david@writer.com"),
    ('"Loveless, David" <david@writer.com>', "david@writer.com"),
])
def test_addresses_are_normalised(raw, want):
    assert cv.addr(raw) == want


def test_a_comma_inside_a_quoted_name_does_not_split_the_header():
    """'"Loveless, David" <d@x>, jo@y' is TWO recipients, not three."""
    got = cv.split_addrs('"Loveless, David" <david@writer.com>, jo@y.com')
    assert got == ["david@writer.com", "jo@y.com"]


def test_display_names_survive_the_split():
    """Splitting to bare addresses first loses the name for good — "David Loveless" degrades
    to "David", because the only fallback left is the local part."""
    parts = cv.split_parts("David Loveless <david@writer.com>, jo@y.com")
    assert cv.display_name(parts[0]) == "David Loveless"


def test_a_name_is_derived_from_the_local_part_when_absent():
    assert cv.display_name("mary.jane@x.com") == "Mary Jane"


# ── the handoff ─────────────────────────────────────────────────────────────────────────

def test_the_writer_handoff_is_detected():
    """The exact case. David arrives as a Cc on a message from THEM."""
    intros = cv.introductions(WRITER_THREAD, ME, known=["victoria.shearer@writer.com"])
    assert len(intros) == 1
    assert intros[0]["email"] == "david@writer.com"
    assert intros[0]["name"] == "David Loveless"
    assert intros[0]["introduced_by_name"] == "Victoria Shearer"


def test_people_WE_added_are_not_introductions():
    """We already know about anyone we chose to email. Only the other side can introduce."""
    thread = [{"id": "m1", "internalDate": "1", "from": f"<{ME}>",
               "to": "a@x.com", "cc": "colleague@x.com", "subject": "hi"}]
    assert cv.introductions(thread, ME, known=["a@x.com"]) == []


def test_the_contact_we_already_track_is_not_an_introduction():
    assert cv.introductions(WRITER_THREAD, ME, known=["victoria.shearer@writer.com",
                                                      "david@writer.com"]) == []


def test_we_are_never_our_own_introduction():
    intros = cv.introductions(WRITER_THREAD, ME, known=[])
    assert ME not in [i["email"] for i in intros]


@pytest.mark.parametrize("robot", [
    "noreply@writer.com", "no-reply@x.com", "donotreply@x.com",
    "notifications@x.com", "mailer-daemon@x.com", "postmaster@x.com",
    "candidates@greenhouse.io", "x@jobs.ashbyhq.com", "invites@calendly.com",
])
def test_robots_are_never_introduced_as_people(robot):
    """Threads collect schedulers, ATS notifications and noreply@ addresses. An auto-added
    contact is one an automated ladder would then EMAIL — this filter is what stands between
    "a new participant appeared" and "we emailed a no-reply mailbox"."""
    thread = [{"id": "m1", "internalDate": "1", "from": "them@writer.com",
               "to": f"<{ME}>", "cc": robot, "subject": "Re: hi"}]
    assert cv.introductions(thread, ME, known=["them@writer.com"]) == []


def test_a_real_person_at_an_ats_free_domain_is_still_introduced():
    """The robot filter must not swallow genuine colleagues."""
    thread = [{"id": "m1", "internalDate": "1", "from": "them@writer.com",
               "to": f"<{ME}>", "cc": "Real Person <real.person@writer.com>", "subject": "Re: hi"}]
    intros = cv.introductions(thread, ME, known=["them@writer.com"])
    assert [i["email"] for i in intros] == ["real.person@writer.com"]


# ── participants and timeline ───────────────────────────────────────────────────────────

def test_participants_include_cc_not_just_senders():
    """An introduction usually arrives as a Cc — reading senders alone misses it entirely."""
    people = {p["email"] for p in cv.participants(WRITER_THREAD, ME)}
    assert people == {"victoria.shearer@writer.com", "david@writer.com"}
    assert ME not in people


def test_the_timeline_marks_direction():
    rows = cv.timeline(WRITER_THREAD, ME)
    assert [r["direction"] for r in rows] == ["out", "in", "out"]
    assert rows[1]["from_name"] == "Victoria Shearer"
    assert rows[1]["cc_addrs"] == ["david@writer.com"]


def test_the_timeline_sorts_numerically_not_lexicographically():
    """internalDate is ms-since-epoch as a STRING: "9999" sorts after "10000" as text."""
    msgs = [{"id": "later", "internalDate": "10000", "from": f"<{ME}>", "to": "", "cc": ""},
            {"id": "earlier", "internalDate": "9999", "from": f"<{ME}>", "to": "", "cc": ""}]
    assert [r["id"] for r in cv.timeline(msgs, ME)] == ["earlier", "later"]


# ── storage: headers only ───────────────────────────────────────────────────────────────

def test_the_messages_table_cannot_hold_a_body():
    """The schema is the guarantee, not a docstring. Storing conversations already changes what
    a leak of the DB costs; bodies would make it correspondence."""
    from applypilot.networking.messages import _MESSAGE_COLUMNS

    cols = set(_MESSAGE_COLUMNS)
    assert not (cols & {"body", "snippet", "content", "text", "html"})
