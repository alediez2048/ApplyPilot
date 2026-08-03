"""What an ATS actually sees, and which of the posting's words are missing.

Two checks that need no new dependency and no tricks.

**The round trip.** A résumé is judged by a parser before a human opens it, so the only fact
that matters is what comes back OUT of the PDF. Nothing verified that. It would have caught the
failure in CLAUDE.md §Lessons 10 immediately: a layout crash fell back to a Python renderer that
wrote a **380-character PDF with no WORK EXPERIENCE**, which is worse than an error because it
looks like a résumé. It would also have saved a wrong diagnosis on 2026-08-03, when the `.txt`
intermediate was read instead of the PDF and reported junk headers that were never in the file
anyone received.

**Keyword coverage, as a REPORT.** Which of the posting's terms appear in the résumé and which
do not. Deliberately not an inserter: if a posting says "Kubernetes" and you know Kubernetes,
writing the literal word is using their vocabulary for something true, and a parser matches
strings rather than meanings. Where it is NOT true the gap has to stay a gap, which is why a
human reads this and decides. Automating the insertion is the line between optimisation and
lying, and it is the operator's call to make, not the tool's.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

#: Headings a parser recognises. A résumé titled "WHERE I'VE MADE IMPACT" is legible to a human
#: and invisible to the machine that decides whether a human ever sees it.
STANDARD_HEADINGS = ("WORK EXPERIENCE", "EXPERIENCE", "EMPLOYMENT", "EDUCATION", "SKILLS",
                     "PERSONAL STATEMENT", "SUMMARY", "PROFILE", "KEY STRENGTHS", "PROJECTS")

#: Below this, the "résumé" is a stub. §Lessons 10's fallback produced 380 characters.
MIN_CHARS = 1200


def extract_pdf_text(path: str | Path) -> tuple[str, str]:
    """(text, how). `how` is "" when nothing could read it.

    Tries the two extractors that are commonly present but declares neither as a dependency —
    this project caps its runtime deps on purpose. Returning "" for `how` rather than an empty
    string for `text` is the point: "I could not check" and "the PDF is empty" are opposite
    findings, and a checker that conflates them reports a perfect score for an unreadable file.
    """
    p = Path(path)
    if not p.exists():
        return "", ""
    if shutil.which("pdftotext"):
        try:
            out = subprocess.run(["pdftotext", str(p), "-"], capture_output=True,
                                 text=True, timeout=30)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout, "pdftotext"
        except Exception:  # noqa: BLE001
            log.debug("pdftotext failed", exc_info=True)
    try:
        import pypdf
        reader = pypdf.PdfReader(str(p))
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        if text.strip():
            return text, "pypdf"
    except Exception:  # noqa: BLE001
        log.debug("pypdf failed", exc_info=True)
    return "", ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def ats_report(pdf_path: str | Path, *, name: str = "", email: str = "", phone: str = "",
               companies: tuple[str, ...] = (), role: str = "") -> dict:
    """What a parser can actually read back. Returns {ok, checks, text, how, note}.

    `ok` is False when anything a screener needs is unreadable. When no extractor is available
    `ok` is None — unknown, not fine.
    """
    text, how = extract_pdf_text(pdf_path)
    if not how:
        return {"ok": None, "checks": [], "text": "", "how": "",
                "note": ("no PDF text extractor available (install poppler for `pdftotext`, or "
                         "`pip install pypdf`) — the résumé was NOT verified")}

    flat = _norm(text)
    checks: list[dict] = []

    def check(label, passed, detail=""):
        checks.append({"label": label, "ok": bool(passed), "detail": detail})

    check("has a real text layer", len(text.strip()) >= MIN_CHARS,
          f"{len(text.strip())} chars extracted, need {MIN_CHARS}+")
    if name:
        # Surname alone: the header may render "J. Diez Magni" or drop a middle name, and a
        # full-string match would fail on a PDF a human reads perfectly.
        last = name.strip().split()[-1].lower()
        check("name is readable", last in flat, f"looked for {last!r}")
    if email:
        check("email is readable", email.lower() in flat, email)
    if phone:
        digits = re.sub(r"\D", "", phone)[-10:]
        got = re.sub(r"\D", "", text)
        check("phone is readable", bool(digits) and digits in got, phone)
    for co in companies:
        if co.strip():
            check(f"employer {co!r} present", _norm(co) in flat)
    if role:
        check("target role in the header", _norm(role) in _norm("\n".join(text.splitlines()[:6])),
              role)
    found_headings = [h for h in STANDARD_HEADINGS if h.lower() in flat]
    check("standard section headings", len(found_headings) >= 2, ", ".join(found_headings[:4]))
    # The thing this whole module exists to notice, from §Lessons 10.
    check("work history survived", any(h.lower() in flat
                                       for h in ("work experience", "experience", "employment")))
    check("no em dashes", not any(c in text for c in "—―"))

    failed = [c for c in checks if not c["ok"]]
    return {"ok": not failed, "checks": checks, "text": text, "how": how,
            "note": ("readable" if not failed
                     else "; ".join(f"{c['label']}" for c in failed))}


# ── keyword coverage ────────────────────────────────────────────────────────

#: Words that appear in every posting and mean nothing as a match signal. Without this the
#: report is 90% "team", "work", "experience" and the real gaps are buried.
_STOP = {
    "the", "and", "for", "with", "you", "your", "our", "we", "will", "are", "have", "has",
    "this", "that", "from", "they", "their", "them", "a", "an", "to", "of", "in", "on", "at",
    "as", "is", "be", "or", "by", "it", "its", "not", "but", "all", "can", "who", "what",
    "team", "teams", "work", "working", "experience", "role", "job", "company", "position",
    "years", "year", "skills", "ability", "strong", "excellent", "good", "great", "new",
    "help", "make", "build", "building", "using", "use", "well", "across", "within", "into",
    "more", "most", "than", "then", "when", "where", "how", "why", "also", "such", "may",
    "should", "would", "could", "must", "about", "other", "any", "every", "each", "one", "two",
    "candidate", "candidates", "applicant", "opportunity", "including", "include", "includes",
    "required", "requirements", "preferred", "qualifications", "responsibilities", "plus",
    "etc", "e.g", "i.e", "us", "do", "does", "done", "get", "go", "like", "look", "looking",
    # Legal and EEO wording. `role_essentials` removes these when a posting has clean section
    # headers; plenty do not, and then the report's top "missing keywords" were "Equal
    # Employment Opportunity", "California Employees" and "Disabilities" — advice to put
    # anti-discrimination boilerplate in a résumé.
    "equal", "employment", "opportunity", "employer", "citizenship", "clearance", "veteran",
    "veterans", "disabilities", "disability", "applicants", "accommodation", "accommodations",
    "benefits", "perks", "compensation", "salary", "resume", "cv", "apply", "application",
    "eeo", "affirmative", "protected", "gender", "race", "religion", "nationality",
    "employees", "individuals", "individual", "people", "person",
}

#: Capitalised or punctuated the way real technologies are. A bare capitalised word is a bad
#: signal — `_named_tools` in validator.py learned that the hard way, flagging "KEY" and "WORK"
#: from section headings while missing Botify and Akamai entirely.
#: `[ \t]+`, never `\s+`: `\s` matches a NEWLINE, so "…Clearance\nApplicants…" was captured as
#: the single term "Clearance Applicants" and a job description became a list of phrases that
#: exist nowhere in it.
_TERM = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:[.+#/-][A-Za-z0-9]+)+|"      # k8s-style, C++, CI/CD
                   r"[A-Z][a-zA-Z0-9]{2,}(?:[ \t]+[A-Z][a-zA-Z0-9]{2,}){0,2})\b")

#: URLs and domains match the technology shape and are never keywords. A bare "/" is NOT in
#: here: it would kill CI/CD, AI/ML and I/O, which are exactly the terms worth reporting. A real
#: URL always brings a scheme, a www, or a TLD with it.
_URLISH = re.compile(r"://|www\.|\.(?:com|io|org|net|co|ai|gov|edu|hq)\b", re.I)

#: Dotted single letters: U.S, a.k.a, e.g, i.e. Rejected by SHAPE rather than by length, because
#: a length rule that catches "U.S" also catches GPS, AWS, SQL and API — three-letter acronyms
#: are the single most common thing an ATS matches on, and dropping them gutted the report.
_DOTTED_ABBREV = re.compile(r"^(?:[A-Za-z]\.){1,}[A-Za-z]?\.?$")


def jd_terms(text: str, extra: tuple[str, ...] = ()) -> list[str]:
    """Candidate keywords from a job description, most-mentioned first.

    Frequency-ordered because a posting that says "Kubernetes" six times is telling you what the
    job is, and one that says it once in a nice-to-have list is not.

    Two filters, both learned by running this on a real posting and reading the output:

    1. The BOILERPLATE is stripped first, via `role_essentials`. Without it the top terms were
       "Additional Perks", "BENEFITS", "California Employees" and "check-ups" — a keyword report
       whose advice is to mention the dental plan.

    2. Sentence-initial capitals are dropped. "Deploy the model" and "Analyze the data" open
       bullets, so "Deploy" and "Analyze" looked like proper nouns and outranked real ones. This
       is exactly the failure `_named_tools` records: capitalisation alone flagged "KEY" and
       "WORK" from headings while missing Botify and Akamai. A term survives only if it appears
       at least once somewhere OTHER than the start of a line or sentence.
    """
    from applypilot.domain.jobdesc import role_essentials
    raw = role_essentials(text or "", limit=6000) or (text or "")

    # Whether THIS occurrence opens a sentence, judged per-match.
    #
    # The first version collected opener words into a set and rejected any term in it, which
    # meant appearing at a line start ONCE disqualified a term that also appeared mid-sentence
    # ten times. "Terraform is the standard. We run Terraform daily." lost Terraform entirely.
    # The rule is "appears at least once somewhere other than an opening", so it has to be
    # evaluated per occurrence, not per word.
    def _opens_a_sentence(at: int) -> bool:
        before = raw[:at].rstrip(" \t").rstrip("-•*·").rstrip(" \t")
        return (not before) or before.endswith(("\n", ".", "!", "?", ":", ";"))

    counts: dict[str, int] = {}
    positions: dict[str, int] = {}
    for m in _TERM.finditer(raw):
        term = m.group(1).strip()
        if len(term) < 3 or term.lower() in _STOP:
            continue
        if _URLISH.search(term):
            continue
        if all(w.lower() in _STOP for w in term.split()):
            continue
        # A phrase is only as good as its words: "Citizenship Required" is two stopwords wearing
        # capital letters, and `all(...)` above misses it because "Required" is not in the list
        # while "Citizenship" is. Any stopword in a multi-word term disqualifies it.
        if len(term.split()) > 1 and any(w.lower() in _STOP for w in term.split()):
            continue
        if _DOTTED_ABBREV.match(term):
            continue
        if len(re.sub(r"[^A-Za-z0-9]", "", term)) < 2:
            continue
        counts[term] = counts.get(term, 0) + 1
        # A term shaped like a technology (CI/CD, C++, k8s) is trustworthy wherever it sits;
        # a plain capitalised word has to earn it by appearing mid-sentence at least once.
        techy = bool(re.search(r"[.+#/-]", term))
        if techy or not _opens_a_sentence(m.start()):
            positions[term] = positions.get(term, 0) + 1
    counts = {t: n for t, n in counts.items() if positions.get(t)}

    # "hands-on" and "Hands-on" are one keyword. Merge case variants, keeping whichever spelling
    # the posting used most, so the report does not spend two slots telling you the same thing.
    merged: dict[str, tuple[str, int]] = {}
    for term, n in counts.items():
        key = term.lower()
        best, total = merged.get(key, (term, 0))
        merged[key] = (term if n > counts.get(best, 0) else best, total + n)
    counts = {v[0]: v[1] for v in merged.values()}
    for e in extra:                      # the operator's own curated skills, always considered
        if e.strip() and e.strip().lower() in raw.lower():
            counts.setdefault(e.strip(), 1)
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))]


def keyword_coverage(jd_text: str, resume_text: str, *, extra: tuple[str, ...] = (),
                     limit: int = 25) -> dict:
    """{covered, missing} — the posting's vocabulary against the résumé's.

    A REPORT, never an edit. `missing` is a list of things to look at, not a list of things to
    paste in: about half of any posting's terms describe work the candidate has genuinely never
    done, and the only person who can tell which half is the one whose name is on the document.
    """
    resume = _norm(resume_text)
    covered, missing = [], []
    for term in jd_terms(jd_text, extra)[:limit]:
        (covered if _norm(term) in resume else missing).append(term)
    total = len(covered) + len(missing)
    return {"covered": covered, "missing": missing, "total": total,
            "pct": round(100 * len(covered) / total) if total else 0}
