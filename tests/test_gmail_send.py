"""NET-4 tests: Gmail send safeguards (gate, atomic claim, daily cap, dedupe, MIME)."""

from __future__ import annotations

import applypilot.database as database
from applypilot.networking import gmail_send, store


def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)
    store.init_contacts()


def _gmail_env(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw")


def _contact(**over):
    base = {"job_url": "http://j/1", "full_name": "Jane", "email": "jane@x.com",
            "email_status": "verified", "outreach_subject": "Hi", "outreach_message": "Body",
            "outreach_status": "drafted", "source": "apollo"}
    base.update(over)
    return base


# ── can_send gating ─────────────────────────────────────────────────────────

def test_can_send_blocks_without_gmail(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    ok, why = gmail_send.can_send(_contact())
    assert ok is False and "Gmail not connected" in why


def test_can_send_blocks_no_address(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _gmail_env(monkeypatch)
    ok, why = gmail_send.can_send(_contact(email="", email_status="none"))
    assert ok is False and "no email" in why


def test_can_send_unverified_requires_confirm(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _gmail_env(monkeypatch)
    c = _contact(email_status="unverified")
    assert gmail_send.can_send(c)[0] is False
    assert gmail_send.can_send(c, confirm_unverified=True)[0] is True


def test_can_send_daily_cap(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _gmail_env(monkeypatch)
    monkeypatch.setenv("OUTREACH_DAILY_LIMIT", "1")
    monkeypatch.setattr(gmail_send, "_DAILY_LIMIT", 1)
    # one already submitted today
    cid = store.upsert_contact(_contact(email="a@x.com"))
    store.claim_for_send(cid)
    store.mark_sent(cid, "<id>")
    ok, why = gmail_send.can_send(_contact(email="b@x.com"))
    assert ok is False and "daily send limit" in why


def test_can_send_cross_job_dedupe(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _gmail_env(monkeypatch)
    cid = store.upsert_contact(_contact(job_url="http://j/1", email="dup@x.com"))
    store.claim_for_send(cid)
    store.mark_sent(cid, "<id>")
    # same human, different job
    ok, why = gmail_send.can_send(_contact(job_url="http://j/2", email="dup@x.com"))
    assert ok is False and "another role" in why


# ── atomic claim ────────────────────────────────────────────────────────────

def test_claim_for_send_is_single_winner(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    cid = store.upsert_contact(_contact())
    assert store.claim_for_send(cid) is True
    assert store.claim_for_send(cid) is False  # already claimed (submitted_at set)


# ── full send (SMTP stubbed) ────────────────────────────────────────────────

def test_send_outreach_happy_path(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _gmail_env(monkeypatch)
    sent = {}

    def fake_smtp(to, subject, body, mid, attachments=None):
        sent.update(to=to, subject=subject, body=body, mid=mid)
    monkeypatch.setattr(gmail_send, "_smtp_send", fake_smtp)

    cid = store.upsert_contact(_contact())
    res = gmail_send.send_outreach(cid)
    assert res["ok"] and res["status"] == "submitted"
    assert sent["to"] == "jane@x.com" and "Body" in sent["body"]
    assert sent["mid"].startswith("<") and sent["mid"].endswith(">")  # client Message-ID
    # persisted + dedupe now blocks a resend
    assert store.get_contact(cid)["outreach_status"] == "submitted"
    assert gmail_send.send_outreach(cid)["ok"] is False


def test_send_outreach_prefers_oauth_when_available(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    # OAuth available -> transport() should pick it over SMTP; use the Gmail API path
    from applypilot.networking import gmail_oauth
    monkeypatch.setattr(gmail_oauth, "available", lambda: True)
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: "me@utexas.edu")
    captured = {}

    def fake_oauth_send(to, subject, body, from_addr, from_name="", attachments=None):
        captured.update(to=to, from_addr=from_addr, body=body)
        return "gmail-real-id-123"
    monkeypatch.setattr(gmail_oauth, "send", fake_oauth_send)
    # ensure SMTP is NOT used
    monkeypatch.setattr(gmail_send, "_smtp_send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SMTP used")))

    cid = store.upsert_contact(_contact())
    res = gmail_send.send_outreach(cid)
    assert res["ok"] and "oauth" in res["message"]
    assert captured["to"] == "jane@x.com" and captured["from_addr"] == "me@utexas.edu"
    assert store.get_contact(cid)["sent_message_id"] == "gmail-real-id-123"  # real Gmail id


def test_send_outreach_dry_run_does_not_send(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _gmail_env(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(gmail_send, "_smtp_send", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    cid = store.upsert_contact(_contact())
    res = gmail_send.send_outreach(cid, dry_run=True)
    assert res["ok"] and called["n"] == 0
    assert store.get_contact(cid)["outreach_status"] == "drafted"  # unchanged


def test_job_attachments_resolves_resume_and_cover(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    # Stage resume + cover PDFs and a job row pointing at their .txt paths.
    resume_txt = tmp_path / "r.txt"
    resume_txt.write_text("x")
    (tmp_path / "r.pdf").write_bytes(b"%PDF-resume")
    cover_txt = tmp_path / "c.txt"
    cover_txt.write_text("x")
    (tmp_path / "c.pdf").write_bytes(b"%PDF-cover")
    conn = database.get_connection()
    conn.execute("INSERT INTO jobs (url, tailored_resume_path, cover_letter_path) VALUES (?,?,?)",
                 ("http://j/1", str(resume_txt), str(cover_txt)))
    conn.commit()
    monkeypatch.setattr("applypilot.config.load_profile",
                        lambda: {"personal": {"full_name": "Jane Q Public"}})

    att = gmail_send.job_attachments("http://j/1")
    names = [f for _, f in att]
    assert names == ["Jane_Q_Public_Resume.pdf", "Jane_Q_Public_Cover_Letter.pdf"]

    # OUTREACH_ATTACH_DOCS=0 disables attachments entirely
    monkeypatch.setenv("OUTREACH_ATTACH_DOCS", "0")
    assert gmail_send.job_attachments("http://j/1") == []


def test_send_outreach_passes_attachments(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _gmail_env(monkeypatch)
    (tmp_path / "r.pdf").write_bytes(b"%PDF-resume")
    conn = database.get_connection()
    conn.execute("INSERT INTO jobs (url, tailored_resume_path) VALUES (?,?)",
                 ("http://j/1", str(tmp_path / "r.txt")))
    conn.commit()
    monkeypatch.setattr("applypilot.config.load_profile",
                        lambda: {"personal": {"full_name": "Jane Public"}})
    seen = {}

    def fake_smtp(to, subject, body, mid, attachments=None):
        seen["attachments"] = attachments
    monkeypatch.setattr(gmail_send, "_smtp_send", fake_smtp)

    cid = store.upsert_contact(_contact())
    gmail_send.send_outreach(cid)
    assert seen["attachments"] == [(str(tmp_path / "r.pdf"), "Jane_Public_Resume.pdf")]


def test_send_outreach_smtp_failure_marks_failed(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _gmail_env(monkeypatch)

    def boom(*a):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(gmail_send, "_smtp_send", boom)
    cid = store.upsert_contact(_contact())
    res = gmail_send.send_outreach(cid)
    assert res["ok"] is False and res["status"] == "failed"
    row = store.get_contact(cid)
    assert row["outreach_status"] == "failed" and row["submitted_at"] is None  # rolled back
