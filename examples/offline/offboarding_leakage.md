> Verified: yes — deterministic playbook, all IDs sourced from DB scanner.

# Investigation: Which ended employees still have surviving access?
Snapshot: 2026-08-15T12:00:00Z  |  Playbook: offboarding_leakage  |  Investigation status: succeeded

## Summary
Scanned all `employment_status='ended'` people and returned the top 5 with surviving accounts, devices, application access, or post-end-date audit activity. Every finding cites the record and event IDs that support it.

## Findings
### Finding 1 — Ended employee retains access (severity: critical)
Talia Brooks (employment_status=ended, end_date=2026-07-08) still has 29 surviving artifact(s): idp_account=idpa_001942 (talia.brooks.1942@northstar.example, last_login=2026-06-21T12:00:00Z); github_account=gha_001942 (talia-brooks-1942, last_active=2026-06-22T12:00:00Z); grant=idp/application:app_0002 via=nested_group role=member; grant=idp/application:app_0006 via=nested_group role=standard; grant=idp/application:app_0003 via=nested_group role=standard; grant=idp/application:app_0006 via=scim role=standard ...
**Evidence:**
- `person:per_001942` — subject (ended employee)
- `idp_account:idpa_001942`
- `github_account:gha_001942`
- `idp_app_assignment:idpaa_000026`
- `application:app_0002`
- `idp_group_membership:idpm_009629`
- `idp_group:idpg_000018`
- `idp_group_membership:idpm_010248`
- `idp_group:idpg_000019`
- `idp_app_assignment:idpaa_000005`
- `application:app_0006`
- `idp_group_membership:idpm_009627`
- `idp_group:idpg_000068`
- `idp_group_membership:idpm_010204`
- `idp_group:idpg_000116`
- `idp_app_assignment:idpaa_000008`
- `application:app_0003`
- `idp_group_membership:idpm_009628`
- `idp_group:idpg_000097`
- `idp_group_membership:idpm_010233`
- `idp_group:idpg_000119`
- `application_user_access:aua_007688`
- `application_user_access:aua_007689`
- `application_user_access:aua_007690`
- `application:app_0005`
- `application_user_access:aua_007691`
- `application:app_0004`
- `application_user_access:aua_007692`
- `application:app_0007`
- `application_user_access:aua_007693`
- `application:app_0001`
- `github_team_membership:ghtm_001387`
- `github_team_repo_permission:ghtrp_000046`
- `github_team:ght_000049`
- `github_repository:ghr_000063`
- `github_team_repo_permission:ghtrp_000096`
- `github_repository:ghr_000033`
- `github_team_repo_permission:ghtrp_000146`
- `github_repository:ghr_000003`
- `github_team_membership:ghtm_001388`
- `github_team_repo_permission:ghtrp_000151`
- `github_team:ght_000051`
- `github_repository:ghr_000001`
- `github_repo_collaborator:ghrp_000121`
- `github_repository:ghr_000072`
- `drive_permission:drvp_000034`
- `drive_resource:drv_000239`
- `workspace_account:gwa_001942`
- `workspace_group_membership:gwm_002743`
- `workspace_group:gwg_000015`
- `drive_permission:drvp_000094`
- `drive_resource:drv_000179`
- `drive_permission:drvp_000154`
- `drive_resource:drv_000119`
- `drive_permission:drvp_000214`
- `drive_resource:drv_000059`
- `drive_permission:drvp_000049`
- `drive_resource:drv_000104`
- `workspace_group_membership:gwm_002909`
- `workspace_group:gwg_000060`
- `drive_permission:drvp_000109`
- `drive_resource:drv_000044`
- `drive_permission:drvp_000169`
- `drive_resource:drv_000224`
- `drive_permission:drvp_000229`
- `drive_resource:drv_000164`
- `audit_event:evt_00048007`
- `audit_event:evt_00011532`
- `audit_event:evt_00015919`
- `audit_event:evt_00031434`
- `audit_event:evt_00025376`

### Finding 2 — Ended employee retains access (severity: critical)
Ariel Chen (employment_status=ended, end_date=2026-08-03) still has 28 surviving artifact(s): idp_account=idpa_001945 (ariel.chen.1945@northstar.example, last_login=2026-07-20T12:00:00Z); workspace_account=gwa_001945 (ariel.chen.1945@northstar.example, last_login=2026-06-26T12:00:00Z); github_account=gha_001945 (ariel-chen-1945, last_active=2026-06-30T12:00:00Z); device=dev_001945 (platform=windows, last_check_in=2026-08-02T13:00:00Z); grant=idp/application:app_0001 via=nested_group role=standard; grant=idp/application:app_0004 via=nested_group role=standard ...
**Evidence:**
- `person:per_001945` — subject (ended employee)
- `idp_account:idpa_001945`
- `workspace_account:gwa_001945`
- `github_account:gha_001945`
- `device:dev_001945`
- `idp_app_assignment:idpaa_000001`
- `application:app_0001`
- `idp_group_membership:idpm_009644`
- `idp_group:idpg_000046`
- `idp_group_membership:idpm_010182`
- `idp_group:idpg_000112`
- `idp_app_assignment:idpaa_000003`
- `application:app_0004`
- `idp_group_membership:idpm_009643`
- `idp_group:idpg_000107`
- `idp_group_membership:idpm_010243`
- `idp_group:idpg_000114`
- `application_user_access:aua_007704`
- `application:app_0006`
- `application_user_access:aua_007705`
- `application:app_0002`
- `application_user_access:aua_007706`
- `application:app_0005`
- `application_user_access:aua_007707`
- `application_user_access:aua_007708`
- `application:app_0007`
- `application_user_access:aua_007709`
- `github_team_membership:ghtm_001393`
- `github_team_repo_permission:ghtrp_000035`
- `github_team:ght_000006`
- `github_repository:ghr_000036`
- `github_team_repo_permission:ghtrp_000085`
- `github_repository:ghr_000006`
- `github_team_repo_permission:ghtrp_000135`
- `github_repository:ghr_000056`
- `github_team_membership:ghtm_001394`
- `github_team_repo_permission:ghtrp_000022`
- `github_team:ght_000037`
- `github_repository:ghr_000055`
- `github_team_repo_permission:ghtrp_000072`
- `github_repository:ghr_000025`
- `github_team_repo_permission:ghtrp_000122`
- `github_repository:ghr_000075`
- `drive_permission:drvp_000055`
- `drive_resource:drv_000146`
- `workspace_group_membership:gwm_002747`
- `workspace_group:gwg_000006`
- `drive_permission:drvp_000115`
- `drive_resource:drv_000086`
- `drive_permission:drvp_000175`
- `drive_resource:drv_000026`
- `drive_permission:drvp_000235`
- `drive_resource:drv_000206`
- `drive_permission:drvp_000010`
- `drive_resource:drv_000071`
- `workspace_group_membership:gwm_002900`
- `workspace_group:gwg_000051`
- `drive_permission:drvp_000070`
- `drive_resource:drv_000011`
- `drive_permission:drvp_000130`
- `drive_resource:drv_000191`
- `drive_permission:drvp_000190`
- `drive_resource:drv_000131`
- `audit_event:evt_00048016`
- `audit_event:evt_00037716`

### Finding 3 — Ended employee retains access (severity: critical)
Rowan Mercer (employment_status=ended, end_date=2026-07-25) still has 28 surviving artifact(s): idp_account=idpa_001941 (rowan.mercer.1941@northstar.example, last_login=2026-07-06T12:00:00Z); workspace_account=gwa_001941 (rowan.mercer.1941@northstar.example, last_login=2026-05-20T12:00:00Z); github_account=gha_001941 (rowan-mercer-1941, last_active=2026-07-30T12:00:00Z); device=dev_001941 (platform=windows, last_check_in=2026-08-02T14:00:00Z); grant=idp/application:app_0002 via=nested_group role=member; grant=idp/application:app_0003 via=nested_group role=standard ...
**Evidence:**
- `person:per_001941` — subject (ended employee)
- `idp_account:idpa_001941`
- `workspace_account:gwa_001941`
- `github_account:gha_001941`
- `device:dev_001941`
- `idp_app_assignment:idpaa_000026`
- `application:app_0002`
- `idp_group_membership:idpm_009624`
- `idp_group:idpg_000018`
- `idp_group_membership:idpm_010248`
- `idp_group:idpg_000019`
- `idp_app_assignment:idpaa_000014`
- `application:app_0003`
- `idp_group_membership:idpm_009623`
- `idp_group:idpg_000055`
- `idp_group_membership:idpm_010191`
- `idp_group:idpg_000125`
- `application_user_access:aua_007681`
- `application:app_0008`
- `application_user_access:aua_007682`
- `application:app_0006`
- `application_user_access:aua_007683`
- `application_user_access:aua_007684`
- `application:app_0005`
- `application_user_access:aua_007685`
- `application:app_0004`
- `application_user_access:aua_007686`
- `application:app_0007`
- `application_user_access:aua_007687`
- `application:app_0001`
- `github_team_membership:ghtm_001384`
- `github_team_repo_permission:ghtrp_000033`
- `github_team:ght_000030`
- `github_repository:ghr_000002`
- `github_team_repo_permission:ghtrp_000083`
- `github_repository:ghr_000052`
- `github_team_repo_permission:ghtrp_000133`
- `github_repository:ghr_000022`
- `github_team_membership:ghtm_001385`
- `github_team_repo_permission:ghtrp_000020`
- `github_team:ght_000011`
- `github_repository:ghr_000021`
- `github_team_repo_permission:ghtrp_000070`
- `github_repository:ghr_000071`
- `github_team_repo_permission:ghtrp_000120`
- `github_repository:ghr_000041`
- `github_team_membership:ghtm_001386`
- `github_team_repo_permission:ghtrp_000151`
- `github_team:ght_000051`
- `github_repository:ghr_000001`
- `drive_permission:drvp_000027`
- `drive_resource:drv_000190`
- `workspace_group_membership:gwm_002741`
- `workspace_group:gwg_000058`
- `drive_permission:drvp_000087`
- `drive_resource:drv_000130`
- `drive_permission:drvp_000147`
- `drive_resource:drv_000070`
- `drive_permission:drvp_000207`
- `drive_resource:drv_000010`
- `audit_event:evt_00048004`
- `audit_event:evt_00018627`
- `audit_event:evt_00014550`
- `audit_event:evt_00023825`

### Finding 4 — Ended employee retains access (severity: critical)
Luis Kaur (employment_status=ended, end_date=2026-07-03) still has 4 surviving artifact(s): grant=idp/application:app_0002 via=nested_group role=member; grant=github/repo:ghr_000001 via=team role=write; grant=oauth/oauth_scope:oag_000219 via=oauth_grant role=n/a; post_end_event=evt_00017412 (idp.user.login at 2026-07-09T16:08:58Z)
**Evidence:**
- `person:per_001395` — subject (ended employee)
- `idp_app_assignment:idpaa_000026`
- `application:app_0002`
- `idp_account:idpa_001395`
- `idp_group_membership:idpm_006915`
- `idp_group:idpg_000018`
- `idp_group_membership:idpm_010248`
- `idp_group:idpg_000019`
- `github_team_membership:ghtm_000998`
- `github_team_repo_permission:ghtrp_000151`
- `github_team:ght_000051`
- `github_repository:ghr_000001`
- `github_account:gha_001395`
- `workspace_oauth_grant:oag_000219`
- `workspace_account:gwa_001395`
- `audit_event:evt_00017412`

### Finding 5 — Ended employee retains access (severity: critical)
Priya Petrov (employment_status=ended, end_date=2026-04-22) still has 7 surviving artifact(s): grant=github/repo:ghr_000073 via=collab role=write; grant=drive/drive_resource:drv_000041 via=direct role=commenter; post_end_event=evt_00040073 (workspace.user.login at 2026-07-28T02:12:47Z); post_end_event=evt_00047513 (workspace.user.login at 2026-07-01T15:40:45Z); post_end_event=evt_00000724 (workspace.oauth.token.used at 2026-06-07T17:53:06Z); post_end_event=evt_00013812 (idp.user.login at 2026-06-04T09:31:08Z) ...
**Evidence:**
- `person:per_001550` — subject (ended employee)
- `github_repo_collaborator:ghrp_000072`
- `github_repository:ghr_000073`
- `github_account:gha_001550`
- `drive_permission:drvp_000280`
- `drive_resource:drv_000041`
- `workspace_account:gwa_001550`
- `audit_event:evt_00040073`
- `audit_event:evt_00047513`
- `audit_event:evt_00000724`
- `audit_event:evt_00013812`
- `audit_event:evt_00015064`

## Gaps / Uncertainty
- Post-end-date audit events with `actor_type='unknown'` prove that an action occurred against the account but not who caused it. Where the actor is the account itself, this is stronger evidence of continued use.
- Application-reported access (`application_user_access`) can lag IdP state; if only IdP-side rows are stale, downstream MFA and login controls may still be intact.
- An effective grant relationship does not by itself prove the linked account can authenticate. Account status is separate and is cited so an analyst can distinguish stale entitlement from currently usable access.

## Recommended actions (advisory only)
- Validate account state, then deactivate each still-active cited `idp_account` and `workspace_account`.
- Suspend each still-active cited `github_account` and rotate its personal-access tokens.
- Retire each cited `device` in MDM and rotate device certificates.
- Revoke each cited `application_user_access` row and re-scan for lingering `workspace_oauth_grant` records.

— investigation_id: a35fe136-ed65-4b90-8c2e-02a6c53adc7e
— status: succeeded  verified: True
— snapshot: 2026-08-15T12:00:00Z  dataset: 1.1.0
