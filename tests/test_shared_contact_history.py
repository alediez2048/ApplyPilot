"""One person, two cards: the second must not offer a cold email.

`store.contact_id()` hashes `(job_url, linkedin_url, name)`, so the same human found for a second
role becomes a second contact row — and `messages` is keyed on `contact_id`, so all the history
stays on the first. Opening the second card showed a compose box for somebody already
mid-conversation, which is how the same person gets written to twice (CO-1's keying half).

Measured live: **5 cards** carry no messages while the same address has a real conversation on
another row, one of them nine messages deep with three replies. **Zero** addresses have history
on both rows, which is what makes surfacing it safe rather than a merge.

The browser side is in `test_email_channel_history.py` and it supplies its own `thread_from`
fixture — so it proves the rendering and nothing about where that flag comes from. Six mutations
survived it, every one of them here (§Lessons 103, twice in one sitting).
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.networking import messages as msg_store, store

ME = "me@work.test"
THEM = "p@bigco.test"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    msg_store.init_messages(conn)
    monkeypatch.setattr("applypilot.networking.gmail_send._our_addresses", lambda: [ME])
    return conn


@pytest.fixture()
def two_cards(db):
    """The same person on two roles. The FIRST has the conversation; the second has nothing."""
    first = store.upsert_contact({"job_url": "http://j/role-a", "full_name": "Patrick",
                                  "email": THEM, "outreach_status": "submitted"}, db)
    second = store.upsert_contact({"job_url": "http://j/role-b", "full_name": "Patrick Omalley",
                                   "email": THEM, "outreach_status": "drafted"}, db)
    # Somebody else entirely, on one card only — they must never appear in the map. Their
    # message uses their REAL contact id: with a dangling one the JOIN drops the row on its own
    # and the fixture, not the HAVING clause, is what makes the assertion pass.
    solo = store.upsert_contact({"job_url": "http://j/role-a", "full_name": "Solo",
                                 "email": "solo@bigco.test", "outreach_status": "submitted"}, db)
    msg_store.upsert_messages([
        {"message_id": "m1", "thread_id": "t9", "contact_id": first, "job_url": "http://j/role-a",
         "direction": "out", "from_addr": ME, "from_name": "", "to_addrs": [THEM],
         "cc_addrs": [], "subject": "quick q about role A", "sent_at": "2026-08-04T13:49",
         "rfc_message_id": "<m1>", "snippet": "quick q"},
        {"message_id": "m2", "thread_id": "t9", "contact_id": first, "job_url": "http://j/role-a",
         "direction": "in", "from_addr": THEM, "from_name": "Patrick", "to_addrs": [ME],
         "cc_addrs": [], "subject": "Re: quick q about role A", "sent_at": "2026-08-07T08:32",
         "rfc_message_id": "<m2>", "snippet": "happy to help"},
        {"message_id": "m3", "thread_id": "s1", "contact_id": solo, "job_url": "http://j/role-a",
         "direction": "in", "from_addr": "solo@bigco.test", "from_name": "Solo",
         "to_addrs": [ME], "cc_addrs": [], "subject": "hello", "sent_at": "2026-08-08T08:00",
         "rfc_message_id": "<m3>", "snippet": "hi"},
    ], db)
    return first, second


# ── the query ───────────────────────────────────────────────────────────────

def test_only_addresses_on_MORE_THAN_ONE_row_are_collected(two_cards, db):
    """Restricted on purpose: the ordinary card pays nothing, and the map is hoisted above the
    job loop on a 2.5s refresh, so it has to stay small."""
    got = msg_store.threads_by_shared_address(db)
    assert set(got) == {THEM}, f"the map is not restricted to duplicates: {sorted(got)}"
    assert len(got[THEM]) == 2


def test_it_lives_in_the_repository_not_the_dashboard():
    """ARCH-4: `web_dashboard.py` runs zero SQL. The first version of this put the query there
    and `test_web_dashboard_runs_no_sql_at_all` caught it."""
    import inspect
    from applypilot import web_dashboard as wd
    assert "SELECT" not in inspect.getsource(wd._sibling_threads).upper()


# ── which thread a card gets ────────────────────────────────────────────────

def test_the_second_card_borrows_the_conversation(two_cards, db):
    """The report: opening the second Google card offered a fresh cold email to somebody nine
    messages into a conversation."""
    from applypilot import web_dashboard as wd
    _first, second = two_cards
    c = dict(store.get_contact(second, db))
    got = wd._thread_for(c, {}, wd._sibling_threads(db))
    assert len(got) == 2, "the second card still shows an empty history"
    assert all(m["from_other_role"] == "http://j/role-a" for m in got), \
        "borrowed history is not marked with the card it belongs to"


def test_a_card_that_OWNS_messages_keeps_only_its_own(two_cards, db):
    """Silently interleaving two roles' correspondence is a worse answer than showing the one
    this card owns — and live, no address has history on both, so the merge never arises."""
    from applypilot import web_dashboard as wd
    first, _second = two_cards
    own = msg_store.thread_for_contact(first, db)
    got = wd._thread_for(dict(store.get_contact(first, db)), {first: own},
                         wd._sibling_threads(db))
    assert len(got) == 2
    assert not any(m.get("from_other_role") for m in got), \
        "a card's own conversation was marked as borrowed"


def test_a_contact_with_no_address_borrows_nothing(db):
    """The join is on the address. Without a guard, an empty one would match every other
    contact that also has none."""
    from applypilot import web_dashboard as wd
    got = wd._thread_for({"id": "x", "email": ""}, {}, {"": [{"message_id": "z"}]})
    assert got == []


# ── what the payload says, which is what the browser branches on ────────────

def test_a_borrowed_card_is_offered_NO_composer(two_cards, db):
    """The dangerous half. `send_reply` resolves recipients from this row's OWN messages, of
    which there are none — so a reply box would render, look entirely normal, and refuse on
    click. Worse than absent (§Lessons 88)."""
    from applypilot import web_dashboard as wd
    _first, second = two_cards
    c = dict(store.get_contact(second, db))
    th = wd._thread_for(c, {}, wd._sibling_threads(db))
    payload = wd._contact_payload(c, company="BigCo", ladders={}, conn_matches={}, thread=th)
    assert payload["reply_targets"] == {}, "offered a composer that cannot send"
    assert payload["thread_from"] == "http://j/role-a", \
        "the payload does not say the conversation belongs elsewhere"


def test_the_card_that_owns_it_still_gets_its_composers(two_cards, db):
    """The negative case: suppressing composers on borrowed threads must not suppress them on
    the real conversation."""
    from applypilot import web_dashboard as wd
    first, _second = two_cards
    c = dict(store.get_contact(first, db))
    own = msg_store.thread_for_contact(first, db)
    payload = wd._contact_payload(c, company="BigCo", ladders={}, conn_matches={},
                                  thread=wd._thread_for(c, {first: own}, wd._sibling_threads(db)))
    assert payload["thread_from"] == ""
    assert set(payload["reply_targets"]) == {"t9"}


def test_is_borrowed_reads_the_source_and_not_merely_a_flag(db):
    from applypilot.web_dashboard import _is_borrowed
    assert _is_borrowed([]) == ""
    assert _is_borrowed(None) == ""
    assert _is_borrowed([{"from_other_role": ""}]) == ""
    assert _is_borrowed([{"from_other_role": "http://j/x"}]) == "http://j/x"


def test_it_actually_reaches_the_status_payload(two_cards, db, monkeypatch):
    """The wiring, from what the SERVER produces rather than from a hand-built argument.

    Every test above calls `_thread_for` with an explicit sibling map, so all of them pass on a
    `/api/status` that never looks siblings up — which is exactly the mutation that survived
    them. §Lessons 39: a value being computed is not evidence it is used.
    """
    from applypilot import web_dashboard as wd
    from applypilot.repo import jobs as _jobs
    _first, second = two_cards
    monkeypatch.setattr(wd, "get_connection", lambda *a, **k: db)
    monkeypatch.setattr(wd, "init_db", lambda *a, **k: db)
    monkeypatch.setattr(wd.config, "load_env", lambda *a, **k: None)
    for url, title in [("http://j/role-a", "Role A"), ("http://j/role-b", "Role B")]:
        _jobs.insert_imported(url, title, "BigCo", "BigCo", url, db)

    payload = wd._status_payload()
    cards = {c["id"]: c for j in payload.get("jobs", []) for c in (j.get("contacts") or [])}
    assert second in cards, "the second card is missing from the payload entirely"
    assert cards[second]["thread_from"] == "http://j/role-a", \
        "/api/status never looked up the sibling conversation"
    assert len(cards[second]["thread"]) == 2
