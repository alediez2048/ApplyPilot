"""Tailor prompt builders — the preserved-entity rules.

`preserved_projects` was collected by the wizard and checked by the validator, but neither tailor
prompt ever told the LLM about it: the check existed, the instruction did not. These tests pin the
rule down, including the two things that are easy to regress — staying silent when a profile has no
projects, and never introducing an em dash (the prompt bans them and the validator errors on them).
"""

from __future__ import annotations

from applypilot.scoring.tailor import (
    _build_aggressive_tailor_prompt,
    _build_tailor_prompt,
    _preserved_projects_rule,
)

EM_DASH = "—"
EN_DASH = "–"

_BUILDERS = (_build_tailor_prompt, _build_aggressive_tailor_prompt)


def _profile(projects=None) -> dict:
    return {
        "personal": {"full_name": "Test User"},
        "experience": {"education_level": "B.S."},
        "skills_boundary": {"languages": ["Python"]},
        "resume_facts": {
            "preserved_companies": ["Acme"],
            "preserved_school": "State University",
            "real_metrics": ["40%"],
            "preserved_projects": projects if projects is not None else [],
        },
    }


# ── the rule itself ───────────────────────────────────────────────────────────

def test_rule_is_empty_when_no_projects():
    """No projects must produce NOTHING — not even a newline, so the prompt is unchanged."""
    assert _preserved_projects_rule({"preserved_projects": []}) == ""
    assert _preserved_projects_rule({}) == ""
    assert _preserved_projects_rule({"preserved_projects": None}) == ""


def test_rule_lists_every_project_name():
    rule = _preserved_projects_rule({"preserved_projects": ["ApplyPilot", "Kordami Suite"]})
    assert "ApplyPilot" in rule
    assert "Kordami Suite" in rule


def test_rule_forbids_renaming_and_inventing():
    """The rule's whole job: names are fixed, and new names may not be invented."""
    rule = _preserved_projects_rule({"preserved_projects": ["ApplyPilot"]})
    lowered = rule.lower()
    assert "keep its real name" in lowered
    assert "never rename" in lowered
    assert "never invent" in lowered


def test_rule_still_permits_dropping_irrelevant_projects():
    """Softer than the companies rule on purpose — the validator only warns on a missing project,
    because dropping one that is irrelevant to the role is legitimate tailoring."""
    rule = _preserved_projects_rule({"preserved_projects": ["ApplyPilot"]})
    assert "drop" in rule.lower()


def test_rule_uses_no_em_dash():
    """The prompt bans em dashes and the validator makes one a hard error, so the rule must not
    prime the model with one."""
    rule = _preserved_projects_rule({"preserved_projects": ["ApplyPilot"]})
    assert EM_DASH not in rule
    assert EN_DASH not in rule


# ── wiring into both prompts ──────────────────────────────────────────────────

def test_no_projects_contributes_nothing_to_either_prompt():
    """An empty list must be byte-identical to the key being absent entirely — the rule adds zero
    characters, not even a stray blank line, so existing profiles see an unchanged prompt."""
    absent = _profile()
    del absent["resume_facts"]["preserved_projects"]
    empty, populated = _profile(), _profile(["ApplyPilot"])
    for build in _BUILDERS:
        text = build(empty)
        assert text == build(absent)
        assert "Real projects" not in text
        assert build(populated) != text


def test_both_prompts_carry_the_rule_when_projects_exist():
    """Standard AND aggressive — the aggressive path is the one the user actually runs
    (TAILOR_AGGRESSIVE=1), so it must not be forgotten."""
    profile = _profile(["ApplyPilot", "Kordami Suite"])
    for build in _BUILDERS:
        text = build(profile)
        assert "Real projects" in text
        assert "ApplyPilot" in text
        assert "Kordami Suite" in text
        assert "never invent a project name" in text.lower()


def test_rule_appears_exactly_once_per_prompt():
    profile = _profile(["ApplyPilot"])
    for build in _BUILDERS:
        assert build(profile).count("Real projects:") == 1


def test_preserved_companies_and_school_survive_alongside_projects():
    """Adding the projects rule must not disturb the existing preserved-entity rules."""
    profile = _profile(["ApplyPilot"])
    for build in _BUILDERS:
        text = build(profile)
        assert "Acme" in text
        assert "State University" in text


def test_project_names_are_not_mangled_by_special_characters():
    """Real project names contain parentheses, ampersands, and hyphens."""
    names = ["Springbox (a Prophet company)", "R&D Pipeline", "Multi-Region Failover"]
    profile = _profile(names)
    for build in _BUILDERS:
        text = build(profile)
        for name in names:
            assert name in text
