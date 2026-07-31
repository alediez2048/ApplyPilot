"""Per-contact intro-deck links — who actually read it.

Email OPEN tracking was considered and rejected, and the reasoning belongs here because it is
what this design is instead of. A tracking pixel fires when Gmail proxies and caches the image
on delivery, when Apple Mail Privacy Protection pre-fetches it (default-on since iOS 15), and
when a corporate gateway scans the message. You would record "Gina opened your email" from her
employer's spam filter and be unable to tell it from a read. A confidently wrong signal is worse
than none: it drives follow-up decisions the data cannot support.

A click is different. No spam filter follows a link and reads a deck. And because the deck is on
the sender's OWN site, it needs no pixel, no third-party analytics, and nothing hidden in the
message beyond a link the recipient can see.
"""

from __future__ import annotations

import applypilot.database as database
import pytest

from applypilot.domain import deck
from applypilot.networking import store

SECRET = "test-install-secret"
BASE = "https://www.jorgealejandrodiez.com/intro/"


# ── the token ────────────────────────────────────────────────────────────────────────────

def test_a_token_is_stable_for_the_same_contact():
    """Derived, not stored. A database restore, a re-discovered contact, or a second machine
    must all reproduce the token — the links are already sitting in people's inboxes and there
    is no lookup table that can fall out of step with them."""
    assert deck.token_for("c1", SECRET) == deck.token_for("c1", SECRET)
    assert deck.token_for("c1", SECRET) != deck.token_for("c2", SECRET)
    assert len(deck.token_for("c1", SECRET)) == deck.TOKEN_LEN


def test_the_secret_is_what_makes_tokens_unguessable():
    """Contact ids are a hash of (job, identity) — reproducible by anyone who knows the inputs.
    A bare hash of the id would let the whole token space be enumerated from a guess at the
    scheme; the per-install secret is the only thing preventing that."""
    assert deck.token_for("c1", "secret-a") != deck.token_for("c1", "secret-b")


def test_no_token_for_a_contact_with_no_id():
    assert deck.token_for("", SECRET) == ""
    assert deck.token_for(None, SECRET) == ""


# ── the URL ──────────────────────────────────────────────────────────────────────────────

def test_the_token_is_appended_without_breaking_an_existing_query():
    assert deck.deck_url(BASE, "abc123de") == f"{BASE}?v=abc123de"
    assert deck.deck_url("https://x.com/i?utm=li", "abc123de") == "https://x.com/i?utm=li&v=abc123de"


def test_a_missing_token_leaves_the_link_working():
    """An un-attributed click is a much smaller loss than a broken deck link."""
    assert deck.deck_url(BASE, "") == BASE
    assert deck.deck_url("", "abc123de") == ""


def test_a_token_is_not_added_twice():
    once = deck.deck_url(BASE, "abc123de")
    assert deck.deck_url(once, "abc123de") == once


def test_the_token_can_be_stripped_for_comparison():
    assert deck.strip_token(f"{BASE}?v=abc123de") == BASE.rstrip("?&")
    assert deck.strip_token(f"{BASE}?utm=li&v=abc123de") == f"{BASE}?utm=li"


# ── the import ───────────────────────────────────────────────────────────────────────────

def test_tokens_are_found_in_any_log_format():
    """Scanning for the SHAPE rather than parsing one provider is what lets this accept a
    Plausible export, a Vercel log and a pasted list without a per-provider adapter."""
    blob = """
    2026-07-31T10:00:00Z GET /intro/?v=aaaaaaaa 200
    "path","visitors"
    "/intro/?v=bbbbbbbb","3"
    https://www.jorgealejandrodiez.com/intro/?utm_source=email&v=cccccccc
    """
    assert deck.tokens_in(blob) == {"aaaaaaaa", "bbbbbbbb", "cccccccc"}


def test_a_random_hex_string_is_not_mistaken_for_a_visitor():
    """Logs are full of 8-hex request ids and git shas. Only our query parameter counts."""
    assert deck.tokens_in("commit deadbeef built in 4s, trace_id=abcdef12") == set()
    assert deck.tokens_in("GET /other?id=aaaaaaaa") == set()
    assert deck.tokens_in("") == set()
    assert deck.tokens_in(None) == set()


def test_tokens_map_back_to_the_right_people():
    contacts = [{"id": "c1", "full_name": "Gina"}, {"id": "c2", "full_name": "Victoria"}]
    hits = deck.match_contacts({deck.token_for("c2", SECRET)}, contacts, SECRET)
    assert [c["full_name"] for c in hits] == ["Victoria"]


def test_an_unknown_token_is_ignored_not_an_error():
    """Most likely a deleted contact or a link from a different install. Neither is worth
    failing an import over."""
    contacts = [{"id": "c1", "full_name": "Gina"}]
    assert deck.match_contacts({"ffffffff"}, contacts, SECRET) == []


# ── recording ────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    conn = database.get_connection(path)
    store.init_contacts(conn)
    return conn


def test_the_first_click_is_reported_once_and_only_once(db):
    """Re-importing the same analytics export must not re-announce the click. Exactly the
    idempotence lesson that produced eleven identical BOUNCED log lines (§Lessons 22)."""
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina",
                                "email": "g@co.com"}, db)
    assert store.mark_deck_viewed(cid, conn=db) is True, "first click was not reported as new"
    assert store.mark_deck_viewed(cid, conn=db) is False
    assert store.mark_deck_viewed(cid, conn=db) is False

    c = store.get_contact(cid, db)
    assert c["deck_views"] == 3, "repeat clicks should still count"
    assert c["deck_viewed_at"], "the first click time was not kept"


def test_the_first_click_time_never_moves(db):
    """`deck_viewed_at` is the event worth acting on; `deck_last_at` is the recency."""
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina",
                                "email": "g@co.com"}, db)
    store.mark_deck_viewed(cid, at="2026-07-01T10:00:00+00:00", conn=db)
    store.mark_deck_viewed(cid, at="2026-07-20T10:00:00+00:00", conn=db)
    c = store.get_contact(cid, db)
    assert c["deck_viewed_at"].startswith("2026-07-01")
    assert c["deck_last_at"].startswith("2026-07-20")


def test_marking_an_unknown_contact_does_not_invent_one(db):
    assert store.mark_deck_viewed("nope", conn=db) is False


# ── the link that actually goes out ──────────────────────────────────────────────────────

def test_the_outreach_link_carries_the_contact_token(monkeypatch):
    from applypilot import config
    from applypilot.networking import outreach

    monkeypatch.setenv("INTRO_DECK_URL", BASE)
    monkeypatch.setattr(config, "install_secret", lambda: SECRET)

    plain = outreach._intro_deck_url({}, None)
    tagged = outreach._intro_deck_url({}, {"id": "c1"})
    assert plain == BASE, "the un-attributed link must still be the plain deck URL"
    assert tagged == f"{BASE}?v={deck.token_for('c1', SECRET)}"
    # And it round-trips: the link we send is the link the import recognises.
    assert deck.tokens_in(tagged) == {deck.token_for("c1", SECRET)}


def test_a_contact_with_no_id_still_gets_a_working_link(monkeypatch):
    from applypilot.networking import outreach
    monkeypatch.setenv("INTRO_DECK_URL", BASE)
    assert outreach._intro_deck_url({}, {"full_name": "No Id"}) == BASE


def test_the_install_secret_is_stable_across_calls(tmp_path, monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    first = config.install_secret()
    assert first and config.install_secret() == first
    assert (tmp_path / "install_secret").exists()
    import os
    assert oct(os.stat(tmp_path / "install_secret").st_mode)[-3:] == "600"
