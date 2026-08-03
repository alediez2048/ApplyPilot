"""No em dashes in anything a recipient reads.

An em dash in a cold email is the clearest "pasted out of a chatbot" signal there is, and a
reader who spots one re-reads the whole message as machine-written. That costs more than the
punctuation is worth, so it is banned in generated output: emails, LinkedIn notes, texts, cover
letters, résumés.

Belt AND braces, because §Lessons 9 and 12 both say the same thing in different words: a prompt
instruction is not a guarantee. Every prompt says not to, AND the output is stripped anyway.

This file is about GENERATED text. Source comments and docs are internal and are not covered.
"""

from __future__ import annotations

import pathlib

import pytest

from applypilot.scoring.validator import sanitize_text, strip_ai_dashes

EM = "—"
EN = "–"
BAR = "―"
MINUS = "−"


# ── the stripper ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    (f"caching {EM} contributing to speed", "caching, contributing to speed"),
    (f"removal{EM}contributing to speed", "removal, contributing to speed"),
    (f"a {BAR} b", "a, b"),
])
def test_em_style_dashes_become_commas(raw, expect):
    assert strip_ai_dashes(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    (f"180{EN}220k", "180-220k"),
    (f"revenue fell {MINUS}20%", "revenue fell -20%"),
])
def test_ranges_and_signs_become_hyphens_not_commas(raw, expect):
    """U+2212 is a real minus. Turning "−20%" into ", 20%" would invert what the sentence
    claims, which is a worse failure than the punctuation it was fixing."""
    assert strip_ai_dashes(raw) == expect


def test_a_line_leading_dash_is_a_bullet_not_a_clause_break():
    """Résumés are lists. A dash opening a line is a bullet marker, and replacing it with a
    comma produces ", Led web performance work" — which reads as broken, not as clean."""
    assert strip_ai_dashes(f"{EM} Led web performance work") == "- Led web performance work"
    assert strip_ai_dashes(f"  {EM} Second bullet") == "  - Second bullet"


def test_it_does_not_leave_doubled_punctuation():
    assert strip_ai_dashes(f"done, {EM} and then") == "done, and then"


def test_a_plain_hyphen_is_untouched():
    """"large-scale" and "T-Mobile" are how people actually type. Over-correcting here would
    mangle the employer names the validator separately requires to be preserved."""
    text = "large-scale work at T-Mobile on a well-known e-commerce site"
    assert strip_ai_dashes(text) == text


def test_empty_and_none_are_safe():
    assert strip_ai_dashes("") == ""
    assert strip_ai_dashes(None) == ""


def test_sanitize_text_still_strips_them():
    """`sanitize_text` is what every drafter already calls. The dash rule has to live inside it,
    not only in the new helper, or existing paths keep shipping dashes."""
    assert EM not in sanitize_text(f"a {EM} b")


# ── every prompt says so too ────────────────────────────────────────────────

def test_every_generation_prompt_forbids_them():
    """A stripper alone leaves a comma where a dash was doing real work, and the sentence reads
    slightly off. Telling the model produces better prose; stripping guarantees the outcome.
    Neither is sufficient alone, which is the whole of §Lessons 12."""
    from applypilot.networking import outreach
    for name in ("_SYSTEM", "_FOLLOWUP_SYSTEM", "_LI_FOLLOWUP_SYSTEM", "_SMS_SYSTEM",
                 "_REPLY_SYSTEM"):
        prompt = getattr(outreach, name, "")
        assert prompt, f"{name} disappeared; this test is measuring nothing"
        assert "em dash" in prompt.lower(), f"{name} does not forbid em dashes"


def test_the_cover_letter_prompt_forbids_them():
    from applypilot.scoring.cover_letter import _build_cover_letter_prompt
    assert "em dash" in _build_cover_letter_prompt({}).lower()


def test_no_prompt_uses_one_itself():
    """The instruction cannot arrive in a sentence containing the thing it bans. §Lessons 9:
    what is IN the prompt comes back out of it, and an example of the forbidden character is
    the most direct version of that mistake possible.
    """
    from applypilot.networking import outreach
    for name in ("_SYSTEM", "_FOLLOWUP_SYSTEM", "_LI_FOLLOWUP_SYSTEM", "_SMS_SYSTEM",
                 "_REPLY_SYSTEM"):
        prompt = getattr(outreach, name, "")
        # The rule itself must SHOW the character once, in parentheses, so the model knows
        # exactly which glyph is meant. More than that and the prompt is modelling the habit.
        assert prompt.count(EM) <= 1, (
            f"{name} contains {prompt.count(EM)} em dashes; the prompt is demonstrating the "
            "thing it forbids")


# ── the last step before a PDF ──────────────────────────────────────────────

def test_the_render_payload_is_scrubbed_however_deep():
    """The catch-all. Three paths reach the renderer and only one is sanitised: model output
    goes through `sanitize_text`, but a section the model OMITS falls back to the original
    résumé text and short bullet lists are padded from the trailing originals. Neither fallback
    is cleaned, and the base résumé really did contain an em dash — so this is one omitted
    section away from a PDF, not a hypothetical.
    """
    from applypilot.scoring.resume_render import _scrub
    payload = {"header": f"Applied AI Engineer {EM} Austin",
               "sections": [{"title": "WORK", "bullets": [f"Led work {EM} shipped it",
                                                          "plain-hyphen bullet"]}],
               "meta": {"n": 3, "flag": True, "none": None}}
    out = _scrub(payload)
    assert EM not in str(out)
    assert out["sections"][0]["bullets"][1] == "plain-hyphen bullet"
    assert out["meta"] == {"n": 3, "flag": True, "none": None}, "non-strings were mangled"


def test_the_renderer_calls_it_before_writing():
    """A scrubber nobody calls is the same as no scrubber — the exact mutation that survived
    when `role_essentials` was added and nothing wired it in."""
    from applypilot.scoring import resume_render
    src = pathlib.Path(resume_render.__file__).read_text(encoding="utf-8")
    assert "json.dump(_scrub(request), fh)" in src, (
        "the render payload is written without scrubbing")


# ── the base résumé, which is the template everything derives from ──────────

def test_the_base_resume_has_none(tmp_path, monkeypatch):
    """It is the TEMPLATE: its text flows through tailoring and lands verbatim in any section
    the model omits. One dash there is a dash in an unknown number of PDFs."""
    from applypilot import config
    base = pathlib.Path(config.APP_DIR) / "resume.txt"
    if not base.exists():
        pytest.skip("no base résumé on this machine")
    text = base.read_text(encoding="utf-8")
    assert EM not in text and BAR not in text, (
        f"the base résumé contains {text.count(EM)} em dash(es); every tailored résumé that "
        "falls back to an original section inherits them")
