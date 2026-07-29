"""Your LinkedIn connections — imported from LinkedIn's CSV export, matched locally.

There is no API to read your own connections, so we use LinkedIn's official data
export (Settings → Data Privacy → Get a copy of your data → Connections). The user
imports that CSV once; we store it in a local `connections` table and match found
contacts against it to surface:
  - company-level: "you already have N connections at {company}"
  - contact-level: "this exact person is already a 1st-degree connection"

All offline, no scraping, no ToS risk.
"""

from __future__ import annotations

import csv
import logging
import re
import sqlite3
from datetime import datetime, timezone
from hashlib import sha1

from applypilot.database import get_connection, schema_ready
# Company matching is a shared domain rule — connections, Apollo org resolution and contact
# verification must all agree. Lives in domain/company.py; re-exported for existing callers.
from applypilot.domain.company import companies_match, norm_company

log = logging.getLogger(__name__)

_CONN_COLUMNS: dict[str, str] = {
    "id": "TEXT PRIMARY KEY",     # sha1(name_norm + company_norm)
    "full_name": "TEXT",
    "name_norm": "TEXT",
    "company": "TEXT",
    "company_norm": "TEXT",
    "position": "TEXT",
    "url": "TEXT",
    "connected_on": "TEXT",
    "imported_at": "TEXT",
}


def _norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


_norm_company = norm_company   # internal alias, kept for existing call sites


def init_connections(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    if conn is None:
        conn = get_connection()
    if schema_ready(conn, "connections"):
        return conn
    cols = ", ".join(f"{n} {t}" for n, t in _CONN_COLUMNS.items())
    conn.execute(f"CREATE TABLE IF NOT EXISTS connections ({cols})")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conn_name ON connections(name_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conn_company ON connections(company_norm)")
    conn.commit()
    return conn


def imported_count(conn: sqlite3.Connection | None = None) -> int:
    if conn is None:
        conn = get_connection()
    init_connections(conn)
    return conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0]


# ── import ──────────────────────────────────────────────────────────────────

def _open_rows(path: str):
    """Yield dict rows from LinkedIn's Connections.csv.

    The export has a few 'Notes:' preamble lines before the real header row that
    starts with 'First Name'. Skip until we find it, then DictReader the rest.
    """
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        lines = fh.readlines()
    start = 0
    for i, ln in enumerate(lines):
        if ln.lstrip().lower().startswith("first name,"):
            start = i
            break
    reader = csv.DictReader(lines[start:])
    for row in reader:
        yield {(k or "").strip(): (v or "").strip() for k, v in row.items()}


def import_csv(path: str, conn: sqlite3.Connection | None = None) -> int:
    """Import a LinkedIn Connections.csv. Replaces the existing set. Returns count."""
    if conn is None:
        conn = get_connection()
    init_connections(conn)
    now = datetime.now(timezone.utc).isoformat()

    rows = list(_open_rows(path))
    # Full re-import (the export is the complete list): clear then insert.
    conn.execute("DELETE FROM connections")
    count = 0
    for r in rows:
        first = r.get("First Name", "")
        last = r.get("Last Name", "")
        full = f"{first} {last}".strip()
        if not full:
            continue
        company = r.get("Company", "")
        name_norm = _norm_name(full)
        company_norm = _norm_company(company)
        cid = sha1(f"{name_norm}\x1f{company_norm}".encode()).hexdigest()[:16]
        conn.execute(
            "INSERT OR REPLACE INTO connections "
            "(id, full_name, name_norm, company, company_norm, position, url, connected_on, imported_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, full, name_norm, company, company_norm, r.get("Position", ""),
             r.get("URL", ""), r.get("Connected On", ""), now),
        )
        count += 1
    conn.commit()
    log.info("Imported %d LinkedIn connections", count)
    return count


# ── matching ────────────────────────────────────────────────────────────────

def match(full_name: str | None, company: str | None = None,
          conn: sqlite3.Connection | None = None) -> dict | None:
    """Return a connection record if `full_name` is a 1st-degree connection, else None.

    Matches on normalized name. If `company` is given and the connection's company
    also matches, the result carries company_match=True (a stronger signal — the
    person is a connection AND currently at this company).
    """
    name_norm = _norm_name(full_name)
    if not name_norm:
        return None
    if conn is None:
        conn = get_connection()
    init_connections(conn)
    rows = conn.execute(
        # ORDER BY rowid so a duplicated name resolves the same way every call — and the
        # same way as match_many(), which has to agree with this row for row.
        "SELECT full_name, company, company_norm, position, url FROM connections "
        "WHERE name_norm = ? ORDER BY rowid",
        (name_norm,),
    ).fetchall()
    if not rows:
        return None
    target = _norm_company(company)
    best = None
    for r in rows:
        rec = dict(zip(r.keys(), r))
        rec["company_match"] = companies_match(target, rec["company_norm"])
        if rec["company_match"]:
            return rec  # exact-ish company match wins immediately
        best = best or rec
    return best


def match_many(full_names: list[str | None], company: str | None = None,
               conn: sqlite3.Connection | None = None) -> dict[str, dict]:
    """`match()` for many people in one query, keyed by the ORIGINAL name string.

    Same selection rule as `match()`: a company match wins outright, otherwise the first
    row for that name. Kept in one place so the two can't drift — `test_match_many_agrees_
    with_match` compares them row by row.
    """
    wanted = {(n or ""): _norm_name(n) for n in full_names}
    wanted = {orig: nn for orig, nn in wanted.items() if nn}
    if not wanted:
        return {}
    if conn is None:
        conn = get_connection()
    init_connections(conn)
    norms = sorted(set(wanted.values()))
    rows = conn.execute(
        f"SELECT name_norm, full_name, company, company_norm, position, url FROM connections "
        f"WHERE name_norm IN ({','.join('?' for _ in norms)}) ORDER BY rowid", norms,
    ).fetchall()

    target = _norm_company(company)
    by_norm: dict[str, dict] = {}
    for r in rows:
        # `name_norm` is an internal key, dropped so a record from here is indistinguishable
        # from one `match()` returns — otherwise the two disagree on dict equality and any
        # equivalence check between them is quietly meaningless.
        rec = {k: r[k] for k in r.keys() if k != "name_norm"}
        rec["company_match"] = companies_match(target, rec["company_norm"])
        current = by_norm.get(r["name_norm"])
        if current is None or (rec["company_match"] and not current["company_match"]):
            by_norm[r["name_norm"]] = rec
    return {orig: by_norm[nn] for orig, nn in wanted.items() if nn in by_norm}


def count_at_company(company: str | None, conn: sqlite3.Connection | None = None) -> int:
    """How many of your connections currently list `company` as their employer."""
    return company_counts([company], conn).get(company or "", 0)


def company_counts(companies: list[str | None],
                   conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Connection counts for MANY companies in one pass.

    `companies_match` is word-aware (see CLAUDE.md §Lessons 1 — substring matching shipped
    four bugs), so this cannot be pushed into SQL and has to compare in Python. Called once
    per company that was fine; called once per JOB it meant re-scanning all 899 connection
    rows every time — 7,192 comparisons per dashboard refresh, at 2.5s intervals.

    One scan, all companies.
    """
    targets = {(c or ""): _norm_company(c) for c in companies}
    targets = {orig: t for orig, t in targets.items() if t}
    out: dict[str, int] = {(c or ""): 0 for c in companies}
    if not targets:
        return out
    if conn is None:
        conn = get_connection()
    init_connections(conn)
    for (cn,) in conn.execute(
        "SELECT company_norm FROM connections WHERE company_norm != ''"
    ).fetchall():
        for orig, target in targets.items():
            if companies_match(target, cn):
                out[orig] += 1
    return out


def at_company(company: str | None, limit: int = 25,
               conn: sqlite3.Connection | None = None) -> list[dict]:
    """Your 1st-degree connections who currently work at `company` (the 'hot' layer).

    Returns connection records {full_name, company, position, url}, most-recently-connected first.
    Company matching is word-aware (see companies_match) — a raw substring test made short
    employer names like "Arm" match Armanino, State Farm and Centrient Pharmaceuticals.
    """
    target = _norm_company(company)
    if not target:
        return []
    if conn is None:
        conn = get_connection()
    init_connections(conn)
    rows = conn.execute(
        "SELECT full_name, company, company_norm, position, url FROM connections "
        "WHERE company_norm != '' ORDER BY connected_on DESC"
    ).fetchall()
    out = []
    for r in rows:
        rec = dict(zip(r.keys(), r))
        cn = rec.get("company_norm") or ""
        if companies_match(target, cn):
            rec.pop("company_norm", None)
            out.append(rec)
            if len(out) >= limit:
                break
    return out
