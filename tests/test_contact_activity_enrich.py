"""CTX-5: pasted public activity becomes contact-level draft context."""

from __future__ import annotations

import applypilot.database as database
from applypilot.networking import service, store


def _db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    return database.get_connection(path)


def _contact(conn, **over):
    store.init_contacts(conn)
    row = {
        "job_url": "http://jobs.example/acme/1",
        "full_name": "Jane Smith",
        "title": "Head of AI",
        "company": "Acme",
        "email": "jane@acme.test",
        "email_status": "verified",
    }
    row.update(over)
    return store.upsert_contact(row, conn)


class _LLM:
    def chat(self, messages, max_tokens=0, temperature=0):  # noqa: ANN001
        self.messages = messages
        self.max_tokens = max_tokens
        self.temperature = temperature
        return "Recently discussed AI support agents and hiring for platform engineering."


def test_contact_activity_enrich_rejects_empty_paste(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cid = _contact(conn)

    res = service.enrich_contact_activity(cid, "   ")

    assert res["ok"] is False
    assert "Paste recent activity" in res["message"]
    assert not (store.get_contact(cid, conn)["noticed"] or "").strip()


def test_contact_activity_enrich_summarizes_and_saves_noticed(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cid = _contact(conn)
    llm = _LLM()

    import applypilot.llm
    monkeypatch.setattr(applypilot.llm, "get_client", lambda tier="light": llm)

    res = service.enrich_contact_activity(cid, "Jane posted about AI support agents.")

    assert res["ok"] is True
    assert res["summary"].startswith("Recently discussed AI support agents")
    row = store.get_contact(cid, conn)
    assert row["noticed"] == res["summary"]
    prompt = "\n".join(m["content"] for m in llm.messages)
    assert "Do not quote or mimic" in prompt
    assert "protected-class" in prompt


def test_contact_activity_enrich_requires_replace_for_existing_context(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    cid = _contact(conn, noticed="Already has context.")
    llm = _LLM()

    import applypilot.llm
    monkeypatch.setattr(applypilot.llm, "get_client", lambda tier="light": llm)

    blocked = service.enrich_contact_activity(cid, "new activity")
    replaced = service.enrich_contact_activity(cid, "new activity", replace=True)

    assert blocked["ok"] is False
    assert blocked["needs_replace"] is True
    assert replaced["ok"] is True
    assert store.get_contact(cid, conn)["noticed"] == replaced["summary"]

