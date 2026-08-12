"""A pasted spreadsheet -> companies and the people at them. Pure — no SQL, no HTTP.

SHEET-1. The unit here is the COMPANY and the row is a PERSON, which is the inversion that makes
this a different parser from `domain/target.py`:

    target.parse_input   one COMPANY per line, typed by hand
    sheet.parse          one PERSON per row, with their company repeated on every one of theirs

Grouping those people under their company IS the bundling the operator asked for. The company
name is STATED rather than recovered from a URL, so none of `networking/derive.py` runs — the
2,000 lines of rules that produced the employers "Ouryahoo", "Edu", "Ats", "Oraclecloud",
"Jobvite" and (the paste that prompted this ticket) "Docs".

**Google Sheets puts TSV on the clipboard.** That is the whole integration: no OAuth, no API key,
no token on disk, nothing to revoke. Worth stating because the obvious build is a Sheets
integration and it buys nothing — the URL cannot be read without credentials, which is exactly
what pasting one into the jobs box already proved.

Two properties drive the design, and both come from what real exports look like rather than from
what would be convenient:

**Columns are found by HEADER, never by position.** Every tool exports them in its own order, and
a positional parser silently files job titles as names — a mistake that is invisible until an
email opens "Hi VP Engineering".

**A first/last name pair is normal.** Every CRM export splits them, so they are JOINED rather
than rejected.
"""

from __future__ import annotations

import csv
import io
import re

from applypilot.domain.target import slug

#: How many data rows one paste may carry. A sheet is not a paste past this point — it wants a
#: file upload, a progress bar and a resumable write, which is a different feature. The caller
#: REPORTS the overflow rather than silently keeping the first N (§Lessons: no silent caps).
MAX_ROWS = 500

#: Header spellings, normalised to letters only. `_norm("Full Name") == "fullname"`.
#: Ordered longest-first within each field so "firstname" cannot be eaten by "name".
_FIELDS: dict[str, tuple[str, ...]] = {
    "company": ("company", "companyname", "organization", "organisation", "org", "employer",
                "account", "accountname"),
    "first": ("firstname", "first", "givenname", "forename"),
    "last": ("lastname", "last", "surname", "familyname"),
    "name": ("fullname", "name", "contact", "contactname", "person"),
    "title": ("title", "position", "role", "jobtitle", "headline"),
    "email": ("email", "emailaddress", "workemail", "mail"),
    "linkedin": ("linkedin", "linkedinurl", "linkedinprofile", "li", "profile", "profileurl"),
    "domain": ("domain", "website", "url", "companywebsite", "site", "companydomain"),
    "notes": ("notes", "note", "comment", "comments", "context"),
    # SHEET-2. What the COMPANY does, one cell, repeated on that company's rows or filled on
    # just one of them. Deliberately separate from `notes`, which is per PERSON: a blurb about
    # the employer belongs on the card and reaches every draft for it, and filing it on a
    # contact would send it to one person and hide it from their colleagues.
    "about": ("about", "description", "companydescription", "whattheydo", "summary",
              "companysummary", "overview", "bio", "blurb"),
}

_NORM = re.compile(r"[^a-z0-9]+")

#: A single pasted URL. Deliberately not limited to docs.google.com — Excel Online, Dropbox
#: Paper, Notion and Airtable links are the same mistake, and a list of hosts only ever protects
#: against the one somebody has already been burned by (§Lessons 79).
_LOOKS_LIKE_A_LINK = re.compile(r"^\s*https?://\S+\s*$", re.I)


def _norm(s: str | None) -> str:
    return _NORM.sub("", (s or "").strip().lower())


def _sniff(text: str) -> str:
    """Tab or comma. Sheets and Excel both put TAB on the clipboard; a saved file is CSV.

    Decided on the HEADER LINE only, and by count rather than by `csv.Sniffer`, which throws on
    short or irregular input and would make a two-column paste unreadable. A company name with a
    comma in it ("Ridgeline Logistics, Inc.") is common enough that guessing comma from the whole
    document would split it.
    """
    head = (text or "").lstrip().splitlines()[0] if (text or "").strip() else ""
    return "\t" if head.count("\t") >= head.count(",") and "\t" in head else ","


def header_map(row: list[str]) -> dict[str, int]:
    """Column name -> index, for the fields we understand. Unknown columns are ignored.

    First match wins per FIELD, so a sheet carrying both "Name" and "Full Name" does not end up
    with the second overwriting the first for no stated reason.
    """
    out: dict[str, int] = {}
    for i, cell in enumerate(row):
        n = _norm(cell)
        if not n:
            continue
        for field, spellings in _FIELDS.items():
            if field not in out and n in spellings:
                out[field] = i
                break
    return out


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


_URL_JUNK = re.compile(r"^https?://(www\.)?", re.I)


def _linkedin(value: str) -> str:
    """Accept a full URL, a bare path, or a handle. Stored as given otherwise.

    Not validated beyond looking like a profile: an operator's sheet may carry a company page or
    a search link, and refusing the row over it loses the person's name and title too.
    """
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("/in/") or v.startswith("in/"):
        return "https://www.linkedin.com/" + v.lstrip("/")
    return v


def _person_name(row: list[str], hm: dict[str, int]) -> str:
    """`name`, or first + last joined. Every CRM export splits them."""
    whole = _cell(row, hm.get("name"))
    if whole:
        return whole
    parts = [_cell(row, hm.get("first")), _cell(row, hm.get("last"))]
    return " ".join(p for p in parts if p).strip()


def _domain(value: str) -> str:
    v = _URL_JUNK.sub("", (value or "").strip().lower()).split("/")[0].strip()
    return v if "." in v else ""


class SheetError(ValueError):
    """The paste cannot be read at all — distinct from rows that individually failed."""


def parse(text: str) -> dict:
    """A pasted sheet -> {companies, people, rejected, dropped, headers}.

    `companies` is one entry per distinct company SLUG, in first-seen order, each carrying the
    name as first written and a domain if any row supplied one.

    `people` carry `company_slug`, so the caller never re-derives the grouping and cannot
    disagree with it.

    `rejected` is a list of {line, reason, text} — per ROW, with the spreadsheet's own line
    number so the operator can go and look at it. "Imported 38 of 41" naming the three is a
    result they can act on; "imported 38" is one they cannot (§Lessons 15).

    Raises `SheetError` when the paste has no company column, because that is the one field with
    no fallback — everything else can be blank. The message NAMES the headers that were found,
    or the operator is left guessing which of their columns we failed to recognise.
    """
    raw = (text or "").strip("\n")
    if not raw.strip():
        raise SheetError("Nothing pasted.")

    # A LINK IS NOT THE DATA, and this is the error the operator actually hits. Reading a
    # spreadsheet from its URL needs Google credentials; the whole point of this feature is that
    # it needs none, because copying the CELLS puts them on the clipboard already.
    #
    # Checked before the header logic so the message says the useful thing. Without it the
    # refusal was "No company column found. Columns seen: https://docs.google.com/spreadsheets/…"
    # — technically true, and it sends someone off to add a Company column to a URL.
    if _LOOKS_LIKE_A_LINK.match(raw.strip()) and "\n" not in raw.strip():
        raise SheetError(
            "That is a link to the sheet, not the sheet. Open it, select the rows including the "
            "header, copy, and paste them here — nothing is read from the URL and no Google "
            "access is needed.")

    delim = _sniff(raw)
    rows = [r for r in csv.reader(io.StringIO(raw), delimiter=delim)]
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        raise SheetError("Nothing pasted.")

    hm = header_map(rows[0])
    found = [c.strip() for c in rows[0] if (c or "").strip()]
    if "company" not in hm:
        raise SheetError(
            "No company column found. Add a column headed 'Company'. "
            f"Columns seen: {', '.join(found) or '(none)'}")
    if "name" not in hm and "first" not in hm and "last" not in hm:
        raise SheetError(
            "No name column found. Add a column headed 'Name' (or 'First Name' / 'Last Name'). "
            f"Columns seen: {', '.join(found)}")

    body = rows[1:]
    dropped = max(0, len(body) - MAX_ROWS)
    body = body[:MAX_ROWS]

    companies: dict[str, dict] = {}
    people: list[dict] = []
    rejected: list[dict] = []
    # Deduplicated per COMPANY, not globally: the same person legitimately appears under two
    # employers in a sheet accumulated over time, and those are two contacts on two cards.
    seen_email: set[tuple[str, str]] = set()
    seen_name: set[tuple[str, str]] = set()

    for offset, row in enumerate(body):
        line = offset + 2                      # 1-based, and row 1 is the header
        text_of = delim.join(c for c in row).strip()
        company = _cell(row, hm.get("company"))
        cslug = slug(company)
        if not cslug:
            rejected.append({"line": line, "reason": "no company", "text": text_of})
            continue
        full_name = _person_name(row, hm)
        if not full_name:
            rejected.append({"line": line, "reason": "no name", "text": text_of})
            continue

        email = _cell(row, hm.get("email")).lower()
        if email and "@" not in email:
            # Kept as a PERSON, dropped as an address. A malformed cell is not a reason to lose
            # the name and title, and storing it would send mail nowhere.
            rejected.append({"line": line, "reason": f"unusable email {email!r} — person kept",
                             "text": text_of})
            email = ""

        key_e = (cslug, email)
        key_n = (cslug, full_name.strip().lower())
        if (email and key_e in seen_email) or key_n in seen_name:
            rejected.append({"line": line, "reason": "duplicate of an earlier row",
                             "text": text_of})
            continue
        if email:
            seen_email.add(key_e)
        seen_name.add(key_n)

        domain = _domain(_cell(row, hm.get("domain")))
        about = _cell(row, hm.get("about"))
        entry = companies.setdefault(cslug, {"slug": cslug, "name": company.strip(),
                                             "domain": domain, "about": about, "people": 0})
        # First non-empty wins for both; a later blank must not erase what an earlier row
        # supplied. That is what lets the operator fill the blurb on ONE of a company's rows
        # rather than repeating it down the column.
        if domain and not entry["domain"]:
            entry["domain"] = domain
        if about and not entry["about"]:
            entry["about"] = about
        entry["people"] += 1

        people.append({
            "company_slug": cslug,
            "company": entry["name"],
            "full_name": full_name.strip(),
            "title": _cell(row, hm.get("title")),
            "email": email,
            "linkedin_url": _linkedin(_cell(row, hm.get("linkedin"))),
            "notes": _cell(row, hm.get("notes")),
            "line": line,
        })

    return {"companies": list(companies.values()), "people": people,
            "rejected": rejected, "dropped": dropped, "headers": sorted(hm)}


#: What each field is FOR, in the order the gap costs something.
#:
#: The sentence is the point. "linkedin: 0 of 105" is a statistic; "no LinkedIn invite can be
#: sent, and there is no profile to read before writing" is the consequence, and the consequence
#: is what tells the operator whether to go back to the sheet.
#:
#: `unit` is which list the count runs over — a blurb about the company is one cell per COMPANY,
#: and reporting it out of 105 rows would understate a sheet that fills it correctly once.
_COVERAGE: tuple[tuple[str, str, str, str, str], ...] = (
    ("email", "people", "email", "Email",
     "cannot be emailed, and email is the only channel that sends"),
    ("linkedin_url", "people", "linkedin", "LinkedIn URL",
     "no LinkedIn invite, and no profile to read before writing"),
    ("full_name", "people", "", "Full name",
     "a first name alone cannot be matched to a real person"),
    ("title", "people", "title", "Title",
     "the draft cannot say what they do"),
    ("about", "companies", "about", "About",
     "the draft has nothing to say about what the company does"),
    ("domain", "companies", "domain", "Website",
     "no domain to confirm the company against"),
)


def _has(entry: dict, field: str) -> bool:
    """Is this field actually supplied? `full_name` needs BOTH halves, not merely a value.

    Every person that survives `parse` has a name — it is a rejection reason — so counting
    non-empty names reports 100% on a sheet of bare first names, which is the exact sheet this
    check exists to catch.
    """
    value = str(entry.get(field) or "").strip()
    if field == "full_name":
        return len(value.split()) >= 2
    return bool(value)


def coverage(parsed: dict) -> list[dict]:
    """Per field: was the COLUMN there, and how many rows filled it.

    Those are different findings with different fixes, and merging them into one percentage
    loses the half that matters. A column nobody added is a change to the sheet's SHAPE — go and
    add it. A column that is present and empty is a change to its CONTENTS — go and fill it in.
    "0 of 105 have a LinkedIn URL" does not say which one you are looking at.

    Runs over the PARSE, not over what was stored, so it describes the paste in front of the
    operator rather than the accumulated state of a card — a re-import that adds one column to
    an existing sheet is answered honestly by "this paste supplies it", and the earlier import
    is not re-litigated.
    """
    people = parsed.get("people") or []
    companies = parsed.get("companies") or []
    headers = set(parsed.get("headers") or [])
    out = []
    for field, unit, header, label, cost in _COVERAGE:
        rows = people if unit == "people" else companies
        if not rows:
            continue
        have = sum(1 for r in rows if _has(r, field))
        # A name has no single column: it arrives as `name`, or as `first` + `last`. Only the
        # split case can be MISSING a column while still producing names, and saying so is the
        # actionable half — a sheet with First Name and no Last Name is a header fix, while one
        # with both and 55 blanks is a data fix.
        if field == "full_name":
            present = "name" in headers or ("first" in headers and "last" in headers)
        else:
            present = header in headers
        out.append({
            "field": field, "unit": unit, "label": label, "cost": cost,
            "have": have, "total": len(rows), "column": present,
            "ok": have == len(rows),
        })
    return out


def recognised_columns() -> list[dict]:
    """Every header spelling this parser accepts, for showing in the import box.

    Generated from `_FIELDS` rather than written out beside it. A hand-maintained list of what
    the importer reads is a second source of truth that goes stale the first time a spelling is
    added, and the operator has no way to tell which of the two is lying.
    """
    required = {"company", "name", "first", "last"}
    return [{"field": f, "spellings": list(s), "required": f in required}
            for f, s in _FIELDS.items()]
