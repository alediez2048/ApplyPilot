"""The apply browser's profile: read it for evidence, keep credentials out of it.

`chrome.py:_ensure_worker_profile` seeds a worker by copying the operator's real Chrome profile.
That is how sessions persist, and the persistence is genuinely worth having — it is what makes a
sign-in wall a once-per-employer cost rather than once-per-job.

It also copied, on the machine this was found on, **682 saved passwords, 2 credit cards and 831
autofill entries**, into the browser the apply agent drives with `bypassPermissions` across
careers pages whose content is written by whoever posted the job. The agent is denied every file
tool so it cannot read the password store directly, but it is steering the browser those
credentials live in.

Cookies are a separate file. Removing the credential stores costs nothing that was ever wanted.
"""

from __future__ import annotations

import sqlite3

import pytest

from applypilot.apply import profile_scan


def _chrome_profile(root, *, passwords=(), cookies=(), cards=0):
    """A minimal stand-in for a Chrome user-data-dir, with the tables actually queried."""
    default = root / "Default"
    default.mkdir(parents=True)

    logins = sqlite3.connect(default / "Login Data")
    logins.execute("CREATE TABLE logins (origin_url TEXT, username_value TEXT, "
                   "password_value BLOB)")
    for origin in passwords:
        logins.execute("INSERT INTO logins VALUES (?,?,?)", (origin, "me@example.com", b"secret"))
    logins.commit()
    logins.close()

    jar = sqlite3.connect(default / "Cookies")
    jar.execute("CREATE TABLE cookies (host_key TEXT, name TEXT)")
    for host in cookies:
        jar.execute("INSERT INTO cookies VALUES (?, 'sid')", (host,))
    jar.commit()
    jar.close()

    web = sqlite3.connect(default / "Web Data")
    web.execute("CREATE TABLE credit_cards (name_on_card TEXT)")
    web.execute("CREATE TABLE autofill (name TEXT)")
    for i in range(cards):
        web.execute("INSERT INTO credit_cards VALUES (?)", (f"card{i}",))
    web.execute("INSERT INTO autofill VALUES ('email')")
    web.commit()
    web.close()
    return root


@pytest.fixture()
def profile(tmp_path):
    return _chrome_profile(
        tmp_path / "worker-0",
        passwords=["https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/login",
                   "https://secure.bank.example/login"],
        cookies=[".salesforce.wd12.myworkdayjobs.com", ".greenhouse.io"],
        cards=2)


# ── reading ─────────────────────────────────────────────────────────────────

def test_it_reads_origins_and_hosts(profile):
    assert ".salesforce.wd12.myworkdayjobs.com" in profile_scan.cookie_hosts(profile)
    assert any("WellsFargoJobs" in o for o in profile_scan.saved_login_origins(profile))


def test_no_password_column_is_ever_named(profile):
    """The queries select `origin_url` and nothing else. `password_value` is an encrypted blob
    and this module has no business decrypting it, so the column name does not appear in the
    source at all — which is a property a reader can check, unlike an intention.
    """
    import pathlib
    src = pathlib.Path(profile_scan.__file__).read_text(encoding="utf-8")
    queries = [ln for ln in src.splitlines() if "SELECT" in ln.upper()]
    assert queries, "no queries found; this test is measuring nothing"
    for line in queries:
        for forbidden in ("password_value", "username_value", "SELECT *"):
            assert forbidden not in line, f"{forbidden} is read from the credential store: {line}"


def test_a_locked_or_missing_profile_yields_no_evidence_rather_than_an_error(tmp_path):
    """Chrome holds a write lock while it runs, and this is called from the dashboard while an
    apply may be live. A profile that cannot be read is not an error — it is silence."""
    assert profile_scan.cookie_hosts(tmp_path / "nope") == set()
    assert profile_scan.saved_login_origins(tmp_path / "nope") == []


def test_reading_does_not_disturb_the_original(profile):
    """It queries a COPY. Opening Chrome's live database read-write from another process is how
    you corrupt a profile mid-apply."""
    before = (profile / "Default" / "Cookies").read_bytes()
    profile_scan.cookie_hosts(profile)
    profile_scan.saved_login_origins(profile)
    assert (profile / "Default" / "Cookies").read_bytes() == before


# ── purging ─────────────────────────────────────────────────────────────────

def test_purge_removes_credentials_and_keeps_cookies(profile):
    exposure = profile_scan.credential_exposure(profile)
    assert exposure["passwords"] == 2 and exposure["credit_cards"] == 2

    profile_scan.purge_credentials(profile)

    assert not (profile / "Default" / "Login Data").exists()
    assert not (profile / "Default" / "Web Data").exists()
    assert (profile / "Default" / "Cookies").exists(), (
        "cookies were removed — every session is gone and every sign-in wall has to be paid "
        "again, which is the one thing the profile copy exists to prevent")
    assert profile_scan.cookie_hosts(profile), "the sessions did not survive"
    assert profile_scan.credential_exposure(profile)["passwords"] == 0


def test_purge_is_idempotent(profile):
    """§Lessons 22: idempotence is tested by running it twice, not by reasoning about it."""
    first = profile_scan.purge_credentials(profile)
    second = profile_scan.purge_credentials(profile)
    assert first["count"] > 0
    assert second["count"] == 0


def test_cookies_are_not_on_the_credential_list():
    """The one entry whose presence would quietly undo the feature."""
    assert "Cookies" not in profile_scan.CREDENTIAL_FILES
    assert "Login Data" in profile_scan.CREDENTIAL_FILES
    assert "Web Data" in profile_scan.CREDENTIAL_FILES


# ── and never copied in again ───────────────────────────────────────────────

def test_a_fresh_worker_profile_inherits_no_credentials(tmp_path, monkeypatch):
    """Purging once is not a fix: `setup_worker_profile` re-copies the real profile whenever a
    worker directory is missing, so the passwords would simply come back on the next fresh
    worker. The exclusion has to be in the copy.
    """
    from applypilot import config
    from applypilot.apply import chrome

    source = _chrome_profile(tmp_path / "real", passwords=["https://bank.example/login"],
                             cookies=[".greenhouse.io"], cards=1)
    workers = tmp_path / "workers"
    workers.mkdir()
    monkeypatch.setattr(config, "CHROME_WORKER_DIR", workers)
    monkeypatch.setattr(config, "get_chrome_user_data", lambda: source)

    made = chrome.setup_worker_profile(0)

    assert (made / "Default" / "Cookies").exists(), "sessions must still be inherited"
    for name in ("Login Data", "Web Data"):
        assert not (made / "Default" / name).exists(), (
            f"{name} was copied into the apply profile again")


def test_the_exclusion_is_applied_to_the_default_subdirectory(tmp_path, monkeypatch):
    """The trap this nearly fell into. The copy loop walks the user-data-dir's TOP level, where
    the only entry that matters is `Default/` — and that is copied whole by `copytree`. Adding
    "Login Data" to the top-level skip set excludes nothing, because it never appears there.
    """
    import pathlib

    from applypilot.apply import chrome
    src = pathlib.Path(chrome.__file__).read_text(encoding="utf-8")
    block = src[src.index("def setup_worker_profile"):]
    block = block[:block.index("def _suppress_restore_nag")]
    ignore = block[block.index("ignore=shutil.ignore_patterns"):]
    assert "credentials" in ignore, (
        "the credential names are not passed to copytree's ignore, so Default/Login Data is "
        "still copied wholesale")
