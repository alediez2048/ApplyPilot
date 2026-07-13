"""Tests for the React-PDF resume renderer bridge and mapping layer.

The mapping tests are pure (no Node). The render test is gated on Node being
available and skips otherwise, so the suite passes on Tier-2-only machines.
"""

from __future__ import annotations

import re

import pytest

from applypilot.scoring import resume_render as rr
from applypilot.scoring import pdf


# ── Pure mapping: skills ────────────────────────────────────────────────────

def test_skills_dict_preserves_order_and_pairs():
    skills = {"Languages": "Python, Go", "Frameworks": "React"}
    out = rr._skills_to_list(skills)
    assert out == [
        {"category": "Languages", "value": "Python, Go"},
        {"category": "Frameworks", "value": "React"},
    ]


def test_skills_string_becomes_single_uncategorized_row():
    assert rr._skills_to_list("Python, SQL") == [{"category": None, "value": "Python, SQL"}]


def test_skills_empty_is_empty_list():
    assert rr._skills_to_list(None) == []
    assert rr._skills_to_list("") == []


# ── Pure mapping: education ─────────────────────────────────────────────────

def test_education_string_splits_school_and_detail():
    out = rr._education_to_list("State University\nB.S. CS\nGPA 3.9")
    assert out == [{"school": "State University", "degree": "",
                    "detail": "B.S. CS · GPA 3.9", "date": ""}]


def test_education_empty_is_empty_list():
    assert rr._education_to_list("") == []
    assert rr._education_to_list(None) == []


def test_education_list_passthrough():
    out = rr._education_to_list([{"school": "MIT", "degree": "PhD"}])
    assert out[0]["school"] == "MIT" and out[0]["degree"] == "PhD"


# ── Pure mapping: full request from LLM data ────────────────────────────────

def test_resume_from_llm_data_uses_profile_for_header_not_llm():
    data = {"title": "Senior Engineer", "summary": "s",
            "skills": {"Lang": "Python"},
            "experience": [{"header": "Acme", "subtitle": "Team", "bullets": ["did x"]}],
            "projects": [], "education": "U\nB.S."}
    profile = {"personal": {"full_name": "Ada Lovelace", "email": "ada@x.com",
                            "phone": "555", "city": "London", "province_state": "UK",
                            "github_url": "https://github.com/ada"}}
    block = rr.resume_from_llm_data(data, profile)
    assert block["contactInfo"]["name"] == "Ada Lovelace"     # from profile
    assert block["contactInfo"]["title"] == "Senior Engineer"  # from LLM data
    assert block["contactInfo"]["location"] == "London, UK"
    assert block["contactInfo"]["links"] == ["github.com/ada"]  # scheme stripped
    assert block["summary"] == "s"
    assert block["skills"] == [{"category": "Lang", "value": "Python"}]
    assert block["experience"][0]["bullets"] == ["did x"]


def test_clean_link_strips_scheme_www_and_slash():
    assert rr._clean_link("https://www.linkedin.com/in/x/") == "linkedin.com/in/x"
    assert rr._clean_link("http://github.com/y") == "github.com/y"


def test_contact_links_deduped():
    # portfolio_url and website_url pointing at the same site collapse to one link
    profile = {"personal": {"full_name": "X", "portfolio_url": "https://me.com",
                            "website_url": "https://me.com/"}}
    block = rr.resume_from_llm_data({"title": "T"}, profile)
    assert block["contactInfo"]["links"] == ["me.com"]


# ── pdf.py text adapter (legacy .txt path) ──────────────────────────────────

def test_split_contact_classifies_email_phone_links():
    email, phone, links = pdf._split_contact("a@b.com | 555-123-4567 | github.com/z")
    assert email == "a@b.com"
    assert phone == "555-123-4567"
    assert links == ["github.com/z"]


def test_resume_block_from_text_parses_sections():
    text = (
        "Ada Lovelace\nSenior Engineer\na@b.com | 555-123-4567\n\n"
        "SUMMARY\nGreat engineer.\n\n"
        "TECHNICAL SKILLS\nLanguages: Python, Go\n\n"
        "EXPERIENCE\nAcme - Staff\nPlatform\n- Built x\n- Shipped y\n\n"
        "EDUCATION\nState University\nB.S. CS\n"
    )
    block = pdf._resume_block_from_text(text)
    assert block["contactInfo"]["name"] == "Ada Lovelace"
    assert block["contactInfo"]["email"] == "a@b.com"
    assert block["summary"].startswith("Great engineer")
    assert block["skills"] == [{"category": "Languages", "value": "Python, Go"}]
    assert block["experience"][0]["header"] == "Acme - Staff"
    assert block["experience"][0]["bullets"] == ["Built x", "Shipped y"]
    assert block["education"][0]["school"] == "State University"


# ── Cover letter prose rendering ────────────────────────────────────────────

def test_cover_letter_html_is_prose_not_resume_sections():
    text = ("Dear Hiring Manager,\n\nI am excited to apply.\n\n"
            "My experience fits well.\n\nSincerely,\nAda Lovelace")
    profile = {"personal": {"full_name": "Ada Lovelace", "email": "ada@x.com", "phone": "555"}}
    html = pdf.build_cover_letter_html(text, profile)
    assert "Dear Hiring Manager," in html
    assert "<p>" in html                      # rendered as paragraphs
    assert "SUMMARY" not in html and "EXPERIENCE" not in html  # not the resume template
    assert "Ada Lovelace" in html              # letterhead from profile
    # multi-line sign-off keeps the line break
    assert "Sincerely,<br>Ada Lovelace" in html


def test_cover_letter_html_escapes_content():
    html = pdf.build_cover_letter_html("Dear <Manager> & team,\n\nRegards", None)
    assert "&lt;Manager&gt;" in html and "&amp;" in html


# ── Cover letter mapping (React-PDF block) ──────────────────────────────────

def test_cover_letter_from_text_builds_block_from_profile():
    profile = {"personal": {"full_name": "Ada Lovelace", "email": "ada@x.com",
                            "phone": "555", "city": "London", "province_state": "UK"}}
    cover = rr.cover_letter_from_text("Dear Hiring Manager,\n\nHi.\n\nSincerely,\nAda",
                                      profile, date="July 13, 2026")
    # candidate reuses the résumé contactInfo shape (shared header)
    assert cover["candidate"]["name"] == "Ada Lovelace"
    assert cover["candidate"]["email"] == "ada@x.com"
    assert cover["candidate"]["location"] == "London, UK"
    assert cover["date"] == "July 13, 2026"
    assert cover["body"].startswith("Dear Hiring Manager,")


# ── Node render (gated) ─────────────────────────────────────────────────────

@pytest.mark.skipif(not rr.node_renderer_available(),
                    reason="Node.js/renderer not available")
def test_render_with_node_produces_one_page_pdf(tmp_path, monkeypatch):
    # Isolate the runtime install into tmp so the test doesn't touch ~/.applypilot.
    import applypilot.config as config
    monkeypatch.setattr(config, "RESUME_RENDERER_RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(rr, "_runtime_ready", None)

    block = rr.resume_from_llm_data(
        {"title": "Engineer", "summary": "Builds things.",
         "skills": {"Lang": "Python"},
         "experience": [{"header": "Acme", "subtitle": "Team", "bullets": ["did x"]}],
         "projects": [], "education": "State U\nB.S."},
        {"personal": {"full_name": "Test User", "email": "t@x.com"}},
    )
    out = tmp_path / "resume.pdf"
    assert rr.render_with_node(block, out) is True
    data = out.read_bytes()
    assert data[:4] == b"%PDF"
    pages = len(re.findall(rb"/Type\s*/Page(?![s])", data))
    assert pages == 1


@pytest.mark.skipif(not rr.node_renderer_available(),
                    reason="Node.js/renderer not available")
def test_render_cover_with_node_produces_pdf(tmp_path, monkeypatch):
    import applypilot.config as config
    monkeypatch.setattr(config, "RESUME_RENDERER_RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(rr, "_runtime_ready", None)

    cover = rr.cover_letter_from_text(
        "Dear Hiring Manager,\n\nI am excited to apply.\n\nSincerely,\nTest User",
        {"personal": {"full_name": "Test User", "email": "t@x.com"}},
        date="July 13, 2026",
    )
    out = tmp_path / "cover.pdf"
    assert rr.render_cover_with_node(cover, out) is True
    assert out.read_bytes()[:4] == b"%PDF"
