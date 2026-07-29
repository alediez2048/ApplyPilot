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


# ── stale hot-contact pruning (self-heal after a matcher fix) ────────────────

def _prune_env(tmp_path, monkeypatch):
    import applypilot.database as database
    from applypilot.networking import connections, store
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)
    conn = database.get_connection(db)
    store.init_contacts(conn)
    connections.init_connections(conn)
    conn.execute("INSERT INTO jobs (url, title, company, site) VALUES (?,?,?,?)",
                 ("http://j/arm", "PM", "Arm", "Arm"))
    # only Waheed genuinely works at Arm
    for name, company in [("Waheed Brown", "Arm"), ("Larissa Tater", "Armanino")]:
        conn.execute(
            "INSERT INTO connections (id, full_name, name_norm, company, company_norm, url) "
            "VALUES (?,?,?,?,?,?)",
            (name, name, connections._norm_name(name), company,
             connections._norm_company(company), f"https://l/in/{name.split()[0].lower()}"))
    conn.commit()
    return database, store, conn


def _add(store, name, **kw):
    return store.upsert_contact({"job_url": "http://j/arm", "full_name": name,
                                 "source": "connection", **kw})


def test_prune_drops_contacts_who_do_not_work_there(tmp_path, monkeypatch):
    from applypilot.networking import service
    database, store, conn = _prune_env(tmp_path, monkeypatch)
    _add(store, "Larissa Tater", email="larissa@armanino.com")   # Armanino, not Arm
    _add(store, "Waheed Brown", email="waheed.brown@arm.com")    # genuinely at Arm

    removed = service._prune_stale_connection_contacts("http://j/arm", "Arm")
    assert removed == ["Larissa Tater"]
    left = {c["full_name"] for c in store.get_contacts_for_job("http://j/arm")}
    assert left == {"Waheed Brown"}


def test_prune_never_touches_a_contact_with_history(tmp_path, monkeypatch):
    """Emailed / invited / hand-annotated rows are kept even when the match is wrong."""
    from applypilot.networking import service
    database, store, conn = _prune_env(tmp_path, monkeypatch)
    _add(store, "Larissa Tater", sent_message_id="<m1@x>")   # was emailed
    _add(store, "Emailed Twin", dm_status="manual")          # invite went out
    _add(store, "Noted Person", phone="+1 555 010 0000")     # you typed a phone in
    _add(store, "Annotated Two", notes="met at a conference")

    assert service._prune_stale_connection_contacts("http://j/arm", "Arm") == []
    assert len(store.get_contacts_for_job("http://j/arm")) == 4


def test_prune_is_a_noop_without_a_known_company(tmp_path, monkeypatch):
    """No employer -> no basis to judge -> never delete."""
    from applypilot.networking import service
    database, store, conn = _prune_env(tmp_path, monkeypatch)
    _add(store, "Larissa Tater")
    assert service._prune_stale_connection_contacts("http://j/arm", None) == []
    assert service._prune_stale_connection_contacts("http://j/arm", "") == []
    assert len(store.get_contacts_for_job("http://j/arm")) == 1


def test_prune_leaves_cold_apollo_contacts_alone(tmp_path, monkeypatch):
    """Apollo found them by domain; the connections table says nothing about them."""
    from applypilot.networking import service
    database, store, conn = _prune_env(tmp_path, monkeypatch)
    store.upsert_contact({"job_url": "http://j/arm", "full_name": "Katarzyna",
                          "source": "apollo"})           # no email, real Arm recruiter
    store.upsert_contact({"job_url": "http://j/arm", "full_name": "Meryl",
                          "source": "apollo", "email": "meryl.xiong@arm.com"})
    assert service._prune_stale_connection_contacts("http://j/arm", "Arm") == []
    assert len(store.get_contacts_for_job("http://j/arm")) == 2
