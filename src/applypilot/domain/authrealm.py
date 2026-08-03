"""What one account covers.

Five of the first nineteen applications died at a sign-in wall — Arm, Salesforce, Deloitte,
Google, Yahoo — and every one of them was handled as a per-JOB failure. It is not. An account
is per ATS **tenant**: one Salesforce Workday account covers every Salesforce job forever, and
Greenhouse, Lever and Ashby need no account at all. So the population of walls across a whole
job search is roughly ten realms, each paid once, by a human, in about ninety seconds.

That reframing is the entire point of this module. Knowing the realm BEFORE an apply runs turns
a 59-second agent run that ends in `needs_human:login` into a decision made for free, and turns
eight interruptions spread over a week into one sitting.

Pure: URL in, `Realm` out. No database, no network, no Chrome.

**`host_is_tenant` is the load-bearing field.** Workday has two URL shapes and only one of them
puts the tenant in the host:

    salesforce.wd12.myworkdayjobs.com/External_Career_Site   → tenant IS the host
    wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs → tenant is a PATH segment

A cookie for `wd1.myworkdaysite.com` therefore proves nothing about Wells Fargo specifically —
that host is shared by every tenant on it. Treating a cookie there as evidence would mark every
myworkdaysite employer as "we have an account" off one unrelated session, which is the same
class of mistake as §Lessons 1: matching a container and calling it the contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

#: An account is needed and can be created by a human on the employer's / ATS's own site.
ACCOUNT = "account"
#: A third-party identity provider (Google, Microsoft). Never automatable, and the operator
#: almost certainly already HAS the account — so the wall is a SESSION problem, not a signup.
SSO = "sso"
#: Public application, no account required. Greenhouse, Lever, Ashby.
NONE = "none"
#: We have never seen this host. Not a guess in either direction — the first apply finds out.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Realm:
    """The identity of one account.

    `id` is what gets stored and compared, so it must name the tenant and nothing wider.
    """

    id: str
    kind: str
    vendor: str
    label: str
    #: True when `id` is a host that belongs to exactly one employer, so a cookie on that host
    #: is real evidence of a session. False for shared hosts, where it is evidence of nothing.
    host_is_tenant: bool = True

    @property
    def needs_account(self) -> bool:
        return self.kind in (ACCOUNT, SSO)


# ── vendors ─────────────────────────────────────────────────────────────────
# Matched on whole host LABELS, never substrings (§Lessons 1: "lever" in "clever.com").

#: `<tenant>.wd<N>.myworkdayjobs.com` — the tenant is the first label.
_WORKDAY_HOST_SUFFIXES = ("myworkdayjobs.com", "myworkdayrecruiting.com")
#: `wd<N>.myworkdaysite.com/<locale>/recruiting/<short>/<Tenant>` — tenant is in the path.
_WORKDAY_PATH_HOSTS = ("myworkdaysite.com",)

#: Vendors whose first host label is the tenant, and which require an account to apply.
_TENANT_ACCOUNT_HOSTS = {
    "icims.com": "iCIMS",
    "taleo.net": "Taleo",
    "successfactors.com": "SuccessFactors",
    "successfactors.eu": "SuccessFactors",
    "brassring.com": "Brassring",
    "avature.net": "Avature",
    "eightfold.ai": "Eightfold",
}

#: Public apply. No account, no wall — the overwhelming majority of what actually goes through.
_OPEN_HOSTS = {
    "greenhouse.io": "Greenhouse",
    "lever.co": "Lever",
    "ashbyhq.com": "Ashby",
    "workable.com": "Workable",
    "breezy.hr": "Breezy",
    "rippling.com": "Rippling",
    "paylocity.com": "Paylocity",
}

#: Identity providers. `id` is the provider, because one Google account is one Google account
#: whichever careers site sends you to it.
_SSO_HOSTS = {
    "google.com": ("accounts.google.com", "Google account"),
    "accounts.google.com": ("accounts.google.com", "Google account"),
    "microsoft.com": ("login.microsoftonline.com", "Microsoft account"),
    "microsoftonline.com": ("login.microsoftonline.com", "Microsoft account"),
    "okta.com": ("okta.com", "Okta"),
}

#: Employer-run application portals that are known to gate on their own account.
_OWN_PORTAL_HOSTS = {
    "apply.deloitte.com": "Deloitte",
}


def _host(url: str | None) -> str:
    """The registrable host, or "" when the string names no host at all.

    `application_url` is frequently RELATIVE — the Google row stores
    `./apply?jobId=CiUAL2Fck…`, straight off the page. Prepending a scheme to that yields
    `https://./apply`, whose hostname is `"."`, which is truthy, so it sailed through as a realm
    id and appeared in the Accounts panel as an employer called ".". Found by running the seed
    against the real job table; nothing about reading the code suggested it.
    """
    if not url:
        return ""
    raw = url.strip()
    if raw.startswith(("/", ".", "?", "#")):
        return ""  # relative: the caller falls back to the posting URL
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""
    # A real host has a dot and starts with a label, which rules out "." and bare "localhost".
    return host if "." in host and host[0].isalnum() else ""


def _suffix_match(host: str, suffix: str) -> bool:
    """True when `suffix` is `host` or a whole-label suffix of it.

    `_suffix_match("clever.com", "lever.co")` is False, which a substring test gets wrong.
    """
    return host == suffix or host.endswith("." + suffix)


def _tenant_label(host: str, suffix: str) -> str:
    """The first label, i.e. the part that names the employer."""
    head = host[: -len(suffix)].rstrip(".")
    return head.split(".")[0] if head else ""


def _titlecase(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-") if w) or slug


def resolve(url: str | None) -> Realm | None:
    """The realm an application URL belongs to, or None if the URL is unusable.

    Deliberately returns UNKNOWN rather than guessing for hosts we have no rule for. A wrong
    "no account needed" sends the agent into a wall it could have avoided; a wrong "account
    needed" blocks a job that would have gone straight through. Neither is worth a guess, and
    the first apply against a new host settles it for every later one (`learn_from_wall`).
    """
    host = _host(url)
    if not host:
        return None

    # ── Workday, tenant in the host ────────────────────────────────────────
    for suffix in _WORKDAY_HOST_SUFFIXES:
        if _suffix_match(host, suffix):
            tenant = _tenant_label(host, suffix)
            if not tenant:
                break
            return Realm(id=host, kind=ACCOUNT, vendor="Workday",
                         label=f"{_titlecase(tenant)} (Workday)", host_is_tenant=True)

    # ── Workday, tenant in the path ────────────────────────────────────────
    for suffix in _WORKDAY_PATH_HOSTS:
        if _suffix_match(host, suffix):
            tenant = _workday_site_tenant(url)
            if not tenant:
                # The host alone is shared by every tenant on it, so it cannot identify an
                # account. Say so rather than inventing one.
                return Realm(id=host, kind=ACCOUNT, vendor="Workday",
                             label="Workday", host_is_tenant=False)
            return Realm(id=f"{host}/{tenant}", kind=ACCOUNT, vendor="Workday",
                         label=f"{_titlecase(tenant)} (Workday)", host_is_tenant=False)

    # ── other tenant-per-subdomain ATSes ───────────────────────────────────
    for suffix, vendor in _TENANT_ACCOUNT_HOSTS.items():
        if _suffix_match(host, suffix):
            tenant = _tenant_label(host, suffix)
            if not tenant:
                return Realm(id=host, kind=ACCOUNT, vendor=vendor, label=vendor,
                             host_is_tenant=False)
            return Realm(id=host, kind=ACCOUNT, vendor=vendor,
                         label=f"{_titlecase(tenant)} ({vendor})", host_is_tenant=True)

    # ── employer portals with their own account ────────────────────────────
    for portal, name in _OWN_PORTAL_HOSTS.items():
        if _suffix_match(host, portal):
            return Realm(id=host, kind=ACCOUNT, vendor=name, label=name, host_is_tenant=True)

    # ── identity providers ─────────────────────────────────────────────────
    for suffix, (realm_id, name) in _SSO_HOSTS.items():
        if _suffix_match(host, suffix):
            return Realm(id=realm_id, kind=SSO, vendor=name, label=name, host_is_tenant=True)

    # ── public apply ───────────────────────────────────────────────────────
    for suffix, vendor in _OPEN_HOSTS.items():
        if _suffix_match(host, suffix):
            return Realm(id=host, kind=NONE, vendor=vendor, label=vendor, host_is_tenant=False)

    return Realm(id=host, kind=UNKNOWN, vendor="", label=host, host_is_tenant=True)


def _workday_site_tenant(url: str | None) -> str:
    """The tenant segment of a `myworkdaysite.com` path.

    `wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/login` → `wellsfargojobs`.
    The shape is `/<locale>/recruiting/<short>/<Tenant>/...`, so the tenant is the segment
    after "recruiting" plus one. Lower-cased, because the same tenant appears in both cases.
    """
    try:
        parts = [p for p in urlparse(url or "").path.split("/") if p]
    except ValueError:
        return ""
    for i, part in enumerate(parts):
        if part.lower() == "recruiting" and len(parts) > i + 2:
            return parts[i + 2].lower()
    return ""


def cookie_hosts_for(realm: Realm) -> tuple[str, ...]:
    """Cookie hosts whose presence is real evidence of a session in this realm.

    Empty when the realm's host is shared. `wd1.myworkdaysite.com` carries cookies for every
    employer on it, so finding one says only that SOMEBODY was visited there.
    """
    if not realm.host_is_tenant:
        return ()
    return (realm.id, "." + realm.id)


def matches_saved_login(realm: Realm, origin_url: str) -> bool:
    """True when a saved-credential origin belongs to this realm.

    Path-scoped realms need the path checked too — that is the only signal that distinguishes
    one myworkdaysite tenant from another, since they all share a host.
    """
    if "/" in realm.id:
        host, _, tenant = realm.id.partition("/")
        return _host(origin_url) == host and f"/{tenant}/" in (
            urlparse(origin_url).path.lower() + "/")
    return _host(origin_url) == realm.id
