"""OpenAI-format tool specifications advertised to the LLM.

We keep these hand-authored (rather than auto-generated from Pydantic) so we
can control the prose the LLM reads and keep parameters short.
"""
from __future__ import annotations


TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "find_person",
            "description": (
                "Resolve a person by name / email / account handle / person_id. "
                "Returns ranked hits with person_id, department, title, employment_status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_person_profile",
            "description": (
                "HR record + linked IdP/Workspace/GitHub accounts + devices + counts."
            ),
            "parameters": {
                "type": "object",
                "properties": {"person_id": {"type": "string"}},
                "required": ["person_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_access_for_person",
            "description": (
                "Every effective access grant (IdP apps direct/group/nested, "
                "app-reported access, GitHub team + collab repos, Drive perms, "
                "OAuth grants). Each grant carries the source chain (path) and "
                "supporting evidence IDs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "person_id": {"type": "string"},
                    "include_revoked": {"type": "boolean", "default": False},
                    "resource_hint": {
                        "type": "string",
                        "description": (
                            "Optional resource name or ID filter. Use this for "
                            "resource-specific questions; zero returned grants is "
                            "a supported negative result when the resource evidence exists."
                        ),
                    },
                    "systems": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["idp", "workspace", "github", "drive", "oauth"],
                        },
                    },
                },
                "required": ["person_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expand_group",
            "description": "Transitive membership of an IdP/Workspace group (or containers).",
            "parameters": {
                "type": "object",
                "properties": {
                    "system": {"type": "string", "enum": ["idp", "workspace"]},
                    "group_id": {"type": "string"},
                    "direction": {
                        "type": "string",
                        "enum": ["members", "containers"],
                        "default": "members",
                    },
                    "max_depth": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 32,
                        "default": 8,
                    },
                },
                "required": ["system", "group_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "who_has_access_to",
            "description": (
                "Reverse lookup: given a resource (application / github_repository / "
                "drive_resource) return every effective grantee with the grant chain."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_kind": {
                        "type": "string",
                        "enum": ["application", "github_repository", "drive_resource"],
                    },
                    "resource_id": {"type": "string"},
                    "include_external": {"type": "boolean", "default": True},
                },
                "required": ["resource_kind", "resource_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_audit_trail",
            "description": (
                "Paginated slice of audit_events for a target_id or actor_id. "
                "Filter by system/event_type/date range."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "actor_id": {"type": "string"},
                    "since": {"type": "string", "description": "ISO 8601"},
                    "until": {"type": "string", "description": "ISO 8601"},
                    "systems": {"type": "array", "items": {"type": "string"}},
                    "event_types": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "default": 50},
                    "cursor": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_disagreements",
            "description": (
                "Run one or more deterministic scanners: offboarding_leakage, "
                "orphan_accounts_recent_login, mfa_policy_vs_enrollment, "
                "external_share_on_restricted_drive, unretired_device_for_ended_employee, "
                "idp_active_but_app_access_revoked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scanners": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "offboarding_leakage",
                                "orphan_accounts_recent_login",
                                "mfa_policy_vs_enrollment",
                                "external_share_on_restricted_drive",
                                "unretired_device_for_ended_employee",
                                "idp_active_but_app_access_revoked",
                            ],
                        },
                    },
                    "limit_per_scanner": {"type": "integer", "default": 25},
                },
                "required": ["scanners"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "safe_select",
            "description": (
                "Narrow escape hatch: run a single read-only SELECT. LIMIT is capped at 50. "
                "INSERT/UPDATE/DELETE/DROP/ATTACH/PRAGMA and multi-statement SQL are rejected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string"},
                    "row_limit": {"type": "integer", "default": 50},
                },
                "required": ["sql"],
            },
        },
    },
]
