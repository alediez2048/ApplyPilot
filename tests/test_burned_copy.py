"""Several people at one company get these, and they sit near each other.

Reported as "I need them to not seem super robotic in case they happen to show each other".
Measured across the 189 live drafts before writing anything, and the repetition was NOT spread
evenly — it was concentrated exactly where a reader looks first:

    subject lines   Google: 16 people, 4 distinct subjects, ten of them identical
                    Saronic: 12 people, 4 subjects. Yahoo: 12 people, 4 subjects.
    the CTA         one sentence, 48 times across the corpus, 9 of 16 at Google
    the sign-off    "Looking forward to connecting!" 16 times
    openings        Salesforce: 6 of 14 people got the same first sentence

Bodies were otherwise fine — Google had 16 distinct openings across 16 people, and all 189
LinkedIn notes were distinct. That shape is the whole diagnosis: the model varies freely where
it is writing and converges wherever the prompt handed it a FORM OF WORDS. Two worked examples
were doing it, and both are §Lessons 9/42 for the fourth and fifth time.
"""

from __future__ import annotations

import pathlib
import re

from applypilot.domain.burned import burned_block, cta, opening
from applypilot.networking import outreach

_SRC = pathlib.Path(outreach.__file__).read_text(encoding="utf-8")


# ── the prompt may not hand the model a sentence to copy ────────────────────

def test_the_cta_example_is_gone():
    """`(e.g. "if you're open to a quick call, grab a time that works here: <link>")` came back
    48 times across 189 drafts, 9 of 16 at Google. §Lessons 42: the prompt's own example comes
    back verbatim, including when the example is a rule."""
    assert "grab a time that works here" not in _SRC
    assert "if you're open to a quick call" not in _SRC.lower()


def test_the_subject_example_is_gone():
    """The one I had not spotted until the measurement. `Subject: … (e.g. "quick q about the
    <role> role")` produced ten identical subject lines at Google — and a subject is visible in
    a forwarded message without anyone opening it."""
    assert "quick q about the" not in _SRC.lower()


def test_the_prompt_still_says_what_the_cta_is_FOR():
    """Deleting an example must not delete the instruction. A prompt that no longer asks for a
    booking link produces emails with no call to action, which is worse than a repeated one."""
    assert "CALL TO ACTION" in _SRC
    assert "verbatim" in _SRC, "the link must still be required to appear exactly"


def test_the_burned_sign_offs_are_named():
    """"Looking forward to connecting!" appeared 16 times. Naming the phrasings as burned is
    what fixed the same failure for SMS (§Lessons 42, 0/5 after)."""
    for phrase in ("Looking forward to connecting", "Thanks in advance", "Best regards"):
        assert phrase in _SRC, phrase


def test_the_prompt_says_recipients_may_compare():
    """The reason, not just the rule. The prompt never mentioned that several people at one
    company receive these — so nothing in it was ever weighed against sounding natural."""
    low = _SRC.lower()
    assert "side by side" in low or "compare" in low
    assert "synonym" in low, "a synonym swap into the same sentence is the obvious wrong fix"


# ── extracting what was already said ────────────────────────────────────────

def test_the_opening_skips_the_greeting():
    """"Hi Sarah," carries nothing and would make every message look identical, so the sentence
    AFTER it is the one a reader compares."""
    body = "Hi Sarah,\n\nI just applied for the Forward Deployed Engineer role at Salesforce.\n"
    assert opening(body).startswith("I just applied for the Forward Deployed")


def test_the_opening_survives_a_greeting_on_the_same_line():
    """Live drafts do both. Affirm had "Hey Matthew, I just applied for the AI Solutions..." on
    one line and a separate greeting line elsewhere."""
    got = opening("Hey Matthew, I just applied for the AI Solutions Engineer role on your team.")
    assert got.startswith("I just applied for the AI Solutions")


def test_the_cta_is_returned_WITHOUT_the_link():
    """The link is identical by design and must appear verbatim in every message. Leaving it in
    would ask the model to vary the one part it cannot change."""
    body = ("Hi,\n\nSome context here about the role. If you're open to a quick call, grab a "
            "time that works here: https://cal.com/jorge-alejandro-diez/30min.\n\nAlejandro")
    got = cta(body)
    assert got and not any("http" in g or "cal.com" in g for g in got)
    assert any("grab a time that works" in g for g in got)


def test_EVERY_link_sentence_is_returned_not_just_the_first():
    """The bug the first live run exposed. An outreach email carries TWO links, the scheduling
    link and the intro deck, and returning only the first meant the deck sentence MASKED the
    booking sentence — so the 48-times-repeated CTA was never burned at all. Six of eight
    Salesforce drafts still shared a deck line because of it."""
    body = ("Hi,\n\nContext about the role goes here.\n\n"
            "Here's a good intro deck we could go over during the call: https://site.test/intro/x\n\n"
            "If you're open to it, grab a time that works here: https://cal.com/x/30min.\n\nAlejandro")
    got = cta(body)
    assert len(got) == 2, got
    assert any("intro deck" in g for g in got) and any("grab a time" in g for g in got)


def test_no_link_means_no_cta_line():
    assert cta("Hi,\n\nJust wanted to say hello.\n\nAlejandro") == []


# ── the block itself ────────────────────────────────────────────────────────

_PREV = [
    {"subject": "quick q about the Startups Performance Lead role",
     "body": "Hi Ann,\n\nI just applied for the Startups Performance Lead role on the PACE team. "
             "If you're open to a quick call, grab a time that works here: https://cal.com/x/30min.\n\nAlejandro"},
    {"subject": "just applied for the Startups Performance Lead role at Google",
     "body": "Hi Bob,\n\nI just applied for the Startups Performance Lead role and would love your "
             "thoughts. You can grab a time that works here: https://cal.com/x/30min.\n\nAlejandro"},
]


def test_the_block_names_all_three_slots():
    """Three lists rather than one blob: a used SUBJECT does not stop a body opening the same
    way, and on the live corpus both failed independently."""
    block = burned_block(_PREV)
    assert "Subject lines already used" in block
    assert "Opening sentences already used" in block
    assert "Link sentences already used" in block
    assert "quick q about the Startups Performance Lead role" in block
    assert "I just applied for the Startups Performance Lead role on the PACE team" in block


def test_the_block_says_a_synonym_swap_is_not_enough():
    assert "different sentence" in burned_block(_PREV)


def test_it_deduplicates_so_the_block_stays_readable():
    """Twelve people at one company with the same CTA would otherwise print it twelve times,
    burying the instruction in its own evidence (§Lessons 40 — the surrounding text wins)."""
    block = burned_block(_PREV * 6)
    assert block.count("quick q about the Startups Performance Lead role") == 1


def test_nothing_sent_yet_produces_NO_block():
    """An empty heading saying "already used:" with nothing under it is an instruction to avoid
    nothing, and it costs tokens on every first contact at a new company."""
    assert burned_block([]) == ""
    assert burned_block(None) == ""
    assert burned_block([{"subject": "", "body": ""}]) == ""


def test_it_is_bounded():
    """An unbounded block puts a full inbox in front of a 400-token generation."""
    many = [{"subject": f"subject number {i}", "body": f"Hi,\n\nOpening number {i} goes here and "
             f"is long enough to count.\n"} for i in range(50)]
    block = burned_block(many)
    assert len(re.findall(r"^  - subject number", block, re.M)) <= 12


# ── it actually reaches the prompt ──────────────────────────────────────────

def test_the_block_reaches_the_generation(monkeypatch):
    """A parameter being accepted is not evidence it is used (§Lessons 39)."""
    seen = {}

    class _C:
        def chat(self, messages, **kw):
            seen["user"] = messages[-1]["content"]
            return '{"subject":"s","body":"b","linkedin_note":"n"}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _C())
    outreach.draft_email({}, {"title": "Eng"}, {"full_name": "Ada", "company": "Google"},
                         previous=_PREV)
    assert "ALREADY USED AT THIS COMPANY" in seen["user"]
    assert "quick q about the Startups Performance Lead role" in seen["user"]


def test_it_lands_AFTER_the_scheduling_and_deck_blocks(monkeypatch):
    """§Lessons 40: two instructions in one prompt disagreeing is a code bug. The scheduling
    block tells the model to include the link; this tells it not to reuse the sentence that
    carried it. The later one has to be the constraint, or it argues with the block above it
    and loses — which is exactly how the SMS ladder beat its own standing rules."""
    seen = {}

    class _C:
        def chat(self, messages, **kw):
            seen["user"] = messages[-1]["content"]
            return '{"subject":"s","body":"b","linkedin_note":"n"}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _C())
    monkeypatch.setenv("SCHEDULING_LINK", "https://cal.com/x/30min")
    outreach.draft_email({}, {"title": "Eng"}, {"full_name": "Ada", "company": "Google"},
                         previous=_PREV)
    user = seen["user"]
    assert user.index("SCHEDULING LINK") < user.index("ALREADY USED AT THIS COMPANY")
    assert user.index("ALREADY USED AT THIS COMPANY") < user.index("Write the outreach email")


def test_a_first_contact_at_a_new_company_gets_no_block(monkeypatch):
    seen = {}

    class _C:
        def chat(self, messages, **kw):
            seen["user"] = messages[-1]["content"]
            return '{"subject":"s","body":"b","linkedin_note":"n"}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _C())
    outreach.draft_email({}, {"title": "Eng"}, {"full_name": "Ada", "company": "NewCo"})
    assert "ALREADY USED" not in seen["user"]


# ── the store side ──────────────────────────────────────────────────────────

def _db(tmp_path, monkeypatch):
    import applypilot.database as database
    from applypilot.networking import store
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    return conn


def _c(conn, name, company, subject, body, status="drafted"):
    from applypilot.networking import store
    return store.upsert_contact({
        "job_url": "http://j/1", "full_name": name, "company": company,
        "outreach_subject": subject, "outreach_message": body,
        "outreach_status": status}, conn)


def test_it_returns_what_that_company_already_got(tmp_path, monkeypatch):
    from applypilot.networking import store
    conn = _db(tmp_path, monkeypatch)
    _c(conn, "Ann", "Google", "quick q about the role", "Hi Ann,\n\nOpening one.\n")
    _c(conn, "Bob", "Google", "another subject", "Hi Bob,\n\nOpening two.\n")
    got = store.copy_already_sent_to_company("Google", conn=conn)
    assert {g["subject"] for g in got} == {"quick q about the role", "another subject"}


def test_it_never_leaks_across_companies(tmp_path, monkeypatch):
    """Two employers repeating each other is invisible to their recipients and would only make
    the copy worse for no reason."""
    from applypilot.networking import store
    conn = _db(tmp_path, monkeypatch)
    _c(conn, "Ann", "Google", "google subject", "Hi Ann,\n\nOpening.\n")
    got = store.copy_already_sent_to_company("Yahoo", conn=conn)
    assert got == []


def test_it_excludes_the_person_being_drafted(tmp_path, monkeypatch):
    """Regenerating a draft must not tell the model to avoid that contact's OWN previous copy
    and nothing else — it would burn the best sentence available and change nothing about who
    can compare notes."""
    from applypilot.networking import store
    conn = _db(tmp_path, monkeypatch)
    cid = _c(conn, "Ann", "Google", "ann subject", "Hi Ann,\n\nOpening.\n")
    assert store.copy_already_sent_to_company("Google", exclude_id=cid, conn=conn) == []


def test_DRAFTED_counts_not_only_sent(tmp_path, monkeypatch):
    """A whole company is drafted minutes apart in one batch. Waiting for `submitted` would let
    the entire batch repeat itself before any feedback existed — which is what produced ten
    identical Google subject lines in the first place."""
    from applypilot.networking import store
    conn = _db(tmp_path, monkeypatch)
    _c(conn, "Ann", "Google", "ann subject", "Hi Ann,\n\nOpening.\n", status="drafted")
    assert len(store.copy_already_sent_to_company("Google", conn=conn)) == 1


def test_a_contact_with_no_draft_is_not_returned(tmp_path, monkeypatch):
    from applypilot.networking import store
    conn = _db(tmp_path, monkeypatch)
    store.upsert_contact({"job_url": "http://j/1", "full_name": "Ann", "company": "Google"}, conn)
    assert store.copy_already_sent_to_company("Google", conn=conn) == []


def test_an_empty_company_returns_nothing(tmp_path, monkeypatch):
    """Otherwise every contact whose employer never resolved would burn each other's copy."""
    from applypilot.networking import store
    conn = _db(tmp_path, monkeypatch)
    _c(conn, "Ann", "", "s", "Hi Ann,\n\nOpening.\n")
    assert store.copy_already_sent_to_company("", conn=conn) == []
