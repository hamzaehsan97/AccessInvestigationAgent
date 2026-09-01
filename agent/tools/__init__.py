"""Typed tool SDK exposed to the LLM."""
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
from agent.tools.dispatch import dispatch_tool_call, summarize_result
from agent.tools.scanners import available_scanners, run_scanner
from agent.tools.specs import TOOL_SPECS

__all__ = [
    "expand_group",
    "find_disagreements",
    "find_person",
    "get_audit_trail",
    "get_person_profile",
    "list_access_for_person",
    "resolve_evidence",
    "safe_select",
    "who_has_access_to",
    "dispatch_tool_call",
    "summarize_result",
    "available_scanners",
    "run_scanner",
    "TOOL_SPECS",
]
