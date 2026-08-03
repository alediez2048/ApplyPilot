"""The `ats_accounts` table: which employers you can already apply to without a wall.

One row per auth realm (see `domain/authrealm.py`), not per job. The whole reason this table
exists is that the previous design had nowhere to put the answer: every apply walked up to
Salesforce's Workday fresh, spent 59 seconds and a full agent run discovering a login page, and
recorded the finding on the JOB — where the next job at the same employer could never read it.

Two independent things are tracked, and conflating them is the bug this table is shaped to
avoid:

    kind          does this realm need an account at all?      (about the SITE)
    have_account  do we have one?                              (about US)

A wall you have no account for is a registration, which takes a human several minutes and their
agreement to someone's terms. A wall you DO have an account for is an expired session, which
takes twenty seconds. They look identical in the browser and they are not the same problem.

Schema init follows the `schema_ready` memo (§Lessons 11): `CREATE TABLE IF NOT EXISTS` is
cheap per statement and ruinous at 2.5-second refresh rates, so it runs once per connection.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from applypilot.database import get_connection, schema_ready
from applypilot.domain import authrealm

#: How we came to believe an account exists, weakest first. A stronger source overwrites a
#: weaker one; the reverse must not happen, or one stale cookie sweep undoes what you told it.
EVIDENCE_RANK = {"": 0, "cookie": 1, "saved-login": 2, "agent": 3, "operator": 4}


def _c(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_connection()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_accounts(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    c = _c(conn)
    if schema_ready(c, "ats_accounts"):
        return c
    c.execute("""
        CREATE TABLE IF NOT EXISTS ats_accounts (
            realm_id      TEXT PRIMARY KEY,
            kind          TEXT NOT NULL,      -- account | sso | none | unknown
            vendor        TEXT,
            label         TEXT,
            have_account  INTEGER,            -- NULL = never established
            evidence      TEXT,               -- cookie | saved-login | agent | operator
            -- Whether realm_id names ONE employer. Stored rather than re-derived: it decides
            -- whether a cookie counts as proof, and every tenant on wd1.myworkdaysite.com
            -- shares a host, so getting it wrong marks all of them solved off one visit.
            host_is_tenant INTEGER DEFAULT 1,
            -- The browser has cookies for this realm. That means somebody LOADED a page there,
            -- which is not the same as having an account, so it never satisfies `preflight` —
            -- it only makes the realm worth ASKING the operator about (§Lessons 34: an
            -- inference must not be handed to a check that treats it as proof).
            session_seen  INTEGER DEFAULT 0,
            signup_url    TEXT,               -- where we last hit the wall; ground truth
            blocked_count INTEGER DEFAULT 0,
            first_seen    TEXT,
            last_seen     TEXT,
            confirmed_at  TEXT,
            note          TEXT
        )
    """)
    c.commit()
    return c


def _dict(row) -> dict | None:
    return dict(zip(row.keys(), row)) if row else None


# ── reads ───────────────────────────────────────────────────────────────────

def get(realm_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    c = init_accounts(conn)
    return _dict(c.execute("SELECT * FROM ats_accounts WHERE realm_id = ?",
                           (realm_id,)).fetchone())


def all_realms(conn: sqlite3.Connection | None = None) -> list[dict]:
    c = init_accounts(conn)
    return [dict(zip(r.keys(), r)) for r in c.execute(
        "SELECT * FROM ats_accounts ORDER BY "
        "  CASE WHEN kind IN ('account','sso') AND COALESCE(have_account,0)=0 THEN 0 ELSE 1 END,"
        "  blocked_count DESC, label COLLATE NOCASE").fetchall()]


def blocking(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Realms that need an account we do not have. The Accounts panel's working set."""
    c = init_accounts(conn)
    return [dict(zip(r.keys(), r)) for r in c.execute(
        "SELECT * FROM ats_accounts WHERE kind IN ('account','sso') "
        "AND COALESCE(have_account, 0) = 0 "
        "ORDER BY blocked_count DESC, label COLLATE NOCASE").fetchall()]


# ── writes ──────────────────────────────────────────────────────────────────

def see(realm: authrealm.Realm, url: str = "", conn: sqlite3.Connection | None = None) -> None:
    """Record that a job belongs to this realm. Never downgrades what is already known.

    `kind` is deliberately only widened, never narrowed: `resolve()` returns UNKNOWN for hosts
    it has no rule for, and a later apply may LEARN that the host walls you. Letting a fresh
    UNKNOWN overwrite that learned ACCOUNT would forget the lesson every time the job list
    refreshed.
    """
    c = init_accounts(conn)
    row = get(realm.id, c)
    if row is None:
        c.execute(
            "INSERT INTO ats_accounts (realm_id, kind, vendor, label, host_is_tenant, "
            "signup_url, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
            (realm.id, realm.kind, realm.vendor, realm.label,
             1 if realm.host_is_tenant else 0, url, _now(), _now()))
    else:
        kind = row["kind"]
        if kind == authrealm.UNKNOWN and realm.kind != authrealm.UNKNOWN:
            kind = realm.kind
        c.execute("UPDATE ats_accounts SET kind = ?, vendor = COALESCE(NULLIF(?,''), vendor), "
                  "label = COALESCE(NULLIF(?,''), label), last_seen = ? WHERE realm_id = ?",
                  (kind, realm.vendor, realm.label, _now(), realm.id))
    c.commit()


def learn_from_wall(realm: authrealm.Realm, url: str = "",
                    conn: sqlite3.Connection | None = None) -> None:
    """An apply hit a sign-in wall here. That settles `kind` for every future job.

    Also stores the URL as `signup_url`. Guessing a registration link per vendor would be four
    more rules to maintain and wrong for the fifth; the page we were actually stopped on is the
    page the human needs, by construction.
    """
    c = init_accounts(conn)
    see(realm, url, c)
    kind = authrealm.SSO if realm.kind == authrealm.SSO else authrealm.ACCOUNT
    c.execute("UPDATE ats_accounts SET kind = ?, blocked_count = COALESCE(blocked_count,0) + 1, "
              "signup_url = COALESCE(NULLIF(?, ''), signup_url), last_seen = ? "
              "WHERE realm_id = ?", (kind, url, _now(), realm.id))
    c.commit()


def set_have_account(realm_id: str, have: bool, evidence: str,
                     conn: sqlite3.Connection | None = None) -> bool:
    """Record whether an account exists. Returns False when weaker evidence was ignored.

    A cookie sweep runs on a schedule and the operator clicks a button once. If the sweep could
    overwrite the click, the answer would silently revert the next time a cookie expired — the
    operator would tell it the same thing repeatedly and watch it forget.
    """
    c = init_accounts(conn)
    row = get(realm_id, c)
    if row is None:
        return False
    if EVIDENCE_RANK.get(evidence, 0) < EVIDENCE_RANK.get(row["evidence"] or "", 0):
        return False
    c.execute("UPDATE ats_accounts SET have_account = ?, evidence = ?, confirmed_at = ? "
              "WHERE realm_id = ?", (1 if have else 0, evidence, _now(), realm_id))
    c.commit()
    return True


def note_session(realm_id: str, conn: sqlite3.Connection | None = None) -> None:
    """The apply browser holds cookies for this realm. A hint for the operator, not an answer."""
    c = init_accounts(conn)
    c.execute("UPDATE ats_accounts SET session_seen = 1 WHERE realm_id = ?", (realm_id,))
    c.commit()


def set_note(realm_id: str, note: str, conn: sqlite3.Connection | None = None) -> None:
    c = init_accounts(conn)
    c.execute("UPDATE ats_accounts SET note = ? WHERE realm_id = ?", (note[:400], realm_id))
    c.commit()


def forget(realm_id: str, conn: sqlite3.Connection | None = None) -> int:
    c = init_accounts(conn)
    n = c.execute("DELETE FROM ats_accounts WHERE realm_id = ?", (realm_id,)).rowcount
    c.commit()
    return n
