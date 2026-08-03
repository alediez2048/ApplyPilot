"""Resume and cover letter validation: banned words, fabrication detection, structural checks.

All validation is profile-driven -- no hardcoded personal data. The validator receives
a profile dict (from applypilot.config.load_profile()) and validates against the user's
actual skills, companies, projects, and school.

Validation modes
----------------
strict  -- banned words = hard errors that trigger retries (original behavior)
normal  -- banned words = warnings only; fabrication/structure = errors (default)
lenient -- banned words ignored; only fabrication and required structure checked
"""

import re
import logging

log = logging.getLogger(__name__)


# ── Universal Constants (not personal data) ───────────────────────────────

BANNED_WORDS: list[str] = [
    "passionate", "dedicated", "committed to",
    "utilizing", "utilize", "harnessing",
    "spearheaded", "spearhead", "orchestrated", "championed", "pioneered",
    "robust", "scalable solutions", "cutting-edge", "state-of-the-art", "best-in-class",
    "proven track record", "track record of success", "demonstrated ability",
    "strong communicator", "team player", "fast learner", "self-starter", "go-getter",
    "synergy", "cross-functional collaboration", "holistic",
    "transformative", "innovative solutions", "paradigm", "ecosystem",
    "proactive", "detail-oriented", "highly motivated",
    "seamless", "full lifecycle",
    "deep understanding", "extensive experience", "comprehensive knowledge",
    "thrives in", "excels at", "adept at", "well-versed in",
    "i am confident", "i believe", "i am excited",
    "plays a critical role", "instrumental in", "integral part of",
    "strong track record", "eager to", "eager",
    # Cover-letter-specific additions
    "this demonstrates", "this reflects", "i have experience with",
    "furthermore", "additionally", "moreover",
]

LLM_LEAK_PHRASES: list[str] = [
    "i am sorry", "i apologize", "i will try", "let me try",
    "i am at a loss", "i am truly sorry", "apologies for",
    "i keep fabricating", "i will have to admit", "one final attempt",
    "one last time", "if it fails again", "persistent errors",
    "i am having difficulty", "i made an error", "my mistake",
    "here is the corrected", "here is the revised", "here is the updated",
    "here is my", "below is the", "as requested",
    "note:", "disclaimer:", "important:",
    "i have rewritten", "i have removed", "i have fixed",
    "i have replaced", "i have updated", "i have corrected",
    "per your feedback", "based on your feedback", "as per the instructions",
    "the following resume", "the resume below",
    "the following cover letter", "the letter below",
]

# Known fabrication markers: completely unrelated tools/languages.
# Reasonable stretches (K8s, Terraform, Redis, Kafka etc.) are ALLOWED.
FABRICATION_WATCHLIST: set[str] = {
    # Languages with zero relation to the candidate's stack
    "c#", "c++", "golang", "rust", "ruby",
    "kotlin", "swift", "scala", "matlab",
    # Frameworks for wrong languages
    "spring", "django", "rails", "angular", "vue", "svelte",
    # Hard lies: certifications can't be stretched
    "certif", "certified", "pmp", "scrum master", "aws certified",
}

REQUIRED_SECTIONS: set[str] = {"SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}


# ── Helpers ───────────────────────────────────────────────────────────────

def _build_skills_set(profile: dict) -> set[str]:
    """Build the set of allowed skills from the profile's skills_boundary."""
    boundary = profile.get("skills_boundary", {})
    allowed: set[str] = set()
    for category in boundary.values():
        if isinstance(category, list):
            allowed.update(s.lower().strip() for s in category)
        elif isinstance(category, set):
            allowed.update(s.lower().strip() for s in category)
    return allowed


#: Dashes no human types on a US keyboard. An em dash in a cold email is the single most
#: recognisable "this was pasted out of a chatbot" signal there is, and a recipient who spots one
#: re-reads the whole message as machine-written — which costs more than the punctuation is worth.
#: U+2015 (horizontal bar) and U+2212 (minus) are here because they render identically and a
#: model reaches for them roughly as often once you have told it to avoid the obvious one.
_EM_DASHES = ("\u2014", "\u2015")
#: Dashes that are RANGES or SIGNS, not clause breaks. U+2212 is a real minus: turning "−20%"
#: into ", 20%" would invert what the sentence claims, which is a worse failure than the
#: punctuation it was fixing.
_HYPHEN_LIKE = ("\u2013", "\u2212")


def strip_ai_dashes(text: str) -> str:
    """Remove every em-style dash. Exposed separately so paths that skip the full sanitiser
    (the résumé assembler's ORIGINAL-text fallback, the renderer) can still call it.

    Order matters. A spaced dash is a clause break and becomes a comma; an unspaced one is
    usually a compound and becomes a comma too, but a dash at the START of a line is a bullet
    marker and must become a hyphen, not a stray comma opening the line.
    """
    if not text:
        return text or ""
    for d in _EM_DASHES:
        # Line-leading dash: a bullet, not a clause break.
        text = re.sub(rf"(?m)^(\s*){re.escape(d)}\s*", r"\1- ", text)
        text = text.replace(f" {d} ", ", ").replace(d, ", ")
    for d in _HYPHEN_LIKE:
        text = text.replace(d, "-")
    # ", ," and " ,": artefacts of a dash next to punctuation that was already there.
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\s+,", ",", text)
    return text


def sanitize_text(text: str) -> str:
    """Auto-fix common LLM output issues instead of rejecting."""
    text = strip_ai_dashes(text)
    text = text.replace("\u201c", '"').replace("\u201d", '"')   # smart double quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")   # smart single quotes
    return text.strip()


# ── JSON Field Validation ─────────────────────────────────────────────────

def _named_tools(base_text: str, profile: dict) -> set[str]:
    """Curated tools from `skills_boundary` that actually appear in the base résumé.

    A regex over capitalised tokens was tried first and was worse than useless: it flagged
    "KEY" and "WORK" from the section headings while MISSING Botify and Akamai, which are
    single capitalised words indistinguishable from "Led" or "Managed". Guessing at product
    names produces misleading warnings, and a misleading warning is worse than silence.

    `skills_boundary` is a list the operator curated. Intersecting it with the base résumé
    needs no heuristics at all.
    """
    low = (base_text or "").lower()
    out: set[str] = set()
    for items in (profile.get("skills_boundary") or {}).values():
        for raw in (items if isinstance(items, list) else [items]):
            tool = str(raw).strip().rstrip(".").strip()
            if ":" in tool:                       # "Tools and Platforms: Jira" -> "Jira"
                tool = tool.split(":")[-1].strip()
            if tool.lower().startswith("and "):   # "and GitHub" -> "GitHub"
                tool = tool[4:].strip()
            # Proper nouns only. Lowercase entries ("technical SEO", "tool calling") are
            # descriptions of ability, not names a keyword screen looks for.
            if len(tool) >= 3 and tool[0].isupper() and tool.lower() in low:
                out.add(tool)
    return out


# Spelled-out numbers matter: the model writes "Ten years" far more often than "10 years",
# and a digits-only regex returned None for exactly the phrasing it prefers — a check that
# silently passes on the common case is worse than no check.
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}
_YEARS = re.compile(
    r"(\d{1,2}|" + "|".join(_WORD_NUMBERS) + r")\s*\+?\s*(?:-|\s)?\s*years?\b", re.I)


def years_claim(text: str) -> int | None:
    """The largest "N years" figure in a résumé, or None. Digits or words.

    Largest, not first: "10+ years of experience ... 3 years in AI/ML" describes a career and
    a specialism, and the career figure is the one that must not shrink.
    """
    nums = []
    for m in _YEARS.finditer(text or ""):
        tok = m.group(1).lower()
        nums.append(int(tok) if tok.isdigit() else _WORD_NUMBERS[tok])
    return max(nums) if nums else None


def understated_experience(base_text: str, tailored_text: str) -> tuple[int, int] | None:
    """(base_years, tailored_years) when the rewrite shrank the claim, else None.

    Understating experience is a factual error against the operator's interest, and it is not
    hypothetical: a "10+ years" résumé came back saying "Seven years" because a worked example
    in the prompt used a different number and the model anchored on it. Only SHRINKING is
    reported — a résumé that says more than the base would be inflation, which the fabrication
    checks and the judge already cover.
    """
    base_y, new_y = years_claim(base_text), years_claim(tailored_text)
    if base_y is None or new_y is None or new_y >= base_y:
        return None
    return (base_y, new_y)


def verbatim_bullets(base_text: str, tailored_text: str) -> list[str]:
    """Bullets copied straight out of the base résumé.

    A structure-preserving prompt can over-correct: told firmly enough what never to change,
    the model returns the original sentences with the clauses shuffled. That happened on a
    real Zello run — the summary was fully rewritten and the work bullets were untouched, so
    the résumé read as "the base résumé with a new first paragraph".

    Exact-match only, deliberately. Similarity scoring would need a threshold, and a
    threshold on prose is a number nobody can defend; "you shipped my own sentence" needs no
    threshold.
    """
    from applypilot.scoring import resume_sections as RS

    def bullets(text: str) -> list[str]:
        # EXPERIENCE only. Education bullets are degrees and dates that must NOT be
        # reworded, and a skills section is a list — counting those as "not tailored" would
        # fire on every résumé ever generated and train the operator to ignore the warning.
        out = []
        for sec in RS.parse(text).sections:
            if sec.kind != RS.KIND_EXPERIENCE:
                continue
            out += [b.strip() for b in sec.bullets() if b.strip()]
        return out

    original = {b.lower() for b in bullets(base_text)}
    return [b for b in bullets(tailored_text) if b.lower() in original]


def _split_schools(raw: str) -> list[str]:
    """"Gauntlet AI; University of Texas" -> two schools that must each be present."""
    return [p.strip() for p in re.split(r"[;/|]|,\s*(?=[A-Z])", raw or "") if p.strip()]


def _sections_to_text(data: dict) -> str:
    """Flatten the returned sections into résumé-shaped text for text-level checks."""
    lines = []
    for sec in data.get("sections") or []:
        lines.append(str(sec.get("title") or "").upper())
        if sec.get("text"):
            lines.append(str(sec["text"]))
        for b in sec.get("bullets") or []:
            lines.append(f"- {b}")
        for e in sec.get("entries") or []:
            if isinstance(e, dict):
                lines.append(str(e.get("employer") or e.get("school") or ""))
                for b in e.get("bullets") or []:
                    lines.append(f"- {b}")
    return "\n".join(lines)


def _validate_sections(data: dict, profile: dict, mode: str = "normal") -> dict:
    """Validate the structure-preserving shape.

    The severity ladder is unchanged (CLAUDE.md): a missing preserved company or school is
    an ERROR and blocks; preserved projects are a warning; banned words are strict-only.
    What is new is checking the thing this rewrite exists to protect — that no section was
    dropped and no employer lost bullets, which is exactly how the old pipeline quietly
    deleted KEY STRENGTHS and cut five bullets to four.
    """
    errors: list[str] = []
    warnings: list[str] = []
    sections = data.get("sections") or []

    if not data.get("title"):
        warnings.append("Missing title")
    if not sections:
        return {"passed": False, "errors": ["No sections returned"], "warnings": warnings}

    resume_facts = profile.get("resume_facts", {})
    text_parts: list[str] = []
    for sec in sections:
        text_parts.append(str(sec.get("text") or ""))
        for b in sec.get("bullets") or []:
            text_parts.append(str(b))
        for e in sec.get("entries") or []:
            if isinstance(e, dict):
                text_parts.extend(str(e.get(k, "")) for k in ("employer", "role", "school",
                                                              "degree", "detail"))
                text_parts.extend(str(b) for b in (e.get("bullets") or []))
            else:
                text_parts.append(str(e))
    all_text = " ".join(text_parts)
    low = all_text.lower()

    for company in resume_facts.get("preserved_companies", []):
        if company.lower() not in low:
            errors.append(f"Preserved company missing: {company}")
    # `preserved_school` is often several schools in one string ("Gauntlet AI; University
    # of Texas"). That concatenation appears nowhere in the résumé — it only ever matched
    # because the OLD prompt instructed the model to echo `"{school} | {level}"` back, so
    # the validator was checking for a string it had just asked for. Check each school.
    for school in _split_schools(resume_facts.get("preserved_school", "")):
        if school.lower() not in low:
            errors.append(f"Preserved school missing: {school}")
    for project in resume_facts.get("preserved_projects", []):
        if project.lower() not in low:
            warnings.append(f"Preserved project missing: {project}")

    # Named tools are what a keyword screen matches on. "…through AEM, Botify, GA4, GSC"
    # becoming "…improving customer experience" makes the résumé worse, not tighter — and
    # the prompt asking nicely is not enforcement: one run kept Botify and the next dropped
    # it. A WARNING, not an error: a tool genuinely irrelevant to the role may be cut, and
    # blocking on that would be worse than reporting it.
    dropped = [t for t in _named_tools(profile.get("_base_resume_text", ""), profile)
               if t.lower() not in low]
    if dropped:
        warnings.append("Named tools dropped from the base résumé: " + ", ".join(sorted(dropped)[:12]))

    # Bullets shipped verbatim mean the résumé was not tailored, only reformatted. A WARNING:
    # one recycled bullet among fourteen is not worth a retry, and the count makes the
    # difference between "mostly rewritten" and "the base résumé with a new summary" visible.
    base_text = profile.get("_base_resume_text", "")
    if base_text:
        try:
            copied = verbatim_bullets(base_text, _sections_to_text(data))
        except Exception:  # noqa: BLE001 - a reporting nicety must never fail validation
            copied = []
        if copied:
            warnings.append(f"{len(copied)} bullet(s) copied verbatim from the base résumé "
                            f"(not tailored): {copied[0][:70]}…")
        # An ERROR, not a warning: this misrepresents the candidate downward, and a retry is
        # far cheaper than sending a résumé that undersells them.
        shrunk = understated_experience(base_text, _sections_to_text(data))
        if shrunk:
            errors.append(f"Years of experience understated: résumé says {shrunk[0]}+, "
                          f"the rewrite says {shrunk[1]}")

    if mode == "strict":
        for word in BANNED_WORDS:
            if re.search(rf"\b{re.escape(word)}\b", low):
                errors.append(f"Banned word: '{word}'")
    elif mode == "normal":
        for word in BANNED_WORDS:
            if re.search(rf"\b{re.escape(word)}\b", low):
                warnings.append(f"Banned word: '{word}'")

    return {"passed": not errors, "errors": errors, "warnings": warnings}


def validate_json_fields(data: dict, profile: dict, mode: str = "normal") -> dict:
    """Validate individual JSON fields from an LLM-generated tailored resume.

    Args:
        data:    Parsed JSON from the LLM (title, summary, skills, experience, projects, education).
        profile: User profile dict from load_profile().
        mode:    Validation strictness — "strict", "normal", or "lenient".
                 strict  → banned words are errors (trigger retries)
                 normal  → banned words are warnings (no retry)
                 lenient → banned words ignored entirely

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Structure-preserving output (a `sections` list mirroring the base résumé) is a
    # different shape entirely. Checking it against the fixed five-key schema reported
    # "Missing required field: summary/skills/experience" on every attempt — four wasted
    # LLM calls per résumé, invisible because aggressive mode forces lenient.
    if data.get("sections"):
        return _validate_sections(data, profile, mode)

    # Required keys — always checked regardless of mode
    for key in ("title", "summary", "skills", "experience", "projects", "education"):
        if key not in data or not data[key]:
            errors.append(f"Missing required field: {key}")
    if errors:
        return {"passed": False, "errors": errors, "warnings": warnings}

    # Collect all text for bulk checks
    all_text_parts: list[str] = [data["summary"]]

    # Skills: check for fabrication (always enforced)
    if isinstance(data["skills"], dict):
        skills_text = " ".join(str(v) for v in data["skills"].values()).lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_text:
                errors.append(f"Fabricated skill: '{fake}'")

    # Experience: preserved companies must be present (always enforced)
    resume_facts = profile.get("resume_facts", {})
    preserved_companies = resume_facts.get("preserved_companies", [])

    if isinstance(data["experience"], list):
        for company in preserved_companies:
            has_company = any(
                company.lower() in str(e.get("header", "")).lower()
                for e in data["experience"]
            )
            if not has_company:
                errors.append(f"Company '{company}' missing from experience")
        for entry in data["experience"]:
            for b in entry.get("bullets", []):
                all_text_parts.append(b)

    # Projects: preserved names are a WARNING, not an error (mirrors validate_tailored_resume).
    # Dropping a project that is irrelevant to the target role is legitimate tailoring -- the
    # tailor prompt explicitly allows it -- so this cannot be an error. What it does catch is a
    # rename: the name went missing while the project itself is still on the resume.
    if isinstance(data["projects"], list):
        project_headers = " ".join(
            str(e.get("header", "")) for e in data["projects"]
        ).lower()
        for project in resume_facts.get("preserved_projects", []):
            if project.lower() not in project_headers:
                warnings.append(f"Project '{project}' not found -- may have been renamed")
        for entry in data["projects"]:
            for b in entry.get("bullets", []):
                all_text_parts.append(b)

    # Education: preserved school must be present (always enforced)
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school:
        edu = str(data.get("education", ""))
        if preserved_school.lower() not in edu.lower():
            errors.append(f"Education '{preserved_school}' missing")

    # Bulk text checks
    all_text = " ".join(all_text_parts).lower()

    # LLM self-talk is always an error regardless of mode (indicates broken output)
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in all_text]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # Banned filler words — severity depends on mode
    if mode != "lenient":
        found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", all_text)]
        if found_banned:
            msg = f"Banned words: {', '.join(found_banned[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


# ── Full Resume Text Validation ───────────────────────────────────────────

def validate_tailored_resume(text: str, profile: dict, original_text: str = "") -> dict:
    """Programmatic validation of a tailored resume against the user's profile.

    Args:
        text: The tailored resume text to validate.
        profile: User profile dict from load_profile().
        original_text: The original base resume text (for fabrication comparison).

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})

    # 1. Check required sections exist (flexible matching)
    section_variants: dict[str, list[str]] = {
        "SUMMARY": ["summary", "professional summary", "profile"],
        "TECHNICAL SKILLS": ["technical skills", "skills", "tech stack", "core skills", "technologies"],
        "EXPERIENCE": ["experience", "work experience", "professional experience"],
        "PROJECTS": ["projects", "personal projects", "key projects", "selected projects"],
        "EDUCATION": ["education", "academic background"],
    }
    for section, variants in section_variants.items():
        if not any(v in text_lower for v in variants):
            errors.append(f"Missing required section: {section} (or variant)")

    # 2. Check name preserved (warn, don't error -- we can inject it)
    full_name = personal.get("full_name", "")
    if full_name and full_name.lower() not in text_lower:
        warnings.append(f"Name '{full_name}' missing -- will be injected")

    # 3. Check companies preserved
    for company in resume_facts.get("preserved_companies", []):
        if company.lower() not in text_lower:
            errors.append(f"Company '{company}' missing -- cannot remove real experience")

    # 4. Check projects preserved
    for project in resume_facts.get("preserved_projects", []):
        if project.lower() not in text_lower:
            warnings.append(f"Project '{project}' not found -- may have been renamed")

    # 5. Check school preserved
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school and preserved_school.lower() not in text_lower:
        errors.append(f"Education '{preserved_school}' missing")

    # 6. Check contact info preserved (warn, don't error -- we can inject)
    email = personal.get("email", "")
    phone = personal.get("phone", "")
    if email and email.lower() not in text_lower:
        warnings.append("Email missing -- will be injected")
    if phone and phone not in text:
        warnings.append("Phone missing -- will be injected")

    # 7. Scan TECHNICAL SKILLS section for fabricated tools
    skills_start = text_lower.find("technical skills")
    skills_end = text_lower.find("experience", skills_start) if skills_start != -1 else -1
    if skills_start != -1 and skills_end != -1:
        skills_block = text_lower[skills_start:skills_end]
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_block:
                errors.append(f"FABRICATED SKILL in Technical Skills: '{fake}'")

    # 8. Scan full document for fabrication watchlist items not in original
    if original_text:
        original_lower = original_text.lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in text_lower and fake not in original_lower:
                warnings.append(f"New tool/skill appeared: '{fake}' (not in original)")

    # 9. Em dashes (should be auto-fixed by sanitize_text, but safety net)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 10. Banned words (word-boundary matching)
    found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
    if found_banned:
        errors.append(f"Banned words: {', '.join(found_banned[:5])}")

    # 11. LLM self-talk leak detection
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 12. Duplicate section detection
    for section_name in ["summary", "experience", "education", "projects"]:
        count = text_lower.count(f"\n{section_name}\n") + text_lower.count(f"\n{section_name} \n")
        if text_lower.startswith(f"{section_name}\n"):
            count += 1
        if count > 1:
            errors.append(f"Section '{section_name}' appears {count} times.")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ── Cover Letter Validation ──────────────────────────────────────────────

def validate_cover_letter(text: str, mode: str = "normal", company: str = "") -> dict:
    """Programmatic validation of a cover letter.

    Args:
        text: The cover letter text to validate.
        mode: Validation strictness — "strict", "normal", or "lenient".
              strict  → banned words are errors (trigger retries); word limit enforced
              normal  → banned words are warnings; word limit is soft (+25 words)
              lenient → banned words ignored; word count not checked
        company: Employer name. If given, it MUST appear in the letter — a real run
              produced a well-tailored body that named DevRev nowhere and opened
              "Dear Hiring Manager,". The prompt asking is not enforcement.

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    # 0. The company must be named. A letter whose body is genuinely tailored but which
    #    never says who it is addressed to reads as a template — and that is the first
    #    thing a human notices. An ERROR (retryable) rather than a warning: it is cheap to
    #    regenerate and expensive to send.
    if company:
        stem = re.split(r"[,.]| Inc| LLC| Ltd| Corp", company.strip(), maxsplit=1)[0].strip()
        if len(stem) >= 3 and stem.lower() not in text_lower:
            msg = f"Cover letter never names the company ({stem})."
            (warnings if mode == "lenient" else errors).append(msg)

    # 1. Em dashes — always an error (sanitize_text should have caught these)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 2. Banned words — severity depends on mode
    if mode != "lenient":
        found = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
        if found:
            msg = f"Banned words: {', '.join(found[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    # 3. Word count
    words = len(text.split())
    if mode == "strict" and words > 250:
        errors.append(f"Too long ({words} words). Max 250.")
    elif mode == "normal" and words > 275:
        warnings.append(f"Long ({words} words). Target 250.")
    # lenient: no word count check

    # 4. LLM self-talk — always an error regardless of mode
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 5. Must start with "Dear" — always checked (preamble should have been stripped)
    stripped = text.strip()
    if not stripped.lower().startswith("dear"):
        errors.append("Must start with 'Dear Hiring Manager,'")

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}
