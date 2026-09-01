# Access Investigation Agent — Low-Level Design

**Status:** Implemented take-home baseline plus explicitly labeled production roadmap
**Author:** Hamza Ehsan
**Last updated:** 2026-09-01
**Scope:** Take-home implementation + credible path to production
**Constraint:** Original take-home timebox and a 60-minute review

---

## 0. Executive Summary

We are building an LLM-driven **Access Investigation Agent** for a Security/IAM team at a ~2,000-person company. The agent answers questions like *"Does this ex-employee still have production access?"* and *"Why does person X have access to application Y?"* by querying a read-only snapshot of HR, IdP, Google Workspace, GitHub, and MDM data plus six months of audit events, and by returning a written conclusion whose every claim is backed by explicit record and event IDs.

The design addresses two operating contexts:

- **Local evaluation**, where the agent runs from a clean checkout and supports live investigation questions during a short review.
- **A hypothetical production owner** at a real security team, who needs a service that is safe, auditable, scalable, and portable to their infrastructure (AWS is the assumed home).

The submitted artifact is a small Python service (CLI + FastAPI + Docker) with a typed tool SDK and a **Planner → Executor → Verifier** loop against OpenRouter. A golden-set evaluation harness, managed persistence, streaming, and production observability are roadmap items, not shipped components. The interfaces are intentionally shaped to make a later migration to Fargate, Kubernetes, or Bedrock AgentCore incremental; the included Terraform is an illustrative sketch rather than a deployable stack.

---

## 1. Problem Statement and Non-Goals

### 1.1 Problem
Security teams routinely need to answer high-stakes access questions where the truth is spread across multiple systems, none of which is complete on its own. Sources disagree; deprovisioning is unreliable; nested groups and syncs create indirect grants; audit logs prove events happened without always proving who caused them. Analysts today do this by hand, joining SQL queries and Splunk searches. An LLM agent with the right tools can compress that work while making its reasoning auditable.

### 1.2 Goals
1. Answer natural-language investigation questions against the supplied snapshot.
2. Produce a structured **Investigation Report** whose findings cite the exact `event_id` and record IDs that support them.
3. Explicitly separate *facts observed in data* from *inferences drawn by the model*, and flag gaps in the evidence.
4. Recommend remediation actions but never execute them.
5. Run from a clean checkout in under 60 seconds, with boundaries that make a later production migration incremental and testable.

### 1.3 Non-goals
1. A polished web UI. A CLI plus a thin FastAPI surface is enough for the take-home.
2. Ingesting live data from HR/IdP/Workspace/GitHub/MDM. The snapshot stands in for the production **Access Graph** store.
3. Executing remediations (revoking access, disabling accounts, retiring devices). The agent emits *proposed* actions only.
4. Learning across investigations (long-term memory). Every run is self-contained; long-term memory is on the production roadmap.

### 1.4 Constraints
- Read-only access to the supplied SQLite DB is mandatory.
- The OpenRouter API key must come from env/config, must not be committed, and reviewers will use their own.
- 4-hour implementation budget.

---

## 2. Users, Use Cases, and Sample Investigations

### 2.1 Personas
- **Security analyst** (primary) — asks ad-hoc questions during incident response and quarterly reviews.
- **IAM lead** — schedules recurring sweeps, reviews reports before they become tickets.
- **Auditor** — needs reproducible, signed evidence for compliance frameworks (SOC2, ISO 27001).

### 2.2 Investigations we will support end-to-end in the take-home
1. **Offboarding leakage** — For each `employment_status='ended'` person, enumerate surviving access (IdP app assignments, application-reported access, GitHub team/repo perms, Drive perms) with the exact grant chain and supporting event IDs from the last 6 months.
2. **Access explain** — *"Why does person X have application Y?"* Trace the full grant path: direct IdP assignment, or IdP group → possibly nested groups → assignment, or SCIM-provisioned in the application itself. Cite each hop.

### 2.3 Investigations the agent can *also* answer (via general tools) but that are not the demo focus
- Orphan accounts with recent logins (`person_id IS NULL` on `idp_accounts`, `workspace_accounts`, `github_accounts` with recent `user.login`/`application.authentication` events).
- MFA policy vs. enrollment vs. observed-sign-in disagreements.
- External Drive shares (`principal_type IN ('domain','external')`) on `restricted`/`confidential` `drive_resources`.
- Unretired devices for ended employees.
- Privileged role drift (admin roles granted directly and not through the standard IdP group).

---

## 3. High-Level Architecture

```
+----------------------+       +---------------------------------+
|   CLI  |  FastAPI    | <---> |  Agent Runtime (Python 3.9+)  |
+----------------------+       |   Planner → Executor → Verifier |
                               +----------------+----------------+
                                                |
                                                v
                             +----------------------------------+
                             |    Typed Tool SDK (Pydantic)     |
                             |  find_person, list_access_for_   |
                             |  person, expand_group, who_has_  |
                             |  access_to, get_audit_trail,     |
                             |  find_disagreements, safe_select |
                             +----------------+-----------------+
                                              |
                                              v
                             +----------------------------------+
                             |  Read-only Database Adapter      |
                             |  (SQLite today; Postgres later)  |
                             |  URI mode=ro + query_only PRAGMA |
                             |  + sqlite3 authorizer callback   |
                             +----------------+-----------------+
                                              |
                                              v
                             +----------------------------------+
                             |  access_snapshot.sqlite (immut.) |
                             +----------------------------------+

  Cross-cutting:
    - RunStore (JSON files today; S3 later): investigation records
    - ModelClient (OpenRouter today; Bedrock/Azure/OpenAI later)
    - Investigation trace + JSON FileRunStore (structured telemetry is roadmap)
```

### 3.1 Why three agent roles instead of one
- A single ReAct loop is easy to build but tends to over-trust its own reasoning and cite made-up IDs.
- Splitting the loop into **Planner** (writes a JSON plan of tool calls), **Executor** (runs the plan, adapts on new evidence), and **Verifier** (re-fetches each cited ID and confirms it supports the claim before the report is emitted) makes evidence discipline a runtime property, not a prompt-engineering hope.
- Cost is bounded by call/time limits and narrow verifier context; the default uses the same configurable model for all three roles.

### 3.2 Why a typed tool SDK instead of "just give the model SQL"
- Free-form SQL is a footgun: the model will happily hallucinate table names, forget the `revoked_at IS NULL AND (expires_at IS NULL OR expires_at > snapshot_time)` filter, and produce plausible but wrong joins across the multi-typed `member_id`/`principal_id` columns.
- Typed tools encode the domain rules once (nested group expansion, revoked/expired filters, `member_type` disambiguation) so the model can't get them wrong.
- We keep a *narrow* SQL escape hatch (`safe_select`) for questions the pre-baked tools don't cover, but it is SELECT-only and guarded by the SQLite authorizer callback.

---

## 4. Data Model and Snapshot Handling

### 4.1 Snapshot invariants (from `SCHEMA.md`)
- `dataset_metadata.snapshot_time = 2026-08-15T12:00:00Z`. All "is it active?" comparisons use this constant, not `now()`.
- A relationship is **effective** iff `revoked_at IS NULL AND (expires_at IS NULL OR expires_at > snapshot_time)`.
- `people.employment_status ∈ {active, ended, leave, prehire}` is authoritative for HR state; account/device state is separate and can disagree.
- `member_type` / `principal_type` disambiguate the polymorphic ID columns; nested groups exist and can be deeper than one level.
- Audit logs are incomplete; an event may prove *something happened* without proving *who* caused it (`actor_type='unknown'`).

### 4.2 Materialized helper: nested group closure
Computed on connection open (small enough — 136 IdP groups, 60 Workspace groups). Two in-memory maps:
- `idp_group_members[group_id] = set(effective_account_ids)` — breadth-first walk over `idp_group_memberships` with `member_type='group'`, cycle-safe with no arbitrary depth cutoff.
- `workspace_group_members[group_id] = set(effective_account_ids)` — same pattern.

This closure is used by `list_access_for_person`, `who_has_access_to`, and `expand_group`, guaranteeing consistent handling of nested groups. The alternative (recursive CTEs at query time) is fine at this scale but harder to unit-test and less portable to Postgres, so we materialize.

### 4.3 Read-only guarantees
1. Connection string: `sqlite3.connect("file:...?mode=ro&immutable=1", uri=True)`.
2. `PRAGMA query_only = ON` set at connection open.
3. `conn.set_authorizer(callback)` — callback returns `SQLITE_OK` only for `SQLITE_SELECT`, `SQLITE_READ`, `SQLITE_FUNCTION`; everything else returns `SQLITE_DENY`. This blocks `ATTACH`, `PRAGMA` writes, temp tables, and any DML/DDL.
4. `safe_select` additionally rejects any statement whose parsed AST (via `sqlglot`) contains anything other than a single `SELECT`, and injects a `LIMIT` if absent.

Any two of these would probably be sufficient; all four are cheap and give defense in depth.

---

## 5. Tool SDK — API Contracts

Every tool is a Python function with a Pydantic input model and a `ToolResult`
envelope. Hand-authored model-facing schemas live in `agent/tools/specs.py` and
are dispatched into the Pydantic models. Every output includes an `evidence`
field: a list of `{kind, id}` pairs the Verifier can re-query. The abbreviated
contracts below explain the design; `agent/core/schemas.py` is authoritative.

### 5.1 Common types

```python
class SnapshotContext(BaseModel):
    snapshot_time: datetime      # 2026-08-15T12:00:00Z
    dataset_version: str

class EvidenceRef(BaseModel):
    kind: Literal[
        "person","idp_account","workspace_account","github_account","device",
        "idp_group","workspace_group","github_team","github_repository",
        "application","drive_resource","drive_permission",
        "idp_app_assignment","application_user_access",
        "idp_group_membership","workspace_group_membership",
        "github_team_membership","github_team_repo_permission","github_repo_collaborator",
        "workspace_oauth_grant","audit_event"
    ]
    id: str

class ToolResult(BaseModel):
    data: Any            # tool-specific payload
    evidence: list[EvidenceRef]
    truncated: bool = False
    notes: list[str] = []
```

### 5.2 Tool: `find_person`

**Purpose:** Fuzzy-resolve a person from a name, email, handle, or ID. Handles the "did they mean the Jane Doe in Eng or the one in Sales?" problem.

```python
class FindPersonIn(BaseModel):
    query: str
    limit: int = 5

class PersonHit(BaseModel):
    person_id: str
    full_name: str
    primary_email: str
    department: Optional[str]
    title: Optional[str]
    employment_status: Literal["active","ended","leave","prehire"]
    manager_person_id: Optional[str]
    score: float  # 0..1 match confidence
    matched_on: list[Literal["person_id","name","email","account_handle"]]

class FindPersonOut(ToolResult):
    data: list[PersonHit]
```

Implementation: SQL `LIKE`/`INSTR` over `people.full_name`, `people.primary_email`, plus join against `idp_accounts.username`, `workspace_accounts.primary_email`, `github_accounts.login`. Score is a simple weighted match. Ties returned; the model chooses.

**Pros:** deterministic, cheap, no vector DB needed at this scale.
**Cons:** English-only, no phonetic match; upgrade path: `pg_trgm` in production.

### 5.3 Tool: `get_person_profile`

**Purpose:** One-shot profile: HR record, linked accounts, devices, high-level counts. Used as the "landing page" of an investigation.

```python
class GetPersonProfileIn(BaseModel):
    person_id: str

class LinkedAccount(BaseModel):
    system: Literal["idp","workspace","github"]
    account_id: str
    handle: str
    status: str
    last_login_at: Optional[datetime]

class DeviceSummary(BaseModel):
    device_id: str
    platform: str
    compliance_status: Optional[str]
    last_checkin_at: Optional[datetime]
    retired_at: Optional[datetime]

class PersonProfile(BaseModel):
    person: dict            # people row, minus internal fields
    accounts: list[LinkedAccount]
    devices: list[DeviceSummary]
    counts: dict            # effective grants plus linked-account/device counts

class GetPersonProfileOut(ToolResult):
    data: PersonProfile
```

### 5.4 Tool: `list_access_for_person`

**Purpose:** The workhorse. Returns every *effective* access grant a person has, across systems, with the path that produced it.

```python
class ListAccessIn(BaseModel):
    person_id: str
    include_revoked: bool = False   # for historical investigations
    systems: Optional[list[str]] = None
    resource_hint: Optional[str] = None

class AccessGrant(BaseModel):
    system: Literal["idp","workspace","github","drive","oauth"]
    resource_kind: Literal["application","group","repo","drive_resource","oauth_scope"]
    resource_id: str
    resource_name: str
    role: Optional[str]
    granted_at: Optional[datetime]
    expires_at: Optional[datetime]
    revoked_at: Optional[datetime]
    effective: bool
    source: Literal["direct","group","nested_group","scim","collab","team","oauth_grant"]
    path: list[EvidenceRef]  # ordered chain: person -> account -> ... -> resource
    evidence: list[EvidenceRef]

class ListAccessOut(ToolResult):
    data: list[AccessGrant]
```

Implementation traverses the relevant account, group, assignment, permission,
and resource tables and applies cycle-safe nested-group closure. The `path`
field is the core mechanism for access explanations: it preserves each observed
edge needed to support the finding.

### 5.5 Tool: `expand_group`

**Purpose:** Return the transitive membership of a group (accounts + sub-groups),
or the groups that transitively contain the supplied group.

```python
class ExpandGroupIn(BaseModel):
    system: Literal["idp","workspace"]
    group_id: str
    direction: Literal["members","containers"] = "members"
    max_depth: int = 8

class GroupExpansion(BaseModel):
    root: EvidenceRef
    direct_members: list[EvidenceRef]
    transitive_members: list[EvidenceRef]
    depth_reached: int
    cycles_detected: list[str]

class ExpandGroupOut(ToolResult):
    data: GroupExpansion
```

### 5.6 Tool: `who_has_access_to`

**Purpose:** Reverse of `list_access_for_person`. Given a resource, who effectively has access, and how did they get it?

```python
class WhoHasAccessIn(BaseModel):
    resource_kind: Literal["application","github_repository","drive_resource"]
    resource_id: str
    include_external: bool = True   # domain/external principals on Drive

class Grantee(BaseModel):
    person_id: Optional[str]        # None for orphan accounts / externals
    account: Optional[EvidenceRef]
    role: Optional[str]
    source: str
    path: list[EvidenceRef]
    evidence: list[EvidenceRef]

class WhoHasAccessOut(ToolResult):
    data: list[Grantee]
```

### 5.7 Tool: `get_audit_trail`

**Purpose:** Retrieve a bounded slice of `audit_events` for a target or actor. Paginated; capped at 100 events per call to protect context.

```python
class GetAuditTrailIn(BaseModel):
    target_id: Optional[str] = None
    actor_id: Optional[str] = None
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    systems: Optional[list[str]] = None
    event_types: Optional[list[str]] = None
    limit: int = 100
    cursor: Optional[str] = None    # opaque next-page cursor

class AuditEventOut(BaseModel):
    event_id: str
    occurred_at: datetime
    system: str
    event_type: str
    actor_type: str
    actor_id: Optional[str]
    target_type: str
    target_id: Optional[str]
    outcome: str
    ip_address: Optional[str]
    correlation_id: Optional[str]
    details: dict

class GetAuditTrailOut(ToolResult):
    data: dict  # {"events": list[AuditEventOut], "next_cursor": Optional[str]}
```

### 5.8 Tool: `find_disagreements`

**Purpose:** Prebuilt scanners for the classes of drift the schema encourages. The model calls this to seed an investigation with candidates and evidence.

```python
class FindDisagreementsIn(BaseModel):
    scanners: list[Literal[
        "offboarding_leakage",
        "orphan_accounts_recent_login",
        "mfa_policy_vs_enrollment",
        "external_share_on_restricted_drive",
        "unretired_device_for_ended_employee",
        "idp_active_but_app_access_revoked",
    ]]
    limit_per_scanner: int = 25

class Finding(BaseModel):
    scanner: str
    severity: Literal["info","low","medium","high","critical"]
    subject: EvidenceRef
    summary: str
    evidence: list[EvidenceRef]

class FindDisagreementsOut(ToolResult):
    data: list[Finding]
```

Each scanner is a hand-written SQL query. They are the deterministic backbone of the offboarding-leakage investigation.

### 5.9 Tool: `safe_select`

**Purpose:** The escape hatch for questions the pre-baked tools don't cover.

```python
class SafeSelectIn(BaseModel):
    sql: str
    parameters: list[Any] = []
    row_limit: int = 100

class SafeSelectOut(ToolResult):
    columns: list[str]
    rows: list[list[Any]]
```

Guardrails:
1. `sqlglot.parse_one(sql)` must yield a single `SELECT` (no CTEs writing, no `INSERT`/`UPDATE`/`DELETE`/`ATTACH`/`PRAGMA`).
2. Injected `LIMIT` if absent, capped at 100.
3. Executed on the read-only, authorizer-guarded connection.
4. Total column bytes returned to the model capped at 32 KB; excess is truncated with `truncated=True`.

**Pros:** flexibility for ad-hoc live-review questions.
**Cons:** the model can still produce semantically wrong SQL (e.g., miss the revoked filter). Mitigated by strong tool prompts and by preferring the pre-baked tools.

### 5.10 Alternatives considered for the tool layer
- **"Give the model raw SQL and the schema."** Simplest, but drops evidence discipline and lets the model hallucinate joins. Rejected as the primary interface; kept as `safe_select`.
- **GraphQL over the schema.** Elegant, but a lot of scaffolding for negligible gain over typed Python functions.
- **A single "search" tool with a query DSL.** Compact but forces the model to learn our DSL. Typed tools scale better.
- **Vector-embedded rows for retrieval.** Overkill at 2k people; upgrade path for production.

---

## 6. Agent Runtime

### 6.1 Roles

| Role | Model (default) | Job | Failure recovery |
|---|---|---|---|
| **Planner** | `openai/gpt-4o-mini` via OpenRouter | Read the question and produce an advisory JSON plan. | Invalid JSON is parsed permissively; unrecoverable errors fail and are persisted. |
| **Executor** | `openai/gpt-4o-mini` via OpenRouter | Adaptively call tools and write the Markdown draft. | Tool executions are strictly capped (15 by default); excess calls are rejected and the model must finalize. |
| **Verifier** | `openai/gpt-4.1-mini` via OpenRouter | Re-fetch cited rows, enforce tool-output provenance, and semantically review claims using runtime-computed temporal facts. | Any semantic, existence, provenance, or final-rewrite citation failure produces `needs_review`. |

### 6.2 Prompts (structure, not full text)
Each role has a system prompt with:
1. **Identity and rules** — "You are the Planner. You produce plans. You never invent record IDs."
2. **Snapshot invariants** — the snapshot_time, revoked/expired semantics, `member_type`/`principal_type` semantics.
3. **Tool catalog** — hand-authored OpenAI tool schemas aligned with the Pydantic inputs.
4. **Output contract** — strict JSON schema for the Planner and Verifier; free-form Markdown for the Executor's final draft.

Prompts live in `agent/llm/prompts.py` so the CLI has no runtime file-loading dependency.

### 6.3 The `Investigation` object (the durable unit of work)

```python
class Investigation(BaseModel):
    id: str
    created_at: str
    snapshot_time: str
    dataset_version: str
    question: str
    trace: list[dict]
    observed_evidence: list[EvidenceRef]
    report_md: str
    verified: bool
    verifier_notes: list[str]
    unresolved_evidence: list[EvidenceRef]
    unobserved_evidence: list[EvidenceRef]
    status: Literal["running","succeeded","failed","needs_review"]
    metrics: dict                # tokens, wall_time_ms, LLM/tool calls
```

Persisted to `RunStore` as JSON. In production the same object goes to DynamoDB or S3 with Object Lock.

### 6.4 Context strategy
- The executor sees only bounded, structured tool results rather than unrestricted tables or the whole database.
- Tools return at most 100 rows; the dispatcher structurally truncates JSON payloads to 8 KB and explicitly tells the model when narrowing is required.
- Only evidence references actually included in the model-visible payload count as observed provenance.
- The verifier receives cited rows plus a bounded set of evidence observed in
  model-visible tool results. This allows safe report repair while provenance
  checks reject unobserved or unchecked citations.

### 6.5 Why not LangGraph / CrewAI / LlamaIndex / etc.?
- We considered LangGraph seriously. It gives us the Planner/Executor/Verifier state machine out of the box and integrates with OpenTelemetry via `langsmith`.
- **Rejected for the take-home** because it adds a heavy dependency and obscures
  the evidence, budget, and repair mechanics implemented directly here.
- **Kept on the roadmap** for production: LangGraph or Strands (AWS's own agent framework) at rung 5.

### 6.6 Cost, latency, and determinism
- `temperature=0` everywhere.
- No response cache or cost estimator ships in the take-home.
- Strict tool-call and wall-clock limits bound execution; production still needs spend quotas and measured latency SLOs.

---

## 7. External Interfaces

### 7.1 CLI
```
python -m agent investigate "Does Jane Doe still have prod access?"
python -m agent investigate --playbook offboarding_leakage
python -m agent investigate --playbook access_explain --person "jane.doe@corp" --resource "GitHub Enterprise"
python -m agent runs list
python -m agent runs show <run_id>
python -m agent runs show <run_id> --format json
```

### 7.2 HTTP API (FastAPI)

FastAPI generates OpenAPI dynamically from the Pydantic models. Shipped endpoints:

```
POST   /investigations
       body: { "question": str, "playbook"?: str, "parameters"?: dict }
       -> 200, completed Investigation JSON

GET    /investigations/{id}
       -> 200, Investigation as JSON

GET    /investigations/{id}/report
       -> 200, text/markdown

GET    /healthz  -> {"status":"ok","snapshot_time":"..."}
GET    /scanners -> scanner names
```

The take-home API is unauthenticated. OIDC/RBAC, SSE, metrics, and asynchronous 202-style execution are production roadmap items.

### 7.3 Playbooks vs. free-form questions
Playbooks are named, parameterized entrypoints. Offline playbooks bypass the LLM entirely; live playbook wrappers still run the Planner with a constrained question and parameters. The two shipping playbooks:

- `offboarding_leakage(limit: int = 5)`
- `access_explain(person_query: str, resource_hint: Optional[str])`

---

## 8. Report Format

Every investigation emits Markdown of the following shape (offline rendering lives in `agent/playbooks/_impl.py`; live rendering is prompt-constrained):

```markdown
# Investigation: <question>
Investigation ID: 3f8c...  |  Snapshot: 2026-08-15T12:00:00Z  |  Verified: yes

## Summary
Two-to-three sentence answer.

## Findings
### Finding 1 — <headline> (severity: high)
Claim. Written in plain English.
**Evidence:**
- `person:person_00842` — Jane Doe, employment_status=ended (2026-07-30)
- `idp_account:idp_00219` — status=active, last_login_at=2026-08-14T10:11:00Z
- `audit_event:evt_9f2c...` — idp.user.login, outcome=success, 2026-08-14T10:11:00Z
- `application_user_access:aua_01142` — application=Snowflake, role=analyst

## Gaps / Uncertainty
- No audit event proves *who* revoked idp_group:grp_004 on 2026-08-01; `actor_type='unknown'`.

## Recommended actions (advisory only)
- Deactivate `idp_account:idp_00219` (Okta console link).
- Retire `device:dev_00871` in MDM.

```

The Verifier's job is to make the "Evidence" bullets true.

---

## 9. Testing and Evaluation

### 9.1 Unit tests (`tests/test_tools.py`)
- `find_person`: name/email/handle/id resolution and score ranking.
- `list_access_for_person`: hand-verified subject with a known nested-group grant, revoked grants excluded when `include_revoked=False`.
- `expand_group`: known 3-level nesting, cycle detection.
- `find_disagreements`: each scanner asserted against hand-picked SQL fixtures.
- `safe_select`: rejects `INSERT`, `ATTACH`, `PRAGMA`; injects `LIMIT`; enforces the byte cap.
- Read-only guarantees: authorizer callback denies writes even when the SQL passes the AST check.

### 9.2 Integration tests (`tests/test_agent_runtime.py`, `tests/test_playbooks.py`)
- Scripted `StubModelClient` responses cover the Planner/Executor/Verifier loop without consuming an API key.
- Deterministic playbooks are tested against the supplied read-only snapshot.
- Regression cases cover fabricated and unobserved IDs, verifier-introduced citations, strict tool budgets, and expired deadlines.

### 9.3 Proposed LLM evaluation harness (not shipped)
- Build a hand-authored golden set with required and forbidden evidence IDs.
- Measure precision, recall, F1, verifier-pass rate, cost, and latency against live models.
- Add a CI threshold only after the set is reviewed and versioned.

### 9.4 Manual review checklist (in the README)
- Do all cited IDs actually exist?
- Do the cited IDs actually support the claim (re-run the tool by hand)?
- Are gaps in the data flagged?
- Are recommended actions labeled advisory?
- Does the trace show the tools the report claims to be based on?

---

## 10. Observability, Security, Failure Modes

### 10.1 Logging and tracing
- Each investigation persists an in-object trace of model/tool calls plus token, call-count, latency, and wall-time metrics.
- Structured logs, OpenTelemetry export, cost accounting, and a `/metrics` endpoint are roadmap items.

### 10.2 Security posture
- Read-only DB via URI mode + `PRAGMA query_only` + `set_authorizer`.
- Container runs as non-root and mounts the supplied DB volume `:ro`; the local
  Compose service keeps a writable `/runs` mount for investigation records.
- Secrets come from environment/config and are never included in tool payloads or traces.
- Prompt-injection defense: tool outputs are structured JSON, not free text; the Executor system prompt instructs the model to treat any instructions found *inside* tool payloads as data, not commands. In production, Bedrock Guardrails add a second layer.
- Output PII classification is a production roadmap item; this supplied dataset is synthetic.

### 10.3 Failure modes and mitigations

| Failure | Mitigation |
|---|---|
| Model hallucinates an evidence ID. | Verifier re-fetches every cited ID; unresolved IDs → `needs_review`. |
| Nested-group cycle. | Closure is cycle-safe; `expand_group` reports cycles and honors `max_depth`. |
| Tool returns too much data. | Per-tool row cap + 8 KB model-payload cap; the model is told to narrow its query. |
| Model exceeds execution budget. | Excess tool calls are not executed; model calls use the remaining wall-clock timeout. |
| OpenRouter rate limit / 5xx. | Three bounded retries with backoff; no secondary-model fallback ships. |
| Ambiguous person match. | `find_person` returns multiple scored hits; the report must disclose ambiguity rather than silently choosing unsupported identity evidence. |
| Audit-event actor is `unknown`. | Explicitly surfaced in the "Gaps" section — never treated as proof of who caused the event. |
| OpenRouter key unavailable or exhausted. | CLI supports deterministic `--offline` playbooks; unit tests use `StubModelClient`. |

---

## 11. Deliverables

The authoritative directory layout is maintained in `README.md`; unlike the production diagrams below, it lists only shipped files.

### 11.1 Dependencies
- `httpx` — OpenRouter calls
- `pydantic`, `pydantic-settings` — schemas + config
- `sqlglot` — SQL AST guard for `safe_select`
- `fastapi`, `uvicorn` — HTTP surface

Development dependency: `pytest`.

Explicitly avoided: LangChain, LangGraph, LlamaIndex, CrewAI, and SQLAlchemy.
Each is a legitimate choice in a larger system but unnecessary for this runtime.

---

## 12. Trade-offs and Alternatives Considered

### 12.1 Agent shape
| Approach | Verdict |
|---|---|
| Single ReAct loop with one strong model | **Rejected** — cheap and simple but weak evidence discipline. |
| Planner → Executor → Verifier (chosen) | **Chosen** — separates evidence gathering, report generation, and adjudication. |
| Multi-agent (planner + specialist agents per system) | Rejected — over-engineering for 2k people; revisit at 200k. |
| Deterministic playbooks only, no free-form loop | Rejected — does not support ad-hoc investigation questions. |
| Pure workflow engine (Temporal / Step Functions) | Overkill for the take-home; production candidate for scheduled sweeps. |

### 12.2 Tooling
| Approach | Verdict |
|---|---|
| Give the model raw SQL and the schema | **Rejected** as primary; **kept** as `safe_select` escape hatch. |
| Hand-written typed tools (chosen) | **Chosen** — encodes domain rules once, testable, reviewer-friendly. |
| GraphQL layer | Rejected — scaffolding cost with negligible gain. |
| Vector search over rows | Overkill; upgrade path at rung 5. |
| MCP server from day one | Attractive but adds a process boundary; production upgrade. |

### 12.3 Storage
| Approach | Verdict |
|---|---|
| SQLite as-is (chosen) | **Chosen** — matches the take-home, portable, embeddable, read-only trivially. |
| Postgres locally | Rejected — no benefit at this scale, higher setup cost for reviewers. |
| DuckDB | Interesting for analytical queries; not worth the swap. |
| In-memory access graph (networkx) | Used *behind* SQLite for the nested-group closure only. |

### 12.4 Model provider
| Approach | Verdict |
|---|---|
| OpenRouter with pluggable model (chosen) | **Chosen** — matches the supplied key, provider-neutral. |
| Direct OpenAI / Anthropic SDKs | Rejected — locks to one provider. |
| Bedrock only | Rejected for take-home; production target. |
| Local model (Llama via Ollama) | Rejected — tool-use quality is not yet competitive. |

### 12.5 UI
| Approach | Verdict |
|---|---|
| CLI + minimal FastAPI (chosen) | **Chosen** — brief explicitly says not to spend time on UI. |
| Streamlit / Gradio dashboard | Rejected — time better spent on the agent. |
| Slack bot | Great production choice; skip for now. |

### 12.6 Evaluation
| Approach | Verdict |
|---|---|
| Golden set of hand-labeled investigations + F1 on evidence IDs | **Next** — the preferred production metric, but not shipped in the timebox. |
| Human eval only | Rejected — not reproducible in CI. |
| LLM-as-judge | Kept as a secondary signal; not the primary metric. |

---

## 13. Path to Production

The take-home establishes useful boundaries for deployment, but production is not merely a configuration change. The ladder below distinguishes what works today from the adapters, infrastructure, and controls still required.

### 13.1 The portability contract (already in the code)
1. All configuration comes from environment variables (12-factor).
2. Investigation state is written via the `RunStore` interface (local FS today; an S3/DynamoDB implementation is not included).
3. DB access is centralized in `ReadOnlyDB`; a portable Postgres interface/implementation remains to be extracted.
4. All model calls go through the `ModelClient` interface (OpenRouter today, Bedrock/OpenAI/Azure later).
5. Tool inputs and result envelopes are Pydantic models, making later MCP/OpenAPI adapters straightforward but not automatic.
6. Investigation traces are provider-neutral; structured logs and OTel instrumentation are not included.

### 13.2 Rung 1 — Laptop / CI
- What it is today.
- Reviewers run `pip install -r requirements.txt && python -m agent investigate "..."`.

### 13.3 Rung 2 — Docker
- `Dockerfile` (multi-stage, `python:3.11-slim`, non-root user, DB mounted `:ro`).
- `docker compose up` brings up the FastAPI service on `:8080` with a health probe.
- **Change from Rung 1:** none in application code when the DB and run directory are mounted as shown by Compose.

### 13.4 Rung 3 — Managed container (AWS Fargate, GCP Cloud Run, Azure Container Apps)
- The included Terraform is explicitly illustrative. It sketches an ECS service, task role, secret reference, and Object Lock bucket; it intentionally omits networking, ALB/auth, ECR wiring, log-group setup, DB distribution, and writable run storage.
- **Code changes required:** implement managed `RunStore`, authentication/RBAC, production logging/metrics, and a durable data adapter; then complete the infrastructure modules.
- **Data:** Compose mounts SQLite read-only. The production task needs an explicit snapshot-delivery mechanism or an Access Graph service.

### 13.5 Rung 4 — Kubernetes (EKS)
- Proposed Helm chart with Deployment, HPA, PDB, NetworkPolicy, and ServiceMonitor.
- Proposed OTel Collector and External Secrets integration.
- Proposed scheduled sweeps as `CronJob`s calling a playbook endpoint.
- These manifests are not included and depend on the managed persistence, auth, and telemetry work from Rung 3.

### 13.6 Rung 5 — Bedrock **AgentCore** (target architecture)
- Expose the existing typed tools through a managed gateway with per-tool IAM
  authorization rather than allowing unrestricted database queries.
- Package the Planner/Executor/Verifier loop behind a managed runtime adapter;
  keep the current `ModelClient`, `RunStore`, and tool contracts as boundaries.
- Replace the local snapshot with a refreshed access-graph service and retain
  snapshot/freshness metadata in every investigation.
- Add organization identity, scoped memory, centralized telemetry, output
  redaction, and durable tamper-evident investigation storage.
- Keep remediation behind explicit human approval and a separate write-capable
  service; this investigation agent remains read-only.

**Rough planning estimate, not a commitment:** several engineering days for a hardened runtime adapter after platform prerequisites exist, plus separate multi-week work for ingestion, identity, operational controls, and validation.

### 13.7 What would change about the agent itself in production
- **Long-term memory** — user- and org-scoped memory of common entity aliases, prior investigations, and known false positives.
- **Streaming reports** — Verifier runs incrementally rather than at the end.
- **Live data caveats** — a "freshness" field per source so the agent can say "this Workspace data is 47 minutes old."
- **Role-based tool visibility** — analysts see all tools; auditors see read-only tools; automation accounts see only playbooks they own.
- **Cost governance** — per-team monthly budgets enforced at the model gateway.

### 13.8 What we would *not* change
- The typed tool SDK.
- The Planner/Executor/Verifier separation.
- The evidence-graph representation.
- The report template.
- The eval harness and its metric (evidence-ID F1).

Those are the parts of the design that hold up at scale.

---

## 14. Open Questions

1. **Snapshot vs. live.** In production, do we accept small staleness in exchange for read-only guarantees, or do we build a read-through cache on top of live source APIs? Current design assumes snapshot; ingestion cadence is the knob.
2. **Multi-tenancy.** If this is offered as a service across multiple companies, do we scope per-tenant IAM roles at the Gateway layer, or per-tenant AgentCore Runtime deployments? The latter is safer but costlier.
3. **Fine-tuning.** Once we have a large golden set, is a small fine-tuned Planner better than prompting a general model? Worth revisiting at ≥ 500 investigations.
4. **Explainable severity.** Today severity is scanner-defined. Should it come from a learned model over historical incidents?
5. **Autonomous remediation.** The agent is advisory today. What is the minimum controls package (dual approval, blast-radius simulation, rollback plan) that would let it *execute* low-risk actions like revoking an unused OAuth grant?

---

## 15. Appendix — Requirements traceability

Mapped directly to the brief's evaluation criteria:

| Criterion | How this design addresses it |
|---|---|
| Agent and tool design | Typed tools with domain rules baked in; Planner/Executor/Verifier separation; narrow SQL escape hatch; MCP-ready shape. |
| Context management | Models receive bounded structured rows rather than unrestricted tables; per-tool row and byte caps, snapshot pinning, and explicit observed-evidence tracking bound context. |
| Evidence and uncertainty | Every claim carries `EvidenceRef`s; Verifier re-fetches them; a dedicated "Gaps" section for what the data cannot prove. |
| Scoping and judgment | Two shipping investigations, plus a general capability; production vision documented but not attempted in-scope. |
| Implementation quality | Small dependency set; testable tools; deterministic and stubbed-model tests; portable interfaces with production gaps labeled explicitly. |

---

*End of design.*
