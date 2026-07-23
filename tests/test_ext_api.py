"""EXT-0 local API: queue eligibility (per-job + all-jobs dedupe), status transitions,
note cap, shared-token auth, loopback / chrome-extension origin guards."""

from __future__ import annotations

import applypilot.config as config
import applypilot.database as database
import applypilot.web_dashboard as wd
from applypilot.networking import store


def _fresh(tmp_path, monkeypatch):
    """Isolated DB + APP_DIR (so the ext_token file lands in the temp dir)."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    database.close_connection(db)
    database.init_db(db)
    store.init_contacts()


def _c(**over):
    base = {"job_url": "http://j/1", "full_name": "P", "title": "Eng",
            "company": "Acme", "email": "p@x.com", "email_status": "verified",
            "outreach_message": "hi", "outreach_status": "drafted",
            "linkedin_url": "https://www.linkedin.com/in/p", "linkedin_message": "hey",
            "source": "apollo"}
    base.update(over)
    return base


class _FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler for guard helpers (only .headers used)."""

    def __init__(self, headers: dict):
        self.headers = headers


# ── /api/ext/queue eligibility ───────────────────────────────────────────────

def test_queue_per_job_ready_only(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ready = store.upsert_contact(_c(full_name="R", linkedin_url="https://www.linkedin.com/in/r"))
    store.upsert_contact(_c(full_name="N", linkedin_url="", email="n@x.com"))       # no LI url
    store.upsert_contact(_c(full_name="X", linkedin_message="",
                            linkedin_url="https://www.linkedin.com/in/x"))          # no note
    sent = store.upsert_contact(_c(full_name="S", linkedin_url="https://www.linkedin.com/in/s"))
    store.mark_dm_sent(sent)                                                        # done
    res = wd._ext_queue("http://j/1")
    assert res["ok"] is True
    ids = [c["id"] for c in res["contacts"]]
    assert ids == [ready]
    row = res["contacts"][0]
    assert row["note"] == "hey" and row["company"] == "Acme" and row["title"] == "Eng"
    assert row["linkedin_url"] == "https://www.linkedin.com/in/r"


def test_queue_composed_stays_eligible(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    composed = store.upsert_contact(_c(full_name="C", linkedin_url="https://www.linkedin.com/in/c"))
    store.mark_dm_composed(composed)
    ids = [c["id"] for c in wd._ext_queue("http://j/1")["contacts"]]
    assert ids == [composed]  # composed is NOT done — human hasn't sent yet


def test_queue_all_jobs_excludes_done_and_dedupes(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    # Same person under two different jobs (differing trailing slash / case) → one row.
    store.upsert_contact(_c(job_url="http://j/1", full_name="Dup",
                            linkedin_url="https://www.linkedin.com/in/dup/"))
    store.upsert_contact(_c(job_url="http://j/2", full_name="Dup",
                            linkedin_url="https://www.linkedin.com/in/DUP"))
    # A distinct ready person on another job.
    other = store.upsert_contact(_c(job_url="http://j/2", full_name="Other",
                                    linkedin_url="https://www.linkedin.com/in/other"))
    # `manual` (genuinely invited) must NOT appear. `skipped` MUST still appear — auto-skip is a
    # false-positive trap, so the queue only retires sent/manual, never skipped.
    m = store.upsert_contact(_c(job_url="http://j/3", full_name="M",
                                linkedin_url="https://www.linkedin.com/in/m"))
    sk = store.upsert_contact(_c(job_url="http://j/3", full_name="Sk",
                                 linkedin_url="https://www.linkedin.com/in/sk"))
    store.mark_dm_manual(m)
    store.mark_dm_skipped(sk)

    res = wd._ext_queue(None)  # all-jobs
    assert res["ok"] is True
    urls = sorted(store._norm_linkedin(c["linkedin_url"]) for c in res["contacts"])
    # dup collapsed to one; manual excluded; skipped ('sk') still present.
    assert urls == ["https://www.linkedin.com/in/dup",
                    "https://www.linkedin.com/in/other",
                    "https://www.linkedin.com/in/sk"]
    assert other in {c["id"] for c in res["contacts"]}
    assert m not in {c["id"] for c in res["contacts"]}  # manual is retired
    assert sk in {c["id"] for c in res["contacts"]}      # skipped re-appears


# ── /api/ext/status transitions ──────────────────────────────────────────────

def test_status_sent_and_manual_stamp_dm_sent_at(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    a = store.upsert_contact(_c(full_name="A", linkedin_url="https://www.linkedin.com/in/a"))
    b = store.upsert_contact(_c(job_url="http://j/2", full_name="B",
                                linkedin_url="https://www.linkedin.com/in/b"))
    payload, code = wd._ext_status({"contact_id": a, "status": "sent"})
    assert code == 200 and payload == {"ok": True}
    ra = store.get_contact(a)
    assert ra["dm_status"] == "sent" and ra["dm_sent_at"]
    assert store.already_dmed("https://www.linkedin.com/in/a")  # dedupe now sees it

    payload, code = wd._ext_status({"contact_id": b, "status": "manual"})
    assert code == 200
    assert store.get_contact(b)["dm_sent_at"]  # manual stamps too


def test_status_skipped_stamps_nothing(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    c = store.upsert_contact(_c(linkedin_url="https://www.linkedin.com/in/z"))
    payload, code = wd._ext_status({"contact_id": c, "status": "skipped"})
    assert code == 200
    row = store.get_contact(c)
    assert row["dm_status"] == "skipped" and not row["dm_sent_at"]


def test_status_rejects_bad_input(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    c = store.upsert_contact(_c(linkedin_url="https://www.linkedin.com/in/z"))
    assert wd._ext_status({"contact_id": c, "status": "bogus"})[1] == 400
    assert wd._ext_status({"status": "sent"})[1] == 400            # missing contact_id
    assert wd._ext_status({"contact_id": "nope", "status": "sent"})[1] == 404


# ── /api/ext/note ─────────────────────────────────────────────────────────────

def test_note_caps_at_300_and_persists(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    cid = store.upsert_contact(_c(linkedin_url="https://www.linkedin.com/in/n",
                                  linkedin_message="old"))
    long = "x" * 500
    payload, code = wd._ext_note({"contact_id": cid, "note": long})
    assert code == 200
    assert payload["ok"] is True and len(payload["note"]) == 300
    assert store.get_contact(cid)["linkedin_message"] == "x" * 300
    # It surfaces on the next queue fetch.
    row = wd._ext_queue("http://j/1")["contacts"][0]
    assert row["note"] == "x" * 300


def test_note_does_not_clobber_email_state(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    cid = store.upsert_contact(_c(outreach_subject="Subj", outreach_message="Body",
                                  outreach_status="drafted",
                                  linkedin_url="https://www.linkedin.com/in/n"))
    wd._ext_note({"contact_id": cid, "note": "new note"})
    row = store.get_contact(cid)
    assert row["linkedin_message"] == "new note"
    assert row["outreach_subject"] == "Subj" and row["outreach_message"] == "Body"
    assert row["outreach_status"] == "drafted"  # untouched (not _save_or_regen_draft)


def test_note_rejects_missing_or_unknown(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    assert wd._ext_note({"note": "x"})[1] == 400
    assert wd._ext_note({"contact_id": "nope", "note": "x"})[1] == 404


# ── auth: shared token ────────────────────────────────────────────────────────

def test_token_created_and_stable(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    tok = wd._ext_token()
    assert tok and (tmp_path / "ext_token").read_text().strip() == tok
    assert wd._ext_token() == tok  # stable across reads


def test_token_ok_guard(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    tok = wd._ext_token()
    assert wd._ext_token_ok(_FakeHandler({wd.EXT_TOKEN_HEADER: tok})) is True
    assert wd._ext_token_ok(_FakeHandler({wd.EXT_TOKEN_HEADER: "wrong"})) is False
    assert wd._ext_token_ok(_FakeHandler({})) is False  # missing header


# ── auth: loopback + origin guards ────────────────────────────────────────────

def test_host_loopback_guard():
    assert wd._host_is_loopback(_FakeHandler({"Host": "localhost:8765"})) is True
    assert wd._host_is_loopback(_FakeHandler({"Host": "127.0.0.1:8765"})) is True
    assert wd._host_is_loopback(_FakeHandler({"Host": "evil.example.com:8765"})) is False


def test_ext_origin_guard():
    # chrome-extension origin allowed (identity proven by token, not a hardcoded id).
    assert wd._ext_origin_ok(_FakeHandler({"Origin": "chrome-extension://abcdef"})) is True
    assert wd._ext_origin_ok(_FakeHandler({"Origin": "http://localhost:8765"})) is True
    assert wd._ext_origin_ok(_FakeHandler({})) is True  # no Origin (non-browser) — token gates it
    assert wd._ext_origin_ok(_FakeHandler({"Origin": "https://evil.example.com"})) is False
