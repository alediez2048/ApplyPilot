"""Business rules, with no idea that a web server or a database exists.

Everything here is a pure function over plain dicts. No `sqlite3`, no `http`, no imports
from `web_dashboard`. The web layer loads rows and calls these; it does not compute.

That boundary is the point of ARCH-1. Before it, the two functions that decide what work
exists (`_job_checklist`, `_followup_panel`) lived in the HTTP server module beside CSS, and
the eval harness had to import a web server to test scheduling rules.
"""

from applypilot.domain.checklist import job_checklist
from applypilot.domain.followup import (
    EMAIL,
    LINKEDIN,
    channel_schedule,
    followup_panel,
)
from applypilot.domain.timeutil import parse_ts
from applypilot.domain.verification import (
    OK,
    REJECT,
    UNVERIFIED,
    email_domain_agrees,
    org_name_agrees,
    verify_contact,
)

__all__ = [
    "job_checklist",
    "followup_panel", "channel_schedule", "EMAIL", "LINKEDIN",
    "parse_ts",
    "verify_contact", "email_domain_agrees", "org_name_agrees",
    "OK", "UNVERIFIED", "REJECT",
]
