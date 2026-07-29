"""Scored regression harness for contact resolution (evals/resolution.jsonl).

Unit tests answer yes/no about a bug already known. This scores the whole resolution
chain — job URL → employer → domain → company matching → per-contact verification —
against labelled real cases, so a fix that tightens one company and loosens another is
visible immediately.

That is the actual failure mode here. While fixing the "Arm" substring bug, the first
attempt broke "Meta Platforms"; while fixing the "WRITER" org bug, the first attempt
found ZERO contacts for BetterUp. Both were caught by hand. This is the system version.

Runs fully offline — no API keys, no network, no Apollo credits, and (since ARCH-1)
without importing the web server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from applypilot.domain import verification as verify
from applypilot.domain.company import companies_match
from applypilot.networking import derive

EVAL_FILE = Path(__file__).resolve().parents[1] / "evals" / "resolution.jsonl"


def _load(kind: str) -> list[dict]:
    if not EVAL_FILE.exists():
        pytest.skip(f"eval set not found at {EVAL_FILE}")
    rows = []
    for line in EVAL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("kind") == kind:
            rows.append(row)
    return rows


def _fail_report(kind: str, failures: list[str], total: int) -> str:
    scored = total - len(failures)
    lines = [f"\n  {kind}: {scored}/{total} correct — {len(failures)} regression(s):"]
    lines += [f"    ✗ {f}" for f in failures]
    return "\n".join(lines)


def test_eval_employer_resolution():
    """job URL → employer name and employer domain."""
    rows = _load("employer")
    assert rows, "no employer cases loaded"
    failures = []
    for r in rows:
        job = {"url": r["url"], "site": r.get("site"), "company": r.get("company"),
               "application_url": r["url"]}
        got_company = derive.derive_company(job)
        got_domain = derive.derive_domain(job)
        if (got_company or None) != (r["expect_company"] or None):
            failures.append(f"{r['id']}: company {got_company!r} != {r['expect_company']!r}"
                            f"  ({r.get('why', '')})")
        if (got_domain or None) != (r["expect_domain"] or None):
            failures.append(f"{r['id']}: domain {got_domain!r} != {r['expect_domain']!r}"
                            f"  ({r.get('why', '')})")
    assert not failures, _fail_report("employer resolution", failures, len(rows) * 2)


def test_eval_company_matching():
    """Is company A the same employer as company B — lenient and strict."""
    rows = _load("company_match")
    assert rows, "no company_match cases loaded"
    failures = []
    for r in rows:
        got = companies_match(r["a"], r["b"])
        if got is not r["expect"]:
            failures.append(f"{r['id']}: match({r['a']!r}, {r['b']!r}) = {got}, "
                            f"expected {r['expect']}  ({r.get('why', '')})")
        if "strict_expect" in r:
            got_s = companies_match(r["a"], r["b"], strict=True)
            if got_s is not r["strict_expect"]:
                failures.append(f"{r['id']}: STRICT match({r['a']!r}, {r['b']!r}) = {got_s}, "
                                f"expected {r['strict_expect']}  ({r.get('why', '')})")
    assert not failures, _fail_report("company matching", failures, len(rows))


def test_eval_contact_verification():
    """Does the evidence say this person actually works at the employer?"""
    rows = _load("verify")
    assert rows, "no verify cases loaded"
    failures = []
    for r in rows:
        got = verify.verify_contact(r["contact"], r["employer"], r.get("domain", ""))
        if got["verdict"] != r["expect"]:
            failures.append(f"{r['id']}: verdict {got['verdict']!r} != {r['expect']!r} "
                            f"[{'; '.join(got['reasons']) or 'no reasons'}]  ({r.get('why', '')})")
    assert not failures, _fail_report("contact verification", failures, len(rows))


def test_eval_set_covers_every_shipped_bug():
    """The negative cases are the point — a happy-path set passed all four shipped bugs."""
    body = EVAL_FILE.read_text(encoding="utf-8")
    for marker in ("Armanino", "State Farm", "Pharmaceuticals", "Writer Corporation",
                   "clever.com", "Meta Platforms", "Hamming AI"):
        assert marker in body, f"eval set lost its regression case for {marker!r}"
    shipped = [json.loads(x) for x in body.splitlines()
               if x.strip() and "shipped bug" in x]
    assert len(shipped) >= 10, "shipped-bug cases are the highest-value rows; do not prune them"
