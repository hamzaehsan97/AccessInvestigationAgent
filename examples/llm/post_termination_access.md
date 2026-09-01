> Captured from the submitted runtime on 2026-09-01.

> Verified: yes — 22 citations were observed and semantically checked.

# Investigation: Ariel Chen has access to critical applications after their end date.
Snapshot: 2026-08-15T12:00:00Z  |  Investigation status: succeeded

## Summary
Ariel Chen's employment ended on August 3, 2026. Despite this, they still have effective access to several critical applications as of the snapshot date. The access grants were established before their termination date and remain effective afterward, indicating that they retain access to these applications.

## Findings
### Finding 1 — Continued Access to Critical Applications (severity: high)
Ariel Chen has effective access to multiple critical applications even after their employment ended. The access grants were created before the termination date and remain effective as of the snapshot.
**Evidence:**
- `person:per_001945`, `idp_account:idpa_001945`, `application_user_access:aua_007709`, `application:app_0001`, `idp_group_membership:idpm_009644`, `idp_group:idpg_000046`, `idp_group_membership:idpm_010182`, `idp_group:idpg_000112`, `idp_app_assignment:idpaa_000001` — Google Workspace access granted on 2021-03-14.
- `person:per_001945`, `idp_account:idpa_001945`, `application_user_access:aua_007705`, `application:app_0002` — GitHub access granted on 2021-03-10.
- `person:per_001945`, `idp_account:idpa_001945`, `application_user_access:aua_007707`, `application:app_0004`, `idp_group_membership:idpm_009643`, `idp_group:idpg_000107`, `idp_group_membership:idpm_010243`, `idp_group:idpg_000114`, `idp_app_assignment:idpaa_000003` — Snowflake access granted on 2021-03-12.
- `person:per_001945`, `idp_account:idpa_001945`, `application_user_access:aua_007706`, `application:app_0005` — PagerDuty access granted on 2021-03-11.
- `person:per_001945`, `idp_account:idpa_001945`, `application_user_access:aua_007708`, `application:app_0007` — Corporate VPN access granted on 2021-03-13.

## Gaps / Uncertainty
- No evidence of any revocation of access after the end date, but the access remains effective as of the snapshot.

## Recommended actions (advisory only)
- Review access for `person:per_001945` to ensure compliance with offboarding policies.
- Consider revoking access to `application:app_0001`, `application:app_0002`, `application:app_0004`, `application:app_0005`, and `application:app_0007` to prevent unauthorized use.

— investigation_id: a5d36155-7682-4cfd-ad02-5beb269a240a
— status: succeeded  verified: True
— snapshot: 2026-08-15T12:00:00Z  dataset: 1.1.0
