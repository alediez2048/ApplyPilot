"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db
    from applypilot.settings import ConfigError

    try:
        load_env()
    except ConfigError as exc:
        # A stack trace for a typo in a config file is noise. Say what is wrong and stop.
        console.print(f"[red]Configuration error[/red]\n\n{exc}")
        raise typer.Exit(code=2) from None
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("sonnet", "--model", "-m", help="Claude model for the browser apply agent (default sonnet — form-filling is the hardest, highest-stakes task; pass 'haiku' for a cheap test run)."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    copilot: bool = typer.Option(False, "--copilot", help="Co-pilot mode: fill the whole application, then STOP and leave the browser open for you to review and submit yourself (never auto-submits)."),
    resume: bool = typer.Option(False, "--resume", help="Resume a co-pilot job that paused on a blocker: reconnect to the still-open browser and continue from the current state (use with --url --copilot)."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        from applypilot.apply.launcher import _ALLOWED_TOOLS, _DISALLOWED_TOOLS
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print("\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions "
            f"--allowedTools '{_ALLOWED_TOOLS}' "
            f"--disallowedTools '{_DISALLOWED_TOOLS}' < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        copilot=copilot,
        resume=resume,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def network(
    url: Optional[str] = typer.Option(None, "--url", help="Find contacts for a specific job URL."),
    per_job: int = typer.Option(5, "--per-job", help="How many contacts to find per job."),
    limit: int = typer.Option(10, "--limit", "-l", help="Max jobs to process (no --url)."),
    no_linkedin: bool = typer.Option(False, "--no-linkedin", help="Apollo only (skip LinkedIn fallback)."),
    linkedin_login: bool = typer.Option(False, "--linkedin-login", help="One-time: open Chrome to log into LinkedIn (for the fallback)."),
    gmail_connect: bool = typer.Option(False, "--gmail-connect", help="One-time: connect Gmail via OAuth for sending outreach."),
    with_content: bool = typer.Option(False, "--with-content", help="With --gmail-connect: also grant gmail.readonly so ApplyPilot can read what replies SAY (CRM-4b). Off by default."),
    fix_threads: bool = typer.Option(False, "--fix-threads", help="Recover Gmail thread ids so follow-ups reply in the original conversation."),
    import_connections: Optional[str] = typer.Option(None, "--import-connections", help="Import your LinkedIn Connections.csv (to flag existing connections)."),
    dm_login: bool = typer.Option(False, "--dm-login", help="One-time: open a browser to log into LinkedIn for the DM sender (agent-browser)."),
    dm_list: bool = typer.Option(False, "--dm-list", help="List contacts with a drafted LinkedIn note + profile URL (DM-eligible)."),
    compose_dm: bool = typer.Option(False, "--compose-dm", help="Compose the LinkedIn note into the invite dialog and leave it open for YOU to click Send."),
    send_dm: bool = typer.Option(False, "--send-dm", help="Alias of --compose-dm (auto-send is disabled — LinkedIn soft-blocks it)."),
    dm_contact: Optional[str] = typer.Option(None, "--dm-contact", help="Contact id for --compose-dm (see --dm-list)."),
    draft: bool = typer.Option(True, "--draft/--no-draft", help="Draft outreach emails for found contacts."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Search + rank only (contacts); with --send-dm, compose but do NOT click Send."),
) -> None:
    """Find people at target companies (Apollo), store contacts, draft outreach."""
    _bootstrap()

    # One-time LinkedIn login for the opt-in fallback (needs consent first).
    if linkedin_login:
        from applypilot.networking import linkedin_agent
        if not linkedin_agent.has_consent():
            console.print("\n[yellow]LinkedIn fallback — please read:[/yellow]\n")
            console.print(linkedin_agent.CONSENT_TEXT)
            if not typer.confirm("Acknowledge the risk and enable the LinkedIn fallback?", default=False):
                console.print("[dim]Cancelled — LinkedIn fallback not enabled.[/dim]")
                raise typer.Exit()
            linkedin_agent.record_consent()
        console.print("[cyan]Opening Chrome — log into LinkedIn, then close the window.[/cyan]")
        linkedin_agent.open_login_browser()
        console.print("[green]Done. Set NETWORKING_LINKEDIN=1 to enable the fallback.[/green]")
        return

    # Import your LinkedIn connections export (to flag warm contacts).
    if import_connections:
        from pathlib import Path as _P
        from applypilot.networking import connections
        path = _P(import_connections).expanduser()
        if not path.exists():
            console.print(f"[red]File not found:[/red] {path}")
            raise typer.Exit(code=1)
        n = connections.import_csv(str(path))
        console.print(f"[green]Imported {n} LinkedIn connection(s).[/green] "
                      "Found contacts who are connections will be flagged.")
        return

    # One-time Gmail OAuth connect for outreach sending.
    if gmail_connect:
        from applypilot.networking import gmail_oauth
        console.print("[cyan]Connecting Gmail (opens a browser)…[/cyan]")
        console.print("  Requesting: send · read (thread follow-ups) · settings (your signature)")
        if with_content:
            # Spelled out before the browser opens, not after. This is the one scope that can
            # read every message in the mailbox, and the operator should see that sentence
            # while they can still press Ctrl+C.
            console.print("  [yellow]· PLUS gmail.readonly — lets ApplyPilot read what replies "
                          "SAY, so it can draft answers.[/yellow]")
            console.print("  [yellow]  That scope can read EVERY message in this mailbox. Google "
                          "has no per-thread scope,\n    so this is all-or-nothing.[/yellow]")
            console.print("  [dim]  What ApplyPilot then does with it is narrower, and that part "
                          "is enforced in code:\n"
                          "    · nothing is read automatically — the poller and `tick` store no "
                          "message text at all\n"
                          "    · text arrives only when you click “⤓ Fetch from Gmail” on one "
                          "conversation\n"
                          "    · at most ~200 chars per message; no column exists that can hold "
                          "a full body[/dim]")
        else:
            console.print("  [dim]· reply CONTENT is off — add --with-content to let ApplyPilot "
                          "read what replies say.[/dim]")
        ok, msg = gmail_oauth.connect(with_content=with_content)
        console.print(f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]")
        if ok:
            # Recover thread ids for anything sent before they were persisted, so those
            # conversations get real replies instead of new threads.
            from applypilot.networking.gmail_send import backfill_thread_ids
            res = backfill_thread_ids()
            console.print(f"[cyan]{res['message']}[/cyan]" if res["ok"] else f"[yellow]{res['message']}[/yellow]")
            sig = gmail_oauth.fetch_signature(gmail_oauth.connected_email())
            console.print(f"[cyan]Signature: {'found — will be appended to outreach' if sig else 'none set on this account'}[/cyan]")
        raise typer.Exit(code=0 if ok else 1)

    # Re-run the thread-id recovery on its own (idempotent).
    if fix_threads:
        from applypilot.networking.gmail_send import backfill_thread_ids
        res = backfill_thread_ids()
        console.print(f"[green]{res['message']}[/green]" if res["ok"] else f"[red]{res['message']}[/red]")
        raise typer.Exit(code=0 if res["ok"] else 1)

    # One-time LinkedIn login for the DM sender (needs consent first).
    if dm_login:
        from applypilot.networking import linkedin_dm
        if not linkedin_dm.agent_browser_bin():
            console.print("[red]agent-browser not found.[/red] Install it, or set AGENT_BROWSER_BIN.")
            raise typer.Exit(code=1)
        if not linkedin_dm.has_consent():
            console.print("\n[yellow]LinkedIn DM automation — please read:[/yellow]\n")
            console.print(linkedin_dm.CONSENT_TEXT)
            if not typer.confirm("Acknowledge the irreversible ban risk and enable DM login?", default=False):
                console.print("[dim]Cancelled — DM sender not enabled.[/dim]")
                raise typer.Exit()
            linkedin_dm.record_consent()
        console.print("[cyan]Opening a browser — log into LinkedIn, then wait…[/cyan]")
        ok = linkedin_dm.open_login_browser()
        if ok:
            console.print("[green]Logged in.[/green] Set NETWORKING_LINKEDIN_DM=1 to allow real sends.")
        else:
            console.print("[red]Did not detect a LinkedIn login (timed out).[/red] Re-run `--dm-login`.")
        raise typer.Exit(code=0 if ok else 1)

    # List DM-eligible contacts (drafted note + LinkedIn URL).
    if dm_list:
        from applypilot.database import get_connection
        from applypilot.networking.store import init_contacts
        conn = get_connection()
        init_contacts(conn)
        rows = conn.execute(
            "SELECT id, full_name, title, company, dm_status FROM contacts "
            "WHERE linkedin_url IS NOT NULL AND linkedin_url != '' "
            "AND linkedin_message IS NOT NULL AND linkedin_message != '' "
            "ORDER BY discovered_at DESC"
        ).fetchall()
        if not rows:
            console.print("[yellow]No DM-eligible contacts (need a LinkedIn URL + a drafted note).[/yellow]")
            raise typer.Exit()
        from rich.table import Table
        t = Table(title="DM-eligible contacts")
        for col in ("id", "name", "title", "company", "dm status"):
            t.add_column(col)
        for r in rows:
            t.add_row(r["id"], r["full_name"] or "", (r["title"] or "")[:30],
                      r["company"] or "", r["dm_status"] or "none")
        console.print(t)
        raise typer.Exit()

    # Compose a LinkedIn connection note into the invite dialog and leave it open for
    # you to click Send (the reliable, low-risk path — LinkedIn soft-blocks automated sends).
    if compose_dm or send_dm:
        from applypilot.networking import linkedin_dm, store
        if not dm_contact:
            console.print("[red]--compose-dm needs --dm-contact <id>[/red] (see `network --dm-list`).")
            raise typer.Exit(code=1)
        contact = store.get_contact(dm_contact)
        if not contact:
            console.print(f"[red]No contact with id:[/red] {dm_contact}")
            raise typer.Exit(code=1)
        console.print(f"Composing LinkedIn note for [cyan]{contact.get('full_name')}[/cyan] "
                      "[dim](review + click Send in the browser)[/dim]")
        res = linkedin_dm.compose(contact)
        color = "green" if res.get("ok") else "red"
        console.print(f"[{color}]{res.get('message')}[/{color}]")
        if res.get("screenshot"):
            console.print(f"[dim]screenshot: {res['screenshot']}[/dim]")
        raise typer.Exit(code=0 if res.get("ok") else 1)

    from applypilot.config import require_contacts_provider
    require_contacts_provider("networking")

    from applypilot.database import get_connection
    from applypilot.networking import service
    from applypilot.networking.store import init_contacts

    conn = get_connection()
    init_contacts(conn)

    if url:
        row = conn.execute(
            "SELECT url, title, company, site, application_url, full_description "
            "FROM jobs WHERE url = ? OR application_url = ? LIMIT 1", (url, url)
        ).fetchone()
        if not row:
            console.print(f"[red]No job found for URL:[/red] {url}")
            raise typer.Exit(code=1)
        jobs = [dict(zip(row.keys(), row))]
    else:
        rows = conn.execute(
            "SELECT j.url, j.title, j.company, j.site, j.application_url, j.full_description "
            "FROM jobs j "
            "WHERE j.applied_at IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.job_url = j.url) "
            "ORDER BY j.applied_at DESC LIMIT ?", (limit,)
        ).fetchall()
        jobs = [dict(zip(r.keys(), r)) for r in rows]

    if not jobs:
        console.print("[yellow]No jobs to process[/yellow] (applied jobs already have contacts, or none applied).")
        return

    console.print(f"\n[bold blue]Networking[/bold blue] — {len(jobs)} job(s), up to {per_job} contacts each"
                  f"{' [dry-run]' if dry_run else ''}\n")

    total_found = total_revealed = 0
    for job in jobs:
        res = service.find_contacts_for_job(
            job, per_job=per_job, use_linkedin=not no_linkedin, dry_run=dry_run, draft=draft
        )
        total_found += res["found"]
        total_revealed += res["revealed"]
        company = res.get("company") or job.get("site") or "?"
        console.print(f"  [cyan]{company}[/cyan] — {res['found']} found, "
                      f"{res['revealed']} with email  [dim]({res['note']})[/dim]")
        for c in res["contacts"]:
            badge = {"verified": "[green]✓[/green]", "unverified": "[yellow]?[/yellow]"}.get(
                c.get("email_status"), "[dim]—[/dim]")
            console.print(f"      {c.get('full_name') or '?'} — {c.get('title') or '?'} "
                          f"[dim]{c.get('match_reason') or ''}[/dim]  {badge} {c.get('email') or ''}")

    console.print(f"\n[bold]Total:[/bold] {total_found} contacts, {total_revealed} with email\n")


@app.command()
def tick(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what it would do; change nothing."),
) -> None:
    """One unattended heartbeat: poll replies, free stale locks, draft what came due.

    Idempotent and safe to run repeatedly — this is what the schedule calls. It NEVER sends
    anything and NEVER starts an apply; both stay a human action.
    """
    from applypilot import tick as tick_mod

    out = tick_mod.run(dry_run=dry_run)
    console.print(f"\n[bold]tick[/bold]{' [dim](dry run)[/dim]' if dry_run else ''}")
    for name, res in out["steps"].items():
        mark = "[red]✗[/red]" if res.get("error") else "[green]✓[/green]"
        console.print(f"  {mark} {name:<11} {res.get('detail', '')}")


@app.command()
def schedule(
    install: bool = typer.Option(False, "--install", help="Install the hourly launchd job."),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove it."),
) -> None:
    """Install or remove the macOS schedule that runs `tick` hourly."""
    from applypilot import schedule as sched

    if install and uninstall:
        console.print("[red]Pick one of --install / --uninstall.[/red]")
        raise typer.Exit(1)
    if install:
        ok, msg = sched.install()
    elif uninstall:
        ok, msg = sched.uninstall()
    else:
        state = "installed" if sched.installed() else "not installed"
        console.print(f"Schedule: [bold]{state}[/bold]  ({sched.plist_path()})")
        return
    console.print(f"[{'green' if ok else 'red'}]{msg}[/]")
    raise typer.Exit(0 if ok else 1)


@app.command()
def stats(
    outreach: bool = typer.Option(False, "--outreach", help="Reply rates and the outreach funnel."),
) -> None:
    """Outcome metrics — what actually worked (CRM-2).

    The same numbers the dashboard's Outcomes panel shows, from the same pure aggregation, so
    the two can never disagree.
    """
    _bootstrap()
    from applypilot.database import get_connection
    from applypilot.domain import metrics as metrics_mod
    from applypilot.domain.timeutil import parse_ts
    from applypilot.networking import store as _store
    from applypilot.networking import touches as _touches
    from applypilot.repo import jobs as _jobs

    if not outreach:
        console.print("Nothing else to show yet — try [bold]applypilot stats --outreach[/bold]")
        return

    conn = get_connection()
    rows = _jobs.dashboard_rows(conn=conn)
    jobs = [dict(zip(r.keys(), r)) if not isinstance(r, dict) else r for r in rows]
    mx = metrics_mod.summary(jobs, _store.all_contacts_for_metrics(conn),
                             _touches.all_sent_touches(conn), parse_ts)

    f = mx["funnel"]
    console.print("\n[bold]Outreach funnel[/bold]")
    for step in f["steps"]:
        console.print(f"  {step['label']:<16} {step['n']}")
    if f["bounced"]:
        console.print(f"  [red]{'bounced':<16} {f['bounced']}[/red]  "
                      f"[dim](never arrived — excluded from every rate below)[/dim]")

    def _rates(title, rates):
        if not rates:
            return
        console.print(f"\n[bold]{title}[/bold]")
        for r in rates:
            # Below the threshold we print the raw counts, never a percentage: a rate from a
            # handful of sends is arithmetic dressed up as evidence.
            value = f"{r['pct']}% ({r['hits']}/{r['n']})" if r["meaningful"] \
                else f"[dim]{r['hits']} of {r['n']} — too few to rate[/dim]"
            console.print(f"  {r['label']:<28} {value}")

    _rates("Overall", [mx["overall"]])
    _rates("Warm vs cold", mx["by_layer"])
    _rates("By verification confidence", mx["by_confidence"])
    _rates("By follow-ups sent", mx["by_touch"])

    if mx["median_hours_to_reply"] is not None:
        console.print(f"\n[bold]Median time to reply:[/bold] {mx['median_hours_to_reply']}h")

    quiet = [r for r in mx["by_company"] if not r["replied"]]
    if quiet:
        console.print("\n[bold]Companies that have never replied[/bold]")
        for r in quiet[:10]:
            extra = f", {r['bounced']} bounced" if r["bounced"] else ""
            console.print(f"  {r['company']:<28} {r['emailed']} emailed{extra}")


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard(
    serve: bool = typer.Option(False, "--serve", help="Run the interactive local operator dashboard."),
    port: int = typer.Option(8765, "--port", help="Port for --serve."),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open the browser automatically."),
) -> None:
    """Generate/open the dashboard, or run the interactive local dashboard."""
    _bootstrap()

    if serve:
        from applypilot.web_dashboard import serve_dashboard

        serve_dashboard(port=port, open_browser=not no_open)
        return

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor(
    config: bool = typer.Option(False, "--config", help="Show every setting, its value, and its source."),
    write_env_example: bool = typer.Option(False, "--write-env-example", help="Regenerate .env.example from the schema."),
) -> None:
    """Check your setup and diagnose missing requirements."""
    import pathlib

    from applypilot import settings as _settings

    if write_env_example:
        target = pathlib.Path(".env.example")
        target.write_text(_settings.render_env_example(), encoding="utf-8")
        console.print(f"[green]Wrote {target}[/green] from src/applypilot/settings.py")
        return

    if config:
        # strict=False on purpose: diagnosing a broken config is exactly when this has to run.
        from applypilot.config import load_env as _load
        problems = _load(strict=False)
        rows = _settings.describe()
        group = None
        for r in rows:
            if r["group"] != group:
                group = r["group"]
                console.print(f"\n[bold]{group}[/bold]")
            # Pad the PLAIN text, then wrap in markup — rich tags count toward f-string
            # width and silently break the columns otherwise.
            mark = {"env": "[cyan]env[/cyan]", ".env": "[blue].env[/blue]",
                    "default": "[dim]default[/dim]"}[r["source"]]
            name = f"{r['name']:<34}"
            if r["deprecated"]:
                name = f"[yellow]{name}[/yellow]"
            val = f"{r['value']:<30}"
            if r["secret"]:
                val = f"[dim]{val}[/dim]"
            console.print(f"  {name}{val}{mark}")
        console.print()
        if problems:
            console.print(f"[red]{len(problems)} invalid value(s):[/red]")
            for prob in problems:
                console.print(f"  [red]✗[/red] {prob}")
        else:
            console.print("[green]All settings parse cleanly.[/green]")
        for warning in _settings.deprecations():
            console.print(f"[yellow]![/yellow] {warning}")
        return

    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    # Show every configured provider (they round-robin + fail over).
    try:
        from applypilot.llm import _detect_providers
        providers = _detect_providers()
        label = ", ".join(f"{n} ({m})" for (n, _b, m, _k) in providers)
        note = f"{len(providers)} provider(s): {label}" if len(providers) > 1 else label
        results.append(("LLM provider(s)", ok_mark, note))
    except Exception:
        results.append(("LLM provider(s)", fail_mark,
                        "Set OPENAI_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY in ~/.applypilot/.env"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # React-PDF resume renderer (optional; falls back to HTML/Chromium if absent)
    try:
        from applypilot.scoring.resume_render import node_renderer_available
        if node_renderer_available():
            results.append(("Resume renderer", ok_mark, "React-PDF (Node) — polished one-page PDFs"))
        elif npx_bin:  # node present but renderer source missing
            results.append(("Resume renderer", "[dim]optional[/dim]",
                            "Falls back to HTML/Chromium renderer"))
        else:
            results.append(("Resume renderer", "[dim]optional[/dim]",
                            "Install Node.js for polished React-PDF resumes (HTML fallback otherwise)"))
    except Exception:
        results.append(("Resume renderer", "[dim]optional[/dim]", "HTML/Chromium fallback"))

    # Contact provider (networking, optional) — live probe of Apollo
    try:
        from applypilot.networking import providers
        prov = providers.active()
        if prov:
            ok, msg = providers.probe()
            results.append(("Contact provider", ok_mark if ok else fail_mark, f"{prov}: {msg}"))
        else:
            results.append(("Contact provider", "[dim]optional[/dim]",
                            "Set APOLLO_API_KEY (paid plan) for networking"))
    except Exception:
        results.append(("Contact provider", warn_mark, "probe failed"))

    # LinkedIn connections import (networking, optional)
    try:
        from applypilot.database import init_db as _init
        from applypilot.networking import connections as _conns
        _init()
        n = _conns.imported_count()
        if n:
            results.append(("LinkedIn connections", ok_mark, f"{n} imported (warm-contact flagging on)"))
        else:
            results.append(("LinkedIn connections", "[dim]optional[/dim]",
                            "import with `network --import-connections Connections.csv`"))
    except Exception:
        results.append(("LinkedIn connections", "[dim]optional[/dim]", "not imported"))

    # LinkedIn fallback (networking, optional, opt-in)
    try:
        from applypilot.networking import linkedin_agent
        if not linkedin_agent.enabled():
            results.append(("LinkedIn fallback", "[dim]optional[/dim]",
                            "off (set NETWORKING_LINKEDIN=1 + `network --linkedin-login`)"))
        elif not linkedin_agent.has_consent() or not linkedin_agent.login_state_ok():
            results.append(("LinkedIn fallback", warn_mark,
                            "enabled but needs `applypilot network --linkedin-login`"))
        else:
            used, cap = linkedin_agent.companies_today(), linkedin_agent._daily_limit()
            results.append(("LinkedIn fallback", ok_mark, f"ready ({used}/{cap} companies today)"))
    except Exception:
        results.append(("LinkedIn fallback", "[dim]optional[/dim]", "off"))

    # LinkedIn DM sender (networking, optional, opt-in, agent-browser)
    try:
        from applypilot.networking import linkedin_dm
        ver = linkedin_dm.version()
        if not ver:
            results.append(("LinkedIn DM sender", "[dim]optional[/dim]",
                            "agent-browser not installed (set AGENT_BROWSER_BIN or install it)"))
        elif not linkedin_dm.has_consent():
            results.append(("LinkedIn DM sender", "[dim]optional[/dim]",
                            f"{ver} — run `network --dm-login` (consent + log in)"))
        elif not linkedin_dm.is_logged_in():
            results.append(("LinkedIn DM sender", warn_mark,
                            f"{ver} — profile not logged in; run `network --dm-login`"))
        else:
            state = "enabled" if linkedin_dm.enabled() else "consented, sends OFF (set NETWORKING_LINKEDIN_DM=1)"
            used, cap = linkedin_dm.store.dm_sent_today(), linkedin_dm._daily_limit()
            results.append(("LinkedIn DM sender", ok_mark, f"{ver} — {state} ({used}/{cap} today)"))
    except Exception:
        results.append(("LinkedIn DM sender", "[dim]optional[/dim]", "off"))

    # Gmail send (outreach, optional) — OAuth preferred, else SMTP; live probe
    try:
        from applypilot.networking.gmail_send import auth_probe, transport
        if transport() is not None:
            ok, msg = auth_probe()
            results.append(("Gmail outreach send", ok_mark if ok else fail_mark, msg))
            # A token issued before the read/settings scopes still sends fine, but
            # follow-ups won't thread and mail goes out unsigned — say so explicitly.
            from applypilot.networking import gmail_oauth
            if gmail_oauth.available():
                missing = gmail_oauth.missing_scopes()
                if missing:
                    short = ", ".join(s.rsplit("/", 1)[-1] for s in missing)
                    results.append(("Gmail scopes", warn_mark,
                                    f"missing {short} — follow-ups won't thread / no signature. "
                                    "Re-run `network --gmail-connect`"))
                else:
                    sig = "signature found" if gmail_oauth.fetch_signature() else "no signature set"
                    results.append(("Gmail scopes", ok_mark, f"send + read + settings ({sig})"))
        else:
            results.append(("Gmail outreach send", "[dim]optional[/dim]",
                            "Run `applypilot network --gmail-connect` (OAuth) to send outreach"))
    except Exception:
        results.append(("Gmail outreach send", warn_mark, "probe failed"))

    # Reply detection (CRM-1). Reported separately from sending: it degrades on its own — a
    # token without gmail.metadata still sends perfectly well, it just cannot see answers.
    try:
        from applypilot.networking import gmail_read
        ok, why = gmail_read.available()
        if ok:
            wm = gmail_read.load_watermark()
            last = (wm.get("checked_at") or "")[:16].replace("T", " ")
            results.append(("Reply detection", ok_mark,
                            f"on — last checked {last}" if last else "on — never polled yet"))
        else:
            results.append(("Reply detection", warn_mark, why))
    except Exception:
        results.append(("Reply detection", warn_mark, "probe failed"))

    # Reply CONTENT (CRM-4b). Reported as a deliberate OFF rather than a missing feature: not
    # granting this is a legitimate choice, and `doctor` should describe the trade, never nag.
    try:
        from applypilot.networking import gmail_read as _gr
        can, why = _gr.can_read_content()
        if can:
            results.append(("Reply content", ok_mark,
                            "on (gmail.readonly) — fetched per conversation on request only; "
                            "nothing is read automatically"))
        else:
            results.append(("Reply content", "[dim]off[/dim]",
                            why if "not connected" in why else
                            "off — headers only. `network --gmail-connect --with-content` "
                            "grants gmail.readonly (reads the WHOLE mailbox) to draft answers"))
    except Exception:
        results.append(("Reply content", warn_mark, "probe failed"))

    # Unattended schedule (CRM-3b). Optional: everything still works by hand without it.
    try:
        from applypilot import schedule as _sched
        if _sched.installed():
            results.append(("Unattended tick", ok_mark, f"scheduled ({_sched.plist_path().name})"))
        else:
            results.append(("Unattended tick", "[dim]optional[/dim]",
                            "not scheduled — run `applypilot schedule --install`"))
    except Exception:
        results.append(("Unattended tick", warn_mark, "probe failed"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Schema version (ARCH-5) — a failed migration is otherwise invisible until something
    # reads a column that was never backfilled.
    try:
        from applypilot import migrations
        from applypilot.database import get_connection, init_db
        init_db()
        st = migrations.status(get_connection())
        line = f"[bold]Schema version:[/bold] {st['version']}"
        if st["failed"]:
            line += f"  [red]{len(st['failed'])} FAILED[/red] — run `applypilot migrate --status`"
        elif st["pending"]:
            line += f"  [yellow]{len(st['pending'])} pending[/yellow]"
        console.print(line)
        console.print()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[dim]Schema version unavailable: {exc}[/dim]\n")

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


@app.command("migrate")
def migrate(
    status: bool = typer.Option(False, "--status", help="Show schema version and pending migrations."),
) -> None:
    """Apply pending schema migrations (ARCH-5), or report what is pending.

    Migrations also run automatically at startup; this is for inspecting them, and for
    retrying one that failed.
    """
    from applypilot import migrations
    from applypilot.database import get_connection, init_db

    init_db()                       # this already runs pending migrations
    conn = get_connection()
    st = migrations.status(conn)

    console.print(f"[bold]Schema version:[/bold] {st['version']}")
    for v in st["applied"]:
        console.print(f"  [green]✓[/green] {v:03d}")
    for f in st["failed"]:
        console.print(f"  [red]✗ {f['version']:03d}[/red] {f['name']}  [dim]{f['error']}[/dim]")
    for p_ in st["pending"]:
        console.print(f"  [yellow]·[/yellow] {p_['version']:03d} {p_['name']} [dim]pending[/dim]")
    if not st["failed"] and not st["pending"]:
        console.print("[dim]Up to date.[/dim]")

    if status:
        return
    results = migrations.run_pending(conn)
    for r in results:
        mark = "[green]✓[/green]" if r["ok"] else "[red]✗[/red]"
        console.print(f"  {mark} {r['version']:03d} {r['name']} {r.get('note') or r.get('error', '')}")
    if not results:
        console.print("[dim]Nothing to apply.[/dim]")
    if any(not r["ok"] for r in results):
        raise typer.Exit(1)


@app.command("migrate-touches")
def migrate_touches(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan; write nothing."),
    verify_only: bool = typer.Option(False, "--verify", help="Check the new tables against the old columns."),
    drop: bool = typer.Option(False, "--drop-legacy", help="Drop the ten migrated columns (after --verify is clean)."),
) -> None:
    """ARCH-3: move follow-up state from ten `contacts` columns into `touches`.

    Order matters and is enforced: --dry-run to see it, plain to write it (a backup is
    taken first), --verify to prove it round-trips, and only then --drop-legacy.
    """
    from applypilot.database import get_connection, init_db
    from applypilot.networking import backfill_touches as B
    from applypilot.networking import store, touches

    init_db()
    conn = get_connection()
    store.init_contacts(conn)
    touches.init_touches(conn)

    if verify_only:
        problems = B.verify(conn)
        if problems:
            console.print(f"[red]✗ {len(problems)} mismatch(es)[/red]")
            for p in problems[:40]:
                console.print(f"  {p}")
            raise typer.Exit(1)
        console.print("[green]✓ new tables round-trip to the old columns exactly[/green]")
        return

    if drop:
        if B.verify(conn):
            console.print("[red]✗ refusing to drop: --verify is not clean[/red]")
            raise typer.Exit(1)
        dropped = B.drop_legacy_columns(conn)
        console.print(f"[green]✓ dropped {len(dropped)} legacy column(s)[/green]: {', '.join(dropped) or '(none)'}")
        return

    items = B.plan(conn)
    console.print(f"[bold]{len(items)} contact/channel ladder(s) to migrate[/bold]")
    console.print(B.describe(items))
    if dry_run:
        console.print("\n[dim]--dry-run: nothing written.[/dim]")
        return

    backup = B.backup_db()
    console.print(f"\n[dim]backup: {backup}[/dim]")
    result = B.apply(conn, items)
    console.print(f"[green]✓ wrote {result['touches']} touch(es), "
                  f"{result['sequences']} sequence row(s)[/green]")
    problems = B.verify(conn)
    if problems:
        console.print(f"[red]✗ verify found {len(problems)} mismatch(es) — legacy columns kept[/red]")
        for p in problems[:40]:
            console.print(f"  {p}")
        raise typer.Exit(1)
    console.print("[green]✓ verified: round-trips to the old columns exactly[/green]")
    console.print("[dim]next: applypilot migrate-touches --drop-legacy[/dim]")


if __name__ == "__main__":
    app()
