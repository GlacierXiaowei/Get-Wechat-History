from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeConfig:
    schema_version: int = 1
    db_dir: str = ""
    last_validated_at: str = ""
    last_validation_status: str = ""


class RuntimeState:
    """Persist non-secret plugin configuration outside the repository."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        local_app_data = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        self.root = Path(root) if root else Path(local_app_data) / "GetWechatHistory"
        self.path = self.root / "config.json"

    def _read_raw(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def load(self) -> RuntimeConfig:
        data = self._read_raw()
        try:
            schema_version = int(data.get("schema_version", 1))
        except (TypeError, ValueError):
            schema_version = 1
        return RuntimeConfig(
            schema_version=schema_version,
            db_dir=str(data.get("db_dir", "") or ""),
            last_validated_at=str(data.get("last_validated_at", "") or ""),
            last_validation_status=str(data.get("last_validation_status", "") or ""),
        )

    def _write(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def save_db_dir(self, db_dir: str | os.PathLike[str]) -> RuntimeConfig:
        data = self._read_raw()
        data.update(
            {
                "schema_version": 1,
                "db_dir": str(Path(db_dir)),
                "last_validated_at": "",
                "last_validation_status": "",
            }
        )
        self._write(data)
        return self.load()

    def validate_db_dir(self) -> bool:
        configured = self.load().db_dir.strip()
        return bool(configured) and Path(configured).is_dir()

    def mark_validation(self, status: str) -> RuntimeConfig:
        data = self._read_raw()
        data.update(
            {
                "schema_version": 1,
                "last_validated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "last_validation_status": status,
            }
        )
        self._write(data)
        return self.load()
