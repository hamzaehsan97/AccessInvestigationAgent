"""Deterministic disagreement scanners.

Each scanner is a hand-written SQL query over the read-only DB. They return
``Finding`` objects with evidence IDs the Verifier can re-fetch. These are the
backbone of the offboarding-leakage investigation and any drift-detection sweep.
"""
from __future__ import annotations

import json
from typing import Callable

from agent.core.db import ReadOnlyDB
from agent.core.schemas import EvidenceRef, Finding


ScannerFn = Callable[[ReadOnlyDB, int], list[Finding]]


# ---------- offboarding_leakage ----------


def _offboarding_leakage(db: ReadOnlyDB, limit: int) -> list[Finding]:
    findings: list[Finding] = []
    ended = db.execute(
        "SELECT person_id, full_name, primary_email, end_date, employment_status "
        "FROM people WHERE employment_status = 'ended' "
        "ORDER BY end_date DESC",
    )
    for p in ended:
        pid = p["person_id"]
        subj = EvidenceRef(kind="person", id=pid)
        ev: list[EvidenceRef] = []
        surviving = []

        # active idp accounts
        for a in db.execute(
            "SELECT account_id, username, status, last_login_at FROM idp_accounts "
            "WHERE person_id = ? AND status = 'active'",
            (pid,),
        ):
            surviving.append(f"idp_account={a['account_id']} ({a['username']}, last_login={a['last_login_at']})")
            ev.append(EvidenceRef(kind="idp_account", id=a["account_id"]))

        # active workspace accounts
        for a in db.execute(
            "SELECT account_id, primary_email, status, last_login_at "
            "FROM workspace_accounts WHERE person_id = ? AND status = 'active'",
            (pid,),
        ):
            surviving.append(
                f"workspace_account={a['account_id']} ({a['primary_email']}, last_login={a['last_login_at']})"
            )
            ev.append(EvidenceRef(kind="workspace_account", id=a["account_id"]))

        # active github accounts
        for a in db.execute(
            "SELECT account_id, login, status, last_active_at "
            "FROM github_accounts WHERE person_id = ? AND status = 'active'",
            (pid,),
        ):
            surviving.append(
                f"github_account={a['account_id']} ({a['login']}, last_active={a['last_active_at']})"
            )
            ev.append(EvidenceRef(kind="github_account", id=a["account_id"]))

        # unretired devices
        for d in db.execute(
            "SELECT device_id, platform, last_check_in_at FROM devices "
            "WHERE person_id = ? AND retired_at IS NULL",
            (pid,),
        ):
            surviving.append(
                f"device={d['device_id']} (platform={d['platform']}, last_check_in={d['last_check_in_at']})"
            )
            ev.append(EvidenceRef(kind="device", id=d["device_id"]))

        # Effective grant relationships across IdP applications, app-reported
        # access, GitHub repositories, Drive, and Workspace OAuth. Importing
        # locally avoids a module cycle because access.py lazily imports scanners.
        from agent.core.schemas import ListAccessIn
        from agent.tools.access import list_access_for_person

        grants = list_access_for_person(ListAccessIn(person_id=pid), db=db)
        for grant in grants.data:
            surviving.append(
                f"grant={grant['system']}/{grant['resource_kind']}:{grant['resource_id']} "
                f"via={grant['source']} role={grant.get('role') or 'n/a'}"
            )
        ev.extend(grants.evidence)

        # recent audit events (post-end_date) for any of their accounts
        acct_ids: list[str] = []
        for tbl, col in [
            ("idp_accounts", "account_id"),
            ("workspace_accounts", "account_id"),
            ("github_accounts", "account_id"),
        ]:
            for r in db.execute(
                f"SELECT {col} FROM {tbl} WHERE person_id = ?", (pid,)
            ):
                acct_ids.append(r[col])
        recent_events_ev: list[EvidenceRef] = []
        if acct_ids and p["end_date"]:
            placeholders = ",".join("?" * len(acct_ids))
            rows = db.execute(
                f"SELECT event_id, occurred_at, system, event_type FROM audit_events "
                f"WHERE occurred_at > ? AND (actor_id IN ({placeholders}) OR target_id IN ({placeholders})) "
                f"ORDER BY occurred_at DESC LIMIT 5",
                (p["end_date"] + "T23:59:59Z", *acct_ids, *acct_ids),
            )
            for r in rows:
                surviving.append(
                    f"post_end_event={r['event_id']} ({r['system']}.{r['event_type']} at {r['occurred_at']})"
                )
                recent_events_ev.append(EvidenceRef(kind="audit_event", id=r["event_id"]))
        ev.extend(recent_events_ev)

        if not surviving:
            continue

        severity = "critical" if any("grant=" in s for s in surviving) else "high"
        summary = (
            f"{p['full_name']} (employment_status=ended, end_date={p['end_date']}) "
            f"still has {len(surviving)} surviving artifact(s): "
            + "; ".join(surviving[:6])
            + (" ..." if len(surviving) > 6 else "")
        )
        deduped_evidence: list[EvidenceRef] = []
        seen_evidence: set[str] = set()
        for ref in ev:
            if ref.key() in seen_evidence:
                continue
            seen_evidence.add(ref.key())
            deduped_evidence.append(ref)
        findings.append(
            Finding(
                scanner="offboarding_leakage",
                severity=severity,
                subject=subj,
                summary=summary,
                evidence=deduped_evidence,
            )
        )
    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    findings.sort(
        key=lambda finding: (
            severity_rank[finding.severity],
            len(finding.evidence),
        ),
        reverse=True,
    )
    return findings[:limit]


# ---------- orphan_accounts_recent_login ----------


def _orphan_accounts_recent_login(db: ReadOnlyDB, limit: int) -> list[Finding]:
    findings: list[Finding] = []
    for r in db.execute(
        "SELECT account_id, username, status, last_login_at FROM idp_accounts "
        "WHERE person_id IS NULL AND status='active' AND last_login_at IS NOT NULL "
        "ORDER BY last_login_at DESC LIMIT ?",
        (limit,),
    ):
        subj = EvidenceRef(kind="idp_account", id=r["account_id"])
        ev = [subj]
        # find a recent login event
        ev_rows = db.execute(
            "SELECT event_id, occurred_at FROM audit_events "
            "WHERE actor_id = ? AND event_type IN ('user.login','application.authentication') "
            "ORDER BY occurred_at DESC LIMIT 2",
            (r["account_id"],),
        )
        for e in ev_rows:
            ev.append(EvidenceRef(kind="audit_event", id=e["event_id"]))
        findings.append(
            Finding(
                scanner="orphan_accounts_recent_login",
                severity="high",
                subject=subj,
                summary=(
                    f"IdP account {r['account_id']} ({r['username']}) is active with no linked person; "
                    f"last_login_at={r['last_login_at']}"
                ),
                evidence=ev,
            )
        )
    # also cover workspace + github
    for r in db.execute(
        "SELECT account_id, primary_email, status, last_login_at FROM workspace_accounts "
        "WHERE person_id IS NULL AND status='active' AND last_login_at IS NOT NULL "
        "ORDER BY last_login_at DESC LIMIT ?",
        (limit,),
    ):
        subj = EvidenceRef(kind="workspace_account", id=r["account_id"])
        findings.append(
            Finding(
                scanner="orphan_accounts_recent_login",
                severity="medium",
                subject=subj,
                summary=(
                    f"Workspace account {r['account_id']} ({r['primary_email']}) is active with "
                    f"no linked person; last_login_at={r['last_login_at']}"
                ),
                evidence=[subj],
            )
        )
    for r in db.execute(
        "SELECT account_id, login, account_type, status, last_active_at FROM github_accounts "
        "WHERE person_id IS NULL AND status='active' AND account_type='user' "
        "AND last_active_at IS NOT NULL "
        "ORDER BY last_active_at DESC LIMIT ?",
        (limit,),
    ):
        subj = EvidenceRef(kind="github_account", id=r["account_id"])
        findings.append(
            Finding(
                scanner="orphan_accounts_recent_login",
                severity="medium",
                subject=subj,
                summary=(
                    f"GitHub user account {r['account_id']} ({r['login']}) has no linked person; "
                    f"last_active_at={r['last_active_at']}"
                ),
                evidence=[subj],
            )
        )
    return findings[:limit * 3]


# ---------- mfa_policy_vs_enrollment ----------


def _mfa_policy_vs_enrollment(db: ReadOnlyDB, limit: int) -> list[Finding]:
    findings: list[Finding] = []
    rows = db.execute(
        "SELECT aua.access_id, aua.application_id, aua.idp_account_id, aua.mfa_enrollment_status, "
        "app.name AS app_name, app.default_mfa_requirement, app.sensitivity, "
        "ia.person_id, ia.username "
        "FROM application_user_access aua "
        "JOIN applications app ON app.application_id = aua.application_id "
        "JOIN idp_accounts ia ON ia.account_id = aua.idp_account_id "
        "WHERE app.default_mfa_requirement = 'required' "
        "AND aua.mfa_enrollment_status IN ('not_enrolled','unknown') "
        "AND aua.status = 'active' AND aua.revoked_at IS NULL "
        "ORDER BY app.sensitivity DESC LIMIT ?",
        (limit,),
    )
    for r in rows:
        subj = EvidenceRef(kind="application_user_access", id=r["access_id"])
        ev = [
            subj,
            EvidenceRef(kind="application", id=r["application_id"]),
            EvidenceRef(kind="idp_account", id=r["idp_account_id"]),
        ]
        if r["person_id"]:
            ev.append(EvidenceRef(kind="person", id=r["person_id"]))
        severity = "high" if r["sensitivity"] == "critical" else "medium"
        findings.append(
            Finding(
                scanner="mfa_policy_vs_enrollment",
                severity=severity,
                subject=subj,
                summary=(
                    f"App '{r['app_name']}' requires MFA but IdP account {r['username']} "
                    f"is {r['mfa_enrollment_status']}"
                ),
                evidence=ev,
            )
        )
    return findings


# ---------- external_share_on_restricted_drive ----------


def _external_share_on_restricted_drive(db: ReadOnlyDB, limit: int) -> list[Finding]:
    findings: list[Finding] = []
    rows = db.execute(
        "SELECT dp.permission_id, dp.resource_id, dp.principal_type, dp.principal_id, dp.role, "
        "dr.name AS resource_name, dr.classification "
        "FROM drive_permissions dp "
        "JOIN drive_resources dr ON dr.resource_id = dp.resource_id "
        "WHERE dp.revoked_at IS NULL "
        "AND dp.principal_type IN ('domain','external_email') "
        "AND dr.classification IN ('restricted','confidential') "
        "ORDER BY dr.classification DESC LIMIT ?",
        (limit,),
    )
    for r in rows:
        subj = EvidenceRef(kind="drive_permission", id=r["permission_id"])
        ev = [
            subj,
            EvidenceRef(kind="drive_resource", id=r["resource_id"]),
        ]
        severity = "critical" if r["classification"] == "restricted" else "high"
        findings.append(
            Finding(
                scanner="external_share_on_restricted_drive",
                severity=severity,
                subject=subj,
                summary=(
                    f"{r['classification']} resource '{r['resource_name']}' shared to "
                    f"{r['principal_type']}={r['principal_id']} with role {r['role']}"
                ),
                evidence=ev,
            )
        )
    return findings


# ---------- unretired_device_for_ended_employee ----------


def _unretired_device_for_ended_employee(db: ReadOnlyDB, limit: int) -> list[Finding]:
    findings: list[Finding] = []
    rows = db.execute(
        "SELECT d.device_id, d.platform, d.compliance_status, d.last_check_in_at, "
        "p.person_id, p.full_name, p.end_date "
        "FROM devices d JOIN people p ON p.person_id = d.person_id "
        "WHERE p.employment_status = 'ended' AND d.retired_at IS NULL "
        "ORDER BY p.end_date DESC LIMIT ?",
        (limit,),
    )
    for r in rows:
        subj = EvidenceRef(kind="device", id=r["device_id"])
        ev = [subj, EvidenceRef(kind="person", id=r["person_id"])]
        findings.append(
            Finding(
                scanner="unretired_device_for_ended_employee",
                severity="high",
                subject=subj,
                summary=(
                    f"Device {r['device_id']} ({r['platform']}) belongs to "
                    f"{r['full_name']} (ended {r['end_date']}); last_check_in={r['last_check_in_at']}"
                ),
                evidence=ev,
            )
        )
    return findings


# ---------- idp_active_but_app_access_revoked ----------


def _idp_active_but_app_access_revoked(db: ReadOnlyDB, limit: int) -> list[Finding]:
    findings: list[Finding] = []
    rows = db.execute(
        "SELECT aua.access_id, aua.application_id, aua.status, aua.revoked_at, "
        "ia.account_id, ia.username, ia.status AS idp_status, app.name AS app_name "
        "FROM application_user_access aua "
        "JOIN idp_accounts ia ON ia.account_id = aua.idp_account_id "
        "JOIN applications app ON app.application_id = aua.application_id "
        "WHERE ia.status = 'active' AND aua.status = 'revoked' "
        "LIMIT ?",
        (limit,),
    )
    for r in rows:
        subj = EvidenceRef(kind="application_user_access", id=r["access_id"])
        ev = [
            subj,
            EvidenceRef(kind="idp_account", id=r["account_id"]),
            EvidenceRef(kind="application", id=r["application_id"]),
        ]
        findings.append(
            Finding(
                scanner="idp_active_but_app_access_revoked",
                severity="low",
                subject=subj,
                summary=(
                    f"IdP account {r['username']} is active but app '{r['app_name']}' "
                    f"reports access as revoked at {r['revoked_at']}"
                ),
                evidence=ev,
            )
        )
    return findings


# ---------- dispatcher ----------


_SCANNERS: dict[str, ScannerFn] = {
    "offboarding_leakage": _offboarding_leakage,
    "orphan_accounts_recent_login": _orphan_accounts_recent_login,
    "mfa_policy_vs_enrollment": _mfa_policy_vs_enrollment,
    "external_share_on_restricted_drive": _external_share_on_restricted_drive,
    "unretired_device_for_ended_employee": _unretired_device_for_ended_employee,
    "idp_active_but_app_access_revoked": _idp_active_but_app_access_revoked,
}


def run_scanner(name: str, db: ReadOnlyDB, limit: int = 25) -> list[Finding]:
    fn = _SCANNERS.get(name)
    if not fn:
        return []
    return fn(db, limit)


def available_scanners() -> list[str]:
    return list(_SCANNERS.keys())
