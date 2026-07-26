"""Hot-contacts layer: your existing LinkedIn connections at the target company.

Cold = Apollo strangers. Hot = people you already know there (from the imported Connections.csv),
enriched via Apollo identity-match and drafted with WARM outreach. These tests cover the pieces
that don't need a live Apollo/LLM call.
"""

from __future__ import annotations

import applypilot.networking.connections as C


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    import importlib
    from applypilot import config as cfg
    importlib.reload(cfg)
    from applypilot import database
    importlib.reload(database)
    importlib.reload(C)
    return database.get_connection()


def _add_conn(conn, name, company):
    from hashlib import sha1
    nn = C._norm_name(name)
    cn = C._norm_company(company)
    cid = sha1(f"{nn}\x1f{cn}".encode()).hexdigest()[:16]
    conn.execute(
        "INSERT OR REPLACE INTO connections (id, full_name, name_norm, company, company_norm, position, url, connected_on, imported_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, name, nn, company, cn, "Engineer", f"https://linkedin.com/in/{nn.replace(' ','')}", "2026-01-01", "2026-01-01"),
    )
    conn.commit()


def test_at_company_returns_your_connections_there(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    C.init_connections(conn)
    _add_conn(conn, "Ada Lovelace", "Google")
    _add_conn(conn, "Alan Turing", "Google, Inc.")   # suffix variant still matches
    _add_conn(conn, "Grace Hopper", "Affirm")         # different company — excluded

    hot = C.at_company("Google", conn=conn)
    names = sorted(h["full_name"] for h in hot)
    assert names == ["Ada Lovelace", "Alan Turing"]
    assert all("company_norm" not in h for h in hot)  # internal field stripped
    assert all(h.get("url") for h in hot)             # carries the profile URL


def test_at_company_empty_for_unknown_company(tmp_path, monkeypatch):
    conn = _fresh(tmp_path, monkeypatch)
    C.init_connections(conn)
    _add_conn(conn, "Ada Lovelace", "Google")
    assert C.at_company("SomeCompanyWeHaveNobodyAt", conn=conn) == []
    assert C.at_company("", conn=conn) == []
    assert C.at_company(None, conn=conn) == []


def test_warm_draft_flag_threads_through(monkeypatch):
    # draft_email(warm=True) must add the warm/reconnect framing to the prompt.
    from applypilot.networking import outreach
    captured = {}

    class _C:
        def chat(self, messages, **k):
            captured["user"] = messages[-1]["content"]
            return '{"subject":"s","body":"b","linkedin_note":"n"}'

    monkeypatch.setattr(outreach, "get_client", lambda *a, **k: _C())
    outreach.draft_email({}, {"title": "Eng"}, {"full_name": "Ada", "company": "Google"}, warm=True)
    assert "WARM" in captured["user"] and "ALREADY CONNECTED" in captured["user"]
    # cold draft must NOT carry the warm block
    captured.clear()
    outreach.draft_email({}, {"title": "Eng"}, {"full_name": "Ada", "company": "Google"}, warm=False)
    assert "ALREADY CONNECTED" not in captured.get("user", "")
