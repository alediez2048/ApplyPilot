"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

THIS IS THE HEAVIEST REFACTOR. Every piece of personal data -- name, email, phone,
skills, companies, projects, school -- is loaded at runtime from the user's profile.
Zero hardcoded personal information.

The LLM returns structured JSON, code assembles the final text. Header (name, contact)
is always code-injected, never LLM-generated. Each retry starts a fresh conversation
to avoid apologetic spirals.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    sanitize_text,
    validate_json_fields,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _aggressive_enabled() -> bool:
    """Aggressive JD-matching tailoring (user opt-in). Skills-match the JD hard; skip the
    fabrication judge. Real companies/school/degrees are still preserved (those are
    background-checkable). Toggle with TAILOR_AGGRESSIVE."""
    import os
    return os.environ.get("TAILOR_AGGRESSIVE", "0").lower() in {"1", "true", "yes", "on"}


def _never_employer_rule(resume_facts: dict) -> str:
    """A hard rule keeping bootcamps/programs out of the EXPERIENCE section.

    Some entities (e.g. the Gauntlet AI bootcamp) are education, not jobs — LinkedIn may list them
    under Experience, but they must never appear as an employer/work history on the resume.
    """
    names = resume_facts.get("never_list_as_employer") or []
    if not names:
        return ""
    joined = ", ".join(names)
    return (f"- NEVER list these as an employer or under work experience — they are EDUCATION/"
            f"training programs (bootcamps), and must appear ONLY in the Education section: {joined}.")


def _preserved_projects_rule(resume_facts: dict) -> str:
    """A rule keeping real project NAMES intact whenever a project is included.

    Deliberately softer than the preserved-companies rule. The validator treats a missing company
    as an error but a missing project as a warning ("may have been renamed"), because dropping a
    project that is irrelevant to the target role is legitimate tailoring. What is never allowed
    is renaming a real project or inventing one, so that is exactly what this rule forbids.

    Returns "" when the profile lists no projects — including the leading newline — so a profile
    without projects produces a byte-identical prompt. Contains no em dash on purpose: the prompt
    bans them and the validator treats one as a hard error, so the rule must not prime for them.
    """
    names = resume_facts.get("preserved_projects") or []
    if not names:
        return ""
    joined = ", ".join(names)
    return (f"\n- Real projects: {joined}. Reorder them, reword their bullets, or drop ones irrelevant "
            f"to this role. But any project you DO include must keep its real name exactly as written. "
            f"Never rename a real project and never invent a project name.")


def _build_aggressive_tailor_prompt(profile: dict) -> str:
    """Aggressive variant: mirror the JD's required skills/keywords into the resume to
    maximize recruiter/ATS match. Preserves real employers, school, and degrees (inventing
    those is background-checkable), but freely adds the JD's technologies to the skills and
    reframes experience to read as if the candidate has done that exact work."""
    resume_facts = profile.get("resume_facts", {})
    companies = resume_facts.get("preserved_companies", [])
    school = resume_facts.get("preserved_school", "")
    education = profile.get("experience", {})
    education_level = education.get("education_level", "")
    companies_str = ", ".join(companies) if companies else "N/A"
    banned_str = ", ".join(BANNED_WORDS)
    return f"""You are a senior technical recruiter rewriting a resume to MAXIMIZE match with a
specific job description. Your only goal: make this candidate look like the ideal hire so they
get the interview.

Take the base resume and the job description. Return a tailored resume as a JSON object.

## STRATEGY — MATCH THE JD AGGRESSIVELY:
- Read the job description's required skills, tools, frameworks, and keywords.
- Put the JD's must-have technologies FRONT AND CENTER in the Skills section — include the
  specific tools/languages/frameworks the JD asks for so ATS keyword filters and the recruiter
  scan both hit.
- Reframe EVERY experience bullet so it reads as if the candidate has done the exact work the
  JD describes. Use the JD's own terminology. Make the overlap obvious.
- Rewrite the Summary to mirror the role: lead with the JD's top requirements as if they are
  the candidate's core strengths.
- Reorder/emphasize projects and bullets so the most JD-relevant appear first.

## PRESERVE (do NOT change — these are background-checkable):
- Real employers: {companies_str} -- names and the fact of employment stay as-is.
- Real school: {school} and degree level: {education_level}.
- Do NOT invent employers, job titles at fake companies, degrees, or certifications.
  Everything else (skills, tools, framings, emphasis) should match the JD as closely as possible.{_preserved_projects_rule(resume_facts)}
{_never_employer_rule(resume_facts)}

## VOICE:
- Write like a real engineer. Short, direct, concrete. Quantify impact where you can.
- No em dashes. Use commas, periods, or hyphens.
- Avoid these filler words: {banned_str}

## FORMAT:
- Must fit 1 page. Max 4 bullets per section.

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary.

{{"title":"Role Title","summary":"2-3 JD-matched sentences.","skills":{{"Languages":"...","Frameworks":"...","DevOps & Infra":"...","Databases":"...","Tools":"..."}},"experience":[{{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}}],"projects":[{{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}}],"education":"{school} | {education_level}"}}"""


def _build_tailor_prompt(profile: dict) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    All skills boundaries, preserved entities, and formatting rules are
    derived from the profile -- nothing is hardcoded.
    """
    if _aggressive_enabled():
        return _build_aggressive_tailor_prompt(profile)
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Format skills boundary for the prompt
    skills_lines = []
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(items)}")
    skills_block = "\n".join(skills_lines)

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    # Include ALL banned words from the validator so the LLM knows exactly
    # what will be rejected — the validator checks for these automatically.
    banned_str = ", ".join(BANNED_WORDS)

    education = profile.get("experience", {})
    education_level = education.get("education_level", "")

    return f"""You are a senior technical recruiter rewriting a resume to get this person an interview.

Take the base resume and job description. Return a tailored resume as a JSON object.

## RECRUITER SCAN (6 seconds):
1. Title -- matches what they're hiring?
2. Summary -- 2 sentences proving you've done this work
3. First 3 bullets of most recent role -- verbs and outcomes match?
4. Skills -- must-haves visible immediately?

## SKILLS BOUNDARY (real skills only):
{skills_block}

You MAY add 2-3 closely related tools (Kubernetes if Docker, Terraform if AWS, Redis if PostgreSQL). No unrelated languages/frameworks.

## TAILORING RULES:

TITLE: Match the target role. Keep seniority (Senior/Lead/Staff). Drop company suffixes and team names.

SUMMARY: Rewrite from scratch. Lead with the 1-2 skills that matter most for THIS role. Sound like someone who's done this job.

SKILLS: Reorder each category so the job's must-haves appear first.

Reframe EVERY bullet for this role. Same real work, different angle. Every bullet must be reworded. Never copy verbatim.

PROJECTS: Reorder by relevance. Drop irrelevant projects entirely.

BULLETS: Strong verb + what you built + quantified impact. Vary verbs (Built, Designed, Implemented, Reduced, Automated, Deployed, Operated, Optimized). Most relevant first. Max 4 per section.

## VOICE:
- Write like a real engineer. Short, direct.
- GOOD: "Automated financial reporting with Python + API integrations, cut processing time from 10 hours to 2"
- BAD: "Leveraged cutting-edge AI technologies to drive transformative operational efficiencies"
- BANNED WORDS (using ANY of these = validation failure — do not use them even once):
  {banned_str}
- No em dashes. Use commas, periods, or hyphens.

## HARD RULES:
- Do NOT invent work, companies, degrees, or certifications
- Do NOT change real numbers ({metrics_str})
- Preserved companies: {companies_str} -- names stay as-is
- Preserved school: {school}{_preserved_projects_rule(resume_facts)}
{_never_employer_rule(resume_facts)}
- Must fit 1 page.

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary. No "here is" preamble.

{{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{{"Languages":"...","Frameworks":"...","DevOps & Infra":"...","Databases":"...","Tools":"..."}},"experience":[{{"header":"Title at Company","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}}],"projects":[{{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}}],"education":"{school} | {education_level}"}}"""


def _build_judge_prompt(profile: dict) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Flatten allowed skills for the judge
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume to target a specific job. Your job is to catch LIES, not style changes.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
ISSUES: (list any problems, or "none")

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Change the title to match the target role
- Rewrite the summary from scratch for the target job
- Reorder bullets and projects to put the most relevant first
- Reframe bullets to use the job's language
- Drop low-relevance bullets and replace with more relevant ones from other sections
- Reorder the skills section to put job-relevant skills first
- Change tone and wording extensively

## WHAT IS FABRICATION (FAIL for these):
1. Adding tools, languages, or frameworks to TECHNICAL SKILLS that aren't in the original. The allowed skills are ONLY: {skills_str}
2. Inventing NEW metrics or numbers not in the original. The real metrics are: {metrics_str}
3. Inventing work that has no basis in any original bullet (completely new achievements).
4. Adding companies, roles, or degrees that don't exist.
5. Changing real numbers (inflating 80% to 95%, 500 nodes to 1000 nodes).

## WHAT IS NOT FABRICATION (do NOT fail for these):
- Rewording any bullet, even heavily, as long as the underlying work is real
- Combining two original bullets into one
- Splitting one original bullet into two
- Describing the same work with different emphasis
- Dropping bullets entirely
- Reordering anything
- Changing the title or summary completely

## TOLERANCE RULE:
The goal is to get interviews, not to be a perfect fact-checker. Allow up to 3 minor stretches per resume:
- Adding a closely related tool the candidate could realistically know is a MINOR STRETCH, not fabrication.
- Reframing a metric with slightly different wording is a MINOR STRETCH.
- Adding any LEARNABLE skill given their existing stack is a MINOR STRETCH.
- Only FAIL if there are MAJOR lies: completely invented projects, fake companies, fake degrees, wildly inflated numbers, or skills from a completely different domain.

Be strict about major lies. Be lenient about minor stretches and learnable skills. Do not fail for style, tone, or restructuring."""


# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Find outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def assemble_resume_text(data: dict, profile: dict) -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact) is ALWAYS code-injected from the profile,
    never LLM-generated. All text fields are sanitized.

    Args:
        data: Parsed JSON resume from the LLM.
        profile: User profile dict from load_profile().

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    lines: list[str] = []

    # Header -- always code-injected from profile
    lines.append(personal.get("full_name", ""))
    lines.append(sanitize_text(data.get("title", "Software Engineer")))

    # Location from search config or profile -- leave blank if not available
    # The location line is optional; the original used a hardcoded city.
    # We omit it here; the LLM prompt can include it if the user sets it.

    # Contact line
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if personal.get("github_url"):
        contact_parts.append(personal["github_url"])
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    # Summary
    lines.append("SUMMARY")
    lines.append(sanitize_text(data["summary"]))
    lines.append("")

    # Technical Skills
    lines.append("TECHNICAL SKILLS")
    if isinstance(data["skills"], dict):
        for cat, val in data["skills"].items():
            lines.append(f"{cat}: {sanitize_text(str(val))}")
    lines.append("")

    # Experience
    lines.append("EXPERIENCE")
    for entry in data.get("experience", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Projects
    lines.append("PROJECTS")
    for entry in data.get("projects", []):
        lines.append(sanitize_text(entry.get("header", "")))
        if entry.get("subtitle"):
            lines.append(sanitize_text(entry["subtitle"]))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Education
    lines.append("EDUCATION")
    lines.append(sanitize_text(str(data.get("education", ""))))

    return "\n".join(lines)




# ── Structure-preserving mode (2026-07-29) ────────────────────────────────
# The base résumé is the template. The previous prompt imposed a fixed five-section shape
# and the differences were not cosmetic — on a real run it deleted KEY STRENGTHS entirely
# (no slot existed), flattened EDUCATION to "Gauntlet AI; University of Texas | Bachelors"
# (the schema stored it as one string), invented a PROJECTS section by promoting a work
# bullet so the same achievement appeared twice, and cut 4,341 characters to 2,571.
#
# Three lines of the old prompt caused most of that: "Max 4 per section", a pre-flattened
# `"education": "{school} | {level}"` literal, and "Must fit 1 page."

def _structure_block(resume_text: str) -> str:
    """Describe the base résumé's own shape so the model reproduces it exactly."""
    from applypilot.scoring import resume_sections as RS

    r = RS.parse(resume_text)
    if not r.sections:
        return ""
    out = ["## THE STRUCTURE YOU MUST REPRODUCE",
           "",
           "This person's résumé has the sections below. Return EXACTLY these, with EXACTLY",
           "these titles, in EXACTLY this order. Do not rename, merge, reorder, add or drop",
           "any section.",
           "",
           "This constrains the SHAPE of the document, not its words. Every section still gets",
           "rewritten for this role — returning one unchanged is a failure, not a safe default.",
           ""]
    for i, sec in enumerate(r.sections, 1):
        n = len(sec.bullets())
        detail = f"{n} bullet(s)" if n else f"{len(sec.text())} chars of prose"
        out.append(f'{i}. "{sec.title}"  (kind={sec.kind}, {detail})')
        if sec.kind == RS.KIND_EXPERIENCE:
            for e in RS.experience_entries(sec):
                out.append(f'     - {e["employer"]} / {e["role"]} / {e["dates"]} '
                           f'-> keep all {len(e["bullets"])} bullets')
    out += ["",
            "BULLET COUNTS ARE A FLOOR, NOT A CEILING. Returning fewer bullets than the",
            "original is a failure. Every employer keeps every bullet; you rewrite them,",
            "you do not select among them.",
            ""]
    return "\n".join(out)


def _build_structured_tailor_prompt(profile: dict, resume_text: str) -> str:
    """Tailoring prompt that preserves the base résumé's sections."""
    resume_facts = profile.get("resume_facts", {})
    companies = resume_facts.get("preserved_companies", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", [])
    banned_str = ", ".join(BANNED_WORDS)
    aggressive = _aggressive_enabled()

    voice = ("Mirror the job description's vocabulary where it honestly applies to work "
             "this person actually did." if aggressive else
             "Write like a real engineer. Short, direct, concrete.")

    return f"""You are a senior technical recruiter rewriting a résumé to get this person an interview.

You are given their résumé and a job description. Rewrite the CONTENT for this role while
keeping the DOCUMENT'S STRUCTURE identical.

{_structure_block(resume_text)}
## WHAT TO CHANGE — REWRITE EVERY BULLET
Reframe EVERY bullet for THIS role. Same real work, a different angle. Every bullet must be
genuinely reworded — lead with the verb and the outcome this job cares about. Reordering the
clauses of the original sentence is NOT rewriting it.

The test, using an unrelated example so you do not borrow its wording or its shape:

  original : "Managed the warehouse inventory system, reducing stockouts by 30% using
              forecasting models and supplier integrations."
  NOT this : "Managed the warehouse inventory system, using forecasting models and supplier
              integrations to reduce stockouts by 30%."      <- same sentence, clauses moved
  REWRITTEN: "Cut stockouts 30% by rebuilding demand forecasting and wiring supplier feeds
              straight into the inventory system."

VARY THE SENTENCE SHAPE across bullets. If every bullet reads "Verb + object: tools, outcome"
the résumé looks generated. Some lead with the outcome, some with the problem, some with what
was built.

- Reorder bullets WITHIN an employer so the most relevant leads. Never delete one.
- Keep every named tool, platform and metric that is already there. Those are what a keyword
  screen matches on; dropping "AEM, Botify, GA4, GSC" to say "improved customer experience"
  makes the résumé worse, not tighter. Keep the fact, change the framing.
- Never copy a bullet verbatim.

## WHAT NEVER CHANGES
- Section titles, section order, employer names, role titles, dates, degrees, schools.
- Real numbers: {', '.join(real_metrics) if real_metrics else 'N/A'}
- Preserved companies: {', '.join(companies) if companies else 'N/A'}
- Preserved school: {school}{_preserved_projects_rule(resume_facts)}
{_never_employer_rule(resume_facts)}
- Never invent work, employers, degrees, certifications, or a section that is not listed above.

## VOICE
- {voice}
- BANNED WORDS (using ANY = validation failure): {banned_str}
- No em dashes. Use commas, periods, or hyphens.

## OUTPUT
Return ONLY valid JSON, no markdown fences, no commentary:

{{"title":"Role Title",
  "sections":[
    {{"title":"<EXACT title from above>","kind":"summary","text":"rewritten prose"}},
    {{"title":"<EXACT title>","kind":"experience","entries":[
        {{"employer":"...","role":"...","dates":"...","bullets":["...","..."]}}]}},
    {{"title":"<EXACT title>","kind":"education","entries":[
        {{"school":"...","degree":"...","detail":"...","date":"..."}}]}},
    {{"title":"<EXACT title>","kind":"skills","bullets":["Category: items","Category: items"]}}
  ]}}"""


def assemble_structured_resume_text(data: dict, profile: dict, resume_text: str) -> str:
    """Render the returned sections in the base résumé's own order and headings."""
    from applypilot.scoring import resume_sections as RS

    base = RS.parse(resume_text)
    returned = {str(s.get("title", "")).strip().upper(): s for s in data.get("sections", [])}
    lines: list[str] = []

    # Header comes from the RÉSUMÉ, not profile.json — the résumé is the source of truth,
    # and profile.json disagreed with it (it drops "Magni" from the name).
    lines.extend(base.header)
    if data.get("title"):
        lines.insert(1, sanitize_text(str(data["title"])))
    lines.append("")

    for sec in base.sections:
        got = returned.get(sec.title.upper())
        lines.append(sec.title)
        if got is None:
            # Model omitted it -> fall back to the original. A section is never lost.
            lines.extend(sec.lines)
            lines.append("")
            continue

        if sec.kind == RS.KIND_EXPERIENCE:
            originals = RS.experience_entries(sec)
            by_employer = {e["employer"].upper(): e for e in originals}
            for entry in got.get("entries", []) or []:
                emp = str(entry.get("employer", "")).strip()
                orig = by_employer.get(emp.upper(), {})
                lines.append(emp or orig.get("employer", ""))
                lines.append(sanitize_text(str(entry.get("role") or orig.get("role", ""))))
                lines.append(str(entry.get("dates") or orig.get("dates", "")))
                bullets = [b for b in (entry.get("bullets") or []) if str(b).strip()]
                # Never emit fewer bullets than the original had. Pad to the original COUNT
                # using the TRAILING originals — do not try to merge by text similarity. A
                # genuinely rewritten bullet does not resemble its source, so matching on a
                # prefix would treat every rewrite as new and duplicate the whole list
                # (3 rewrites + 3 originals = 6). The prompt asks for most-relevant-first,
                # so the bullets a truncating model drops are the trailing ones.
                original = orig.get("bullets", [])
                if len(bullets) < len(original):
                    bullets = list(bullets) + list(original[len(bullets):])
                for b in bullets:
                    lines.append(f"- {sanitize_text(str(b))}")
                lines.append("")
        elif sec.kind == RS.KIND_EDUCATION:
            for entry in got.get("entries", []) or []:
                if isinstance(entry, str):
                    lines.append(f"- {sanitize_text(entry)}")
                    continue
                label = " ".join(x for x in [str(entry.get("school", "")).strip(),
                                             f"({entry['degree']})" if entry.get("degree") else "",
                                             str(entry.get("detail", "")).strip()] if x)
                date = str(entry.get("date", "")).strip()
                lines.append(f"- {sanitize_text(label)}" + (f" | {date}" if date else ""))
            if not got.get("entries"):
                lines.extend(sec.lines)
            lines.append("")
        else:
            bullets = [b for b in (got.get("bullets") or []) if str(b).strip()]
            text = str(got.get("text") or "").strip()
            if text:
                lines.append(sanitize_text(text))
            for b in bullets:
                lines.append(f"- {sanitize_text(str(b))}")
            if not text and not bullets:
                lines.extend(sec.lines)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss.

    Args:
        original_text: Base resume text.
        tailored_text: Tailored resume text.
        job_title: Target job title.
        profile: User profile for building the judge prompt.

    Returns:
        {"passed": bool, "verdict": str, "issues": str, "raw": str}
    """
    judge_prompt = _build_judge_prompt(profile)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{original_text}\n\n---\n\n"
            f"TAILORED RESUME:\n{tailored_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client("heavy")
    response = client.chat(messages, max_tokens=512, temperature=0.1)

    passed = "VERDICT: PASS" in response.upper()
    issues = "none"
    if "ISSUES:" in response.upper():
        issues_idx = response.upper().index("ISSUES:")
        issues = response[issues_idx + 7:].strip()

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
) -> tuple[str, dict]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    Key design choices:
    - LLM returns structured JSON, code assembles the text (no header leaks)
    - Each retry starts a FRESH conversation (no apologetic spiral)
    - Issues from previous attempts are noted in the system prompt
    - Em dashes and smart quotes are auto-fixed, not rejected

    Args:
        resume_text:      Base resume text.
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".
                          strict  -- banned words trigger retries; judge must pass
                          normal  -- banned words = warnings only; judge can fail on last retry
                          lenient -- banned words ignored; LLM judge skipped

    Returns:
        (tailored_text, report) where report contains validation details.
    """
    # TAILOR_AGGRESSIVE used to force lenient here, which silently disabled the fabrication
    # judge AND every banned-word check. That was a defensible trade when the mode existed to
    # let the résumé mirror the JD's skills — content preservation depended on the prompt.
    # It no longer does: `assemble_structured_resume_text` enforces sections and bullet counts
    # mechanically, so the mode now only needs to change VOICE. Skipping fabrication detection
    # to get JD-matching vocabulary was buying something it no longer has to pay for.

    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
    }
    avoid_notes: list[str] = []
    tailored = ""
    client = get_client("heavy")
    # Structure-preserving mode when the base résumé actually has parseable sections.
    # Falls back to the legacy fixed schema otherwise, so an unstructured résumé (or a
    # profile with no résumé at all) behaves exactly as before.
    from applypilot.scoring import resume_sections as _RS
    _structured = len(_RS.parse(resume_text).sections) >= 2
    tailor_prompt_base = (_build_structured_tailor_prompt(profile, resume_text)
                          if _structured else _build_tailor_prompt(profile))

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        # Fresh conversation every attempt
        prompt = tailor_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
        ]

        raw = client.chat(messages, max_tokens=2048, temperature=0.4)

        # Parse JSON from response
        try:
            data = extract_json(raw)
        except ValueError:
            avoid_notes.append("Output was not valid JSON. Return ONLY a JSON object, nothing else.")
            continue

        # Layer 1: Validate JSON fields
        # The named-tool check needs the base résumé; pass it alongside the profile
        # rather than changing validate_json_fields' signature for every caller.
        validation = validate_json_fields(
            data, {**profile, "_base_resume_text": resume_text}, mode=validation_mode)
        report["validator"] = validation

        if not validation["passed"]:
            # Only retry if there are hard errors (warnings never block)
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            # Last attempt — assemble whatever we got
            tailored = (assemble_structured_resume_text(data, profile, resume_text)
                        if _structured else assemble_resume_text(data, profile))
            report["resume_data"] = data
            report["status"] = "failed_validation"
            return tailored, report

        # Assemble text (header injected by code, em dashes auto-fixed)
        tailored = (assemble_structured_resume_text(data, profile, resume_text)
                    if _structured else assemble_resume_text(data, profile))
        report["resume_data"] = data

        # Layer 2: LLM judge (catches subtle fabrication) — skipped in lenient mode
        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "approved"
            return tailored, report

        judge = judge_tailored_resume(resume_text, tailored, job.get("title", ""), profile)
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries:
                # In normal mode, only retry on judge failure if there are retries left
                if validation_mode != "lenient":
                    continue
            # Accept best attempt on last retry (all modes) or if lenient
            report["status"] = "approved_with_judge_warning"
            return tailored, report

        # Both passed
        report["status"] = "approved"
        return tailored, report

    report["status"] = "exhausted_retries"
    return tailored, report


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_tailoring(min_score: int = 7, limit: int = 20,
                  validation_mode: str = "normal") -> dict:
    """Generate tailored resumes for high-scoring jobs.

    Args:
        min_score:       Minimum fit_score to tailor for.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".

    Returns:
        {"approved": int, "failed": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    jobs = get_jobs_by_stage(conn=conn, stage="pending_tailor", min_score=min_score, limit=limit)

    if not jobs:
        log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Tailoring resumes for %d jobs (score >= %d)...", len(jobs), min_score)
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    stats: dict[str, int] = {"approved": 0, "failed_validation": 0, "failed_judge": 0, "error": 0}

    for job in jobs:
        completed += 1
        try:
            tailored, report = tailor_resume(resume_text, job, profile,
                                             validation_mode=validation_mode)

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
            prefix = f"{safe_site}_{safe_title}"

            # Save tailored resume text
            txt_path = TAILORED_DIR / f"{prefix}.txt"
            txt_path.write_text(tailored, encoding="utf-8")

            # Save structured resume JSON for the React-PDF renderer (sidecar).
            # Kept separate from the validation report; the renderer reads this.
            resume_data = report.pop("resume_data", None)
            if resume_data is not None:
                data_path = TAILORED_DIR / f"{prefix}_DATA.json"
                data_path.write_text(json.dumps(resume_data, indent=2), encoding="utf-8")

            # Save job description for traceability
            job_path = TAILORED_DIR / f"{prefix}_JOB.txt"
            job_desc = (
                f"Title: {job['title']}\n"
                f"Company: {job['site']}\n"
                f"Location: {job.get('location', 'N/A')}\n"
                f"Score: {job.get('fit_score', 'N/A')}\n"
                f"URL: {job['url']}\n\n"
                f"{job.get('full_description', '')}"
            )
            job_path.write_text(job_desc, encoding="utf-8")

            # Save validation report
            report_path = TAILORED_DIR / f"{prefix}_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            # Generate PDF for approved resumes (best-effort)
            # "approved_with_judge_warning" is also a success — resume was generated.
            pdf_path = None
            if report["status"] in ("approved", "approved_with_judge_warning"):
                try:
                    from applypilot.scoring.pdf import convert_to_pdf
                    pdf_path = str(convert_to_pdf(txt_path))
                except Exception:
                    log.debug("PDF generation failed for %s", txt_path, exc_info=True)

            result = {
                "url": job["url"],
                "path": str(txt_path),
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
                "status": report["status"],
                "attempts": report["attempts"],
            }
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "status": "error", "attempts": 0, "path": None, "pdf_path": None,
            }
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

        results.append(result)
        stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1

        elapsed = time.time() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        log.info(
            "%d/%d [%s] attempts=%s | %.1f jobs/min | %s",
            completed, len(jobs),
            result["status"].upper(),
            result.get("attempts", "?"),
            rate * 60,
            result["title"][:40],
        )

    # Persist to DB: increment attempt counter for ALL, save path only for approved
    now = datetime.now(timezone.utc).isoformat()
    _success_statuses = {"approved", "approved_with_judge_warning"}
    for r in results:
        if r["status"] in _success_statuses:
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["path"], now, r["url"]),
            )
        else:
            conn.execute(
                "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info(
        "Tailoring done in %.1fs: %d approved, %d failed_validation, %d failed_judge, %d errors",
        elapsed,
        stats.get("approved", 0),
        stats.get("failed_validation", 0),
        stats.get("failed_judge", 0),
        stats.get("error", 0),
    )

    return {
        "approved": stats.get("approved", 0),
        "failed": stats.get("failed_validation", 0) + stats.get("failed_judge", 0),
        "errors": stats.get("error", 0),
        "elapsed": elapsed,
    }
