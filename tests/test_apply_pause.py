"""Pause stops the AGENT and keeps the BROWSER. Stop kills everything.

Asked for 2026-07-29: "can we add a pause application that stops the browser agent while its
applying?"

`/api/stop` already existed and is the wrong tool for this: it does
`os.killpg(proc.pid, SIGTERM)`, and the group includes Chrome, so it destroys the half-filled
form. Pause stops only the Claude agent and hands the browser over as `needs_human:paused` —
the same handover co-pilot performs, so Continue / Mark-submitted keep working on it.

The signal is a FILE because the apply runs in a separate OS process from the dashboard, so
there is no shared memory to flip a flag in.

The dangerous failure mode is a flag that outlives its run: a leftover would pause every future
application the instant it began, which looks exactly like the apply being broken. Both ends of
that lifecycle are pinned here.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.apply import pause


@pytest.fixture()
def app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pause.config, "APP_DIR", tmp_path)
    return tmp_path


# ── the flag itself ──────────────────────────────────────────────────────────────────────

def test_no_flag_means_no_pause(app_dir):
    assert pause.pause_requested() is False


def test_requesting_then_observing_then_clearing(app_dir):
    pause.request_pause()
    assert pause.pause_requested() is True
    pause.clear_pause()
    assert pause.pause_requested() is False


def test_clearing_when_nothing_is_set_is_not_an_error(app_dir):
    """Called unconditionally at the start of every run, so it must be a no-op when idle."""
    pause.clear_pause()
    pause.clear_pause()
    assert pause.pause_requested() is False


def test_requesting_twice_stays_a_single_pause(app_dir):
    pause.request_pause()
    pause.request_pause()
    pause.clear_pause()
    assert pause.pause_requested() is False, "a second request left a second flag behind"


def test_the_flag_lives_in_app_dir_so_it_survives_the_dashboard(app_dir):
    """Deliberately not a temp dir: if the dashboard dies mid-pause the flag must still be
    findable and clearable rather than stranding the next run."""
    pause.request_pause()
    assert (app_dir / pause.PAUSE_FILENAME).exists()


def test_a_missing_app_dir_is_created_rather_than_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(pause.config, "APP_DIR", tmp_path / "not" / "there" / "yet")
    pause.request_pause()
    assert pause.pause_requested() is True


# ── the lifecycle guarantee that keeps it from breaking every future apply ────────────────

def test_a_run_clears_a_stale_flag_before_it_starts():
    """A flag left by a crash would pause the next application instantly. `main()` clears it,
    and this asserts the call is actually there rather than trusting the comment."""
    import inspect

    from applypilot.apply import launcher
    src = inspect.getsource(launcher.main)
    assert "pause.clear_pause()" in src, \
        "main() does not clear a stale pause flag; the next apply would pause on start-up"


def test_the_agent_loop_consumes_the_flag_it_acts_on():
    """If the check did not clear it, the SECOND job in a batch would pause too — one click
    would silently pause everything that followed."""
    import inspect

    from applypilot.apply import launcher
    src = inspect.getsource(launcher.run_job)
    assert "pause.pause_requested()" in src, "the agent loop never checks for a pause"
    idx = src.index("pause.pause_requested()")
    assert "pause.clear_pause()" in src[idx:idx + 400], \
        "the pause flag is observed but never consumed"


def test_pausing_keeps_the_browser_and_hands_over():
    """The whole point. `paused` must reach keep_chrome_alive and mark needs_human — if it fell
    through to the generic failure branch, cleanup_worker would reap Chrome and lose the form."""
    import inspect

    from applypilot.apply import launcher
    lines = inspect.getsource(launcher).splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == 'elif result == "paused":']
    assert starts, "no branch handles a paused result"
    i = starts[0]
    indent = len(lines[i]) - len(lines[i].lstrip())

    # Take ONLY this branch: stop at the next elif/else at the SAME indent. Slicing by the next
    # "elif" anywhere matched this branch's own keyword at offset 0 and made the body empty, so
    # every assertion below passed against "". Bounding it properly also stops the test
    # succeeding on a NEIGHBOURING branch's keep_chrome_alive call.
    body: list[str] = []
    for ln in lines[i + 1:]:
        stripped = ln.strip()
        if stripped and (len(ln) - len(ln.lstrip())) <= indent and stripped.startswith(("elif ", "else:")):
            break
        body.append(ln)
    seg = "\n".join(body)
    assert seg.strip(), "the paused branch has an empty body"

    assert "keep_chrome_alive" in seg, f"a paused run does not keep its browser:\n{seg}"
    assert "needs_human" in seg, f"a paused run is not handed to the operator:\n{seg}"
    assert "chrome_proc = None" in seg, f"the finally would still reap Chrome:\n{seg}"


# ── the endpoint ─────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    return database.get_connection(path)


@pytest.fixture()
def wd(db, monkeypatch, app_dir):
    from applypilot import web_dashboard
    monkeypatch.setattr(web_dashboard, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(web_dashboard, "init_db", lambda *a, **k: db)
    return web_dashboard


def _job(conn, url, title, **cols):
    base = {"site": title, "strategy": "dashboard_upload",
            "discovered_at": "2026-07-29T10:00:00+00:00"}
    base.update(cols)
    keys = ", ".join(["url", "title", *base])
    marks = ", ".join("?" for _ in range(len(base) + 2))
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", (url, title, *base.values()))
    conn.commit()


def test_pausing_with_nothing_running_sets_no_flag(wd, db, app_dir):
    """Otherwise the flag waits around and kills the NEXT application the user starts."""
    _job(db, "http://j/1", "Idle", apply_status=None)
    res = wd._pause_apply()
    assert res["ok"] is False
    assert pause.pause_requested() is False, "left a pause flag behind with nothing to pause"


def test_pausing_a_live_apply_sets_the_flag_and_names_the_job(wd, db, app_dir):
    _job(db, "http://j/sf", "Salesforce - Forward Deployed Engineer",
         apply_status="in_progress", last_attempted_at="2026-07-29T23:00:00+00:00")
    res = wd._pause_apply()
    assert res["ok"] is True
    assert pause.pause_requested() is True
    assert "Salesforce" in res["message"], res["message"]
    assert "browser stays open" in res["message"], res["message"]
