"""Pydantic models used by tools, agent runtime, and reports.

These models are the wire format between the LLM and the deterministic layer.
They are intentionally small and JSON-friendly. Every tool output carries an
``evidence`` list of ``EvidenceRef`` so the Verifier can re-check citations.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ---------- shared ----------

EvidenceKind = Literal[
    "person",
    "idp_account",
    "workspace_account",
    "github_account",
    "device",
    "idp_group",
    "workspace_group",
    "github_team",
    "github_repository",
    "application",
    "drive_resource",
    "drive_permission",
    "idp_app_assignment",
    "application_user_access",
    "idp_group_membership",
    "workspace_group_membership",
    "github_org_membership",
    "github_team_membership",
    "github_team_repo_permission",
    "github_repo_collaborator",
    "workspace_oauth_grant",
    "audit_event",
]


class EvidenceRef(BaseModel):
    kind: EvidenceKind
    id: str

    def key(self) -> str:
        return f"{self.kind}:{self.id}"


class ToolResult(BaseModel):
    data: Any
    evidence: list[EvidenceRef] = Field(default_factory=list)
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)


# ---------- find_person ----------


class FindPersonIn(BaseModel):
    query: str
    limit: int = Field(default=5, ge=1, le=50)


class PersonHit(BaseModel):
    person_id: str
    full_name: str
    primary_email: str
    department: Optional[str] = None
    title: Optional[str] = None
    employment_status: str
    manager_person_id: Optional[str] = None
    end_date: Optional[str] = None
    score: float
    matched_on: list[str]


# ---------- get_person_profile ----------


class LinkedAccount(BaseModel):
    system: Literal["idp", "workspace", "github"]
    account_id: str
    handle: str
    status: str
    last_login_at: Optional[str] = None


class DeviceSummary(BaseModel):
    device_id: str
    platform: str
    ownership: str
    compliance_status: str
    last_check_in_at: Optional[str] = None
    retired_at: Optional[str] = None


class PersonProfile(BaseModel):
    person: dict
    accounts: list[LinkedAccount]
    devices: list[DeviceSummary]
    counts: dict


# ---------- list_access_for_person ----------


class AccessGrant(BaseModel):
    system: Literal["idp", "workspace", "github", "drive", "oauth"]
    resource_kind: Literal[
        "application", "group", "repo", "drive_resource", "oauth_scope"
    ]
    resource_id: str
    resource_name: str
    role: Optional[str] = None
    granted_at: Optional[str] = None
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None
    effective: bool
    source: Literal[
        "direct", "group", "nested_group", "scim", "collab", "team", "oauth_grant"
    ]
    path: list[EvidenceRef]
    evidence: list[EvidenceRef]


class ListAccessIn(BaseModel):
    person_id: str
    include_revoked: bool = False
    systems: Optional[list[str]] = None
    resource_hint: Optional[str] = None


# ---------- expand_group ----------


class ExpandGroupIn(BaseModel):
    system: Literal["idp", "workspace"]
    group_id: str
    direction: Literal["members", "containers"] = "members"
    max_depth: int = Field(default=8, ge=1, le=32)


class GroupExpansion(BaseModel):
    root: EvidenceRef
    direct_members: list[EvidenceRef]
    transitive_members: list[EvidenceRef]
    depth_reached: int
    cycles_detected: list[str] = Field(default_factory=list)


# ---------- who_has_access_to ----------


class WhoHasAccessIn(BaseModel):
    resource_kind: Literal["application", "github_repository", "drive_resource"]
    resource_id: str
    include_external: bool = True


class Grantee(BaseModel):
    person_id: Optional[str] = None
    person_name: Optional[str] = None
    account: Optional[EvidenceRef] = None
    external_principal: Optional[str] = None
    role: Optional[str] = None
    source: str
    path: list[EvidenceRef]
    evidence: list[EvidenceRef]


# ---------- get_audit_trail ----------


class GetAuditTrailIn(BaseModel):
    target_id: Optional[str] = None
    actor_id: Optional[str] = None
    since: Optional[str] = None
    until: Optional[str] = None
    systems: Optional[list[str]] = None
    event_types: Optional[list[str]] = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Optional[str] = None


class AuditEventOut(BaseModel):
    event_id: str
    occurred_at: str
    system: str
    event_type: str
    actor_type: str
    actor_id: Optional[str] = None
    target_type: str
    target_id: Optional[str] = None
    outcome: str
    source: str
    ip_address: Optional[str] = None
    correlation_id: Optional[str] = None
    details: dict


# ---------- find_disagreements ----------

ScannerName = Literal[
    "offboarding_leakage",
    "orphan_accounts_recent_login",
    "mfa_policy_vs_enrollment",
    "external_share_on_restricted_drive",
    "unretired_device_for_ended_employee",
    "idp_active_but_app_access_revoked",
]


class FindDisagreementsIn(BaseModel):
    scanners: list[ScannerName]
    limit_per_scanner: int = Field(default=25, ge=1, le=100)


class Finding(BaseModel):
    scanner: str
    severity: Literal["info", "low", "medium", "high", "critical"]
    subject: EvidenceRef
    summary: str
    evidence: list[EvidenceRef]


# ---------- safe_select ----------


class SafeSelectIn(BaseModel):
    sql: str
    row_limit: int = Field(default=50, ge=1, le=100)


class SafeSelectOut(BaseModel):
    columns: list[str]
    rows: list[list]


# ---------- resolve_evidence ----------


class ResolveEvidenceIn(BaseModel):
    refs: list[EvidenceRef]


class ResolvedEvidence(BaseModel):
    ref: EvidenceRef
    exists: bool
    summary: str
    row: Optional[dict] = None


# ---------- investigation ----------


class ToolCallStep(BaseModel):
    role: Literal["planner", "executor", "verifier"]
    tool: str
    arguments: dict
    result_digest: str
    truncated: bool = False
    latency_ms: int = 0
    error: Optional[str] = None


class LLMCallStep(BaseModel):
    role: Literal["planner", "executor", "verifier"]
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    content_preview: Optional[str] = None


class Investigation(BaseModel):
    id: str
    created_at: str
    snapshot_time: str
    dataset_version: str
    question: str
    requester: Optional[str] = None
    playbook: Optional[str] = None
    parameters: dict = Field(default_factory=dict)
    trace: list[dict] = Field(default_factory=list)
    report_md: str = ""
    verified: bool = False
    verifier_notes: list[str] = Field(default_factory=list)
    observed_evidence: list[EvidenceRef] = Field(default_factory=list)
    unresolved_evidence: list[EvidenceRef] = Field(default_factory=list)
    unobserved_evidence: list[EvidenceRef] = Field(default_factory=list)
    status: Literal["running", "succeeded", "failed", "needs_review"] = "running"
    metrics: dict = Field(default_factory=dict)
