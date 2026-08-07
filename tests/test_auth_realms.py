"""Sign-in walls: resolving them, remembering them, and not paying for one twice.

Five of the first nineteen applications died at a wall — Arm, Salesforce, Deloitte, Google,
Yahoo — and each one cost a Chrome launch, a full agent run and about a minute to rediscover a
fact that never changes. The finding was recorded on the JOB, where the next job at the same
employer could not read it.

The unit that matters is the ATS TENANT, and these tests are mostly about the ways that unit
can be got wrong: a shared host mistaken for one employer, a relative URL mistaken for a host,
and an inference mistaken for proof.
"""

from __future__ import annotations

import pytest

import applypilot.database as database
from applypilot.apply import accounts as acct
from applypilot.domain import authrealm as ar
from applypilot.repo import accounts as repo


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.close_connection(path)
    database.init_db(path)
    return repo.init_accounts(database.get_connection(path))


# ── resolving the realm ─────────────────────────────────────────────────────

@pytest.mark.parametrize("url,realm_id,kind", [
    ("https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/x",
     "salesforce.wd12.myworkdayjobs.com", ar.ACCOUNT),
    ("https://experienced-arm.icims.com/jobs/12345/login", "experienced-arm.icims.com", ar.ACCOUNT),
    ("https://acme.taleo.net/careersection/x", "acme.taleo.net", ar.ACCOUNT),
    ("https://apply.deloitte.com/en_US/careers/InviteToApply?jobId=1", "apply.deloitte.com", ar.ACCOUNT),
    ("https://www.google.com/about/careers/applications/jobs/results/92-x/",
     "accounts.google.com", ar.SSO),
    ("https://job-boards.greenhouse.io/iterable/jobs/7984113", "job-boards.greenhouse.io", ar.NONE),
    ("https://jobs.ashbyhq.com/fluidstack/b39b", "jobs.ashbyhq.com", ar.NONE),
    ("https://jobs.lever.co/acme/123", "jobs.lever.co", ar.NONE),
])
def test_known_vendors_resolve(url, realm_id, kind):
    realm = ar.resolve(url)
    assert (realm.id, realm.kind) == (realm_id, kind)


@pytest.mark.parametrize("url", [
    "https://careers.clever.com/jobs/1",   # NOT Lever
    "https://jobsight.com/apply/9",        # NOT a board
    "https://carelever.io/x",              # NOT Lever
])
def test_a_lookalike_host_is_not_a_vendor(url):
    """§Lessons 1, four bugs deep: `"lever" in "clever.com"`. Whole host LABELS or nothing.

    An unknown host must stay unknown — claiming it needs no account sends the agent into a
    wall, and claiming it needs one blocks a job that would have gone straight through.
    """
    assert ar.resolve(url).kind == ar.UNKNOWN


def test_a_relative_application_url_has_no_realm():
    """The Google row stores `./apply?jobId=CiUAL2Fck…` as its application_url, straight off the
    page. Prepending a scheme yields `https://./apply`, whose hostname is "." — truthy, so it
    became a realm id and the Accounts panel listed an employer called ".".

    Found by running the seed against the real jobs table. Reading the code did not suggest it.
    """
    for relative in ("./apply?jobId=abc", "/signin/usernamerecovery?continue=x", "?src=linkedin"):
        assert ar.resolve(relative) is None, relative


def test_it_falls_back_to_the_posting_when_the_apply_url_is_relative():
    """The consequence of the above: the Google job must still resolve, via its posting URL."""
    realm = acct.realm_for({"application_url": "./apply?jobId=abc",
                            "url": "https://www.google.com/about/careers/applications/jobs/x"})
    assert realm.id == "accounts.google.com"


def test_the_application_url_wins_when_it_is_absolute():
    """A careers page fronting an ATS applies through the ATS, and that is where the wall is."""
    realm = acct.realm_for({"application_url": "https://experienced-arm.icims.com/jobs/1",
                            "url": "https://careers.arm.com/job/austin/pm/3309"})
    assert realm.id == "experienced-arm.icims.com"


# ── the shared-host trap ────────────────────────────────────────────────────

def test_a_workday_tenant_in_the_path_is_not_identified_by_its_host():
    """`wd1.myworkdaysite.com` is shared by EVERY employer on it. Two different tenants must not
    collapse into one realm, or signing into one marks the other as done."""
    wf = ar.resolve("https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/login")
    other = ar.resolve("https://wd1.myworkdaysite.com/en-US/recruiting/ot/SomeoneElse/job/2")
    assert wf.id != other.id
    assert wf.id == "wd1.myworkdaysite.com/wellsfargojobs"
    assert not wf.host_is_tenant


def test_a_cookie_is_only_evidence_when_the_host_names_one_employer():
    """The reason `host_is_tenant` exists. A cookie on a shared host says somebody visited it,
    which is true of every tenant there at once."""
    shared = ar.resolve("https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/login")
    owned = ar.resolve("https://salesforce.wd12.myworkdayjobs.com/x/job/1")
    assert ar.cookie_hosts_for(shared) == ()
    assert "salesforce.wd12.myworkdayjobs.com" in ar.cookie_hosts_for(owned)


def test_a_saved_login_must_match_the_path_for_a_shared_host():
    """The only signal that tells one myworkdaysite tenant from another."""
    wf = ar.resolve("https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/login")
    assert ar.matches_saved_login(
        wf, "https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/login")
    assert not ar.matches_saved_login(
        wf, "https://wd1.myworkdaysite.com/en-US/recruiting/ot/SomeoneElse/login")


# ── what the store remembers ────────────────────────────────────────────────

def test_a_wall_is_learned_once_and_read_by_every_later_job(db):
    """The whole point. Job A pays for the discovery; job B at the same employer does not."""
    a = {"url": "https://acme.wd5.myworkdayjobs.com/careers/job/1"}
    b = {"url": "https://acme.wd5.myworkdayjobs.com/careers/job/2"}
    assert acct.preflight(a, db)[0], "an unseen realm must be allowed to try"
    acct.note_wall(a, "login", db)
    ok, realm_id, why = acct.preflight(b, db)
    assert not ok and realm_id == "acme.wd5.myworkdayjobs.com"
    assert "account" in why.lower()


@pytest.mark.parametrize("reason,teaches", [
    ("login", True), ("sso_required", True), ("login_issue", True),
    ("field", False), ("captcha", False), ("timeout", False), ("paused", False),
])
def test_only_a_sign_in_reason_teaches_a_wall(db, reason, teaches):
    """A stuck field is not a sign-in wall. Recording one as a wall would block every future
    job at that employer over a form quirk that had nothing to do with accounts.

    Uses a host with no vendor rule, so the ONLY thing that can block it is what this call
    learned — on a Workday URL the rule blocks it regardless and the test measures nothing.
    """
    job = {"url": "https://careers.example-portal.com/apply/1"}
    acct.refresh([job], db)
    assert bool(acct.note_wall(job, reason, db)) is teaches
    assert acct.preflight(job, db)[0] is not teaches
    assert (repo.get("careers.example-portal.com", db)["blocked_count"] or 0) == int(teaches)


def test_having_an_account_unblocks_the_realm(db):
    job = {"url": "https://acme.wd5.myworkdayjobs.com/careers/job/1"}
    acct.note_wall(job, "login", db)
    assert not acct.preflight(job, db)[0]
    repo.set_have_account("acme.wd5.myworkdayjobs.com", True, "operator", db)
    assert acct.preflight(job, db)[0]


def test_an_open_ats_never_blocks(db):
    """Greenhouse, Lever and Ashby are the overwhelming majority of what goes through. A false
    positive here would stop the applications that actually work."""
    for url in ("https://job-boards.greenhouse.io/x/jobs/1", "https://jobs.lever.co/x/2",
                "https://jobs.ashbyhq.com/x/3"):
        assert acct.preflight({"url": url}, db)[0], url


def test_an_unknown_host_never_blocks(db):
    """Deliberately biased: a wasted agent run is recoverable, a job silently never attempted
    is not."""
    assert acct.preflight({"url": "https://careers.arm.com/job/austin/pm/3309"}, db)[0]


def test_weaker_evidence_cannot_overwrite_the_operator(db):
    """The cookie sweep runs every five minutes; the operator clicks once. If the sweep could
    win, the answer would revert the next time a session expired and they would be told to
    re-answer something they had already settled."""
    acct.note_wall({"url": "https://acme.wd5.myworkdayjobs.com/x/job/1"}, "login", db)
    assert repo.set_have_account("acme.wd5.myworkdayjobs.com", True, "operator", db)
    assert not repo.set_have_account("acme.wd5.myworkdayjobs.com", False, "cookie", db)
    assert repo.get("acme.wd5.myworkdayjobs.com", db)["have_account"] == 1


def test_a_later_sighting_does_not_forget_a_learned_wall(db):
    """`resolve()` returns UNKNOWN for hosts it has no rule for, and `refresh()` runs on every
    background poll. If a fresh UNKNOWN overwrote a learned ACCOUNT, the system would forget
    every wall it had ever discovered, roughly every five minutes."""
    job = {"url": "https://careers.example-ats.com/apply/1"}
    acct.refresh([job], db)
    acct.note_wall(job, "login", db)
    acct.refresh([job], db)          # the poll runs again
    assert repo.get("careers.example-ats.com", db)["kind"] == ar.ACCOUNT
    assert not acct.preflight(job, db)[0]


def test_the_signup_url_is_where_we_were_actually_stopped(db):
    """Not a per-vendor guess. The page the agent hit the wall on is the page the human needs,
    by construction, and it cannot go stale in a way a hardcoded registration link can."""
    job = {"url": "https://acme.wd5.myworkdayjobs.com/careers/job/1",
           "application_url": "https://acme.wd5.myworkdayjobs.com/careers/login"}
    acct.note_wall(job, "login", db)
    assert repo.get("acme.wd5.myworkdayjobs.com", db)["signup_url"].endswith("/careers/login")


# ── the panel ───────────────────────────────────────────────────────────────

def test_the_panel_separates_blocked_from_done(db):
    acct.note_wall({"url": "https://a.wd5.myworkdayjobs.com/x/1"}, "login", db)
    acct.note_wall({"url": "https://b.wd5.myworkdayjobs.com/x/1"}, "login", db)
    repo.set_have_account("b.wd5.myworkdayjobs.com", True, "operator", db)
    p = acct.panel(db)
    assert [e["realm"] for e in p["blocking"]] == ["a.wd5.myworkdayjobs.com"]
    assert [e["realm"] for e in p["ready"]] == ["b.wd5.myworkdayjobs.com"]


def test_sync_never_turns_a_cookie_into_an_account(db, monkeypatch):
    """The guarantee, exercised through the function that could break it.

    A first version of this test called `note_session` directly and asserted the row — so
    rewriting `sync_evidence` to call `set_have_account("cookie")` instead passed every test in
    this file. Mutation testing caught that. The check has to run the real path.

    Why it matters: Workday sets a cookie when you merely VIEW a job. If that satisfied
    preflight, the queue would walk into the wall this whole feature exists to avoid, and do it
    silently — a system that had learned to be confidently wrong.
    """
    from applypilot.apply import profile_scan
    acct.note_wall({"url": "https://a.wd5.myworkdayjobs.com/x/1"}, "login", db)
    monkeypatch.setattr(profile_scan, "cookie_hosts",
                        lambda *a, **k: {".a.wd5.myworkdayjobs.com"})
    monkeypatch.setattr(profile_scan, "saved_login_origins", lambda *a, **k: [])

    found = acct.sync_evidence(db)

    assert found["sessions"] == 1 and found["accounts"] == 0
    row = repo.get("a.wd5.myworkdayjobs.com", db)
    assert not row["have_account"], "a page visit was recorded as an account"
    assert row["session_seen"] == 1
    assert not acct.preflight({"url": "https://a.wd5.myworkdayjobs.com/x/1"}, db)[0]


def test_sync_does_accept_a_saved_credential(db, monkeypatch):
    """The other half — Chrome only stores a credential after a real sign-in, so this IS proof.
    Without this, `sync_evidence` returning nothing at all would also pass the test above."""
    from applypilot.apply import profile_scan
    acct.note_wall({"url": "https://a.wd5.myworkdayjobs.com/x/1"}, "login", db)
    monkeypatch.setattr(profile_scan, "cookie_hosts", lambda *a, **k: set())
    monkeypatch.setattr(profile_scan, "saved_login_origins",
                        lambda *a, **k: ["https://a.wd5.myworkdayjobs.com/x/login"])

    assert acct.sync_evidence(db)["accounts"] == 1
    assert repo.get("a.wd5.myworkdayjobs.com", db)["have_account"] == 1
    assert acct.preflight({"url": "https://a.wd5.myworkdayjobs.com/x/1"}, db)[0]


def test_a_session_hint_is_never_an_answer(db):
    """§Lessons 34: an inference handed to a check that treats it as proof. A Workday cookie is
    set by VIEWING a job, so if it satisfied preflight the queue would sail into a wall it had
    been built to avoid — silently, which is the version that costs a day."""
    acct.note_wall({"url": "https://a.wd5.myworkdayjobs.com/x/1"}, "login", db)
    repo.note_session("a.wd5.myworkdayjobs.com", db)
    p = acct.panel(db)
    assert p["blocking"][0]["session_seen"] is True
    assert p["blocking"][0]["have"] is False
    assert not acct.preflight({"url": "https://a.wd5.myworkdayjobs.com/x/1"}, db)[0]


# ── the panel is scoped to the Space on screen ──────────────────────────────
#
# `ats_accounts` has no `space_id` and must not grow one: an account covers an ATS TENANT, so
# one Salesforce Workday login is the same fact on every tab. What is not shared is the
# SENTENCE — "8 employers need an account before their jobs can run" is a claim about jobs, and
# on a Space holding one job it named eight belonging to another.

def _wall(url, db):
    acct.note_wall({"url": url}, "login", db)
    return {"url": url, "application_url": "", "applied_at": "", "rejected_at": ""}


def test_the_panel_names_only_realms_this_space_would_hit(db):
    mine = _wall("https://acme.wd5.myworkdayjobs.com/x/1", db)
    _wall("https://other.wd5.myworkdayjobs.com/x/1", db)     # a job on a different tab
    p = acct.panel(db, jobs=[mine])
    assert [e["realm"] for e in p["blocking"]] == ["acme.wd5.myworkdayjobs.com"], p["blocking"]


def test_no_jobs_named_still_returns_everything(db):
    """The CLI's accounts view, and every caller predating Spaces, has no Space in hand. `None`
    has to keep meaning "all of them" — distinct from an EMPTY list, which means a Space whose
    jobs hit no wall at all."""
    _wall("https://acme.wd5.myworkdayjobs.com/x/1", db)
    _wall("https://other.wd5.myworkdayjobs.com/x/1", db)
    assert len(acct.panel(db)["blocking"]) == 2
    assert acct.panel(db, jobs=[])["blocking"] == [], "an empty Space still shows other tabs"


def test_a_finished_job_stops_blocking(db):
    """Applied and rejected rows cannot run, so a wall they once hit is not standing between
    the operator and anything. Without this the banner keeps naming an employer forever after
    its only job is done."""
    done = _wall("https://acme.wd5.myworkdayjobs.com/x/1", db)
    done["applied_at"] = "2026-08-01T10:00:00+00:00"
    assert acct.panel(db, jobs=[done])["blocking"] == []

    rejected = _wall("https://beta.wd5.myworkdayjobs.com/x/1", db)
    rejected["rejected_at"] = "2026-08-01T10:00:00+00:00"
    assert acct.panel(db, jobs=[rejected])["blocking"] == []


def test_an_account_stays_shared_across_spaces(db):
    """The half that keeps the registry honest. Filtering the PANEL must not turn into
    partitioning the TABLE — signing in once has to cover the same employer on every tab, which
    is the entire reason a realm is per tenant rather than per job."""
    job = _wall("https://acme.wd5.myworkdayjobs.com/x/1", db)
    repo.set_have_account("acme.wd5.myworkdayjobs.com", True, "operator", db)
    row = repo.get("acme.wd5.myworkdayjobs.com", db)
    assert row["have_account"], "the account was not recorded"
    assert acct.panel(db, jobs=[job])["blocking"] == [], "a paid wall still reads as blocking"
    # A job at the same employer in ANOTHER Space inherits it — same realm, same row.
    twin = {"url": "https://acme.wd5.myworkdayjobs.com/x/999", "application_url": "",
            "applied_at": "", "rejected_at": ""}
    assert acct.panel(db, jobs=[twin])["blocking"] == []


def test_the_application_url_decides_the_realm(db):
    """A careers page fronting a different vendor's tenant. Scoping must use the same rule the
    apply itself does, or the panel filters on a realm no apply would ever reach."""
    acct.note_wall({"url": "https://careers.acme.com/x",
                    "application_url": "https://acme.wd5.myworkdayjobs.com/x/1"}, "login", db)
    job = {"url": "https://careers.acme.com/x", "applied_at": "", "rejected_at": "",
           "application_url": "https://acme.wd5.myworkdayjobs.com/x/1"}
    assert [e["realm"] for e in acct.panel(db, jobs=[job])["blocking"]] == \
        ["acme.wd5.myworkdayjobs.com"]
