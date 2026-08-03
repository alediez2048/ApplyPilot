"""Read the apply browser's profile for evidence, and take the credentials out of it.

`chrome.py:_ensure_worker_profile` seeds a worker by copying the operator's real Chrome
profile, which is how sessions persist across applications (831 cookie hosts, and that property
is genuinely valuable — it is what makes a sign-in wall a once-per-employer cost). It also
copied, on this machine, **682 saved passwords, 2 credit cards and 831 autofill entries**,
including a bank, an Apple ID and a corporate Okta.

The apply agent drives that browser with `bypassPermissions` on careers pages whose content is
controlled by whoever wrote the posting. It is denied every file tool, so it cannot read the
password store directly — but it is steering the browser those credentials live in, and that is
a much shorter path than it needs to be.

The two halves of this module follow from that:

* **Evidence.** Cookies and saved-login *origins* say which ATS tenants already have a session
  or an account. That is exactly the question the Accounts panel exists to answer, and the
  answer was already sitting on disk, unread, while the system re-discovered each wall the
  expensive way.
* **Purge.** Removing the credential stores costs nothing that matters. Cookies are a separate
  file, so every session survives and no wall gets paid twice.

**No password value is ever read here.** The queries select `origin_url` and nothing else —
`password_value` is an encrypted blob and this module has no business decrypting it, so the
column is not named anywhere in this file. Usernames are skipped for the same reason: the
question is "is there an account here", which the origin alone answers.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

from applypilot import config

#: Everything Chrome keeps credentials, cards or autofill in. Deleted from a worker profile and
#: never copied into a new one. Chrome recreates each of these empty on next launch.
#:
#: `Cookies` is deliberately NOT here. It is what makes an account worth having.
CREDENTIAL_FILES = (
    "Login Data",
    "Login Data For Account",
    "Web Data",
    "Account Web Data",
    "Affiliation Database",
)
CREDENTIAL_DIRS = ("AutofillStrikeDatabase", "AutofillAiModelCache")


def worker_profiles() -> list[Path]:
    """Every worker user-data-dir that has been initialised."""
    root = config.CHROME_WORKER_DIR
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if (p / "Default").is_dir())


def _query(db_file: Path, sql: str) -> list[tuple]:
    """Run one read against a copy of a Chrome database.

    Always against a copy: Chrome holds a write lock while it is running, and this is called
    from the dashboard while an apply may be live.
    """
    if not db_file.exists():
        return []
    tmp = Path(tempfile.mkdtemp()) / db_file.name
    try:
        shutil.copy2(db_file, tmp)
        with sqlite3.connect(f"file:{tmp}?mode=ro", uri=True) as chrome_db:
            return chrome_db.execute(sql).fetchall()
    except Exception:  # noqa: BLE001 — a profile we cannot read is not an error, just no evidence
        return []
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def cookie_hosts(profile: Path | None = None) -> set[str]:
    """Hosts with at least one cookie in the apply profile."""
    hosts: set[str] = set()
    for prof in ([profile] if profile else worker_profiles()):
        for (host,) in _query(prof / "Default" / "Cookies",
                              "SELECT DISTINCT host_key FROM cookies"):
            if host:
                hosts.add(host.lower())
    return hosts


def saved_login_origins(profile: Path | None = None) -> list[str]:
    """Origin URLs of saved credentials. Origins only — no username, no password."""
    out: list[str] = []
    for prof in ([profile] if profile else worker_profiles()):
        for (origin,) in _query(prof / "Default" / "Login Data",
                                "SELECT origin_url FROM logins"):
            if origin:
                out.append(origin)
    return out


def credential_exposure(profile: Path | None = None) -> dict:
    """How much is sitting in the apply profile right now. For reporting, never for display
    of the values themselves."""
    totals = {"passwords": 0, "credit_cards": 0, "autofill": 0, "profiles": 0}
    for prof in ([profile] if profile else worker_profiles()):
        totals["profiles"] += 1
        default = prof / "Default"
        rows = _query(default / "Login Data", "SELECT COUNT(*) FROM logins")
        totals["passwords"] += rows[0][0] if rows else 0
        for table, key in (("credit_cards", "credit_cards"), ("autofill", "autofill")):
            rows = _query(default / "Web Data", f"SELECT COUNT(*) FROM {table}")
            totals[key] += rows[0][0] if rows else 0
    return totals


def purge_credentials(profile: Path | None = None) -> dict:
    """Delete the credential stores from the apply profile(s). Cookies are untouched.

    Idempotent, and safe to run while Chrome is closed. Running it while Chrome is LIVE is
    pointless rather than harmful — Chrome holds the files open and rewrites them on exit — so
    the caller checks first.
    """
    removed: list[str] = []
    for prof in ([profile] if profile else worker_profiles()):
        default = prof / "Default"
        for name in CREDENTIAL_FILES:
            for path in (default / name, default / f"{name}-journal"):
                if path.exists():
                    try:
                        path.unlink()
                        removed.append(f"{prof.name}/{path.name}")
                    except OSError:
                        pass
        for name in CREDENTIAL_DIRS:
            path = default / name
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                removed.append(f"{prof.name}/{name}/")
    return {"removed": removed, "count": len(removed)}
