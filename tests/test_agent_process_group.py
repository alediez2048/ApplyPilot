"""Killing the agent must not kill the run that is killing it.

2026-07-30. Pause was clicked on a live Deloitte application. The flag was consumed, the agent
died — and so did the apply CLI, with exit code -9:

    Command exited with code -9

`_kill_process_tree` does `os.killpg(os.getpgid(pid), SIGKILL)`, and the agent was spawned
without `start_new_session`, so `getpgid(agent)` is the CLI's OWN group. Pausing SIGKILLed the
process that was about to record the handover, so `elif result == "paused"` never ran: the job
stayed `in_progress`, the browser stayed open, and no Continue button ever appeared.

Latent long before pause existed — the Ctrl+C "skip this job" handlers call the same helper on
the same shared group, so skipping one job would kill the whole run.
"""

from __future__ import annotations

import inspect
import os
import platform
import subprocess
import sys

import pytest

from applypilot.apply import launcher


def test_the_agent_is_spawned_into_its_own_process_group():
    src = inspect.getsource(launcher.run_job)
    assert "start_new_session=" in src, (
        "the agent shares this process's group, so os.killpg on it is suicide")


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX process groups")
def test_killpg_on_a_new_session_child_leaves_the_parent_alive():
    """The mechanism itself, not a string match. Without start_new_session this kills the test
    runner, which is precisely what it did to the apply CLI."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             start_new_session=True)
    try:
        assert os.getpgid(child.pid) != os.getpgid(os.getpid()), \
            "child landed in our own process group"
        launcher._kill_process_tree(child.pid)
        child.wait(timeout=10)
        os.getpgid(os.getpid())  # we are still here
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX process groups")
def test_a_shared_group_child_would_have_taken_us_down():
    """Proves the bug was real rather than theoretical: without start_new_session the child
    shares our group, so a killpg aimed at it would reach us too."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert os.getpgid(child.pid) == os.getpgid(os.getpid()), (
            "expected a shared process group without start_new_session — if this ever fails, "
            "the platform changed and the fix's rationale needs rechecking")
    finally:
        child.kill()


def test_an_orphan_agent_cannot_outlive_the_run():
    """Own process group means the agent no longer dies with us automatically, so it has to be
    reaped explicitly — otherwise an interrupted run leaves a Claude session driving a browser
    with nobody watching."""
    src = inspect.getsource(launcher)
    assert "atexit.register(_reap_agents_on_exit)" in src
    body = inspect.getsource(launcher._reap_agents_on_exit)
    assert "_kill_process_tree" in body and "_claude_procs" in body
