"""NET-2 tests: dashboard contact payload, Origin guard, network task registry."""

from __future__ import annotations

from applypilot import web_dashboard as wd


class _Headers:
    def __init__(self, d):
        self._d = d

    def get(self, k, default=None):
        return self._d.get(k, default)


class _Handler:
    def __init__(self, headers):
        self.headers = _Headers(headers)


def test_origin_ok_allows_localhost():
    assert wd._origin_ok(_Handler({"Host": "127.0.0.1:8765"})) is True
    assert wd._origin_ok(_Handler({"Origin": "http://localhost:8765", "Host": "localhost:8765"})) is True


def test_origin_ok_rejects_cross_origin():
    assert wd._origin_ok(_Handler({"Origin": "http://evil.com", "Host": "127.0.0.1:8765"})) is False


def test_contact_payload_shape(tmp_path, monkeypatch):
    import applypilot.database as database
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)  # connections.match() needs the DB (no connections imported -> no match)

    p = wd._contact_payload({
        "id": "c1", "full_name": "Jane", "title": "Eng", "email": "j@x.com",
        "email_status": "verified", "linkedin_url": "https://l/in/j", "match_reason": "same role",
        "outreach_subject": "Hi", "outreach_message": "Body", "outreach_status": "drafted",
    }, "Acme")
    assert p["id"] == "c1" and p["full_name"] == "Jane" and p["email"] == "j@x.com"
    assert p["outreach_subject"] == "Hi" and p["outreach_message"] == "Body"
    assert p["outreach_status"] == "drafted"
    assert p["is_connection"] is False  # none imported
    # missing fields default cleanly
    empty = wd._contact_payload({})
    assert empty["email_status"] == "none" and empty["outreach_status"] == "none"


def test_network_runner_rejects_concurrent_same_job(monkeypatch):
    runner = wd.NetworkRunner()
    # make the worker block so the task stays "running"
    import threading
    gate = threading.Event()
    monkeypatch.setattr(runner, "_run", lambda *a: gate.wait(timeout=2))
    ok1, _ = runner.start("http://j/1", 5, False)
    ok2, msg = runner.start("http://j/1", 5, False)  # same job, still running
    assert ok1 is True and ok2 is False and "already" in msg
    ok3, _ = runner.start("http://j/2", 5, False)     # different job runs
    assert ok3 is True
    gate.set()
