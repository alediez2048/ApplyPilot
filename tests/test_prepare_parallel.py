"""Prepare runs several jobs at once, and says when the fabrication judge was overruled.

Measured 2026-08-13 on a real posting, which is what decided the shape:

    tailor            107.0s   <- 8 LLM calls: 4x (generate ~25s + judge ~4s)
    cover letter        4.2s
    PDF render          0.4s

The tailor is 95% of prepare and all of it is waiting on the network, so the fix is to overlap
the waits rather than to make any call cheaper. It costs no extra tokens — the same calls,
concurrent.

The second half of this file is not about speed. Reading the 56 stored reports to find out WHY
the tailor retries four times turned up something worse: `approved_with_judge_warning` means the
fabrication judge rejected the résumé on every attempt and we shipped the last one anyway, and it
was treated as identical to `approved` everywhere — so the verdict reached nothing anyone could
see. All four are real inventions ("Python, JavaScript, TypeScript" on a résumé listing none of
them; "PostgreSQL"; "C++, TCP/IP, UDP"), and THREE were submitted to real employers.
"""

from __future__ import annotations

import threading
import time

import pytest

import applypilot.database as database


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    conn._test_path = path
    return conn


@pytest.fixture()
def wd(db, monkeypatch, tmp_path):
    from applypilot import web_dashboard
    monkeypatch.setattr(web_dashboard, "get_connection",
                        lambda *a, **k: database.get_connection(db._test_path))
    monkeypatch.setattr(web_dashboard, "init_db", lambda *a, **k: db)
    monkeypatch.setattr(web_dashboard.config, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(web_dashboard.config, "ensure_dirs", lambda *a, **k: None)
    return web_dashboard


# ── the pool ────────────────────────────────────────────────────────────────

def test_the_worker_count_is_a_setting_and_is_bounded(wd, monkeypatch):
    monkeypatch.setenv("PREPARE_WORKERS", "4")
    assert wd.prepare_workers() == 4
    monkeypatch.setenv("PREPARE_WORKERS", "99")
    assert wd.prepare_workers() == 8, "unbounded concurrency rate-limits you against yourself"
    monkeypatch.setenv("PREPARE_WORKERS", "0")
    assert wd.prepare_workers() == 1
    monkeypatch.setenv("PREPARE_WORKERS", "nope")
    assert wd.prepare_workers() == 3, "a malformed value must not mean zero or unlimited"
    monkeypatch.delenv("PREPARE_WORKERS")
    assert wd.prepare_workers() == 3


def test_jobs_really_do_overlap(wd, monkeypatch):
    """The whole point. Four jobs that each sleep must not take four sleeps of wall clock.

    Asserts on OVERLAP (how many ran at once) rather than only on elapsed time — a timing-only
    assertion passes on a fast machine even when the work is serial.
    """
    monkeypatch.setenv("PREPARE_WORKERS", "4")
    live, peak, lock = 0, 0, threading.Lock()

    def slow(job):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.25)
        with lock:
            live -= 1
        return job, True, ""

    jobs = [{"url": f"http://j/{i}", "site": f"Co{i}"} for i in range(4)]
    t = time.time()
    out = wd._prepare_pool(slow, jobs, "tailor")
    elapsed = time.time() - t

    assert len(out) == 4
    assert peak >= 2, f"nothing overlapped — peak concurrency was {peak}"
    assert elapsed < 0.9, f"four 0.25s jobs took {elapsed:.2f}s; they ran one after another"


def test_one_worker_is_exactly_the_old_sequential_behaviour(wd, monkeypatch):
    """PREPARE_WORKERS=1 must not merely be slow — it must not use the pool at all, so the
    single-job path keeps its current stack trace and ordering."""
    monkeypatch.setenv("PREPARE_WORKERS", "1")
    order = []
    jobs = [{"url": f"http://j/{i}", "site": f"Co{i}"} for i in range(4)]
    wd._prepare_pool(lambda j: (order.append(j["site"]), (j, True, ""))[1], jobs, "tailor")
    assert order == ["Co0", "Co1", "Co2", "Co3"], "sequential mode reordered the queue"


def test_one_failing_job_does_not_cost_the_others_theirs(wd, monkeypatch):
    """An unreachable posting must not take the other three résumés down with it."""
    monkeypatch.setenv("PREPARE_WORKERS", "4")

    def flaky(job):
        if job["site"] == "Bad":
            raise RuntimeError("boom")
        return job, True, ""

    jobs = [{"url": f"http://j/{i}", "site": s}
            for i, s in enumerate(["Good1", "Bad", "Good2", "Good3"])]
    out = wd._prepare_pool(flaky, jobs, "tailor")
    assert len(out) == 4
    assert sum(1 for _j, ok, _e in out if ok) == 3
    assert any("boom" in err for _j, ok, err in out if not ok)


def test_every_job_gets_a_result_even_when_all_of_them_fail(wd, monkeypatch):
    """A dropped result is a job that silently never gets counted, which reads as success."""
    monkeypatch.setenv("PREPARE_WORKERS", "3")

    def always_bad(job):
        raise RuntimeError("no")

    jobs = [{"url": f"http://j/{i}", "site": f"Co{i}"} for i in range(3)]
    out = wd._prepare_pool(always_bad, jobs, "tailor")
    assert len(out) == 3 and not any(ok for _j, ok, _e in out)


# ── the judge, which is the part that matters ───────────────────────────────

def _seed(db, url="http://j/1"):
    db.execute(
        "INSERT INTO jobs (url, title, site, company, strategy, full_description, fit_score) "
        "VALUES (?, 'Engineer', 'Acme', 'Acme', 'dashboard_upload', 'A real description.', 9)",
        (url,))
    db.commit()
    return url


def _run_prepare(wd, db, monkeypatch, tmp_path, status, issues, judge_passed=False):
    """Drive `run_dashboard_prepare` with the tailor stubbed to a chosen verdict."""
    from applypilot import config as cfg
    monkeypatch.setattr(cfg, "TAILORED_DIR", tmp_path / "tail", raising=False)
    monkeypatch.setattr(cfg, "COVER_LETTER_DIR", tmp_path / "cl", raising=False)
    monkeypatch.setattr(cfg, "RESUME_PATH", tmp_path / "r.txt", raising=False)
    (tmp_path / "r.txt").write_text("BASE RESUME\nWORK EXPERIENCE\n- did things\n")
    monkeypatch.setattr(cfg, "load_profile", lambda: {"personal": {"full_name": "Dana Okafor"}})
    monkeypatch.setattr("applypilot.scoring.tailor.tailor_resume",
                        lambda *a, **k: ("TAILORED RESUME TEXT", {
                            "status": status, "attempts": 4,
                            "judge": {"passed": judge_passed, "issues": issues},
                            "validator": {"warnings": []}}))
    monkeypatch.setattr("applypilot.scoring.cover_letter.generate_cover_letter",
                        lambda *a, **k: "Dear Acme, ...")
    monkeypatch.setattr("applypilot.scoring.pdf.convert_to_pdf", lambda *a, **k: None)
    return wd.run_dashboard_prepare(limit=0)


FABRICATION = ('1. **Fabricated technical skills**: adds "Python, JavaScript, TypeScript" to '
               'TECHNICAL SKILLS. The original resume does NOT list these languages.')


def test_an_overruled_judge_reaches_the_activity_tab(wd, db, monkeypatch, tmp_path):
    """THE bug this found. `approved_with_judge_warning` shipped a résumé the judge rejected on
    all four attempts, and was treated as identical to `approved` — so nothing said so anywhere
    the operator looks. Three such résumés went to real employers."""
    url = _seed(db)
    _run_prepare(wd, db, monkeypatch, tmp_path, "approved_with_judge_warning", FABRICATION)

    events = database.get_job_events(url, conn=db)
    details = [e["detail"] for e in events]
    hit = [d for d in details if "fabrication judge rejected" in (d or "")]
    assert hit, f"the overruled judge is invisible: {details}"
    assert "Python, JavaScript, TypeScript" in hit[0], \
        "the note does not say WHAT was invented, so it cannot be acted on"


def test_it_is_logged_as_a_FAILURE_not_as_a_note(wd, db, monkeypatch, tmp_path):
    """`info` is where the ordinary validator notes go and they scroll past. A résumé that
    invents skills is not a note — and it is about to be sent to an employer."""
    url = _seed(db)
    _run_prepare(wd, db, monkeypatch, tmp_path, "approved_with_judge_warning", FABRICATION)
    levels = {e["status"] for e in database.get_job_events(url, conn=db)
              if "fabrication judge" in (e["detail"] or "")}
    assert levels == {"failed"}, f"logged at {levels}, which reads as routine"


def test_the_resume_is_STILL_accepted(wd, db, monkeypatch, tmp_path):
    """Deliberate, and the negative case for the two above. Discarding a rendered résumé
    mid-prepare is what left "the full app didn't go through" with no materials at all, and the
    operator is the one who can say whether a skill is genuinely theirs. Warn loudly, do not
    silently withhold."""
    url = _seed(db)
    res = _run_prepare(wd, db, monkeypatch, tmp_path, "approved_with_judge_warning", FABRICATION)
    assert res["tailored"] == 1 and res["tailor_errors"] == 0
    row = db.execute("SELECT tailored_resume_path FROM jobs WHERE url = ?", (url,)).fetchone()
    assert row[0], "the résumé was withheld — prepare now produces nothing to apply with"


def test_a_clean_resume_says_nothing_about_the_judge(wd, db, monkeypatch, tmp_path):
    """A warning that fires on every job is one nobody reads. 50 of the 56 stored reports are
    plain `approved` and must stay silent."""
    url = _seed(db)
    _run_prepare(wd, db, monkeypatch, tmp_path, "approved", "none", judge_passed=True)
    details = [e["detail"] or "" for e in database.get_job_events(url, conn=db)]
    assert not any("fabrication judge" in d for d in details), details
    assert any("Tailored résumé generated" in d for d in details)


def test_prepare_really_threads_and_the_db_writes_survive_it(wd, db, monkeypatch, tmp_path):
    """The end-to-end version, and the one the single-job tests above cannot give.

    With one job `_prepare_pool` runs sequentially, so every test that seeds one job proves
    nothing about threading — a worker reusing the request thread's sqlite handle passes them
    all and then fails on the first real two-job prepare with "SQLite objects created in a
    thread can only be used in that same thread", which is a database error where a résumé
    should be.
    """
    urls = [_seed(db, f"http://j/{i}") for i in range(4)]
    monkeypatch.setenv("PREPARE_WORKERS", "4")
    seen_threads = set()

    from applypilot import config as cfg
    monkeypatch.setattr(cfg, "TAILORED_DIR", tmp_path / "tail", raising=False)
    monkeypatch.setattr(cfg, "COVER_LETTER_DIR", tmp_path / "cl", raising=False)
    monkeypatch.setattr(cfg, "RESUME_PATH", tmp_path / "r.txt", raising=False)
    (tmp_path / "r.txt").write_text("BASE RESUME\nWORK EXPERIENCE\n- did things\n")
    monkeypatch.setattr(cfg, "load_profile", lambda: {"personal": {"full_name": "Dana Okafor"}})

    def fake_tailor(*a, **k):
        seen_threads.add(threading.current_thread().name)
        time.sleep(0.1)
        return "TAILORED", {"status": "approved", "attempts": 1,
                            "judge": {"passed": True, "issues": "none"},
                            "validator": {"warnings": []}}

    monkeypatch.setattr("applypilot.scoring.tailor.tailor_resume", fake_tailor)
    monkeypatch.setattr("applypilot.scoring.cover_letter.generate_cover_letter",
                        lambda *a, **k: "Dear Acme, ...")
    monkeypatch.setattr("applypilot.scoring.pdf.convert_to_pdf", lambda *a, **k: None)

    res = wd.run_dashboard_prepare(limit=0)

    assert len(seen_threads) > 1, f"prepare never threaded: {seen_threads}"
    assert res["tailored"] == 4 and res["tailor_errors"] == 0, res
    # The writes are the point: every row must actually carry its résumé afterwards.
    rows = db.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND url IN "
        f"({','.join('?' * len(urls))})", urls).fetchone()[0]
    assert rows == 4, f"only {rows} of 4 résumés were recorded — a worker's write was lost"
    assert res["covers"] == 4, res


# ── the round-robin, which only matters once calls overlap ──────────────────

def test_the_failover_client_spreads_across_providers_under_concurrency():
    """Several résumés at once must still alternate providers, not pile onto one.

    **This does NOT prove the lock is load-bearing, and saying so is the point.** Measured on
    CPython 3.11 with `sys.setswitchinterval(1e-6)`, 8 threads × 2000 unlocked increments lost
    zero — so removing the lock is an equivalent mutant here and no honest test kills it. What
    this DOES catch is the thing that would really break spreading: an advance that never
    happens (see the mutation "every call takes the same provider", which it kills).
    """
    from applypilot import llm

    class Fake:
        def __init__(self, name):
            self.name = name
            self.model = "test-model"   # `_no_think` inspects it before dispatching
            self.hits = 0

        def attempt(self, messages, temperature, max_tokens):
            self.hits += 1
            time.sleep(0.002)
            return "ok"

    a, b = Fake("a"), Fake("b")
    client = llm.FailoverClient([a, b])
    monkey = [threading.Thread(target=lambda: client.chat([{"role": "user", "content": "x"}]))
              for _ in range(40)]
    for t in monkey:
        t.start()
    for t in monkey:
        t.join()

    assert a.hits + b.hits == 40
    assert min(a.hits, b.hits) >= 15, \
        f"round-robin collapsed onto one provider under load: a={a.hits} b={b.hits}"
