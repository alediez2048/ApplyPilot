"""Deprecated shim — contact verification moved to `applypilot.domain.verification`.

It is a business rule, not a networking concern: it takes dicts and returns a verdict, with
no provider, no HTTP, and no database. Kept so existing imports keep working.
"""

from applypilot.domain.verification import (  # noqa: F401
    OK,
    REJECT,
    UNVERIFIED,
    email_domain_agrees,
    org_name_agrees,
    verify_contact,
)
