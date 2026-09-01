"""Tests for the Planner → Executor → Verifier loop, with a stubbed model.

We simulate an LLM by scripted responses so no API key is required.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent.core.config import Settings
from agent.core.db import ReadOnlyDB
from agent.llm.model_client import ChatResult, StubModelClient
from agent.core.run_store import FileRunStore
from agent.core.schemas import EvidenceRef, Investigation
from agent.llm.runtime import (
    Agent,
    InvestigationDeadlineExceeded,
    _derive_access_temporal_facts,
    _extract_citations,
    _extract_reference_mentions,
    _find_incomplete_access_chains,
)

DB_PATH = Path(__file__).resolve().parents[1] / "input_data" / "access_snapshot.sqlite"


@pytest.fixture(scope="module")
def db() -> ReadOnlyDB:
    return ReadOnlyDB(str(DB_PATH))


def _make_client(script):
    """script is a list of responses; each call pops the next."""
    remaining = list(script)

    def responder(model, messages, tools):
        assert remaining, "stub script exhausted"
        return remaining.pop(0)

    return StubModelClient(responder)


def test_citation_extraction_ignores_bad_kinds() -> None:
    text = (
        "Foo `person:per_1` bar `nonsense:x_1` baz `audit_event:evt_9` "
        "and also `person:per_1` again."
    )
    refs = _extract_citations(text)
    kinds = {r.kind for r in refs}
    assert "person" in kinds
    assert "audit_event" in kinds
    assert "nonsense" not in kinds
    # dedup
    assert len([r for r in refs if r.kind == "person"]) == 1
    assert _extract_reference_mentions("See (person:per_1) without backticks.") == [
        EvidenceRef(kind="person", id="per_1")
    ]


def test_access_temporal_comparisons_are_computed_not_inferred(
    db: ReadOnlyDB,
) -> None:
    facts = _derive_access_temporal_facts(
        "per_001945",
        [
            {
                "resource_id": "app_0001",
                "resource_name": "Google Workspace",
                "granted_at": "2024-09-15T12:00:00Z",
                "effective": True,
                "evidence": [
                    {"kind": "idp_app_assignment", "id": "idpaa_000001"}
                ],
            }
        ],
        db,
    )

    assert facts == [
        {
            "person_id": "per_001945",
            "employment_status": "ended",
            "end_date": "2026-08-03",
            "snapshot_time": "2026-08-15T12:00:00Z",
            "snapshot_after_end_date": True,
            "resource_id": "app_0001",
            "resource_name": "Google Workspace",
            "granted_at": "2024-09-15T12:00:00Z",
            "grant_after_end_date": False,
            "effective_at_snapshot": True,
            "effective_after_end_date": True,
            "evidence": ["idp_app_assignment:idpaa_000001"],
        }
    ]


def test_access_chain_completeness_requires_every_observed_edge() -> None:
    facts = [
        {
            "resource_id": "app_1",
            "effective_at_snapshot": True,
            "evidence": [
                "person:p1",
                "idp_account:a1",
                "idp_group_membership:m1",
                "idp_app_assignment:x1",
                "application:app_1",
            ],
        }
    ]
    incomplete = [EvidenceRef(kind="application", id="app_1")]
    complete = [
        EvidenceRef(kind=key.split(":", 1)[0], id=key.split(":", 1)[1])
        for key in facts[0]["evidence"]
    ]

    gaps = _find_incomplete_access_chains(incomplete, facts)
    assert gaps[0]["resource_id"] == "app_1"
    assert "idp_app_assignment:x1" in gaps[0]["missing_from_shortest_observed_chain"]
    assert _find_incomplete_access_chains(complete, facts) == []
    assert _find_incomplete_access_chains(incomplete, []) == []


def test_stubbed_agent_verifies_good_citations(db: ReadOnlyDB, tmp_path) -> None:
    # pick a real person to cite
    row = db.one("SELECT person_id FROM people LIMIT 1")
    pid = row["person_id"]

    planner_resp = ChatResult(
        content=json.dumps(
            {
                "question_restatement": "Test",
                "strategy": "cite one real ID",
                "steps": [],
                "stop_when": "done",
            }
        ),
        tool_calls=[],
        tokens_in=10,
        tokens_out=10,
        model="stub",
        raw={},
    )
    executor_resp = ChatResult(
        content="",
        tool_calls=[
            {
                "id": "call_find_person",
                "type": "function",
                "function": {
                    "name": "find_person",
                    "arguments": json.dumps({"query": pid, "limit": 1}),
                },
            }
        ],
        tokens_in=10,
        tokens_out=10,
        model="stub",
        raw={},
    )
    final_resp = ChatResult(
        content=(
            "# Investigation: Test\n\n## Summary\nSample.\n\n"
            "## Findings\n### Finding 1 — sample (severity: info)\n"
            f"Claim.\n**Evidence:**\n- `person:{pid}`\n\n"
            "## Gaps / Uncertainty\n- none\n\n"
            "## Recommended actions (advisory only)\n- none\n"
        ),
        tool_calls=[],
        tokens_in=10,
        tokens_out=10,
        model="stub",
        raw={},
    )
    verifier_resp = ChatResult(
        content=json.dumps(
            {
                "verified": True,
                "report_md": final_resp.content,
                "notes": ["Claim matches the observed person row."],
            }
        ),
        tool_calls=[],
        tokens_in=10,
        tokens_out=10,
        model="stub",
        raw={},
    )
    client = _make_client([planner_resp, executor_resp, final_resp, verifier_resp])
    settings = Settings(agent_run_store_dir=str(tmp_path), openrouter_api_key="stub")
    agent = Agent(
        client=client,
        db=db,
        settings=settings,
        run_store=FileRunStore(tmp_path),
    )
    inv = agent.investigate("Does this person exist?")
    assert inv.status == "succeeded"
    assert inv.verified is True
    assert not inv.unresolved_evidence


def test_stubbed_agent_flags_hallucinated_id(db: ReadOnlyDB, tmp_path) -> None:
    planner_resp = ChatResult(
        content=json.dumps({"steps": []}),
        tool_calls=[],
        tokens_in=1, tokens_out=1, model="stub", raw={},
    )
    executor_resp = ChatResult(
        content=(
            "# Investigation: Bogus\n\n## Summary\nMade up.\n\n"
            "## Findings\n### Finding 1 — fabricated (severity: high)\n"
            "Made up.\n**Evidence:**\n- `person:per_FAKE_9999`\n\n"
            "## Gaps / Uncertainty\n- n/a\n\n"
            "## Recommended actions (advisory only)\n- n/a\n"
        ),
        tool_calls=[], tokens_in=1, tokens_out=1, model="stub", raw={},
    )
    # verifier LLM will be called because a citation is unresolved
    verifier_resp = ChatResult(
        content=json.dumps(
            {
                "verified": False,
                "report_md": (
                    "# Investigation: Bogus (revised by Verifier)\n\n"
                    "## Gaps / Uncertainty\n- Cited person does not exist.\n"
                ),
                "notes": ["Unresolved citation."],
            }
        ),
        tool_calls=[], tokens_in=1, tokens_out=1, model="stub", raw={},
    )
    client = _make_client([planner_resp, executor_resp, verifier_resp])
    settings = Settings(agent_run_store_dir=str(tmp_path), openrouter_api_key="stub")
    agent = Agent(
        client=client, db=db, settings=settings, run_store=FileRunStore(tmp_path)
    )
    inv = agent.investigate("test hallucination detection")
    assert inv.status == "needs_review"
    assert not inv.verified
    assert any(r.id == "per_FAKE_9999" for r in inv.unresolved_evidence)


def test_real_but_unobserved_citation_is_not_verified(db: ReadOnlyDB, tmp_path) -> None:
    row = db.one("SELECT person_id FROM people LIMIT 1")
    pid = row["person_id"]
    verifier_resp = ChatResult(
        content=json.dumps(
            {
                "verified": True,
                "report_md": "# Investigation\n\nThe model incorrectly approved it.",
                "notes": ["Provenance must still fail closed."],
            }
        ),
        tool_calls=[], tokens_in=1, tokens_out=1, model="stub", raw={},
    )
    agent = Agent(
        client=_make_client([verifier_resp]),
        db=db,
        settings=Settings(agent_run_store_dir=str(tmp_path), openrouter_api_key="stub"),
        run_store=FileRunStore(tmp_path),
    )
    inv = Investigation(
        id="test",
        created_at="2026-08-15T12:00:00Z",
        snapshot_time=db.snapshot_time_iso,
        dataset_version=db.dataset_version,
        question="fabricated claim",
    )
    _, verified, unresolved, unobserved = agent._verify(
        inv, f"Ariel owns payments. `person:{pid}`"
    )
    assert not verified
    assert not unresolved
    assert unobserved == [EvidenceRef(kind="person", id=pid)]


def test_verifier_rewrite_cannot_introduce_unchecked_citation(
    db: ReadOnlyDB, tmp_path
) -> None:
    pid = db.one("SELECT person_id FROM people LIMIT 1")["person_id"]
    fake = "per_FAKE_AFTER_REWRITE"
    verifier_resp = ChatResult(
        content=json.dumps(
            {
                "verified": True,
                "report_md": f"# Rewritten\n\nUnsupported `person:{fake}`",
                "notes": [],
            }
        ),
        tool_calls=[],
        tokens_in=1,
        tokens_out=1,
        model="stub",
        raw={},
    )
    agent = Agent(
        client=_make_client([verifier_resp]),
        db=db,
        settings=Settings(agent_run_store_dir=str(tmp_path), openrouter_api_key="stub"),
        run_store=FileRunStore(tmp_path),
    )
    inv = Investigation(
        id="rewrite-test",
        created_at="2026-08-15T12:00:00Z",
        snapshot_time=db.snapshot_time_iso,
        dataset_version=db.dataset_version,
        question="test rewrite",
        observed_evidence=[EvidenceRef(kind="person", id=pid)],
    )

    report, verified, unresolved, unobserved = agent._verify(
        inv, f"Original supported claim. `person:{pid}`"
    )

    assert not verified
    assert f"person:{fake}" in report
    assert EvidenceRef(kind="person", id=fake) in unresolved
    assert EvidenceRef(kind="person", id=fake) in unobserved


def test_verifier_can_add_observed_citation_from_evidence_packet(
    db: ReadOnlyDB, tmp_path
) -> None:
    rows = db.execute("SELECT person_id FROM people ORDER BY person_id LIMIT 2")
    first, second = rows[0]["person_id"], rows[1]["person_id"]
    rewritten = f"# Rewritten\n\n`person:{first}` and `person:{second}` are observed."
    verifier_resp = ChatResult(
        content=json.dumps(
            {"verified": True, "report_md": rewritten, "notes": ["Added observed evidence."]}
        ),
        tool_calls=[], tokens_in=1, tokens_out=1, model="stub", raw={},
    )
    agent = Agent(
        client=_make_client([verifier_resp]),
        db=db,
        settings=Settings(agent_run_store_dir=str(tmp_path), openrouter_api_key="stub"),
        run_store=FileRunStore(tmp_path),
    )
    inv = Investigation(
        id="observed-rewrite-test",
        created_at="2026-08-15T12:00:00Z",
        snapshot_time=db.snapshot_time_iso,
        dataset_version=db.dataset_version,
        question="test observed rewrite",
        observed_evidence=[
            EvidenceRef(kind="person", id=first),
            EvidenceRef(kind="person", id=second),
        ],
    )

    report, verified, unresolved, unobserved = agent._verify(
        inv, f"Original `person:{first}`"
    )

    assert verified
    assert f"person:{second}" in report
    assert not unresolved
    assert not unobserved


def test_corrected_report_gets_separate_semantic_recheck(
    db: ReadOnlyDB, tmp_path
) -> None:
    pid = db.one("SELECT person_id FROM people LIMIT 1")["person_id"]
    draft = f"Unsupported wording. `person:{pid}`"
    corrected = f"# Corrected\n\nSupported wording. `person:{pid}`"
    repair = ChatResult(
        content=json.dumps(
            {"verified": False, "report_md": corrected, "notes": ["Repaired draft."]}
        ),
        tool_calls=[], tokens_in=1, tokens_out=1, model="stub", raw={},
    )
    adjudication = ChatResult(
        content=json.dumps(
            {"verified": True, "report_md": corrected, "notes": ["Candidate passes."]}
        ),
        tool_calls=[], tokens_in=1, tokens_out=1, model="stub", raw={},
    )
    agent = Agent(
        client=_make_client([repair, adjudication]),
        db=db,
        settings=Settings(agent_run_store_dir=str(tmp_path), openrouter_api_key="stub"),
        run_store=FileRunStore(tmp_path),
    )
    inv = Investigation(
        id="recheck-test",
        created_at="2026-08-15T12:00:00Z",
        snapshot_time=db.snapshot_time_iso,
        dataset_version=db.dataset_version,
        question="test recheck",
        observed_evidence=[EvidenceRef(kind="person", id=pid)],
    )

    report, verified, unresolved, unobserved = agent._verify(inv, draft)

    assert verified
    assert corrected in report
    assert not unresolved
    assert not unobserved
    assert any(step.get("kind") == "llm_call_recheck" for step in inv.trace)


def test_executor_never_executes_more_than_tool_budget(
    db: ReadOnlyDB, tmp_path
) -> None:
    pid = db.one("SELECT person_id FROM people LIMIT 1")["person_id"]
    tool_calls = [
        {
            "id": f"call_{index}",
            "type": "function",
            "function": {
                "name": "find_person",
                "arguments": json.dumps({"query": pid}),
            },
        }
        for index in range(2)
    ]
    first = ChatResult(
        content="",
        tool_calls=tool_calls,
        tokens_in=1,
        tokens_out=1,
        model="stub",
        raw={},
    )
    final = ChatResult(
        content="# Final",
        tool_calls=[],
        tokens_in=1,
        tokens_out=1,
        model="stub",
        raw={},
    )
    agent = Agent(
        client=_make_client([first, final]),
        db=db,
        settings=Settings(agent_run_store_dir=str(tmp_path), openrouter_api_key="stub"),
        run_store=FileRunStore(tmp_path),
    )
    inv = Investigation(
        id="budget-test",
        created_at="2026-08-15T12:00:00Z",
        snapshot_time=db.snapshot_time_iso,
        dataset_version=db.dataset_version,
        question="test budget",
    )

    assert agent._execute(inv, {}, budget=1) == "# Final"
    executed = [
        step
        for step in inv.trace
        if step.get("kind") == "tool_call" and step.get("executed")
    ]
    rejected = [
        step
        for step in inv.trace
        if step.get("kind") == "tool_call" and not step.get("executed")
    ]
    assert len(executed) == 1
    assert len(rejected) == 1
    assert "budget exhausted" in rejected[0]["error"]


def test_expired_deadline_fails_before_model_call(db: ReadOnlyDB, tmp_path) -> None:
    agent = Agent(
        client=_make_client([]),
        db=db,
        settings=Settings(agent_run_store_dir=str(tmp_path), openrouter_api_key="stub"),
        run_store=FileRunStore(tmp_path),
    )
    inv = Investigation(
        id="deadline-test",
        created_at="2026-08-15T12:00:00Z",
        snapshot_time=db.snapshot_time_iso,
        dataset_version=db.dataset_version,
        question="test deadline",
    )

    with pytest.raises(InvestigationDeadlineExceeded):
        agent._plan(inv, deadline=time.monotonic() - 0.01)
