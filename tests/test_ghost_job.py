"""👻 Ghost job — the third way out, and the live bug that finding it exposed.

Three ways a job leaves the pipeline without an interview, sharing a TIMESTAMP and differing in
REASON:

    rejected    they read it and said no. An outcome, and it belongs in the funnel.
    cancelled   the posting stopped existing — req pulled, freeze, filled internally. Bad luck.
    ghost       the opening was never real. An evergreen requisition, a role reposted every few
                weeks, a listing kept up so the company looks like it is growing.

`ghost` is its own state rather than a flavour of `cancelled`, and the distinction is the only
thing it can teach: cancelled means something real STOPPED, ghost means it never started. One is
luck, the other is a SOURCE — a board or an employer worth avoiding next time. Folded together,
that is lost.

## The bug this uncovered

Adding a third state meant sweeping for the second, and the sweep found `_status_payload`:

    if apply_status == "rejected":
        status = "rejected"

`cancelled` was never in it. So for the whole time that state has existed, a cancelled job that
had been applied to came over the wire as **`applied`**, and one that had not came over as
**`imported`** — no ⊘ badge, absent from its own filter, and `isClosed()` false, which left it in
the 🔔 counter still being offered follow-ups. One such row was live in the operator's database.

`test_cancelled_job.py` has fifteen tests and none of them caught it: they check the repo layer or
grep the JS, and the one test that EXECUTES the frontend feeds `{status:'cancelled'}` by hand — a
value the server never actually emitted. §Lessons 47's exact shape, where the write is perfect and
the read drops it in silence.

So the payload assertions below are the point of this file, not the ghost state itself.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import applypilot.database as database
from applypilot import web_dashboard as wd
from applypilot.repo import jobs as repo

from browser_stubs import BROWSER_GLOBALS


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
    # A second row that was never applied to — you can spot a ghost listing without applying,
    # and that path fell through to `imported` rather than to the closed pile.
    conn.execute("INSERT INTO jobs (url, title, site, strategy, space_id) VALUES (?,?,?,?,?)",
                 ("http://j/2", "Never applied", "Greenhouse", "dashboard_upload", "job-search"))
    conn.commit()
    return conn


def _row(conn, url="http://j/1"):
    r = conn.execute("SELECT apply_status, rejected_at, applied_at FROM jobs WHERE url = ?",
                     (url,)).fetchone()
    return dict(zip(r.keys(), r))


# ── the state ───────────────────────────────────────────────────────────────

def test_ghost_is_its_own_status(db):
    repo.mark_rejected("http://j/1", db, status="ghost")
    assert _row(db)["apply_status"] == "ghost"


def test_it_stamps_when_the_job_left(db):
    """One timestamp for "left the pipeline", read by the ORDER BY that sinks closed rows and by
    the temperature band that refuses to rate them. Neither cares why."""
    repo.mark_rejected("http://j/1", db, status="ghost")
    assert _row(db)["rejected_at"]


def test_it_keeps_the_record_that_you_applied(db):
    """A listing being fake does not un-happen the application, and losing `applied_at` would
    quietly shrink the Applied number in the funnel."""
    repo.mark_rejected("http://j/1", db, status="ghost")
    assert _row(db)["applied_at"] == "2026-08-01T00:00:00+00:00"


def test_it_is_restorable_like_the_others(db):
    repo.mark_rejected("http://j/1", db, status="ghost")
    repo.unmark_rejected("http://j/1", "applied", db)
    assert _row(db)["rejected_at"] is None


# ── the payload, which is where the real bug was ────────────────────────────

def _wire(conn, url):
    return [j for j in wd._status_payload("job-search")["jobs"] if j["url"] == url][0]["status"]


@pytest.mark.parametrize("status", ["rejected", "cancelled", "ghost"])
@pytest.mark.parametrize("url", ["http://j/1", "http://j/2"])
def test_every_closed_state_reaches_the_browser_as_itself(db, status, url):
    """The regression that mattered. `_status_payload` named only 'rejected', so a cancelled job
    arrived as `applied` (if it had been) or `imported` (if it had not) — no badge, no filter, and
    `isClosed()` false, which kept it in the counter and still owing follow-ups.

    Parametrised over BOTH rows on purpose: applied and never-applied fell through to different
    wrong answers, so a fixture with only one would have half-passed.
    """
    repo.mark_rejected(url, db, status=status)
    assert _wire(db, url) == status


def test_the_precedence_is_read_from_the_tuple_not_typed_out(db):
    """A fourth closed state must not need this line edited. Both misses so far — the ORDER BY
    and the payload — were hand-written lists that fell behind the tuple (§Lessons 49)."""
    import inspect
    src = inspect.getsource(wd._status_payload)
    assert "CLOSED_STATUSES" in src
    assert 'apply_status == "rejected"' not in src


def test_a_closed_job_outranks_applied(db):
    """Closing something you already applied to must show as closed, not as applied. This is the
    precedence that was inverted for `cancelled` for its whole existence."""
    repo.mark_rejected("http://j/1", db, status="ghost")
    assert _row(db)["applied_at"], "fixture must have an applied job for this to mean anything"
    assert _wire(db, "http://j/1") == "ghost"


# ── it is refused when misspelled, and it sinks ─────────────────────────────

def test_an_unknown_status_is_still_refused(db):
    with pytest.raises(ValueError):
        repo.mark_rejected("http://j/1", db, status="ghosted")
    assert wd._mark_rejected("http://j/1", "ghosted")["ok"] is False


def test_a_ghost_job_sinks_like_the_other_closed_states(db):
    """The ORDER BY is generated from CLOSED_STATUSES now. It previously named the states by
    hand, and naming only 'rejected' had left cancelled jobs sorting above live work."""
    repo.mark_rejected("http://j/1", db, status="ghost")
    urls = [r["url"] for r in repo.dashboard_rows(conn=db, space_id="job-search")]
    assert urls.index("http://j/2") < urls.index("http://j/1"), "a ghost job sorts above live work"


# ── the copy says which of the three it is ──────────────────────────────────

def test_the_message_says_it_is_not_a_rejection(db):
    res = wd._mark_rejected("http://j/1", "ghost")
    assert res["ok"]
    assert "rejection rate" in res["message"]
    assert "Not a rejection" in wd._CLOSED_COPY["ghost"][0]


def test_all_three_messages_differ(db):
    """One shared message makes the states indistinguishable everywhere the operator looks, which
    is the only place the difference exists for them."""
    msgs = [wd._CLOSED_COPY[s] for s in ("rejected", "cancelled", "ghost")]
    assert len(set(msgs)) == 3


def test_the_copy_covers_every_closed_status(db):
    """`_CLOSED_COPY[status]` is indexed directly, so a state without an entry is a KeyError at
    the moment the operator clicks it."""
    assert set(wd._CLOSED_COPY) == set(repo.CLOSED_STATUSES)


# ── the frontend ────────────────────────────────────────────────────────────

def _js() -> str:
    return (Path(wd.__file__).parent / "static" / "dashboard.js").read_text(encoding="utf-8")


def test_it_has_a_badge_a_filter_and_a_menu_item():
    js = _js()
    assert "ghost:           { icon: '👻'" in js
    order = js[js.index("JOB_FILTER_ORDER"):]
    assert "'ghost'" in order[:140]
    assert "markGhost" in js


def test_the_menu_says_what_a_ghost_job_IS():
    """Two menu items one row apart both close a job without it being a rejection. If the wording
    does not separate them the operator picks whichever is on top."""
    js = _js()
    assert "Never a real opening" in js
    assert "evergreen" in js.lower()


def test_it_is_visually_distinct_from_cancelled():
    """They sit next to each other in the filter strip, and a grey pill beside a grey pill is one
    pill — §Lessons 43, where the won row was greyed 2.7% against white and the click 'did
    nothing'."""
    css = (Path(wd.__file__).parent / "static" / "dashboard.css").read_text(encoding="utf-8")
    assert ".st-ghost" in css
    ghost = css[css.index(".st-ghost"):css.index(".st-ghost") + 120]
    cancelled = css[css.index(".st-cancelled"):css.index(".st-cancelled") + 120]
    assert ghost.split("background:")[1][:8] != cancelled.split("background:")[1][:8]


# ── and the predicate is EXECUTED ───────────────────────────────────────────

def test_isClosed_and_the_label_cover_all_three(tmp_path):
    """Grep proves where a string is, not what the code does (§Lessons 48). `isClosed` drives the
    row menu and the 🔔 counter, so a predicate that quietly dropped 'ghost' would leave ghost
    jobs owing follow-ups with every grep test above still green.

    `closedLabel` is here too because it replaced a two-way ternary that would have printed
    "Rejected" above a ghost job's date — the same silent miss one line lower.
    """
    src = _js()
    script = tmp_path / "ghost.mjs"
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
const F = (new Function(SRC + '; return { isClosed, closedLabel, jobInBucket };'))();
console.log(JSON.stringify({
  ghost: F.isClosed({status:'ghost'}),
  cancelled: F.isClosed({status:'cancelled'}),
  rejected: F.isClosed({status:'rejected'}),
  applied: F.isClosed({status:'applied'}),
  label: F.closedLabel('ghost'),
  cancelLabel: F.closedLabel('cancelled'),
  inBucket: F.jobInBucket({status:'ghost'}, 'ghost'),
  notInCancelled: F.jobInBucket({status:'ghost'}, 'cancelled'),
  cancelledNotInGhost: F.jobInBucket({status:'cancelled'}, 'ghost'),
  rejectedNotInGhost: F.jobInBucket({status:'rejected'}, 'ghost'),
}));
""", encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["ghost"] is True, "isClosed does not treat a ghost job as closed"
    assert out["cancelled"] is True and out["rejected"] is True
    # Guard the guard: True for everything satisfies the three above and removes every live job
    # from the counter.
    assert out["applied"] is False
    assert out["label"] == "Ghost job", "a ghost job's date line would read 'Rejected'"
    assert out["cancelLabel"] == "Cancelled"
    # Its own bucket, and the separation has to hold in BOTH directions. Checking only that a
    # ghost job stays out of Cancelled survived a mutation that put `cancelled` INSIDE the ghost
    # bucket — every cancelled job would then appear under both pills, which is exactly the
    # conflation this state exists to prevent, and the counts would no longer sum to the table.
    assert out["inBucket"] is True
    assert out["notInCancelled"] is False
    assert out["cancelledNotInGhost"] is False, "cancelled jobs are showing under Ghost jobs"
    assert out["rejectedNotInGhost"] is False
