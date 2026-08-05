"""SPACE-1: a Space is a manifest, and the pipeline stages read it.

`test_adding_a_space_needs_no_schema_change` is this document's central claim executed rather
than asserted in prose — the same move as `test_adding_a_channel_needs_no_schema_change`, which
is why SMS really did cost one column and which caught the one line where that claim was false
(`followup_panel` spelled out `buckets[EMAIL.name], buckets[LINKEDIN.name]` by hand, so a third
channel passed through the entire engine correctly and then vanished at the return statement).

**The fake Space is named something that will never ship.** The channel version of this test
originally named SMS, and shipping SMS silently broke its arithmetic: the fake channel started
resolving real settings, so `default_schedule=(24, 72)` quietly became `[72, 168]`. A test
proving "an unknown Space works" has to name one that stays unknown. `spaces-prd.md` names
job-search, partnerships and acme; this one names lighthouse tenders, and it is deliberately
absurd so nobody is tempted to make it real.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot import migrations
from applypilot.domain import space as sp
from applypilot.networking import store, touches
from applypilot.repo import jobs as repo
from applypilot.repo import spaces

#: Never going to be a real Space here. See the module docstring.
UNKNOWN = "lighthouse-tenders"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    touches.init_touches(conn)
    return conn


def _row(conn, url, space_id=spaces.DEFAULT_SPACE_ID, **kw):
    cols = {"url": url, "title": "PM", "site": "Greenhouse", "strategy": "dashboard_upload",
            "space_id": space_id, "full_description": "a real posting, long enough to matter"}
    cols.update(kw)
    conn.execute(f"INSERT INTO jobs ({', '.join(cols)}) "
                 f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))
    conn.commit()
    return url


def _schema(conn) -> set:
    return {(r[0], r[1]) for r in conn.execute(
        "SELECT type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


# ── the falsifier ───────────────────────────────────────────────────────────

def test_adding_a_space_needs_no_schema_change(db):
    """Define a Space this codebase has never heard of and drive it end to end.

    If this ever needs a migration, a column, or an `if space.id == ...`, the thesis of
    `spaces-prd.md` is wrong and the document should say so rather than absorbing the cost.
    """
    schema_before = _schema(db)
    version_before = migrations.current_version(db)

    space = spaces.create_space(UNKNOWN, "Lighthouse tenders", "outreach", conn=db)
    assert space.shape == sp.TARGETS_SHAPE          # from the template, not from an argument
    assert space.terminal == "booked"
    assert space.tailor_docs is False

    # A row in it, anchored the way a target is — no URL to scrape, because there is no posting.
    anchor = f"target:{UNKNOWN}:trinity-house"
    _row(db, anchor, space_id=UNKNOWN, title="Trinity House", detail_scraped_at=None)
    cid = store.upsert_contact(
        {"job_url": anchor, "space_id": UNKNOWN, "full_name": "Ada L",
         "email": "ada@trinityhouse.test", "linkedin_url": "https://l/in/ada"}, db)
    touches.record_sent(cid, "email", conn=db)

    # The whole point: the six-stage job pipeline does not touch it.
    assert anchor not in [r["url"] for r in repo.queue_needing_detail(conn=db)], \
        "a target row reached the scraper — §Lessons 44 is what happens next"
    assert anchor not in [r["url"] for r in repo.queue_for_tailor(conn=db)]
    assert anchor not in [r["url"] for r in repo.queue_for_cover(conn=db)]
    assert anchor not in [r["url"] for r in repo.queue_for_apply(10, 5, conn=db)]
    repo.bypass_scoring(db)
    assert db.execute("SELECT fit_score FROM jobs WHERE url=?", (anchor,)).fetchone()[0] is None

    # ...while everything the Space DOES share still works, unmodified.
    assert store.get_contact(cid, db)["space_id"] == UNKNOWN
    assert touches.ladder_state(cid, "email", conn=db)["count"] == 1
    assert spaces.load(UNKNOWN, db) == space           # manifest survives the round trip

    assert _schema(db) == schema_before, "adding a Space changed the schema"
    assert migrations.current_version(db) == version_before, "adding a Space ran a migration"
    assert not migrations.pending(db)


def test_the_unknown_space_really_is_unknown():
    """Guards the guard. If this name ever becomes real, the test above stops testing anything.

    This is the SMS lesson: the fake channel was fine until the real one shipped, and then it
    silently started resolving real settings while still passing.
    """
    import pathlib

    import applypilot
    root = pathlib.Path(applypilot.__file__).parent
    hits = [str(p.relative_to(root)) for p in root.rglob("*.py")
            if UNKNOWN in p.read_text(encoding="utf-8")]
    assert not hits, f"{UNKNOWN!r} now exists in the codebase: {hits}"
    assert UNKNOWN not in {t for t in sp.TEMPLATES}


# ── the stage gate, on its own ──────────────────────────────────────────────

def test_a_jobs_space_can_turn_documents_off_without_leaving_the_pipeline(db):
    """`makes_documents` is narrower than `runs_job_pipeline`, and the difference is real.

    Collapsing the two would make "apply to postings with a fixed résumé" unexpressible, which
    is a legitimate configuration and the reason these are two calls rather than one.
    """
    spaces.create_space("fixed-cv", "Fixed CV", "jobs", conn=db, tailor_docs=False)
    url = _row(db, "http://j/plain", space_id="fixed-cv", detail_scraped_at=None)

    assert url in [r["url"] for r in repo.queue_needing_detail(conn=db)]
    assert url not in [r["url"] for r in repo.queue_for_tailor(conn=db)]
    assert "fixed-cv" in spaces.jobs_shaped_ids(db)
    assert "fixed-cv" not in spaces.document_making_ids(db)


def test_the_default_space_still_flows_through_every_stage(db):
    """The negative controls above prove nothing unless the positive case still works.

    Five vacuous tests shipped in one session of this project (§Lessons 13); a filter that
    excludes everything passes every "is it excluded?" assertion.
    """
    url = _row(db, "http://j/real", detail_scraped_at=None)
    assert url in [r["url"] for r in repo.queue_needing_detail(conn=db)]

    db.execute("UPDATE jobs SET detail_scraped_at='now' WHERE url=?", (url,))
    db.commit()
    assert url in [r["url"] for r in repo.queue_for_tailor(conn=db)]

    db.execute("UPDATE jobs SET tailored_resume_path='/tmp/r.txt' WHERE url=?", (url,))
    db.commit()
    assert url in [r["url"] for r in repo.queue_for_cover(conn=db)]
    assert url in [r["url"] for r in repo.queue_for_apply(10, 5, conn=db)]
    assert repo.bypass_scoring(db) == 1


def test_a_stage_queue_refuses_an_empty_registry(db):
    """The guard reaches the callers, not just the helper it lives in.

    `jobs_shaped_ids()` raising is only useful if the queues actually go through it — testing
    the helper alone would pass with every call site removed.
    """
    _row(db, "http://j/orphan", detail_scraped_at=None)
    db.execute("DELETE FROM spaces")
    db.commit()
    for call in (lambda: repo.queue_needing_detail(conn=db),
                 lambda: repo.queue_for_tailor(conn=db),
                 lambda: repo.queue_for_cover(conn=db),
                 lambda: repo.queue_for_apply(10, 5, conn=db),
                 lambda: repo.bypass_scoring(db)):
        with pytest.raises(spaces.RegistryEmpty):
            call()


# ── the manifest itself (pure) ──────────────────────────────────────────────

def test_a_manifest_round_trips_through_its_json_blob():
    s = sp.from_template("x", "X", "outreach", tone="Dry.", offer="We build agents.",
                         channels=("email",))
    back = sp.Space.from_row({"id": "x", "name": "X", "template": "outreach",
                              "shape": sp.TARGETS_SHAPE, "identity_id": "personal",
                              "config": s.config_json()})
    assert back == s


def test_only_non_default_values_are_stored():
    """A blob holding every field freezes today's defaults into every Space ever created.

    Changing a default later would then change nothing for anyone — the same trap as recording
    `draft_variant` as a version number instead of its inputs.
    """
    import json
    plain = sp.from_template("x", "X", "jobs")
    assert json.loads(plain.config_json()) == {}
    assert "tone" not in plain.config_json()


def test_a_corrupt_config_blob_yields_defaults_not_an_error():
    """The columns identify a Space; the blob is preference.

    Refusing to load would take a whole panel down for a hand-edit, and the defaults are the
    documented behaviour anyway.
    """
    for bad in ("{not json", "", None, "[1,2,3]"):
        s = sp.Space.from_row({"id": "x", "name": "X", "template": "jobs",
                               "shape": sp.JOBS_SHAPE, "config": bad})
        assert s.tailor_docs is True and s.terminal == "interview"


@pytest.mark.parametrize("bad", [
    {"shape": "pipeline/people"},
    {"template": "recruiting"},
    {"terminal": "hired"},
    {"id": ""},
])
def test_a_manifest_that_cannot_mean_anything_is_refused(bad):
    """Fail where the bad value is written, not where it silently excludes rows.

    A Space with an unknown shape is not degraded — `jobs_shaped_ids()` drops it from every
    queue and its rows sit in a panel that never processes them, which renders exactly like a
    healthy Space (§Lessons 44).
    """
    good = {"id": "x", "name": "X", "template": "jobs", "shape": sp.JOBS_SHAPE}
    with pytest.raises(ValueError):
        sp.Space(**{**good, **bad})


@pytest.mark.parametrize("frozen", ["id", "shape"])
def test_the_id_and_shape_cannot_be_changed_after_creation(frozen):
    """§13.2. The id is hashed into every `contact_id` in a targets Space.

    Refused in the domain object rather than only in the UI, so a future endpoint cannot
    reintroduce it by forgetting (§Lessons 49).
    """
    s = sp.from_template("x", "X", "outreach")
    with pytest.raises(ValueError):
        s.with_(**{frozen: "something-else"})
    assert s.with_(name="Renamed").name == "Renamed"      # the editable half still works


def test_save_cannot_move_a_space_id_or_shape(db):
    """The same guarantee at the persistence end — both ends, not whichever is remembered."""
    spaces.create_space("x", "X", "outreach", conn=db)
    hand_built = sp.Space(id="x", name="X", template="outreach", shape=sp.JOBS_SHAPE)
    spaces.save(hand_built, db)
    assert spaces.load("x", db).shape == sp.TARGETS_SHAPE, \
        "save() wrote a shape; it must not be in the UPDATE at all"


def test_business_differs_from_outreach_by_identity_and_wording_alone():
    """`spaces-prd.md` §Headline 2 and SPACE-6's falsifier, as far as it can be checked now.

    If these two templates ever diverge structurally, the claim that the third template is a
    config row rather than a build has already failed.
    """
    a = sp.TEMPLATE_DEFAULTS["outreach"]
    b = sp.TEMPLATE_DEFAULTS["business"]
    assert {k: v for k, v in a.items() if k != "tone"} == {k: v for k, v in b.items() if k != "tone"}


def test_unapplied_fields_are_really_unapplied():
    """The declared-but-inert list is honest, and can only shrink.

    A field that is accepted but never read is a lie the caller cannot see — §Lessons 39, where
    `conversation_transcript` took a `thread` it barely used and the resulting draft could
    restate but never continue. Wiring one of these is what removes it from `UNAPPLIED`.
    """
    import pathlib

    import applypilot
    root = pathlib.Path(applypilot.__file__).parent
    owners = {"domain/space.py", "repo/spaces.py"}
    for name in sp.UNAPPLIED:
        assert name in {f.name for f in __import__("dataclasses").fields(sp.Space)}, \
            f"{name} is listed as unapplied but is not a field"
        readers = sorted(
            str(p.relative_to(root)) for p in root.rglob("*.py")
            if str(p.relative_to(root)) not in owners
            and f"space.{name}" in p.read_text(encoding="utf-8"))
        assert not readers, (
            f"`{name}` is read by {readers} but is still listed in UNAPPLIED — "
            f"remove it from that tuple")
