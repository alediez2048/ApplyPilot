"""A pasted spreadsheet becomes companies and the people at them.

SHEET-1 C1. The parser is where the judgment is, so it is pure and this file drives it directly.

The paste that prompted the ticket is the shape to keep in mind: a Google Sheets URL went into
Gauntlet's JOBS import box, `_URL_RE` took the link as a posting, and the row stored
`title="Docs uploaded job", company="Docs"` — the host label as the employer for the eighth time
(Ats · Hr · Edu · Ouryahoo · Oraclecloud · Recruitics · Jobvite · Docs). Here the operator STATES
the company in a column, so none of that machinery runs at all.
"""

from __future__ import annotations

import pytest

from applypilot.domain import sheet

TSV = "\t".join
SHEET = "\n".join([
    TSV(["Company", "Name", "Position", "Email", "LinkedIn"]),
    TSV(["Ridgeline Logistics", "Dana Okafor", "VP Engineering", "dana@ridge.com",
         "https://linkedin.com/in/danaokafor"]),
    TSV(["Ridgeline Logistics", "Sam Iyer", "Staff Engineer", "", ""]),
    TSV(["Northwind", "Alex Roy", "Head of Talent", "alex@northwind.io", "/in/alexroy"]),
])


# ── the core shape ──────────────────────────────────────────────────────────

def test_people_are_grouped_into_company_cards():
    """The grouping IS the bundling that was asked for: three rows, two cards."""
    out = sheet.parse(SHEET)
    assert [c["slug"] for c in out["companies"]] == ["ridgeline-logistics", "northwind"]
    assert [c["people"] for c in out["companies"]] == [2, 1]
    assert len(out["people"]) == 3
    assert out["rejected"] == []


def test_each_person_carries_the_slug_so_the_caller_never_regroups():
    """Two computations of "which card is this person on" is two answers that can disagree."""
    out = sheet.parse(SHEET)
    assert {p["full_name"]: p["company_slug"] for p in out["people"]} == {
        "Dana Okafor": "ridgeline-logistics",
        "Sam Iyer": "ridgeline-logistics",
        "Alex Roy": "northwind"}


def test_the_fields_survive():
    out = sheet.parse(SHEET)
    dana = out["people"][0]
    assert dana["title"] == "VP Engineering"
    assert dana["email"] == "dana@ridge.com"
    assert dana["linkedin_url"] == "https://linkedin.com/in/danaokafor"


# ── headers, not positions ──────────────────────────────────────────────────

def test_columns_are_found_by_header_not_by_position():
    """Every tool exports columns in its own order. A positional parser files job titles as
    names, which is invisible until an email opens "Hi VP Engineering"."""
    shuffled = "\n".join([
        TSV(["Position", "Email", "Company", "Name"]),
        TSV(["VP Engineering", "dana@ridge.com", "Ridgeline", "Dana Okafor"]),
    ])
    p = sheet.parse(shuffled)["people"][0]
    assert p["full_name"] == "Dana Okafor" and p["title"] == "VP Engineering"
    assert p["company_slug"] == "ridgeline"


@pytest.mark.parametrize("header", ["Company", "COMPANY", "Company Name", "Organization",
                                    "Employer", "Account Name"])
def test_company_column_spellings(header):
    out = sheet.parse(f"{header}\tName\nRidgeline\tDana Okafor")
    assert out["companies"][0]["slug"] == "ridgeline"


@pytest.mark.parametrize("header", ["Position", "Title", "Role", "Job Title"])
def test_title_column_spellings(header):
    out = sheet.parse(f"Company\tName\t{header}\nRidgeline\tDana\tVP Engineering")
    assert out["people"][0]["title"] == "VP Engineering"


def test_first_and_last_name_columns_are_joined():
    """Every CRM export splits them. Rejecting those rows loses the whole sheet."""
    out = sheet.parse("Company\tFirst Name\tLast Name\nRidgeline\tDana\tOkafor")
    assert out["people"][0]["full_name"] == "Dana Okafor"


def test_a_full_name_column_wins_over_a_split_pair():
    out = sheet.parse("Company\tFull Name\tFirst Name\nRidgeline\tDana Okafor\tWRONG")
    assert out["people"][0]["full_name"] == "Dana Okafor"


def test_unknown_columns_are_ignored_not_fatal():
    """A real export carries a dozen columns nobody here cares about."""
    out = sheet.parse("Company\tName\tLead Score\tSFDC ID\nRidgeline\tDana\t92\tX1")
    assert out["people"][0]["full_name"] == "Dana"


# ── refusals name what they saw ─────────────────────────────────────────────

def test_a_missing_company_column_refuses_and_names_the_headers_it_found():
    """Company is the one field with no fallback. The message must not leave the operator
    guessing which of their columns went unrecognised (§Lessons 15)."""
    with pytest.raises(sheet.SheetError) as e:
        sheet.parse("Name\tPosition\nDana Okafor\tVP")
    msg = str(e.value)
    assert "company" in msg.lower()
    assert "Name" in msg and "Position" in msg


def test_a_missing_name_column_refuses_too():
    with pytest.raises(sheet.SheetError) as e:
        sheet.parse("Company\tPosition\nRidgeline\tVP")
    assert "name" in str(e.value).lower()


def test_an_empty_paste_refuses():
    for blank in ("", "   ", "\n\n"):
        with pytest.raises(sheet.SheetError):
            sheet.parse(blank)


# ── rejects are returned, per row, with line numbers ────────────────────────

def test_rejected_rows_are_returned_with_their_line_numbers():
    """"Imported 38 of 41" naming the three is a result the operator can act on."""
    out = sheet.parse("\n".join([
        TSV(["Company", "Name"]),
        TSV(["Ridgeline", "Dana Okafor"]),
        TSV(["", "Nobody Ltd"]),          # line 3 — no company
        TSV(["Northwind", ""]),           # line 4 — no name
        TSV(["Northwind", "Alex Roy"]),
    ]))
    assert len(out["people"]) == 2
    assert [r["line"] for r in out["rejected"]] == [3, 4]
    assert {r["reason"] for r in out["rejected"]} == {"no company", "no name"}


def test_a_bad_email_keeps_the_person_and_drops_the_address():
    """A malformed cell is not a reason to lose the name and the title too."""
    out = sheet.parse("Company\tName\tEmail\nRidgeline\tDana Okafor\tnot-an-email")
    assert len(out["people"]) == 1
    assert out["people"][0]["email"] == ""
    assert "unusable email" in out["rejected"][0]["reason"]
    assert "person kept" in out["rejected"][0]["reason"]


# ── dedupe ──────────────────────────────────────────────────────────────────

def test_the_same_person_twice_is_one_contact():
    """A sheet accumulated by hand repeats people, and two rows for one person is two ladders
    aimed at one inbox."""
    out = sheet.parse("\n".join([
        TSV(["Company", "Name", "Email"]),
        TSV(["Ridgeline", "Dana Okafor", "dana@ridge.com"]),
        TSV(["Ridgeline", "Dana Okafor", "dana@ridge.com"]),
    ]))
    assert len(out["people"]) == 1
    assert out["rejected"][0]["reason"] == "duplicate of an earlier row"


def test_one_company_spelled_three_ways_is_one_card():
    """`target.slug` strips corporate suffixes from the END only — "Inc" is noise in
    "Ridgeline Inc" and load-bearing in "Inc Magazine"."""
    out = sheet.parse("\n".join([
        TSV(["Company", "Name"]),
        TSV(["Ridgeline", "A One"]),
        TSV(["Ridgeline Inc.", "B Two"]),
        TSV(["ridgeline,", "C Three"]),
    ]))
    assert len(out["companies"]) == 1
    assert out["companies"][0]["people"] == 3


def test_the_same_name_at_two_companies_is_two_people():
    """Dedupe is per COMPANY. Someone who appears under two employers is two contacts on two
    cards, and a global dedupe would silently drop the second."""
    out = sheet.parse("\n".join([
        TSV(["Company", "Name"]),
        TSV(["Ridgeline", "Dana Okafor"]),
        TSV(["Northwind", "Dana Okafor"]),
    ]))
    assert len(out["people"]) == 2
    assert out["rejected"] == []


# ── delimiters and shapes real exports produce ──────────────────────────────

def test_csv_works_as_well_as_tsv():
    out = sheet.parse("Company,Name,Position\nRidgeline,Dana Okafor,VP Engineering")
    assert out["people"][0]["title"] == "VP Engineering"


def test_a_comma_inside_a_company_name_does_not_split_a_tsv():
    """"Ridgeline Logistics, Inc." is a normal cell. Deciding the delimiter from the whole
    document rather than the header line splits it."""
    out = sheet.parse(TSV(["Company", "Name"]) + "\nRidgeline Logistics, Inc.\tDana")
    assert out["companies"][0]["name"] == "Ridgeline Logistics, Inc."
    assert out["companies"][0]["slug"] == "ridgeline-logistics"


def test_a_quoted_csv_field_containing_a_comma():
    out = sheet.parse('Company,Name\n"Ridgeline Logistics, Inc.",Dana Okafor')
    assert out["companies"][0]["slug"] == "ridgeline-logistics"
    assert out["people"][0]["full_name"] == "Dana Okafor"


def test_blank_lines_are_skipped_not_rejected():
    out = sheet.parse("Company\tName\n\nRidgeline\tDana\n\n\nNorthwind\tAlex\n")
    assert len(out["people"]) == 2 and out["rejected"] == []


def test_a_bare_linkedin_path_becomes_a_url():
    out = sheet.parse("Company\tName\tLinkedIn\nRidgeline\tDana\t/in/danaokafor")
    assert out["people"][0]["linkedin_url"] == "https://www.linkedin.com/in/danaokafor"


def test_a_domain_column_is_carried_to_the_company():
    out = sheet.parse("\n".join([
        TSV(["Company", "Name", "Website"]),
        TSV(["Ridgeline", "Dana", "https://www.ridge.com/about"]),
        TSV(["Ridgeline", "Sam", ""]),
    ]))
    assert out["companies"][0]["domain"] == "ridge.com"


def test_a_later_blank_domain_does_not_erase_an_earlier_one():
    """First non-empty wins. Order-dependence here would make the same sheet import differently
    depending on how it happened to be sorted."""
    out = sheet.parse("\n".join([
        TSV(["Company", "Name", "Domain"]),
        TSV(["Ridgeline", "Sam", ""]),
        TSV(["Ridgeline", "Dana", "ridge.com"]),
    ]))
    assert out["companies"][0]["domain"] == "ridge.com"


# ── no silent caps ──────────────────────────────────────────────────────────

def test_an_oversized_paste_reports_what_it_dropped():
    """A cap that is not stated reads as "that is all there was"."""
    rows = [TSV(["Company", "Name"])] + [TSV([f"Co{i}", f"P{i}"]) for i in range(sheet.MAX_ROWS + 25)]
    out = sheet.parse("\n".join(rows))
    assert len(out["people"]) == sheet.MAX_ROWS
    assert out["dropped"] == 25


def test_a_paste_within_the_cap_reports_nothing_dropped():
    assert sheet.parse(SHEET)["dropped"] == 0


# ── the boundary ────────────────────────────────────────────────────────────

def test_the_parser_is_pure():
    """`domain/` may not import sqlite3, http or the web layer (ARCH-1). This one is also the
    reason the employer machinery never runs: the company is STATED, so `derive` is not here.

    Parsed as an AST, not grepped. The first version searched the SOURCE TEXT and failed on the
    module docstring, which names `networking/derive.py` to explain why it is absent — §Lessons
    25, where a heuristic matching the wrong thing pushed correct code onto an allowlist for a
    rule it never broke.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(sheet))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            if node.module.startswith("applypilot."):
                imported.add(node.module)
    for banned in ("sqlite3", "httpx", "requests"):
        assert banned not in imported, f"domain/sheet.py imports {banned}"
    assert not any(m.startswith("applypilot.networking") or m.startswith("applypilot.web")
                   for m in imported), f"domain/sheet.py reaches outside domain/: {imported}"


# ── a link is not the data ──────────────────────────────────────────────────

SHEET_URL = ("https://docs.google.com/spreadsheets/d/"
             "1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/edit?gid=373473585#gid=373473585")


def test_pasting_the_LINK_says_so_instead_of_blaming_the_headers():
    """What the operator actually did, twice. Reading a sheet from its URL needs Google
    credentials; copying the CELLS needs none, which is the whole reason this feature is a paste.

    Before this the refusal read "No company column found. Columns seen: https://docs.google…"
    — true, and it sends someone off to add a Company column to a URL (§Lessons 15: a refusal
    has to name the way out)."""
    with pytest.raises(sheet.SheetError) as e:
        sheet.parse(SHEET_URL)
    msg = str(e.value)
    assert "link to the sheet, not the sheet" in msg
    assert "select the rows" in msg
    assert "column" not in msg.lower(), "still blaming the headers for a pasted URL"


@pytest.mark.parametrize("url", [
    "https://docs.google.com/spreadsheets/d/abc/edit",
    "https://acme.sharepoint.com/:x:/r/sites/x/Doc.xlsx",
    "https://www.notion.so/Some-Table-abc123",
    "https://airtable.com/appXXXX/tblYYYY",
])
def test_any_document_link_gets_the_same_answer(url):
    """No host list. Excel Online, Notion and Airtable are the same mistake, and a blocklist only
    ever covers the vendor somebody was already burned by (§Lessons 79)."""
    with pytest.raises(sheet.SheetError) as e:
        sheet.parse(url)
    assert "not the sheet" in str(e.value)


def test_a_url_INSIDE_a_real_paste_is_not_mistaken_for_a_link():
    """Guard the guard: a Website column full of URLs must still import."""
    out = sheet.parse("\n".join([
        TSV(["Company", "Name", "Website"]),
        TSV(["Ridgeline", "Dana", "https://ridgeline.test/about"]),
    ]))
    assert out["people"][0]["full_name"] == "Dana"
    assert out["companies"][0]["domain"] == "ridgeline.test"
