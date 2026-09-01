"""Tests for the read-only DB adapter and nested-group closure."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.core.db import ReadOnlyDB

DB_PATH = Path(__file__).resolve().parents[1] / "input_data" / "access_snapshot.sqlite"


@pytest.fixture(scope="module")
def db() -> ReadOnlyDB:
    return ReadOnlyDB(str(DB_PATH))


def test_snapshot_metadata_present(db: ReadOnlyDB) -> None:
    assert db.snapshot_time_iso.startswith("2026-08-15")
    assert db.dataset_version


def test_writes_are_denied_at_all_layers(db: ReadOnlyDB) -> None:
    # authorizer denies non-read actions
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute(
            "INSERT INTO people (person_id, full_name, primary_email, worker_type, "
            "department, title, employment_status, start_date, location) "
            "VALUES ('x', 'x', 'x', 'employee', 'x', 'x', 'active', '2020-01-01', 'x')"
        )
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("DELETE FROM people WHERE person_id = 'per_000001'")
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("DROP TABLE people")
    # ATTACH should be denied
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("ATTACH DATABASE ':memory:' AS m")


def test_idp_group_closure_is_stable(db: ReadOnlyDB) -> None:
    # Any real group should return a set (possibly empty) without raising
    row = db.one("SELECT group_id FROM idp_groups LIMIT 1")
    assert row is not None
    members = db.idp_group_members(row["group_id"])
    assert isinstance(members, set)


def test_effective_helper_respects_snapshot(db: ReadOnlyDB) -> None:
    assert db.is_effective(None, None) is True
    assert db.is_effective("2020-01-01T00:00:00Z", None) is False
    # expires far in the future
    assert db.is_effective(None, "2999-01-01T00:00:00Z") is True
    # already expired at snapshot
    assert db.is_effective(None, "2025-01-01T00:00:00Z") is False
