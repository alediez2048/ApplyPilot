"""Install `applypilot tick` as a macOS launchd job.

A real CLI command on a timer, not a daemon: `launchd` already solves supervision, restart and
logging, and a command stays testable by hand. Writing a daemon here would mean reimplementing
all of that badly.

Hourly during working hours by default. `tick` never sends and never applies, so the worst case
of an unwanted run is a few drafts queued for review and one Gmail read.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.applypilot.tick"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _log_dir() -> Path:
    from applypilot import config
    return config.LOG_DIR


def build_plist(hours: list[int] | None = None) -> dict:
    """The launchd job. `sys.executable` so the venv's interpreter is used, not system python."""
    hours = hours or list(range(8, 20))  # 08:00–19:00
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "applypilot.cli", "tick"],
        "StartCalendarInterval": [{"Hour": h, "Minute": 0} for h in hours],
        "StandardOutPath": str(_log_dir() / "tick.log"),
        "StandardErrorPath": str(_log_dir() / "tick.err.log"),
        # Deliberately NOT RunAtLoad: installing the schedule should not immediately fire a
        # run, and `launchd` reloads agents at login — a login should not trigger work either.
        "RunAtLoad": False,
    }


def install(hours: list[int] | None = None) -> tuple[bool, str]:
    from applypilot import config
    config.ensure_dirs()
    path = plist_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            plistlib.dump(build_plist(hours), fh)
    except OSError as e:
        return False, f"could not write {path}: {e}"

    # Unload first so re-installing picks up a changed plist instead of silently keeping the
    # old schedule. Both calls are best-effort: bootout fails when nothing is loaded.
    uid = _uid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                   capture_output=True, check=False)
    proc = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return False, f"wrote {path} but launchctl failed: {(proc.stderr or '').strip()[:200]}"
    return True, f"installed — hourly {min(build_plist(hours)['StartCalendarInterval'], key=lambda d: d['Hour'])['Hour']:02d}:00 onwards ({path})"


def uninstall() -> tuple[bool, str]:
    path = plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{LABEL}"],
                   capture_output=True, check=False)
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            return False, f"unloaded but could not remove {path}: {e}"
    return True, "uninstalled"


def installed() -> bool:
    return plist_path().exists()


def _uid() -> int:
    import os
    return os.getuid()
