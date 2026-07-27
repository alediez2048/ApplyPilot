"""validate_json_fields — the preserved-projects check.

The tailor stage validates via validate_json_fields, but that path never checked preserved project
names; the check existed only in the legacy text path (validate_tailored_resume). These tests pin
the JSON-path check, and above all pin its SEVERITY: it must warn and never block, because the
tailor prompt explicitly permits dropping a project that is irrelevant to the target role.
"""

from __future__ import annotations

from applypilot.scoring.validator import validate_json_fields


def _profile(projects=None) -> dict:
    return {
        "personal": {"full_name": "Test User"},
        "resume_facts": {
            "preserved_companies": ["Acme"],
            "preserved_school": "State University",
            "preserved_projects": projects if projects is not None else [],
        },
    }


def _data(project_headers=("ApplyPilot - job pipeline",)) -> dict:
    """A minimally valid LLM payload; every required key must be truthy."""
    return {
        "title": "Engineer",
        "summary": "Built things.",
        "skills": {"Languages": "Python"},
        "experience": [
            {"header": "Engineer at Acme", "subtitle": "Python | 2020", "bullets": ["Shipped a service."]}
        ],
        "projects": [
            {"header": h, "subtitle": "Python | 2024", "bullets": ["Did the work."]}
            for h in project_headers
        ],
        "education": "State University | B.S.",
    }


def _project_warnings(result: dict) -> list[str]:
    return [w for w in result["warnings"] if w.startswith("Project ")]


# ── severity: this must never block ───────────────────────────────────────────

def test_missing_project_warns_but_still_passes():
    """The single most important property: a missing project never fails validation, so it can
    never trigger a retry or a failed_validation status."""
    result = validate_json_fields(
        _data([("Something Else - a different project")]),
        _profile(["ApplyPilot"]),
    )
    assert result["passed"] is True
    assert result["errors"] == []
    assert _project_warnings(result)


def test_missing_project_is_not_promoted_to_error_in_strict_mode():
    """strict mode escalates banned words, not preserved projects."""
    result = validate_json_fields(
        _data([("Unrelated - thing")]), _profile(["ApplyPilot"]), mode="strict"
    )
    assert result["passed"] is True
    assert not any("Project" in e for e in result["errors"])


def test_missing_company_still_errors():
    """Contrast: companies keep their harder severity. The new check must not soften them."""
    data = _data()
    data["experience"] = [{"header": "Engineer at Nowhere", "subtitle": "", "bullets": ["x"]}]
    result = validate_json_fields(data, _profile(["ApplyPilot"]))
    assert result["passed"] is False
    assert any("Acme" in e for e in result["errors"])


# ── the check itself ──────────────────────────────────────────────────────────

def test_present_project_produces_no_warning():
    result = validate_json_fields(_data(["ApplyPilot - job pipeline"]), _profile(["ApplyPilot"]))
    assert _project_warnings(result) == []


def test_renamed_project_is_caught():
    result = validate_json_fields(
        _data(["Automated Application Tool - job pipeline"]), _profile(["ApplyPilot"])
    )
    assert any("ApplyPilot" in w for w in _project_warnings(result))


def test_match_is_case_insensitive():
    result = validate_json_fields(_data(["APPLYPILOT - job pipeline"]), _profile(["ApplyPilot"]))
    assert _project_warnings(result) == []


def test_each_missing_project_warns_once():
    result = validate_json_fields(
        _data(["ApplyPilot - job pipeline"]), _profile(["ApplyPilot", "Kordami Suite", "Atlas"])
    )
    warns = _project_warnings(result)
    assert len(warns) == 2
    assert any("Kordami Suite" in w for w in warns)
    assert any("Atlas" in w for w in warns)


def test_no_preserved_projects_means_no_project_warnings():
    """The common case today — an empty list must stay completely silent."""
    result = validate_json_fields(_data(["Anything - at all"]), _profile([]))
    assert _project_warnings(result) == []


def test_names_with_special_characters_match():
    """Real names carry parentheses and ampersands."""
    name = "Springbox (a Prophet company) Redesign"
    result = validate_json_fields(_data([f"{name} - marketing site"]), _profile([name]))
    assert _project_warnings(result) == []


def test_project_bullets_are_still_collected_for_bulk_checks():
    """The block also feeds bullet text into the banned-word scan; that must survive the edit."""
    data = _data(["ApplyPilot - job pipeline"])
    data["projects"][0]["bullets"] = ["Leveraged synergy to deliver value."]
    result = validate_json_fields(data, _profile(["ApplyPilot"]), mode="strict")
    assert any("Banned words" in e for e in result["errors"])
