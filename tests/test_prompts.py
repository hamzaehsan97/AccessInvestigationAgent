"""Prompt contract regressions for evidence and temporal reasoning."""
from agent.llm.prompts import executor_system, verifier_system


def test_executor_distinguishes_grant_time_from_effective_access() -> None:
    prompt = executor_system("2026-08-15T12:00:00Z", "test", 5, 30)

    assert "`effective=true` as effective at the stated snapshot" in prompt
    assert "Do not claim access was granted after termination" in prompt
    assert "`person`, linked account, grant/permission, and resource" in prompt


def test_verifier_rechecks_temporal_language_after_rewrite() -> None:
    prompt = verifier_system("2026-08-15T12:00:00Z", "test")

    assert "A grant created\n    before end_date can remain effective after termination" in prompt
    assert "After rewriting, evaluate the corrected report" in prompt
    assert "`grant_after_end_date=false` forbids saying" in prompt
