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

    load_env()
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
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
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
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
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
    draft: bool = typer.Option(True, "--draft/--no-draft", help="Draft outreach emails for found contacts."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Search + rank only; no Apollo reveal (no credits)."),
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

    # One-time Gmail OAuth connect for outreach sending.
    if gmail_connect:
        from applypilot.networking import gmail_oauth
        console.print("[cyan]Connecting Gmail (opens a browser to authorize send-only access)…[/cyan]")
        ok, msg = gmail_oauth.connect()
        console.print(f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]")
        raise typer.Exit(code=0 if ok else 1)

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
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
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

    # Contact provider (networking, optional) — live probe of Hunter/Apollo
    try:
        from applypilot.networking import providers
        prov = providers.active()
        if prov:
            ok, msg = providers.probe()
            results.append(("Contact provider", ok_mark if ok else fail_mark, f"{prov}: {msg}"))
        else:
            results.append(("Contact provider", "[dim]optional[/dim]",
                            "Set HUNTER_API_KEY (free) or APOLLO_API_KEY for networking"))
    except Exception:
        results.append(("Contact provider", warn_mark, "probe failed"))

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

    # Gmail send (outreach, optional) — OAuth preferred, else SMTP; live probe
    try:
        from applypilot.networking.gmail_send import auth_probe, transport
        if transport() is not None:
            ok, msg = auth_probe()
            results.append(("Gmail outreach send", ok_mark if ok else fail_mark, msg))
        else:
            results.append(("Gmail outreach send", "[dim]optional[/dim]",
                            "Run `applypilot network --gmail-connect` (OAuth) to send outreach"))
    except Exception:
        results.append(("Gmail outreach send", warn_mark, "probe failed"))

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


if __name__ == "__main__":
    app()
