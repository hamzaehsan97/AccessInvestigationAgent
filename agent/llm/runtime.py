"""Planner → Executor → Verifier orchestration.

Public entrypoint: ``Agent.investigate(question, ...) -> Investigation``.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from agent.core.config import Settings, get_settings
from agent.core.db import ReadOnlyDB, get_db
from agent.llm.model_client import ChatResult, ModelClient, default_client
from agent.llm.prompts import executor_system, planner_system, verifier_system
from agent.core.run_store import FileRunStore, RunStore
from agent.core.schemas import EvidenceRef, Investigation, ResolvedEvidence
from agent.tools.dispatch import dispatch_tool_call, summarize_result
from agent.tools.specs import TOOL_SPECS
from agent.tools.access import resolve_evidence
from agent.core.schemas import ResolveEvidenceIn


# Regex to pull `kind:id` citations out of a Markdown report.
# kinds are lowercase snake, ids are ASCII (letters, digits, _, -, .)
_CITATION_RE = re.compile(
    r"`([a-z_]+):([A-Za-z0-9_.\-]+)`"
)
_REFERENCE_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_])([a-z_]+):([A-Za-z0-9_.\-]+)"
)


VALID_KINDS = {
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
}


class InvestigationDeadlineExceeded(TimeoutError):
    """Raised when an investigation exhausts its configured wall-clock budget."""


def _remaining_seconds(deadline: Optional[float]) -> Optional[float]:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InvestigationDeadlineExceeded("investigation wall-clock budget exhausted")
    return remaining


def _dedupe_refs(refs: list[EvidenceRef]) -> list[EvidenceRef]:
    seen: set[str] = set()
    out: list[EvidenceRef] = []
    for ref in refs:
        if ref.key() in seen:
            continue
        seen.add(ref.key())
        out.append(ref)
    return out


def _find_incomplete_access_chains(
    cited: list[EvidenceRef], temporal_facts: list[dict]
) -> list[dict]:
    """Require one complete observed grant chain per cited effective resource."""
    cited_keys = {ref.key() for ref in cited}
    cited_resource_ids = {
        ref.id
        for ref in cited
        if ref.kind in {"application", "github_repository", "drive_resource"}
    }
    gaps: list[dict] = []
    for resource_id in sorted(cited_resource_ids):
        candidates: list[list[str]] = []
        for fact in temporal_facts:
            if (
                fact.get("resource_id") != resource_id
                or fact.get("effective_at_snapshot") is not True
            ):
                continue
            chain = [str(key) for key in fact.get("evidence", [])]
            if chain:
                candidates.append(chain)
        if not candidates or any(set(chain) <= cited_keys for chain in candidates):
            continue
        shortest = min(candidates, key=len)
        gaps.append(
            {
                "resource_id": resource_id,
                "missing_from_shortest_observed_chain": [
                    key for key in shortest if key not in cited_keys
                ],
                "shortest_observed_chain": shortest,
            }
        )
    return gaps


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _derive_access_temporal_facts(
    person_id: str,
    grants: object,
    db: ReadOnlyDB,
) -> list[dict]:
    """Compute date relationships that an LLM should never have to infer."""
    if not isinstance(grants, list):
        return []
    person = db.one(
        "SELECT employment_status, end_date FROM people WHERE person_id = ?",
        (person_id,),
    )
    if not person or not person["end_date"]:
        return []
    end_at = _parse_iso(person["end_date"])
    snapshot_at = _parse_iso(db.snapshot_time_iso)
    if end_at is None or snapshot_at is None:
        return []

    facts: list[dict] = []
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        granted_at = _parse_iso(grant.get("granted_at"))
        effective = grant.get("effective") is True
        evidence = [
            f"{ref.get('kind')}:{ref.get('id')}"
            for ref in grant.get("evidence", [])
            if isinstance(ref, dict) and ref.get("kind") and ref.get("id")
        ]
        facts.append(
            {
                "person_id": person_id,
                "employment_status": person["employment_status"],
                "end_date": person["end_date"],
                "snapshot_time": db.snapshot_time_iso,
                "snapshot_after_end_date": snapshot_at > end_at,
                "resource_id": grant.get("resource_id"),
                "resource_name": grant.get("resource_name"),
                "granted_at": grant.get("granted_at"),
                "grant_after_end_date": (
                    granted_at > end_at if granted_at is not None else None
                ),
                "effective_at_snapshot": effective,
                "effective_after_end_date": effective and snapshot_at > end_at,
                "evidence": evidence,
            }
        )
    return facts


def _extract_citations(text: str) -> list[EvidenceRef]:
    seen: set[tuple[str, str]] = set()
    out: list[EvidenceRef] = []
    for m in _CITATION_RE.finditer(text):
        kind, id_ = m.group(1), m.group(2)
        if kind not in VALID_KINDS:
            continue
        key = (kind, id_)
        if key in seen:
            continue
        seen.add(key)
        out.append(EvidenceRef(kind=kind, id=id_))
    return out


def _extract_reference_mentions(text: str) -> list[EvidenceRef]:
    """Find typed references for repair only; final citations still need backticks."""
    seen: set[tuple[str, str]] = set()
    out: list[EvidenceRef] = []
    for match in _REFERENCE_MENTION_RE.finditer(text):
        kind, id_ = match.group(1), match.group(2)
        key = (kind, id_)
        if kind not in VALID_KINDS or key in seen:
            continue
        seen.add(key)
        out.append(EvidenceRef(kind=kind, id=id_))
    return out


class Agent:
    def __init__(
        self,
        client: Optional[ModelClient] = None,
        db: Optional[ReadOnlyDB] = None,
        settings: Optional[Settings] = None,
        run_store: Optional[RunStore] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db or get_db()
        self.client = client  # may be None until first LLM call
        self.run_store = run_store or FileRunStore(self.settings.run_store_dir)

    # ---------- top-level entrypoint ----------

    def investigate(
        self,
        question: str,
        requester: Optional[str] = None,
        playbook: Optional[str] = None,
        parameters: Optional[dict] = None,
        max_tool_calls: Optional[int] = None,
    ) -> Investigation:
        inv = Investigation(
            id=str(uuid.uuid4()),
            created_at=_now_iso(),
            snapshot_time=self.db.snapshot_time_iso,
            dataset_version=self.db.dataset_version,
            question=question,
            requester=requester,
            playbook=playbook,
            parameters=parameters or {},
        )
        t0 = time.monotonic()
        max_wall = max(1, self.settings.agent_max_wall_seconds)
        deadline = t0 + max_wall
        try:
            if self.client is None:
                self.client = default_client()

            # 1. Planner
            plan = self._plan(inv, deadline=deadline)
            inv.trace.append({"role": "planner", "kind": "plan", "content": plan})

            # 2. Executor
            budget = max_tool_calls if max_tool_calls is not None else self.settings.agent_max_tool_calls
            draft = self._execute(inv, plan, max(1, budget), deadline=deadline)

            # 3. Verifier
            report, verified, unresolved, unobserved = self._verify(
                inv, draft, deadline=deadline
            )
            inv.report_md = report
            inv.verified = verified
            inv.unresolved_evidence = unresolved
            inv.unobserved_evidence = unobserved
            inv.status = "succeeded" if verified else "needs_review"
        except Exception as e:
            inv.report_md = f"# Investigation FAILED\n\n{type(e).__name__}: {e}"
            inv.status = "failed"
            inv.trace.append({"role": "runtime", "kind": "error", "content": str(e)})
        finally:
            inv.metrics["wall_time_ms"] = int((time.monotonic() - t0) * 1000)
            inv.metrics["llm_calls"] = sum(
                1 for step in inv.trace if str(step.get("kind", "")).startswith("llm_call")
            )
            inv.metrics["tool_calls"] = sum(
                1
                for step in inv.trace
                if step.get("kind") == "tool_call" and step.get("executed", True)
            )
            inv.metrics["tokens_in"] = sum(
                int(step.get("tokens_in") or 0) for step in inv.trace
            )
            inv.metrics["tokens_out"] = sum(
                int(step.get("tokens_out") or 0) for step in inv.trace
            )
            self.run_store.save(inv)
        return inv

    # ---------- 1. Planner ----------

    def _plan(self, inv: Investigation, deadline: Optional[float] = None) -> dict:
        sys = planner_system(inv.snapshot_time, inv.dataset_version)
        user = f"QUESTION:\n{inv.question}\n\nIf a playbook is provided, honor it: {inv.playbook or 'none'}\nParameters: {json.dumps(inv.parameters)}"
        result: ChatResult = self.client.chat(
            model=self.settings.agent_model_planner,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
            ],
            tools=None,
            temperature=0.0,
            max_tokens=1200,
            timeout_seconds=_remaining_seconds(deadline),
        )
        _remaining_seconds(deadline)
        inv.trace.append(
            {
                "role": "planner",
                "kind": "llm_call",
                "model": result.model,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
            }
        )
        # extract JSON blob (be permissive of code fences)
        text = result.content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            plan = json.loads(text)
        except Exception:
            # last-ditch: find first {...} block
            m = re.search(r"\{.*\}", text, flags=re.S)
            plan = json.loads(m.group(0)) if m else {"steps": []}
        return plan

    # ---------- 2. Executor ----------

    def _execute(
        self,
        inv: Investigation,
        plan: dict,
        budget: int,
        deadline: Optional[float] = None,
    ) -> str:
        sys = executor_system(
            inv.snapshot_time,
            inv.dataset_version,
            budget,
            self.settings.agent_max_wall_seconds,
        )
        messages: list[dict] = [
            {"role": "system", "content": sys},
            {
                "role": "user",
                "content": (
                    f"QUESTION:\n{inv.question}\n\n"
                    f"PLAN FROM PLANNER (advisory; adapt if needed):\n{json.dumps(plan)[:3000]}\n\n"
                    "Begin. Call tools until you have enough evidence, then output the Markdown report."
                ),
            },
        ]
        tool_calls_used = 0
        turn = 0
        while True:
            _remaining_seconds(deadline)
            # Force at least one tool call on the first turn so the model
            # cannot short-circuit with an evidence-free report.
            if turn == 0:
                choice: str | dict = "required"
            elif tool_calls_used < budget:
                choice = "auto"
            else:
                choice = "none"
            turn += 1
            result = self.client.chat(
                model=self.settings.agent_model_executor,
                messages=messages,
                tools=TOOL_SPECS,
                tool_choice=choice,
                temperature=0.0,
                max_tokens=2500,
                timeout_seconds=_remaining_seconds(deadline),
            )
            _remaining_seconds(deadline)
            inv.trace.append(
                {
                    "role": "executor",
                    "kind": "llm_call",
                    "model": result.model,
                    "tokens_in": result.tokens_in,
                    "tokens_out": result.tokens_out,
                    "tool_calls": [tc.get("function", {}).get("name") for tc in result.tool_calls],
                }
            )

            if not result.tool_calls:
                # final report
                return result.content.strip()

            # append assistant message with tool_calls (required by API for follow-up)
            messages.append(
                {
                    "role": "assistant",
                    "content": result.content or None,
                    "tool_calls": result.tool_calls,
                }
            )
            remaining_budget = max(0, budget - tool_calls_used)
            for call_index, tc in enumerate(result.tool_calls):
                fn = tc.get("function", {})
                name = fn.get("name") or ""
                try:
                    arguments = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    arguments = {}
                executed = call_index < remaining_budget
                if executed:
                    tool_calls_used += 1
                    _remaining_seconds(deadline)
                    try:
                        tool_res, latency = dispatch_tool_call(name, arguments, self.db)
                        payload = summarize_result(tool_res)
                        visible_payload = json.loads(payload)
                        observed = {ref.key() for ref in inv.observed_evidence}
                        for raw_ref in visible_payload.get("evidence", []):
                            ref = EvidenceRef(**raw_ref)
                            if ref.key() not in observed:
                                inv.observed_evidence.append(ref)
                                observed.add(ref.key())
                        error = None
                    except Exception as e:
                        payload = json.dumps({"error": f"{type(e).__name__}: {e}"})
                        latency = 0
                        error = str(e)
                else:
                    error = "tool-call budget exhausted; call was not executed"
                    payload = json.dumps({"error": error})
                    latency = 0
                inv.trace.append(
                    {
                        "role": "executor",
                        "kind": "tool_call",
                        "tool": name,
                        "arguments": arguments,
                        "latency_ms": latency,
                        "error": error,
                        "result_bytes": len(payload),
                        "executed": executed,
                        "computed_temporal_facts": (
                            _derive_access_temporal_facts(
                                str(arguments.get("person_id") or ""),
                                visible_payload.get("data"),
                                self.db,
                            )
                            if executed
                            and error is None
                            and name == "list_access_for_person"
                            else []
                        ),
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "name": name,
                        "content": payload,
                    }
                )
            if tool_calls_used >= budget:
                # push the executor to finalize
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You have reached your tool-call budget. Produce the final "
                            "Markdown report now using the evidence you already have."
                        ),
                    }
                )
                final = self.client.chat(
                    model=self.settings.agent_model_executor,
                    messages=messages,
                    tools=TOOL_SPECS,
                    tool_choice="none",
                    temperature=0.0,
                    max_tokens=2500,
                    timeout_seconds=_remaining_seconds(deadline),
                )
                _remaining_seconds(deadline)
                inv.trace.append(
                    {
                        "role": "executor",
                        "kind": "llm_call_final",
                        "model": final.model,
                        "tokens_in": final.tokens_in,
                        "tokens_out": final.tokens_out,
                    }
                )
                return final.content.strip()
    # ---------- 3. Verifier ----------

    def _verify(
        self,
        inv: Investigation,
        draft: str,
        deadline: Optional[float] = None,
    ) -> tuple[str, bool, list[EvidenceRef], list[EvidenceRef]]:
        cited = _extract_citations(draft)
        if not cited:
            # nothing to verify; return draft with an annotation
            note = "\n\n> Verifier: no `kind:id` citations found in the report."
            inv.verifier_notes.append(note)
            return draft + note, False, [], []

        # Resolve cited IDs plus the evidence observed during execution. Giving
        # the verifier the observed rows lets it safely repair a missing link in
        # a citation chain instead of guessing from the draft's prose.
        available_refs = _dedupe_refs(cited + inv.observed_evidence)
        resolved = resolve_evidence(ResolveEvidenceIn(refs=available_refs), db=self.db)
        available_resolutions: list[ResolvedEvidence] = [
            ResolvedEvidence(**r) for r in resolved.data
        ]
        resolution_by_key = {r.ref.key(): r for r in available_resolutions}
        resolutions = [resolution_by_key[r.key()] for r in cited]
        unresolved = [r.ref for r in resolutions if not r.exists]
        observed_keys = {r.key() for r in inv.observed_evidence}
        cited_keys = {r.key() for r in cited}
        unobserved = [r for r in cited if r.key() not in observed_keys]

        inv.trace.append(
            {
                "role": "verifier",
                "kind": "resolve",
                "cited": len(cited),
                "unresolved": len(unresolved),
                "unobserved": len(unobserved),
            }
        )

        # Existence is necessary but not sufficient. The semantic verifier sees
        # the cited rows and whether each citation was actually observed during
        # this investigation, then checks that they support the surrounding claim.
        sys = verifier_system(inv.snapshot_time, inv.dataset_version)
        computed_temporal_facts: list[dict] = []
        seen_temporal_facts: set[str] = set()
        for step in inv.trace:
            if step.get("kind") != "tool_call":
                continue
            for fact in step.get("computed_temporal_facts", []):
                fact_key = json.dumps(fact, sort_keys=True, default=str)
                if fact_key in seen_temporal_facts:
                    continue
                seen_temporal_facts.add(fact_key)
                computed_temporal_facts.append(fact)
        temporal_facts_truncated = len(computed_temporal_facts) > 25
        computed_temporal_facts = computed_temporal_facts[:25]
        payload = {
            "unresolved": [r.model_dump() for r in unresolved],
            "unobserved": [r.model_dump() for r in unobserved],
            "available_evidence": [
                {
                    "ref": r.ref.model_dump(),
                    "exists": r.exists,
                    "cited": r.ref.key() in cited_keys,
                    "observed": r.ref.key() in observed_keys,
                    "summary": r.summary,
                    "row": r.row,
                }
                for r in available_resolutions
            ],
            "computed_temporal_facts": computed_temporal_facts,
            "computed_temporal_facts_truncated": temporal_facts_truncated,
        }
        payload_json = json.dumps(payload, default=str, separators=(",", ":"))
        if len(payload_json.encode("utf-8")) > 24000:
            # Keep the verifier input valid JSON if an unusually large report
            # cites many rows. Summaries retain the relevant relationship fields.
            for item in payload["available_evidence"]:
                item.pop("row", None)
            payload_json = json.dumps(payload, default=str, separators=(",", ":"))
        if len(payload_json.encode("utf-8")) > 24000:
            # Retain every cited row, then as many additional observed rows as
            # fit. Citation rows are mandatory for the semantic verdict.
            required = [
                item for item in payload["available_evidence"] if item["cited"]
            ]
            optional = [
                item for item in payload["available_evidence"] if not item["cited"]
            ]
            payload["available_evidence"] = required
            payload["evidence_packet_truncated"] = True
            for item in optional:
                payload["available_evidence"].append(item)
                candidate = json.dumps(payload, default=str, separators=(",", ":"))
                if len(candidate.encode("utf-8")) > 24000:
                    payload["available_evidence"].pop()
                    break
            payload_json = json.dumps(payload, default=str, separators=(",", ":"))
        evidence_packet_keys = {
            f"{item['ref']['kind']}:{item['ref']['id']}"
            for item in payload["available_evidence"]
            if item["observed"] and item["exists"]
        }
        result = self.client.chat(
            model=self.settings.agent_model_verifier,
            messages=[
                {"role": "system", "content": sys},
                {
                    "role": "user",
                    "content": (
                        "DRAFT REPORT:\n" + draft + "\n\n"
                        "EVIDENCE VALIDATION INPUT (JSON):\n"
                        + payload_json
                    ),
                },
            ],
            tools=None,
            temperature=0.0,
            max_tokens=2500,
            timeout_seconds=_remaining_seconds(deadline),
        )
        _remaining_seconds(deadline)
        inv.trace.append(
            {
                "role": "verifier",
                "kind": "llm_call",
                "model": result.model,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
            }
        )
        raw = result.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        try:
            verdict = json.loads(raw)
            semantic_ok = verdict.get("verified") is True
            report = str(verdict.get("report_md") or draft).strip()
            notes = verdict.get("notes") or []
            if isinstance(notes, list):
                inv.verifier_notes.extend(str(n) for n in notes)
        except Exception:
            semantic_ok = False
            report = draft
            inv.verifier_notes.append("Verifier returned invalid JSON; semantic verification failed.")

        # A verifier can correctly repair a draft yet conservatively retain a
        # false verdict because the *original* contained an error. When the
        # deterministic existence/provenance gates are already clean, separate
        # repair from adjudication and judge only the corrected candidate.
        if (
            not semantic_ok
            and report != draft
            and not unresolved
            and not unobserved
        ):
            recheck = self.client.chat(
                model=self.settings.agent_model_verifier,
                messages=[
                    {"role": "system", "content": sys},
                    {
                        "role": "user",
                        "content": (
                            "CORRECTED CANDIDATE REPORT:\n"
                            + report
                            + "\n\nEVIDENCE VALIDATION INPUT (JSON):\n"
                            + payload_json
                            + "\n\nJudge only the corrected candidate above. Do not penalize "
                            "it for errors that existed only in an earlier draft. Return "
                            "verified=true if every remaining claim is supported; otherwise "
                            "return verified=false and repair the remaining issue."
                        ),
                    },
                ],
                tools=None,
                temperature=0.0,
                max_tokens=2500,
                timeout_seconds=_remaining_seconds(deadline),
            )
            _remaining_seconds(deadline)
            inv.trace.append(
                {
                    "role": "verifier",
                    "kind": "llm_call_recheck",
                    "model": recheck.model,
                    "tokens_in": recheck.tokens_in,
                    "tokens_out": recheck.tokens_out,
                }
            )
            recheck_raw = re.sub(
                r"\s*```$",
                "",
                re.sub(r"^```(?:json)?\s*", "", recheck.content.strip()),
            )
            try:
                recheck_verdict = json.loads(recheck_raw)
                semantic_ok = recheck_verdict.get("verified") is True
                report = str(recheck_verdict.get("report_md") or report).strip()
                recheck_notes = recheck_verdict.get("notes") or []
                if isinstance(recheck_notes, list):
                    inv.verifier_notes.extend(str(n) for n in recheck_notes)
            except Exception:
                semantic_ok = False
                inv.verifier_notes.append(
                    "Verifier recheck returned invalid JSON; semantic verification failed."
                )

        # A semantic model can approve an application catalog row as though it
        # proved access. Enforce complete observed chains deterministically and
        # give the verifier one bounded opportunity to add the missing evidence
        # or remove the unsupported resource claim.
        candidate_cited = _extract_reference_mentions(report)
        access_chain_gaps = _find_incomplete_access_chains(
            candidate_cited, computed_temporal_facts
        )
        if access_chain_gaps and not unresolved and not unobserved:
            chain_repair = self.client.chat(
                model=self.settings.agent_model_verifier,
                messages=[
                    {"role": "system", "content": sys},
                    {
                        "role": "user",
                        "content": (
                            "CANDIDATE REPORT:\n"
                            + report
                            + "\n\nDETERMINISTIC ACCESS-CHAIN GAPS (JSON):\n"
                            + json.dumps(access_chain_gaps, separators=(",", ":"))
                            + "\n\nEVIDENCE VALIDATION INPUT (JSON):\n"
                            + payload_json
                            + "\n\nFor each gap, either cite every ID in one supplied "
                            "observed chain in the relevant finding or remove the "
                            "unsupported resource-access claim. Every evidence ID must "
                            "use the exact backticked `kind:id` form. Raw IDs such as "
                            "`idpaa_000001` without their kind do not count. Copy each "
                            "typed value from `shortest_observed_chain` exactly inside "
                            "backticks. Return the complete corrected report and judge "
                            "that corrected report."
                        ),
                    },
                ],
                tools=None,
                temperature=0.0,
                max_tokens=2500,
                timeout_seconds=_remaining_seconds(deadline),
            )
            _remaining_seconds(deadline)
            inv.trace.append(
                {
                    "role": "verifier",
                    "kind": "llm_call_chain_repair",
                    "model": chain_repair.model,
                    "tokens_in": chain_repair.tokens_in,
                    "tokens_out": chain_repair.tokens_out,
                }
            )
            chain_raw = re.sub(
                r"\s*```$",
                "",
                re.sub(r"^```(?:json)?\s*", "", chain_repair.content.strip()),
            )
            try:
                chain_verdict = json.loads(chain_raw)
                semantic_ok = chain_verdict.get("verified") is True
                report = str(chain_verdict.get("report_md") or report).strip()
                chain_notes = chain_verdict.get("notes") or []
                if isinstance(chain_notes, list):
                    inv.verifier_notes.extend(str(n) for n in chain_notes)
            except Exception:
                semantic_ok = False
                inv.verifier_notes.append(
                    "Access-chain repair returned invalid JSON; semantic verification failed."
                )

        # The verifier may rewrite the report. Treat that output as untrusted and
        # run the deterministic existence/provenance checks again. Newly-added
        # citations were not part of the semantic verifier's evidence packet, so
        # they fail closed unless the row was observed and included in the
        # semantic verifier's evidence packet.
        final_cited = _extract_citations(report)
        final_access_chain_gaps = _find_incomplete_access_chains(
            final_cited, computed_temporal_facts
        )
        if final_access_chain_gaps:
            inv.verifier_notes.append(
                "Final report lacks a complete observed access chain for: "
                + ", ".join(gap["resource_id"] for gap in final_access_chain_gaps)
            )
        final_resolved = resolve_evidence(
            ResolveEvidenceIn(refs=final_cited), db=self.db
        ) if final_cited else None
        final_resolutions = (
            [ResolvedEvidence(**row) for row in final_resolved.data]
            if final_resolved is not None
            else []
        )
        final_unresolved = [row.ref for row in final_resolutions if not row.exists]
        final_unobserved = [
            ref for ref in final_cited if ref.key() not in observed_keys
        ]
        original_keys = {ref.key() for ref in cited}
        introduced = [ref for ref in final_cited if ref.key() not in original_keys]
        introduced_unchecked = [
            ref for ref in introduced if ref.key() not in evidence_packet_keys
        ]
        if introduced_unchecked:
            inv.verifier_notes.append(
                "Verifier rewrite introduced citations that were not semantically checked: "
                + ", ".join(ref.key() for ref in introduced_unchecked)
            )
        unresolved = _dedupe_refs(unresolved + final_unresolved)
        unobserved = _dedupe_refs(
            unobserved + final_unobserved + introduced_unchecked
        )
        verified = (
            semantic_ok
            and bool(final_cited)
            and not unresolved
            and not unobserved
            and not final_access_chain_gaps
        )
        inv.trace.append(
            {
                "role": "verifier",
                "kind": "final_report_validation",
                "cited": len(final_cited),
                "unresolved": len(final_unresolved),
                "unobserved": len(final_unobserved),
                "introduced": len(introduced),
                "introduced_unchecked": len(introduced_unchecked),
                "incomplete_access_chains": len(final_access_chain_gaps),
            }
        )
        if verified:
            header = f"> Verified: yes — {len(final_cited)} citations were observed and semantically checked.\n\n"
        else:
            header = (
                "> Verified: no — semantic or provenance checks failed "
                f"({len(unresolved)} unresolved, {len(unobserved)} unobserved).\n\n"
            )
        return header + report, verified, unresolved, unobserved
