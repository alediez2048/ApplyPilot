"""`APPLY_ALLOW_CONTRACT` — letting the apply agent finish contract and hourly postings.

The motivating case, measured rather than imagined: an Ethos "Expert Opportunity" refused SIX
times in a row with `not_a_job_application`, ~20-30s each. The agent was right — the posting says
`Compensation: $80/hour`, `Commitment: Flexible, 5-20 hours per week`, and Ethos calls itself an
expert network — and the prompt said "FULL-TIME salaried positions only".

Two things this file exists to hold:

**Widening SCOPE must not widen SAFETY.** Permissions, biometrics, payment details, SSN and
executables are unchanged in both modes. What moves is the commercial shape of the work.

**The rule is REPLACED, not caveated.** §Lessons 40: appending "…but contract is fine" beneath a
line reading "FULL-TIME salaried positions only" is two instructions disagreeing in one prompt,
and the more emphatic one wins. The SMS ladder proved that and saying the other side louder
changed nothing.
"""

from __future__ import annotations

import pytest

from applypilot.apply import prompt

def _job(tmp_path):
    """A posting shaped like the one that prompted this: hourly, with a rate stated."""
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    return {"url": "https://agent.example.test/opportunities/product-manager-tech-expert",
            "title": "Expert Opportunity", "site": "Example",
            "full_description": "Compensation: $80/hour\nCommitment: 5-20 hours per week",
            "application_url": "", "tailored_resume_path": str(pdf)}


#: Everything that must be identical whichever way the flag is set. These are safety, not scope.
SAFETY = [
    "NEVER grant camera, microphone, screen sharing, or location permissions",
    "NEVER do video/audio verification, selfie capture, ID photo upload, or biometric",
    "NEVER install browser extensions, download executables",
    "NEVER enter payment info, bank details, or SSN/SIN",
    'NEVER click "Allow" on any browser permission popup',
]


def test_the_flag_is_off_by_default(monkeypatch):
    """The refusal is the safe default; turning it off is a deliberate act by the operator."""
    monkeypatch.delenv("APPLY_ALLOW_CONTRACT", raising=False)
    assert prompt.allow_contract() is False


@pytest.mark.parametrize("value,want", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("maybe", False),
])
def test_the_flag_reads_the_usual_spellings(monkeypatch, value, want):
    monkeypatch.setenv("APPLY_ALLOW_CONTRACT", value)
    assert prompt.allow_contract() is want


def test_it_is_a_declared_setting_so_doctor_can_show_it():
    """An undeclared env var is one nobody can find. `doctor --config` reads this registry, and
    `.env.example` is generated from it."""
    from applypilot import settings
    s = next((x for x in settings.SETTINGS if x.name == "APPLY_ALLOW_CONTRACT"), None)
    assert s is not None, "the flag is not in the settings registry"
    assert s.default is False
    assert s.group == "apply"


# ── scope: off ──────────────────────────────────────────────────────────────

def test_with_the_flag_off_contract_work_is_still_refused():
    off = prompt._build_scope_section(False)
    assert "FULL-TIME salaried positions only" in off
    assert "not_a_job_application" in off


# ── scope: on ───────────────────────────────────────────────────────────────

def test_with_the_flag_on_contract_work_is_in_scope():
    on = prompt._build_scope_section(True)
    assert "CONTRACT, hourly, freelance and part-time postings ARE in scope" in on
    assert "FULL-TIME salaried positions only" not in on, \
        "the old rule survived alongside the new one — the prompt now contradicts itself"


def test_the_old_rule_is_REPLACED_not_appended():
    """§Lessons 40. Two instructions disagreeing in one prompt is a code bug, not a wording
    problem, and the earlier/louder one wins whatever the later one says."""
    on, off = prompt._build_scope_section(True), prompt._build_scope_section(False)
    for phrase in ("FULL-TIME salaried positions only",
                   "NEVER agree to hourly/contract rates"):
        assert phrase in off and phrase not in on


def test_marketplace_ONBOARDING_is_still_refused_when_contract_is_allowed():
    """The line that matters in BOTH modes. Applying to a named opportunity is an application;
    creating an account to be listed is not — and without this the agent would build freelancer
    profiles on any site that asked."""
    on = prompt._build_scope_section(True)
    assert "STILL REFUSE marketplace ONBOARDING" in on
    assert "not_a_job_application" in on
    low = on.lower()
    assert "assessment" in low and "profile" in low


def test_a_stated_rate_is_accepted_rather_than_negotiated():
    """The posting names $80/hour as a fact. An agent that haggles on a fixed-rate contract is
    doing something the operator did not ask for, in a stranger's form."""
    on = prompt._build_scope_section(True)
    assert "not a negotiation" in on or "not an opening offer" in on


# ── safety is untouched ─────────────────────────────────────────────────────

@pytest.mark.parametrize("rule", SAFETY)
def test_no_safety_rule_moves_with_the_flag(rule, monkeypatch, tmp_path):
    """Whatever scope does, the agent still runs `bypassPermissions` on attacker-controlled
    careers pages. These lines are why that is survivable."""
    from applypilot.config import load_profile
    job = _job(tmp_path)
    for flag in ("0", "1"):
        monkeypatch.setenv("APPLY_ALLOW_CONTRACT", flag)
        text = prompt.build_prompt(job, "RESUME", load_profile(), {}, "COVER")
        assert rule in text, f"safety rule vanished with APPLY_ALLOW_CONTRACT={flag}: {rule}"


def test_the_whole_prompt_changes_only_where_it_should(monkeypatch, tmp_path):
    """A frozen-artifact style check: flipping the flag must move the scope block and the salary
    tail, and nothing else. Anything wider means the flag reaches further than its name says."""
    from applypilot.config import load_profile
    job = _job(tmp_path)
    monkeypatch.setenv("APPLY_ALLOW_CONTRACT", "0")
    off = prompt.build_prompt(job, "RESUME", load_profile(), {}, "COVER")
    monkeypatch.setenv("APPLY_ALLOW_CONTRACT", "1")
    on = prompt.build_prompt(job, "RESUME", load_profile(), {}, "COVER")

    only_off = set(off.splitlines()) - set(on.splitlines())
    only_on = set(on.splitlines()) - set(off.splitlines())
    for line in only_off | only_on:
        assert ("scope" in line.lower() or "CONTRACT" in line or "contract" in line
                or "freelanc" in line.lower() or "marketplace" in line.lower()
                or "salaried" in line or "hourly" in line.lower()
                or "availability" in line.lower() or "rate" in line.lower()
                or "assessment" in line.lower() or "opportunit" in line.lower()
                or "editorialise" in line or "account to be listed" in line), \
            f"the flag changed a line that is not about scope or rate:\\n  {line!r}"


# ── the salary floor, which would refuse it one step later ──────────────────

def test_a_fixed_contract_rate_is_not_measured_against_the_salaried_floor():
    """Widening scope alone buys NOTHING without this, and the numbers are why: the floor is
    $200,000, which is $96/hr, so an $80/hr posting is "below floor" and the agent refuses one
    step later. §Lessons 49 — a rule relaxed at one of the two places that enforce it.
    """
    from applypilot.config import load_profile
    profile = load_profile()
    on = prompt._build_salary_section(profile, allow_contract=True)
    off = prompt._build_salary_section(profile, allow_contract=False)
    assert "employer's terms" in on
    assert "employer's terms" not in off
    # And the floor still governs the case it was written for.
    assert "FLOOR" in on and "FLOOR" in off


def test_the_salary_section_defaults_to_the_old_behaviour():
    """Called with one argument anywhere else in the codebase, it must not silently widen."""
    from applypilot.config import load_profile
    assert "employer's terms" not in prompt._build_salary_section(load_profile())
