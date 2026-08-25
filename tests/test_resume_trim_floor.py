"""A trimmed résumé must still look like a résumé.

Reported from a live PDF: the text it was rendered from had 5/5/4 bullets and the PDF had
**4/1/1**, with a 731-character personal statement and 542 characters of key strengths untouched.
Nothing said so — the validator and the fabrication judge both run on the TEXT, before the PDF
exists, and `_REPORT.json` said "approved".

Two causes, both in `render.mjs`:

  * the trim ORDER drained the OLDEST role to exactly one bullet before touching anything else,
    and never trimmed the skills section at all;
  * the skills section is `skills: [{category, value}]` in the RENDER block and `bullets` only in
    the tailor's own JSON, so a check written against `bullets` silently matched nothing.

The operator's ordering, which is what these tests pin: at least three bullets per company, and
the personal statement and key strengths go first.
"""
import json
import subprocess

import pytest

from applypilot import config
from applypilot.scoring import resume_render

MIN = 3


def _resume(bullets=(6, 6, 5), summary_len=900, skills=2):
    return {
        "contactInfo": {"name": "Jorge Alejandro Diez Magni", "title": "IS Technology Partner",
                        "email": "a@b.test", "phone": "5125550000"},
        "sections": [
            {"title": "PERSONAL STATEMENT", "kind": "summary", "text": "Sentence about work. " * (summary_len // 20)},
            {"title": "WORK EXPERIENCE", "kind": "experience", "entries": [
                {"header": f"Employer{i} — Some Role", "subtitle": "2020/2024",
                 "bullets": [f"Did a substantial thing number {j} with measurable outcomes and detail. " * 2
                             for j in range(n)]}
                for i, n in enumerate(bullets)]},
            {"title": "KEY STRENGTHS", "kind": "skills", "skills": [
                {"category": f"Area {i}", "value": "A long comma separated list of capabilities, " * 8}
                for i in range(skills)]},
        ],
    }


def _render(tmp_path, resume, fit="auto"):
    runtime = resume_render.ensure_runtime()
    if runtime is None:
        pytest.skip("resume renderer runtime is not installed")
    req = tmp_path / "req.json"
    out = tmp_path / "out.pdf"
    req.write_text(json.dumps({"schemaVersion": resume_render.SCHEMA_VERSION,
                               "options": {"kind": "resume", "fit": fit, "theme": "classic"},
                               "resume": resume}), encoding="utf-8")
    proc = subprocess.run([config.get_node_path(), str(runtime / "render.mjs"), str(req), str(out)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-1500:]
    return out, proc.stderr


def _bullets_per_role(pdf):
    from applypilot.scoring import ats
    txt, _ = ats.extract_pdf_text(str(pdf))
    counts, cur = [], None
    for line in [x.strip() for x in (txt or "").splitlines() if x.strip()]:
        if line.startswith("Employer"):
            counts.append(0)
            cur = True
        elif line.startswith("•") and cur:
            counts[-1] += 1
        elif line in ("EDUCATION", "KEY STRENGTHS"):
            cur = None
    return counts



def test_no_company_is_cut_below_three_bullets(tmp_path):
    """The reported bug: two employers reduced to a single line each."""
    pdf, err = _render(tmp_path, _resume())
    counts = _bullets_per_role(pdf)
    assert counts, "no roles rendered at all"
    assert min(counts) >= MIN, f"a company was cut to {min(counts)} bullets: {counts} — {err[-400:]}"



def test_the_roles_stay_within_one_bullet_of_each_other(tmp_path):
    """Trimming evenly is what stops 4/1/1. It reads as a deliberate résumé, not a broken one."""
    counts = _bullets_per_role(_render(tmp_path, _resume())[0])
    assert max(counts) - min(counts) <= 1, f"lopsided: {counts}"



def test_the_summary_and_skills_are_spent_before_any_bullet(tmp_path):
    """The operator's own ordering, and the opposite of what shipped."""
    _, err = _render(tmp_path, _resume())
    assert "TRIMMED" in err, "a résumé this long must have been trimmed"
    line = [x for x in err.splitlines() if "TRIMMED to fit" in x][0]
    assert "personal statement" in line, "prose survived while bullets were cut"
    assert "key-strengths" in line, (
        "the skills section was never trimmed — it is `skills`, not `bullets`, in the render block")



def test_the_trim_is_reported_at_all(tmp_path):
    """It was silent, and that is why 8 of 14 bullets went unnoticed. Everything upstream
    validates the text; only this line describes the file that actually ships."""
    _, err = _render(tmp_path, _resume())
    assert "resume-renderer: TRIMMED" in err



def test_a_short_resume_is_not_trimmed_at_all(tmp_path):
    """Guard the guard: trimming everything makes the report meaningless."""
    _, err = _render(tmp_path, _resume(bullets=(2, 2), summary_len=120, skills=1))
    assert "TRIMMED" not in err
    counts = _bullets_per_role(_render(tmp_path, _resume(bullets=(2, 2), summary_len=120, skills=1))[0])
    assert counts == [2, 2], f"a résumé that already fits was altered: {counts}"


def test_python_drains_the_notes_once():
    """A second render must not inherit the first one's notes."""
    resume_render.LAST_TRIM_NOTES.clear()
    resume_render.LAST_TRIM_NOTES.append("TRIMMED to fit one page: something")
    assert resume_render.take_trim_notes() == ["TRIMMED to fit one page: something"]
    assert resume_render.take_trim_notes() == []


def test_the_floor_holds_even_when_holding_it_costs_a_second_page():
    """The case the floor exists for, and the one a modest fixture cannot see.

    A résumé that still overflows at three bullets per role must come out at three bullets per
    role on two pages — NOT one page with roles cut to a single line. A mutation lowering the
    floor to 1 survived until this test existed, because the smaller fixture always fit before
    the floor was reached (§Lessons 124: ask what the mutated code would do, not merely whether
    the fixture has more than one row).
    """
    import tempfile
    from pathlib import Path
    big = _resume(bullets=(9, 9, 9, 9, 9), summary_len=1600, skills=4)
    with tempfile.TemporaryDirectory() as td:
        pdf, err = _render(Path(td), big)
        counts = _bullets_per_role(pdf)
    assert counts, "no roles rendered"
    assert min(counts) >= MIN, (
        f"a role was cut below the {MIN}-bullet floor to save a page: {counts}")
    assert "still 2 pages" in err, (
        "a résumé this dense must report that it kept the floor instead of cutting further")
