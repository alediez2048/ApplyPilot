"""Read the base résumé's own structure, so tailoring can preserve it.

Until now the pipeline imposed a fixed five-section shape — SUMMARY / TECHNICAL SKILLS /
EXPERIENCE / PROJECTS / EDUCATION — regardless of what the base résumé actually contained.
Three consequences, all observed on a real run:

  - a section with no slot was silently DELETED (KEY STRENGTHS, which held every AI/ML tool)
  - EDUCATION was stored as one string, so "B.A. Economics B.S. Advertising | 2011-2015"
    collapsed to "Bachelors"
  - PROJECTS was invented by promoting a work bullet, so the same achievement appeared twice

The base résumé is the source of truth. This reads its sections — names, order, and
contents — and tailoring rewrites *within* them.

Deliberately dumb: uppercase-ish lines with no trailing punctuation are headings. That is a
convention the file already follows, and a parser that guesses less is a parser that
surprises less. Anything it cannot classify stays as free text and is passed through
untouched rather than dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A heading is a short line that is mostly capitals and does not end a sentence.
# "WORK EXPERIENCE" yes; "Led enterprise-scale platform initiatives..." no.
_HEADING = re.compile(r"^[A-Z][A-Z &/'-]{2,40}$")

# Section kinds the renderer knows how to lay out. Anything else renders as bullets/text,
# which is why an unrecognised section is preserved rather than lost.
KIND_SUMMARY = "summary"
KIND_EXPERIENCE = "experience"
KIND_EDUCATION = "education"
KIND_SKILLS = "skills"
KIND_TEXT = "text"

_KIND_HINTS = (
    (KIND_SUMMARY, ("summary", "statement", "profile", "objective", "about")),
    (KIND_EXPERIENCE, ("experience", "employment", "history", "work")),
    (KIND_EDUCATION, ("education", "academic", "training", "certification")),
    (KIND_SKILLS, ("skill", "strength", "competenc", "technical", "expertise")),
)


def classify(title: str) -> str:
    """Map a heading to a layout kind. Unknown headings are `text`, never dropped."""
    low = title.lower()
    for kind, hints in _KIND_HINTS:
        if any(h in low for h in hints):
            return kind
    return KIND_TEXT


@dataclass
class Section:
    title: str                      # VERBATIM from the résumé — never normalised
    kind: str
    lines: list[str] = field(default_factory=list)

    def bullets(self) -> list[str]:
        return [ln.lstrip("-•* ").strip() for ln in self.lines if ln.lstrip().startswith(("-", "•", "*"))]

    def text(self) -> str:
        return " ".join(ln.strip() for ln in self.lines if not ln.lstrip().startswith(("-", "•", "*"))).strip()


@dataclass
class Resume:
    header: list[str] = field(default_factory=list)     # name + contact, above any heading
    sections: list[Section] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Section]:
        return [s for s in self.sections if s.kind == kind]

    def titles(self) -> list[str]:
        return [s.title for s in self.sections]


def parse(text: str) -> Resume:
    """Split a résumé into its own sections, preserving order and heading text."""
    resume = Resume()
    current: Section | None = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            if current:
                current.lines.append("")
            continue
        if _HEADING.match(line.strip()):
            current = Section(title=line.strip(), kind=classify(line.strip()))
            resume.sections.append(current)
            continue
        if current is None:
            resume.header.append(line.strip())
        else:
            current.lines.append(line)
    # Trim trailing blanks that the blank-line passthrough collected.
    for s in resume.sections:
        while s.lines and not s.lines[-1].strip():
            s.lines.pop()
    return resume


def experience_entries(section: Section) -> list[dict]:
    """Split a work-experience block into {employer, role, dates, bullets}.

    The file's convention is employer / role / dates on three consecutive lines, then
    bullets. Anything that does not fit becomes a bullet rather than being discarded —
    losing a line of someone's work history to a parser guess is not an acceptable trade.
    """
    entries: list[dict] = []
    pending: list[str] = []

    def flush():
        if not pending:
            return
        head = [x for x in pending if not x.lstrip().startswith(("-", "•", "*"))]
        bl = [x.lstrip("-•* ").strip() for x in pending if x.lstrip().startswith(("-", "•", "*"))]
        entry = {"employer": "", "role": "", "dates": "", "bullets": bl}
        if head:
            entry["employer"] = head[0].strip()
        if len(head) > 1:
            entry["role"] = head[1].strip()
        if len(head) > 2:
            entry["dates"] = head[2].strip()
        if len(head) > 3:                      # unexpected extra lines keep their content
            entry["bullets"] = [h.strip() for h in head[3:]] + entry["bullets"]
        entries.append(entry)
        pending.clear()

    for line in section.lines:
        if not line.strip():
            continue
        starts_new = (not line.lstrip().startswith(("-", "•", "*"))
                      and any(p.lstrip().startswith(("-", "•", "*")) for p in pending))
        if starts_new:
            flush()
        pending.append(line)
    flush()
    return entries


def summarize(text: str) -> dict:
    """Structure for the prompt: what sections exist, in what order, and how big.

    Sizes matter — the previous pipeline cut a 4,341-character résumé to 2,571 and nobody
    noticed, because nothing recorded what the original looked like.
    """
    r = parse(text)
    return {
        "header": r.header,
        "sections": [
            {"title": s.title, "kind": s.kind,
             "bullet_count": len(s.bullets()),
             "chars": sum(len(x) for x in s.lines)}
            for s in r.sections
        ],
        "total_chars": len(text or ""),
    }
