"""A parsed sheet becomes company cards and contacts — SHEET-1 C2, the half that writes.

The parsing guarantees live in `test_sheet_import.py`. This file is about what lands in the
database, and in particular the three decisions that are cheap now and expensive later:
`source`, verification, and `email_status`. Each one is a question CRM-2 or the verifier will
ask later and cannot re-derive.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.domain import space as sp
from applypilot.networking import sheet_import, store
from applypilot.repo import spaces as _spaces

TSV = "\t".join
SHEET = "\n".join([
    TSV(["Company", "Name", "Position", "Email", "LinkedIn"]),
    TSV(["Ridgeline Logistics", "Dana Okafor", "VP Engineering", "dana@ridge.test",
         "https://linkedin.test/in/danaokafor"]),
    TSV(["Ridgeline Logistics", "Sam Iyer", "Staff Engineer", "", ""]),
    TSV(["Northwind", "Alex Roy", "Head of Talent", "alex@northwind.test", ""]),
])


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    _spaces.create_space("sheets", "Sheets", "outreach", shape=sp.TARGETS_SHAPE, conn=conn)
    return conn


def _import(conn, text=SHEET, space="sheets"):
    return sheet_import.import_sheet(space, text, conn)


# ── the cards and the people ────────────────────────────────────────────────

def test_it_creates_one_card_per_company_and_one_contact_per_person(db):
    out = _import(db)
    assert out["ok"] is True
    assert sorted(out["cards_added"]) == ["Northwind", "Ridgeline Logistics"]
    assert out["people_added"] == 3

    rows = db.execute("SELECT url, company FROM jobs WHERE space_id='sheets' ORDER BY url").fetchall()
    assert [r["url"] for r in rows] == ["target:sheets:northwind",
                                        "target:sheets:ridgeline-logistics"]
    n = db.execute("SELECT COUNT(*) FROM contacts WHERE job_url LIKE 'target:sheets:%'").fetchone()[0]
    assert n == 3


def test_people_hang_off_their_own_company_card(db):
    _import(db)
    ridge = db.execute(
        "SELECT full_name FROM contacts WHERE job_url='target:sheets:ridgeline-logistics' "
        "ORDER BY full_name").fetchall()
    assert [r["full_name"] for r in ridge] == ["Dana Okafor", "Sam Iyer"]


def test_a_card_has_no_posting_and_does_not_look_like_a_failed_scrape(db):
    """`detail_scraped_at` is stamped at creation. Left NULL the row owes a scrape forever, which
    is how a target ends up rendering as a job that failed to enrich."""
    _import(db)
    r = db.execute("SELECT title, detail_scraped_at, detail_error, application_url "
                   "FROM jobs WHERE url='target:sheets:northwind'").fetchone()
    assert r["detail_scraped_at"]
    assert not r["detail_error"]
    assert r["title"] == "Northwind"


# ── the three decisions ─────────────────────────────────────────────────────

def test_an_imported_contact_is_never_sourced_apollo(db):
    """CRM-2's `by_layer()` compares a warm channel against a cold list. Filing a hand-built
    sheet as a cold find makes that question unanswerable forever."""
    _import(db)
    sources = {r["source"] for r in db.execute(
        "SELECT source FROM contacts WHERE job_url LIKE 'target:sheets:%'")}
    assert sources == {"import"}


def test_verification_is_skipped_not_run(db):
    """`verify_contact` catches people who work somewhere ELSE, which cannot happen to a name the
    operator typed (§Lessons 19). Running it drops everyone for having no Apollo record —
    §Lessons 14, where narrowing before checking meant being right produced nothing."""
    out = _import(db)
    assert out["people_added"] == 3, "verification ate the imported contacts"
    conf = {r["confidence"] for r in db.execute(
        "SELECT confidence FROM contacts WHERE job_url LIKE 'target:sheets:%'")}
    assert conf == {"high"}

    # Parsed, not grepped. The first version searched the source TEXT and matched this module's
    # own docstring, which names `verify_contact` to explain why it is absent — §Lessons 25, and
    # the second time that exact heuristic misfired in this session.
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(sheet_import))
    called = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    called |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "verify_contact" not in called, "the import path calls the verifier"


def test_email_status_records_that_a_spreadsheet_is_not_evidence(db):
    """`verified` is a claim about the ADDRESS. Typed into a sheet it is unconfirmed; absent it
    is `none` — the same rule that separates a Cc off a live thread from one typed from memory."""
    _import(db)
    got = {r["full_name"]: r["email_status"] for r in db.execute(
        "SELECT full_name, email_status FROM contacts WHERE job_url LIKE 'target:sheets:%'")}
    assert got == {"Dana Okafor": "unverified", "Sam Iyer": "none", "Alex Roy": "unverified"}


# ── idempotence ─────────────────────────────────────────────────────────────

def test_pasting_the_same_sheet_twice_changes_nothing(db):
    """The natural way to use this is to paste a GROWING sheet again, so re-import has to update
    rather than duplicate. Tested by running it twice, not by reading the code (§Lessons 22)."""
    _import(db)
    second = _import(db)
    assert second["people_added"] == 0
    assert second["people_updated"] == 3
    assert second["cards_added"] == []
    assert sorted(second["cards_existing"]) == ["Northwind", "Ridgeline Logistics"]
    n = db.execute("SELECT COUNT(*) FROM contacts WHERE job_url LIKE 'target:sheets:%'").fetchone()[0]
    assert n == 3
    cards = db.execute("SELECT COUNT(*) FROM jobs WHERE space_id='sheets'").fetchone()[0]
    assert cards == 2


def test_a_growing_sheet_adds_only_the_new_rows(db):
    _import(db)
    grown = SHEET + "\n" + TSV(["Northwind", "Priya Raman", "Recruiter", "priya@northwind.test", ""])
    out = _import(db, grown)
    assert out["people_added"] == 1 and out["people_updated"] == 3


def test_a_re_import_WITHOUT_the_linkedin_column_does_not_duplicate_anyone(db):
    """The bug the first version shipped, found by running it against the live endpoint.

    `contact_id` is (job_url, linkedin_url, name). Export the same sheet again without the
    LinkedIn column — which is what happens the moment the operator rearranges it — and every
    person hashes differently and lands as a SECOND contact, with their own ladder, their own
    draft and a second email to one inbox.

    The original idempotence test could not see it: it re-pasted a BYTE-IDENTICAL sheet, so the
    hash inputs were identical too. Re-importing a CHANGED sheet is the normal way to use this
    (§Lessons 22 — idempotence has to be tested by running it, and by running it on the input
    that actually differs).
    """
    _import(db)
    before = db.execute("SELECT COUNT(*) FROM contacts WHERE job_url LIKE 'target:sheets:%'"
                        ).fetchone()[0]

    no_li = "\n".join([
        TSV(["Company", "Name", "Position", "Email"]),
        TSV(["Ridgeline Logistics", "Dana Okafor", "VP Engineering", "dana@ridge.test"]),
        TSV(["Ridgeline Logistics", "Sam Iyer", "Staff Engineer", ""]),
        TSV(["Northwind", "Alex Roy", "Head of Talent", "alex@northwind.test"]),
    ])
    out = _import(db, no_li)
    assert out["people_added"] == 0, "the same people were re-added under new ids"
    assert out["people_updated"] == 3
    after = db.execute("SELECT COUNT(*) FROM contacts WHERE job_url LIKE 'target:sheets:%'"
                       ).fetchone()[0]
    assert after == before == 3
    names = [r["full_name"] for r in db.execute(
        "SELECT full_name FROM contacts WHERE job_url='target:sheets:ridgeline-logistics'")]
    assert sorted(names) == ["Dana Okafor", "Sam Iyer"]


def test_a_person_is_matched_by_email_even_if_their_name_changed(db):
    """A sheet gets corrected: "Dana Okafor" becomes "Dana Okafor-Reid". Same address, same
    person, same card — not a new contact and a second ladder."""
    _import(db)
    renamed = "\n".join([
        TSV(["Company", "Name", "Email"]),
        TSV(["Ridgeline Logistics", "Dana Okafor-Reid", "dana@ridge.test"]),
    ])
    out = _import(db, renamed)
    assert out["people_added"] == 0 and out["people_updated"] == 1
    assert db.execute(
        "SELECT full_name FROM contacts WHERE email='dana@ridge.test'").fetchone()["full_name"] \
        == "Dana Okafor-Reid"


def test_a_person_with_no_email_is_matched_by_name(db):
    """Name is the only handle when there is no address — and this has to be set up so that it
    is the ONLY thing that can match.

    The first version imported Sam with no email AND no LinkedIn, then re-imported him the same
    way, so `contact_id` hashed identically and the row was found without the name path running
    at all. Deleting name matching left it green (§Lessons 13, caught by mutation). Here he
    arrives WITH a LinkedIn URL and returns without one, so the hash cannot match and nothing but
    the name can save him.
    """
    first = "\n".join([
        TSV(["Company", "Name", "Position", "LinkedIn"]),
        TSV(["Ridgeline Logistics", "Sam Iyer", "Staff Engineer", "/in/samiyer"]),
    ])
    _import(db, first)
    again = "\n".join([
        TSV(["Company", "Name", "Position"]),
        TSV(["Ridgeline Logistics", "Sam Iyer", "Principal Engineer"]),
    ])
    out = _import(db, again)
    assert out["people_added"] == 0, "he was re-added under a new id — the hash changed"
    rows = db.execute("SELECT title, linkedin_url FROM contacts "
                      "WHERE full_name='Sam Iyer'").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "Principal Engineer"
    # The LinkedIn URL from the first sheet SURVIVES: `upsert_contact` writes only non-None
    # fields, so a later sheet that carries fewer columns must not erase what an earlier one
    # supplied.
    assert rows[0]["linkedin_url"] == "https://www.linkedin.com/in/samiyer"


def test_two_different_people_at_one_company_stay_two(db):
    """Guard the guard: matching everyone to the first contact on the card also makes
    `people_added == 0`."""
    _import(db)
    out = _import(db, "\n".join([
        TSV(["Company", "Name", "Email"]),
        TSV(["Ridgeline Logistics", "Rae Nolan", "rae@ridge.test"]),
    ]))
    assert out["people_added"] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM contacts WHERE job_url='target:sheets:ridgeline-logistics'"
    ).fetchone()[0] == 3


def test_re_import_does_not_wipe_a_sent_draft(db):
    """`upsert_contact` updates only non-None fields, and this leans on it: a re-paste must not
    clear outreach state the operator has already spent."""
    _import(db)
    cid = db.execute("SELECT id FROM contacts WHERE full_name='Dana Okafor'").fetchone()["id"]
    db.execute("UPDATE contacts SET outreach_status='sent', sent_message_id='m1' WHERE id=?", (cid,))
    db.commit()
    _import(db)
    r = db.execute("SELECT outreach_status, sent_message_id FROM contacts WHERE id=?", (cid,)).fetchone()
    assert r["outreach_status"] == "sent" and r["sent_message_id"] == "m1"


# ── reporting ───────────────────────────────────────────────────────────────

def test_the_message_names_every_non_zero_outcome(db):
    out = _import(db)
    m = out["message"]
    assert "2 new companies" in m and "3 people" in m


def test_skipped_rows_are_named_with_their_line_numbers(db):
    out = _import(db, "\n".join([
        TSV(["Company", "Name"]),
        TSV(["Ridgeline", "Dana Okafor"]),
        TSV(["", "Nobody"]),
    ]))
    assert "1 row(s) skipped" in out["message"] and "line 3" in out["message"]


def test_an_oversized_paste_says_what_it_did_not_read(db):
    """A cap that is not stated reads as "that is all there was"."""
    from applypilot.domain import sheet as _sheet
    rows = [TSV(["Company", "Name"])] + [TSV([f"Co{i}", f"P{i}"])
                                         for i in range(_sheet.MAX_ROWS + 3)]
    out = _import(db, "\n".join(rows))
    assert "3 row(s) past" in out["message"] and "NOT read" in out["message"]


# ── the endpoint ────────────────────────────────────────────────────────────

def test_a_jobs_space_refuses_the_sheet(db):
    """The paste that prompted this ticket went into a JOBS box and became
    `title="Docs uploaded job", company="Docs"` — the host label as the employer for the eighth
    time. A jobs Space must refuse rather than parse a link out of it."""
    _spaces.create_space("postings", "Postings", "jobs", conn=db)
    out = wd._import_sheet({"space": "postings", "text": SHEET})
    assert out["ok"] is False
    assert "job postings" in out["message"]
    assert db.execute("SELECT COUNT(*) FROM jobs WHERE space_id='postings'").fetchone()[0] == 0


def test_a_header_problem_is_reported_as_a_header_problem(db):
    """Not as "3 rows failed". Merged, the operator goes looking in the wrong place."""
    out = wd._import_sheet({"space": "sheets", "text": "Name\tPosition\nDana\tVP"})
    assert out["ok"] is False
    assert "company column" in out["message"].lower()
    assert "Name" in out["message"]


def test_the_endpoint_writes_into_the_space_on_screen(db):
    """§Lessons 70: `/api/import` carried no Space for days and the column DEFAULT filed every
    paste under `job-search`, so Gauntlet held zero jobs from the day it was created. This is
    that path's third sibling."""
    _spaces.create_space("other", "Other", "outreach", shape=sp.TARGETS_SHAPE, conn=db)
    wd._import_sheet({"space": "other", "text": SHEET})
    assert db.execute("SELECT COUNT(*) FROM jobs WHERE space_id='other'").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM jobs WHERE space_id='sheets'").fetchone()[0] == 0


# ── a URL is never a company ────────────────────────────────────────────────

def test_the_targets_box_refuses_a_document_link(db):
    """What the operator actually did: pasted a Google Sheets URL into "＋ Add targets" and got
    the card "spreadsheets/d/1HreblDeVn3vDFlmy4fROR9OThwd5tsld2PQkmtvG8qQ/edit?gid=…".

    `parse_line` stripped `docs.google.com` as a domain and the leftover PATH became the company
    name. Same family as "Ats", "Hr", "Edu" and — from this same link a day earlier — "Docs":
    a URL turning into an employer (§Lessons 20, 52, 79)."""
    from applypilot.domain import target
    url = ("https://docs.google.com/spreadsheets/d/"
           "1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/edit?gid=373473585#gid=373473585")
    assert target.parse_line(url) is None

    parsed, rejected = target.parse_input(url)
    assert parsed == [] and rejected == [url]


def test_a_bare_domain_is_still_a_company(db):
    """The rule is about a PATH, not about URLs. "ridgeline.com" is a company stated as a
    domain and must keep working — refusing it would be a worse bug than the one being fixed."""
    from applypilot.domain import target
    assert target.parse_line("ridgeline.com") == {"name": "Ridgeline", "domain": "ridgeline.com"}
    assert target.parse_line("https://www.ridgeline.com") == {"name": "Ridgeline",
                                                              "domain": "ridgeline.com"}


def test_a_named_company_with_a_deep_link_still_works(db):
    """The name was given explicitly, so the path is just a note."""
    from applypilot.domain import target
    got = target.parse_line("Ridgeline Logistics — https://www.ridgeline.com/about/team")
    assert got["name"] == "Ridgeline Logistics"
    assert got["domain"] == "ridgeline.com"


# ── a spreadsheet row is not a company name ─────────────────────────────────

def test_a_tab_separated_row_is_never_a_company(db):
    """106 cards were created by pasting a lead sheet into "Add one company". Each was named
    after a whole row — including the HEADER row, which became the card
    `Name\\tCompany\\tEmail\\tEmail Status\\tTitle\\t…`.

    A company name never contains a tab. Refused rather than salvaged by taking the first cell:
    guessing which column holds the company is what the sheet importer does properly with
    headers, and doing it badly here would produce cards that look right and are wrong for any
    sheet whose first column is not the company — this operator's is the NAME.
    """
    from applypilot.domain import target
    row = "Tracy Stdic\tZapier\ttracy@zapier.test\tverified\tGlobal Head of Talent"
    assert target.parse_line(row) is None
    header = "Name\tCompany\tEmail\tEmail Status\tTitle\tLinkedIn"
    assert target.parse_line(header) is None


def test_the_refusal_points_at_the_importer(db):
    """"106 not understood" is accurate and useless — the box that reads exactly this is two
    inches below (§Lessons 15, and §Lessons 89: findable is not findable FROM WHERE THE WORK IS).
    """
    _spaces.create_space("t2", "T2", "outreach", shape=sp.TARGETS_SHAPE, conn=db)
    rows = "\n".join([
        "Name\tCompany\tEmail",
        "Tracy Stdic\tZapier\ttracy@zapier.test",
        "Frank Tiemann\tApex Fintech Services\t",
    ])
    out = wd._add_targets({"space": "t2", "text": rows})
    assert out["ok"] is False
    assert "spreadsheet row" in out["message"]
    assert "Import a sheet" in out["message"]
    assert db.execute("SELECT COUNT(*) FROM jobs WHERE space_id='t2'").fetchone()[0] == 0


def test_the_same_rows_import_correctly_through_the_sheet_box(db):
    """The other half of the pair: what was refused above has to work where it belongs, or the
    message is sending the operator somewhere that fails too."""
    rows = "\n".join([
        "Name\tCompany\tEmail\tTitle",
        "Tracy Stdic\tZapier\ttracy@zapier.test\tGlobal Head of Talent",
        "Frank Tiemann\tApex Fintech Services\t\t",
    ])
    out = wd._import_sheet({"space": "sheets", "text": rows})
    assert out["ok"] is True
    assert sorted(out["cards_added"]) == ["Apex Fintech Services", "Zapier"]
    assert out["people_added"] == 2
    got = db.execute("SELECT full_name, title, company FROM contacts "
                     "WHERE job_url='target:sheets:zapier'").fetchone()
    assert got["full_name"] == "Tracy Stdic"
    assert got["title"] == "Global Head of Talent"


# ── SHEET-2: context per company ────────────────────────────────────────────

ABOUT = "\n".join([
    TSV(["Company", "Name", "Email", "About"]),
    TSV(["Ridgeline Logistics", "Dana Okafor", "dana@ridge.test",
         "Freight brokerage, 300 people, Austin HQ. Moving off spreadsheets."]),
    TSV(["Ridgeline Logistics", "Sam Iyer", "", ""]),
    TSV(["Northwind", "Alex Roy", "", "Analytics for hospital procurement."]),
])


def test_an_about_column_lands_on_the_CARD_not_the_person(db):
    """It feeds "WHAT THIS COMPANY DOES" in every draft for everyone there. Filed on a contact it
    would reach one person and be invisible to their colleagues."""
    out = sheet_import.import_sheet("sheets", ABOUT, db)
    assert out["ok"] is True
    r = db.execute("SELECT full_description FROM jobs "
                   "WHERE url='target:sheets:ridgeline-logistics'").fetchone()
    assert "Freight brokerage" in r["full_description"]
    # and NOT on the people
    notes = {x["notes"] for x in db.execute(
        "SELECT notes FROM contacts WHERE job_url='target:sheets:ridgeline-logistics'")}
    assert not any((n or "").strip() for n in notes)


def test_one_filled_cell_covers_the_whole_company(db):
    """The operator should not have to repeat the blurb down every row of a company. First
    non-empty wins, so filling it once is enough — and Sam's blank row must not erase it."""
    sheet_import.import_sheet("sheets", ABOUT, db)
    r = db.execute("SELECT full_description FROM jobs "
                   "WHERE url='target:sheets:ridgeline-logistics'").fetchone()
    assert "Freight brokerage" in r["full_description"]


def test_a_re_import_without_the_about_column_does_not_erase_it(db):
    """Same rule as the contact fields, and the same bug that erased a LinkedIn URL before it
    was caught: an empty cell means "this sheet does not say", never "clear it"."""
    sheet_import.import_sheet("sheets", ABOUT, db)
    sheet_import.import_sheet("sheets", SHEET, db)          # no About column at all
    r = db.execute("SELECT full_description FROM jobs "
                   "WHERE url='target:sheets:ridgeline-logistics'").fetchone()
    assert "Freight brokerage" in r["full_description"]


def test_a_later_sheet_can_UPDATE_the_blurb(db):
    """Not erasing is not the same as never changing. A corrected sheet has to win."""
    sheet_import.import_sheet("sheets", ABOUT, db)
    revised = "\n".join([
        TSV(["Company", "Name", "About"]),
        TSV(["Ridgeline Logistics", "Dana Okafor", "Freight brokerage. Now 500 people."]),
    ])
    sheet_import.import_sheet("sheets", revised, db)
    r = db.execute("SELECT full_description FROM jobs "
                   "WHERE url='target:sheets:ridgeline-logistics'").fetchone()
    assert "500 people" in r["full_description"]


def test_setting_a_blurb_does_not_claim_a_page_was_scraped(db):
    """`attach_posting` stamps the scrape columns because a posting really arrived. A hand-typed
    sentence must stay distinguishable from a fetched description."""
    from applypilot.repo import jobs as _jobs
    sheet_import.import_sheet("sheets", SHEET, db)
    before = db.execute("SELECT detail_scraped_at FROM jobs "
                        "WHERE url='target:sheets:northwind'").fetchone()["detail_scraped_at"]
    _jobs.set_about("target:sheets:northwind", "Analytics for hospital procurement.", db)
    after = db.execute("SELECT detail_scraped_at, full_description FROM jobs "
                       "WHERE url='target:sheets:northwind'").fetchone()
    assert after["detail_scraped_at"] == before
    assert "hospital procurement" in after["full_description"]


def test_an_empty_blurb_is_a_no_op(db):
    from applypilot.repo import jobs as _jobs
    sheet_import.import_sheet("sheets", ABOUT, db)
    assert _jobs.set_about("target:sheets:ridgeline-logistics", "   ", db) is False
    r = db.execute("SELECT full_description FROM jobs "
                   "WHERE url='target:sheets:ridgeline-logistics'").fetchone()
    assert "Freight brokerage" in r["full_description"]
