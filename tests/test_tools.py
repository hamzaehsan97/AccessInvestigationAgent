"""Tests for the typed tool SDK.

We rely on the real DB fixture — it's read-only, small, and gives us realistic
join coverage. Tests target invariants (evidence resolves, effective filter
works, dangerous SQL is rejected) rather than exact row counts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.core.db import ReadOnlyDB
from agent.core.schemas import (
    EvidenceRef,
    ExpandGroupIn,
    FindDisagreementsIn,
    FindPersonIn,
    GetAuditTrailIn,
    ListAccessIn,
    ResolveEvidenceIn,
    SafeSelectIn,
    WhoHasAccessIn,
)
from agent.tools.access import (
    expand_group,
    find_disagreements,
    find_person,
    get_audit_trail,
    get_person_profile,
    list_access_for_person,
    resolve_evidence,
    safe_select,
    who_has_access_to,
)
from agent.tools.dispatch import summarize_result

DB_PATH = Path(__file__).resolve().parents[1] / "input_data" / "access_snapshot.sqlite"


@pytest.fixture(scope="module")
def db() -> ReadOnlyDB:
    return ReadOnlyDB(str(DB_PATH))


def _all_evidence_resolves(db: ReadOnlyDB, refs: list[EvidenceRef]) -> bool:
    if not refs:
        return True
    res = resolve_evidence(ResolveEvidenceIn(refs=refs), db=db)
    return all(r["exists"] for r in res.data)


def test_find_person_by_name(db: ReadOnlyDB) -> None:
    res = find_person(FindPersonIn(query="Emi Kim"), db=db)
    assert res.data
    assert res.data[0]["person_id"].startswith("per_")
    assert _all_evidence_resolves(db, res.evidence)


def test_find_person_by_id(db: ReadOnlyDB) -> None:
    res = find_person(FindPersonIn(query="per_000001"), db=db)
    assert res.data
    assert res.data[0]["person_id"] == "per_000001"


def test_list_access_evidence_resolves(db: ReadOnlyDB) -> None:
    # any active person
    row = db.one("SELECT person_id FROM people WHERE employment_status='active' LIMIT 1")
    assert row
    res = list_access_for_person(ListAccessIn(person_id=row["person_id"]), db=db)
    assert isinstance(res.data, list)
    # all cited IDs should exist
    assert _all_evidence_resolves(db, res.evidence)
    # every grant that says "effective=True" must not carry a revoked_at
    for g in res.data:
        if g["effective"]:
            assert g.get("revoked_at") is None


def test_each_access_grant_contains_complete_subject_chain(db: ReadOnlyDB) -> None:
    row = db.one("SELECT person_id FROM people WHERE employment_status='ended' LIMIT 1")
    person_id = row["person_id"]
    res = list_access_for_person(ListAccessIn(person_id=person_id), db=db)

    assert res.data
    for grant in res.data:
        assert grant["path"][0] == {"kind": "person", "id": person_id}
        assert {"kind": "person", "id": person_id} in grant["evidence"]
        assert any(ref["kind"].endswith("_account") for ref in grant["evidence"])
    assert any("granted_at only records" in note for note in res.notes)


def test_resource_hint_filters_grants_and_supports_negative_result(
    db: ReadOnlyDB,
) -> None:
    no_snowflake = list_access_for_person(
        ListAccessIn(person_id="per_000001", resource_hint="Snowflake"), db=db
    )
    assert no_snowflake.data == []
    assert EvidenceRef(kind="person", id="per_000001") in no_snowflake.evidence
    assert EvidenceRef(kind="application", id="app_0004") in no_snowflake.evidence

    salesforce = list_access_for_person(
        ListAccessIn(person_id="per_000001", resource_hint="Salesforce"), db=db
    )
    assert salesforce.data
    assert {grant["resource_id"] for grant in salesforce.data} == {"app_0003"}

    critical = list_access_for_person(
        ListAccessIn(person_id="per_001945", resource_hint="critical applications"),
        db=db,
    )
    assert critical.data
    critical_ids = {
        row["application_id"]
        for row in db.execute(
            "SELECT application_id FROM applications WHERE sensitivity='critical'"
        )
    }
    assert {grant["resource_id"] for grant in critical.data} <= critical_ids

    unknown = list_access_for_person(
        ListAccessIn(person_id="per_001945", resource_hint="not-a-real-resource"),
        db=db,
    )
    assert unknown.data == []
    assert any("does not prove absence" in note for note in unknown.notes)


def test_nested_access_path_contains_membership_edges(db: ReadOnlyDB) -> None:
    res = list_access_for_person(ListAccessIn(person_id="per_001945"), db=db)
    snowflake = [
        grant
        for grant in res.data
        if grant["resource_id"] == "app_0004"
        and grant["source"] == "nested_group"
    ]
    assert snowflake
    path_ids = [ref["id"] for ref in snowflake[0]["path"]]
    assert "idpm_009643" in path_ids
    assert "idpm_010243" in path_ids


def test_summarized_large_result_is_valid_json(db: ReadOnlyDB) -> None:
    import json

    res = list_access_for_person(ListAccessIn(person_id="per_001945"), db=db)
    payload = summarize_result(res)
    parsed = json.loads(payload)
    assert parsed["truncated"] is True
    assert len(payload.encode("utf-8")) <= 8000


def test_list_access_excludes_revoked_by_default(db: ReadOnlyDB) -> None:
    row = db.one("SELECT person_id FROM people WHERE employment_status='active' LIMIT 1")
    res = list_access_for_person(ListAccessIn(person_id=row["person_id"]), db=db)
    for g in res.data:
        assert g.get("revoked_at") is None or g["effective"] is False


def test_get_person_profile_has_counts(db: ReadOnlyDB) -> None:
    row = db.one("SELECT person_id FROM people WHERE employment_status='active' LIMIT 1")
    res = get_person_profile(row["person_id"], db=db)
    assert res.data
    assert "counts" in res.data
    assert res.data["counts"].get("effective_grants", 0) >= 0


def test_expand_group_returns_transitive_set(db: ReadOnlyDB) -> None:
    row = db.one("SELECT group_id FROM idp_groups LIMIT 1")
    res = expand_group(ExpandGroupIn(system="idp", group_id=row["group_id"]), db=db)
    assert res.data["root"]["kind"] == "idp_group"
    assert isinstance(res.data["transitive_members"], list)


def test_expand_group_reports_cycles_and_transitive_containers() -> None:
    rows = [
        {
            "membership_id": "m1",
            "group_id": "root",
            "member_type": "group",
            "member_id": "child",
            "revoked_at": None,
        },
        {
            "membership_id": "m2",
            "group_id": "child",
            "member_type": "account",
            "member_id": "account-1",
            "revoked_at": None,
        },
        {
            "membership_id": "m3",
            "group_id": "child",
            "member_type": "group",
            "member_id": "root",
            "revoked_at": None,
        },
        {
            "membership_id": "m4",
            "group_id": "parent",
            "member_type": "group",
            "member_id": "root",
            "revoked_at": None,
        },
        {
            "membership_id": "m5",
            "group_id": "grandparent",
            "member_type": "group",
            "member_id": "parent",
            "revoked_at": None,
        },
    ]

    class FakeDB:
        @staticmethod
        def is_effective(revoked_at, expires_at=None):
            return revoked_at is None

        @staticmethod
        def execute(sql, params=()):
            if "WHERE member_type='group'" in sql:
                return [row for row in rows if row["member_type"] == "group"]
            return rows

    members = expand_group(
        ExpandGroupIn(system="idp", group_id="root", max_depth=8), db=FakeDB()
    )
    assert {ref["id"] for ref in members.data["transitive_members"]} == {
        "account-1"
    }
    assert members.data["cycles_detected"]
    assert any(ref.kind == "idp_group_membership" for ref in members.evidence)

    containers = expand_group(
        ExpandGroupIn(
            system="idp", group_id="root", direction="containers", max_depth=8
        ),
        db=FakeDB(),
    )
    assert {ref["id"] for ref in containers.data["direct_members"]} == {
        "child",
        "parent",
    }
    assert {ref["id"] for ref in containers.data["transitive_members"]} >= {
        "parent",
        "grandparent",
    }


def test_expand_group_honors_max_depth() -> None:
    rows = [
        {
            "membership_id": "m1",
            "group_id": "root",
            "member_type": "group",
            "member_id": "child",
            "revoked_at": None,
        },
        {
            "membership_id": "m2",
            "group_id": "child",
            "member_type": "account",
            "member_id": "account-1",
            "revoked_at": None,
        },
    ]

    class FakeDB:
        @staticmethod
        def is_effective(revoked_at, expires_at=None):
            return revoked_at is None

        @staticmethod
        def execute(sql, params=()):
            return rows

    result = expand_group(
        ExpandGroupIn(system="idp", group_id="root", max_depth=1), db=FakeDB()
    )
    assert result.data["transitive_members"] == []
    assert result.truncated is True


def test_who_has_access_to_application(db: ReadOnlyDB) -> None:
    row = db.one("SELECT application_id FROM applications LIMIT 1")
    res = who_has_access_to(
        WhoHasAccessIn(resource_kind="application", resource_id=row["application_id"]),
        db=db,
    )
    assert isinstance(res.data, list)
    assert _all_evidence_resolves(db, res.evidence)


def test_get_audit_trail_pagination(db: ReadOnlyDB) -> None:
    res = get_audit_trail(GetAuditTrailIn(systems=["idp"], limit=5), db=db)
    assert len(res.data["events"]) <= 5
    # every event_id must resolve
    refs = [EvidenceRef(kind="audit_event", id=e["event_id"]) for e in res.data["events"]]
    assert _all_evidence_resolves(db, refs)


def test_get_audit_trail_cursor_does_not_skip_equal_timestamps(
    db: ReadOnlyDB,
) -> None:
    duplicate = db.one(
        "SELECT occurred_at FROM audit_events GROUP BY occurred_at "
        "HAVING COUNT(*) > 1 ORDER BY occurred_at DESC LIMIT 1"
    )
    assert duplicate is not None
    timestamp = duplicate["occurred_at"]
    first = get_audit_trail(
        GetAuditTrailIn(until=timestamp, limit=1), db=db
    )
    second = get_audit_trail(
        GetAuditTrailIn(until=timestamp, limit=1, cursor=first.data["next_cursor"]),
        db=db,
    )
    assert first.data["events"][0]["occurred_at"] == timestamp
    assert second.data["events"][0]["occurred_at"] == timestamp
    assert first.data["events"][0]["event_id"] != second.data["events"][0]["event_id"]


def test_scanner_offboarding_produces_findings(db: ReadOnlyDB, monkeypatch) -> None:
    original_execute = db.execute
    ended_queries: list[str] = []

    def recording_execute(sql, params=()):
        if "FROM people WHERE employment_status = 'ended'" in sql:
            ended_queries.append(sql)
        return original_execute(sql, params)

    monkeypatch.setattr(db, "execute", recording_execute)
    res = find_disagreements(
        FindDisagreementsIn(scanners=["offboarding_leakage"], limit_per_scanner=3), db=db
    )
    assert res.data, "expected at least one offboarding-leakage finding"
    for f in res.data:
        assert f["subject"]["kind"] == "person"
        # every citation must resolve
        refs = [EvidenceRef(**f["subject"])] + [EvidenceRef(**e) for e in f["evidence"]]
        assert _all_evidence_resolves(db, refs)
    assert ended_queries
    assert "LIMIT" not in ended_queries[0].upper()


def test_scanner_external_share_produces_findings(db: ReadOnlyDB) -> None:
    res = find_disagreements(
        FindDisagreementsIn(
            scanners=["external_share_on_restricted_drive"], limit_per_scanner=5
        ),
        db=db,
    )
    # may be empty on some snapshots, but must not error
    for f in res.data:
        assert f["subject"]["kind"] == "drive_permission"
        refs = [EvidenceRef(**e) for e in f["evidence"]]
        assert _all_evidence_resolves(db, refs)


def test_safe_select_allows_reads(db: ReadOnlyDB) -> None:
    res = safe_select(
        SafeSelectIn(sql="SELECT COUNT(*) AS c FROM people WHERE employment_status='ended'"),
        db=db,
    )
    assert res.data is not None
    assert res.data["columns"] == ["c"]
    assert res.data["rows"][0][0] > 0


def test_safe_select_rejects_forbidden_keywords(db: ReadOnlyDB) -> None:
    for bad in [
        "DELETE FROM people",
        "INSERT INTO people (person_id) VALUES ('x')",
        "UPDATE people SET full_name='x'",
        "DROP TABLE people",
        "ATTACH DATABASE ':memory:' AS m",
        "PRAGMA journal_mode = wal",
    ]:
        res = safe_select(SafeSelectIn(sql=bad), db=db)
        assert res.data is None, f"{bad!r} should have been rejected"
        assert any("rejected" in n or "error" in n for n in res.notes)


def test_safe_select_injects_limit(db: ReadOnlyDB) -> None:
    res = safe_select(SafeSelectIn(sql="SELECT person_id FROM people", row_limit=3), db=db)
    assert res.data is not None
    assert len(res.data["rows"]) <= 3


def test_resolve_evidence_for_missing(db: ReadOnlyDB) -> None:
    res = resolve_evidence(
        ResolveEvidenceIn(
            refs=[
                EvidenceRef(kind="person", id="per_DOES_NOT_EXIST"),
                EvidenceRef(kind="audit_event", id="evt_DOES_NOT_EXIST"),
            ]
        ),
        db=db,
    )
    assert all(r["exists"] is False for r in res.data)
