"""Deterministic playbooks.

Playbooks bypass the Planner and hand the Executor a pre-baked question with a
hard-wired first tool call. This gives us reliable demos (offboarding sweeps,
access-explain) that will land the same evidence citations every run.

For maximum reliability, ``offboarding_leakage`` also builds a fully
deterministic report from the ``find_disagreements`` scanner without any LLM
involvement — useful for the ``--offline`` flag and CI.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from agent.core.db import ReadOnlyDB, get_db
from agent.llm.runtime import Agent
from agent.core.schemas import (
    EvidenceRef,
    FindDisagreementsIn,
    FindPersonIn,
    Investigation,
    ListAccessIn,
    WhoHasAccessIn,
)
from agent.tools.access import (
    find_disagreements,
    find_person,
    list_access_for_person,
    who_has_access_to,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- offline offboarding-leakage report ----------


def offline_offboarding_report(
    limit: int = 5, db: Optional[ReadOnlyDB] = None
) -> Investigation:
    """Produce an offboarding-leakage investigation without any LLM call.

    Useful for the ``--offline`` flag and for reviewers who exhaust their key.
    """
    db = db or get_db()
    findings = find_disagreements(
        FindDisagreementsIn(scanners=["offboarding_leakage"], limit_per_scanner=limit),
        db=db,
    )
    lines: list[str] = []
    lines.append("# Investigation: Which ended employees still have surviving access?")
    lines.append(
        f"Snapshot: {db.snapshot_time_iso}  |  Playbook: offboarding_leakage  |  Investigation status: succeeded"
    )
    lines.append("")
    lines.append("## Summary")
    lines.append(
        f"Scanned all `employment_status='ended'` people and returned the top {len(findings.data)} "
        "with surviving accounts, devices, application access, or post-end-date audit "
        "activity. Every finding cites the record and event IDs that support it."
    )
    lines.append("")
    lines.append("## Findings")
    for i, f in enumerate(findings.data, 1):
        sev = f.get("severity", "medium")
        summ = f.get("summary", "")
        subj = f.get("subject", {})
        lines.append(
            f"### Finding {i} — Ended employee retains access "
            f"(severity: {sev})"
        )
        lines.append(summ)
        lines.append("**Evidence:**")
        # subject first
        if subj:
            k, i_ = subj.get("kind"), subj.get("id")
            lines.append(f"- `{k}:{i_}` — subject (ended employee)")
        seen_report_evidence = {f"{subj.get('kind')}:{subj.get('id')}"} if subj else set()
        for ev in f.get("evidence", []):
            k, i_ = ev.get("kind"), ev.get("id")
            evidence_key = f"{k}:{i_}"
            if evidence_key in seen_report_evidence:
                continue
            seen_report_evidence.add(evidence_key)
            lines.append(f"- `{k}:{i_}`")
        lines.append("")
    lines.append("## Gaps / Uncertainty")
    lines.append(
        "- Post-end-date audit events with `actor_type='unknown'` prove that an "
        "action occurred against the account but not who caused it. Where the "
        "actor is the account itself, this is stronger evidence of continued use."
    )
    lines.append(
        "- Application-reported access (`application_user_access`) can lag IdP "
        "state; if only IdP-side rows are stale, downstream MFA and login "
        "controls may still be intact."
    )
    lines.append(
        "- An effective grant relationship does not by itself prove the linked "
        "account can authenticate. Account status is separate and is cited so an "
        "analyst can distinguish stale entitlement from currently usable access."
    )
    lines.append("")
    lines.append("## Recommended actions (advisory only)")
    lines.append("- Validate account state, then deactivate each still-active cited `idp_account` and `workspace_account`.")
    lines.append("- Suspend each still-active cited `github_account` and rotate its personal-access tokens.")
    lines.append("- Retire each cited `device` in MDM and rotate device certificates.")
    lines.append(
        "- Revoke each cited `application_user_access` row and re-scan for lingering `workspace_oauth_grant` records."
    )
    report = "\n".join(lines)

    # collect all evidence for the record
    all_ev: list[EvidenceRef] = []
    for f in findings.data:
        all_ev.append(EvidenceRef(**f["subject"]))
        for e in f.get("evidence", []):
            all_ev.append(EvidenceRef(**e))

    inv = Investigation(
        id=str(uuid.uuid4()),
        created_at=_now_iso(),
        snapshot_time=db.snapshot_time_iso,
        dataset_version=db.dataset_version,
        question="Which ended employees still have surviving access?",
        playbook="offboarding_leakage",
        parameters={"limit": limit, "offline": True},
        trace=[
            {
                "role": "playbook",
                "kind": "scanner",
                "tool": "find_disagreements",
                "scanners": ["offboarding_leakage"],
                "n_findings": len(findings.data),
            }
        ],
        report_md=f"> Verified: yes — deterministic playbook, all IDs sourced from DB scanner.\n\n{report}",
        verified=True,
        status="succeeded",
        metrics={"tool_calls": 1, "llm_calls": 0},
    )
    return inv


# ---------- offline access-explain report ----------


def offline_access_explain_report(
    person_query: str,
    resource_hint: Optional[str] = None,
    db: Optional[ReadOnlyDB] = None,
) -> Investigation:
    """Answer *"why does person X have application/repo/drive Y?"* deterministically.

    ``resource_hint`` may be an application name (case-insensitive substring),
    an `app_xxx` id, a `ghr_xxx` id, or None (return the top grants).
    """
    db = db or get_db()
    p = find_person(FindPersonIn(query=person_query, limit=1), db=db)
    if not p.data:
        return Investigation(
            id=str(uuid.uuid4()),
            created_at=_now_iso(),
            snapshot_time=db.snapshot_time_iso,
            dataset_version=db.dataset_version,
            question=f"Why does {person_query} have access to {resource_hint or '?'}",
            playbook="access_explain",
            parameters={"person_query": person_query, "resource_hint": resource_hint, "offline": True},
            report_md=f"# Investigation FAILED — no person matched query `{person_query}`",
            verified=False,
            status="failed",
        )
    hit = p.data[0]
    pid = hit["person_id"]
    grants_res = list_access_for_person(ListAccessIn(person_id=pid), db=db)
    grants = grants_res.data

    # Resolve the resource independently from the person's grants. This lets us
    # distinguish "resource does not exist" from "resource exists, no access".
    resource_matches: list[EvidenceRef] = []
    filtered = grants
    if resource_hint:
        hint = resource_hint.lower()
        for table, id_col, name_col, kind in [
            ("applications", "application_id", "name", "application"),
            ("github_repositories", "repository_id", "name", "github_repository"),
            ("drive_resources", "resource_id", "name", "drive_resource"),
        ]:
            for row in db.execute(
                f"SELECT {id_col} FROM {table} "
                f"WHERE lower({id_col}) = ? OR lower({name_col}) LIKE ? LIMIT 10",
                (hint, f"%{hint}%"),
            ):
                resource_matches.append(EvidenceRef(kind=kind, id=row[id_col]))
        filtered = [
            g
            for g in grants
            if hint in (g.get("resource_id") or "").lower()
            or hint in (g.get("resource_name") or "").lower()
        ]
    resource_not_found = bool(resource_hint) and not resource_matches
    show = filtered if resource_hint else grants[:5]
    investigation_status = "needs_review" if resource_not_found else "succeeded"

    lines: list[str] = []
    title = f"Why does {hit['full_name']} have access to {resource_hint or 'their resources'}?"
    lines.append(f"# Investigation: {title}")
    lines.append(
        f"Snapshot: {db.snapshot_time_iso}  |  Playbook: access_explain  |  "
        f"Investigation status: {investigation_status}"
    )
    lines.append("")
    lines.append("## Summary")
    lines.append(
        f"{hit['full_name']} (`person:{pid}`, {hit['title']}, {hit['department']}, "
        f"employment_status={hit['employment_status']}) has "
        f"{len(grants)} effective grant(s) in total. "
        + (
            f"Filtered to {len(filtered)} matching grant(s) for '{resource_hint}'."
            if resource_hint and not resource_not_found
            else (
                f"No supported resource matched '{resource_hint}'."
                if resource_not_found
                else "Showing top 5."
            )
        )
    )
    lines.append("")
    lines.append("## Findings")
    if not show:
        if resource_not_found:
            lines.append("### Finding 1 — Resource not found (severity: info)")
            lines.append(
                f"No application, GitHub repository, or Drive resource in the snapshot "
                f"matches `{resource_hint}`. Access was not inferred from unrelated grants."
            )
        else:
            lines.append("### Finding 1 — No matching access found (severity: info)")
            lines.append(
                f"The resource exists, but no effective grant for {hit['full_name']} "
                f"matches `{resource_hint}`."
            )
        lines.append("**Evidence:**")
        lines.append(f"- `person:{pid}`")
        for ref in resource_matches:
            lines.append(f"- `{ref.key()}` — matched resource")
        lines.append("")
    for i, g in enumerate(show, 1):
        src = g.get("source", "?")
        role = g.get("role") or "n/a"
        headline = f"Grant chain via {src} to {g['resource_name']} (role={role})"
        sev = "info"
        # bump severity if the resource is a critical app or sensitive repo
        rname = (g.get("resource_name") or "").lower()
        if any(x in rname for x in ("prod", "payments", "critical", "restricted")):
            sev = "medium"
        lines.append(f"### Finding {i} — {headline} (severity: {sev})")
        # narrate the path
        path_desc = " → ".join(f"`{p['kind']}:{p['id']}`" for p in g["path"])
        lines.append(
            f"Path: {path_desc}. Source category: **{src}**. "
            f"granted_at={g.get('granted_at')}, revoked_at={g.get('revoked_at') or 'null'}, "
            f"effective={g.get('effective')}."
        )
        lines.append("**Evidence:**")
        seen_ids: set[str] = set()
        for e in g.get("evidence", []):
            key = f"{e['kind']}:{e['id']}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            lines.append(f"- `{key}`")
        lines.append("")
    lines.append("## Gaps / Uncertainty")
    lines.append(
        "- IdP assignments and application-reported access may disagree; the tool "
        "returns both paths when both exist so you can compare."
    )
    lines.append(
        "- Nested-group membership was expanded with cycle-safe traversal. The report "
        "shows the shortest discovered path and cites every membership edge on it."
    )
    lines.append("")
    lines.append("## Recommended actions (advisory only)")
    if resource_not_found:
        lines.append(
            "- Confirm the resource name or identifier and whether it belongs to a "
            "system represented in this snapshot before investigating access."
        )
    elif not show:
        lines.append("- No access-removal action is supported by the current snapshot.")
    elif hit["employment_status"] == "ended":
        lines.append(
            f"- Person is ended (end_date={hit.get('end_date')}). Deactivate the "
            "underlying account(s) upstream of every grant chain above."
        )
    if show:
        lines.append(
            "- Review each grant's `source` — grants labelled `nested_group` are the most "
            "commonly forgotten during offboarding."
        )
    report = "\n".join(lines)

    inv = Investigation(
        id=str(uuid.uuid4()),
        created_at=_now_iso(),
        snapshot_time=db.snapshot_time_iso,
        dataset_version=db.dataset_version,
        question=title,
        playbook="access_explain",
        parameters={
            "person_query": person_query,
            "resource_hint": resource_hint,
            "person_id": pid,
            "n_grants_total": len(grants),
            "n_grants_shown": len(show),
            "offline": True,
        },
        trace=[
            {"role": "playbook", "kind": "tool_call", "tool": "find_person", "hits": len(p.data)},
            {"role": "playbook", "kind": "tool_call", "tool": "list_access_for_person", "grants": len(grants)},
        ],
        observed_evidence=[
            *p.evidence,
            *grants_res.evidence,
            *resource_matches,
        ],
        report_md=(
            "> Verified: no — the requested resource was not found in supported catalogs.\n\n"
            if resource_not_found
            else "> Verified: yes — deterministic playbook, all IDs sourced from tool outputs.\n\n"
        )
        + report,
        verified=not resource_not_found,
        status=investigation_status,
        metrics={"tool_calls": 2, "llm_calls": 0},
    )
    return inv


# ---------- LLM-driven wrappers (Agent under the hood) ----------


def offboarding_leakage_llm(agent: Agent, limit: int = 5) -> Investigation:
    question = (
        "List the top ended employees who still have surviving access "
        f"(top {limit}). For each, enumerate the surviving accounts, devices, "
        "application access, and any post-end-date audit activity. "
        "Cite every finding with record and event IDs."
    )
    return agent.investigate(
        question=question,
        playbook="offboarding_leakage",
        parameters={"limit": limit},
    )


def access_explain_llm(
    agent: Agent, person_query: str, resource_hint: Optional[str] = None
) -> Investigation:
    question = (
        f"Determine whether {person_query} has effective access to "
        f"{resource_hint or 'their applications and repositories'} at the snapshot time. "
        "Do not assume the premise is true. If access exists, return the exact grant "
        "chain (direct / group / nested_group / SCIM / team / collab / oauth) with "
        "evidence IDs. If the resource exists but no matching effective grant is "
        "returned, report that supported negative result. Note any source disagreements."
    )
    return agent.investigate(
        question=question,
        playbook="access_explain",
        parameters={"person_query": person_query, "resource_hint": resource_hint},
    )
