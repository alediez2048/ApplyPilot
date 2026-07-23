"""Co-pilot review handoff — the Chrome keep-alive contract.

In co-pilot mode the agent fills the application and STOPS, leaving the browser open for the human
to review + submit. That only works if the browser survives ALL cleanup paths (per-worker cleanup,
kill_all_chrome, the atexit handler). These tests pin that: a kept-alive worker is never reaped,
while normal workers still are.
"""

from __future__ import annotations

import applypilot.apply.chrome as ch


class _FakeProc:
    def __init__(self):
        self.pid = 424242

    def poll(self):
        return None  # "still running"


def _reset():
    with ch._chrome_lock:
        ch._chrome_procs.clear()
        ch._keep_alive_ports.clear()


def test_kept_alive_worker_survives_every_cleanup_path(monkeypatch):
    _reset()
    kills = {"n": 0}
    monkeypatch.setattr(ch, "_kill_process_tree", lambda pid: kills.__setitem__("n", kills["n"] + 1))
    monkeypatch.setattr(ch, "_kill_on_port", lambda port: None)

    proc = _FakeProc()
    with ch._chrome_lock:
        ch._chrome_procs[0] = proc

    ch.keep_chrome_alive(0)
    assert 0 not in ch._chrome_procs                       # stopped tracking
    assert (ch.BASE_CDP_PORT + 0) in ch._keep_alive_ports  # port protected

    ch.cleanup_worker(0, proc)
    ch.kill_all_chrome()
    ch.cleanup_on_exit()
    assert kills["n"] == 0, "a kept-alive review browser must never be killed"


def test_normal_worker_is_still_cleaned_up(monkeypatch):
    _reset()
    kills = {"n": 0}
    monkeypatch.setattr(ch, "_kill_process_tree", lambda pid: kills.__setitem__("n", kills["n"] + 1))
    monkeypatch.setattr(ch, "_kill_on_port", lambda port: None)

    proc = _FakeProc()
    with ch._chrome_lock:
        ch._chrome_procs[1] = proc
    ch.cleanup_worker(1, proc)
    assert kills["n"] == 1, "a normal (non-kept) worker's Chrome must still be reaped"
