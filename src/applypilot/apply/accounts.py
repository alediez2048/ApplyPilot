"""Account state for the apply pipeline: what blocks, what is already paid for.

Joins the three pieces — `domain/authrealm` (what realm is this?), `repo/accounts` (what do we
know?) and `apply/profile_scan` (what does the browser already have?) — into the four things
callers actually want:

    sync_evidence()   read the browser, fill in what we already have accounts for
    refresh(jobs)     make sure every job's realm has a row
    preflight(job)    should this apply even launch?
    panel()           what the Accounts UI renders

`preflight` is the one that pays for the rest. Before it, discovering that Salesforce's Workday
wants an account cost a Chrome launch, a Claude Code run, 59 seconds, and a job left in
`needs_human` — repeated for every job at that employer. After it, the same fact costs a
dictionary lookup, and the operator gets one batched list of registrations to do instead of
being ambushed mid-queue.
"""

from __future__ import annotations

import sqlite3

from applypilot.domain import authrealm
from applypilot.repo import accounts as repo


def realm_for(job: dict) -> authrealm.Realm | None:
    """The realm a job would apply through.

    `application_url` first: it is where the apply actually goes, and it is frequently a
    different vendor from the posting (a careers page fronting an iCIMS tenant). Falls back to
    the posting URL, which is all an un-enriched job has.
    """
    return (authrealm.resolve(job.get("application_url"))
            or authrealm.resolve(job.get("url")))


def refresh(jobs: list[dict], conn: sqlite3.Connection | None = None) -> int:
    """Ensure every job's realm has a row. Returns how many realms are known after."""
    seen: set[str] = set()
    for job in jobs:
        realm = realm_for(job)
        if realm is None or realm.id in seen:
            continue
        seen.add(realm.id)
        repo.see(realm, job.get("application_url") or job.get("url") or "", conn)
    return len(seen)


def sync_evidence(conn: sqlite3.Connection | None = None) -> dict:
    """Fill in `have_account` from what the apply browser already holds.

    This is the step that makes the feature start out useful instead of empty: on this machine
    it finds live sessions at Salesforce, Yahoo and Arm — three of the five employers that had
    blocked an application — which the system had never once asked about.

    Cookie evidence is only accepted for realms whose HOST identifies one employer. Every
    tenant on `wd1.myworkdaysite.com` shares that host, so a cookie there would otherwise mark
    all of them as solved off one unrelated visit.
    """
    from applypilot.apply import profile_scan

    hosts = profile_scan.cookie_hosts()
    origins = profile_scan.saved_login_origins()
    found = {"accounts": 0, "sessions": 0}

    for row in repo.all_realms(conn):
        realm = authrealm.Realm(id=row["realm_id"], kind=row["kind"],
                                vendor=row["vendor"] or "", label=row["label"] or "",
                                host_is_tenant=bool(row["host_is_tenant"]))
        # A SAVED CREDENTIAL is proof: Chrome only stores one after a real sign-in.
        if any(authrealm.matches_saved_login(realm, o) for o in origins):
            if repo.set_have_account(realm.id, True, "saved-login", conn):
                found["accounts"] += 1
        # A COOKIE is not. Workday sets one on an anonymous job view, so treating it as an
        # account would skip the wall check for an employer you have never registered with —
        # and the failure would be silent, which is the version that costs a day.
        cookie_keys = authrealm.cookie_hosts_for(realm)
        if cookie_keys and any(k in hosts for k in cookie_keys):
            repo.note_session(realm.id, conn)
            found["sessions"] += 1
    return found


def preflight(job: dict, conn: sqlite3.Connection | None = None) -> tuple[bool, str, str]:
    """May this apply launch? Returns (ok, realm_id, reason-if-not).

    Only ever blocks on a realm we KNOW walls you and KNOW you have no account for. An unknown
    host always goes: a wrong "needs an account" would stop a job that would have sailed
    through, which is a worse failure than the wasted run this exists to prevent.
    """
    realm = realm_for(job)
    if realm is None:
        return True, "", ""

    row = repo.get(realm.id, conn)
    # What was LEARNED outranks what the rules can infer. `resolve()` says UNKNOWN for any host
    # it has no rule for, which is most employer-run portals — so reading the resolved kind and
    # returning early meant a wall could be discovered, stored correctly, and then never
    # consulted again. The row is the memory; this function's whole job is to read it.
    kind = row["kind"] if row and row["kind"] in (authrealm.ACCOUNT, authrealm.SSO) else realm.kind
    if kind not in (authrealm.ACCOUNT, authrealm.SSO):
        return True, realm.id, ""

    if row is None:
        # Never seen. Record it and let the apply find out — that IS how a realm gets learned.
        repo.see(realm, job.get("application_url") or job.get("url") or "", conn)
        return True, realm.id, ""
    if row["have_account"]:
        return True, realm.id, ""
    if row["kind"] == authrealm.SSO:
        return False, realm.id, (
            f"{row['label']} sign-in is required and no session is saved. Sign in once in the "
            f"apply browser (Accounts panel) — it is reused for every job there.")
    return False, realm.id, (
        f"{row['label']} requires an account before you can apply, and there is no sign-in "
        f"saved. Create it once in the Accounts panel; every future job there skips this.")


def note_wall(job: dict, reason: str, conn: sqlite3.Connection | None = None) -> str:
    """An apply stopped at a wall. Teach the realm, so the next job there never repeats it.

    Called with the agent's own reason, so a `needs_human:field` does not get mistaken for a
    login wall — only login/sso do.
    """
    if (reason or "").lower() not in ("login", "sso_required", "login_issue", "account"):
        return ""
    realm = realm_for(job)
    if realm is None:
        return ""
    repo.learn_from_wall(realm, job.get("application_url") or job.get("url") or "", conn)
    return realm.id


def panel(conn: sqlite3.Connection | None = None) -> dict:
    """What the Accounts UI renders: what is blocking, and what is already handled."""
    rows = repo.all_realms(conn)
    blocking, ready = [], []
    for row in rows:
        entry = {
            "realm": row["realm_id"],
            "label": row["label"] or row["realm_id"],
            "vendor": row["vendor"] or "",
            "kind": row["kind"],
            "have": bool(row["have_account"]),
            "evidence": row["evidence"] or "",
            "session_seen": bool(row["session_seen"]),
            "signup_url": row["signup_url"] or "",
            "blocked": row["blocked_count"] or 0,
            "note": row["note"] or "",
        }
        if row["kind"] in (authrealm.ACCOUNT, authrealm.SSO) and not entry["have"]:
            blocking.append(entry)
        elif row["kind"] in (authrealm.ACCOUNT, authrealm.SSO):
            ready.append(entry)
    return {"blocking": blocking, "ready": ready,
            "open_count": len([r for r in rows if r["kind"] == authrealm.NONE])}
