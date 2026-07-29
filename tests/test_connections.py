"""Tests for LinkedIn connections import + matching."""

from __future__ import annotations

import applypilot.database as database
import pytest

from applypilot.networking import connections as C

_CSV = '''Notes:
"When exporting your connection data, some emails may be missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Ali,Coppinger,https://www.linkedin.com/in/ali-coppinger,,Affirm,Senior HR BP,10 Jan 2025
Jane,Doe,https://www.linkedin.com/in/janedoe,,"Affirm, Inc.",Engineer,01 Feb 2024
Bob,Smith,https://www.linkedin.com/in/bobsmith,,Stripe,PM,03 Mar 2023
'''


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    database.close_connection(db)
    database.init_db(db)
    csv_path = tmp_path / "Connections.csv"
    csv_path.write_text(_CSV, encoding="utf-8")
    return str(csv_path)


def test_import_skips_preamble_and_counts(tmp_path, monkeypatch):
    csv_path = _setup(tmp_path, monkeypatch)
    assert C.import_csv(csv_path) == 3
    assert C.imported_count() == 3


def test_company_normalization_matches_suffix_variants(tmp_path, monkeypatch):
    C.import_csv(_setup(tmp_path, monkeypatch))
    assert C.count_at_company("Affirm") == 2
    assert C.count_at_company("Affirm, Inc.") == 2
    assert C.count_at_company("Stripe") == 1
    assert C.count_at_company("Unknown Co") == 0


def test_match_connection_at_company(tmp_path, monkeypatch):
    C.import_csv(_setup(tmp_path, monkeypatch))
    m = C.match("Ali Coppinger", "Affirm")
    assert m and m["company_match"] is True
    assert m["url"].endswith("/ali-coppinger")


def test_match_connection_elsewhere_flags_not_here(tmp_path, monkeypatch):
    C.import_csv(_setup(tmp_path, monkeypatch))
    m = C.match("Bob Smith", "Affirm")
    assert m and m["company_match"] is False  # a connection, but not at Affirm


def test_no_match_for_non_connection(tmp_path, monkeypatch):
    C.import_csv(_setup(tmp_path, monkeypatch))
    assert C.match("Douglas Kessel", "Affirm") is None
    assert C.match("", "Affirm") is None


def test_reimport_replaces(tmp_path, monkeypatch):
    csv_path = _setup(tmp_path, monkeypatch)
    C.import_csv(csv_path)
    # a second import of a smaller file replaces (not appends)
    small = tmp_path / "small.csv"
    small.write_text("First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
                     "Ali,Coppinger,,,Affirm,HR,10 Jan 2025\n", encoding="utf-8")
    assert C.import_csv(str(small)) == 1
    assert C.imported_count() == 1


# ── company matching: word-aware, not substring ──────────────────────────────
# Regression: a raw `a in b or b in a` test made the 3-letter employer "Arm" match
# 6 unrelated companies out of 8 "connections at Arm" on real imported data.



@pytest.mark.parametrize("target,other", [
    ("Arm", "Armanino"),                      # arm-anino
    ("Arm", "Armadillo World Headquarters"),  # arm-adillo
    ("Arm", "State Farm"),                    # state f-arm
    ("Arm", "Centrient Pharmaceuticals"),     # ph-arm-aceuticals
    ("Arm", "Dharma Capital Holding"),        # dh-arm-a
    ("Arm", "T.REX ARMS, Inc."),              # arm-s
    ("Apple", "Apple Bank"),                  # a real, different company
    ("Arm", ""),
    ("", "Arm"),
])
def test_companies_do_not_match(target, other):
    assert C.companies_match(target, other) is False


@pytest.mark.parametrize("target,other", [
    ("Arm", "Arm"),
    ("Arm", "Arm Holdings"),          # trailing corporate descriptor is allowed
    ("Affirm", "Affirm, Inc."),       # legal suffix stripped
    ("Google", "Google LLC"),
    ("Stripe", "Stripe Technologies"),
    ("Meta", "Meta Platforms"),
    ("arm", "ARM"),                   # case-insensitive
])
def test_companies_match(target, other):
    assert C.companies_match(target, other) is True


def test_company_match_is_symmetric():
    assert C.companies_match("Arm Holdings", "Arm") is True
    assert C.companies_match("Armanino", "Arm") is False
