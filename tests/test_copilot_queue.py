"""A co-pilot review must never be closed by the next queued application.

2026-07-29, on a real run. A Zello application was filled correctly in 78 seconds and handed
over: the agent even said "Leaving the browser open on the completed application — do not
click Submit." The next queued job started 428 MILLISECONDS later and killed that browser,
because launching an apply clears whatever holds the CDP port.

    21:24:30.724  Zello    -> ready_to_submit, Chrome kept alive for review
    21:24:31.152  Deloitte -> in_progress          <- 428ms later, same port

The row still read `ready_to_submit`, so the status claimed a form was waiting that no longer
existed. Nothing errored. Batching N jobs in co-pilot mode leaves every one un-reviewable
except the last, and the filled work is unrecoverable — the form dies with the browser.

The rule these tests enforce was originally written as "co-pilot is inherently one-at-a-time",
and that was the correct fix for the architecture at the time: every dashboard apply ran on
worker 0, so there was exactly ONE browser and a second launch always destroyed it.

**The constraint was never one job, it was one job per BROWSER** (2026-08-12). Each worker has
its own CDP port (`BASE_CDP_PORT + worker_id`) and its own Chrome profile, so `APPLY_WORKERS`
applications can be filled at once and each waits for the human independently. What must still
never happen is the thing that cost two applications: a slot being handed a second job while it
is holding a filled form. So these tests now enforce

  - no slot is ever given two jobs while it holds a browser,
  - no two concurrent jobs share a slot,
  - a full house still refuses to start,
  - a job that needs no browser (applied, failed) releases its slot for the next one,

which is the same safety property, stated per slot instead of per run.
"""

from __future__ import annotations

import pytest

import applypilot.database as database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    # The path is kept so `wd` can hand out a THREAD-LOCAL connection rather than this one
    # object. Applies now run in parallel threads, and sqlite refuses a connection created in
    # another thread — a fixture that returns one fixed object would make every test fail with
    # a database error while production, which is thread-local by design, worked fine.
    conn._test_path = path
    return conn



def _write(db, sql, *params):
    """Write as the apply subprocess would: through THIS thread's own connection.

    In production each apply is a separate process with its own sqlite handle. A fake that
    reuses the main thread's connection raises "SQLite objects created in a thread can only be
    used in that same thread" and every parallel test fails for a reason that has nothing to do
    with what it is testing.
    """
    conn = database.get_connection(db._test_path)
    conn.execute(sql, params)
    conn.commit()


def _job(conn, url, title, **cols):
    base = {"site": title, "strategy": "dashboard_upload", "tailored_resume_path": "/tmp/r.pdf",
            "discovered_at": "2026-07-29T10:00:00+00:00"}
    base.update(cols)
    keys = ", ".join(["url", "title", *base])
    marks = ", ".join("?" for _ in range(len(base) + 2))
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", (url, title, *base.values()))
    conn.commit()


@pytest.fixture()
def wd(db, monkeypatch):
    from applypilot import web_dashboard
    # Thread-local, exactly like production: each apply worker thread gets its own connection
    # to the same file, and the main thread keeps the one the test asserts against.
    monkeypatch.setattr(web_dashboard, "get_connection",
                        lambda *a, **k: database.get_connection(db._test_path))
    monkeypatch.setattr(web_dashboard, "init_db", lambda *a, **k: db)
    monkeypatch.setattr(web_dashboard.config, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(web_dashboard.config, "ensure_dirs", lambda *a, **k: None)
    return web_dashboard


def _never_runs(*a, **k):
    raise AssertionError("an apply subprocess was launched when it must not have been")


@pytest.fixture()
def live_review_browser(monkeypatch):
    """Pretend the co-pilot review window is still open.

    The guard blocks on a pending review only while its browser actually exists — a row alone
    is not enough, because `apply_status` outlives the process that set it (see
    test_copilot_stale_reviews.py). These tests are about a REAL open review, so they have to
    say so; without this they would pass for the wrong reason on a machine with no Chrome.
    """
    from applypilot.apply import chrome
    monkeypatch.setattr(chrome, "chrome_alive_on_port", lambda *a, **k: True)


def test_a_batch_will_not_start_while_a_review_is_open(wd, db, monkeypatch, live_review_browser):
    """The exact loss: an open review browser is invisible to `queue_for_apply`, which
    filters on the JOB, not on whether a browser is in use."""
    _job(db, "http://j/zello", "Zello", apply_status="ready_to_submit")
    _job(db, "http://j/next", "NextCo")
    monkeypatch.setattr(wd.subprocess, "run", _never_runs)

    res = wd.run_dashboard_apply(limit=10, copilot=True)
    assert res["queued"] == 0
    assert "waiting for you" in res["blocked"]
    assert "Zello" in res["blocked"], "the operator needs to know WHICH review is open"


def test_a_needs_human_blocker_also_blocks(wd, db, monkeypatch, live_review_browser):
    """`needs_human` (captcha/login/registration wall) is also an open browser."""
    _job(db, "http://j/arm", "Arm", apply_status="needs_human", apply_error="login")
    _job(db, "http://j/next", "NextCo")
    monkeypatch.setattr(wd.subprocess, "run", _never_runs)
    assert wd.run_dashboard_apply(limit=10, copilot=True)["queued"] == 0


class Done:
    """A finished subprocess. `capture_output=True` means stdout/stderr are read."""
    returncode = 0
    stdout = ""
    stderr = ""


def _handoff_runner(db, launched, lock=None):
    """A fake apply that does what the real co-pilot agent does: fill, then hand over."""
    import threading
    lock = lock or threading.Lock()

    def fake_run(args, **kw):
        url = args[args.index("--url") + 1]
        slot = int(args[args.index("--worker-id") + 1]) if "--worker-id" in args else 0
        with lock:
            launched.append((url, slot))
        _write(db, "UPDATE jobs SET apply_status='ready_to_submit' WHERE url=?", url)
        return Done()
    return fake_run


def test_a_slot_never_gets_a_second_job_while_it_holds_a_filled_form(wd, db, monkeypatch):
    """THE property that cost two applications, restated per slot.

    Six eligible jobs, three slots, every one of them hands over. Exactly three applies may
    run — one per browser — and no slot may appear twice. A slot appearing twice IS the Zello
    incident: the second launch clears that port and the first filled form is gone.
    """
    for i, name in enumerate(("Zello", "Deloitte", "Affirm", "Visa", "Okta", "Stripe")):
        _job(db, f"http://j/{i}", name)
    monkeypatch.setenv("APPLY_WORKERS", "3")
    launched: list[tuple[str, int]] = []
    monkeypatch.setattr(wd.subprocess, "run", _handoff_runner(db, launched))

    res = wd.run_dashboard_apply(limit=10, copilot=True)

    slots = [s for _u, s in launched]
    assert len(launched) == 3, f"launched {len(launched)} applies into 3 browsers"
    assert len(set(slots)) == 3, f"a slot was reused while holding a form: {slots}"
    assert sorted(set(slots)) == [0, 1, 2], f"slots must be distinct ports: {slots}"
    assert res["needs_review"] == 3
    assert res["held_back"] == 3


def test_one_worker_still_behaves_exactly_as_it_used_to(wd, db, monkeypatch):
    """The old guarantee is still reachable, and is what APPLY_WORKERS=1 means. Without this,
    "we made it parallel" would have quietly removed the setting's safest value."""
    for i, name in enumerate(("Zello", "Deloitte", "Affirm")):
        _job(db, f"http://j/{i}", name)
    monkeypatch.setenv("APPLY_WORKERS", "1")
    launched: list[tuple[str, int]] = []
    monkeypatch.setattr(wd.subprocess, "run", _handoff_runner(db, launched))

    res = wd.run_dashboard_apply(limit=10, copilot=True)
    assert len(launched) == 1
    assert res["held_back"] == 2


def test_the_worker_count_is_a_setting_and_is_bounded(wd, monkeypatch):
    """Unbounded, this is a way to open forty Chrome windows by typing a number. The cap is
    about the human: past a handful, filled forms get closed or forgotten before anyone
    reaches them, which is the same loss in a slower shape."""
    monkeypatch.setenv("APPLY_WORKERS", "3")
    assert wd.apply_workers() == 3
    monkeypatch.setenv("APPLY_WORKERS", "40")
    assert wd.apply_workers() == 6
    monkeypatch.setenv("APPLY_WORKERS", "0")
    assert wd.apply_workers() == 1
    monkeypatch.setenv("APPLY_WORKERS", "nonsense")
    assert wd.apply_workers() == 3, "a malformed value must not mean zero or unlimited"
    monkeypatch.delenv("APPLY_WORKERS")
    assert wd.apply_workers() == 3


def test_a_busy_slot_is_skipped_rather_than_reused(wd, db, monkeypatch):
    """Slot 0 is holding a live review from an earlier run. New work must route AROUND it."""
    from applypilot.apply import chrome
    _job(db, "http://j/open", "Zello", apply_status="ready_to_submit")
    for i, name in enumerate(("Deloitte", "Affirm")):
        _job(db, f"http://j/{i}", name)
    monkeypatch.setenv("APPLY_WORKERS", "3")
    # Only port 9222 answers: slot 0 is occupied, 1 and 2 are free.
    monkeypatch.setattr(chrome, "chrome_alive_on_port",
                        lambda port, **k: port == chrome.BASE_CDP_PORT)
    launched: list[tuple[str, int]] = []
    monkeypatch.setattr(wd.subprocess, "run", _handoff_runner(db, launched))

    wd.run_dashboard_apply(limit=10, copilot=True)
    slots = [s for _u, s in launched]
    assert 0 not in slots, f"work was sent to the slot holding an open review: {slots}"
    assert sorted(slots) == [1, 2]


def test_a_paused_queue_says_so_in_the_activity_log(wd, db, monkeypatch):
    """Silently stopping is its own bug — the operator would assume the rest had run."""
    for i, name in enumerate(("Zello", "Deloitte", "Affirm", "Visa")):
        _job(db, f"http://j/{i}", name)
    monkeypatch.setenv("APPLY_WORKERS", "2")
    monkeypatch.setattr(wd.subprocess, "run", _handoff_runner(db, []))
    wd.run_dashboard_apply(limit=10, copilot=True)
    # Queue ORDER is a UI-precedence decision, not something this test should assume — look
    # for the note on whichever job paused. Asserting on "http://j/0" made this fail for the
    # wrong reason when the queue happened to run the other job first.
    details = [e["detail"] for url in (f"http://j/{i}" for i in range(4))
               for e in database.get_job_events(url, conn=db)]
    assert any("held back" in (d or "") for d in details), details


def test_applied_jobs_do_not_pause_the_queue(wd, db, monkeypatch):
    """Only a PENDING HUMAN blocks a slot. A job that fully applied holds no browser, so its
    slot goes back for the next one — and the run keeps going past `APPLY_WORKERS`.

    Deliberately SEVEN jobs against TWO slots. With two of each the test would pass on an
    implementation that does exactly one job per slot and stops, which is what the first
    version of this change actually did.
    """
    for i in range(7):
        _job(db, f"http://j/{i}", f"Co{i}")
    monkeypatch.setenv("APPLY_WORKERS", "2")
    launched = []
    import threading
    lock = threading.Lock()

    def fake_run(args, **kw):
        url = args[args.index("--url") + 1]
        with lock:
            launched.append(url)
        _write(db, "UPDATE jobs SET apply_status='applied', "
                   "applied_at='2026-07-29T00:00:00+00:00' WHERE url=?", url)
        return Done()

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    res = wd.run_dashboard_apply(limit=10, copilot=True)
    assert len(launched) == 7, f"the queue stopped early at {len(launched)} of 7"
    assert res["applied"] == 7
    assert res["held_back"] == 0


def test_a_slot_freed_by_a_FAILURE_is_reused_too(wd, db, monkeypatch):
    """A failed apply closes its browser (only genuine dead ends do), so that slot is free.
    Treating failure as "holding a form" would stall the queue behind a job nobody can see."""
    for i in range(5):
        _job(db, f"http://j/{i}", f"Co{i}")
    monkeypatch.setenv("APPLY_WORKERS", "1")
    launched = []

    def fake_run(args, **kw):
        url = args[args.index("--url") + 1]
        launched.append(url)
        _write(db, "UPDATE jobs SET apply_status='failed', apply_error='expired' "
                   "WHERE url=?", url)
        return Done()

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    res = wd.run_dashboard_apply(limit=10, copilot=True)
    assert len(launched) == 5, "one failure stalled the whole queue"
    assert res["failed"] == 5


def test_dry_run_is_not_gated(wd, db, monkeypatch, live_review_browser):
    """A dry run opens no browser, so a pending review cannot be harmed by it."""
    _job(db, "http://j/zello", "Zello", apply_status="ready_to_submit")
    _job(db, "http://j/next", "NextCo")
    launched = []

    class Done:
        returncode = 0

    monkeypatch.setattr(wd.subprocess, "run",
                        lambda args, **kw: (launched.append(1), Done())[1])
    wd.run_dashboard_apply(limit=10, copilot=True, dry_run=True)
    assert launched, "a dry run was blocked even though it opens no browser"


def test_a_LOGIN_WALL_holds_its_slot_just_like_a_filled_form(wd, db, monkeypatch):
    """`needs_human` is an open browser too — a captcha, a registration wall, a stuck field.
    The operator is meant to finish it by hand in that window.

    Treating only `ready_to_submit` as "holding" gives the slot a second job and closes the
    login the operator was halfway through, which is the original loss wearing a different
    status. Both statuses leave the browser open; both must hold the slot.
    """
    for i in range(4):
        _job(db, f"http://j/{i}", f"Co{i}")
    monkeypatch.setenv("APPLY_WORKERS", "2")
    launched: list[tuple[str, int]] = []
    import threading
    lock = threading.Lock()

    def fake_run(args, **kw):
        url = args[args.index("--url") + 1]
        slot = int(args[args.index("--worker-id") + 1])
        with lock:
            launched.append((url, slot))
        _write(db, "UPDATE jobs SET apply_status='needs_human', apply_error='login' "
                   "WHERE url=?", url)
        return Done()

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    res = wd.run_dashboard_apply(limit=10, copilot=True)

    slots = [s for _u, s in launched]
    assert len(launched) == 2, f"{len(launched)} applies ran into 2 browsers holding logins"
    assert len(set(slots)) == 2, f"a slot was reused while holding a login wall: {slots}"
    assert res["held_back"] == 2


def test_a_dry_run_opens_one_browser_and_not_a_fleet(wd, db, monkeypatch):
    """A dry run submits nothing and hands over nothing, so it needs no parallel review slots.
    Spawning APPLY_WORKERS Chrome instances for it is pure cost — and on a machine where each
    profile is gigabytes, it is the kind of cost that gets noticed a week later."""
    for i in range(4):
        _job(db, f"http://j/{i}", f"Co{i}")
    monkeypatch.setenv("APPLY_WORKERS", "3")
    slots = []
    monkeypatch.setattr(wd.subprocess, "run",
                        lambda args, **kw: (
                            slots.append(int(args[args.index("--worker-id") + 1])), Done())[1])

    wd.run_dashboard_apply(limit=10, dry_run=True, copilot=True)
    assert set(slots) == {0}, f"a dry run spread across slots {sorted(set(slots))}"
    assert len(slots) == 4, "a dry run stopped early — nothing is holding a browser"
