"""LDM tests: agent-browser bridge, verbatim controller, send() gates, DB claim/dedupe.

The agent-browser subprocess and the LLM are mocked — no live browser or LinkedIn.
"""

from __future__ import annotations

from pathlib import Path

import applypilot.database as database
from applypilot.networking import dm_prompt, linkedin_dm, store


def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)
    store.init_contacts()


def _contact(**over):
    base = {
        "job_url": "http://j/1", "full_name": "Jane Roe",
        "linkedin_url": "https://www.linkedin.com/in/jane-roe/",
        "linkedin_message": "Hi Jane — I applied to the SEO role and would love to connect.",
        "outreach_status": "drafted", "source": "apollo",
    }
    base.update(over)
    return base


# ── binary discovery precedence ──────────────────────────────────────────────

def test_bin_env_override_wins(tmp_path, monkeypatch):
    fake = tmp_path / "ab"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AGENT_BROWSER_BIN", str(fake))
    assert linkedin_dm.agent_browser_bin() == str(fake)


def test_bin_env_missing_falls_through_to_path(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_BIN", "/nonexistent/agent-browser")
    monkeypatch.setattr(linkedin_dm, "_REPO_BIN", Path("/no/repo"))
    monkeypatch.setattr(linkedin_dm.shutil, "which", lambda _: "/usr/local/bin/agent-browser")
    assert linkedin_dm.agent_browser_bin() == "/usr/local/bin/agent-browser"


def test_bin_none_when_absent(monkeypatch):
    monkeypatch.delenv("AGENT_BROWSER_BIN", raising=False)
    monkeypatch.setattr(linkedin_dm, "_REPO_BIN", Path("/no/repo"))
    monkeypatch.setattr(linkedin_dm.shutil, "which", lambda _: None)
    monkeypatch.setattr(linkedin_dm, "_KNOWN_LOCAL_BIN", Path("/no/such"))
    assert linkedin_dm.agent_browser_bin() is None


# ── controller prompt: verbatim + action space ───────────────────────────────

def test_prompt_carries_verbatim_note_and_send_only_rule():
    sys = dm_prompt.build_system_prompt()
    assert "verbatim" in sys.lower()
    assert "one action" in sys.lower() or "one step" in sys.lower()
    turn = dm_prompt.build_turn_prompt("Jane", "https://x", "SECRET NOTE 123", "snap", [], dry_run=True)
    assert "SECRET NOTE 123" in turn  # the exact note is shown, never rewritten
    assert "DRY-RUN" in turn


def test_prompt_describes_connection_request_path():
    """Path A (Connect + note) must be present — it's the flow for non-connections."""
    sys = dm_prompt.build_system_prompt().lower()
    assert "connect" in sys and "add a note" in sys
    assert "300" in sys  # note fits the invitation character limit


def test_parse_action_rejects_unknown():
    assert dm_prompt.parse_action('{"action":"navigate","url":"evil"}')["action"] == "abort"
    assert dm_prompt.parse_action("garbage")["action"] == "abort"
    assert dm_prompt.parse_action('{"action":"send","ref":"@e1"}')["action"] == "send"


# ── DB: atomic claim, dedupe, daily cap ──────────────────────────────────────

def test_claim_dm_send_single_winner(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    cid = store.upsert_contact(_contact())
    assert store.claim_dm_send(cid) is True
    assert store.claim_dm_send(cid) is False  # already claimed
    # failure rolls back so it can be retried
    store.mark_dm_failed(cid, "boom")
    assert store.claim_dm_send(cid) is True


def test_already_dmed_normalizes_url(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    cid = store.upsert_contact(_contact())
    store.claim_dm_send(cid)
    store.mark_dm_sent(cid)
    # different job, same person (trailing slash / query differences) → deduped
    other = store.upsert_contact(_contact(job_url="http://j/2",
                                          linkedin_url="https://www.linkedin.com/in/jane-roe?trk=x"))
    assert store.already_dmed("https://www.linkedin.com/in/jane-roe/", exclude_id=other)
    assert store.dm_sent_today() == 1


# ── send() refusal paths (no browser touched) ────────────────────────────────

def _patch_bin(monkeypatch, present=True):
    monkeypatch.setattr(linkedin_dm, "agent_browser_bin",
                        lambda: "/bin/agent-browser" if present else None)
    # Default: Chrome not running (profile free), and _open succeeds. Individual tests override.
    monkeypatch.setattr(linkedin_dm, "_chrome_running", lambda: False)
    monkeypatch.setattr(linkedin_dm, "_open", lambda url, headed=True, timeout=60: (0, "ok"))


def test_send_refuses_without_binary(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_bin(monkeypatch, present=False)
    res = linkedin_dm.send(_contact(), dry_run=True)
    assert not res["ok"] and "agent-browser" in res["message"]


def test_send_refuses_without_consent(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_bin(monkeypatch)
    monkeypatch.setattr(linkedin_dm, "has_consent", lambda: False)
    res = linkedin_dm.send(_contact(), dry_run=True)
    assert not res["ok"] and "consent" in res["message"].lower()


def test_send_refuses_live_when_disabled(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_bin(monkeypatch)
    monkeypatch.setattr(linkedin_dm, "has_consent", lambda: True)
    monkeypatch.setattr(linkedin_dm, "enabled", lambda: False)
    res = linkedin_dm.send(_contact(), dry_run=False)
    assert not res["ok"] and "NETWORKING_LINKEDIN_DM" in res["message"]


def test_send_refuses_without_note_or_url(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_bin(monkeypatch)
    monkeypatch.setattr(linkedin_dm, "has_consent", lambda: True)
    assert not linkedin_dm.send(_contact(linkedin_message=""), dry_run=True)["ok"]
    assert not linkedin_dm.send(_contact(linkedin_url=""), dry_run=True)["ok"]


def test_send_refuses_when_session_not_authenticated(tmp_path, monkeypatch):
    """If the opened session lands on a login wall (e.g. profile locked by another Chrome)."""
    _fresh_db(tmp_path, monkeypatch)
    _patch_bin(monkeypatch)
    monkeypatch.setattr(linkedin_dm, "has_consent", lambda: True)
    # _open succeeds but the session is NOT authenticated (get url → login wall).
    monkeypatch.setattr(linkedin_dm, "_ab",
                        lambda args, timeout=45: (0, "https://www.linkedin.com/login/"))
    res = linkedin_dm.send(_contact(), dry_run=True)
    assert not res["ok"] and "authenticated" in res["message"].lower()


# ── dry-run controller: composes verbatim, never sends ───────────────────────

def test_dry_run_composes_verbatim_and_never_clicks_send(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _patch_bin(monkeypatch)
    monkeypatch.setattr(linkedin_dm, "has_consent", lambda: True)
    monkeypatch.setattr(linkedin_dm, "is_logged_in", lambda: True)

    calls = []

    def fake_ab(args, timeout=45):
        calls.append(list(args))
        if args[0] == "snapshot":
            return 0, "button 'Message' @e1\ntextbox 'Write a message' @e2\nbutton 'Send' @e3"
        if args[:2] == ["get", "url"]:
            return 0, "https://www.linkedin.com/in/jane-roe/"
        return 0, "ok"

    monkeypatch.setattr(linkedin_dm, "_ab", fake_ab)

    # LLM: click Message → click composer → type_message → send
    script = iter([
        '{"action":"click","ref":"@e1","why":"open composer"}',
        '{"action":"click","ref":"@e2","why":"focus box"}',
        '{"action":"type_message","why":"focused"}',
        '{"action":"send","ref":"@e3","why":"send it"}',
    ])

    class FakeClient:
        def chat(self, *a, **k):
            return next(script)

    monkeypatch.setattr("applypilot.llm.get_client", lambda: FakeClient())

    contact = _contact()
    cid = store.upsert_contact(contact)
    contact["id"] = cid
    res = linkedin_dm.send(contact, dry_run=True)

    assert res["ok"] and res["status"] == "drafted"
    # The verbatim note was inserted via keyboard inserttext with the EXACT text.
    inserts = [c for c in calls if c[:2] == ["keyboard", "inserttext"]]
    assert inserts and inserts[0][2] == contact["linkedin_message"]
    # No click on the Send ref (@e3) happened in dry-run.
    assert ["click", "@e3"] not in calls
    # Dry-run must not persist a 'sent' state.
    assert store.get_contact(cid)["dm_status"] in (None, "none")


def test_dry_run_connection_request_path(tmp_path, monkeypatch):
    """Path A: invite dialog → deterministic Add-a-note → insert verbatim → Send NOT clicked."""
    _fresh_db(tmp_path, monkeypatch)
    _patch_bin(monkeypatch)
    monkeypatch.setattr(linkedin_dm, "has_consent", lambda: True)
    monkeypatch.setattr(linkedin_dm, "is_logged_in", lambda: True)

    calls = []
    state = {"note_open": False}

    def fake_ab(args, timeout=45):
        calls.append(list(args))
        if args[0] == "snapshot":
            if state["note_open"]:  # after "Add a note" click: textarea + Send button
                return 0, ('textbox "" [ref=e50]\nbutton "Send invitation" [ref=e51]')
            # first screen: Add a note / Send without a note, plus the top-nav search box
            return 0, ('button "Add a note" [ref=e42]\n'
                       'button "Send without a note" [ref=e43]\n'
                       'textbox "I\'m looking for…" [ref=e2]')
        if args[:2] == ["get", "url"]:
            return 0, "https://www.linkedin.com/in/jane-roe/"
        if args[:2] == ["click", "@e42"]:  # clicking Add a note opens the note textarea
            state["note_open"] = True
            return 0, "ok"
        return 0, "ok"

    monkeypatch.setattr(linkedin_dm, "_ab", fake_ab)

    contact = _contact()
    cid = store.upsert_contact(contact)
    contact["id"] = cid
    res = linkedin_dm.send(contact, dry_run=True)

    assert res["ok"] and res["status"] == "drafted"
    # "Add a note" WAS clicked; the note was inserted VERBATIM; the search box was NOT typed into.
    assert ["click", "@e42"] in calls
    inserts = [c for c in calls if c[:2] == ["keyboard", "inserttext"]]
    assert inserts and inserts[0][2] == contact["linkedin_message"]
    assert ["click", "@e2"] not in calls          # never focus the search box
    assert ["click", "@e51"] not in calls         # Send invitation NOT clicked in dry-run
