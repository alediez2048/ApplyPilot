"""ARCH-5: numbered migrations that run exactly once and say when they don't.

The additive column dicts handle ~15 schema changes without friction and are staying. This
covers what they cannot express — rename, drop, backfill — and the three ways a migration
runner gets it wrong: running twice, running never, and failing silently.

The concurrency test is the one that matters. The dashboard and a CLI command routinely
start within the same second (the dashboard shells out to `applypilot apply`), and a
backfill that runs twice on a `contacts` table is not a theoretical problem.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

import applypilot.database as database
from applypilot import migrations


@pytest.fixture()
def dbpath(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    return path


def _conn(path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), timeout=30)
    c.row_factory = sqlite3.Row
    return c


# ── the framework ───────────────────────────────────────────────────────────

def test_version_starts_at_zero_and_is_recorded(dbpath):
    conn = _conn(dbpath)
    migrations.init(conn)
    assert migrations.current_version(conn) == 0
    assert migrations.status(conn)["applied"] == []


def test_a_migration_runs_exactly_once(dbpath, monkeypatch):
    conn = _conn(dbpath)
    calls = []
    monkeypatch.setattr(migrations, "discover",
                        lambda: [(1, "m001_x", _module(lambda c: calls.append(1)))])
    migrations.run_pending(conn)
    migrations.run_pending(conn)
    migrations.run_pending(conn)
    assert calls == [1]
    assert migrations.current_version(conn) == 1


def test_concurrent_starts_do_not_double_apply(dbpath, monkeypatch):
    """The dashboard and a CLI start in the same second; both call init_db().

    A plain "SELECT then INSERT" would let both read "not applied" and run the migration
    twice. The claim takes BEGIN IMMEDIATE, so the second process blocks, then sees the row.
    """
    calls = []
    lock = threading.Lock()

    def slow_up(_conn):
        with lock:
            calls.append(1)
        threading.Event().wait(0.05)      # widen the window a real race would use

    monkeypatch.setattr(migrations, "discover", lambda: [(1, "m001_x", _module(slow_up))])

    errors = []

    def worker():
        try:
            migrations.run_pending(_conn(dbpath))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert calls == [1], f"migration ran {len(calls)} times across 6 concurrent starts"


def test_a_failure_is_recorded_with_its_version_and_leaves_the_db_usable(dbpath, monkeypatch):
    conn = _conn(dbpath)

    def boom(_c):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(migrations, "discover", lambda: [(1, "m001_boom", _module(boom))])
    results = migrations.run_pending(conn)

    assert results[0]["ok"] is False and "disk on fire" in results[0]["error"]
    st = migrations.status(conn)
    assert st["version"] == 0                    # not counted as applied
    assert st["failed"][0]["version"] == 1
    assert "disk on fire" in st["failed"][0]["error"]
    conn.execute("SELECT 1").fetchone()          # database still usable


def test_a_crashed_run_is_reclaimed_once_its_lease_expires(dbpath, monkeypatch):
    """A process killed mid-migration leaves `running` behind and never reports back.

    A live `running` claim must be respected (that is what stops double-applying), so the
    only thing separating "someone is working on it" from "someone died" is the lease.
    """
    conn = _conn(dbpath)
    migrations.init(conn)
    conn.execute("INSERT INTO schema_migrations (version, name, status, claimed_at) "
                 "VALUES (1, 'm001_x', 'running', ?)",
                 ("2026-07-29T10:00:00+00:00",))          # hours ago
    conn.commit()
    ran = []
    monkeypatch.setattr(migrations, "discover", lambda: [(1, "m001_x", _module(lambda c: ran.append(1)))])
    migrations.run_pending(conn)
    assert ran == [1] and migrations.current_version(conn) == 1


def test_a_live_claim_is_respected(dbpath, monkeypatch):
    """The other half: a fresh `running` row means a process is mid-migration. Hands off."""
    conn = _conn(dbpath)
    migrations.init(conn)
    from datetime import datetime, timezone
    conn.execute("INSERT INTO schema_migrations (version, name, status, claimed_at) "
                 "VALUES (1, 'm001_x', 'running', ?)",
                 (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    ran = []
    monkeypatch.setattr(migrations, "discover", lambda: [(1, "m001_x", _module(lambda c: ran.append(1)))])
    migrations.run_pending(conn)
    assert ran == [], "stole a migration from a live process"


def test_a_failed_migration_is_retried_not_wedged(dbpath, monkeypatch):
    """A killed process must not permanently block the database.

    This app gets killed mid-operation routinely — that is how two applies died on
    2026-07-29. Refusing to retry would turn one interruption into a wedged install.
    Migrations are required to be idempotent precisely so retrying is safe.
    """
    conn = _conn(dbpath)
    attempts = []

    def flaky(_c):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("interrupted")

    monkeypatch.setattr(migrations, "discover", lambda: [(1, "m001_flaky", _module(flaky))])
    migrations.run_pending(conn)
    assert migrations.current_version(conn) == 0
    migrations.run_pending(conn)
    assert migrations.current_version(conn) == 1 and len(attempts) == 2


def test_a_failure_stops_later_migrations(dbpath, monkeypatch):
    """002 may depend on 001. Running it over a half-migrated database is worse than stopping."""
    conn = _conn(dbpath)
    ran = []
    monkeypatch.setattr(migrations, "discover", lambda: [
        (1, "m001_boom", _module(lambda c: (_ for _ in ()).throw(RuntimeError("nope")))),
        (2, "m002_next", _module(lambda c: ran.append(2))),
    ])
    migrations.run_pending(conn)
    assert ran == []
    # 1 is still pending too (failed != applied); the point is that 2 never ran.
    assert 2 in [p_["version"] for p_ in migrations.status(conn)["pending"]]
    assert migrations.status(conn)["failed"][0]["version"] == 1


def test_migrations_are_discovered_in_numeric_order():
    found = migrations.discover()
    assert [v for v, _, _ in found] == sorted(v for v, _, _ in found)
    assert found, "no migrations discovered — the mNNN_ naming convention broke"


# ── migration 001 against real shapes ───────────────────────────────────────

def test_001_is_a_noop_on_a_fresh_database(dbpath):
    conn = database.init_db(dbpath)          # runs migrations as part of startup
    assert migrations.current_version(conn) == 1
    cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    assert not [c for c in cols if "followup" in c]


def test_001_migrates_an_old_database_and_is_idempotent(dbpath):
    """Build a pre-ARCH-3 database, migrate it, and run it twice more."""
    from applypilot.networking import backfill_touches as B
    from applypilot.networking import store, touches

    conn = database.init_db(dbpath)
    store.init_contacts(conn)
    touches.init_touches(conn)
    for col in B.LEGACY_COLUMNS:                       # resurrect the old shape
        kind = "INTEGER DEFAULT 0" if col.endswith("_count") else "TEXT"
        conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {kind}")
    conn.execute("INSERT INTO jobs (url, title, strategy) VALUES ('http://j/1','PM','dashboard_upload')")
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Ali C"}, conn)
    conn.execute("UPDATE contacts SET followup_count=2, followed_up_at=?, followup_status='sent' "
                 "WHERE id=?", ("2026-07-20T10:00:00+00:00", cid))
    conn.execute("DELETE FROM schema_migrations")      # pretend 001 never ran
    conn.commit()

    from applypilot.migrations import m001_touches_backfill as m1
    for _ in range(3):                                 # idempotent: three runs, same result
        m1.up(conn)
        assert touches.ladder_state(cid, "email", conn)["count"] == 2
    cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    assert not [c for c in cols if "followup" in c], "legacy columns survived the migration"


def test_001_refuses_to_drop_when_verification_fails(dbpath, monkeypatch):
    """The whole safety property of an irreversible migration.

    If the new tables do not re-derive the old columns, the old columns must survive —
    they are still the truth, and the old code path can still read them.
    """
    from applypilot.networking import backfill_touches as B
    from applypilot.networking import store, touches
    from applypilot.migrations import m001_touches_backfill as m1

    conn = database.init_db(dbpath)
    store.init_contacts(conn)
    touches.init_touches(conn)
    for col in B.LEGACY_COLUMNS:
        kind = "INTEGER DEFAULT 0" if col.endswith("_count") else "TEXT"
        conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {kind}")
    conn.execute("INSERT INTO jobs (url, title, strategy) VALUES ('http://j/1','PM','dashboard_upload')")
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Ali C"}, conn)
    conn.execute("UPDATE contacts SET followup_count=1, followup_status='sent' WHERE id=?", (cid,))
    conn.commit()

    monkeypatch.setattr(B, "verify", lambda c=None: ["synthetic mismatch"])
    with pytest.raises(RuntimeError, match="did not round-trip"):
        m1.up(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    assert "followup_count" in cols, "columns were dropped despite a failed verification"


def test_fresh_and_fully_migrated_databases_have_identical_schemas(tmp_path, monkeypatch):
    """ARCH-5 criterion: an upgraded old DB must not be a second, subtly different shape."""
    from applypilot.networking import backfill_touches as B
    from applypilot.networking import store, touches

    def build(name: str, legacy: bool):
        path = tmp_path / name
        monkeypatch.setattr(database, "DB_PATH", path)
        database.close_connection(path)
        conn = database.init_db(path)
        store.init_contacts(conn)
        touches.init_touches(conn)
        if legacy:
            for col in B.LEGACY_COLUMNS:
                kind = "INTEGER DEFAULT 0" if col.endswith("_count") else "TEXT"
                conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {kind}")
            conn.execute("DELETE FROM schema_migrations")
            conn.commit()
            database.forget_schema(conn)
            from applypilot.migrations import m001_touches_backfill as m1
            m1.up(conn)
        # COLUMN SETS, not the CREATE statement. `ALTER TABLE ... ADD COLUMN` appends, so an
        # upgraded database lists the same columns in a different order and its stored SQL
        # is historic. Comparing the text would fail for a difference that does not exist.
        return {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
                for (t,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    fresh = build("fresh.db", legacy=False)
    upgraded = build("old.db", legacy=True)

    assert set(fresh) == set(upgraded), f"table sets differ: {set(fresh) ^ set(upgraded)}"
    for table in sorted(fresh):
        assert fresh[table] == upgraded[table], (
            f"{table} differs between a fresh DB and an upgraded one: "
            f"{fresh[table] ^ upgraded[table]}"
        )


def _module(fn):
    """Minimal stand-in for a migration module."""
    class M:
        NOTE = "test"
        up = staticmethod(fn)
    return M
