"""Pause a running apply and hand the browser to the operator.

The apply runs in a SEPARATE OS PROCESS from the dashboard (`subprocess.run` on the CLI), so
the dashboard cannot flip a flag in its memory. It also must not signal the process group:
`/api/stop` does `os.killpg(SIGTERM)`, which reaches Chrome too and destroys the half-filled
form — the exact loss this feature exists to avoid.

So the signal is a file. It survives across processes, needs no ports or IPC, and if the
dashboard dies mid-pause the flag is still there to be found and cleared.

Pause is NOT suspend-and-resume of the agent. The agent is stopped for good; what is preserved
is the BROWSER and everything typed into it, handed over as `needs_human:paused`. That is the
useful operation — "stop touching it, I'll finish this myself" — and it is the same handover
co-pilot already performs, so the dashboard's Continue / Mark-submitted paths work unchanged.
"""

from __future__ import annotations

import logging

from applypilot import config

logger = logging.getLogger(__name__)

#: Presence of this file means "stop the agent at the next opportunity, keep the browser".
#: Lives in APP_DIR rather than a temp dir so it is visible to anything that needs to clear it.
PAUSE_FILENAME = "apply.pause"


def _pause_path():
    return config.APP_DIR / PAUSE_FILENAME


def request_pause(url: str = "") -> None:
    """Ask the running apply to stop and hand over. Safe to call when nothing is running."""
    try:
        config.APP_DIR.mkdir(parents=True, exist_ok=True)
        _pause_path().write_text(url or "any", encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        logger.warning("Could not write the pause flag: %s", e)


def clear_pause() -> None:
    """Remove the flag. Called when a pause is consumed AND when a run starts.

    Clearing at start-up matters: a flag left behind by a crash would otherwise pause every
    future application the moment it began, which looks exactly like the apply being broken.
    """
    try:
        _pause_path().unlink(missing_ok=True)
    except OSError as e:  # noqa: BLE001
        logger.warning("Could not clear the pause flag: %s", e)


def pause_requested() -> bool:
    """True if a pause is pending. Cheap enough to poll per agent action."""
    try:
        return _pause_path().exists()
    except OSError:
        return False
