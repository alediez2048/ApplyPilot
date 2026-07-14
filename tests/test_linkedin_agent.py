"""NET-5 tests: prompt/read-only scoping, daily cap, JSON parse, fallback gating.

The live agent run is not exercised (needs a real LinkedIn login); these cover the
deterministic, safety-critical logic.
"""

from __future__ import annotations

from applypilot.networking import linkedin_agent as la
from applypilot.networking import prompt


def test_readonly_toolset_excludes_all_mutating_tools():
    allowed = set(prompt.READONLY_TOOLS)
    for banned in ("browser_click", "browser_type", "browser_fill_form",
                   "browser_select_option", "browser_press_key", "browser_file_upload",
                   "browser_evaluate", "browser_handle_dialog"):
        assert not any(banned in t for t in allowed), f"{banned} must not be allowed"
    # and it does include navigation/observation
    assert any("browser_navigate" in t for t in allowed)
    assert any("browser_snapshot" in t for t in allowed)


def test_prompt_is_read_only_and_targets_company_role():
    p = prompt.build_linkedin_prompt("Affirm", "AI Solutions Engineer", 5)
    assert "Affirm" in p and "AI Solutions Engineer" in p
    assert "read-only" in p.lower()
    assert "do not send" in p.lower() or "must not" in p.lower()
    assert "JSON array" in p


def test_parse_people_extracts_last_json_array():
    out = (
        'some log noise\n'
        '[{"name": "Ignore Me", "title": "old", "profile_url": "https://x/1"}]\n'
        'final answer:\n'
        '[{"name": "Jane Smith", "title": "Staff Eng", "profile_url": "https://www.linkedin.com/in/jane"},'
        ' {"name": "Bob", "title": "Recruiter", "profile_url": "https://www.linkedin.com/in/bob"}]'
    )
    people = la._parse_people(out, limit=5)
    assert len(people) == 2
    assert people[0]["full_name"] == "Jane Smith"
    assert people[0]["linkedin_url"].endswith("/jane")


def test_parse_people_empty_on_garbage():
    assert la._parse_people("no json here at all", 5) == []
    assert la._parse_people("[]", 5) == []


def test_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "_USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.setenv("NETWORKING_LINKEDIN_DAILY_LIMIT", "2")
    assert la.companies_today() == 0 and la.under_daily_cap() is True
    la._bump_usage()
    la._bump_usage()
    assert la.companies_today() == 2 and la.under_daily_cap() is False


def test_find_people_gated_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("NETWORKING_LINKEDIN", raising=False)  # disabled
    # even with consent + usage stubbed, disabled flag short-circuits before any spawn
    assert la.find_people("Affirm", "Engineer", 5) == []


def test_find_people_requires_consent(tmp_path, monkeypatch):
    monkeypatch.setenv("NETWORKING_LINKEDIN", "1")
    monkeypatch.setattr(la, "_CONSENT_FILE", tmp_path / "nope")  # no consent
    assert la.has_consent() is False
    assert la.find_people("Affirm", "Engineer", 5) == []


def test_consent_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "_CONSENT_FILE", tmp_path / "consent")
    assert la.has_consent() is False
    la.record_consent()
    assert la.has_consent() is True
