"""Write a parsed spreadsheet into a targets Space: one card per company, one contact per person.

SHEET-1 C2. The parsing is pure and lives in `domain/sheet.py`; this is the half that writes.

**These contacts are SUPPLIED, not discovered**, and three consequences of that are decided here
rather than inherited from the Apollo path:

`source='import'` — never `'apollo'`. CRM-2's `by_layer()` exists to compare a warm channel
against a cold list, and filing a hand-built list as a cold find makes that question unanswerable
forever.

**Verification is SKIPPED, not run and passed.** `verify_contact` exists to catch people who work
somewhere ELSE, which cannot happen to a name the operator typed into their own sheet (§Lessons
19). Running it here would drop every imported person for having no Apollo record — §Lessons 14,
where narrowing before checking meant being right produced nothing.

`email_status` is `unverified` with an address and `none` without. `verified` is a claim about the
ADDRESS, and a spreadsheet is not evidence — the same rule that already separates a Cc taken off a
live thread from one typed from memory.

Idempotent by construction: `add_target` is idempotent on the anchor and `contact_id` hashes
(job_url, linkedin, name), so re-pasting the same sheet updates rather than duplicating. That
matters more than usual here, because the natural way to use this is to paste a growing sheet
again.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from applypilot.domain import sheet as _sheet
from applypilot.domain import target as _target

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _existing_id(on_card: list[dict], person: dict) -> str:
    """The id this person already has on this card, or "".

    **Found by email then by name, never by `contact_id`.** That hash is
    (job_url, linkedin_url, name), so the SAME PERSON re-imported from a sheet that happens not
    to carry a LinkedIn column hashes differently and lands as a second contact — with its own
    ladder, its own draft, and a second email to one inbox.

    Found by running it, not by reading it: a second import that added one new person reported
    two, and the card showed "Dana Okafor" twice, once with a LinkedIn URL and once without.
    Re-importing a GROWN sheet is the normal way to use this feature, so the duplicate is the
    default outcome rather than an edge case. §Lessons 22 — idempotence has to be tested by
    running it twice.

    This is CO-1's unfixed keying problem showing up somewhere new. It is fixed HERE rather than
    in `contact_id` because changing that hash would re-key all 244 stored contacts.
    """
    email = (person.get("email") or "").strip().lower()
    name = (person.get("full_name") or "").strip().lower()
    if email:
        for c in on_card:
            if (c.get("email") or "").strip().lower() == email:
                return c["id"]
    if name:
        for c in on_card:
            if (c.get("full_name") or "").strip().lower() == name:
                return c["id"]
    return ""


def import_sheet(space_id: str, text: str, conn=None) -> dict:
    """Parse and write. Returns a report the dashboard renders verbatim.

    Raises `domain.sheet.SheetError` when the paste cannot be read at all — a missing company
    column is a different thing from rows that individually failed, and merging them would make
    "fix your header" read as "three of your rows are bad".
    """
    from applypilot.database import get_connection, log_event
    from applypilot.networking.store import (
        contact_id, contact_ref, get_contacts_for_job, init_contacts, upsert_contact,
    )
    from applypilot.repo import jobs as _jobs

    conn = conn or get_connection()
    init_contacts(conn)

    parsed = _sheet.parse(text)                      # may raise SheetError — let it
    by_slug = {c["slug"]: c for c in parsed["companies"]}

    cards_added, cards_existing, anchors = [], [], {}
    for c in parsed["companies"]:
        try:
            out = _jobs.add_target(space_id, c["name"], c.get("domain", ""), conn)
        except ValueError:
            # A name that yields no slug cannot reach here — the parser rejected it — but
            # add_target is the authority on its own precondition and must not be assumed.
            parsed["rejected"].append({"line": 0, "reason": f"unusable company {c['name']!r}",
                                       "text": c["name"]})
            continue
        anchors[c["slug"]] = out["url"]
        # The About column, onto the CARD. It feeds "WHAT THIS COMPANY DOES" in every draft for
        # every person here, which is why it belongs on the company rather than on a contact.
        if c.get("about"):
            _jobs.set_about(out["url"], c["about"], conn)
        (cards_added if out["added"] else cards_existing).append(out["name"])
        if out["added"]:
            log_event(out["url"], "system", "ok",
                      f"Imported from a sheet: {out['name']}"
                      + (f" ({c['domain']})" if c.get("domain") else ""), conn)

    people_added, people_updated, people_skipped = 0, 0, 0
    # Who is already on each card, so a re-import finds them by WHO THEY ARE rather than by a
    # hash that a missing column changes. Loaded once per card, not per person.
    existing_by_card: dict[str, list[dict]] = {}
    for p in parsed["people"]:
        anchor = anchors.get(p["company_slug"])
        if not anchor:
            people_skipped += 1                      # its company failed above; already reported
            continue
        if anchor not in existing_by_card:
            existing_by_card[anchor] = get_contacts_for_job(anchor, conn)
        cid = _existing_id(existing_by_card[anchor], p) or contact_id(
            anchor, p.get("linkedin_url"), p.get("full_name"))
        # Through the store, not a SELECT here. ARCH-4's boundary test caught the raw query and
        # the fix is to route it, never to join the allowlist — that list is supposed to only
        # ever shrink. `contact_ref` is the existing "does this exist" read and fetches two
        # columns rather than the 42-column row.
        existed = contact_ref(cid, conn) is not None
        # A COLUMN THE SHEET NO LONGER CARRIES MUST NOT ERASE WHAT AN EARLIER ONE SUPPLIED.
        # `upsert_contact` skips fields that are None and WRITES fields that are "" — so passing
        # `p.get("linkedin_url") or ""` blanks the profile URL the moment the operator re-exports
        # without that column, which is a normal thing to do. Found by a test, not by review.
        def keep(value):
            return value if (value or "").strip() else None

        email = keep(p.get("email"))
        upsert_contact({
            "id": cid,
            "job_url": anchor,
            "space_id": space_id,
            "full_name": p["full_name"],
            "title": keep(p.get("title")),
            "email": email,
            "linkedin_url": keep(p.get("linkedin_url")),
            "company": by_slug[p["company_slug"]]["name"],
            "source": "import",
            # Only stated when there is an address to state it about. On a re-import with no
            # email column this stays None and the stored status survives; a brand-new contact
            # with no address gets "none", which is what it means.
            "email_status": "unverified" if email else (None if existed else "none"),
            # No unconfirmed chip. Verification catches people who work somewhere else, which is
            # not a thing that happens to a name the operator chose (§Lessons 19).
            "confidence": "high",
            "verify_note": "from an imported sheet — not verified against a provider",
            "notes": keep(p.get("notes")),
            "discovered_at": _now(),
        }, conn)
        if existed:
            people_updated += 1
        else:
            people_added += 1

    conn.commit()
    return {
        "ok": True,
        "cards_added": cards_added,
        "cards_existing": cards_existing,
        "people_added": people_added,
        "people_updated": people_updated,
        "people_skipped": people_skipped,
        "rejected": parsed["rejected"],
        "dropped": parsed["dropped"],
        # What the sheet SUPPLIES, beside what it imported. A row count is a claim about the
        # paste succeeding; this is a claim about whether the result can be acted on, and the
        # two came apart badly on the first real sheet — 45 companies and 105 people imported
        # cleanly, of whom 85 had no address to write to and 0 had a LinkedIn URL. The import
        # said "Imported 45 companies, 105 people." and nothing else for weeks (§Lessons 15).
        "coverage": _sheet.coverage(parsed),
        "message": summarize(cards_added, cards_existing, people_added, people_updated,
                             parsed["rejected"], parsed["dropped"]),
    }


def summarize(cards_added, cards_existing, people_added, people_updated,
              rejected, dropped) -> str:
    """One sentence the operator can act on.

    Every non-zero outcome is NAMED. "Imported 38" hides that three rows failed, and a partial
    has to read differently from a success (§Lessons 15) — which is exactly the failure that made
    a contact search that kept nobody look identical to a button that never fired.
    """
    bits = []
    if cards_added:
        bits.append(f"{len(cards_added)} new {'company' if len(cards_added) == 1 else 'companies'}")
    if cards_existing:
        bits.append(f"{len(cards_existing)} already here")
    if people_added:
        bits.append(f"{people_added} {'person' if people_added == 1 else 'people'}")
    if people_updated:
        bits.append(f"{people_updated} updated")
    if not bits:
        bits.append("nothing new")
    msg = "Imported " + ", ".join(bits) + "."
    if rejected:
        lines = ", ".join(str(r["line"]) for r in rejected[:6] if r.get("line"))
        msg += f" {len(rejected)} row(s) skipped" + (f" (line {lines})" if lines else "") + "."
    if dropped:
        msg += (f" {dropped} row(s) past the {_sheet.MAX_ROWS}-row limit were NOT read — "
                f"paste the rest separately.")
    return msg


#: Re-exported so callers catch one name without reaching into `domain/`.
SheetError = _sheet.SheetError


def anchor_for(space_id: str, company: str) -> str:
    """The card a company name lands on. Exposed so a caller can look one up without re-slugging
    — two implementations of "which card is this" is two answers that can disagree."""
    return _target.anchor(space_id, company)
