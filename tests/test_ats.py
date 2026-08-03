"""What an ATS reads back, and which of the posting's words are missing.

The round-trip check exists because nothing verified the artifact that actually gets attached.
§Lessons 10 is exactly that gap: a layout crash fell through to the HTML renderer, which wrote a
**380-character PDF with no WORK EXPERIENCE** and it went out on real applications, because a
stub looks like a résumé to everything except a parser.

It would also have prevented a wrong diagnosis on 2026-08-03, when the `.txt` intermediate was
read instead of the PDF and "14 of 16 résumés have a junk header" was reported about a string
that appears in zero delivered files.
"""

from __future__ import annotations

import pytest

from applypilot.scoring.ats import (
    MIN_CHARS,
    ats_report,
    extract_pdf_text,
    jd_terms,
    keyword_coverage,
)

JD = """
About the Role

You will build multi-agent systems in Rust and Python for export-controlled hardware.
Hands-on work with GPS, IMUs and low-level firmware. You will own CI/CD for the fleet.
Deploy models to the edge. Analyze telemetry from real-world deployments.

Benefits

Dental, vision and a generous equity package. Flexible Spending Wallets.

Equal Opportunity Employer

We consider all Applicants without regard to race or religion. California Employees
have additional rights. Citizenship Required for this position.
"""


# ── the round trip ──────────────────────────────────────────────────────────

def test_a_missing_file_is_unverified_not_a_pass():
    """"I could not check" and "the PDF is fine" are opposite findings. A checker that
    conflates them reports a perfect score for a file that does not exist."""
    text, how = extract_pdf_text("/nonexistent/nope.pdf")
    assert (text, how) == ("", "")
    rep = ats_report("/nonexistent/nope.pdf")
    assert rep["ok"] is None, "an unreadable PDF reported as OK"
    assert "NOT verified" in rep["note"]


def test_a_stub_pdf_fails_the_text_layer_check():
    """§Lessons 10's fallback produced 380 characters. That is the case this catches."""
    assert MIN_CHARS > 380, "the threshold is below the stub it exists to catch"


@pytest.mark.parametrize("field,value,label", [
    ("email", "me@example.com", "email is readable"),
    ("phone", "(512) 709-7014", "phone is readable"),
])
def test_contact_details_must_survive_the_pdf(monkeypatch, field, value, label):
    """A screener cannot reply to an address the parser could not read, however good it looks."""
    body = "x" * MIN_CHARS + "\nWORK EXPERIENCE\nDiez\n"
    monkeypatch.setattr("applypilot.scoring.ats.extract_pdf_text",
                        lambda _p: (body, "stub"))
    rep = ats_report("any.pdf", **{field: value})
    assert any(c["label"] == label and not c["ok"] for c in rep["checks"])

    monkeypatch.setattr("applypilot.scoring.ats.extract_pdf_text",
                        lambda _p: (body + value, "stub"))
    rep = ats_report("any.pdf", **{field: value})
    assert any(c["label"] == label and c["ok"] for c in rep["checks"])


def test_a_reformatted_phone_number_still_matches(monkeypatch):
    """The renderer writes "5127097014" while the profile holds "(512) 709-7014". Comparing the
    strings would fail on a PDF a human reads perfectly."""
    monkeypatch.setattr("applypilot.scoring.ats.extract_pdf_text",
                        lambda _p: ("x" * MIN_CHARS + " 5127097014 WORK EXPERIENCE", "stub"))
    rep = ats_report("any.pdf", phone="(512) 709-7014")
    assert next(c for c in rep["checks"] if c["label"] == "phone is readable")["ok"]


def test_the_surname_is_enough_for_the_name_check(monkeypatch):
    """Headers render "Jorge Alejandro Diez" on some résumés and "…Diez Magni" on others — a
    full-string match fails on half the corpus for no real reason."""
    monkeypatch.setattr("applypilot.scoring.ats.extract_pdf_text",
                        lambda _p: ("Jorge Alejandro Diez Magni " + "x" * MIN_CHARS, "stub"))
    rep = ats_report("any.pdf", name="Jorge Alejandro Diez")
    assert next(c for c in rep["checks"] if c["label"] == "name is readable")["ok"]


def test_a_missing_work_history_is_caught(monkeypatch):
    monkeypatch.setattr("applypilot.scoring.ats.extract_pdf_text",
                        lambda _p: ("PERSONAL STATEMENT\nEDUCATION\n" + "x" * MIN_CHARS, "stub"))
    rep = ats_report("any.pdf")
    assert not next(c for c in rep["checks"] if c["label"] == "work history survived")["ok"]


def test_em_dashes_in_a_pdf_are_caught(monkeypatch):
    monkeypatch.setattr("applypilot.scoring.ats.extract_pdf_text",
                        lambda _p: ("WORK EXPERIENCE\nLed work — shipped it" + "x" * MIN_CHARS,
                                    "stub"))
    assert not next(c for c in ats_report("any.pdf")["checks"]
                    if c["label"] == "no em dashes")["ok"]


def test_the_renderer_calls_the_check():
    """A verifier nobody calls is the same as no verifier — the mutation that survived when
    `role_essentials` shipped unwired."""
    from applypilot.scoring import pdf
    src = open(pdf.__file__, encoding="utf-8").read()
    body = src[src.index("def text_to_pdf") if "def text_to_pdf" in src else 0:]
    assert body.count("verify_pdf_is_readable(out)") >= 2, (
        "the check does not run on BOTH render paths; the HTML fallback is the one that "
        "produced the 380-character résumé")


def test_the_check_never_raises(monkeypatch):
    """Advisory by design. A PDF that exists and is imperfect beats no PDF at the moment
    somebody is trying to apply, so a failure here must not break the stage."""
    from applypilot.scoring.pdf import verify_pdf_is_readable
    monkeypatch.setattr("applypilot.scoring.ats.ats_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert verify_pdf_is_readable("whatever.pdf")["ok"] is None


# ── keyword coverage ────────────────────────────────────────────────────────

def test_real_technologies_are_found():
    terms = jd_terms(JD)
    for want in ("Rust", "GPS", "IMUs", "CI/CD"):
        assert want in terms, f"{want} was not extracted; found {terms[:12]}"


def test_benefits_and_eeo_never_reach_the_report():
    """The first version's top "missing keywords" were "Additional Perks", "BENEFITS",
    "California Employees" and "Disabilities" — a report whose advice is to put
    anti-discrimination boilerplate in a résumé."""
    terms = " | ".join(jd_terms(JD))
    for junk in ("Dental", "Equal Opportunity", "California Employees", "Citizenship",
                 "Applicants", "Flexible Spending"):
        assert junk not in terms, f"boilerplate {junk!r} reached the keyword report"


def test_sentence_initial_capitals_are_not_treated_as_proper_nouns():
    """"Deploy the model" and "Analyze telemetry" open sentences, so those words looked like
    technologies and outranked real ones. Same failure `_named_tools` records: capitalisation
    alone flagged "KEY" and "WORK" while missing Botify and Akamai."""
    terms = jd_terms(JD)
    assert "Deploy" not in terms and "Analyze" not in terms, terms[:12]


def test_terms_never_span_a_newline():
    """`\\s+` matches a newline, so two unrelated lines fused into one term and the report listed
    phrases that appear nowhere in the posting.

    The first version of this test used "Clearance\\nApplicants", which are both stopwords now —
    so the multi-word filter killed the term whatever the regex did, and the test passed while
    the bug was live. Mutation testing caught that. These are real terms that must survive
    individually and must never be fused.
    """
    # Both terms must also appear MID-sentence somewhere, or the sentence-initial filter drops
    # them for its own (correct) reasons and this test measures nothing.
    text = ("About the Role\nBuilt with Kubernetes\nTerraform is the standard here.\n"
            "We run Terraform and Kubernetes together every day.")
    terms = jd_terms(text)
    assert not any("\n" in t for t in terms), terms
    assert "Kubernetes" in terms and "Terraform" in terms, terms
    assert not any(t == "Kubernetes Terraform" for t in terms), (
        "two lines were fused into one term that appears nowhere in the posting")


def test_urls_are_not_keywords():
    text = "About the Role\nApply at jobs.ashbyhq.com/fluidstack/05c2 or see fluidstack.io today"
    terms = jd_terms(text)
    assert not any("ashbyhq" in t or "fluidstack.io" in t for t in terms), terms


def test_case_variants_are_one_keyword():
    """"hands-on" and "Hands-on" spent two slots saying the same thing."""
    text = "About the Role\nHands-on work is needed. This is hands-on and hands-on daily."
    assert len([t for t in jd_terms(text) if t.lower() == "hands-on"]) == 1


def test_coverage_splits_what_you_have_from_what_you_do_not():
    resume = "Built multi-agent systems in Rust. Owned CI/CD. WORK EXPERIENCE"
    cov = keyword_coverage(JD, resume)
    assert "Rust" in cov["covered"] and "CI/CD" in cov["covered"]
    assert "GPS" in cov["missing"] and "IMUs" in cov["missing"]
    assert cov["pct"] == round(100 * len(cov["covered"]) / cov["total"])


def test_it_reports_and_never_edits():
    """The line between optimisation and lying. About half of any posting's terms describe work
    the candidate has never done, and only the person whose name is on the document can say
    which half — so this returns lists, never modified text."""
    cov = keyword_coverage(JD, "WORK EXPERIENCE\nnothing relevant here")
    assert set(cov) == {"covered", "missing", "total", "pct"}
    assert all(isinstance(v, (list, int)) for v in cov.values())


def test_an_empty_posting_does_not_divide_by_zero():
    cov = keyword_coverage("", "anything")
    assert cov["total"] == 0 and cov["pct"] == 0
