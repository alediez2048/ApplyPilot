"""Register once, then every later application to that employer is already authenticated.

Deloitte, Workday and Salesforce make you create an account before you can apply. The agent
cannot do that — it needs a password, an email code, sometimes SSO — and it should not: it
drives attacker-controlled careers pages, so it has no inbox access and no credentials.

The profile it uses IS persistent (830 cookie hosts and counting), so the session survives.
What was wrong was the TIMING: the wall was discovered mid-run, with an agent burning minutes
on something only a human can do.

"Sign in first" opens that same profile with NO agent attached, so the account is created
deliberately, once, before anything starts.
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
    return database.get_connection(path)


def _job(conn, url, title, **cols):
    base = {"site": title, "strategy": "dashboard_upload",
            "discovered_at": "2026-07-30T10:00:00+00:00"}
    base.update(cols)
    keys = ", ".join(["url", "title", *base])
    marks = ", ".join("?" for _ in range(len(base) + 2))
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({marks})", (url, title, *base.values()))
    conn.commit()


@pytest.fixture()
def wd(db, monkeypatch):
    from applypilot import web_dashboard
    monkeypatch.setattr(web_dashboard, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(web_dashboard, "init_db", lambda *a, **k: db)
    web_dashboard._SIGNIN_OPEN.clear()
    return web_dashboard


@pytest.fixture()
def chrome(monkeypatch):
    """Record launch_chrome calls instead of starting a browser."""
    from applypilot.apply import chrome as chrome_mod
    calls: list[dict] = []
    monkeypatch.setattr(chrome_mod, "launch_chrome",
                        lambda wid, **kw: (calls.append({"worker": wid, **kw}), object())[1])
    monkeypatch.setattr(chrome_mod, "_kill_on_port", lambda p: calls.append({"killed": p}))
    return calls


def test_it_opens_the_application_page_for_the_human(wd, db, chrome, monkeypatch):
    monkeypatch.setattr(wd, "_review_browser_alive", lambda *a, **k: False)
    _job(db, "http://j/dl", "Deloitte", application_url="https://apply.deloitte.com/x")

    res = wd._start_signin("http://j/dl")
    assert res["ok"] is True
    assert chrome[0]["url"] == "https://apply.deloitte.com/x", chrome[0]
    assert chrome[0]["human"] is True, "launched in agent mode; the password manager is suppressed there"


def test_it_falls_back_to_the_job_url_when_there_is_no_apply_link(wd, db, chrome, monkeypatch):
    monkeypatch.setattr(wd, "_review_browser_alive", lambda *a, **k: False)
    _job(db, "http://j/dl", "Deloitte")
    wd._start_signin("http://j/dl")
    assert chrome[0]["url"] == "http://j/dl"


def test_it_refuses_while_an_application_is_being_filled(wd, db, chrome, monkeypatch):
    """Chrome cannot run two instances on one user-data-dir, and launch_chrome would clear the
    port — silently reaping the agent's browser mid-application."""
    monkeypatch.setattr(wd, "_review_browser_alive", lambda *a, **k: False)
    _job(db, "http://j/dl", "Deloitte uploaded job", apply_status="in_progress",
         last_attempted_at="2026-07-30T15:20:00+00:00")
    _job(db, "http://j/other", "Other")

    res = wd._start_signin("http://j/other")
    assert res["ok"] is False
    assert "Deloitte" in res["message"], res["message"]
    assert not chrome, "launched Chrome anyway, killing the in-flight application's browser"


def test_it_refuses_while_a_review_browser_is_open(wd, db, chrome, monkeypatch):
    """Same profile, same port — opening a sign-in window would destroy a filled form."""
    monkeypatch.setattr(wd, "_review_browser_alive", lambda *a, **k: True)
    _job(db, "http://j/dl", "Deloitte")
    res = wd._start_signin("http://j/dl")
    assert res["ok"] is False
    assert not chrome


def test_an_unknown_job_is_rejected(wd, db, chrome, monkeypatch):
    monkeypatch.setattr(wd, "_review_browser_alive", lambda *a, **k: False)
    assert wd._start_signin("http://j/nope")["ok"] is False
    assert not chrome


# ── the two exits ────────────────────────────────────────────────────────────────────────

def test_fill_it_now_resumes_into_the_open_browser(wd, db, chrome, monkeypatch):
    """The point of the whole feature. Closing and relaunching would drop the session that was
    just created and walk straight back into the wall — so it must RESUME, which reconnects to
    the live browser on the same port."""
    started: list = []
    monkeypatch.setattr(wd, "_start_continue", lambda u: (started.append(u), (True, "started"))[1])
    wd._SIGNIN_OPEN["http://j/dl"] = 1.0

    res = wd._finish_signin("http://j/dl", fill=True)
    assert res["ok"] is True
    assert started == ["http://j/dl"], "did not hand the open browser to the agent"
    assert not any("killed" in c for c in chrome), "closed the browser it was meant to reuse"


def test_done_for_now_closes_the_window(wd, db, chrome):
    wd._SIGNIN_OPEN["http://j/dl"] = 1.0
    res = wd._finish_signin("http://j/dl", fill=False)
    assert res["ok"] is True
    assert any("killed" in c for c in chrome), "left an orphan browser holding the profile"


def test_either_exit_clears_the_waiting_state(wd, db, chrome, monkeypatch):
    """A stuck flag would keep the row showing the sign-in bar forever."""
    monkeypatch.setattr(wd, "_start_continue", lambda u: (True, "ok"))
    for fill in (True, False):
        wd._SIGNIN_OPEN["http://j/dl"] = 1.0
        wd._finish_signin("http://j/dl", fill=fill)
        assert "http://j/dl" not in wd._SIGNIN_OPEN


def test_the_waiting_state_needs_a_live_browser_not_just_a_flag(wd, monkeypatch):
    """If the operator closes Chrome themselves the row must stop offering to resume into it."""
    wd._SIGNIN_OPEN["http://j/dl"] = 1.0
    monkeypatch.setattr(wd, "_review_browser_alive", lambda *a, **k: True)
    assert wd._signin_state("http://j/dl") is True
    monkeypatch.setattr(wd, "_review_browser_alive", lambda *a, **k: False)
    assert wd._signin_state("http://j/dl") is False


# ── the agent must hand off fast, not explore ────────────────────────────────────────────

def test_the_agent_is_told_to_hand_off_immediately_on_an_account_wall():
    """It used to spend minutes discovering it could not register. Every one of those minutes
    is wasted: no amount of retrying gets an agent through account creation."""
    from applypilot.apply import prompt as prompt_mod
    src = __import__("inspect").getsource(prompt_mod)
    assert "HAND OFF IMMEDIATELY" in src
    assert "CREATE AN ACCOUNT" in src
    assert "NEEDS_HUMAN:login" in src


# ── the bug these tests uncovered ────────────────────────────────────────────────────────

def test_a_repo_row_becomes_a_real_job_dict_not_key_to_key():
    """`dict(zip(row.keys(), row))` on a DICT maps every key to ITSELF.

    repo.find_by_any_url returns a dict (it already did the zip), so the dashboard's
    "Find contacts" button was handing contact discovery:

        {"url": "url", "company": "company", "application_url": "application_url", ...}

    It searched Apollo for a company literally named "company" and verification dropped
    whatever came back — while the SAME search from the CLI, which passes a real row, worked.
    That is why it read as an Apollo coverage problem rather than a bug.
    """
    from applypilot.repo import jobs as _jobs

    real = {"url": "http://j/1", "title": "Engineer", "company": "Wander",
            "site": "Wander", "application_url": "http://a/1", "full_description": "x"}
    assert dict(real) == real
    broken = dict(zip(real.keys(), real))
    assert broken["company"] == "company", "sanity: this is the shape the bug produced"
    assert dict(real)["company"] == "Wander"

    src = __import__("inspect").getsource(_jobs.find_by_any_url)
    assert "_dict(" in src, "find_by_any_url no longer returns a dict; recheck every caller"


def test_no_caller_re_zips_an_already_dict_row():
    """Guards the two call sites that had it. _rows_to_dicts keeps the pattern legitimately —
    it is guarded by an isinstance check for genuine sqlite3.Row input."""
    import inspect

    from applypilot import web_dashboard
    src = inspect.getsource(web_dashboard)
    offenders = [ln.strip() for ln in src.splitlines()
                 if "dict(zip(row.keys(), row))" in ln and not ln.strip().startswith("#")]
    assert len(offenders) == 1, f"expected only the guarded _rows_to_dicts use, got: {offenders}"
    guarded = inspect.getsource(web_dashboard._rows_to_dicts)
    assert "isinstance(rows[0], dict)" in guarded
