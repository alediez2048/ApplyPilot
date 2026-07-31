"""One schema for every environment variable (ARCH-6).

Before this, ~40 variables were read with `os.environ.get(...)` scattered across 15 modules,
each with its own default, its own parsing, and its own silent fallback. Typo
`FOLLOWUP_SCHEDULE=48;96` and the ladder quietly reverted to 48/96/168 — no warning, no log
line, and follow-ups going out on a schedule you did not choose.

A dataclass and a few validators, **not pydantic**: keeping the runtime dependency count at
8 is an explicit non-goal to protect (`architecture-prd.md` §5).

Three things this buys:
  - a malformed value fails at startup naming the variable and what was expected
  - `applypilot doctor --config` shows every setting, its value, and where it came from
  - `.env.example` is generated from this file, so it cannot drift
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# Captured at import, BEFORE `config.load_env()` runs python-dotenv. That is the only way to
# tell "exported in your shell" from "written in ~/.applypilot/.env" — dotenv writes into
# os.environ and afterwards the two are indistinguishable.
_OS_ENV_AT_IMPORT = frozenset(os.environ)


class ConfigError(ValueError):
    """A setting was present but unusable. Names the variable and what was expected."""


@dataclass(frozen=True)
class Setting:
    name: str
    group: str
    kind: str                       # str | int | bool | csv_int | path
    default: Any
    help: str
    secret: bool = False
    deprecated_by: str | None = None
    choices: tuple[str, ...] = field(default=())


def _parse_bool(raw: str, s: Setting):
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{s.name}={raw!r} — expected a boolean (1/0, true/false, yes/no)")


def _parse_int(raw: str, s: Setting):
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"{s.name}={raw!r} — expected a whole number") from None


def _parse_csv_int(raw: str, s: Setting):
    """Comma-separated positive hours. The one that used to fail silently.

    `48;96` produced no integers at all and fell back to the default. Now it says so.
    """
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ConfigError(f"{s.name}={raw!r} — expected comma-separated hours, e.g. 48,96,168")
    out = []
    for p in parts:
        try:
            n = int(p)
        except ValueError:
            raise ConfigError(
                f"{s.name}={raw!r} — {p!r} is not a number. Expected comma-separated hours, "
                f"e.g. 48,96,168 (did you use ';' or a space instead of ','?)") from None
        if n <= 0:
            raise ConfigError(f"{s.name}={raw!r} — hours must be positive, got {n}")
        out.append(n)
    return out


def _parse_str(raw: str, s: Setting):
    v = raw.strip()
    if s.choices and v and v not in s.choices:
        raise ConfigError(f"{s.name}={raw!r} — expected one of {', '.join(s.choices)}")
    return v


_PARSERS = {"bool": _parse_bool, "int": _parse_int, "csv_int": _parse_csv_int,
            "str": _parse_str, "path": _parse_str}


# ── the registry ────────────────────────────────────────────────────────────

SETTINGS: tuple[Setting, ...] = (
    # llm
    Setting("LLM_PROVIDER_ORDER", "llm", "str", "", "Provider failover order, comma-separated."),
    Setting("LLM_MODEL", "llm", "str", "", "Override the model for the active provider."),
    Setting("LLM_URL", "llm", "str", "", "Base URL for a local/self-hosted model."),
    Setting("LLM_API_KEY", "llm", "str", "", "Generic LLM key (fallback).", secret=True),
    Setting("OPENAI_API_KEY", "llm", "str", "", "OpenAI key.", secret=True),
    Setting("GEMINI_API_KEY", "llm", "str", "", "Gemini key.", secret=True),
    Setting("ANTHROPIC_API_KEY", "llm", "str", "", "Anthropic key.", secret=True),
    Setting("CLAUDE_API_KEY", "llm", "str", "", "Alias for ANTHROPIC_API_KEY.", secret=True),

    # follow-up ladders
    Setting("FOLLOWUP_SCHEDULE", "followup", "csv_int", [48, 96, 168],
            "Hours after the previous message that each email follow-up comes due."),
    Setting("LINKEDIN_FOLLOWUP_SCHEDULE", "followup", "csv_int", [120, 288],
            "Same, for LinkedIn. Slower on purpose — nudging a new connection reads badly."),
    Setting("FOLLOWUP_AFTER_DAYS", "followup", "int", None,
            "DEPRECATED — derived from the first entry of FOLLOWUP_SCHEDULE.",
            deprecated_by="FOLLOWUP_SCHEDULE"),

    # outreach
    Setting("OUTREACH_DAILY_LIMIT", "outreach", "int", 20, "Max emails sent per 24h."),
    Setting("OUTREACH_COOLDOWN_DAYS", "outreach", "int", 30,
            "Don't email the same address again within this many days."),
    Setting("OUTREACH_FROM_NAME", "outreach", "str", "", "Display name on outgoing mail."),
    Setting("OUTREACH_FROM_ADDRESS", "outreach", "str", "", "From address (defaults to the connected Gmail)."),
    Setting("OUTREACH_SIGNATURE", "outreach", "str", "", "Signature HTML; falls back to the Gmail one."),
    Setting("OUTREACH_STYLE", "outreach", "str", "", "Extra style guidance for drafts."),
    Setting("OUTREACH_ATTACH_DOCS", "outreach", "bool", True, "Attach résumé + cover letter to the first email."),
    Setting("OUTREACH_ATTACH_DECK", "outreach", "bool", False, "Also attach the intro deck."),
    Setting("INTRO_DECK_PATH", "outreach", "path", "", "Path to the intro deck PDF."),
    Setting("INTRO_DECK_URL", "outreach", "str", "https://www.jorgealejandrodiez.com/intro/",
            "Intro-deck link offered in every outreach email (not LinkedIn notes)."),
    Setting("SCHEDULING_LINK", "outreach", "str", "", "Calendar link for the call-to-action."),
    # Deck-click pull. The click happens on the sender's own site; the dashboard is
    # localhost-only and cannot receive a webhook, so it POLLS. Empty = the feature is off and
    # `deck-hits` stays a manual import.
    Setting("DECK_HITS_URL", "outreach", "str", "",
            "Endpoint that returns recorded intro-deck clicks (see deploy/netlify/)."),
    Setting("DECK_HITS_TOKEN", "outreach", "str", "",
            "Bearer token for DECK_HITS_URL.", secret=True),
    Setting("GMAIL_ADDRESS", "outreach", "str", "", "SMTP fallback address.", secret=False),
    Setting("GMAIL_APP_PASSWORD", "outreach", "str", "", "SMTP app password.", secret=True),

    # networking / contacts
    Setting("APOLLO_API_KEY", "networking", "str", "", "Apollo.io key (paid plan).", secret=True),
    Setting("NETWORKING_LINKEDIN", "networking", "bool", False, "Enable read-only LinkedIn augmentation."),
    Setting("NETWORKING_LINKEDIN_DM", "networking", "bool", False, "Enable LinkedIn DM composing."),
    Setting("NETWORKING_LINKEDIN_DAILY_LIMIT", "networking", "int", 20, "Max LinkedIn actions per day."),
    Setting("LINKEDIN_DM_DAILY_LIMIT", "networking", "int", 15, "Max LinkedIn DMs per day."),
    Setting("LINKEDIN_DM_COOLDOWN_DAYS", "networking", "int", 30, "Don't DM the same person within this many days."),
    Setting("LINKEDIN_DM_SESSION", "networking", "str", "", "LinkedIn session cookie.", secret=True),
    Setting("LINKEDIN_DM_CHROME_EXEC", "networking", "path", "", "Chrome binary for the DM helper."),
    Setting("LINKEDIN_DM_CHROME_PROFILE", "networking", "path", "", "Chrome profile dir for the DM helper."),

    # apply
    Setting("APPLY_STALE_MINUTES", "apply", "int", 30,
            "Release an in-progress apply lock after this long (an interrupted run)."),
    Setting("APPLY_AGENT_TIMEOUT", "apply", "int", 900,
            "Seconds the apply agent gets per application before it is given up on."),
    Setting("TAILOR_AGGRESSIVE", "apply", "bool", False,
            "Mirror the job description closely and skip the fabrication judge."),
    Setting("CHROME_PATH", "apply", "path", "", "Chrome binary for the apply agent."),
    Setting("NODE_BIN", "apply", "path", "", "Node binary for the résumé renderer."),
    Setting("AGENT_BROWSER_BIN", "apply", "path", "", "agent-browser binary."),
    Setting("CAPSOLVER_API_KEY", "apply", "str", "", "CAPTCHA solver key.", secret=True),

    # paths
    Setting("APPLYPILOT_DIR", "paths", "path", "", "Override the data directory (~/.applypilot)."),
)

_BY_NAME = {s.name: s for s in SETTINGS}


# ── resolution ──────────────────────────────────────────────────────────────

def resolve(env: dict | None = None) -> tuple[dict, list[str]]:
    """(values, errors). Every setting gets a value; errors describe what was unusable.

    A setting that fails to parse keeps its DEFAULT in `values`, so a caller that chooses
    to continue is running on documented behaviour rather than on half-parsed input.
    """
    env = os.environ if env is None else env
    values: dict[str, Any] = {}
    errors: list[str] = []

    for s in SETTINGS:
        raw = env.get(s.name)
        # Empty (or whitespace-only) means "use the default". `FOLLOWUP_SCHEDULE=` is how
        # people disable a line in a .env without deleting it; treating it as malformed
        # would fail startup on a perfectly ordinary edit.
        if raw is None or not raw.strip():
            values[s.name] = s.default
            continue
        try:
            values[s.name] = _PARSERS[s.kind](raw, s)
        except ConfigError as exc:
            errors.append(str(exc))
            values[s.name] = s.default

    # One concept, one knob. FOLLOWUP_AFTER_DAYS predates FOLLOWUP_SCHEDULE and says the same
    # thing in different units — the schedule's first entry IS "when the first follow-up is
    # owed". Derived unless explicitly set, and warned about when it is.
    if values.get("FOLLOWUP_AFTER_DAYS") is None:
        schedule = values.get("FOLLOWUP_SCHEDULE") or [48]
        values["FOLLOWUP_AFTER_DAYS"] = max(0, round(schedule[0] / 24))
    return values, errors


def deprecations(env: dict | None = None) -> list[str]:
    env = os.environ if env is None else env
    out = []
    for s in SETTINGS:
        if s.deprecated_by and env.get(s.name):
            out.append(f"{s.name} is deprecated — use {s.deprecated_by} instead "
                       f"(hours, not days: {s.deprecated_by}=48,96,168). "
                       f"It still works for now.")
    return out


def validate(env: dict | None = None) -> list[str]:
    """Every problem at once, so a broken .env is fixed in one pass rather than N restarts."""
    return resolve(env)[1]


def source(name: str, env: dict | None = None) -> str:
    """Where a value came from: shell env, .env file, or the built-in default."""
    env = os.environ if env is None else env
    if name not in env or env[name] == "":
        return "default"
    return "env" if name in _OS_ENV_AT_IMPORT else ".env"


def describe(env: dict | None = None) -> list[dict]:
    """Every setting for `doctor --config`. Secrets are reported set/not set, never printed."""
    env = os.environ if env is None else env
    values, _ = resolve(env)
    rows = []
    for s in SETTINGS:
        raw = env.get(s.name)
        if s.secret:
            shown = "set" if raw else "not set"
        else:
            v = values[s.name]
            shown = ",".join(str(x) for x in v) if isinstance(v, list) else str(v)
            if shown == "":
                shown = "—"
        rows.append({"name": s.name, "group": s.group, "value": shown,
                     "source": source(s.name, env), "help": s.help,
                     "deprecated": bool(s.deprecated_by), "secret": s.secret})
    return rows


def render_env_example() -> str:
    """Generate `.env.example` from this registry so it cannot drift out of date."""
    lines = ["# ApplyPilot configuration — GENERATED from src/applypilot/settings.py.",
             "# Edit that file and run `applypilot doctor --write-env-example`, not this one.",
             "#", "# Every value below is the default; uncomment only what you change.", ""]
    for group in dict.fromkeys(s.group for s in SETTINGS):
        lines.append(f"# ── {group} " + "─" * max(0, 60 - len(group)))
        for s in SETTINGS:
            if s.group != group:
                continue
            default = s.default
            if isinstance(default, list):
                default = ",".join(str(x) for x in default)
            elif isinstance(default, bool):
                default = "1" if default else "0"
            elif default is None:
                default = ""
            tag = "  [DEPRECATED]" if s.deprecated_by else ""
            lines.append(f"# {s.help}{tag}")
            lines.append(f"#{s.name}={default}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
