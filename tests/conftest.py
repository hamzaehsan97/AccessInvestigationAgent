"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.db import ReadOnlyDB

DB_PATH = Path(__file__).resolve().parents[1] / "input_data" / "access_snapshot.sqlite"


@pytest.fixture(scope="session")
def db() -> ReadOnlyDB:
    """One read-only DB handle shared across the whole test session."""
    return ReadOnlyDB(str(DB_PATH))
