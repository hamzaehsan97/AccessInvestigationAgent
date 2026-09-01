"""Typed tool SDK. These are the only functions the LLM can invoke.

Design principles:
  * Every tool takes a Pydantic input, returns a Pydantic ``ToolResult`` whose
    ``data`` is a Pydantic model / list of Pydantic models.
  * Every tool populates ``evidence`` with the record IDs the reader would need
    to re-verify the result. The Verifier re-fetches these.
  * Domain rules (effective vs. revoked, nested groups, member_type dispatch)
    are encoded once, here — the model never sees raw joins.
  * Row caps and byte caps keep the model's context small.
"""
from __future__ import annotations

import json
import re
import base64
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import sqlglot
import sqlglot.expressions as exp

from agent.core.db import ReadOnlyDB, get_db
from agent.core.schemas import (
    AccessGrant,
    AuditEventOut,
    EvidenceRef,
    ExpandGroupIn,
    FindDisagreementsIn,
    FindPersonIn,
    Finding,
    GetAuditTrailIn,
    Grantee,
    GroupExpansion,
    LinkedAccount,
    ListAccessIn,
    PersonHit,
    PersonProfile,
    DeviceSummary,
    ResolveEvidenceIn,
    ResolvedEvidence,
    SafeSelectIn,
    SafeSelectOut,
    ToolResult,
    WhoHasAccessIn,
)

MAX_ROWS = 100


# ---------- helpers ----------


def _row_to_dict(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _person_row(db: ReadOnlyDB, person_id: str) -> Optional[dict]:
    r = db.one("SELECT * FROM people WHERE person_id = ?", (person_id,))
    return _row_to_dict(r) if r else None


def _person_name(db: ReadOnlyDB, person_id: Optional[str]) -> Optional[str]:
    if not person_id:
        return None
    r = db.one("SELECT full_name FROM people WHERE person_id = ?", (person_id,))
    return r["full_name"] if r else None


# ---------- find_person ----------


def find_person(inp: FindPersonIn, db: Optional[ReadOnlyDB] = None) -> ToolResult:
    """Resolve a person from name, email, handle, or ID (fuzzy)."""
    db = db or get_db()
    q = (inp.query or "").strip()
    if not q:
        return ToolResult(data=[], notes=["empty query"])

    pat = f"%{q.lower()}%"
    exact_pat = q.lower()

    # Direct hit by person_id
    hits: dict[str, PersonHit] = {}

    r = db.one("SELECT * FROM people WHERE lower(person_id) = ?", (exact_pat,))
    if r:
        hits[r["person_id"]] = PersonHit(
            person_id=r["person_id"],
            full_name=r["full_name"],
            primary_email=r["primary_email"],
            department=r["department"],
            title=r["title"],
            employment_status=r["employment_status"],
            manager_person_id=r["manager_person_id"],
            end_date=r["end_date"],
            score=1.0,
            matched_on=["person_id"],
        )

    # people table by name/email
    rows = db.execute(
        "SELECT * FROM people "
        "WHERE lower(full_name) LIKE ? OR lower(primary_email) LIKE ? "
        "ORDER BY full_name LIMIT 50",
        (pat, pat),
    )
    for r in rows:
        pid = r["person_id"]
        score = 0.9 if exact_pat in (r["primary_email"] or "").lower() else 0.7
        matched = []
        if exact_pat in (r["full_name"] or "").lower():
            matched.append("name")
        if exact_pat in (r["primary_email"] or "").lower():
            matched.append("email")
        if pid in hits:
            hits[pid].score = max(hits[pid].score, score)
            hits[pid].matched_on = list(set(hits[pid].matched_on + matched))
            continue
        hits[pid] = PersonHit(
            person_id=pid,
            full_name=r["full_name"],
            primary_email=r["primary_email"],
            department=r["department"],
            title=r["title"],
            employment_status=r["employment_status"],
            manager_person_id=r["manager_person_id"],
            end_date=r["end_date"],
            score=score,
            matched_on=matched or ["name"],
        )

    # by account handle in each system
    for sql, matched_on in [
        (
            "SELECT ia.person_id FROM idp_accounts ia "
            "WHERE lower(ia.username) LIKE ? AND ia.person_id IS NOT NULL",
            "idp_handle",
        ),
        (
            "SELECT wa.person_id FROM workspace_accounts wa "
            "WHERE lower(wa.primary_email) LIKE ? AND wa.person_id IS NOT NULL",
            "workspace_email",
        ),
        (
            "SELECT gh.person_id FROM github_accounts gh "
            "WHERE lower(gh.login) LIKE ? AND gh.person_id IS NOT NULL",
            "github_login",
        ),
    ]:
        for r in db.execute(sql, (pat,))[:50]:
            pid = r["person_id"]
            if pid in hits:
                hits[pid].matched_on = list(set(hits[pid].matched_on + [matched_on]))
                hits[pid].score = max(hits[pid].score, 0.75)
                continue
            pr = _person_row(db, pid)
            if not pr:
                continue
            hits[pid] = PersonHit(
                person_id=pid,
                full_name=pr["full_name"],
                primary_email=pr["primary_email"],
                department=pr["department"],
                title=pr["title"],
                employment_status=pr["employment_status"],
                manager_person_id=pr["manager_person_id"],
                end_date=pr["end_date"],
                score=0.75,
                matched_on=[matched_on],
            )

    ordered = sorted(hits.values(), key=lambda h: h.score, reverse=True)[: inp.limit]
    evidence = [EvidenceRef(kind="person", id=h.person_id) for h in ordered]
    return ToolResult(
        data=[h.model_dump() for h in ordered],
        evidence=evidence,
        notes=[f"query='{q}' -> {len(ordered)} hit(s)"],
    )


# ---------- get_person_profile ----------


def get_person_profile(
    person_id: str, db: Optional[ReadOnlyDB] = None
) -> ToolResult:
    """Return HR + linked accounts + devices + high-level counts."""
    db = db or get_db()
    p = _person_row(db, person_id)
    if not p:
        return ToolResult(data=None, notes=[f"no person with id {person_id}"])

    accounts: list[LinkedAccount] = []
    ev: list[EvidenceRef] = [EvidenceRef(kind="person", id=person_id)]

    for r in db.execute(
        "SELECT account_id, username, status, last_login_at FROM idp_accounts "
        "WHERE person_id = ?",
        (person_id,),
    ):
        accounts.append(
            LinkedAccount(
                system="idp",
                account_id=r["account_id"],
                handle=r["username"],
                status=r["status"],
                last_login_at=r["last_login_at"],
            )
        )
        ev.append(EvidenceRef(kind="idp_account", id=r["account_id"]))

    for r in db.execute(
        "SELECT account_id, primary_email, status, last_login_at "
        "FROM workspace_accounts WHERE person_id = ?",
        (person_id,),
    ):
        accounts.append(
            LinkedAccount(
                system="workspace",
                account_id=r["account_id"],
                handle=r["primary_email"],
                status=r["status"],
                last_login_at=r["last_login_at"],
            )
        )
        ev.append(EvidenceRef(kind="workspace_account", id=r["account_id"]))

    for r in db.execute(
        "SELECT account_id, login, status, last_active_at "
        "FROM github_accounts WHERE person_id = ?",
        (person_id,),
    ):
        accounts.append(
            LinkedAccount(
                system="github",
                account_id=r["account_id"],
                handle=r["login"],
                status=r["status"],
                last_login_at=r["last_active_at"],
            )
        )
        ev.append(EvidenceRef(kind="github_account", id=r["account_id"]))

    devices: list[DeviceSummary] = []
    for r in db.execute(
        "SELECT device_id, platform, ownership, compliance_status, "
        "last_check_in_at, retired_at FROM devices WHERE person_id = ?",
        (person_id,),
    ):
        devices.append(
            DeviceSummary(
                device_id=r["device_id"],
                platform=r["platform"],
                ownership=r["ownership"],
                compliance_status=r["compliance_status"],
                last_check_in_at=r["last_check_in_at"],
                retired_at=r["retired_at"],
            )
        )
        ev.append(EvidenceRef(kind="device", id=r["device_id"]))

    # counts (effective)
    grants = list_access_for_person(
        ListAccessIn(person_id=person_id, include_revoked=False), db=db
    )
    counts = {
        "effective_grants": len(grants.data or []),
        "idp_accounts": sum(1 for a in accounts if a.system == "idp"),
        "workspace_accounts": sum(1 for a in accounts if a.system == "workspace"),
        "github_accounts": sum(1 for a in accounts if a.system == "github"),
        "devices_active": sum(1 for d in devices if not d.retired_at),
    }

    profile = PersonProfile(
        person=p,
        accounts=accounts,
        devices=devices,
        counts=counts,
    )
    return ToolResult(data=profile.model_dump(), evidence=ev)


# ---------- list_access_for_person ----------


def _app_row(db: ReadOnlyDB, app_id: str) -> Optional[dict]:
    r = db.one("SELECT * FROM applications WHERE application_id = ?", (app_id,))
    return _row_to_dict(r) if r else None


def _repo_row(db: ReadOnlyDB, repo_id: str) -> Optional[dict]:
    r = db.one(
        "SELECT * FROM github_repositories WHERE repository_id = ?", (repo_id,)
    )
    return _row_to_dict(r) if r else None


def _drive_row(db: ReadOnlyDB, res_id: str) -> Optional[dict]:
    r = db.one("SELECT * FROM drive_resources WHERE resource_id = ?", (res_id,))
    return _row_to_dict(r) if r else None


def _idp_group_row(db: ReadOnlyDB, gid: str) -> Optional[dict]:
    r = db.one("SELECT * FROM idp_groups WHERE group_id = ?", (gid,))
    return _row_to_dict(r) if r else None


def list_access_for_person(
    inp: ListAccessIn, db: Optional[ReadOnlyDB] = None
) -> ToolResult:
    """Return every access grant a person has, with the grant chain (path)."""
    db = db or get_db()
    person_id = inp.person_id
    person_ref = EvidenceRef(kind="person", id=person_id)
    systems = set(inp.systems) if inp.systems else None
    grants: list[AccessGrant] = []
    ev: list[EvidenceRef] = [person_ref]

    # collect linked accounts
    idp_accts = db.execute(
        "SELECT account_id, username FROM idp_accounts WHERE person_id = ?",
        (person_id,),
    )
    ws_accts = db.execute(
        "SELECT account_id, primary_email FROM workspace_accounts WHERE person_id = ?",
        (person_id,),
    )
    gh_accts = db.execute(
        "SELECT account_id, login FROM github_accounts WHERE person_id = ?",
        (person_id,),
    )

    # ---------- IdP: applications via direct or via groups ----------
    if not systems or "idp" in systems:
        for a in idp_accts:
            aid = a["account_id"]
            acct_ref = EvidenceRef(kind="idp_account", id=aid)

            # 1a: direct idp_app_assignments to the account
            for r in db.execute(
                "SELECT * FROM idp_app_assignments "
                "WHERE principal_type='account' AND principal_id=?",
                (aid,),
            ):
                effective = db.is_effective(r["revoked_at"], None)
                if not effective and not inp.include_revoked:
                    continue
                app = _app_row(db, r["application_id"])
                if not app:
                    continue
                grants.append(
                    AccessGrant(
                        system="idp",
                        resource_kind="application",
                        resource_id=r["application_id"],
                        resource_name=app["name"],
                        role=r["role"],
                        granted_at=r["granted_at"],
                        revoked_at=r["revoked_at"],
                        effective=effective,
                        source="direct",
                        path=[
                            acct_ref,
                            EvidenceRef(kind="application", id=r["application_id"]),
                        ],
                        evidence=[
                            EvidenceRef(kind="idp_app_assignment", id=r["assignment_id"]),
                            EvidenceRef(kind="application", id=r["application_id"]),
                            acct_ref,
                        ],
                    )
                )

            # 1b: via IdP groups (with nesting)
            group_ids = db.idp_groups_for_account(aid)
            for gid in sorted(group_ids):
                for r in db.execute(
                    "SELECT * FROM idp_app_assignments "
                    "WHERE principal_type='group' AND principal_id=?",
                    (gid,),
                ):
                    effective = db.is_effective(r["revoked_at"], None)
                    if not effective and not inp.include_revoked:
                        continue
                    app = _app_row(db, r["application_id"])
                    if not app:
                        continue
                    membership_path = db.idp_membership_path(aid, gid)
                    if not membership_path:
                        continue
                    direct = len(membership_path) == 1
                    source = "group" if direct else "nested_group"
                    path = [acct_ref]
                    evidence = [
                        EvidenceRef(kind="idp_app_assignment", id=r["assignment_id"]),
                        EvidenceRef(kind="application", id=r["application_id"]),
                        acct_ref,
                    ]
                    for membership_id, _, _, parent_gid in membership_path:
                        membership_ref = EvidenceRef(
                            kind="idp_group_membership", id=membership_id
                        )
                        group_ref = EvidenceRef(kind="idp_group", id=parent_gid)
                        path.extend([membership_ref, group_ref])
                        evidence.extend([membership_ref, group_ref])
                    path.append(EvidenceRef(kind="application", id=r["application_id"]))
                    grants.append(
                        AccessGrant(
                            system="idp",
                            resource_kind="application",
                            resource_id=r["application_id"],
                            resource_name=app["name"],
                            role=r["role"],
                            granted_at=r["granted_at"],
                            revoked_at=r["revoked_at"],
                            effective=effective,
                            source=source,
                            path=path,
                            evidence=evidence,
                        )
                    )

            # 1c: application_user_access (what the app reports for this account)
            for r in db.execute(
                "SELECT * FROM application_user_access WHERE idp_account_id = ?",
                (aid,),
            ):
                effective = (r["status"] == "active") and db.is_effective(
                    r["revoked_at"], None
                )
                if not effective and not inp.include_revoked:
                    continue
                app = _app_row(db, r["application_id"])
                if not app:
                    continue
                src = "scim" if r["provisioning_source"] == "idp_scim" else "direct"
                grants.append(
                    AccessGrant(
                        system="idp",
                        resource_kind="application",
                        resource_id=r["application_id"],
                        resource_name=app["name"] + " (app-reported)",
                        role=r["role"],
                        granted_at=r["assigned_at"],
                        revoked_at=r["revoked_at"],
                        effective=effective,
                        source=src,
                        path=[
                            acct_ref,
                            EvidenceRef(kind="application", id=r["application_id"]),
                        ],
                        evidence=[
                            EvidenceRef(
                                kind="application_user_access", id=r["access_id"]
                            ),
                            EvidenceRef(kind="application", id=r["application_id"]),
                            acct_ref,
                        ],
                    )
                )

    # ---------- GitHub: teams -> repos, and direct repo collaborators ----------
    if not systems or "github" in systems:
        for a in gh_accts:
            aid = a["account_id"]
            acct_ref = EvidenceRef(kind="github_account", id=aid)

            # team memberships
            teams = db.execute(
                "SELECT * FROM github_team_memberships WHERE account_id = ?",
                (aid,),
            )
            for tm in teams:
                if not db.is_effective(tm["revoked_at"], None):
                    if not inp.include_revoked:
                        continue
                tid = tm["team_id"]
                team = db.one(
                    "SELECT * FROM github_teams WHERE team_id = ?", (tid,)
                )
                if not team:
                    continue
                # team's repos
                for tp in db.execute(
                    "SELECT * FROM github_team_repo_permissions WHERE team_id = ?",
                    (tid,),
                ):
                    effective = db.is_effective(tp["revoked_at"], None) and db.is_effective(
                        tm["revoked_at"], None
                    )
                    if not effective and not inp.include_revoked:
                        continue
                    repo = _repo_row(db, tp["repository_id"])
                    if not repo:
                        continue
                    grants.append(
                        AccessGrant(
                            system="github",
                            resource_kind="repo",
                            resource_id=tp["repository_id"],
                            resource_name=repo["name"],
                            role=tp["permission"],
                            granted_at=tp["granted_at"],
                            revoked_at=tp["revoked_at"] or tm["revoked_at"],
                            effective=effective,
                            source="team",
                            path=[
                                acct_ref,
                                EvidenceRef(kind="github_team", id=tid),
                                EvidenceRef(
                                    kind="github_repository", id=tp["repository_id"]
                                ),
                            ],
                            evidence=[
                                EvidenceRef(
                                    kind="github_team_membership",
                                    id=tm["membership_id"],
                                ),
                                EvidenceRef(
                                    kind="github_team_repo_permission",
                                    id=tp["permission_id"],
                                ),
                                EvidenceRef(kind="github_team", id=tid),
                                EvidenceRef(
                                    kind="github_repository", id=tp["repository_id"]
                                ),
                                acct_ref,
                            ],
                        )
                    )

            # direct collab
            for cr in db.execute(
                "SELECT * FROM github_repo_collaborators WHERE account_id = ?",
                (aid,),
            ):
                effective = db.is_effective(cr["revoked_at"], cr["expires_at"])
                if not effective and not inp.include_revoked:
                    continue
                repo = _repo_row(db, cr["repository_id"])
                if not repo:
                    continue
                grants.append(
                    AccessGrant(
                        system="github",
                        resource_kind="repo",
                        resource_id=cr["repository_id"],
                        resource_name=repo["name"],
                        role=cr["permission"],
                        granted_at=cr["granted_at"],
                        expires_at=cr["expires_at"],
                        revoked_at=cr["revoked_at"],
                        effective=effective,
                        source="collab",
                        path=[
                            acct_ref,
                            EvidenceRef(kind="github_repository", id=cr["repository_id"]),
                        ],
                        evidence=[
                            EvidenceRef(
                                kind="github_repo_collaborator",
                                id=cr["permission_id"],
                            ),
                            EvidenceRef(
                                kind="github_repository", id=cr["repository_id"]
                            ),
                            acct_ref,
                        ],
                    )
                )

    # ---------- Workspace: Drive perms via account or group ----------
    if not systems or "drive" in systems or "workspace" in systems:
        for a in ws_accts:
            aid = a["account_id"]
            acct_ref = EvidenceRef(kind="workspace_account", id=aid)
            # direct account principal
            for r in db.execute(
                "SELECT * FROM drive_permissions "
                "WHERE principal_type='account' AND principal_id=?",
                (aid,),
            ):
                effective = db.is_effective(r["revoked_at"], r["expires_at"])
                if not effective and not inp.include_revoked:
                    continue
                res = _drive_row(db, r["resource_id"])
                if not res:
                    continue
                grants.append(
                    AccessGrant(
                        system="drive",
                        resource_kind="drive_resource",
                        resource_id=r["resource_id"],
                        resource_name=res["name"] + f" ({res['classification']})",
                        role=r["role"],
                        granted_at=r["granted_at"],
                        expires_at=r["expires_at"],
                        revoked_at=r["revoked_at"],
                        effective=effective,
                        source="direct",
                        path=[
                            acct_ref,
                            EvidenceRef(kind="drive_resource", id=r["resource_id"]),
                        ],
                        evidence=[
                            EvidenceRef(kind="drive_permission", id=r["permission_id"]),
                            EvidenceRef(kind="drive_resource", id=r["resource_id"]),
                            acct_ref,
                        ],
                    )
                )
            # via groups
            group_ids = db.workspace_groups_for_account(aid)
            for gid in sorted(group_ids):
                membership_path = db.workspace_membership_path(aid, gid)
                if not membership_path:
                    continue
                for r in db.execute(
                    "SELECT * FROM drive_permissions "
                    "WHERE principal_type='group' AND principal_id=?",
                    (gid,),
                ):
                    effective = db.is_effective(r["revoked_at"], r["expires_at"])
                    if not effective and not inp.include_revoked:
                        continue
                    res = _drive_row(db, r["resource_id"])
                    if not res:
                        continue
                    path = [acct_ref]
                    evidence = [
                        EvidenceRef(kind="drive_permission", id=r["permission_id"]),
                        EvidenceRef(kind="drive_resource", id=r["resource_id"]),
                        acct_ref,
                    ]
                    for membership_id, _, _, parent_gid in membership_path:
                        membership_ref = EvidenceRef(
                            kind="workspace_group_membership", id=membership_id
                        )
                        group_ref = EvidenceRef(kind="workspace_group", id=parent_gid)
                        path.extend([membership_ref, group_ref])
                        evidence.extend([membership_ref, group_ref])
                    path.append(EvidenceRef(kind="drive_resource", id=r["resource_id"]))
                    grants.append(
                        AccessGrant(
                            system="drive",
                            resource_kind="drive_resource",
                            resource_id=r["resource_id"],
                            resource_name=res["name"] + f" ({res['classification']})",
                            role=r["role"],
                            granted_at=r["granted_at"],
                            expires_at=r["expires_at"],
                            revoked_at=r["revoked_at"],
                            effective=effective,
                            source=("group" if len(membership_path) == 1 else "nested_group"),
                            path=path,
                            evidence=evidence,
                        )
                    )

            # OAuth grants (third-party apps)
            if not systems or "oauth" in systems or "workspace" in systems:
                for r in db.execute(
                    "SELECT * FROM workspace_oauth_grants WHERE account_id = ?",
                    (aid,),
                ):
                    effective = db.is_effective(r["revoked_at"], None)
                    if not effective and not inp.include_revoked:
                        continue
                    scopes = []
                    try:
                        scopes = json.loads(r["scopes_json"])
                    except Exception:
                        pass
                    grants.append(
                        AccessGrant(
                            system="oauth",
                            resource_kind="oauth_scope",
                            resource_id=r["grant_id"],
                            resource_name=f"{r['application_name']} ({','.join(scopes) or 'no scopes'})",
                            role=None,
                            granted_at=r["granted_at"],
                            revoked_at=r["revoked_at"],
                            effective=effective,
                            source="oauth_grant",
                            path=[acct_ref],
                            evidence=[
                                EvidenceRef(
                                    kind="workspace_oauth_grant", id=r["grant_id"]
                                ),
                                acct_ref,
                            ],
                        )
                    )

    # dedupe by (resource_id, source, path key)
    seen: set[tuple] = set()
    dedup: list[AccessGrant] = []
    for g in grants:
        key = (g.system, g.resource_kind, g.resource_id, g.source, tuple(e.key() for e in g.path))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(g)

    matched_resources: list[EvidenceRef] = []
    if inp.resource_hint:
        hint = inp.resource_hint.strip().lower()
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
                matched_resources.append(EvidenceRef(kind=kind, id=row[id_col]))
        if hint in {"critical", "critical application", "critical applications"}:
            for row in db.execute(
                "SELECT application_id FROM applications "
                "WHERE lower(sensitivity) = 'critical' ORDER BY application_id"
            ):
                matched_resources.append(
                    EvidenceRef(kind="application", id=row["application_id"])
                )
        matched_resources = _dedupe_evidence(matched_resources)
        matched_ids = {ref.id for ref in matched_resources}
        # A zero catalog match is "resource not found", not proof that the
        # person lacks access. Return no unrelated grants and make that state
        # explicit in notes. When matches exist, filter by their stable IDs.
        dedup = [grant for grant in dedup if grant.resource_id in matched_ids]
        ev.extend(matched_resources)

    truncated = False
    if len(dedup) > MAX_ROWS:
        dedup = dedup[:MAX_ROWS]
        truncated = True
    for g in dedup:
        # Make each grant self-contained. A verifier (or human reviewer) must be
        # able to trace the resource back through the linked account to the
        # subject without relying on a separate top-level evidence list.
        if not g.path or g.path[0].key() != person_ref.key():
            g.path.insert(0, person_ref)
        if all(ref.key() != person_ref.key() for ref in g.evidence):
            g.evidence.insert(0, person_ref)
        ev.extend(g.evidence)
    return ToolResult(
        data=[g.model_dump() for g in dedup],
        evidence=_dedupe_evidence(ev),
        truncated=truncated,
        notes=[
            f"{len(dedup)} effective/relevant grant(s) for person {person_id}",
            (
                f"resource_hint={inp.resource_hint!r} matched "
                f"{len(matched_resources)} catalog resource(s)"
                if inp.resource_hint
                else "no resource filter applied"
            ),
            (
                "zero catalog matches means resource not found; it does not prove absence of access"
                if inp.resource_hint and not matched_resources
                else "resource filter resolved against the catalog"
            ),
            (
                f"effective=true means effective at snapshot {db.snapshot_time_iso}; "
                "granted_at only records when the grant began"
            ),
        ],
    )


def _account_directly_in_idp_group(db: ReadOnlyDB, account_id: str, group_id: str) -> bool:
    r = db.one(
        "SELECT membership_id FROM idp_group_memberships "
        "WHERE group_id=? AND member_type='account' AND member_id=? "
        "AND revoked_at IS NULL",
        (group_id, account_id),
    )
    return r is not None


def _dedupe_evidence(refs: list[EvidenceRef]) -> list[EvidenceRef]:
    seen = set()
    out = []
    for r in refs:
        k = r.key()
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# ---------- expand_group ----------


def expand_group(inp: ExpandGroupIn, db: Optional[ReadOnlyDB] = None) -> ToolResult:
    db = db or get_db()
    kind = "idp_group" if inp.system == "idp" else "workspace_group"
    account_kind = "idp_account" if inp.system == "idp" else "workspace_account"
    membership_kind = (
        "idp_group_membership"
        if inp.system == "idp"
        else "workspace_group_membership"
    )
    table = (
        "idp_group_memberships"
        if inp.system == "idp"
        else "workspace_group_memberships"
    )
    root = EvidenceRef(kind=kind, id=inp.group_id)
    evidence: list[EvidenceRef] = [root]
    cycles: set[str] = set()
    truncated = False

    if inp.direction == "members":
        rows = db.execute(
            f"SELECT membership_id, group_id, member_type, member_id, revoked_at FROM {table}"
        )
        children: dict[str, list] = {}
        for row in rows:
            if db.is_effective(row["revoked_at"], None):
                children.setdefault(row["group_id"], []).append(row)
        direct_refs: list[EvidenceRef] = []
        account_ids: set[str] = set()
        depth_reached = 0
        queue: list[tuple[str, int, tuple[str, ...]]] = [
            (inp.group_id, 0, (inp.group_id,))
        ]
        visited_at_depth: dict[str, int] = {inp.group_id: 0}
        while queue:
            group_id, group_depth, ancestry = queue.pop(0)
            for row in children.get(group_id, []):
                member_depth = group_depth + 1
                membership_ref = EvidenceRef(
                    kind=membership_kind, id=row["membership_id"]
                )
                evidence.append(membership_ref)
                member_type, member_id = row["member_type"], row["member_id"]
                member_ref = EvidenceRef(
                    kind=account_kind if member_type == "account" else kind,
                    id=member_id,
                )
                if group_depth == 0:
                    direct_refs.append(member_ref)
                depth_reached = max(depth_reached, member_depth)
                if member_type == "account":
                    account_ids.add(member_id)
                    evidence.append(member_ref)
                    continue
                evidence.append(member_ref)
                if member_id in ancestry:
                    cycles.add(" -> ".join((*ancestry, member_id)))
                    continue
                if member_depth >= inp.max_depth:
                    if children.get(member_id):
                        truncated = True
                    continue
                previous_depth = visited_at_depth.get(member_id)
                if previous_depth is not None and previous_depth <= member_depth:
                    continue
                visited_at_depth[member_id] = member_depth
                queue.append((member_id, member_depth, (*ancestry, member_id)))
        transitive_refs = [
            EvidenceRef(kind=account_kind, id=account_id)
            for account_id in sorted(account_ids)
        ]
        data = GroupExpansion(
            root=root,
            direct_members=direct_refs,
            transitive_members=transitive_refs,
            depth_reached=min(depth_reached, inp.max_depth),
            cycles_detected=sorted(cycles),
        )
    else:
        rows = db.execute(
            f"SELECT membership_id, group_id, member_id, revoked_at FROM {table} "
            "WHERE member_type='group'"
        )
        parents: dict[str, list] = {}
        for row in rows:
            if db.is_effective(row["revoked_at"], None):
                parents.setdefault(row["member_id"], []).append(row)
        direct_refs: list[EvidenceRef] = []
        container_ids: set[str] = set()
        depth_reached = 0
        queue = [(inp.group_id, 0, (inp.group_id,))]
        visited_at_depth = {inp.group_id: 0}
        while queue:
            group_id, group_depth, ancestry = queue.pop(0)
            for row in parents.get(group_id, []):
                parent_depth = group_depth + 1
                parent_id = row["group_id"]
                parent_ref = EvidenceRef(kind=kind, id=parent_id)
                evidence.extend(
                    [
                        EvidenceRef(kind=membership_kind, id=row["membership_id"]),
                        parent_ref,
                    ]
                )
                if group_depth == 0:
                    direct_refs.append(parent_ref)
                container_ids.add(parent_id)
                depth_reached = max(depth_reached, parent_depth)
                if parent_id in ancestry:
                    cycles.add(" -> ".join((*ancestry, parent_id)))
                    continue
                if parent_depth >= inp.max_depth:
                    if parents.get(parent_id):
                        truncated = True
                    continue
                previous_depth = visited_at_depth.get(parent_id)
                if previous_depth is not None and previous_depth <= parent_depth:
                    continue
                visited_at_depth[parent_id] = parent_depth
                queue.append((parent_id, parent_depth, (*ancestry, parent_id)))
        containers = [
            EvidenceRef(kind=kind, id=group_id)
            for group_id in sorted(container_ids)
        ]
        data = GroupExpansion(
            root=root,
            direct_members=direct_refs,
            transitive_members=containers,
            depth_reached=min(depth_reached, inp.max_depth),
            cycles_detected=sorted(cycles),
        )
    return ToolResult(
        data=data.model_dump(),
        evidence=_dedupe_evidence(evidence),
        truncated=truncated,
        notes=[
            f"expanded through depth {data.depth_reached}"
            + (f"; {len(cycles)} cycle(s) detected" if cycles else "")
            + ("; max_depth reached" if truncated else "")
        ],
    )


# ---------- who_has_access_to ----------


def who_has_access_to(
    inp: WhoHasAccessIn, db: Optional[ReadOnlyDB] = None
) -> ToolResult:
    db = db or get_db()
    grantees: list[Grantee] = []
    ev: list[EvidenceRef] = []

    if inp.resource_kind == "application":
        app_ref = EvidenceRef(kind="application", id=inp.resource_id)
        ev.append(app_ref)
        # 1: idp_app_assignments direct/group
        for r in db.execute(
            "SELECT * FROM idp_app_assignments WHERE application_id = ?",
            (inp.resource_id,),
        ):
            if not db.is_effective(r["revoked_at"], None):
                continue
            if r["principal_type"] == "account":
                _add_account_grantee(
                    db, grantees, r["principal_id"], r, app_ref, "direct"
                )
            else:
                gid = r["principal_id"]
                for acct_id in sorted(db.idp_group_members(gid)):
                    _add_account_grantee(
                        db, grantees, acct_id, r, app_ref, "group", via_group=gid
                    )
        # 2: application_user_access (what app reports)
        for r in db.execute(
            "SELECT * FROM application_user_access WHERE application_id = ? AND status='active'",
            (inp.resource_id,),
        ):
            if not db.is_effective(r["revoked_at"], None):
                continue
            acct = db.one(
                "SELECT * FROM idp_accounts WHERE account_id = ?",
                (r["idp_account_id"],),
            )
            if not acct:
                continue
            grantees.append(
                Grantee(
                    person_id=acct["person_id"],
                    person_name=_person_name(db, acct["person_id"]),
                    account=EvidenceRef(kind="idp_account", id=acct["account_id"]),
                    role=r["role"],
                    source=f"app_reported/{r['provisioning_source']}",
                    path=[
                        EvidenceRef(kind="idp_account", id=acct["account_id"]),
                        app_ref,
                    ],
                    evidence=[
                        EvidenceRef(kind="application_user_access", id=r["access_id"]),
                        app_ref,
                        EvidenceRef(kind="idp_account", id=acct["account_id"]),
                    ],
                )
            )
    elif inp.resource_kind == "github_repository":
        repo_ref = EvidenceRef(kind="github_repository", id=inp.resource_id)
        ev.append(repo_ref)
        # team perms
        for tp in db.execute(
            "SELECT * FROM github_team_repo_permissions WHERE repository_id = ?",
            (inp.resource_id,),
        ):
            if not db.is_effective(tp["revoked_at"], None):
                continue
            tid = tp["team_id"]
            for tm in db.execute(
                "SELECT * FROM github_team_memberships WHERE team_id = ?", (tid,)
            ):
                if not db.is_effective(tm["revoked_at"], None):
                    continue
                gh_acct = db.one(
                    "SELECT * FROM github_accounts WHERE account_id = ?",
                    (tm["account_id"],),
                )
                if not gh_acct:
                    continue
                grantees.append(
                    Grantee(
                        person_id=gh_acct["person_id"],
                        person_name=_person_name(db, gh_acct["person_id"]),
                        account=EvidenceRef(
                            kind="github_account", id=gh_acct["account_id"]
                        ),
                        role=tp["permission"],
                        source="team",
                        path=[
                            EvidenceRef(
                                kind="github_account", id=gh_acct["account_id"]
                            ),
                            EvidenceRef(kind="github_team", id=tid),
                            repo_ref,
                        ],
                        evidence=[
                            EvidenceRef(
                                kind="github_team_membership", id=tm["membership_id"]
                            ),
                            EvidenceRef(
                                kind="github_team_repo_permission",
                                id=tp["permission_id"],
                            ),
                            EvidenceRef(kind="github_team", id=tid),
                            repo_ref,
                        ],
                    )
                )
        # direct collab
        for cr in db.execute(
            "SELECT * FROM github_repo_collaborators WHERE repository_id = ?",
            (inp.resource_id,),
        ):
            if not db.is_effective(cr["revoked_at"], cr["expires_at"]):
                continue
            gh_acct = db.one(
                "SELECT * FROM github_accounts WHERE account_id = ?",
                (cr["account_id"],),
            )
            if not gh_acct:
                continue
            grantees.append(
                Grantee(
                    person_id=gh_acct["person_id"],
                    person_name=_person_name(db, gh_acct["person_id"]),
                    account=EvidenceRef(
                        kind="github_account", id=gh_acct["account_id"]
                    ),
                    role=cr["permission"],
                    source="collab",
                    path=[
                        EvidenceRef(kind="github_account", id=gh_acct["account_id"]),
                        repo_ref,
                    ],
                    evidence=[
                        EvidenceRef(
                            kind="github_repo_collaborator", id=cr["permission_id"]
                        ),
                        repo_ref,
                        EvidenceRef(kind="github_account", id=gh_acct["account_id"]),
                    ],
                )
            )
    elif inp.resource_kind == "drive_resource":
        res_ref = EvidenceRef(kind="drive_resource", id=inp.resource_id)
        ev.append(res_ref)
        for r in db.execute(
            "SELECT * FROM drive_permissions WHERE resource_id = ?",
            (inp.resource_id,),
        ):
            if not db.is_effective(r["revoked_at"], r["expires_at"]):
                continue
            pt = r["principal_type"]
            pid = r["principal_id"]
            perm_ref = EvidenceRef(kind="drive_permission", id=r["permission_id"])
            if pt == "account":
                acct = db.one(
                    "SELECT * FROM workspace_accounts WHERE account_id = ?", (pid,)
                )
                if acct:
                    grantees.append(
                        Grantee(
                            person_id=acct["person_id"],
                            person_name=_person_name(db, acct["person_id"]),
                            account=EvidenceRef(
                                kind="workspace_account", id=acct["account_id"]
                            ),
                            role=r["role"],
                            source="direct",
                            path=[
                                EvidenceRef(
                                    kind="workspace_account", id=acct["account_id"]
                                ),
                                res_ref,
                            ],
                            evidence=[perm_ref, res_ref, EvidenceRef(
                                kind="workspace_account", id=acct["account_id"]
                            )],
                        )
                    )
            elif pt == "group":
                for acct_id in sorted(db.workspace_group_members(pid)):
                    acct = db.one(
                        "SELECT * FROM workspace_accounts WHERE account_id = ?",
                        (acct_id,),
                    )
                    if not acct:
                        continue
                    membership_path = db.workspace_membership_path(acct_id, pid)
                    if not membership_path:
                        continue
                    account_ref = EvidenceRef(
                        kind="workspace_account", id=acct["account_id"]
                    )
                    path = [account_ref]
                    evidence = [perm_ref, res_ref, account_ref]
                    for membership_id, _, _, parent_gid in membership_path:
                        membership_ref = EvidenceRef(
                            kind="workspace_group_membership", id=membership_id
                        )
                        group_ref = EvidenceRef(kind="workspace_group", id=parent_gid)
                        path.extend([membership_ref, group_ref])
                        evidence.extend([membership_ref, group_ref])
                    path.append(res_ref)
                    grantees.append(
                        Grantee(
                            person_id=acct["person_id"],
                            person_name=_person_name(db, acct["person_id"]),
                            account=EvidenceRef(
                                kind="workspace_account", id=acct["account_id"]
                            ),
                            role=r["role"],
                            source=("group" if len(membership_path) == 1 else "nested_group"),
                            path=path,
                            evidence=evidence,
                        )
                    )
            elif pt in ("domain", "external_email"):
                if not inp.include_external:
                    continue
                grantees.append(
                    Grantee(
                        person_id=None,
                        person_name=None,
                        account=None,
                        external_principal=pid,
                        role=r["role"],
                        source=f"external/{pt}",
                        path=[res_ref],
                        evidence=[perm_ref, res_ref],
                    )
                )

    # dedupe
    seen = set()
    unique: list[Grantee] = []
    for g in grantees:
        k = (
            g.person_id,
            g.account.key() if g.account else None,
            g.external_principal,
            g.source,
            g.role,
            tuple(e.key() for e in g.path),
        )
        if k in seen:
            continue
        seen.add(k)
        unique.append(g)

    truncated = False
    if len(unique) > MAX_ROWS:
        unique = unique[:MAX_ROWS]
        truncated = True
    for grantee in unique:
        ev.extend(grantee.evidence)
    return ToolResult(
        data=[g.model_dump() for g in unique],
        evidence=_dedupe_evidence(ev),
        truncated=truncated,
        notes=[f"{len(unique)} grantee(s) for {inp.resource_kind}:{inp.resource_id}"],
    )


def _add_account_grantee(
    db: ReadOnlyDB,
    out: list[Grantee],
    account_id: str,
    assignment_row,
    app_ref: EvidenceRef,
    source: str,
    via_group: Optional[str] = None,
):
    acct = db.one("SELECT * FROM idp_accounts WHERE account_id = ?", (account_id,))
    if not acct:
        return
    account_ref = EvidenceRef(kind="idp_account", id=account_id)
    path = [account_ref]
    ev = [
        EvidenceRef(kind="idp_app_assignment", id=assignment_row["assignment_id"]),
        app_ref,
        account_ref,
    ]
    if via_group:
        membership_path = db.idp_membership_path(account_id, via_group)
        if not membership_path:
            return
        source = "group" if len(membership_path) == 1 else "nested_group"
        for membership_id, _, _, parent_gid in membership_path:
            membership_ref = EvidenceRef(
                kind="idp_group_membership", id=membership_id
            )
            group_ref = EvidenceRef(kind="idp_group", id=parent_gid)
            path.extend([membership_ref, group_ref])
            ev.extend([membership_ref, group_ref])
    path.append(app_ref)
    out.append(
        Grantee(
            person_id=acct["person_id"],
            person_name=_person_name(db, acct["person_id"]),
            account=EvidenceRef(kind="idp_account", id=account_id),
            role=assignment_row["role"],
            source=source,
            path=path,
            evidence=ev,
        )
    )


# ---------- get_audit_trail ----------


def _encode_audit_cursor(occurred_at: str, event_id: str) -> str:
    payload = json.dumps([occurred_at, event_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_audit_cursor(cursor: str) -> tuple[str, Optional[str]]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        if (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(item, str) for item in value)
        ):
            return value[0], value[1]
    except Exception:
        pass
    # Backward compatibility with the original timestamp-only cursor.
    return cursor, None


def get_audit_trail(
    inp: GetAuditTrailIn, db: Optional[ReadOnlyDB] = None
) -> ToolResult:
    db = db or get_db()
    clauses = []
    params: list = []
    if inp.target_id:
        clauses.append("target_id = ?")
        params.append(inp.target_id)
    if inp.actor_id:
        clauses.append("actor_id = ?")
        params.append(inp.actor_id)
    if inp.since:
        clauses.append("occurred_at >= ?")
        params.append(inp.since)
    if inp.until:
        clauses.append("occurred_at <= ?")
        params.append(inp.until)
    if inp.systems:
        clauses.append(
            "system IN (" + ",".join("?" * len(inp.systems)) + ")"
        )
        params.extend(inp.systems)
    if inp.event_types:
        clauses.append(
            "event_type IN (" + ",".join("?" * len(inp.event_types)) + ")"
        )
        params.extend(inp.event_types)
    if inp.cursor:
        cursor_time, cursor_event_id = _decode_audit_cursor(inp.cursor)
        if cursor_event_id:
            clauses.append("(occurred_at < ? OR (occurred_at = ? AND event_id < ?))")
            params.extend([cursor_time, cursor_time, cursor_event_id])
        else:
            clauses.append("occurred_at < ?")
            params.append(cursor_time)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = min(inp.limit, MAX_ROWS)
    sql = (
        "SELECT * FROM audit_events " + where +
        " ORDER BY occurred_at DESC, event_id DESC LIMIT ?"
    )
    params.append(limit + 1)
    rows = db.execute(sql, params)
    has_more = len(rows) > limit
    rows = rows[:limit]
    out: list[AuditEventOut] = []
    ev: list[EvidenceRef] = []
    for r in rows:
        try:
            details = json.loads(r["details_json"])
        except Exception:
            details = {}
        out.append(
            AuditEventOut(
                event_id=r["event_id"],
                occurred_at=r["occurred_at"],
                system=r["system"],
                event_type=r["event_type"],
                actor_type=r["actor_type"],
                actor_id=r["actor_id"],
                target_type=r["target_type"],
                target_id=r["target_id"],
                outcome=r["outcome"],
                source=r["source"],
                ip_address=r["ip_address"],
                correlation_id=r["correlation_id"],
                details=details,
            )
        )
        ev.append(EvidenceRef(kind="audit_event", id=r["event_id"]))
    next_cursor = (
        _encode_audit_cursor(out[-1].occurred_at, out[-1].event_id)
        if has_more and out
        else None
    )
    return ToolResult(
        data={
            "events": [e.model_dump() for e in out],
            "next_cursor": next_cursor,
        },
        evidence=ev,
        truncated=has_more,
        notes=[f"{len(out)} event(s)" + (" (more available)" if has_more else "")],
    )


# ---------- find_disagreements ----------


def find_disagreements(
    inp: FindDisagreementsIn, db: Optional[ReadOnlyDB] = None
) -> ToolResult:
    db = db or get_db()
    from agent.tools.scanners import run_scanner

    findings: list[Finding] = []
    for s in inp.scanners:
        findings.extend(run_scanner(s, db, limit=inp.limit_per_scanner))
    ev: list[EvidenceRef] = []
    for f in findings:
        ev.append(f.subject)
        ev.extend(f.evidence)
    return ToolResult(
        data=[f.model_dump() for f in findings],
        evidence=_dedupe_evidence(ev),
        notes=[
            f"{len(findings)} finding(s) across {len(inp.scanners)} scanner(s)"
        ],
    )


# ---------- safe_select ----------

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|create|replace|pragma|vacuum|reindex|analyze)\b",
    re.IGNORECASE,
)


def safe_select(inp: SafeSelectIn, db: Optional[ReadOnlyDB] = None) -> ToolResult:
    db = db or get_db()
    sql = inp.sql.strip().rstrip(";")
    # naive keyword check
    if _FORBIDDEN_KEYWORDS.search(sql):
        return ToolResult(
            data=None,
            notes=["rejected: contains forbidden keyword"],
        )
    # AST check
    try:
        parsed = sqlglot.parse(sql, read="sqlite")
    except Exception as e:
        return ToolResult(data=None, notes=[f"rejected: parse error {e}"])
    if len(parsed) != 1:
        return ToolResult(
            data=None, notes=["rejected: only a single SELECT statement is allowed"]
        )
    stmt = parsed[0]
    if not isinstance(stmt, exp.Select) and not isinstance(stmt, exp.Union):
        return ToolResult(
            data=None,
            notes=[f"rejected: root node is {type(stmt).__name__}, not SELECT"],
        )
    # Inject / cap LIMIT
    limit_val = min(inp.row_limit, MAX_ROWS)
    if not stmt.args.get("limit"):
        stmt.set("limit", exp.Limit(expression=exp.Literal.number(limit_val)))
    else:
        try:
            current = int(stmt.args["limit"].expression.this)
            if current > limit_val:
                stmt.set("limit", exp.Limit(expression=exp.Literal.number(limit_val)))
        except Exception:
            stmt.set("limit", exp.Limit(expression=exp.Literal.number(limit_val)))
    guarded = stmt.sql(dialect="sqlite")
    try:
        cur = db.conn.execute(guarded)
    except Exception as e:
        return ToolResult(data=None, notes=[f"execution error: {e}"])
    cols = [d[0] for d in (cur.description or [])]
    rows = [list(r) for r in cur.fetchmany(limit_val)]
    # cap total bytes
    total_bytes = sum(len(json.dumps(r, default=str)) for r in rows)
    truncated = False
    if total_bytes > 32000:
        while rows and sum(len(json.dumps(r, default=str)) for r in rows) > 32000:
            rows.pop()
        truncated = True
    return ToolResult(
        data=SafeSelectOut(columns=cols, rows=rows).model_dump(),
        evidence=[],
        truncated=truncated,
        notes=[f"executed: {guarded[:200]}"],
    )


# ---------- resolve_evidence (Verifier-only) ----------

_PK_MAP: dict[str, tuple[str, str, list[str]]] = {
    # kind -> (table, pk_column, summary_columns)
    "person": ("people", "person_id", ["full_name", "primary_email", "employment_status", "end_date"]),
    "idp_account": ("idp_accounts", "account_id", ["username", "status", "last_login_at", "person_id"]),
    "workspace_account": ("workspace_accounts", "account_id", ["primary_email", "status", "person_id"]),
    "github_account": ("github_accounts", "account_id", ["login", "status", "person_id"]),
    "device": ("devices", "device_id", ["platform", "compliance_status", "retired_at", "person_id"]),
    "idp_group": ("idp_groups", "group_id", ["name", "management_type"]),
    "workspace_group": ("workspace_groups", "group_id", ["email", "name"]),
    "github_team": ("github_teams", "team_id", ["name", "source_idp_group_id"]),
    "github_repository": ("github_repositories", "repository_id", ["name", "visibility", "sensitivity", "archived"]),
    "application": ("applications", "application_id", ["name", "sensitivity", "default_mfa_requirement"]),
    "drive_resource": ("drive_resources", "resource_id", ["name", "resource_type", "classification"]),
    "drive_permission": ("drive_permissions", "permission_id", ["resource_id", "principal_type", "principal_id", "role", "revoked_at"]),
    "idp_app_assignment": ("idp_app_assignments", "assignment_id", ["application_id", "principal_type", "principal_id", "role", "revoked_at"]),
    "application_user_access": ("application_user_access", "access_id", ["application_id", "idp_account_id", "role", "status", "mfa_enrollment_status", "revoked_at"]),
    "idp_group_membership": ("idp_group_memberships", "membership_id", ["group_id", "member_type", "member_id", "source", "revoked_at"]),
    "workspace_group_membership": ("workspace_group_memberships", "membership_id", ["group_id", "member_type", "member_id", "role", "revoked_at"]),
    "github_org_membership": ("github_org_memberships", "membership_id", ["account_id", "org_role", "revoked_at"]),
    "github_team_membership": ("github_team_memberships", "membership_id", ["team_id", "account_id", "role", "revoked_at"]),
    "github_team_repo_permission": ("github_team_repo_permissions", "permission_id", ["team_id", "repository_id", "permission", "revoked_at"]),
    "github_repo_collaborator": ("github_repo_collaborators", "permission_id", ["account_id", "repository_id", "permission", "revoked_at", "expires_at"]),
    "workspace_oauth_grant": ("workspace_oauth_grants", "grant_id", ["account_id", "application_name", "scopes_json", "revoked_at", "last_used_at"]),
    "audit_event": ("audit_events", "event_id", ["occurred_at", "system", "event_type", "actor_type", "actor_id", "target_type", "target_id", "outcome"]),
}


def resolve_evidence(
    inp: ResolveEvidenceIn, db: Optional[ReadOnlyDB] = None
) -> ToolResult:
    db = db or get_db()
    out: list[ResolvedEvidence] = []
    for ref in inp.refs:
        info = _PK_MAP.get(ref.kind)
        if not info:
            out.append(
                ResolvedEvidence(ref=ref, exists=False, summary=f"unknown kind {ref.kind}")
            )
            continue
        table, pk, cols = info
        row = db.get_row(table, pk, ref.id)
        if not row:
            out.append(ResolvedEvidence(ref=ref, exists=False, summary="not found"))
            continue
        row_dict = _row_to_dict(row)
        summary_parts = [f"{c}={row_dict.get(c)!r}" for c in cols if c in row_dict]
        out.append(
            ResolvedEvidence(
                ref=ref,
                exists=True,
                summary=", ".join(summary_parts),
                row=row_dict,
            )
        )
    return ToolResult(
        data=[o.model_dump() for o in out],
        evidence=inp.refs,
    )


# ---------- registry for the agent runtime ----------


TOOL_REGISTRY: dict[str, tuple[Callable, type]] = {
    "find_person": (find_person, FindPersonIn),
    "get_person_profile": (
        lambda inp, db=None: get_person_profile(inp.person_id, db=db),
        # convenience wrapper input
        type("_GetPersonProfileIn", (), {}),  # placeholder; we handle below
    ),
    "list_access_for_person": (list_access_for_person, ListAccessIn),
    "expand_group": (expand_group, ExpandGroupIn),
    "who_has_access_to": (who_has_access_to, WhoHasAccessIn),
    "get_audit_trail": (get_audit_trail, GetAuditTrailIn),
    "find_disagreements": (find_disagreements, FindDisagreementsIn),
    "safe_select": (safe_select, SafeSelectIn),
    "resolve_evidence": (resolve_evidence, ResolveEvidenceIn),
}
