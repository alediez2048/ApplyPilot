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

    def fake_oauth_send(to, subject, body, from_addr, from_name="", attachments=None,
                        thread_id=None, in_reply_to=None):
        captured.update(to=to, from_addr=from_addr, body=body)
        return {"id": "gmail-real-id-123", "thread_id": "thr-9",
                "rfc_message_id": "<abc@x.com>"}
    monkeypatch.setattr(gmail_oauth, "send", fake_oauth_send)
    # ensure SMTP is NOT used
    monkeypatch.setattr(gmail_send, "_smtp_send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SMTP used")))

    cid = store.upsert_contact(_contact())
    res = gmail_send.send_outreach(cid)
    assert res["ok"] and "oauth" in res["message"]
    assert captured["to"] == "jane@x.com" and captured["from_addr"] == "me@utexas.edu"
    got = store.get_contact(cid)
    assert got["sent_message_id"] == "gmail-real-id-123"     # real Gmail id
    # threading ids are captured at send time — no extra OAuth scope needed for them
    assert got["thread_id"] == "thr-9" and got["rfc_message_id"] == "<abc@x.com>"


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


# ── follow-up sequence ───────────────────────────────────────────────────────

def _emailed_contact(**kw):
    c = _contact()
    c.update(outreach_status="submitted", sent_message_id="gid-1", thread_id="thr-1",
             rfc_message_id="<orig@x.com>", outreach_subject="Question about the role",
             followup_subject="Re: Question about the role",
             followup_message="Floating this back up — no worries if the timing's off.")
    c.update(kw)
    return c


def test_followup_threads_into_the_original_conversation(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    from applypilot.networking import gmail_oauth
    monkeypatch.setattr(gmail_oauth, "available", lambda: True)
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: "me@x.com")
    seen = {}

    def fake_send(to, subject, body, from_addr, from_name="", attachments=None,
                  thread_id=None, in_reply_to=None):
        seen.update(thread_id=thread_id, in_reply_to=in_reply_to, attachments=attachments)
        return {"id": "gid-2", "thread_id": thread_id or "", "rfc_message_id": "<fu@x.com>"}
    monkeypatch.setattr(gmail_oauth, "send", fake_send)

    cid = store.upsert_contact(_emailed_contact())
    res = gmail_send.send_followup(cid)
    assert res["ok"] is True and res["touch"] == 1
    # the whole point: it lands inside the existing thread, not as a new cold email
    assert seen["thread_id"] == "thr-1" and seen["in_reply_to"] == "<orig@x.com>"
    assert not seen["attachments"]          # resume/cover went with email #1
    got = store.get_contact(cid)
    assert got["followup_count"] == 1 and got["followup_status"] == "sent"
    assert got["followed_up_at"]
    assert not got["followup_message"]      # draft consumed, so it can't be re-sent verbatim


def test_followup_requires_a_first_email_and_a_draft(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    never_emailed = store.upsert_contact(_contact())
    assert gmail_send.send_followup(never_emailed)["ok"] is False

    no_draft = store.upsert_contact(_emailed_contact(
        linkedin_url="https://l/in/other", full_name="Other Person",
        followup_subject="", followup_message=""))
    r = gmail_send.send_followup(no_draft)
    assert r["ok"] is False and "draft" in r["message"]


def test_followup_blocked_once_stopped_or_replied(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    for status in ("stopped", "replied"):
        cid = store.upsert_contact(_emailed_contact(
            full_name=f"P {status}", linkedin_url=f"https://l/in/{status}",
            followup_status=status))
        r = gmail_send.send_followup(cid)
        assert r["ok"] is False and status in r["message"]


def test_followup_claim_is_atomic(tmp_path, monkeypatch):
    """Two clicks must not produce two follow-ups."""
    _fresh_db(tmp_path, monkeypatch)
    cid = store.upsert_contact(_emailed_contact())
    assert store.claim_followup_send(cid) is True
    assert store.claim_followup_send(cid) is False


def test_followup_warns_when_the_original_cannot_be_threaded(tmp_path, monkeypatch):
    """Emails sent before threading existed have no ids — say so instead of silently not threading."""
    _fresh_db(tmp_path, monkeypatch)
    from applypilot.networking import gmail_oauth
    monkeypatch.setattr(gmail_oauth, "available", lambda: True)
    monkeypatch.setattr(gmail_oauth, "connected_email", lambda: "me@x.com")
    monkeypatch.setattr(gmail_oauth, "send",
                        lambda *a, **k: {"id": "gid-3", "thread_id": "", "rfc_message_id": "<n@x>"})
    cid = store.upsert_contact(_emailed_contact(thread_id="", rfc_message_id=""))
    res = gmail_send.send_followup(cid)
    assert res["ok"] is True and "new email" in res["message"]


def test_attachment_filenames_carry_the_company(tmp_path, monkeypatch):
    """Six tailored resumes all named `..._Resume.pdf` look like one document resent."""
    _fresh_db(tmp_path, monkeypatch)
    (tmp_path / "r.pdf").write_bytes(b"%PDF-r")
    (tmp_path / "c.pdf").write_bytes(b"%PDF-c")
    conn = database.get_connection()
    conn.execute("INSERT INTO jobs (url, company, tailored_resume_path, cover_letter_path) "
                 "VALUES (?,?,?,?)",
                 ("https://job-boards.greenhouse.io/devrev/jobs/1", "DevRev",
                  str(tmp_path / "r.txt"), str(tmp_path / "c.txt")))
    conn.commit()
    monkeypatch.setattr("applypilot.config.load_profile",
                        lambda: {"personal": {"full_name": "Jane Public"}})
    names = [f for _, f in gmail_send.job_attachments("https://job-boards.greenhouse.io/devrev/jobs/1")]
    assert names == ["Jane_Public_Resume_DevRev.pdf", "Jane_Public_Cover_Letter_DevRev.pdf"]


def test_attachment_filename_omits_a_noise_company_slug(tmp_path, monkeypatch):
    """A bare host derives a 1-char 'company' — better no suffix than `Resume_J.pdf`."""
    _fresh_db(tmp_path, monkeypatch)
    (tmp_path / "r.pdf").write_bytes(b"%PDF-r")
    conn = database.get_connection()
    conn.execute("INSERT INTO jobs (url, tailored_resume_path) VALUES (?,?)",
                 ("http://j/1", str(tmp_path / "r.txt")))
    conn.commit()
    monkeypatch.setattr("applypilot.config.load_profile",
                        lambda: {"personal": {"full_name": "Jane Public"}})
    assert [f for _, f in gmail_send.job_attachments("http://j/1")] == ["Jane_Public_Resume.pdf"]


# ── signature + threading backfill ──────────────────────────────────────────

def test_signature_prefers_local_file_over_api(tmp_path, monkeypatch):
    from applypilot.networking import gmail_oauth
    gmail_send._SIG_CACHE.clear()
    sig_file = tmp_path / "signature.html"
    sig_file.write_text("<b>Local Sig</b>")
    monkeypatch.setattr(gmail_send, "signature_path", lambda: sig_file)
    monkeypatch.setattr(gmail_oauth, "fetch_signature", lambda a="": "<i>From API</i>")
    assert gmail_send.signature_html("me@x.com") == "<b>Local Sig</b>"


def test_signature_falls_back_to_gmail_settings(tmp_path, monkeypatch):
    from applypilot.networking import gmail_oauth
    gmail_send._SIG_CACHE.clear()
    monkeypatch.setattr(gmail_send, "signature_path", lambda: tmp_path / "nope.html")
    monkeypatch.setattr(gmail_oauth, "fetch_signature", lambda a="": "<i>From API</i>")
    assert gmail_send.signature_html("me@x.com") == "<i>From API</i>"


def test_signature_can_be_disabled(tmp_path, monkeypatch):
    gmail_send._SIG_CACHE.clear()
    monkeypatch.setenv("OUTREACH_SIGNATURE", "0")
    monkeypatch.setattr(gmail_send, "signature_path", lambda: tmp_path / "s.html")
    assert gmail_send.signature_html("me@x.com") == ""


def test_body_to_html_escapes_and_keeps_paragraphs():
    from applypilot.networking.gmail_oauth import _body_to_html
    out = _body_to_html("Hi <script>alert(1)</script>\n\nSecond para")
    assert "&lt;script&gt;" in out and "<script>" not in out
    assert out.count("<p>") == 2


def test_backfill_requires_the_read_scope(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    from applypilot.networking import gmail_oauth
    monkeypatch.setattr(gmail_oauth, "has_scope", lambda s: False)
    r = gmail_send.backfill_thread_ids()
    assert r["ok"] is False and "read scope" in r["message"]


def test_backfill_recovers_thread_ids_and_is_idempotent(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    from applypilot.networking import gmail_oauth
    monkeypatch.setattr(gmail_oauth, "has_scope", lambda s: True)
    calls = {"n": 0}

    def fake_info(mid):
        calls["n"] += 1
        return {"thread_id": "thr-" + mid, "rfc_message_id": "<" + mid + "@x>"}
    monkeypatch.setattr(gmail_oauth, "message_thread_info", fake_info)

    cid = store.upsert_contact(_contact(outreach_status="submitted", sent_message_id="gid1"))
    r = gmail_send.backfill_thread_ids()
    assert r["ok"] is True and r["updated"] == 1
    got = store.get_contact(cid)
    assert got["thread_id"] == "thr-gid1" and got["rfc_message_id"] == "<gid1@x>"

    # second run must be a no-op — only rows still MISSING ids are touched
    before = calls["n"]
    r2 = gmail_send.backfill_thread_ids()
    assert r2["updated"] == 0 and calls["n"] == before


def test_backfill_reports_messages_it_cannot_find(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    from applypilot.networking import gmail_oauth
    monkeypatch.setattr(gmail_oauth, "has_scope", lambda s: True)
    monkeypatch.setattr(gmail_oauth, "message_thread_info", lambda mid: {})
    store.upsert_contact(_contact(outreach_status="submitted", sent_message_id="gone"))
    r = gmail_send.backfill_thread_ids()
    assert r["ok"] is True and r["updated"] == 0 and r["missing"] == 1


def test_apply_agent_cannot_read_the_mailbox():
    """The agent browses attacker-controlled pages; inbox read must be doubly blocked."""
    from applypilot.apply import launcher
    allowed = launcher._ALLOWED_TOOLS.split(",")
    denied = launcher._DISALLOWED_TOOLS.split(",")
    for tool in ("mcp__gmail__read_email", "mcp__gmail__search_emails",
                 "mcp__gmail__download_attachment"):
        assert tool not in allowed, f"{tool} must not be allowlisted"
        assert tool in denied, f"{tool} must also be explicitly denied"
    # send is the ONLY gmail capability the agent gets
    assert [t for t in allowed if t.startswith("mcp__gmail__")] == ["mcp__gmail__send_email"]


def test_requested_gmail_scope_is_metadata_not_full_read():
    """gmail.metadata cannot read message bodies; gmail.readonly can. Keep the narrow one."""
    from applypilot.networking import gmail_oauth
    assert gmail_oauth.READ_SCOPE.endswith("gmail.metadata")
    assert not any(s.endswith("gmail.readonly") for s in gmail_oauth.SCOPES)
