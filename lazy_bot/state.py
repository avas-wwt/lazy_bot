"""state.json remembers what each platform already has, so a re-run never redoes finished work."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

SAVED = "saved"  # draft saved (or entries written) but not submitted
SUBMITTED = "submitted"  # submitted, or written to a site that has no drafts


class State:
    def __init__(self, path: Path, data: dict | None = None):
        self.path = path
        self.data = data if data is not None else {"version": 1, "weeks": {}, "cache": {}}

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls(path)
        return cls(path, json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.path)

    def week(self, key: str) -> dict:
        return self.data["weeks"].setdefault(key, {"platforms": {}})

    def record(self, week_key: str, platform: str) -> dict:
        """The platform's entry for a week: status, data hash, and `data` the adapter may keep (e.g. ids)."""
        record = self.week(week_key)["platforms"].setdefault(platform, {})
        record.setdefault("data", {})
        return record

    def mark(self, week_key: str, platform: str, status: str, data_hash: str) -> None:
        self.record(week_key, platform).update(
            status=status, hash=data_hash, at=datetime.now().isoformat(timespec="seconds")
        )
        self.save()

    def cache(self, platform: str) -> dict:
        """Values an adapter wants to keep across weeks (e.g. Clockify workspace and project ids)."""
        return self.data["cache"].setdefault(platform, {})
