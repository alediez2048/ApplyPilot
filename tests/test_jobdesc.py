"""`role_essentials` — spend the job description on the job.

Outreach read `full_description[:1200]`. Measured on the live Affirm posting, that window is:
company mission (180 chars), team background (340), and then the role STARTS at ~520 — so the
model was handed a mission statement and an org chart, and the sentence saying what the person
actually does began right where the budget ran out. The emails were enthusiastic and
non-specific because that is all the input supported.
"""

from __future__ import annotations

import pytest

from applypilot.domain.jobdesc import _classify, role_essentials, split_sections

AFFIRM = """Affirm is reinventing credit to make it more honest and friendly, giving consumers
the flexibility to buy now and pay later without any hidden fees.

About the Team:

People Tech & Analytics builds and owns the data, AI, and technology infrastructure for the
People function. The team runs like a product engineering group embedded in HR.

About the Role:

This is a hands-on engineering role. You will build, deploy, and maintain AI-powered systems.
The work is taking messy business problems and turning them into working software: agents,
APIs, applications, and infrastructure.

Benefits:

100% subsidized medical coverage, dental and vision for you and your dependents. Flexible
Spending Wallets. Away Days. And a generous equity package.

Equal Opportunity Employer:

Affirm is proud to be aced equal opportunity employer and considers all applicants without
regard to race, colour, religion, or any other protected characteristic.
""" * 3        # long enough to trip the length gate


def test_the_company_mission_is_dropped():
    out = role_essentials(AFFIRM)
    assert "reinventing credit" not in out, "the mission statement survived"
    assert "buy now and pay later" not in out


def test_what_the_job_actually_is_survives():
    out = role_essentials(AFFIRM)
    assert "messy business problems" in out, "the sentence describing the work was dropped"
    assert "build, deploy, and maintain AI-powered systems" in out


def test_benefits_and_eeo_are_dropped():
    """Nobody has ever replied to a cold email because it mentioned their dental plan."""
    out = role_essentials(AFFIRM)
    assert "subsidized medical" not in out
    assert "without regard to race" not in out


@pytest.mark.parametrize("header,kind", [
    ("About the Role", "keep"),
    ("What you'll do", "keep"),
    ("Responsibilities", "keep"),
    ("About Us", "drop"),
    ("Benefits", "drop"),
    ("Equal Opportunities at Arm", "drop"),      # plural — the miss that leaked 405 chars
    ("Equal Opportunity Employer", "drop"),
    ("Hybrid Working at Arm", "drop"),
    ("Pay Transparency", "drop"),
    ("Required Skills & Experience", "unknown"),
])
def test_headers_are_classified(header, kind):
    assert _classify(header) == kind


def test_an_unrecognised_section_is_kept_not_discarded():
    """The Arm posting's "Required Skills & Experience" — 1,463 characters of exactly what an
    email should reference — was thrown away by a rule that kept only the FIRST unknown section,
    because "Job Overview" had already spent the slot. Both are the job."""
    text = ("Job Overview\n" + "The role involves shipping compilers. " * 40 +
            "\n\nRequired Skills & Experience\n" + "Deep C++ and LLVM experience. " * 40 +
            "\n\nEqual Opportunities at Arm\n" + "We consider all applicants. " * 40)
    out = role_essentials(text)
    assert "shipping compilers" in out
    assert "LLVM experience" in out, "a second unlabelled section was discarded"
    assert "consider all applicants" not in out


def test_a_short_posting_still_gets_its_boilerplate_dropped():
    """There used to be a length gate returning short postings whole. It was wrong: a short
    posting can be half boilerplate, and the gate silently turned the feature off for it —
    which is the version of this bug that would never have been noticed."""
    short = "About Us\nWe are a startup.\n\nThe Role\nYou will write Rust."
    out = role_essentials(short)
    assert "You will write Rust" in out
    assert "We are a startup" not in out, "the length gate is back"


def test_a_posting_with_no_headers_at_all_still_returns_something():
    """The fallback matters more than the parsing: a draft with a thin description beats no
    draft, and `full_description` is scraped text of wildly varying shape."""
    blob = "We need someone to build things. " * 200
    out = role_essentials(blob)
    assert out and "build things" in out


def test_empty_input_is_empty_not_an_exception():
    assert role_essentials("") == ""
    assert role_essentials(None) == ""


def test_it_never_returns_more_than_the_limit():
    out = role_essentials(AFFIRM, limit=800)
    assert len(out) <= 800


def test_it_cuts_at_a_boundary_not_mid_sentence():
    """A description that stops halfway through a responsibility invites the model to finish
    the thought itself — which is how an invented requirement reaches a recruiter."""
    out = role_essentials(AFFIRM, limit=900)
    assert not out.endswith((",", " and", " the", " a")), f"cut mid-phrase: …{out[-40:]!r}"


def test_a_bulleted_line_is_not_mistaken_for_a_header():
    """Bullets are short, capitalised and on their own line — everything a header looks like.
    Treating them as headers shatters the document into unusable confetti."""
    text = ("What you'll do\n" + "\n".join(f"- Ship feature {i}" for i in range(40)) +
            "\n\nBenefits\n" + "Dental. " * 100)
    secs = split_sections(text)
    assert len(secs) <= 3, f"bullets were parsed as headers: {[h for h, _ in secs]}"
    out = role_essentials(text)
    assert "Ship feature 39" in out and "Dental" not in out


# ── it has to be WIRED, not merely correct ─────────────────────────────────

def test_the_drafter_actually_uses_it(monkeypatch):
    """Caught by mutation: reverting `draft_email` to `full_description[:1200]` passed every
    test above, because they all exercise the extractor in isolation. A correct function nobody
    calls is the same as no function.

    The fixture puts 1,400 characters of boilerplate in front of the role, so the old window
    cannot reach the role and the new one cannot include the boilerplate.
    """
    from applypilot.networking import outreach

    seen = {}

    class Fake:
        def chat(self, messages, **kw):
            seen["user"] = messages[1]["content"]
            return '{"subject": "s", "body": "b", "linkedin_note": "n"}'

    monkeypatch.setattr(outreach, "get_client", lambda *_a, **_k: Fake())
    desc = ("About Us\n" + "We are reinventing widgets for the modern era. " * 32 +
            "\n\nAbout the Role\nYou will maintain the flux capacitor pipeline.")
    assert len(desc) > 1400 and "flux capacitor" not in desc[:1200], "fixture is not testing it"

    outreach.draft_email({"personal": {"name": "A"}},
                         {"url": "http://j/1", "title": "Engineer", "company": "Acme",
                          "full_description": desc},
                         {"id": "c1", "full_name": "Ada", "email": "a@x.com"})
    assert "flux capacitor pipeline" in seen["user"], (
        "the drafter is still reading the first N characters, so the role never reaches it")
    assert "reinventing widgets" not in seen["user"], "the company boilerplate reached the prompt"
