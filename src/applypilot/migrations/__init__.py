"""Numbered, recorded migrations (ARCH-5).

The additive column dicts (`_ALL_COLUMNS`, `_CONTACT_COLUMNS`, `_TOUCH_COLUMNS`) stay exactly
as they are — they absorbed ~15 schema changes in one session without friction and there is
no reason to replace them. This exists for what they cannot express: **rename, drop, backfill,
and data fixes.** Both real ones so far (reducing `preserved_companies`, re-rendering four
stale cover-letter PDFs) were hand-written one-off scripts that ran once and then vanished.

A migration is a module `mNNN_description.py` exposing `up(conn)`.

**Migrations MUST be idempotent.** This is a single-user local app that can be killed at any
moment — the dashboard runs applies as child processes and gets restarted regularly (see
CLAUDE.md §Known debt). A migration interrupted halfway has to be safe to run again, because
that is what will happen. Every one here is written to be re-runnable, and a test enforces it
by running each migration three times and diffing the result.

**No down-migrations, deliberately** (ticket §Risks). The rollback for a local app is the
backup taken before the run, not an inverse script nobody ever tests.
"""

from __future__ import annotations

import importlib
import pkgutil
import sqlite3
from datetime import datetime, timezone

_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT,
    status     TEXT,          -- running | done | failed
    applied_at TEXT,
    claimed_at TEXT,          -- lease start; see _claim
    error      TEXT
)
"""

# How long a `running` claim is trusted. Long enough that no real migration on a
# single-user database is mistaken for dead; short enough that a killed process does not
# wedge the install for the rest of the day.
LEASE_SECONDS = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE)
    # Additive pass — the table shipped before `claimed_at` existed.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(schema_migrations)")}
    if "claimed_at" not in cols:
        conn.execute("ALTER TABLE schema_migrations ADD COLUMN claimed_at TEXT")
    conn.commit()


def _lease_expired(claimed_at: str | None) -> bool:
    if not claimed_at:
        return True
    from applypilot.domain.timeutil import parse_ts
    started = parse_ts(claimed_at)
    if started is None:
        return True
    return (datetime.now(timezone.utc) - started).total_seconds() > LEASE_SECONDS


def discover() -> list[tuple[int, str, object]]:
    """Every `mNNN_*.py` in this package, ordered by number."""
    found = []
    for mod in pkgutil.iter_modules(__path__):
        name = mod.name
        if not (name.startswith("m") and name[1:4].isdigit()):
            continue
        module = importlib.import_module(f"{__name__}.{name}")
        if not hasattr(module, "up"):
            continue
        found.append((int(name[1:4]), name, module))
    return sorted(found, key=lambda t: t[0])


def applied(conn: sqlite3.Connection) -> dict[int, dict]:
    init(conn)
    return {r["version"]: dict(zip(r.keys(), r))
            for r in conn.execute("SELECT * FROM schema_migrations")}


def current_version(conn: sqlite3.Connection) -> int:
    """Highest successfully applied version, or 0."""
    done = [v for v, row in applied(conn).items() if row.get("status") == "done"]
    return max(done) if done else 0


def pending(conn: sqlite3.Connection) -> list[tuple[int, str, object]]:
    done = {v for v, row in applied(conn).items() if row.get("status") == "done"}
    return [m for m in discover() if m[0] not in done]


def _claim(conn: sqlite3.Connection, version: int, name: str) -> bool:
    """Take exclusive ownership of one migration. False means someone else has it.

    `BEGIN IMMEDIATE` takes the write lock up front, so a dashboard and a CLI starting at
    the same moment serialise here: the second one blocks, then sees the row and skips.
    A plain `INSERT OR IGNORE` outside a transaction would let both read "not applied"
    and run the migration twice.

    Three states, three answers — and the first version of this collapsed two of them,
    which made six concurrent starts run the migration six times:

      done     -> skip. Finished.
      failed   -> reclaim immediately. The process finished and told us it failed; a
                  retry is exactly what should happen, and migrations are idempotent.
      running  -> reclaim ONLY if the lease has expired. A live process is mid-migration;
                  a dead one left the row behind. Without the lease, "retry after a crash"
                  and "don't double-apply" are the same branch and you cannot have both.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status, claimed_at FROM schema_migrations WHERE version = ?",
                           (version,)).fetchone()
        if row:
            if row["status"] == "done":
                conn.rollback()
                return False
            if row["status"] == "running" and not _lease_expired(row["claimed_at"]):
                conn.rollback()
                return False
        conn.execute(
            "INSERT INTO schema_migrations (version, name, status, applied_at, claimed_at, error) "
            "VALUES (?, ?, 'running', NULL, ?, NULL) "
            "ON CONFLICT(version) DO UPDATE SET status='running', claimed_at=excluded.claimed_at, "
            "error=NULL",
            (version, name, _now()))
        conn.commit()
        return True
    except sqlite3.OperationalError:
        # Lock contention past the busy timeout: another process is running migrations.
        # Skipping is correct — it will finish them.
        conn.rollback()
        return False


def run_pending(conn: sqlite3.Connection, on_event=None) -> list[dict]:
    """Apply every pending migration in order. Returns one result dict each.

    A failure stops the run — later migrations may depend on an earlier one — but leaves
    the database usable and records WHICH version failed and why, so `migrate --status`
    can say so instead of the operator finding out through a stack trace.
    """
    init(conn)
    results = []
    for version, name, module in pending(conn):
        if not _claim(conn, version, name):
            continue
        if on_event:
            on_event("start", version, name, "")
        try:
            module.up(conn)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            conn.execute("UPDATE schema_migrations SET status='failed', error=? WHERE version=?",
                         (str(exc)[:500], version))
            conn.commit()
            results.append({"version": version, "name": name, "ok": False, "error": str(exc)})
            if on_event:
                on_event("failed", version, name, str(exc))
            break
        conn.execute("UPDATE schema_migrations SET status='done', applied_at=?, error=NULL "
                     "WHERE version=?", (_now(), version))
        conn.commit()
        results.append({"version": version, "name": name, "ok": True,
                        "note": getattr(module, "NOTE", "")})
        if on_event:
            on_event("done", version, name, "")
    return results


def status(conn: sqlite3.Connection) -> dict:
    init(conn)
    rows = applied(conn)
    return {
        "version": current_version(conn),
        "applied": sorted(v for v, r in rows.items() if r.get("status") == "done"),
        "failed": [{"version": v, "name": r.get("name"), "error": r.get("error")}
                   for v, r in sorted(rows.items()) if r.get("status") == "failed"],
        "pending": [{"version": v, "name": n} for v, n, _ in pending(conn)],
    }
