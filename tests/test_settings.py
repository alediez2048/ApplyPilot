"""ARCH-6: one schema for every environment variable.

The failure this exists to stop: `FOLLOWUP_SCHEDULE=48;96` used to parse to zero integers,
fall back to 48/96/168, and send follow-ups on a schedule the operator did not choose — with
no warning, no log line, and nothing in the UI to suggest anything had happened.

`test_the_registry_covers_every_variable_the_code_reads` is the one that ages well: a schema
nobody updates is worse than no schema, because it looks authoritative.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from applypilot import settings


# ── the failure that motivated the ticket ───────────────────────────────────

def test_the_semicolon_typo_is_caught_and_explained():
    problems = settings.validate({"FOLLOWUP_SCHEDULE": "48;96"})
    assert len(problems) == 1
    assert "FOLLOWUP_SCHEDULE" in problems[0]
    assert "';'" in problems[0], "the message should name the likely mistake, not just reject it"


@pytest.mark.parametrize("raw", ["48 96", "48, ninety", "0,96", "-48", "48,,96,x"])
def test_malformed_schedules_are_rejected(raw):
    assert settings.validate({"FOLLOWUP_SCHEDULE": raw}), f"{raw!r} was accepted"


@pytest.mark.parametrize("raw", ["", "   "])
def test_an_empty_value_means_use_the_default(raw):
    """`FOLLOWUP_SCHEDULE=` is how a .env line gets disabled without deleting it.

    Treating that as malformed would fail startup on a perfectly ordinary edit.
    """
    values, problems = settings.resolve({"FOLLOWUP_SCHEDULE": raw})
    assert not problems and values["FOLLOWUP_SCHEDULE"] == [48, 96, 168]


@pytest.mark.parametrize("raw,expected", [
    ("48,96,168", [48, 96, 168]),
    (" 24 , 48 ", [24, 48]),
    ("72", [72]),
])
def test_valid_schedules_parse(raw, expected):
    values, problems = settings.resolve({"FOLLOWUP_SCHEDULE": raw})
    assert not problems and values["FOLLOWUP_SCHEDULE"] == expected


@pytest.mark.parametrize("name,raw", [
    ("OUTREACH_DAILY_LIMIT", "lots"),
    ("OUTREACH_ATTACH_DOCS", "maybe"),
    ("APPLY_STALE_MINUTES", "30m"),
])
def test_every_kind_reports_what_it_expected(name, raw):
    problems = settings.validate({name: raw})
    assert problems and name in problems[0] and "expected" in problems[0]


def test_a_bad_value_still_leaves_a_usable_default():
    """A caller that chooses to continue runs on documented behaviour, not half-parsed input."""
    values, problems = settings.resolve({"FOLLOWUP_SCHEDULE": "nonsense"})
    assert problems
    assert values["FOLLOWUP_SCHEDULE"] == [48, 96, 168]


def test_all_problems_are_reported_at_once():
    """Otherwise a broken .env is fixed one restart at a time."""
    problems = settings.validate({"FOLLOWUP_SCHEDULE": "48;96",
                                  "OUTREACH_DAILY_LIMIT": "lots",
                                  "OUTREACH_ATTACH_DOCS": "sure"})
    assert len(problems) == 3


# ── one knob per concept ────────────────────────────────────────────────────

def test_followup_after_days_is_derived_from_the_schedule():
    """These two said the same thing in different units and defaulted independently."""
    values, _ = settings.resolve({"FOLLOWUP_SCHEDULE": "72,144"})
    assert values["FOLLOWUP_AFTER_DAYS"] == 3          # 72h

    values, _ = settings.resolve({})
    assert values["FOLLOWUP_AFTER_DAYS"] == 2          # default 48h


def test_the_legacy_name_still_wins_and_warns():
    """Deprecated, not removed — someone's .env has it today."""
    values, _ = settings.resolve({"FOLLOWUP_SCHEDULE": "72,144", "FOLLOWUP_AFTER_DAYS": "9"})
    assert values["FOLLOWUP_AFTER_DAYS"] == 9
    warnings = settings.deprecations({"FOLLOWUP_AFTER_DAYS": "9"})
    assert warnings and "FOLLOWUP_SCHEDULE" in warnings[0]
    assert not settings.deprecations({}), "warned about a variable that was never set"


def test_the_checklist_reads_the_derived_value(monkeypatch):
    from applypilot.domain import checklist
    monkeypatch.setenv("FOLLOWUP_SCHEDULE", "120,240")
    monkeypatch.delenv("FOLLOWUP_AFTER_DAYS", raising=False)
    assert checklist.followup_after_days() == 5


def test_the_ladder_reads_the_schema(monkeypatch):
    from applypilot.domain.followup import EMAIL, channel_schedule
    monkeypatch.setenv("FOLLOWUP_SCHEDULE", "12,24,36")
    assert channel_schedule(EMAIL) == [12, 24, 36]


# ── secrets and reporting ───────────────────────────────────────────────────

def test_secrets_are_never_printed():
    rows = settings.describe({"OPENAI_API_KEY": "sk-do-not-leak-me",
                              "APOLLO_API_KEY": "apollo-secret"})
    blob = repr(rows)
    assert "sk-do-not-leak-me" not in blob and "apollo-secret" not in blob
    secret_rows = {r["name"]: r["value"] for r in rows if r["secret"]}
    assert secret_rows["OPENAI_API_KEY"] == "set"
    assert secret_rows["ANTHROPIC_API_KEY"] == "not set"


def test_every_secret_is_marked_as_one():
    """A key that is not flagged gets printed in full by `doctor --config`."""
    looks_secret = [s.name for s in settings.SETTINGS
                    if re.search(r"(API_KEY|PASSWORD|SESSION|TOKEN|SECRET)$", s.name)]
    missed = [n for n in looks_secret if not settings._BY_NAME[n].secret]
    assert not missed, f"these look like secrets but are not flagged: {missed}"


def test_describe_reports_a_source_for_every_setting():
    rows = settings.describe({"FOLLOWUP_SCHEDULE": "24"})
    assert len(rows) == len(settings.SETTINGS)
    assert {r["source"] for r in rows} <= {"env", ".env", "default"}
    assert next(r for r in rows if r["name"] == "FOLLOWUP_SCHEDULE")["source"] != "default"


# ── the registry has to stay honest ─────────────────────────────────────────

def test_the_registry_covers_every_variable_the_code_reads():
    """A schema that drifts is worse than none — it looks authoritative while lying.

    Scans the source for `os.environ.get("X")` / `os.getenv("X")` and requires each to be
    declared. OS-provided variables the app only reads (PATH-style) are exempt.
    """
    import applypilot
    root = pathlib.Path(applypilot.__file__).parent
    external = {"LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PATH", "HOME",
                "USERPROFILE", "APPDATA"}
    found: set[str] = set()
    for path in root.rglob("*.py"):
        if path.name == "settings.py":
            continue
        for m in re.finditer(r'os\.(?:environ\.get|getenv)\(\s*["\']([A-Z][A-Z0-9_]*)["\']',
                             path.read_text(encoding="utf-8")):
            found.add(m.group(1))
    undeclared = sorted(found - set(settings._BY_NAME) - external)
    assert not undeclared, (
        "these are read from the environment but missing from settings.SETTINGS: "
        + ", ".join(undeclared)
    )


def test_env_example_is_generated_and_current():
    """It is checked in, so it can go stale silently. Regenerate with
    `applypilot doctor --write-env-example`."""
    import applypilot
    repo_root = pathlib.Path(applypilot.__file__).parent.parent.parent
    example = repo_root / ".env.example"
    if not example.exists():
        pytest.skip(".env.example not present (installed package, not a checkout)")
    assert example.read_text(encoding="utf-8") == settings.render_env_example(), \
        ".env.example is stale — run `applypilot doctor --write-env-example`"


def test_every_setting_has_help_and_a_group():
    for s in settings.SETTINGS:
        assert s.help.strip(), f"{s.name} has no help text; it lands in .env.example blank"
        assert s.group, f"{s.name} has no group"
        assert s.kind in ("str", "int", "bool", "csv_int", "path"), f"{s.name}: {s.kind}"
