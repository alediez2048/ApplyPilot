"""The base résumé is the template — tailoring rewrites content inside it.

Before this, the pipeline imposed a fixed five-section schema. Measured on a real DevRev run
against the real base résumé:

  KEY STRENGTHS          deleted outright (no slot existed in the schema)
  EDUCATION              "B.A. Economics B.S. Advertising | 2011-2015" -> "Bachelors"
  PROJECTS               invented by promoting a work bullet, duplicating the achievement
  T-Mobile / Verizon     5 bullets each -> 4 ("Max 4 per section" was in the prompt)
  named tools            "AEM, Botify, GA4, GSC" -> "improving customer experience"
  4,341 chars            -> 2,571

Nothing failed. The validator said `approved`. These tests exist because the only signal was
a human reading the output and saying it looked bare.
"""

from __future__ import annotations

import pytest

from applypilot.scoring import resume_sections as RS
from applypilot.scoring import validator as V

BASE = """Jorge Alejandro Diez Magni
(512) 709-7014 - jorge@example.com - linkedin.com/in/x

PERSONAL STATEMENT
Seasoned Technical Project Manager with 10+ years of experience.

WORK EXPERIENCE

T-Mobile
Technical Project Manager
2023/2025
- Led enterprise platform initiatives through AEM, Botify, GA4, and GSC.
- Owned the retail location experience platform, 40% YoY traffic increase.
- Drove Dialed In, increasing organic traffic by 1M visits in six months.

Verizon
Technical Program Manager
2021/2023
- Managed the authenticated web experience with REST/GraphQL integration.
- Led web promotion best practices, 25% organic traffic increase.

EDUCATION
- The University of Texas at Austin (B.A. Economics B.S. Advertising) | 2011-2015
- Gauntlet AI Fellowship (Advanced AI engineering) | February 2026 - April 2026

KEY STRENGTHS
- Analytics: Google Analytics, Adobe Analytics, MLOps, A/B testing.
- AI Development: prompt engineering, agentic workflows, RAG, knowledge graphs.
"""


# ── parsing ─────────────────────────────────────────────────────────────────

def test_sections_keep_their_own_names_and_order():
    r = RS.parse(BASE)
    assert r.titles() == ["PERSONAL STATEMENT", "WORK EXPERIENCE", "EDUCATION", "KEY STRENGTHS"]
    assert [s.kind for s in r.sections] == ["summary", "experience", "education", "skills"]


def test_the_header_is_read_from_the_resume_not_a_profile():
    """profile.json disagrees with the résumé — it drops "Magni". The résumé wins."""
    assert RS.parse(BASE).header[0] == "Jorge Alejandro Diez Magni"


def test_no_line_is_ever_lost():
    """A parser that silently drops a line loses part of someone's work history."""
    r = RS.parse(BASE)
    kept = set(r.header) | {s.title for s in r.sections}
    kept |= {ln.strip() for s in r.sections for ln in s.lines if ln.strip()}
    missing = [ln.strip() for ln in BASE.splitlines() if ln.strip() and ln.strip() not in kept]
    assert not missing, missing


def test_experience_entries_split_into_employer_role_dates_bullets():
    exp = RS.parse(BASE).by_kind("experience")[0]
    entries = RS.experience_entries(exp)
    assert [e["employer"] for e in entries] == ["T-Mobile", "Verizon"]
    assert entries[0]["role"] == "Technical Project Manager"
    assert entries[0]["dates"] == "2023/2025"
    assert len(entries[0]["bullets"]) == 3 and len(entries[1]["bullets"]) == 2


def test_an_unrecognised_section_is_preserved_as_text_not_dropped():
    """The exact failure mode being fixed: no slot must never mean deleted."""
    r = RS.parse(BASE + "\nPUBLICATIONS\n- A paper about things.\n")
    assert "PUBLICATIONS" in r.titles()
    assert r.sections[-1].kind == RS.KIND_TEXT
    assert r.sections[-1].bullets() == ["A paper about things."]


# ── the prompt describes the real structure ─────────────────────────────────

def test_the_prompt_names_every_section_and_its_bullet_counts():
    from applypilot.scoring.tailor import _structure_block
    block = _structure_block(BASE)
    for title in ("PERSONAL STATEMENT", "WORK EXPERIENCE", "EDUCATION", "KEY STRENGTHS"):
        assert title in block
    assert "keep all 3 bullets" in block and "keep all 2 bullets" in block
    assert "FLOOR, NOT A CEILING" in block, "nothing stops the model trimming bullets again"


# ── assembly preserves what the model returns AND what it drops ─────────────

def _profile():
    return {"personal": {"full_name": "Jorge Alejandro Diez", "email": "x@y.com"},
            "resume_facts": {"preserved_companies": ["T-Mobile", "Verizon"],
                             "preserved_school": "Gauntlet AI; University of Texas"}}


def test_a_section_the_model_omits_falls_back_to_the_original():
    """The model dropping KEY STRENGTHS is exactly what used to happen. It must not matter."""
    from applypilot.scoring.tailor import assemble_structured_resume_text
    data = {"title": "PM", "sections": [
        {"title": "PERSONAL STATEMENT", "kind": "summary", "text": "Rewritten."}]}
    out = assemble_structured_resume_text(data, _profile(), BASE)
    assert "KEY STRENGTHS" in out and "knowledge graphs" in out
    assert "WORK EXPERIENCE" in out and "T-Mobile" in out


def _bullets_under(text: str, employer: str) -> list[str]:
    """Bullets belonging to ONE employer — up to the next non-bullet line.

    Counting from `split(employer)[1]` to the end of the document sweeps in every later
    section's bullets, which made the first version of this test pass even with the
    top-up logic deleted. Measuring the wrong span is the same failure as not measuring.
    """
    out, seen = [], False
    for line in text.splitlines():
        if line.strip() == employer:
            seen = True
            continue
        if not seen:
            continue
        if line.startswith("- "):
            out.append(line[2:])
        elif line.strip() and out:          # next employer/heading ends this block
            break
    return out


def test_bullets_are_never_fewer_than_the_original():
    """The prompt asks; the assembler enforces. "Max 4 per section" cost a real bullet."""
    from applypilot.scoring.tailor import assemble_structured_resume_text
    data = {"title": "PM", "sections": [
        {"title": "WORK EXPERIENCE", "kind": "experience", "entries": [
            {"employer": "T-Mobile", "role": "TPM", "dates": "2023/2025",
             "bullets": ["Only one bullet survived."]}]}]}
    out = assemble_structured_resume_text(data, _profile(), BASE)
    bullets = _bullets_under(out, "T-Mobile")
    assert len(bullets) == 3, f"expected the original 3, got {len(bullets)}: {bullets}"
    assert bullets[0] == "Only one bullet survived."    # the rewrite leads
    assert any("40% YoY" in b for b in bullets)         # padded from the trailing originals


def test_a_full_rewrite_is_not_duplicated_against_the_original():
    """The obvious wrong fix: merge by text similarity.

    A genuine rewrite does not resemble its source, so prefix-matching classifies every
    rewritten bullet as new and appends the originals too — three rewrites become six
    bullets, half of them saying the same thing twice.
    """
    from applypilot.scoring.tailor import assemble_structured_resume_text
    data = {"title": "PM", "sections": [
        {"title": "WORK EXPERIENCE", "kind": "experience", "entries": [
            {"employer": "T-Mobile", "role": "TPM", "dates": "2023/2025", "bullets": [
                "Completely different wording one.",
                "Completely different wording two.",
                "Completely different wording three."]}]}]}
    out = assemble_structured_resume_text(data, _profile(), BASE)
    bullets = _bullets_under(out, "T-Mobile")
    assert len(bullets) == 3, f"duplicated into {len(bullets)}: {bullets}"
    assert not any("Botify" in b for b in bullets), "original text leaked in beside the rewrite"


def test_the_span_helper_itself_is_not_measuring_the_whole_document():
    """Guard the guard: `_bullets_under` must stop at the next employer."""
    from applypilot.scoring.tailor import assemble_structured_resume_text
    out = assemble_structured_resume_text({"sections": []}, _profile(), BASE)
    assert len(_bullets_under(out, "T-Mobile")) == 3
    assert len(_bullets_under(out, "Verizon")) == 2


def test_the_assembled_header_keeps_the_full_name():
    from applypilot.scoring.tailor import assemble_structured_resume_text
    out = assemble_structured_resume_text({"sections": []}, _profile(), BASE)
    assert out.startswith("Jorge Alejandro Diez Magni")


def test_no_section_is_invented():
    from applypilot.scoring.tailor import assemble_structured_resume_text
    data = {"sections": [{"title": "PROJECTS", "kind": "text",
                          "bullets": ["Dialed In - duplicated from a work bullet"]}]}
    out = assemble_structured_resume_text(data, _profile(), BASE)
    assert "PROJECTS" not in out, "a section absent from the base résumé was rendered"


# ── validation of the new shape ─────────────────────────────────────────────

def _sections_payload(**over):
    data = {"title": "PM", "sections": [
        {"title": "WORK EXPERIENCE", "kind": "experience", "entries": [
            {"employer": "T-Mobile", "role": "TPM", "dates": "2023/2025", "bullets": ["a"]},
            {"employer": "Verizon", "role": "TPM", "dates": "2021/2023", "bullets": ["b"]}]},
        {"title": "EDUCATION", "kind": "education", "entries": [
            {"school": "The University of Texas at Austin", "degree": "B.A.", "date": "2011-2015"},
            {"school": "Gauntlet AI Fellowship", "degree": "AI", "date": "2026"}]}]}
    data.update(over)
    return data


def test_the_sections_shape_validates_instead_of_reporting_missing_fields():
    """It used to fail every attempt with "Missing required field: summary" and retry 4x."""
    res = V.validate_json_fields(_sections_payload(), _profile(), mode="normal")
    assert res["passed"], res["errors"]


def test_a_missing_preserved_company_still_blocks():
    payload = _sections_payload(sections=[{"title": "WORK EXPERIENCE", "kind": "experience",
                                           "entries": [{"employer": "T-Mobile", "bullets": ["a"]}]}])
    res = V.validate_json_fields(payload, _profile(), mode="normal")
    assert not res["passed"] and any("Verizon" in e for e in res["errors"])


def test_a_multi_school_preserved_value_is_checked_school_by_school():
    """"Gauntlet AI; University of Texas" appears nowhere as one string.

    It only ever passed because the OLD prompt told the model to echo
    `"{school} | {level}"` back — the validator was checking for text it had just asked
    for, which is a check that cannot fail.
    """
    assert V._split_schools("Gauntlet AI; University of Texas") == \
        ["Gauntlet AI", "University of Texas"]
    res = V.validate_json_fields(_sections_payload(), _profile(), mode="normal")
    assert res["passed"], res["errors"]

    missing_one = _sections_payload()
    missing_one["sections"][1]["entries"] = [{"school": "Gauntlet AI Fellowship"}]
    res = V.validate_json_fields(missing_one, _profile(), mode="normal")
    assert not res["passed"] and any("University of Texas" in e for e in res["errors"])


@pytest.mark.parametrize("mode,expect_error", [("strict", True), ("normal", False)])
def test_banned_words_follow_the_documented_severity_ladder(mode, expect_error):
    payload = _sections_payload()
    payload["sections"].append({"title": "PERSONAL STATEMENT", "kind": "summary",
                                "text": f"I am a {V.BANNED_WORDS[0]} person."})
    res = V.validate_json_fields(payload, _profile(), mode=mode)
    assert bool(res["errors"]) is expect_error


# ── the render request ──────────────────────────────────────────────────────

def test_the_render_request_carries_section_titles():
    """The PDF used to hardcode "Professional Summary" / "Technical Skills" while the .txt
    said "SUMMARY" / "TECHNICAL SKILLS" — the two disagreed on every heading."""
    from applypilot.scoring import resume_render as RR
    block = RR.resume_from_sections(_sections_payload(), _profile(),
                                    header=["Jorge Alejandro Diez Magni"])
    assert block["contactInfo"]["name"] == "Jorge Alejandro Diez Magni"
    assert [s["title"] for s in block["sections"]] == ["WORK EXPERIENCE", "EDUCATION"]
    assert block["sections"][1]["education"][0]["school"].startswith("The University")


def test_skills_bullets_split_into_bold_categories():
    from applypilot.scoring import resume_render as RR
    rows = RR._skills_from_bullets(["Analytics: GA, Adobe", "no colon here"])
    assert rows[0] == {"category": "Analytics", "value": "GA, Adobe"}
    assert rows[1]["category"] is None


# ── named tools must survive ────────────────────────────────────────────────

_BOUNDARY = {"skills_boundary": {
    "seo_search": ["Botify", "GA4", "Adobe Analytics", "technical SEO"],
    "tools": ["Tools and Platforms: Jira", "Docker", "and GitHub."],
}}


def test_curated_tools_are_read_from_the_profile_not_guessed():
    """A regex over capitalised words flagged "KEY"/"WORK" from headings and MISSED Botify.

    Botify and Akamai are single capitalised words, indistinguishable from "Led" or
    "Managed". `skills_boundary` is curated, so no guessing is needed.
    """
    base = "Led work through Botify, GA4 and Docker. Used Jira and GitHub."
    tools = V._named_tools(base, _BOUNDARY)
    assert {"Botify", "GA4", "Docker", "Jira", "GitHub"} <= tools
    assert "technical SEO" not in tools, "lowercase ability descriptions are not tool names"
    assert not any(t in tools for t in ("KEY", "WORK", "Led"))


def test_a_dropped_tool_is_reported_as_a_warning_not_an_error():
    """"…through AEM, Botify, GA4, GSC" became "…improving customer experience" on a real
    run. The prompt asks for tools to be kept; one run obeyed and the next did not, so it
    needs measuring. A warning, because cutting a genuinely irrelevant tool is legitimate."""
    profile = {**_profile(), **_BOUNDARY,
               "_base_resume_text": "Led platform work through Botify and GA4."}
    payload = _sections_payload()
    payload["sections"].append({"title": "PERSONAL STATEMENT", "kind": "summary",
                                "text": "Improved customer experience broadly."})
    res = V.validate_json_fields(payload, profile, mode="normal")
    assert res["passed"], "a dropped tool must not block the résumé"
    assert any("Botify" in w for w in res["warnings"]), res["warnings"]


def test_no_warning_when_the_tools_survive():
    profile = {**_profile(), **_BOUNDARY,
               "_base_resume_text": "Led platform work through Botify and GA4."}
    payload = _sections_payload()
    payload["sections"].append({"title": "WORK EXPERIENCE", "kind": "experience", "entries": [
        {"employer": "T-Mobile", "bullets": ["Ran the platform through Botify and GA4."]},
        {"employer": "Verizon", "bullets": ["b"]}]})
    res = V.validate_json_fields(payload, profile, mode="normal")
    assert not any("dropped" in w for w in res["warnings"]), res["warnings"]
