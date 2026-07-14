"""Prompt + tool scoping for the LinkedIn people-search agent.

Read-only is ENFORCED at the tool layer (see READONLY_TOOLS), not just requested in
the prompt — the agent is launched with an allowlist that excludes every click/type/
form tool, so it physically cannot connect/message/apply.
"""

from __future__ import annotations

# Playwright-MCP read-only allowlist. Only navigation + observation tools.
# Anything that mutates the page (click/type/fill/select/upload/dialog/evaluate) is
# intentionally excluded so the agent cannot send connections/InMail or click Apply.
READONLY_TOOLS = [
    "mcp__playwright__browser_navigate",
    "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_take_screenshot",
    "mcp__playwright__browser_wait_for",
    "mcp__playwright__browser_console_messages",
    "mcp__playwright__browser_network_requests",
]


def build_linkedin_prompt(company: str, role: str | None, n: int = 5) -> str:
    """Instruction for a READ-ONLY LinkedIn People search."""
    role_line = f'people whose title relates to "{role}"' if role else "people who work there"
    return f"""You are researching contacts on LinkedIn. You are LOGGED IN as the user.
You have ONLY read-only browser tools — you cannot click, type, or message anyone, and
you must not attempt to. Do NOT send connection requests, messages, or InMail.

TASK: Find up to {n} {role_line} at "{company}".

STEPS:
1. Navigate to https://www.linkedin.com/search/results/people/ and take a snapshot.
   If you see a login wall / "sign in" page, STOP and return an empty list.
2. Use the People search for company "{company}"{f' with keywords from the role "{role}"' if role else ''}.
   You can navigate directly to a search URL with the company and keywords as query params.
3. Read the FIRST page of results only. Do not paginate. Do not open profiles.
4. For up to {n} results, capture: full name, current title, and profile URL.

Return ONLY a JSON array, nothing else:
[{{"name": "Jane Smith", "title": "Staff Engineer", "profile_url": "https://www.linkedin.com/in/..."}}]

If you hit a login wall, CAPTCHA, or find nothing, return [].
"""
