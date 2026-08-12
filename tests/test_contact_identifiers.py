"""Adding an email or a LinkedIn URL to a contact that arrived without one.

A pasted sheet supplies whatever its columns hold, and the first real one held very little: of
105 imported people **85 had no email address and none had a LinkedIn URL**. Every one of them
was a person the operator could name, could not write to, and — until this — could not fix.
Reported as *"I'm unable to add linkedin accounts for existing contacts with emails"*.

Two halves are tested here. The cleaning is pure and shared with the sheet parser, because two
paths writing one field is how one of them enforces a rule and the other quietly does not
(§Lessons 49). The endpoint is where the `None` vs `""` distinction lives: a field the browser
did not render must be left ALONE, not cleared (§Lessons 75).
"""

from __future__ import annotations

import pytest

import applypilot.web_dashboard as wd
from applypilot.domain import contactfield as cf
from applypilot.domain import sheet


# ── the cleaning ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("given,want", [
    ("https://www.linkedin.com/in/danaokafor", "https://www.linkedin.com/in/danaokafor"),
    ("/in/danaokafor", "https://www.linkedin.com/in/danaokafor"),
    ("in/danaokafor", "https://www.linkedin.com/in/danaokafor"),
    # A bare handle is what you have in your hand after copying the last segment out of the
    # address bar. Stored raw it renders as a link that navigates nowhere.
    ("danaokafor", "https://www.linkedin.com/in/danaokafor"),
    ("dana-okafor-1a2b3c", "https://www.linkedin.com/in/dana-okafor-1a2b3c"),
    ("", ""),
    ("   ", ""),
])
def test_a_linkedin_reference_becomes_a_usable_url(given, want):
    assert cf.clean_linkedin(given) == want


@pytest.mark.parametrize("given", [
    "linkedin.com/company/acme",              # a company page, not a profile
    "https://www.linkedin.com/search/results/all/?keywords=dana",
    "acme.com",                               # has a dot: a domain, not a handle
    "Dana Okafor",                            # has a space: prose
])
def test_anything_that_is_not_a_handle_is_left_exactly_as_given(given):
    """Guessing here is worse than storing what was typed. A sheet legitimately carries a company
    page or a search link, and rewriting one into `/in/<the whole thing>` invents a profile."""
    assert cf.clean_linkedin(given) == given


@pytest.mark.parametrize("given,want", [
    ("  Dana@Ridge.TEST ", "dana@ridge.test"),
    ("dana@ridge.test", "dana@ridge.test"),
])
def test_an_address_is_lowercased_and_trimmed(given, want):
    assert cf.clean_email(given) == want


@pytest.mark.parametrize("given", ["dana", "dana@", "@ridge.test", "dana ridge@test.com", ""])
def test_an_address_that_reaches_nobody_is_not_an_address(given):
    """Storing it buys a field that looks populated and a send that fails later, further from
    the typo that caused it."""
    assert cf.clean_email(given) == ""


def test_the_refusal_names_what_was_wrong_and_is_silent_when_nothing_is():
    assert cf.email_problem("") == ""
    assert cf.email_problem("dana@ridge.test") == ""
    problem = cf.email_problem("dana")
    assert "dana" in problem and problem


def test_the_sheet_parser_and_the_contact_card_clean_identically():
    """ONE implementation, asserted by running the sheet path and comparing.

    Two copies is how a pasted handle becomes a URL and a typed one stays a dead string — and
    nothing would raise, because both are valid values for the column.
    """
    text = "\t".join(["Company", "Name", "LinkedIn", "Email"]) + "\n" + \
           "\t".join(["Apex", "Frank Tiemann", "franktiemann", "  Frank@APEX.test "])
    person = sheet.parse(text)["people"][0]
    assert person["linkedin_url"] == cf.clean_linkedin("franktiemann")
    assert person["email"] == cf.clean_email("  Frank@APEX.test ")
    assert person["linkedin_url"].startswith("https://www.linkedin.com/in/")


# ── the endpoint ────────────────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path, monkeypatch):
    import applypilot.database as database
    from applypilot.networking import store
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)
    c = database.get_connection(db)
    store.init_contacts(c)
    store.upsert_contact({"id": "c1", "job_url": "target:sheet-search:apex",
                          "full_name": "Lanie Okafor", "company": "Apex",
                          "source": "import", "email_status": "none"})
    return c


def row(conn, *cols):
    return conn.execute(f"SELECT {', '.join(cols)} FROM contacts WHERE id='c1'").fetchone()


def test_an_address_can_be_added_to_a_contact_that_had_none(conn):
    assert wd._save_contact_details({"contact_id": "c1", "email": " Lanie@Apex.TEST "})["ok"]
    r = row(conn, "email", "email_status")
    assert r["email"] == "lanie@apex.test"
    # `verified` is a claim about the ADDRESS, and typing one is not evidence for it.
    assert r["email_status"] == "unverified"


def test_a_linkedin_url_can_be_added_to_a_contact_that_already_has_an_email(conn):
    """The reported case exactly: an imported contact with an address and no profile."""
    wd._save_contact_details({"contact_id": "c1", "email": "lanie@apex.test"})
    assert wd._save_contact_details({"contact_id": "c1", "linkedin_url": "lanieokafor"})["ok"]
    r = row(conn, "email", "linkedin_url")
    assert r["linkedin_url"] == "https://www.linkedin.com/in/lanieokafor"
    assert r["email"] == "lanie@apex.test", "adding a profile wiped the address"


def test_a_bad_address_is_REFUSED_and_nothing_is_written(conn):
    """A silently dropped field is an edit the operator watched succeed and which never
    happened — they find out when a send fails days later (§Lessons 75)."""
    out = wd._save_contact_details({"contact_id": "c1", "email": "lanie-at-apex"})
    assert out["ok"] is False
    assert "lanie-at-apex" in out["message"]
    assert (row(conn, "email")["email"] or "") == "", "a refused address was stored anyway"


def test_a_field_the_caller_did_not_SEND_is_left_alone(conn):
    """The whole reason the add-pane can render one box without wiping the other two.

    `upsert_contact` skips None and WRITES "", so a handler that defaulted a missing key to ""
    would blank the email every time a LinkedIn URL was saved — which is the bug SHEET-1b fixed
    one layer down, where a sparser re-import erased stored profile URLs.
    """
    wd._save_contact_details({"contact_id": "c1", "email": "lanie@apex.test",
                              "linkedin_url": "lanieokafor", "phone": "+1 555 000 1111"})
    wd._save_contact_details({"contact_id": "c1", "notes": "spoke Tuesday"})
    r = row(conn, "email", "linkedin_url", "phone", "notes")
    assert r["email"] == "lanie@apex.test"
    assert r["linkedin_url"] == "https://www.linkedin.com/in/lanieokafor"
    assert r["phone"] == "+1 555 000 1111"
    assert r["notes"] == "spoke Tuesday"


def test_a_field_sent_EMPTY_is_a_clear(conn):
    """The other direction, or "left alone" is indistinguishable from "cannot be removed"."""
    wd._save_contact_details({"contact_id": "c1", "email": "lanie@apex.test"})
    assert wd._save_contact_details({"contact_id": "c1", "email": ""})["ok"]
    r = row(conn, "email", "email_status")
    assert (r["email"] or "") == ""
    assert r["email_status"] == "none"


def test_re_saving_the_SAME_address_does_not_downgrade_a_verified_one(conn):
    """The notes box sits beside these fields, so an unconditional downgrade would quietly
    un-verify an address Apollo had confirmed every time a note was edited."""
    from applypilot.networking import store
    store.upsert_contact({"id": "c1", "job_url": "target:sheet-search:apex",
                          "email": "lanie@apex.test", "email_status": "verified"})
    assert wd._save_contact_details({"contact_id": "c1", "email": "lanie@apex.test"})["ok"]
    assert row(conn, "email_status")["email_status"] == "verified"


def test_only_a_REAL_change_reaches_the_timeline(conn):
    """Re-saving must not spam it, and the wording has to separate adding from replacing — the
    second is the act worth finding again when a send goes somewhere unexpected."""
    # `log_contact_event` resolves the contact's job and appends to that job's activity log —
    # there is no per-contact table. Read it where it actually lands, or this asserts against a
    # table nothing writes and passes on an empty list forever.
    def events():
        return [r["detail"] for r in conn.execute(
            "SELECT detail FROM job_events WHERE job_url='target:sheet-search:apex' "
            "AND stage='outreach' ORDER BY rowid")]

    wd._save_contact_details({"contact_id": "c1", "email": "lanie@apex.test"})
    wd._save_contact_details({"contact_id": "c1", "email": "lanie@apex.test"})
    log = events()
    assert len(log) == 1, f"re-saving the same address logged twice: {log}"
    assert "Added" in log[0] and "lanie@apex.test" in log[0]

    wd._save_contact_details({"contact_id": "c1", "email": "l.okafor@apex.test"})
    log = events()
    assert len(log) == 2
    assert "Changed" in log[1], "replacing an address was recorded as adding one"
    assert "lanie@apex.test" in log[1] and "l.okafor@apex.test" in log[1]


def test_an_unknown_contact_is_refused_rather_than_created(conn):
    assert wd._save_contact_details({"contact_id": "nope", "email": "x@y.test"})["ok"] is False
    assert wd._save_contact_details({"email": "x@y.test"})["ok"] is False


def test_sending_no_editable_field_at_all_is_not_a_silent_success(conn):
    assert wd._save_contact_details({"contact_id": "c1"})["ok"] is False
