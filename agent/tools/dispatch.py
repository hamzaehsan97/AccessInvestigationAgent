"""Dispatch LLM tool calls (JSON args) to the typed Python tools."""
from __future__ import annotations

import json
import time
from typing import Any

from agent.core.db import ReadOnlyDB
from agent.core.schemas import (
    EvidenceRef,
    ExpandGroupIn,
    FindDisagreementsIn,
    FindPersonIn,
    GetAuditTrailIn,
    ListAccessIn,
    ResolveEvidenceIn,
    SafeSelectIn,
    ToolResult,
    WhoHasAccessIn,
)
from agent.tools.access import (
    expand_group,
    find_disagreements,
    find_person,
    get_audit_trail,
    get_person_profile,
    list_access_for_person,
    resolve_evidence,
    safe_select,
    who_has_access_to,
)


def dispatch_tool_call(name: str, arguments: dict, db: ReadOnlyDB) -> tuple[ToolResult, int]:
    """Run a tool. Returns (result, latency_ms). Raises on schema/param errors."""
    t0 = time.time()
    if name == "find_person":
        res = find_person(FindPersonIn(**arguments), db=db)
    elif name == "get_person_profile":
        pid = arguments.get("person_id")
        if not pid:
            raise ValueError("person_id required")
        res = get_person_profile(pid, db=db)
    elif name == "list_access_for_person":
        res = list_access_for_person(ListAccessIn(**arguments), db=db)
    elif name == "expand_group":
        res = expand_group(ExpandGroupIn(**arguments), db=db)
    elif name == "who_has_access_to":
        res = who_has_access_to(WhoHasAccessIn(**arguments), db=db)
    elif name == "get_audit_trail":
        res = get_audit_trail(GetAuditTrailIn(**arguments), db=db)
    elif name == "find_disagreements":
        res = find_disagreements(FindDisagreementsIn(**arguments), db=db)
    elif name == "safe_select":
        res = safe_select(SafeSelectIn(**arguments), db=db)
    elif name == "resolve_evidence":
        refs = [EvidenceRef(**r) for r in arguments.get("refs", [])]
        res = resolve_evidence(ResolveEvidenceIn(refs=refs), db=db)
    else:
        raise ValueError(f"unknown tool: {name}")
    dt_ms = int((time.time() - t0) * 1000)
    return res, dt_ms


def summarize_result(res: ToolResult, max_bytes: int = 8000) -> str:
    """Return a compact JSON string small enough for a single Executor turn."""
    payload = {
        "data": res.data,
        "evidence": [e.model_dump() for e in res.evidence],
        "truncated": res.truncated,
        "notes": res.notes,
    }
    def encode(value: dict) -> str:
        return json.dumps(value, default=str, separators=(",", ":"))

    def fits(value: dict) -> bool:
        return len(encode(value).encode("utf-8")) <= max_bytes

    s = encode(payload)
    if fits(payload):
        return s

    original_count = len(res.data) if isinstance(res.data, list) else None
    payload["truncated"] = True
    payload["notes"] = list(res.notes) + [
        "Result was structurally truncated to fit the executor context."
    ]

    # Reduce list-shaped data progressively while preserving valid JSON.
    if isinstance(res.data, list):
        for count in (20, 10, 5, 2, 1, 0):
            payload["data"] = res.data[:count]
            payload["notes"][-1] = (
                f"Result data truncated from {original_count} to {min(count, original_count)} items."
            )
            for evidence_count in (40, 20, 10, 5, 0):
                payload["evidence"] = [e.model_dump() for e in res.evidence[:evidence_count]]
                if fits(payload):
                    return encode(payload)
    elif isinstance(res.data, dict):
        # Preserve scalar metadata and progressively trim nested lists.
        compact_data: dict = {}
        for key, value in res.data.items():
            compact_data[key] = value[:10] if isinstance(value, list) else value
        payload["data"] = compact_data
        for list_count in (10, 5, 2, 1, 0):
            payload["data"] = {
                key: (value[:list_count] if isinstance(value, list) else value)
                for key, value in res.data.items()
            }
            for evidence_count in (40, 20, 10, 5, 0):
                payload["evidence"] = [e.model_dump() for e in res.evidence[:evidence_count]]
                if fits(payload):
                    return encode(payload)

    # Last-resort payload remains valid JSON and explicitly tells the model to
    # retry with narrower filters.
    fallback = {
        "data": None,
        "evidence": [],
        "truncated": True,
        "notes": ["Result exceeded context limit; retry with narrower filters."],
    }
    return encode(fallback)
