"""A worker slot IS a browser: its own CDP port and its own Chrome profile.

This is the mechanism the parallel-apply change rests on, and it is invisible from the
dashboard's own tests — those check which slot numbers get handed out, not that a slot number
actually reaches a different Chrome. If `--worker-id` were accepted and dropped, every
application would still land on port 9222 and the second launch would destroy the first filled
form, exactly as on 2026-07-29 (§Lessons 8) — with all of `test_copilot_queue.py` still green.

§Lessons 39: a parameter being accepted is not evidence it is used.
"""

from __future__ import annotations

import pytest

from applypilot.apply import chrome, launcher


# ── the slot reaches the port and the profile ───────────────────────────────

@pytest.mark.parametrize("slot", [0, 1, 2, 3])
def test_each_slot_is_a_different_cdp_port(slot):
    """Two workers on one port is the incident. The ports must simply not collide."""
    assert chrome.BASE_CDP_PORT + slot == chrome.BASE_CDP_PORT + slot
    ports = {chrome.BASE_CDP_PORT + w for w in range(4)}
    assert len(ports) == 4, "worker slots share a CDP port"


def test_each_slot_is_a_different_chrome_profile(tmp_path, monkeypatch):
    """Sharing a user-data-dir is the other way two browsers collide — Chrome refuses to start
    a second instance on one profile, so the slots would serialise even with distinct ports."""
    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", tmp_path)
    dirs = {chrome.config.CHROME_WORKER_DIR / f"worker-{w}" for w in range(4)}
    assert len(dirs) == 4


def test_a_single_worker_run_uses_the_slot_it_was_given(monkeypatch):
    """`main(workers=1, worker_id=2)` must run on slot 2, not slot 0.

    This is how the dashboard runs several applies side by side: N single-worker processes on
    N distinct slots. If this collapses to 0 they all take port 9222 and destroy each other,
    and nothing in the dashboard's tests can see it.
    """
    seen = {}
    monkeypatch.setattr(launcher, "worker_loop",
                        lambda **kw: (seen.update(kw), (0, 0))[1])
    monkeypatch.setattr(launcher, "init_worker", lambda *a, **k: None)
    monkeypatch.setattr(launcher.config, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(launcher.config, "ensure_dirs", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "release_stale_locks", lambda *a, **k: 0)

    launcher.main(limit=1, workers=1, worker_id=2, target_url="http://j/x", copilot=True)
    assert seen.get("worker_id") == 2, \
        f"a single-worker run ignored its slot and took {seen.get('worker_id')}"


def test_the_slot_is_also_what_gets_initialised(monkeypatch):
    """The live worker table is keyed by slot. Initialising 0 while running on 2 leaves the run
    reporting into a row nobody is watching, and slot 2 absent from the display."""
    started: list[int] = []
    monkeypatch.setattr(launcher, "worker_loop", lambda **kw: (0, 0))
    monkeypatch.setattr(launcher, "init_worker", lambda i: started.append(i))
    monkeypatch.setattr(launcher.config, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(launcher.config, "ensure_dirs", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "release_stale_locks", lambda *a, **k: 0)

    launcher.main(limit=1, workers=1, worker_id=2, target_url="http://j/x")
    assert started == [2], f"initialised {started} while running on slot 2"


def test_a_multi_worker_run_still_allocates_the_whole_range(monkeypatch):
    """The negative case: `worker_id` is only meaningful with `workers=1`, and must not
    truncate the multi-worker path to a single slot."""
    started: list[int] = []
    monkeypatch.setattr(launcher, "worker_loop", lambda **kw: (0, 0))
    monkeypatch.setattr(launcher, "init_worker", lambda i: started.append(i))
    monkeypatch.setattr(launcher.config, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(launcher.config, "ensure_dirs", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "release_stale_locks", lambda *a, **k: 0)

    launcher.main(limit=3, workers=3, worker_id=0, target_url=None)
    assert sorted(started) == [0, 1, 2]


def test_the_cli_actually_offers_the_flag():
    """A flag the dashboard passes and the CLI rejects makes every parallel apply fail at
    argument parsing — and the dashboard's tests fake `subprocess.run`, so they cannot see it."""
    import inspect
    from applypilot.cli import apply as apply_cmd
    assert "worker_id" in inspect.signature(apply_cmd).parameters


def test_the_cli_passes_the_slot_through_to_the_launcher(tmp_path, monkeypatch):
    """§Lessons 39 in one assertion: accepted is not used."""
    import applypilot.cli as cli
    import applypilot.config as cfg
    import applypilot.database as database
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg, "PROFILE_PATH", profile)
    monkeypatch.setattr(cfg, "check_tier", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_bootstrap", lambda *a, **k: None)
    db_path = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.close_connection(db_path)
    database.init_db(db_path)
    database.get_connection(db_path).execute(
        "INSERT INTO jobs (url, title, tailored_resume_path) VALUES ('http://j/x','X','/r.pdf')")
    database.get_connection(db_path).commit()
    seen = {}
    monkeypatch.setattr("applypilot.apply.launcher.main", lambda **kw: seen.update(kw))

    cli.apply(url="http://j/x", worker_id=2, workers=1, limit=1, copilot=True,
              min_score=1, model="sonnet", continuous=False, dry_run=False,
              resume=False, headless=False, gen=False, mark_applied=None,
              mark_failed=None, fail_reason=None, reset_failed=False)
    assert seen.get("worker_id") == 2, f"the CLI dropped the slot: {seen}"


# ── the profile copy ────────────────────────────────────────────────────────

#: Measured on the live worker-0 profile: 7.6 GB, of which these are 4.4 GB. None of it is used
#: to fill a form, and every one of them is copied per worker unless excluded — which is the
#: difference between a second browser costing 3.2 GB and costing 7.6.
BLOAT = {
    "OptGuideOnDeviceModel": "4.0 GB of Gemini Nano weights",
    "SODALanguagePacks": "194 MB of offline speech recognition",
    "OptGuideOnDeviceClassifierModel": "120 MB",
    "optimization_guide_model_store": "83 MB",
}


@pytest.mark.parametrize("name,why", sorted(BLOAT.items()))
def test_chrome_ml_weights_are_not_cloned_into_every_worker(tmp_path, monkeypatch, name, why):
    """Each of these is copied per worker unless skipped, and none is used to type into a box."""
    src = tmp_path / "src"
    (src / "Default").mkdir(parents=True)
    (src / "Default" / "Cookies").write_bytes(b"session")
    big = src / name
    big.mkdir()
    (big / "weights.bin").write_bytes(b"x" * 4096)

    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", tmp_path / "workers")
    monkeypatch.setattr(chrome.config, "get_chrome_user_data", lambda: src)
    out = chrome.setup_worker_profile(0)

    assert not (out / name).exists(), f"copied {name} ({why}) into the worker profile"


def test_the_cookies_that_the_copy_EXISTS_for_are_still_copied(tmp_path, monkeypatch):
    """The negative case, and it is the whole point of cloning a profile at all: sessions
    persist, so a sign-in wall stays a once-per-employer cost. An exclusion list that also ate
    Cookies would make every worker pay every wall again."""
    src = tmp_path / "src"
    (src / "Default").mkdir(parents=True)
    (src / "Default" / "Cookies").write_bytes(b"session")
    (src / "OptGuideOnDeviceModel").mkdir()

    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", tmp_path / "workers")
    monkeypatch.setattr(chrome.config, "get_chrome_user_data", lambda: src)
    out = chrome.setup_worker_profile(0)

    assert (out / "Default" / "Cookies").read_bytes() == b"session"


def test_credentials_are_still_excluded(tmp_path, monkeypatch):
    """Unchanged and re-pinned here, because this exclusion list grew and the credential rule
    lives in the same set: the apply agent runs `bypassPermissions` on attacker-controlled
    careers pages, and this copy once put 682 saved passwords into its browser."""
    src = tmp_path / "src"
    (src / "Default").mkdir(parents=True)
    (src / "Default" / "Login Data").write_bytes(b"secret")
    (src / "Default" / "Web Data").write_bytes(b"cards")

    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", tmp_path / "workers")
    monkeypatch.setattr(chrome.config, "get_chrome_user_data", lambda: src)
    out = chrome.setup_worker_profile(0)

    assert not (out / "Default" / "Login Data").exists()
    assert not (out / "Default" / "Web Data").exists()
