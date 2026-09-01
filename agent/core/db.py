"""Read-only SQLite adapter with defense-in-depth guards and materialized helpers.

Guarantees applied on every connection:
  1. URI ``mode=ro&immutable=1`` — sqlite refuses to open a write handle.
  2. ``PRAGMA query_only = ON`` — belt-and-suspenders at the pager layer.
  3. ``set_authorizer`` — deny anything that is not a pure read.

Nothing that touches the model is allowed to bypass this module.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# authorizer action codes we allow. See https://sqlite.org/c3ref/c_alter_table.html
_SQLITE_SELECT = 21
_SQLITE_READ = 20
_SQLITE_FUNCTION = 31
_SQLITE_RECURSIVE = 33
_ALLOWED = {_SQLITE_SELECT, _SQLITE_READ, _SQLITE_FUNCTION, _SQLITE_RECURSIVE}

SQLITE_OK = 0
SQLITE_DENY = 1


def _authorizer(action: int, arg1, arg2, arg3, arg4) -> int:
    if action in _ALLOWED:
        return SQLITE_OK
    return SQLITE_DENY


class ReadOnlyDB:
    """Thin wrapper around sqlite3.Connection with read-only guards."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        uri = f"file:{self.db_path}?mode=ro&immutable=1"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = ON")
        self.conn.set_authorizer(_authorizer)

        # cached snapshot metadata
        self._snapshot_time: datetime | None = None
        self._dataset_version: str | None = None
        self._load_metadata()

        # nested-group closures (computed lazily on first use)
        self._idp_closure: dict[str, set[str]] | None = None
        self._workspace_closure: dict[str, set[str]] | None = None
        # reverse maps: account -> set(groups it belongs to transitively)
        self._idp_reverse: dict[str, set[str]] | None = None
        self._workspace_reverse: dict[str, set[str]] | None = None

    # ---------- basic queries ----------

    def execute(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        cur = self.conn.execute(sql, tuple(params))
        return cur.fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        cur = self.conn.execute(sql, tuple(params))
        return cur.fetchone()

    # ---------- metadata ----------

    def _load_metadata(self) -> None:
        rows = self.execute("SELECT key, value FROM dataset_metadata")
        meta = {r["key"]: r["value"] for r in rows}
        # snapshot_at key per SCHEMA.md; fall back to snapshot_time
        ts = meta.get("snapshot_at") or meta.get("snapshot_time")
        if ts:
            try:
                self._snapshot_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                self._snapshot_time = None
        self._dataset_version = meta.get("dataset_version") or meta.get("generator_version") or "unknown"

    @property
    def snapshot_time(self) -> datetime:
        if self._snapshot_time is None:
            # sensible fallback
            return datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        return self._snapshot_time

    @property
    def snapshot_time_iso(self) -> str:
        return self.snapshot_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def dataset_version(self) -> str:
        return self._dataset_version or "unknown"

    # ---------- effective-relationship helpers ----------

    def is_effective(self, revoked_at: str | None, expires_at: str | None = None) -> bool:
        """A relationship is effective iff not revoked and not yet expired at snapshot_time."""
        if revoked_at:
            return False
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp <= self.snapshot_time:
                    return False
            except ValueError:
                pass
        return True

    # ---------- nested-group closure (idp) ----------

    def _build_idp_closure(self) -> None:
        # Build a directed graph: group -> children (accounts and sub-groups),
        # then compute transitive account set per group.
        children: dict[str, list[tuple[str, str]]] = defaultdict(list)  # group_id -> [(member_type, member_id)]
        for r in self.execute(
            "SELECT group_id, member_type, member_id, revoked_at "
            "FROM idp_group_memberships"
        ):
            if not self.is_effective(r["revoked_at"], None):
                continue
            children[r["group_id"]].append((r["member_type"], r["member_id"]))

        closure: dict[str, set[str]] = {}
        reverse: dict[str, set[str]] = defaultdict(set)
        group_ids = {r["group_id"] for r in self.execute("SELECT group_id FROM idp_groups")}
        group_ids.update(children)
        for root in group_ids:
            accounts: set[str] = set()
            visited_groups: set[str] = set()
            queue = deque([root])
            while queue:
                gid = queue.popleft()
                if gid in visited_groups:
                    continue
                visited_groups.add(gid)
                for member_type, member_id in children.get(gid, []):
                    if member_type == "account":
                        accounts.add(member_id)
                    elif member_type == "group":
                        queue.append(member_id)
            closure[root] = accounts

        for gid, accts in closure.items():
            for a in accts:
                reverse[a].add(gid)

        self._idp_closure = closure
        self._idp_reverse = dict(reverse)

    def idp_group_members(self, group_id: str) -> set[str]:
        if self._idp_closure is None:
            self._build_idp_closure()
        return set(self._idp_closure.get(group_id, set()))  # copy

    def idp_groups_for_account(self, account_id: str) -> set[str]:
        if self._idp_reverse is None:
            self._build_idp_closure()
        return set(self._idp_reverse.get(account_id, set()))

    def idp_group_direct_children(self, group_id: str) -> list[tuple[str, str, str]]:
        """Return effective direct (member_type, member_id, membership_id)."""
        out = []
        for r in self.execute(
            "SELECT membership_id, member_type, member_id, revoked_at "
            "FROM idp_group_memberships WHERE group_id = ?",
            (group_id,),
        ):
            if self.is_effective(r["revoked_at"], None):
                out.append((r["member_type"], r["member_id"], r["membership_id"]))
        return out

    def _membership_path(
        self, table: str, account_id: str, target_group_id: str
    ) -> list[tuple[str, str, str, str]]:
        """Return the shortest effective account-to-group path with edge IDs.

        Each tuple is ``(membership_id, member_type, member_id, parent_group_id)``.
        The relationship rows are part of the evidence; entity IDs alone do not
        prove that an account reaches a nested group.
        """
        rows_by_member: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in self.execute(
            f"SELECT membership_id, group_id, member_type, member_id, revoked_at FROM {table}"
        ):
            if self.is_effective(row["revoked_at"], None):
                rows_by_member[(row["member_type"], row["member_id"])].append(row)

        queue = deque([(("account", account_id), [])])
        visited_groups: set[str] = set()
        while queue:
            node, path = queue.popleft()
            for row in rows_by_member.get(node, []):
                parent = row["group_id"]
                edge = (
                    row["membership_id"],
                    row["member_type"],
                    row["member_id"],
                    parent,
                )
                next_path = path + [edge]
                if parent == target_group_id:
                    return next_path
                if parent not in visited_groups:
                    visited_groups.add(parent)
                    queue.append((("group", parent), next_path))
        return []

    def idp_membership_path(
        self, account_id: str, target_group_id: str
    ) -> list[tuple[str, str, str, str]]:
        return self._membership_path(
            "idp_group_memberships", account_id, target_group_id
        )

    # ---------- nested-group closure (workspace) ----------

    def _build_workspace_closure(self) -> None:
        children: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for r in self.execute(
            "SELECT group_id, member_type, member_id, revoked_at "
            "FROM workspace_group_memberships"
        ):
            if not self.is_effective(r["revoked_at"], None):
                continue
            children[r["group_id"]].append((r["member_type"], r["member_id"]))

        closure: dict[str, set[str]] = {}
        reverse: dict[str, set[str]] = defaultdict(set)
        group_ids = {
            r["group_id"] for r in self.execute("SELECT group_id FROM workspace_groups")
        }
        group_ids.update(children)
        for root in group_ids:
            accounts: set[str] = set()
            visited_groups: set[str] = set()
            queue = deque([root])
            while queue:
                gid = queue.popleft()
                if gid in visited_groups:
                    continue
                visited_groups.add(gid)
                for member_type, member_id in children.get(gid, []):
                    if member_type == "account":
                        accounts.add(member_id)
                    elif member_type == "group":
                        queue.append(member_id)
            closure[root] = accounts
        for gid, accts in closure.items():
            for a in accts:
                reverse[a].add(gid)
        self._workspace_closure = closure
        self._workspace_reverse = dict(reverse)

    def workspace_group_members(self, group_id: str) -> set[str]:
        if self._workspace_closure is None:
            self._build_workspace_closure()
        return set(self._workspace_closure.get(group_id, set()))

    def workspace_groups_for_account(self, account_id: str) -> set[str]:
        if self._workspace_reverse is None:
            self._build_workspace_closure()
        return set(self._workspace_reverse.get(account_id, set()))

    def workspace_membership_path(
        self, account_id: str, target_group_id: str
    ) -> list[tuple[str, str, str, str]]:
        return self._membership_path(
            "workspace_group_memberships", account_id, target_group_id
        )

    # ---------- convenience row lookups (used by evidence verifier) ----------

    def get_row(self, table: str, pk_column: str, pk_value: str) -> sqlite3.Row | None:
        # table/pk_column are whitelisted at call sites; still guarded by authorizer
        return self.one(f"SELECT * FROM {table} WHERE {pk_column} = ?", (pk_value,))

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ---------- module-level accessor (lazy singleton for CLI/API use) ----------

_db_singleton: ReadOnlyDB | None = None


def get_db(db_path: str | Path | None = None) -> ReadOnlyDB:
    global _db_singleton
    if _db_singleton is None:
        from agent.core.config import get_settings

        settings = get_settings()
        path = db_path or settings.agent_db_path
        _db_singleton = ReadOnlyDB(path)
    return _db_singleton


def reset_db() -> None:
    """For tests."""
    global _db_singleton
    if _db_singleton is not None:
        _db_singleton.close()
    _db_singleton = None
