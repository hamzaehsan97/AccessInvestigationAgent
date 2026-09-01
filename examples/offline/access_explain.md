> Verified: yes — deterministic playbook, all IDs sourced from tool outputs.

# Investigation: Why does Ariel Chen have access to Snowflake?
Snapshot: 2026-08-15T12:00:00Z  |  Playbook: access_explain  |  Investigation status: succeeded

## Summary
Ariel Chen (`person:per_001945`, Software Engineer, Engineering, employment_status=ended) has 22 effective grant(s) in total. Filtered to 2 matching grant(s) for 'Snowflake'.

## Findings
### Finding 1 — Grant chain via nested_group to Snowflake (role=standard) (severity: info)
Path: `person:per_001945` → `idp_account:idpa_001945` → `idp_group_membership:idpm_009643` → `idp_group:idpg_000107` → `idp_group_membership:idpm_010243` → `idp_group:idpg_000114` → `application:app_0004`. Source category: **nested_group**. granted_at=2024-09-17T12:00:00Z, revoked_at=null, effective=True.
**Evidence:**
- `person:per_001945`
- `idp_app_assignment:idpaa_000003`
- `application:app_0004`
- `idp_account:idpa_001945`
- `idp_group_membership:idpm_009643`
- `idp_group:idpg_000107`
- `idp_group_membership:idpm_010243`
- `idp_group:idpg_000114`

### Finding 2 — Grant chain via scim to Snowflake (app-reported) (role=standard) (severity: info)
Path: `person:per_001945` → `idp_account:idpa_001945` → `application:app_0004`. Source category: **scim**. granted_at=2021-03-12T00:00:00Z, revoked_at=null, effective=True.
**Evidence:**
- `person:per_001945`
- `application_user_access:aua_007707`
- `application:app_0004`
- `idp_account:idpa_001945`

## Gaps / Uncertainty
- IdP assignments and application-reported access may disagree; the tool returns both paths when both exist so you can compare.
- Nested-group membership was expanded with cycle-safe traversal. The report shows the shortest discovered path and cites every membership edge on it.

## Recommended actions (advisory only)
- Person is ended (end_date=2026-08-03). Deactivate the underlying account(s) upstream of every grant chain above.
- Review each grant's `source` — grants labelled `nested_group` are the most commonly forgotten during offboarding.

— investigation_id: 7d07ec8a-e51d-4e33-9ce4-2c21d719f949
— status: succeeded  verified: True
— snapshot: 2026-08-15T12:00:00Z  dataset: 1.1.0
