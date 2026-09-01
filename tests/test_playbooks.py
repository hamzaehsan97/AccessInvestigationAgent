"""End-to-end tests for the deterministic (offline) playbooks."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.core.db import ReadOnlyDB
from agent.playbooks import (
    offline_access_explain_report,
    offline_offboarding_report,
)
from agent.llm.runtime import _extract_citations

DB_PATH = Path(__file__).resolve().parents[1] / "input_data" / "access_snapshot.sqlite"


@pytest.fixture(scope="module")
def db() -> ReadOnlyDB:
    return ReadOnlyDB(str(DB_PATH))


def test_offboarding_playbook_cites_resolvable_ids(db: ReadOnlyDB) -> None:
    inv = offline_offboarding_report(limit=3, db=db)
    assert inv.status == "succeeded"
    assert inv.verified is True
    cited = _extract_citations(inv.report_md)
    assert cited, "expected at least one `kind:id` citation in report"
    # verify all resolve
    from agent.tools.access import resolve_evidence
    from agent.core.schemas import ResolveEvidenceIn

    res = resolve_evidence(ResolveEvidenceIn(refs=cited), db=db)
    unresolved = [r for r in res.data if not r["exists"]]
    assert not unresolved, f"unresolved: {unresolved[:3]}"


def test_access_explain_playbook_traces_grant_chain(db: ReadOnlyDB) -> None:
    # Pick an active person we know has grants
    row = db.one(
        "SELECT full_name FROM people WHERE employment_status='active' "
        "AND person_id IN (SELECT person_id FROM idp_accounts WHERE person_id IS NOT NULL) "
        "LIMIT 1"
    )
    assert row is not None
    inv = offline_access_explain_report(person_query=row["full_name"], db=db)
    assert inv.status == "succeeded"
    assert "**Evidence:**" in inv.report_md
    # citations should be resolvable
    cited = _extract_citations(inv.report_md)
    assert cited
    from agent.tools.access import resolve_evidence
    from agent.core.schemas import ResolveEvidenceIn

    res = resolve_evidence(ResolveEvidenceIn(refs=cited), db=db)
    unresolved = [r for r in res.data if not r["exists"]]
    assert not unresolved


def test_access_explain_returns_no_match_gracefully(db: ReadOnlyDB) -> None:
    inv = offline_access_explain_report(
        person_query="Definitely Not A Real Person 12345", db=db
    )
    assert inv.status == "failed"


def test_access_explain_does_not_substitute_unrelated_grants(db: ReadOnlyDB) -> None:
    inv = offline_access_explain_report(
        person_query="Ariel Chen",
        resource_hint="DefinitelyNoSuchResource",
        db=db,
    )
    assert inv.status == "needs_review"
    assert inv.verified is False
    assert "Resource not found" in inv.report_md
    assert "Snowflake" not in inv.report_md
