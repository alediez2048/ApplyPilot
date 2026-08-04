"""SPACE-1a: `space_id` is membership, the registry is never empty, and 003 re-keys nothing.

The migration this covers is deliberately small — two tables and two rows — because the two
schema changes `spaces-prd.md` §6 put in it belong in the additive column dicts and are there
instead. What is left is the part with a failure mode: a registry that can be read before it is
seeded makes every pipeline queue select nothing, which looks exactly like a healthy run with
nothing to do (§Lessons 15, §Lessons 44).

The contact-id test is the one that would hurt to lose. 196 contacts key 55 touches, 24
sequences, 142 messages and 10 interactions off `contact_id`, and nothing in the schema would
complain if they all detached at once.
"""

from __future__ import annotations

import sqlite3

import pytest

import applypilot.database as database
from applypilot import migrations
from applypilot.migrations import m003_spaces
from applypilot.networking import store, touches
from applypilot.repo import spaces


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    conn.execute("INSERT INTO jobs (url, title, site, strategy) VALUES (?,?,?,?)",
                 ("http://j/1", "PM", "Greenhouse", "dashboard_upload"))
    conn.commit()
    return conn


@pytest.fixture()
def raw(tmp_path):
    """A database no migration has touched — the only way to observe an unseeded registry.

    The `db` fixture goes through `init_db`, which runs the migration runner, so 003 has always
    already fired there. That is worth having (it proves the wiring), but it means the
    unseeded state has to be built deliberately or the guard below tests nothing.
    """
    conn = sqlite3.connect(str(tmp_path / "raw.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _contact(conn, name="Jane Doe", **kw) -> str:
    row = {"job_url": "http://j/1", "full_name": name, "email": f"{name[0]}@x.com",
           "linkedin_url": f"https://l/in/{name.split()[0].lower()}"}
    row.update(kw)
    return store.upsert_contact(row, conn)


def _schema(conn) -> set:
    return {(r[0], r[1]) for r in conn.execute(
        "SELECT type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


# ── the column default does the backfill ────────────────────────────────────

def test_space_id_defaults_without_a_backfill(db):
    """Rows written by code that has never heard of Spaces still land in one.

    This is why `space_id` ships in `_ALL_COLUMNS` / `_CONTACT_COLUMNS` rather than in the
    migration: a DEFAULT is applied by the ALTER itself, so there is no window in which a row
    exists without a Space and no UPDATE that can miss whatever is inserted while it runs.
    """
    assert db.execute("SELECT space_id FROM jobs WHERE url='http://j/1'").fetchone()[0] \
        == spaces.DEFAULT_SPACE_ID

    cid = _contact(db)
    assert db.execute("SELECT space_id FROM contacts WHERE id=?", (cid,)).fetchone()[0] \
        == spaces.DEFAULT_SPACE_ID


def test_an_explicit_space_survives_an_unrelated_update(db):
    """A contact in another Space is not dragged back by a write that never mentions Spaces.

    `upsert_contact` names every column in the INSERT, so the DEFAULT never fires there and the
    value is filled in code. Doing that fill on the shared `row` dict instead would put
    'job-search' into the UPDATE path too, and any later write — a draft saved, a reply
    detected — would quietly move the contact.
    """
    cid = _contact(db, "Ada L", space_id="partnerships")
    assert db.execute("SELECT space_id FROM contacts WHERE id=?", (cid,)).fetchone()[0] \
        == "partnerships"

    store.upsert_contact({"id": cid, "job_url": "http://j/1", "notes": "called"}, db)
    assert db.execute("SELECT space_id FROM contacts WHERE id=?", (cid,)).fetchone()[0] \
        == "partnerships", "an unrelated update reset the contact's Space"


def test_a_spaceless_row_is_refused_by_the_database(db):
    """The invariant is enforced by the schema, not only by `upsert_contact` remembering.

    Mutation-found: dropping NOT NULL from either column passed the entire suite, because the
    fill in `upsert_contact` means nothing ever writes a NULL through the normal path. That
    made the constraint a claim in a comment. 15 modules still execute SQL directly (CLAUDE.md
    §Known debt) and any of them can insert a contact; the point of the constraint is the
    writer that has not been through this file.
    """
    for table, cols, values in (
        ("contacts", "(id, job_url, space_id)", ("c1", "http://j/1", None)),
        ("jobs", "(url, title, space_id)", ("http://j/2", "PM", None)),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"INSERT INTO {table} {cols} VALUES (?,?,?)", values)
        db.rollback()


# ── 003 re-keys nothing ─────────────────────────────────────────────────────

def test_every_contact_id_is_unchanged_by_003(db):
    """The migration is additive-only, so identity cannot move — asserted, not reasoned.

    `contact_id()` hashes (job_url, linkedin_url, name). `space_id` is deliberately NOT in that
    tuple: adding it would re-key every contact and orphan its touches, sequences and messages,
    which is the failure `spaces-prd.md` §Headline 5 is built around.
    """
    ids = [_contact(db, n) for n in ("Ada L", "Grace H", "Alan T")]
    touches.record_sent(ids[0], "email", conn=db)
    touches.record_sent(ids[0], "email", conn=db)
    touches.record_sent(ids[1], "linkedin", conn=db)
    before = {r[0] for r in db.execute("SELECT id FROM contacts")}
    joined = {r[0] for r in db.execute("SELECT DISTINCT contact_id FROM touches")}
    assert joined, "the fixture recorded no touches, so the join below proves nothing"

    m003_spaces.up(db)

    after = {r[0] for r in db.execute("SELECT id FROM contacts")}
    assert after == before, "003 changed a contact id"
    assert {r[0] for r in db.execute("SELECT DISTINCT contact_id FROM touches")} == joined
    # Every touch still points at a contact that exists — the join, not just the count.
    orphans = db.execute(
        "SELECT COUNT(*) FROM touches t LEFT JOIN contacts c ON c.id = t.contact_id "
        "WHERE c.id IS NULL").fetchone()[0]
    assert orphans == 0


def test_003_is_idempotent(db):
    """Three runs, identical schema and identical rows — the package's stated contract.

    This app gets killed mid-operation, so "it already ran" is a state the migration will
    actually be started in, not a hypothetical.
    """
    m003_spaces.up(db)
    schema, rows = _schema(db), db.execute("SELECT * FROM spaces").fetchall()
    for _ in range(3):
        m003_spaces.up(db)
    assert _schema(db) == schema
    assert db.execute("SELECT * FROM spaces").fetchall() == rows
    assert db.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM identities").fetchone()[0] == 1


def test_003_does_not_undo_a_rename(db):
    """The seed is INSERT OR IGNORE, never OR REPLACE.

    The operator may rename their Space (§13.2 — the name is free, the id is not). A re-run
    after that must not put "Job Search" back, and OR REPLACE is the one-character version of
    this bug.
    """
    m003_spaces.up(db)
    spaces.rename(spaces.DEFAULT_SPACE_ID, "Contract hunt", db)
    m003_spaces.up(db)
    assert spaces.get_space(spaces.DEFAULT_SPACE_ID, db)["name"] == "Contract hunt"


def test_init_db_really_runs_003(db):
    """The registry is seeded by opening the database, not by a step somebody must remember.

    `jobs_shaped_ids()` raising on an empty registry is only safe because this holds — otherwise
    the guard turns a forgotten migration into a crash on every pipeline run.
    """
    assert migrations.applied(db)[3]["status"] == "done"
    assert not migrations.pending(db)
    assert spaces.get_space(spaces.DEFAULT_SPACE_ID, db)["shape"] == spaces.JOBS_SHAPE
    assert spaces.identities(db)[0]["id"] == m003_spaces.DEFAULT_IDENTITY_ID


# ── the registry is never silently empty ────────────────────────────────────

def test_an_empty_registry_raises_rather_than_returning_nothing(raw, db):
    """§Lessons 15: a zero result must be as loud as an error.

    `jobs_shaped_ids()` gates the five pipeline stage queues. Returning `[]` would make every
    one of them select no rows, and a prepare run that quietly does nothing is byte-identical to
    a healthy one with an empty queue. That is the shape of §Lessons 44, where a partial result
    with nothing in it retired a job permanently while reporting returncode 0.

    Two different empties, because they fail for different reasons and both are reachable: the
    migration never ran, and the migration ran but the rows are gone.
    """
    with pytest.raises(spaces.RegistryEmpty):
        spaces.jobs_shaped_ids(raw)         # no `spaces` table at all

    db.execute("DELETE FROM spaces")        # table exists, nothing in it
    db.commit()
    with pytest.raises(spaces.RegistryEmpty):
        spaces.jobs_shaped_ids(db)


def test_a_targets_only_registry_still_raises(db):
    """The empty that is easiest to miss: Spaces exist, but none of them is job-shaped.

    A `len(spaces) == 0` guard passes here and every pipeline queue still selects nothing.
    """
    db.execute("UPDATE spaces SET shape = ?", (spaces.TARGETS_SHAPE,))
    db.commit()
    with pytest.raises(spaces.RegistryEmpty):
        spaces.jobs_shaped_ids(db)


def test_the_empty_registry_error_names_the_way_out(raw):
    with pytest.raises(spaces.RegistryEmpty) as exc:
        spaces.jobs_shaped_ids(raw)
    assert "migrate --status" in str(exc.value), \
        "the error says something is wrong without saying how to see what"


def test_all_spaces_tolerates_a_missing_registry(raw):
    """The asymmetry is deliberate.

    "What Spaces exist" can honestly be answered "none yet". "Which rows may the pipeline
    touch" cannot — that question has no safe empty answer.
    """
    assert spaces.all_spaces(conn=raw) == []


# ── shape is what keeps the pipeline off a target row ───────────────────────

def test_a_targets_space_is_not_job_shaped(db):
    """SPACE-1a D3, and the whole reason this function exists.

    A target lives in `jobs` with `strategy='dashboard_upload'` — the operator did paste it in —
    so `QUEUE_SQL` alone selects it and `queue_needing_detail` would hand
    `target:acme:ridgeline` to the scraper.
    """
    m003_spaces.up(db)
    spaces.create_space("partnerships", "Partnerships", "outreach",
                        spaces.TARGETS_SHAPE, conn=db)
    ids = spaces.jobs_shaped_ids(db)
    assert spaces.DEFAULT_SPACE_ID in ids
    assert "partnerships" not in ids


def test_create_space_refuses_an_unknown_shape(db):
    m003_spaces.up(db)
    with pytest.raises(ValueError):
        spaces.create_space("x", "X", "outreach", "pipeline/people", conn=db)


def test_a_space_can_be_renamed_but_never_re_keyed(db):
    """§13.2: the id is hashed into every `contact_id` in a targets Space.

    So the registry offers `rename()` and nothing else. This is a structural assertion, not a
    behavioural one — the absence of the capability is the guarantee.
    """
    m003_spaces.up(db)
    spaces.rename(spaces.DEFAULT_SPACE_ID, "Contract hunt", db)
    assert spaces.get_space(spaces.DEFAULT_SPACE_ID, db)["name"] == "Contract hunt"
    assert not [n for n in dir(spaces)
                if "id" in n and n.startswith(("rekey", "change_id", "set_id"))]


# ── the two partition keys stay disjoint ────────────────────────────────────

def test_membership_is_never_decided_by_strategy():
    """SPACE-1a D2. `strategy` means provenance; `space_id` means membership.

    Two partition keys over one table is §Lessons 49 — a rule implemented at one of its call
    sites is not implemented, and last time that left 8 known Google connections unsearched.
    Checked against the source because the drift this guards against is a future edit, not a
    value in the database today.
    """
    import ast
    import pathlib

    import applypilot
    src = (pathlib.Path(applypilot.__file__).parent / "repo" / "spaces.py").read_text("utf-8")
    tree = ast.parse(src)
    # Strip comments and docstrings — this module EXPLAINS the distinction in prose, and a
    # grep over the raw text would only ever find the explanation. §Lessons 48: grep proves
    # where a string is, not what the code does.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body[0].value.value = ""
    code = ast.unparse(tree).lower()
    assert "strategy" not in code, \
        "the Space registry is consulting `strategy`; membership is space_id's job alone"


def test_the_default_space_id_has_one_definition():
    """It is on 25 jobs and 196 contacts, and inside every targets `contact_id`.

    Two spellings of it is two defaults — the shape of the `OUTREACH_ATTACH_DECK` bug, where
    `_intro_deck_path` defaulted it "1" while `settings.py` declared False and
    `doctor --config` reported it off for the whole time it was on.
    """
    assert m003_spaces.DEFAULT_SPACE_ID == spaces.DEFAULT_SPACE_ID
    assert store._spaces.DEFAULT_SPACE_ID == spaces.DEFAULT_SPACE_ID
    assert database._ALL_COLUMNS["space_id"].endswith(f"DEFAULT '{spaces.DEFAULT_SPACE_ID}'")
    assert store._CONTACT_COLUMNS["space_id"].endswith(f"DEFAULT '{spaces.DEFAULT_SPACE_ID}'")


def test_a_plain_connection_still_works(tmp_path):
    """Migrations receive whatever connection the runner has, including a bare sqlite3 one."""
    conn = sqlite3.connect(str(tmp_path / "p.db"))
    conn.row_factory = sqlite3.Row
    out = m003_spaces.up(conn)
    assert out["spaces"] == 1 and out["identities"] == 1
