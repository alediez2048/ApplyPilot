"""Tests for the Hunter.io provider and the provider registry."""

from __future__ import annotations

import httpx

from applypilot.networking import hunter, providers


# ── hunter parsing ──────────────────────────────────────────────────────────

_DOMAIN_SEARCH = {
    "data": {
        "organization": "Affirm",
        "pattern": "{first}.{last}",
        "emails": [
            {"value": "jane@affirm.com", "first_name": "Jane", "last_name": "Smith",
             "position": "Staff AI Engineer", "seniority": "senior", "department": "engineering",
             "linkedin": "https://www.linkedin.com/in/jane",
             "verification": {"status": "valid"}},
            {"value": "omar@affirm.com", "first_name": "Omar", "last_name": "Reyes",
             "position": "Technical Recruiter", "seniority": "senior", "department": "hr",
             "linkedin": "linkedin.com/in/omar", "verification": {"status": "accept_all"}},
        ],
    }
}


def test_hunter_search_parses_people_with_emails(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "k")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=_DOMAIN_SEARCH))
    out = hunter.search_candidates("Affirm", "affirm.com", per_page=10)
    assert len(out) == 2
    jane = out[0]
    assert jane["key"] == "jane@affirm.com" and jane["full_name"] == "Jane Smith"
    assert jane["email"] == "jane@affirm.com" and jane["email_status"] == "verified"
    assert jane["linkedin_url"].endswith("/jane")
    # accept_all -> unverified; bare handle -> normalized URL
    assert out[1]["email_status"] == "unverified"
    assert out[1]["linkedin_url"].startswith("https://")


def test_hunter_search_surfaces_pagination_error(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "k")
    err = {"errors": [{"code": 400, "details": "limited to 10"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=err))
    assert hunter.search_candidates("Affirm", None, per_page=25) == []


def test_hunter_enrich_reads_back_cached_emails():
    selected = [{"key": "a@x.com", "email": "a@x.com", "email_status": "verified",
                 "linkedin_url": "https://l/a"}]
    rev = hunter.enrich(selected)
    assert rev["a@x.com"]["email"] == "a@x.com"
    assert rev["a@x.com"]["email_status"] == "verified"


def test_hunter_no_key_returns_empty(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    assert hunter.search_candidates("Affirm", "affirm.com") == []


# ── provider registry selection ─────────────────────────────────────────────

def _clear(monkeypatch):
    for k in ("HUNTER_API_KEY", "APOLLO_API_KEY", "NETWORKING_PROVIDER"):
        monkeypatch.delenv(k, raising=False)


def test_registry_prefers_hunter_when_both_present(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("HUNTER_API_KEY", "h")
    monkeypatch.setenv("APOLLO_API_KEY", "a")
    assert providers.active() == "hunter"


def test_registry_falls_back_to_apollo(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("APOLLO_API_KEY", "a")
    assert providers.active() == "apollo"


def test_registry_explicit_override(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("HUNTER_API_KEY", "h")
    monkeypatch.setenv("APOLLO_API_KEY", "a")
    monkeypatch.setenv("NETWORKING_PROVIDER", "apollo")
    assert providers.active() == "apollo"


def test_registry_none_without_keys(monkeypatch):
    _clear(monkeypatch)
    assert providers.active() is None
    assert providers.available() is False
    ok, _ = providers.probe()
    assert ok is False


def test_registry_search_routes_to_hunter(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("HUNTER_API_KEY", "h")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=_DOMAIN_SEARCH))
    out = providers.search("Affirm", "affirm.com", "AI Engineer", ["AI Engineer"], per_page=10)
    assert out and out[0]["key"] == "jane@affirm.com"
    # enrich reads back the cached emails (no second call)
    rev = providers.enrich(out)
    assert rev["jane@affirm.com"]["email"] == "jane@affirm.com"
