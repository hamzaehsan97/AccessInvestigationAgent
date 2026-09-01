# Access Snapshot Data Guide

`access_snapshot.sqlite` is a synthetic SQLite database for a company with
roughly 2,000 people. It contains two kinds of information:

- **Snapshot tables** describe people, accounts, groups, permissions, devices,
  and application access as reported at the snapshot time.
- **Audit events** describe actions recorded by the source systems during the
  previous six months.

All people, companies, email addresses, identifiers, and events are fictional.

## Start here

The snapshot was taken at `2026-08-15T12:00:00Z`. The same value and the dataset
version are stored in `dataset_metadata`.

You can open the file with any SQLite client. The database includes its table
and column definitions, foreign keys, constraints, and indexes. This guide
explains how those tables relate and what the data means.

Keep these rules in mind:

- All timestamps are UTC ISO 8601 strings. HR dates use `YYYY-MM-DD`.
- A relationship with `revoked_at IS NULL` has not been explicitly revoked. If
  it also has an `expires_at`, compare that value with the snapshot time.
- Account state comes from the account's `status` field. Device state uses
  `retired_at`. Employment state comes from `people.employment_status`.
- Those states can disagree. A person whose employment ended may still have an
  active account, for example. The disagreement may be expected, delayed, or
  incorrect.
- `person_id` connects records from different systems to an HR person. A `NULL`
  value means the source account or device could not be confidently matched to
  a person; it is not necessarily bad data.
- Audit logs are incomplete records from individual systems. An event may prove
  that an action occurred without proving which human caused it.
- Fields ending in `_json` contain valid JSON text. Their contents vary by
  record or event type.

## How the data fits together

| To connect | Follow these tables |
| --- | --- |
| A person to their accounts | `people` to an account table through `person_id` |
| An identity account to its groups | `idp_accounts` to `idp_group_memberships` to `idp_groups` |
| A user to an application | `idp_accounts` to `application_user_access` to `applications` |
| An identity assignment to an application | `idp_accounts` or `idp_groups` to `idp_app_assignments` to `applications` |
| A Workspace account to Drive access | `workspace_accounts`, possibly through `workspace_group_memberships`, to `drive_permissions` and `drive_resources` |
| A GitHub account to repository access | `github_accounts`, through either team membership and team permissions or `github_repo_collaborators`, to `github_repositories` |
| A record to activity over time | Match its identifier to `audit_events.actor_id` or `audit_events.target_id`, using the accompanying type fields |

### IDs that can refer to more than one table

Some relationship tables allow either an account or a group in the same ID
column. Read the accompanying type column before interpreting the ID:

- In a group-membership table, `member_type = 'account'` means `member_id`
  refers to an account. `member_type = 'group'` means it refers to another
  group.
- In `idp_app_assignments`, `principal_type` tells you whether `principal_id`
  refers to an identity account or an identity group.
- In `drive_permissions`, `principal_type` tells you whether `principal_id` is
  an account ID, group ID, domain name, or external email address.

These conditional connections are not enforced as SQLite foreign keys. Use both
the type and ID fields when following them. Groups can contain other groups at
more than one level.

## Application access and MFA

Three sources describe different parts of application access:

- `idp_app_assignments` is what the identity provider is configured to grant to
  an account or group.
- `application_user_access` is what the application currently reports for a
  particular identity account.
- `application.authentication` records in `audit_events` describe individual
  sign-ins observed by an application.

These sources can disagree because they come from different systems and points
in time.

MFA also has three separate meanings:

- `applications.default_mfa_requirement` is the application's general policy.
- `application_user_access.mfa_enrollment_status` says whether that user is
  enrolled in MFA for that application.
- An `application.authentication` event can say whether MFA occurred during
  that particular sign-in.

Enrollment does not prove that MFA happened during every sign-in. A policy that
requires MFA does not prove that every user is enrolled.

## Table reference

### Company roster

#### `dataset_metadata`

Key/value metadata for the snapshot, including its timestamp and generator
version.

#### `people`

The HR roster. `person_id` is the shared person identifier. The table also
contains name, primary email, worker type, department, title, location, manager,
employment status, and employment dates.

`manager_person_id` refers to another row in `people` and may be `NULL`.
`employment_status` is `active`, `ended`, `leave`, or `prehire`.

### Identity provider

The identity provider, shortened to **IdP**, manages sign-in accounts, groups,
and application assignments.

#### `idp_accounts`

IdP accounts, linked to `people` when a reliable match exists. `status` is
`active`, `suspended`, or `deprovisioned`. The table also records creation,
deactivation, and last-login times.

#### `applications`

The application catalog. Each application has a category, sensitivity,
authentication mode, and default MFA requirement. The MFA requirement is a
policy, not evidence about a particular user or sign-in.

#### `idp_groups`

IdP groups. `management_type` says whether a group is maintained manually, from
HR data, or by an application sync. `rule_expression`, when present, is a
human-readable description of a rule rather than executable code.

#### `idp_group_memberships`

Direct account-to-group and group-to-group relationships. `member_type`
determines what `member_id` refers to. `source` says whether the relationship
was manual, created from HR data, or created by group nesting.

Nested groups can create indirect access. Do not assume nesting stops after one
level.

#### `idp_app_assignments`

Application roles assigned to either an IdP account or an IdP group.
`principal_type` determines what `principal_id` refers to. A group assignment
can give the role to people who belong to that group directly or through nested
groups.

#### `application_user_access`

One row per IdP account and application, showing what the application reports.
It includes the user's role, access status, how the account was provisioned,
assignment and revocation times, MFA enrollment and registered methods, and the
most recent authentication information known to the application.

`provisioning_source = 'idp_scim'` means the account was created or updated by
an automated identity-provider sync. The other values represent manual or
application-local management.

### Google Workspace

#### `workspace_accounts`

Workspace accounts, linked to `people` when possible. The table contains account
status, creation time, and last-login time.

#### `workspace_groups`

Workspace groups, including their email address, name, description, and
creation time.

#### `workspace_group_memberships`

Account-to-group and group-to-group relationships in Workspace. `member_type`
determines what `member_id` refers to. Each row also records the member's role,
the source of the relationship, and when it was granted or revoked.

#### `drive_resources`

Drive files, folders, and shared drives. Each resource has an owner when known,
a resource type, and a classification from `public` through `restricted`.

#### `drive_permissions`

Permissions on Drive resources. A permission can belong to an account, group,
domain, or external email address. `principal_type` determines how to interpret
`principal_id`. The table also records the role and grant, expiration, and
revocation times.

#### `workspace_oauth_grants`

Third-party application access granted by Workspace users. `scopes_json` lists
the permissions requested by the third-party application. The snapshot does not
include an approved-application list or vendor-risk information.

### GitHub

#### `github_accounts`

GitHub accounts, linked to `people` when possible. An account can represent a
human user or a service account. Service accounts and unmatched users may have
no `person_id`.

#### `github_org_memberships`

Organization membership, including the account's organization role, whether it
came from identity sync or a manual action, and when it was granted or revoked.

#### `github_teams`

GitHub teams. `source_idp_group_id`, when present, identifies the IdP group that
synchronizes membership into the team.

#### `github_team_memberships`

Current and historical GitHub team membership. The table records the GitHub
account, team, role, source, and grant or revocation time. For synchronized
membership, the upstream IdP group relationships can explain why the account
joined the team.

#### `github_repositories`

Repository name, visibility, sensitivity, archive state, and creation time.

#### `github_team_repo_permissions`

Repository permissions granted to a GitHub team. Combine this with team
membership to determine which accounts receive the permission.

#### `github_repo_collaborators`

Repository permissions granted directly to an account rather than through a
team. Approval references and expiration times may be missing because the
source data does not always contain them.

### Device management

#### `devices`

Known devices, linked to `people` when possible. The table includes platform,
ownership, compliance status, enrollment time, last check-in, and retirement
time.

### Audit history

#### `audit_events`

A common event format containing records from HR, the IdP, Workspace, GitHub,
and device management.

| Field | Meaning |
| --- | --- |
| `event_id` | Stable event identifier to include as evidence |
| `occurred_at` | Event time in UTC |
| `system`, `event_type` | System that produced the event and its event name |
| `actor_type`, `actor_id` | Account, service, system, or unknown actor that initiated the event |
| `target_type`, `target_id` | Record affected by the event |
| `source` | Whether the event came from a console, sync, API, or user action |
| `outcome` | `success`, `failure`, or `partial` |
| `ip_address` | Source address when the system recorded one |
| `correlation_id` | Identifier shared by related events when available |
| `details_json` | Additional fields specific to the event type |

Event types are not limited to a fixed list. Interpret actors and targets using
both their type and ID. Investigations will often need both audit history and
snapshot tables because neither is complete on its own.

## Scale

The snapshot contains approximately:

- 2,048 people
- 4,900 linked accounts
- 8,000 user-to-application relationships
- 196 identity and Workspace groups, including nested groups
- 80 GitHub repositories and 240 Drive resources
- 48,000 audit events

Indexes cover common relationships and event lookups. Select context
deliberately rather than sending complete tables to the model.
