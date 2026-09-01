"""Persistent storage for Investigation objects. JSON files today; S3 tomorrow."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from agent.core.schemas import Investigation


class RunStore:
    def save(self, inv: Investigation) -> None:
        raise NotImplementedError

    def load(self, inv_id: str) -> Optional[Investigation]:
        raise NotImplementedError

    def list_ids(self) -> list[str]:
        raise NotImplementedError


class FileRunStore(RunStore):
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, inv_id: str) -> Path:
        return self.root / f"{inv_id}.json"

    def save(self, inv: Investigation) -> None:
        p = self._path(inv.id)
        p.write_text(json.dumps(inv.model_dump(), indent=2, default=str))

    def load(self, inv_id: str) -> Optional[Investigation]:
        p = self._path(inv_id)
        if not p.exists():
            return None
        return Investigation(**json.loads(p.read_text()))

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))
