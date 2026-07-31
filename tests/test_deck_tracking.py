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

**The link is a NAMED PATH, not a tracking parameter.** `/intro/gina`, not `/intro/?v=9b83068a`.
Both identify the reader; only one looks like it. The first version used an opaque token, and a
token in a query string is the shape people have been trained to distrust — in the one message
whose whole point is sounding personal. A path segment with their first name looks deliberate,
because it is.
"""

from __future__ import annotations

import applypilot.database as database
import pytest

from applypilot.domain import deck
from applypilot.networking import store

SECRET = "test-install-secret"
BASE = "https://www.jorgealejandrodiez.com/intro/"


# ── the slug ─────────────────────────────────────────────────────────────────────────────

def test_a_name_becomes_a_clean_url_segment():
    """The entire point: the URL has to look like it was written for them."""
    assert deck.slugify("Gina Johnson") == "gina"
    assert deck.slugify("gina") == "gina"
    # Accents folded, not percent-encoded — "%C3%A9" in a link is the ugliness being fixed.
    assert deck.slugify("Renée Dupont") == "renee"
    assert deck.slugify("Jean-Luc Picard") == "jean-luc"


def test_a_slug_never_collides_with_a_real_page():
    """`/intro/assets` and `/intro/index` are (or could become) real paths. A contact named
    "Index" must not shadow one."""
    for reserved in ("index", "assets", "api", "admin"):
        assert deck.slugify(reserved) == ""
    assert deck.slugify("") == "" and deck.slugify(None) == ""


def test_two_people_with_the_same_first_name_stay_distinct():
    """Two Ginas is the normal case, not an edge case — and the older link is already in
    somebody's inbox, so the second person must be the one who moves."""
    assert deck.disambiguate("gina", set(), "Gina Johnson") == "gina"
    assert deck.disambiguate("gina", {"gina"}, "Gina Bavagnoli") == "gina-b"
    # A number only when a last initial cannot help — that is where a friendly URL starts
    # looking like a token again, which is the thing this replaced.
    assert deck.disambiguate("gina", {"gina", "gina-b"}, "Gina Bavagnoli") == "gina-2"
    assert deck.disambiguate("gina", {"gina"}, "Gina") == "gina-2"


def test_the_url_is_a_path_not_a_query():
    assert deck.deck_url("https://x.com/intro/", "gina") == "https://x.com/intro/gina"
    assert deck.deck_url("https://x.com/intro", "gina") == "https://x.com/intro/gina"
    assert "?" not in deck.deck_url("https://x.com/intro/", "gina")
    # An existing query (a utm tag, say) survives rather than being clobbered.
    assert deck.deck_url("https://x.com/intro?utm=li", "gina") == "https://x.com/intro/gina?utm=li"


def test_a_missing_slug_leaves_a_working_link():
    """An un-attributed click is a far smaller loss than a 404 on the deck itself."""
    assert deck.deck_url(BASE, "") == BASE
    assert deck.deck_url("", "gina") == ""


# ── the import ───────────────────────────────────────────────────────────────────────────

def test_visits_are_found_in_any_log_format():
    """Scanning for the SHAPE rather than parsing one provider is what lets this accept a
    Plausible export, a Vercel log and a pasted list without a per-provider adapter."""
    blob = """
    2026-07-31T10:00:00Z GET /intro/gina 200
    "path","visitors"
    "/intro/victoria","3"
    https://www.jorgealejandrodiez.com/intro/david-l?utm_source=email
    """
    assert deck.slugs_in(blob) == {"gina", "victoria", "david-l"}


def test_a_real_subpage_is_not_mistaken_for_a_visitor():
    """A path is a MUCH broader namespace than the old query parameter, so this filter matters
    more than it did: /intro/assets is a directory, not a person."""
    assert deck.slugs_in("GET /intro/assets/deck.png 200") == set()
    assert deck.slugs_in("GET /other/gina 200") == set()
    # And narrowing to slugs actually issued removes the rest.
    assert deck.slugs_in("GET /intro/careers 200", known={"gina"}) == set()
    assert deck.slugs_in("GET /intro/gina 200", known={"gina"}) == {"gina"}
    assert deck.slugs_in("") == set() and deck.slugs_in(None) == set()


def test_slugs_map_back_to_the_right_people():
    contacts = [{"id": "c1", "full_name": "Gina", "deck_slug": "gina"},
                {"id": "c2", "full_name": "Victoria", "deck_slug": "victoria"}]
    assert [c["full_name"] for c in deck.match_contacts({"victoria"}, contacts)] == ["Victoria"]


def test_an_unknown_slug_is_ignored_not_an_error():
    """Most likely a deleted contact or a link from a different install."""
    assert deck.match_contacts({"nobody"}, [{"id": "c1", "deck_slug": "gina"}]) == []


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

def test_the_outreach_link_is_a_named_path(db, monkeypatch):
    """What the recipient actually sees. This is the whole change: `/intro/gina` reads as
    "I made this for you"; `/intro/?v=9b83068a` reads as surveillance."""
    from applypilot.networking import outreach

    monkeypatch.setenv("INTRO_DECK_URL", BASE)
    monkeypatch.setattr(outreach, "log", outreach.log)
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina Johnson",
                                "email": "g@co.com"}, db)
    import applypilot.networking.store as _store_mod
    monkeypatch.setattr(_store_mod, "get_connection", lambda *a, **k: db)

    url = outreach._intro_deck_url({}, {"id": cid, "full_name": "Gina Johnson"})
    assert url == f"{BASE}gina", url
    assert "?" not in url and "=" not in url, "the link still looks like a tracker"
    # It round-trips: the link we send is the visit the import recognises.
    assert deck.slugs_in(url) == {"gina"}


def test_the_slug_is_assigned_once_and_never_moves(db):
    """Links are already sitting in inboxes. A reassigned slug would credit the wrong person
    for a click on a message sent last week."""
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina Johnson",
                                "email": "g@co.com"}, db)
    first = store.ensure_deck_slug(cid, "Gina Johnson", db)
    assert first == "gina"
    # Even if the display name later changes, the issued link keeps working.
    db.execute("UPDATE contacts SET full_name = ? WHERE id = ?", ("Regina Smith", cid))
    db.commit()
    assert store.ensure_deck_slug(cid, "Regina Smith", db) == "gina"


def test_a_second_gina_gets_her_own_link(db):
    a = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina Johnson",
                              "email": "a@x.com"}, db)
    b = store.upsert_contact({"job_url": "http://j/2", "full_name": "Gina Bavagnoli",
                              "email": "b@x.com"}, db)
    assert store.ensure_deck_slug(a, "Gina Johnson", db) == "gina"
    assert store.ensure_deck_slug(b, "Gina Bavagnoli", db) == "gina-b"


def test_a_contact_with_no_usable_name_still_gets_a_working_link(db, monkeypatch):
    """A plain deck link beats a broken one — the conversation matters more than the metric."""
    from applypilot.networking import outreach
    monkeypatch.setenv("INTRO_DECK_URL", BASE)
    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "", "email": "x@y.com"}, db)
    import applypilot.networking.store as _store_mod
    monkeypatch.setattr(_store_mod, "get_connection", lambda *a, **k: db)
    assert outreach._intro_deck_url({}, {"id": cid, "full_name": ""}) == BASE


# ── the pull from the site ───────────────────────────────────────────────────────────────

def test_the_pull_accepts_the_shapes_a_hand_rolled_endpoint_produces():
    """The collector is something the OPERATOR deploys and edits. Rejecting their JSON over a
    key name is a silly way to lose a click."""
    slug = "gina"
    for payload in (
        [slug],
        {"hits": [slug]},
        {"events": [{"slug": slug, "at": "2026-07-31T10:00:00Z"}]},
        {"data": [{"path": f"/intro/{slug}"}]},
        [{"id": slug, "ts": "2026-07-31T10:00:00Z"}],
        [f"https://x.com/intro/{slug}"],           # a raw URL is a legitimate item too
    ):
        assert [h["slug"] for h in deck.hits_from_payload(payload)] == [slug], payload


def test_junk_from_the_collector_yields_nothing_rather_than_raising():
    for payload in (None, "", 42, {"unexpected": 1}, [None, 7, {}], [{"slug": "///"}]):
        assert deck.hits_from_payload(payload) == []


def test_the_pull_is_off_until_both_settings_are_present(monkeypatch):
    from applypilot.networking import deck_hits

    monkeypatch.delenv("DECK_HITS_URL", raising=False)
    monkeypatch.delenv("DECK_HITS_TOKEN", raising=False)
    ok, why = deck_hits.configured()
    assert ok is False and "deck-hits" in why, "the refusal must name the manual fallback"

    monkeypatch.setenv("DECK_HITS_URL", "https://x.com/api/deck-hits")
    ok, why = deck_hits.configured()
    assert ok is False and "TOKEN" in why, (
        "a URL with no token would send an unauthenticated request forever and look like "
        "'nobody clicked'")

    monkeypatch.setenv("DECK_HITS_TOKEN", "s3cret")
    assert deck_hits.configured()[0] is True


def test_a_wrong_token_is_reported_as_a_wrong_token(monkeypatch):
    """401 otherwise looks exactly like 'nobody has clicked' — the §Lessons 15 shape."""
    import urllib.error

    from applypilot.networking import deck_hits

    monkeypatch.setenv("DECK_HITS_URL", "https://x.com/api/deck-hits")
    monkeypatch.setenv("DECK_HITS_TOKEN", "wrong")

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 401, "no", {}, None)

    monkeypatch.setattr(deck_hits.urllib.request, "urlopen", boom)
    hits, err = deck_hits.fetch()
    assert hits == [] and "DECK_HITS_TOKEN" in err


def test_an_unreachable_collector_never_raises(monkeypatch):
    from applypilot.networking import deck_hits

    monkeypatch.setenv("DECK_HITS_URL", "https://x.com/api/deck-hits")
    monkeypatch.setenv("DECK_HITS_TOKEN", "s")
    monkeypatch.setattr(deck_hits.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("dns")))
    hits, err = deck_hits.fetch()
    assert hits == [] and "could not reach" in err


def test_a_plain_text_log_response_still_works(monkeypatch):
    """The endpoint is the operator's. If they serve a log file instead of JSON, scan it."""
    import io

    from applypilot.networking import deck_hits

    monkeypatch.setenv("DECK_HITS_URL", "https://x.com/api/deck-hits")
    monkeypatch.setenv("DECK_HITS_TOKEN", "s")

    class _R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(deck_hits.urllib.request, "urlopen",
                        lambda *a, **k: _R(b"GET /intro/gina 200"))
    hits, err = deck_hits.fetch()
    assert err == "" and [h["slug"] for h in hits] == ["gina"]


def test_polling_records_a_click_and_says_who(db, monkeypatch):
    from applypilot.networking import deck_hits

    cid = store.upsert_contact({"job_url": "http://j/1", "full_name": "Josh Guild",
                                "email": "j@co.com"}, db)
    slug = store.ensure_deck_slug(cid, "Josh Guild", db)
    monkeypatch.setattr(deck_hits, "fetch", lambda: ([{"slug": slug, "at": ""}], ""))

    first = deck_hits.poll(db)
    assert first["new"] == 1 and first["names"] == ["Josh Guild"]
    # Idempotent: the collector keeps a rolling window that we re-read every hour.
    again = deck_hits.poll(db)
    assert again["new"] == 0 and again["recorded"] == 1
    assert store.get_contact(cid, db)["deck_views"] == 2


def test_an_unknown_token_is_never_attributed_to_the_wrong_person(db, monkeypatch):
    """The worst thing this feature could do.

    A token from a deleted contact, a different install, or somebody sharing the link onward
    must record against NOBODY. Attributing it to whichever contact happened to be first would
    put "opened the deck" on a person who never saw it — and the operator would follow up on it.
    """
    from applypilot.networking import deck_hits

    a = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina", "email": "g@co.com"}, db)
    b = store.upsert_contact({"job_url": "http://j/1", "full_name": "Josh", "email": "j@co.com"}, db)
    monkeypatch.setattr(deck_hits, "fetch", lambda: ([{"slug": "nobody", "at": ""}], ""))

    res = deck_hits.poll(db)
    assert res["recorded"] == 0 and res["new"] == 0
    for cid in (a, b):
        c = store.get_contact(cid, db)
        assert not (c["deck_viewed_at"] or ""), f"{c['full_name']} was credited with a click"
        assert not (c["deck_views"] or 0)


def test_a_click_lands_on_the_right_person_when_several_exist(db, monkeypatch):
    """The positive half: with two contacts, only the one whose token arrived is marked."""
    from applypilot.networking import deck_hits

    a = store.upsert_contact({"job_url": "http://j/1", "full_name": "Gina", "email": "g@co.com"}, db)
    b = store.upsert_contact({"job_url": "http://j/1", "full_name": "Josh", "email": "j@co.com"}, db)
    store.ensure_deck_slug(a, "Gina", db)
    slug_b = store.ensure_deck_slug(b, "Josh", db)
    monkeypatch.setattr(deck_hits, "fetch", lambda: ([{"slug": slug_b, "at": ""}], ""))

    assert deck_hits.poll(db)["names"] == ["Josh"]
    assert not (store.get_contact(a, db)["deck_viewed_at"] or "")
    assert store.get_contact(b, db)["deck_viewed_at"]


def test_tick_skips_the_deck_step_when_it_is_not_configured(db, monkeypatch):
    from applypilot import tick

    monkeypatch.delenv("DECK_HITS_URL", raising=False)
    out = tick.run(conn=db)["steps"]["deck"]
    assert out.get("skipped") is True
    assert "deck-hits" in out["detail"], "the skip must name the manual fallback"


def test_tick_never_makes_the_deck_step_fatal(db, monkeypatch):
    """A dead collector must not stop the heartbeat that also polls replies."""
    from applypilot import tick
    from applypilot.networking import deck_hits

    monkeypatch.setenv("DECK_HITS_URL", "https://x.com/api/deck-hits")
    monkeypatch.setenv("DECK_HITS_TOKEN", "s")
    monkeypatch.setattr(deck_hits, "poll",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    out = tick.run(conn=db)
    assert "down" in out["steps"]["deck"]["detail"]
    assert "followups" in out["steps"], "a broken deck step aborted the rest of the tick"


# ── the shape of the link is the feature ─────────────────────────────────────────────────

def test_no_issued_link_ever_looks_like_a_tracker(db, monkeypatch):
    """The requirement, stated as a test: "i no longer want to do the tracking id route, it
    does not look good to be sending urls with weird urls".

    A query parameter, a hex blob, or an opaque id is the shape people have been trained to
    distrust — in the one message whose whole point is sounding personal. This asserts on what
    the RECIPIENT sees, so a future change that reintroduces a token fails here rather than
    passing quietly because the plumbing still works.
    """
    import re as _re

    from applypilot.networking import outreach
    import applypilot.networking.store as _store_mod

    monkeypatch.setenv("INTRO_DECK_URL", BASE)
    monkeypatch.setattr(_store_mod, "get_connection", lambda *a, **k: db)

    for name in ("Gina Johnson", "Renée Dupont", "Jean-Luc Picard", "CJ", "Ali Coppinger"):
        cid = store.upsert_contact({"job_url": "http://j/1", "full_name": name,
                                    "email": f"{name.split()[0].lower()}@x.com"}, db)
        url = outreach._intro_deck_url({}, {"id": cid, "full_name": name})
        assert "?" not in url, f"{url} carries a query string"
        assert "=" not in url, f"{url} carries a parameter"
        assert "%" not in url, f"{url} is percent-encoded"
        # No hex blob masquerading as a name.
        tail = url.rsplit("/", 1)[-1]
        assert not _re.fullmatch(r"[0-9a-f]{6,}", tail), f"{url} ends in an opaque id"
        assert tail == deck.slugify(name) or tail.startswith(deck.slugify(name)), url


# ── re-linking existing drafts ───────────────────────────────────────────────────────────

NEW = "https://www.jorgealejandrodiez.com/intro/gina"


def test_relink_swaps_the_link_and_nothing_else():
    """Regenerating is the obvious move and the wrong one: it spends an LLM call and rewrites
    copy the operator may already have read and edited, to change a URL."""
    body = ("Hey Gina, long time without connecting!\n\n"
            "Here's a good intro deck we could go over during the call: "
            "https://www.jorgealejandrodiez.com/intro/?v=4e49a7ad. If you're open to a quick "
            "chat, grab a time here: https://cal.com/jorge-alejandro-diez/30min.\n\n"
            "Catch up soon!\nAlejandro")
    out, n = deck.relink(body, NEW)
    assert n == 1
    assert NEW in out and "?v=" not in out
    # The sentence's full stop survives — the real drafts end "…/intro/?v=4e49a7ad." and eating
    # that period would rewrite the sentence rather than the link.
    assert f"{NEW}. If you're open" in out
    # Everything else is byte-identical, including the OTHER link.
    assert "https://cal.com/jorge-alejandro-diez/30min." in out
    assert out.replace(NEW, "https://www.jorgealejandrodiez.com/intro/?v=4e49a7ad") == body


def test_relink_is_idempotent_and_handles_the_new_scheme():
    once, n1 = deck.relink("see https://www.jorgealejandrodiez.com/intro/?v=abc12345 ok", NEW)
    twice, n2 = deck.relink(once, NEW)
    assert twice == once and n1 == 1 and n2 == 1


def test_relink_leaves_text_with_no_deck_link_alone():
    for body in ("no links here", "", None, "https://cal.com/jorge-alejandro-diez/30min"):
        out, n = deck.relink(body, NEW)
        assert n == 0 and out == (body or "")


def test_relink_only_touches_OUR_deck():
    """Somebody else's /intro/ page — an article, a post someone linked — is not our deck.

    The host is taken from the replacement URL, so nothing extra has to be passed in. Without
    it the pattern is "any /intro/ URL anywhere", which is a rewrite waiting to mangle a real
    citation in a draft.
    """
    body = ("ours https://www.jorgealejandrodiez.com/intro/?v=abc12345 "
            "theirs https://someoneelse.com/intro/x "
            "and https://x.com/introduction/y")
    out, n = deck.relink(body, NEW)
    assert n == 1, "only our own deck link should be replaced"
    assert "https://someoneelse.com/intro/x" in out, "another site's /intro/ was rewritten"
    assert "https://x.com/introduction/y" in out, "an /introduction/ path was rewritten"
    assert NEW in out
