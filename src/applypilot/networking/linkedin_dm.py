"""LinkedIn DM sender — drives the `agent-browser` binary against your REAL Chrome.

Separate-repos architecture: ApplyPilot never imports agent-browser; it shells out to
the CLI (same model as it drives `claude` / `npx` / Chrome). agent-browser keeps ONE
persistent, named browser session across CLI invocations, so a send is a sequence of
calls that all share `--session`:

    open <profile_url> --profile Default --executable-path <real Chrome>
        → snapshot → click → insert → click Send

**Why real Chrome + --profile Default:** agent-browser defaults to its own bundled
Chromium, which CANNOT decrypt Google Chrome's cookies (macOS Keychain-bound), so it
lands on the login page. Pointed at the real Google Chrome binary (`--executable-path`)
on your `Default` profile, Chrome decrypts its own cookies and your existing LinkedIn
login is reused — no re-auth. Caveat: Chrome must be QUIT during a send (a profile can
only be open in one Chrome instance at a time).

A tiny LLM controller loop picks each step from the page's accessibility snapshot; the
driver only ever executes the fixed action set in dm_prompt.ACTIONS. The approved note is
inserted VERBATIM (the model never supplies text), so a prompt-injected page can't change
what gets said.

Safeguards, all enforced here:
  - off by default (NETWORKING_LINKEDIN_DM=0); one-time consent acknowledgement required
  - login precheck — aborts cleanly if Chrome's profile isn't logged into LinkedIn
  - daily cap (LINKEDIN_DM_DAILY_LIMIT), cross-contact dedupe (LINKEDIN_DM_COOLDOWN_DAYS)
  - atomic claim (dm_sent_at IS NULL) — no double-send under the threading server
  - dry-run — composes the note but never clicks Send
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from applypilot import config
from applypilot.networking import dm_prompt, store

log = logging.getLogger(__name__)

_CONSENT_FILE = config.APP_DIR / ".linkedin_dm_consent"
_ARTIFACT_DIR = config.LOG_DIR / "linkedin-dm"

_MAX_STEPS = 14
_CALL_TIMEOUT = 45  # seconds per agent-browser call
# Prefer the 0.32.1 repo build (supports --executable-path); env override wins.
_REPO_BIN = Path.home() / "Desktop" / "agent-browser" / "bin" / "agent-browser-darwin-arm64"
_KNOWN_LOCAL_BIN = Path.home() / ".local" / "bin" / "agent-browser"
# Named session shared across all calls (matches the proven-working `--session` flow).
_SESSION = os.environ.get("LINKEDIN_DM_SESSION", "applypilot-li")

CONSENT_TEXT = (
    "LinkedIn DM automation drives YOUR LinkedIn account with a browser agent to SEND\n"
    "messages. This violates LinkedIn's no-bots Terms of Service and can result in\n"
    "PERMANENT restriction of your account — not just a temporary block. Sends are\n"
    "off by default, capped, verbatim, and human-triggered, but the risk is real and\n"
    "irreversible. A secondary account is strongly recommended.\n"
)


# ── binary discovery ─────────────────────────────────────────────────────────

def agent_browser_bin() -> str | None:
    """Locate agent-browser: env override → 0.32.1 repo build → PATH → known local.

    The repo build is preferred because it supports `--executable-path` (needed to drive
    real Google Chrome so the existing login decrypts).
    """
    override = os.environ.get("AGENT_BROWSER_BIN")
    if override and Path(override).exists():
        return override
    if _REPO_BIN.exists():
        return str(_REPO_BIN)
    found = shutil.which("agent-browser")
    if found:
        return found
    if _KNOWN_LOCAL_BIN.exists():
        return str(_KNOWN_LOCAL_BIN)
    return None


def _chrome_profile() -> str:
    """Which Chrome user profile to reuse (defaults to Default = your main login)."""
    return os.environ.get("LINKEDIN_DM_CHROME_PROFILE", "Default")


def _chrome_exec() -> str | None:
    """Path to REAL Google Chrome (not agent-browser's bundled Chromium)."""
    env = os.environ.get("LINKEDIN_DM_CHROME_EXEC") or os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    try:
        from applypilot.config import get_chrome_path
        return get_chrome_path()
    except Exception:  # noqa: BLE001
        return None


def _chrome_running() -> bool:
    """True if the USER's Google Chrome is open (which locks the Default profile we need).

    Excludes the Chrome that agent-browser itself launches — that one uses an
    `agent-browser-profile-*` user-data-dir, so it must not count as "the user's Chrome"
    (otherwise a compose that just opened its own Chrome would refuse the next one)."""
    try:
        out = subprocess.run(["pgrep", "-af", "Google Chrome"], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    for line in out.stdout.splitlines():
        if "Contents/MacOS/Google Chrome" not in line:
            continue          # skip framework/helper paths
        if "--type=" in line:
            continue          # skip renderer/gpu helper subprocesses
        if "agent-browser-profile" in line:
            continue          # agent-browser's own Chrome, not the user's
        return True           # a real, user-launched Chrome main process
    return False


def version() -> str | None:
    bin_ = agent_browser_bin()
    if not bin_:
        return None
    try:
        out = subprocess.run([bin_, "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr or "").strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _ab(args: list[str], timeout: int = _CALL_TIMEOUT) -> tuple[int, str]:
    """Run one agent-browser command against the named session. Returns (rc, stdout+stderr)."""
    bin_ = agent_browser_bin()
    if not bin_:
        return 127, "agent-browser not found"
    try:
        p = subprocess.run([bin_, "--session", _SESSION, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"agent-browser {args[0] if args else ''} timed out"
    except OSError as e:
        return 1, str(e)


def _open(url: str, headed: bool = True, timeout: int = 60) -> tuple[int, str]:
    """Launch/navigate the session on real Chrome + the reused profile (the working combo)."""
    bin_ = agent_browser_bin()
    if not bin_:
        return 127, "agent-browser not found"
    cmd = [bin_, "--session", _SESSION, "--profile", _chrome_profile()]
    exe = _chrome_exec()
    if exe:
        cmd += ["--executable-path", exe]
    if headed:
        cmd.append("--headed")
    cmd += ["open", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "agent-browser open timed out"
    except OSError as e:
        return 1, str(e)


# ── feature flags / consent ──────────────────────────────────────────────────

def enabled() -> bool:
    return os.environ.get("NETWORKING_LINKEDIN_DM", "0").lower() in {"1", "true", "yes", "on"}


def has_consent() -> bool:
    return _CONSENT_FILE.exists()


def record_consent() -> None:
    _CONSENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONSENT_FILE.write_text("acknowledged\n", encoding="utf-8")


def _daily_limit() -> int:
    try:
        return int(os.environ.get("LINKEDIN_DM_DAILY_LIMIT", "5") or "5")
    except ValueError:
        return 5


def _cooldown_days() -> int:
    try:
        return int(os.environ.get("LINKEDIN_DM_COOLDOWN_DAYS", "30") or "30")
    except ValueError:
        return 30


def under_daily_cap() -> bool:
    return store.dm_sent_today() < _daily_limit()


# ── login state ──────────────────────────────────────────────────────────────

def _current_url() -> str:
    rc, out = _ab(["get", "url"])
    return out.strip() if rc == 0 else ""


def _is_authenticated_url(url: str) -> bool:
    u = (url or "").lower()
    if not u or "linkedin.com" not in u:
        return False
    walls = ("/login", "/authwall", "/checkpoint", "/uas/login", "signup", "/join")
    return not any(w in u for w in walls)


def is_logged_in() -> bool:
    """Reuse your Chrome Default profile and check LinkedIn is authenticated (headless).

    If a separate Chrome instance holds the profile lock, the launch won't be
    authenticated and this returns False — the reliable signal, vs. sniffing processes.
    """
    if not agent_browser_bin():
        return False
    rc, _ = _open("https://www.linkedin.com/feed/", headed=False, timeout=60)
    if rc != 0:
        return False
    time.sleep(2)
    return _is_authenticated_url(_current_url())


def open_login_browser(wait_seconds: int = 300) -> bool:
    """Open real Chrome (Default profile, headed); return True once LinkedIn is authenticated.

    With profile reuse this is usually instant (you're already logged in). If not, log in
    in the opened window — it writes to your real Chrome profile, so it persists normally.
    """
    if not agent_browser_bin():
        log.error("agent-browser not installed.")
        return False
    if _chrome_running():
        log.error("Quit Google Chrome first — its profile is locked while it's open.")
        return False
    rc, out = _open("https://www.linkedin.com/feed/", headed=True, timeout=60)
    if rc != 0:
        log.error("Could not open Chrome: %s", out)
        return False
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _is_authenticated_url(_current_url()):
            return True
        time.sleep(5)
    return False


# ── controller loop ──────────────────────────────────────────────────────────

def _safe_ref(ref: str | None) -> str | None:
    """A ref/selector we're willing to hand to `agent-browser click`. Blocks flag-like values."""
    r = (ref or "").strip()
    if not r or r.startswith("-"):
        return None
    return r


# Snapshot line -> (role, name, ref), e.g.  - menuitem "Connect" [ref=e42].
# Matches ANY role (button, link, menuitem, textbox, …) so dropdown items are found too;
# role filtering happens in the specific helpers below.
_REF_RE = re.compile(r'([a-zA-Z]+)\s+"([^"]*)"\s*\[[^\]]*ref=(e\d+)', re.I)


def _find(args: list[str], timeout: int = _CALL_TIMEOUT) -> tuple[int, str]:
    """agent-browser `find` — a LIVE-DOM semantic locator with a real (trusted) click.
    Unlike snapshot refs, it sees LinkedIn's modal and fires events React responds to."""
    return _ab(["find", *args], timeout=timeout)


def _button_ref(snapshot: str, *name_substrings: str, roles: tuple[str, ...] = ("button", "link")
                ) -> str | None:
    """Find the ref of a button/link whose accessible name contains any given substring.

    Deterministic clicking by ref is far more reliable than `find --name` for LinkedIn's
    invite dialog (whose buttons didn't match name-based locators)."""
    wants = [s.lower() for s in name_substrings]
    for role, name, ref in _REF_RE.findall(snapshot or ""):
        if role.lower() not in roles:
            continue
        low = name.lower()
        if any(w in low for w in wants):
            return ref
    return None


def _send_button_ref(snapshot: str) -> str | None:
    """Ref of the invite dialog's Send button — EXACT name match ('Send' / 'Send invitation'
    / 'Send now') so it never collides with 'Send without a note' or a messaging Send."""
    exact = {"send", "send invitation", "send now"}
    for role, name, ref in _REF_RE.findall(snapshot or ""):
        if role.lower() in ("button", "link") and name.strip().lower() in exact:
            return ref
    return None


def _note_textbox_ref(snapshot: str) -> str | None:
    """Ref of the invite dialog's note textarea, EXCLUDING LinkedIn's global search box."""
    for role, name, ref in _REF_RE.findall(snapshot or ""):
        if role.lower() not in ("textbox", "textarea"):
            continue
        low = name.lower()
        if "looking for" in low or "search" in low:  # skip the top-nav search box
            continue
        return ref
    return None


def _action_bar_more_ref(snapshot: str) -> str | None:
    """The profile action-bar 'More' button — the one immediately followed by a
    'Follow <name>' button (distinguishes it from the top-nav 'More')."""
    seq = _REF_RE.findall(snapshot or "")
    for i, (role, name, ref) in enumerate(seq):
        if role.lower() == "button" and name.strip().lower() == "more":
            for _r2, name2, _ref2 in seq[i + 1:i + 3]:
                if "follow" in name2.lower():
                    return ref
    return None


def _connect_ref(snapshot: str, target_name: str) -> str | None:
    """A 'Connect' action for THE TARGET — the dropdown item 'Connect', or an
    'Invite <target> to connect' button. Never a sidebar 'Invite <other> to connect'."""
    first = (target_name or "").split()[0].lower() if target_name else ""
    for _role, name, ref in _REF_RE.findall(snapshot or ""):
        low = name.strip().lower()
        if low == "connect":
            return ref
        if "invite" in low and "to connect" in low and first and first in low:
            return ref
    return None


def _try_connect_nav(snapshot: str, target_name: str, state: dict) -> bool:
    """Deterministically drive toward the invite dialog: click Connect, or open the
    action-bar More to reveal it. Returns True if it clicked something (re-loop to see result)."""
    cref = _connect_ref(snapshot, target_name)
    if cref:
        _ab(["click", f"@{cref}"])
        time.sleep(1.5)
        return True
    if not state.get("opened_more"):
        more = _action_bar_more_ref(snapshot)
        if more:
            state["opened_more"] = True
            _ab(["click", f"@{more}"])
            time.sleep(1.5)
            return True
    return False


def _invite_dialog_present(snapshot: str) -> bool:
    """True when LinkedIn's Connect invitation dialog is open (Add-a-note / Send-invitation)."""
    s = (snapshot or "").lower()
    return ("add a note to your invitation" in s
            or "send invitation" in s
            or ("add a note" in s and "send without a note" in s))


def _complete_invite(message: str, dry_run: bool, artifact_prefix: str) -> dict:
    """Deterministically finish a Connect invitation: Add a note -> insert verbatim -> Send.

    Called once the invitation dialog is detected, so the fragile final steps don't depend
    on the LLM. Clicks by snapshot ref (reliable). Respects dry_run (composes, never Sends).
    """
    _, snap = _ab(["snapshot"])

    # 1. Click "Add a note" to open the note textarea. The dialog's first screen offers
    #    "Add a note" / "Send without a note"; the note field only exists after this click.
    add_ref = _button_ref(snap, "add a note")
    if add_ref:
        _ab(["click", f"@{add_ref}"])
        time.sleep(1.2)
        _, snap = _ab(["snapshot"])

    # 2. Focus the note textarea (NOT the top-nav search box) and insert the note VERBATIM.
    tb = _note_textbox_ref(snap)
    if tb:
        _ab(["click", f"@{tb}"])
        time.sleep(0.4)
    rc, _ = _ab(["keyboard", "inserttext", message])
    time.sleep(0.6)

    # 3. Verify the note actually landed: the dialog must have advanced PAST the
    #    "Add a note / Send without a note" choice (a note textarea is now present).
    _, snap = _ab(["snapshot"])
    composed = rc == 0 and _note_textbox_ref(snap) is not None and not _button_ref(snap, "add a note")

    shot = _screenshot(f"{artifact_prefix}-preflight")
    if not composed:
        return {"ok": False, "status": "failed",
                "message": "could not open/fill the invitation note field"}
    if dry_run:
        return {"ok": True, "status": "drafted", "screenshot": shot,
                "message": "dry-run: invitation note composed — Send NOT clicked"}

    # 3. Click the dialog's Send button with a REAL (trusted) click. agent-browser's
    #    `find` does a live-DOM locator + native click — this both bypasses the a11y
    #    snapshot (which misses LinkedIn's modal) AND fires a trusted event that LinkedIn's
    #    React send handler actually responds to (a synthetic JS .click() does not).
    #    Verification is JS-based (snapshot can't see the modal).
    for _attempt in range(3):
        sent_click = False
        for name in ("Send", "Send invitation", "Send now"):
            rc, out = _find(["role", "button", "--name", name, "--exact"])
            if rc == 0 and "✗" not in out:
                sent_click = True
                break
        if not sent_click:
            _, chk = _ab(["eval", _JS_DIALOG_OPEN])
            if "closed" in (chk or "").lower():
                break  # dialog already gone
            return {"ok": False, "status": "failed",
                    "message": "invitation dialog present but Send button not clickable"}
        time.sleep(2.5)
        _, chk = _ab(["eval", _JS_DIALOG_OPEN])
        if "closed" in (chk or "").lower():
            break  # dialog gone → invitation sent
    else:
        return {"ok": False, "status": "failed",
                "message": "clicked Send but the invitation dialog stayed open "
                           "(possible weekly invitation limit or a LinkedIn error)"}

    return {"ok": True, "status": "sent",
            "screenshot": _screenshot(f"{artifact_prefix}-sent"),
            "message": "connection invitation sent"}


# JS run inside the live session (the a11y snapshot doesn't capture LinkedIn's modal).
_JS_CLICK_SEND = (
    "(()=>{const d=document.querySelector('[role=dialog]');const s=d||document;"
    "const b=[...s.querySelectorAll('button,[role=button],a')].find(x=>{"
    "const t=(x.textContent||'').trim();const a=(x.getAttribute('aria-label')||'').trim();"
    "return /^send( invitation| now)?$/i.test(t)||/^send( invitation| now)?$/i.test(a);});"
    "if(b){b.click();return 'clicked';}return 'notfound';})()"
)
_JS_DIALOG_OPEN = "(()=>document.querySelector('[role=dialog]')?'open':'closed')()"


def _run_controller(target_name: str, target_url: str, message: str,
                    dry_run: bool, artifact_prefix: str) -> dict:
    """Drive the open session to compose (and, unless dry_run, send) the note.

    Assumes the session is already navigated to target_url. Returns a result dict.
    """
    system = dm_prompt.build_system_prompt()
    history: list[str] = []
    composed = False
    client = None  # lazily created — the deterministic path handles most profiles without an LLM
    nav_state: dict = {}

    for step in range(_MAX_STEPS):
        rc, snapshot = _ab(["snapshot"])
        if rc != 0:
            return {"ok": False, "status": "failed", "message": f"snapshot failed: {snapshot[:200]}"}

        if not _is_authenticated_url(_current_url()):
            return {"ok": False, "status": "failed",
                    "message": "hit a LinkedIn login wall / checkpoint — log in again (--dm-login)"}

        # DETERMINISTIC completion: once the Connect invitation dialog is open, finish it in
        # code (Add a note -> insert verbatim -> Send).
        if _invite_dialog_present(snapshot):
            log.info("DM: invitation dialog detected — completing deterministically")
            return _complete_invite(message, dry_run, artifact_prefix)

        # DETERMINISTIC navigation: click Connect (or open the action-bar More to reveal it).
        # This removes the flaky LLM click-guessing that kept mis-navigating to Message.
        if _try_connect_nav(snapshot, target_name, nav_state):
            log.info("DM step %d: deterministic nav toward Connect", step + 1)
            continue

        if client is None:
            from applypilot.llm import get_client
            client = get_client()
        user = dm_prompt.build_turn_prompt(target_name, target_url, message, snapshot, history, dry_run)
        try:
            raw = client.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=250, temperature=0.0,
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": "failed", "message": f"controller LLM error: {e}"}

        action = dm_prompt.parse_action(raw)
        act = action.get("action")
        log.info("DM step %d: %s (%s)", step + 1, act, action.get("why") or action.get("reason") or "")

        if act == "abort":
            return {"ok": False, "status": "failed",
                    "message": f"agent aborted: {action.get('reason', 'unspecified')}"}

        if act == "click":
            ref = _safe_ref(action.get("ref"))
            if not ref:
                history.append("click skipped (no valid ref)")
                continue
            rc, out = _ab(["click", ref])
            history.append(f"clicked {ref} → {'ok' if rc == 0 else out[:80]}")
            time.sleep(1.2)
            continue

        if act == "type_message":
            # VERBATIM insert into the focused composer — the model never supplies text.
            rc, out = _ab(["keyboard", "inserttext", message])
            composed = rc == 0
            history.append(f"inserted note → {'ok' if composed else out[:80]}")
            time.sleep(0.8)
            continue

        if act == "send":
            if not composed:
                history.append("send refused: note not composed yet")
                continue
            shot = _screenshot(f"{artifact_prefix}-preflight")
            if dry_run:
                return {"ok": True, "status": "drafted", "screenshot": shot,
                        "message": "dry-run: note field holds the verbatim note — Send NOT clicked"}
            ref = _safe_ref(action.get("ref"))
            if not ref:
                return {"ok": False, "status": "failed", "message": "no valid Send button ref"}
            rc, out = _ab(["click", ref])
            if rc != 0:
                return {"ok": False, "status": "failed", "message": f"Send click failed: {out[:120]}"}
            time.sleep(2)
            return {"ok": True, "status": "sent", "screenshot": _screenshot(f"{artifact_prefix}-sent"),
                    "message": f"note sent to {target_name}"}

        if act == "done":
            if dry_run or not composed:
                return {"ok": False, "status": "failed",
                        "message": "agent reported done without a confirmed send"}
            return {"ok": True, "status": "sent", "message": f"note sent to {target_name}"}

    return {"ok": False, "status": "failed", "message": f"gave up after {_MAX_STEPS} steps"}


def _screenshot(name: str) -> str:
    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    path = _ARTIFACT_DIR / f"{name}.png"
    _ab(["screenshot", str(path)])
    return str(path)


def _dismiss_interstitial() -> None:
    """Clear LinkedIn promo modals (e.g. 'Job search with confidence') that load with a
    profile and intercept clicks. Escape closes them; safe here since no composer/invite
    dialog is open yet (only call right after navigating to a profile)."""
    for _ in range(2):
        _ab(["press", "Escape"])
        time.sleep(0.4)


# ── main entry ───────────────────────────────────────────────────────────────

def compose(contact: dict) -> dict:
    """Drive the browser to compose the connection note into the invite dialog, then STOP,
    leaving the window open for the user to review and click Send themselves.

    This is the reliable, low-risk path: automation composes (works perfectly), the HUMAN
    sends. LinkedIn silently soft-blocks *automated* Send clicks but honors real human ones,
    and a human click is near-zero account-restriction risk. Fails soft (never raises).
    """
    name = contact.get("full_name") or "this contact"
    cid = contact.get("id") or ""
    profile_url = (contact.get("linkedin_url") or "").strip()
    note = (contact.get("linkedin_message") or "").strip()

    # Relaxed gates — no auto-send, so no consent/enabled/cap/dedupe needed here.
    if not agent_browser_bin():
        return {"ok": False, "status": "none", "message": "agent-browser not installed"}
    if not profile_url:
        return {"ok": False, "status": "none", "message": "no LinkedIn URL for this contact"}
    if not note:
        return {"ok": False, "status": "none", "message": "no LinkedIn note drafted — generate one first"}
    if _chrome_running():
        return {"ok": False, "status": "none",
                "message": "Quit Google Chrome first — the composer needs your Chrome profile."}
    if not is_logged_in():
        return {"ok": False, "status": "none",
                "message": "DM profile isn't logged into LinkedIn — run `applypilot network --dm-login`"}

    try:
        rc, out = _open(profile_url, headed=True, timeout=60)
        if rc != 0:
            raise RuntimeError(f"could not open profile: {out[:120]}")
        time.sleep(2)
        if not _is_authenticated_url(_current_url()):
            raise RuntimeError("Chrome session isn't authenticated on LinkedIn — quit Chrome and retry")
        _dismiss_interstitial()
        # dry_run=True drives navigation + note composition but never clicks Send.
        result = _run_controller(name, profile_url, note, dry_run=True, artifact_prefix=cid or "dm")
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "status": "failed", "message": f"compose error: {e}"}

    if result.get("ok"):
        if cid:
            store.mark_dm_composed(cid)
        result["status"] = "composed"
        result["message"] = (f"Note composed for {name}. Review the browser window and click "
                             "Send to send the invitation.")
    return result


def send(contact: dict, dry_run: bool = False, headed: bool = True) -> dict:
    """Send (or dry-run compose) the drafted LinkedIn note to one contact.

    Fails soft — always returns {"ok", "status", "message", ...}, never raises.
    """
    name = contact.get("full_name") or "this contact"
    cid = contact.get("id") or ""
    profile_url = (contact.get("linkedin_url") or "").strip()
    note = (contact.get("linkedin_message") or "").strip()

    # ── gates (cheap, before touching the browser) ──────────────────────────
    if not agent_browser_bin():
        return {"ok": False, "status": "none", "message": "agent-browser not installed"}
    if not has_consent():
        return {"ok": False, "status": "none",
                "message": "LinkedIn DM needs one-time consent — run `applypilot network --dm-login`"}
    if not profile_url:
        return {"ok": False, "status": "none", "message": "no LinkedIn URL for this contact"}
    if not note:
        return {"ok": False, "status": "none", "message": "no LinkedIn note drafted — generate one first"}
    if contact.get("dm_status") == "sent":
        return {"ok": False, "status": "sent", "message": "already DM'd this contact"}

    if not dry_run:
        if not enabled():
            return {"ok": False, "status": "none",
                    "message": "DM sending is off — set NETWORKING_LINKEDIN_DM=1 to enable"}
        if not under_daily_cap():
            return {"ok": False, "status": "none",
                    "message": f"daily DM limit reached ({_daily_limit()})"}
        prior = store.already_dmed(profile_url, _cooldown_days(), exclude_id=cid)
        if prior:
            return {"ok": False, "status": "none",
                    "message": f"already DM'd this person for another role on {prior[:10]}"}

    # ── claim (live only) then drive ────────────────────────────────────────
    claimed = False
    if not dry_run:
        if not store.claim_dm_send(cid):
            return {"ok": False, "status": "sending", "message": "a DM send is already in progress / done"}
        claimed = True

    try:
        # Open the target profile on real Chrome (reuses your Default login).
        rc, out = _open(profile_url, headed=headed, timeout=60)
        if rc != 0:
            raise RuntimeError(f"could not open profile: {out[:120]}")
        time.sleep(2)
        if not _is_authenticated_url(_current_url()):
            raise RuntimeError("Chrome session isn't authenticated on LinkedIn. Fully QUIT Google "
                               "Chrome (it locks your profile), then retry — or log into LinkedIn "
                               "in Chrome's Default profile first")
        _dismiss_interstitial()  # clear promo modals before the controller acts

        result = _run_controller(name, profile_url, note, dry_run, artifact_prefix=cid or "dm")
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "status": "failed", "message": f"DM driver error: {e}"}

    # ── persist (live only) ─────────────────────────────────────────────────
    if claimed:
        if result.get("ok"):
            store.mark_dm_sent(cid)
        else:
            store.mark_dm_failed(cid, result.get("message", "unknown error"))
    return result
