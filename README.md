# Access Investigation Agent

An LLM-driven agent that helps a security team investigate employee access
across HR, an identity provider, Google Workspace, GitHub, and device
management. Answers natural-language questions ("Does Ariel Chen still have
prod access?"), runs deterministic playbook sweeps ("who among the ended
employees still has surviving access?"), and produces Markdown reports whose
every claim is backed by explicit record and event IDs from the supplied
SQLite snapshot.

- Read-only SQLite by construction (URI mode + `PRAGMA query_only` + a SQLite
  authorizer callback + a SELECT-only AST guard on the LLM's escape hatch).
- **Planner → Executor → Verifier** loop: the Verifier re-fetches cited rows,
  checks that every citation came from this run's tool outputs, and performs a
  semantic pass over each claim before marking the report verified.
- Typed tool SDK (Pydantic in/out) instead of raw SQL for the LLM.
- Runs from a clean checkout in ~30 seconds. `--offline` gives fully
  deterministic playbooks without touching the LLM.
- The same core runs as a CLI, FastAPI service, and Docker container. Provider,
  persistence, and database boundaries reduce—but do not eliminate—the code
  required for a future Fargate, EKS, or Bedrock AgentCore migration.

The full architectural rationale, API contracts, tradeoffs, and production
migration plan live in **[`design.md`](./design.md)**.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate      # optional
make install                                             # or: pip install -e '.[dev]'
cp .env.example .env      # then paste your OpenRouter key in .env
                          # (or drop it in token.txt — .gitignored)

make test        # 45 tests, no API key needed
make demo        # two deterministic investigations, no key needed
make demo-live   # two LLM-driven investigations, needs OPENROUTER_API_KEY
make server      # FastAPI on :8080
make docker      # build + run in Docker

# LLM-driven, free-form
python -m agent investigate "Does Ariel Chen still have access to any critical applications after their end date?"

# Same tools, LLM-driven, playbook shape
python -m agent investigate --playbook offboarding_leakage --limit 5
python -m agent investigate --playbook access_explain --person "Emi Kim" --resource "Snowflake"

# Deterministic — no LLM, no key needed
python -m agent investigate --offline --playbook offboarding_leakage --limit 5
python -m agent investigate --offline --playbook access_explain --person "Ariel Chen" --resource "Snowflake"

# Inspect stored runs
python -m agent runs list
python -m agent runs show <run_id>
python -m agent runs show <run_id> --format json
python -m agent scanners
```

Runs a FastAPI service on port 8080:
```bash
uvicorn agent.interfaces.server:app --port 8080
# then:  curl -s http://localhost:8080/healthz
#        curl -s -X POST http://localhost:8080/investigations \
#             -H 'content-type: application/json' \
#             -d '{"offline":true,"playbook":"offboarding_leakage","parameters":{"limit":3}}'
```

Or with Docker:
```bash
docker compose -f deploy/docker-compose.yml up --build
```

---

## Configuration

All configuration is env-var driven (12-factor). See `.env.example`.

| Var | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Required for live LLM calls. `token.txt` is used as a local fallback and is `.gitignore`d. |
| `AGENT_DB_PATH` | `input_data/access_snapshot.sqlite` | Path to the read-only SQLite snapshot. |
| `AGENT_MODEL_PLANNER` | `openai/gpt-4o-mini` | Planner model (OpenRouter id). |
| `AGENT_MODEL_EXECUTOR` | `openai/gpt-4o-mini` | Executor model. Each role can be configured independently with an OpenRouter model id. |
| `AGENT_MODEL_VERIFIER` | `openai/gpt-4.1-mini` | Semantic verifier model; stronger instruction following is used for evidence and temporal checks. |
| `AGENT_MAX_TOOL_CALLS` | `15` | Executor budget per investigation. |
| `AGENT_MAX_WALL_SECONDS` | `90` | Wall-clock budget. |
| `AGENT_RUN_STORE_DIR` | `runs/` | Where investigations are persisted as JSON. Ignored by git. |

---

## Architecture (at a glance)

```
CLI  |  FastAPI  ----->  Agent (Planner -> Executor -> Verifier)
                                |
                                v
                         Typed Tool SDK (Pydantic)
                          find_person, list_access_for_person,
                          expand_group, who_has_access_to,
                          get_audit_trail, find_disagreements,
                          safe_select, resolve_evidence
                                |
                                v
                    Read-only SQLite adapter
                    (URI ro + PRAGMA query_only +
                     set_authorizer + SQL AST guard +
                     materialised nested-group closure)
                                |
                                v
                  input_data/access_snapshot.sqlite
```

Three ideas do most of the work:

1. **Typed, purpose-built tools** encode the domain rules once (nested-group
   expansion, `revoked_at/expires_at` filtering, polymorphic `member_type` /
   `principal_type` dispatch). The model never writes raw joins.
2. **Evidence is a data structure, not a prompt convention.** Every tool
   returns an `evidence: [{kind, id}, …]` list, and the report must cite
   claims with `` `kind:id` `` markers.
3. **The Verifier checks existence, provenance, and claim support.** Every cited
   row is re-fetched, citations must have appeared in this run's tool outputs,
   and the verifier reviews whether the rows support the surrounding claim.

Read `design.md` for the long form.

---

## Investigations shipped

### 1. Offboarding leakage
> "Which `employment_status='ended'` people still have surviving accounts,
> devices, application access, or post-end-date audit activity?"

Powered by the `offboarding_leakage` scanner + `find_disagreements` tool. Both
deterministic (`--offline`) and LLM-driven variants ship.

- Deterministic sample:
  [`examples/offline/offboarding_leakage.md`](./examples/offline/offboarding_leakage.md)
- Current LLM sample (verified against the submitted runtime):
  [`examples/llm/post_termination_access.md`](./examples/llm/post_termination_access.md)

### 2. Access explain
> "Does person X have access to application/repo Y, and if so, why?"

Traces the exact grant chain: direct IdP assignment, IdP group, nested group,
SCIM-provisioned app-reported access, GitHub team, direct collaborator, or
OAuth grant. Each hop is cited as an evidence ID.

- Deterministic sample:
  [`examples/offline/access_explain.md`](./examples/offline/access_explain.md)
- Current LLM sample (verified against the submitted runtime):
  [`examples/llm/access_explain.md`](./examples/llm/access_explain.md)

Other detectors available for free-form questions via `find_disagreements`:
`orphan_accounts_recent_login`, `mfa_policy_vs_enrollment`,
`external_share_on_restricted_drive`, `unretired_device_for_ended_employee`,
`idp_active_but_app_access_revoked`.

---

## Testing

```bash
pytest -q
```

45 tests. They exercise:

- **Read-only guarantees** — `INSERT`, `DELETE`, `DROP`, and `ATTACH` all
  raise `DatabaseError` at the authorizer layer even after passing the SQL
  parser (`tests/test_db.py`).
- **Tool correctness** — for every tool, cited evidence IDs resolve back to
  real rows via `resolve_evidence`. Effective/revoked filters honored. Nested
  group expansion returns sets, not lists. Scanners produce findings whose
  every ID resolves. (`tests/test_tools.py`)
- **Playbooks** — the `--offline` playbooks produce reports whose citations
  all resolve. (`tests/test_playbooks.py`)
- **Agent runtime with a stubbed model** — verifies the loop's evidence
  discipline: a stub that cites a real ID is `verified=True`, one that cites a
  fabricated ID is flagged `needs_review` with the fabricated ID enumerated
  in `unresolved_evidence`, verifier rewrites cannot introduce unchecked IDs,
  and tool/deadline budgets fail closed. No API key needed.
  (`tests/test_agent_runtime.py`)
- **Edge cases** — equal-timestamp audit pagination, cycle-aware/depth-bounded
  group traversal, invalid API inputs, offline run persistence, CLI process
  exit codes, temporal-claim prompting, and container entrypoint importability.
  (`tests/test_tools.py`, `tests/test_cli.py`, `tests/test_prompts.py`,
  `tests/test_deploy.py`)

### How I checked the agent while building it

1. Ran each tool by hand against the DB and confirmed evidence IDs resolve
   (see `tests/test_tools.py` — this is that check formalized).
2. Ran the deterministic playbook to establish a ground-truth report for
   comparison with live-model output.
3. Ran the LLM agent with the same question and diffed the cited IDs against
   the deterministic set. The two files under `examples/llm/` are captured
   outputs from the exact commands in `make demo-live`. That target prints and
   persists runs under `runs/`; it does not overwrite the checked-in Markdown
   examples automatically.
4. Deliberately fed the Verifier a fabricated ID
   (`test_stubbed_agent_flags_hallucinated_id`) and confirmed the run comes
   back `needs_review`.

---

## Known limitations

- **Model dependence.** Live report quality still varies by model even though
  deterministic provenance, temporal, and access-chain checks fail closed.
  Planner, Executor, and Verifier models are independently configurable.
- **Nested-group paths return one shortest route.** All membership edges on that
  route are cited, but alternate parallel routes are not enumerated.
- **`safe_select` accepts semantically-wrong SELECTs.** The AST guard blocks
  writes, but a model can still forget the `revoked_at IS NULL` filter and
  produce a plausible-but-incorrect answer. The typed tools are always
  preferred over `safe_select`; the LLM is instructed accordingly.
- **No streaming.** The FastAPI surface returns the finished Investigation.
  Streaming Server-Sent Events is a small addition (described in `design.md`)
  but skipped for the 4-hour timebox.
- **No monetary/token-cost quota.** Tool-call and wall-clock limits are
  enforced, but production still needs per-org token and spend quotas at the
  model gateway.
- **Single-node file `RunStore`.** Fine for the CLI and one container. Swap
  for S3 with Object Lock (WORM) in production; the interface is designed for
  it.
- **The `find_person` "handle" match uses `LIKE '%q%'`** so a query like `"kim"`
  will match every "Kim" in the company. In production, add `pg_trgm` or a
  small inverted index; not worth it at 2 000 people.

## What I would build next

1. **AgentCore migration** — package the same tools as an AgentCore Gateway
   target, package the loop as a Strands agent for AgentCore Runtime, and
   swap OpenRouter for Bedrock. Rationale and step-by-step plan in
   `design.md` §13.
2. **Golden-set evaluation** with F1 on cited evidence IDs, wired into CI.
   The harness sketch is in `design.md` §9.3.
3. **Streaming reports** over SSE + a Slack bot front end.
4. **RBAC** — analyst / lead / auditor / read-only, plus per-tool ACLs behind
   the FastAPI middleware.
5. **Signed, WORM-persisted investigations** for SOC2-friendly audit
   evidence.
6. **Long-term memory** of entity aliases ("Jane Doe = jdoe@ = person_00842")
   and known false positives across investigations.

---

## Repository layout

```
.
├── agent/                   # the Python package
│   ├── __init__.py
│   ├── __main__.py                  # `python -m agent ...`
│   ├── core/                        # framework-neutral primitives
│   │   ├── config.py                #   12-factor settings
│   │   ├── db.py                    #   read-only SQLite + nested-group closure
│   │   ├── schemas.py               #   Pydantic tool + Investigation contracts
│   │   └── run_store.py             #   JSON RunStore (S3 impl behind same iface)
│   ├── tools/                       # the typed tool SDK
│   │   ├── access.py                #   find_person, list_access_for_person, …
│   │   ├── scanners.py              #   deterministic disagreement scanners
│   │   ├── specs.py                 #   OpenAI-format tool schemas for the LLM
│   │   └── dispatch.py              #   LLM tool call -> typed tool
│   ├── llm/                         # provider abstraction + orchestration
│   │   ├── model_client.py          #   OpenRouter (swappable) + StubModelClient
│   │   ├── prompts.py               #   Planner / Executor / Verifier prompts
│   │   └── runtime.py               #   Planner -> Executor -> Verifier loop
│   ├── playbooks/                   # deterministic + LLM-driven playbooks
│   │   └── _impl.py
│   └── interfaces/                  # entry points over the same core
│       ├── cli.py                   #   `python -m agent investigate …`
│       └── server.py                #   FastAPI at /investigations, /healthz
├── tests/                   # 45 tests; no API key needed
│   ├── conftest.py                  # shared read-only DB fixture
│   ├── test_db.py
│   ├── test_deploy.py
│   ├── test_tools.py
│   ├── test_playbooks.py
│   ├── test_cli.py
│   ├── test_prompts.py
│   └── test_agent_runtime.py
├── examples/
│   ├── offline/                     # deterministic playbook outputs
│   │   ├── offboarding_leakage.md
│   │   └── access_explain.md
│   └── llm/                         # captured final-code live outputs
│       ├── post_termination_access.md
│       └── access_explain.md
├── deploy/
│   ├── Dockerfile                   # multi-stage, non-root, healthcheck
│   ├── docker-compose.yml
│   └── terraform/                   # unapplied Fargate sketch
│       └── main.tf
├── docs/
│   └── brief.md                     # original take-home brief
├── input_data/              # provided read-only SQLite + SCHEMA.md
├── design.md                # detailed low-level design and roadmap
├── README.md
├── Makefile                 # `make install / test / demo / server / docker`
├── pyproject.toml
├── requirements.txt
├── .env.example
└── .gitignore               # excludes local secrets, runs, caches, transcript export
```

---

## Submission packaging

Create an archive from tracked files so local keys, virtual environments,
caches, and generated run records cannot be included accidentally:

```bash
git archive --format=zip --output=access-investigation-agent.zip HEAD
```

The assignment requires coding-agent prompts or chat transcripts. They are
intentionally not tracked in this repository; attach the official conversation
export separately when submitting the repository or archive.
