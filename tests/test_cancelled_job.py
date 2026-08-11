""""Job removed / cancelled" — the other way a job leaves the pipeline.

Rejected and cancelled are the same TERMINAL STATE and different FACTS:

    rejected    they read it and said no. An outcome, and it belongs in the funnel.
    cancelled   the posting stopped existing — req pulled, hiring freeze, filled internally.
                Nothing was decided about you, and counting it as a rejection makes every
                number about your rejection rate slightly false.

They share `rejected_at`, which means "when this left the pipeline" and is what sinks the row and
withholds a temperature reading. `apply_status` carries the why. Renaming the column to say that
would cost a migration to express what the pair already expresses (the `job_url` → `anchor`
rename is deferred on the same grounds).

The real risk was not the new state, it was the EIGHT places the frontend checked
`status === 'rejected'` to mean "closed". Adding a second magic string beside each one is
§Lessons 49 with a guarantee of missing one, and the miss would be silent: a cancelled job still
being offered follow-ups looks exactly like a working row.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.repo import jobs as repo


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    conn.execute("INSERT INTO jobs (url, title, company, site, strategy, applied_at, space_id) "
                 "VALUES (?,?,?,?,?,?,?)",
                 ("http://j/1", "Engineer", "Acme", "Greenhouse", "dashboard_upload",
                  "2026-08-01T00:00:00+00:00", "job-search"))
    conn.commit()
    return conn


def _row(conn):
    r = conn.execute("SELECT apply_status, rejected_at, applied_at FROM jobs "
                     "WHERE url = 'http://j/1'").fetchone()
    return dict(zip(r.keys(), r))


# ── the two closures ────────────────────────────────────────────────────────

def test_cancelling_is_its_own_status(db):
    repo.mark_rejected("http://j/1", db, status="cancelled")
    assert _row(db)["apply_status"] == "cancelled"


def test_rejecting_still_works_unchanged(db):
    """The default is `rejected`, so every existing caller behaves exactly as before."""
    repo.mark_rejected("http://j/1", db)
    assert _row(db)["apply_status"] == "rejected"


def test_both_stamp_when_the_job_left(db):
    """One timestamp for "left the pipeline". The ORDER BY that sinks closed rows and the
    temperature band that refuses to rate them both read it, and neither cares why."""
    repo.mark_rejected("http://j/1", db, status="cancelled")
    assert _row(db)["rejected_at"]


def test_both_keep_the_record_that_you_applied(db):
    """`applied_at` survives. A cancelled requisition does not un-happen the application, and
    losing it would quietly reduce the "Applied" number in the funnel."""
    repo.mark_rejected("http://j/1", db, status="cancelled")
    assert _row(db)["applied_at"] == "2026-08-01T00:00:00+00:00"


def test_an_unknown_status_is_refused(db):
    """The column is a free-text field; a typo would create a state nothing renders and nothing
    filters, and the row would simply vanish from every bucket."""
    with pytest.raises(ValueError):
        repo.mark_rejected("http://j/1", db, status="withdrawn")


def test_the_handler_refuses_it_too(db):
    """The endpoint takes the status from the POST body, so the guard cannot live only in the
    repo — this is reachable without the UI."""
    assert wd._mark_rejected("http://j/1", "nonsense")["ok"] is False


def test_restoring_works_from_either(db):
    """One Restore for both, because undoing them is the same operation."""
    for status in ("rejected", "cancelled"):
        repo.mark_rejected("http://j/1", db, status=status)
        repo.unmark_rejected("http://j/1", "applied", db)
        assert _row(db)["rejected_at"] is None
        assert _row(db)["apply_status"] == "applied"


# ── the messages say which ──────────────────────────────────────────────────

def test_cancelling_says_it_is_not_a_rejection(db):
    """The two menu items are one row apart and only one belongs in a funnel. If the
    confirmation and the log line do not say which happened, the distinction exists only in the
    database."""
    res = wd._mark_rejected("http://j/1", "cancelled")
    assert res["ok"]
    # The confirmation the operator reads has to name the consequence that differs. This line
    # originally ended in `or True`, which §Lessons 13 names by name as a vacuous assertion this
    # repo has already shipped once — it passed against any message at all, including the
    # rejection one.
    assert "rejection rate" in res["message"]
    assert "Not a rejection" in wd._CLOSED_COPY["cancelled"][0]


def test_the_two_messages_differ(db):
    """Guard the guard: one shared message would make the states indistinguishable everywhere the
    operator actually looks."""
    assert wd._CLOSED_COPY["rejected"] != wd._CLOSED_COPY["cancelled"]


# ── it sorts like a closed job ──────────────────────────────────────────────

def test_a_cancelled_job_sinks_like_a_rejected_one(db):
    """The miss this test exists for was real: the ORDER BY named only 'rejected', so a cancelled
    job sorted with LIVE work — above jobs still being prepared. Found by sweeping for the string
    rather than by the UI, because nothing about the row would have looked wrong."""
    db.execute("INSERT INTO jobs (url, title, site, strategy, space_id) VALUES (?,?,?,?,?)",
               ("http://j/2", "Live one", "Greenhouse", "dashboard_upload", "job-search"))
    db.commit()
    repo.mark_rejected("http://j/1", db, status="cancelled")
    urls = [r["url"] for r in repo.dashboard_rows(conn=db, space_id="job-search")]
    assert urls.index("http://j/2") < urls.index("http://j/1"), (
        "a cancelled job is sorting above live work")


# ── the frontend has ONE predicate ──────────────────────────────────────────

def _js() -> str:
    from pathlib import Path
    return (Path(wd.__file__).parent / "static" / "dashboard.js").read_text()


def test_the_terminal_check_is_one_function():
    """Eight places asked `status === 'rejected'` to mean "closed". A second magic string beside
    each is a guaranteed miss, and a silent one."""
    js = _js()
    assert "function isClosed(j)" in js
    body = js[js.index("function jobInBucket"):]
    assert "status === 'rejected'" not in body.replace(
        "j.status === 'cancelled' ? 'Cancelled' : 'Rejected'", ""), (
        "a raw rejected check is back below the predicate")


def test_the_menu_offers_it_and_says_what_it_is_not():
    js = _js()
    assert "markCancelled" in js
    assert "Job removed / cancelled" in js
    assert "not a rejection" in js.lower()


def test_it_has_its_own_badge_and_filter():
    js = _js()
    assert "cancelled:       { icon: '⊘'" in js
    assert "'cancelled'" in js[js.index("JOB_FILTER_ORDER"):js.index("JOB_FILTER_ORDER") + 120]


def test_restore_is_offered_for_both():
    """A cancelled job that could not be reopened would be a delete with extra steps."""
    js = _js()
    menu = js[js.index("function rowMenu"):]
    menu = menu[:menu.index("return `<details")]
    assert "isClosed(j)" in menu
    assert "unmarkRejected" in menu


# ── and the predicate is EXECUTED, not grepped ──────────────────────────────

def test_isClosed_really_covers_both(tmp_path):
    """The grep tests above all survived a mutation that made `isClosed` know only 'rejected'.

    They assert the string is present, which is §Lessons 48 — grep proves where a string is, not
    what the code does. `dueByChannel` and the row menu both branch on this, so a predicate that
    quietly dropped 'cancelled' would leave cancelled jobs in the 🔔 counter and offering
    follow-ups, and every test in this file would still be green.

    Uses the SHARED browser stub. Six files hand-rolled their own and drifted until 47 tests
    failed at once (`tests/browser_stubs.py`); a seventh copy is the same bet.
    """
    import json
    import subprocess

    from applypilot import web_dashboard as wd

    from browser_stubs import BROWSER_GLOBALS

    src = (wd._STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
    script = tmp_path / "closed.mjs"
    script.write_text(
        BROWSER_GLOBALS + """
const el = () => ({ innerHTML:'', textContent:'', hidden:false, value:'', style:{},
  closest:()=>el(), querySelector:()=>el(), querySelectorAll:()=>[], setAttribute(){},
  getAttribute:()=>null, addEventListener(){}, appendChild(){}, classList:{add(){},remove(){},
  toggle(){}}, dataset:{} });
globalThis.document = { getElementById:()=>el(), querySelector:()=>el(),
  querySelectorAll:()=>[], addEventListener(){}, body:el(), hasFocus:()=>false,
  activeElement:null };
const SRC = """ + json.dumps(src) + """;
const F = (new Function(SRC + '; return { isClosed };'))();
console.log(JSON.stringify({
  rejected: F.isClosed({status:'rejected'}),
  cancelled: F.isClosed({status:'cancelled'}),
  applied: F.isClosed({status:'applied'}),
  nothing: F.isClosed(null),
}));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["cancelled"] is True, "isClosed does not treat a cancelled job as closed"
    assert out["rejected"] is True
    # Guard the guard: returning True for everything would satisfy the two above while silently
    # removing every live job from the counter.
    assert out["applied"] is False
    assert out["nothing"] is False
