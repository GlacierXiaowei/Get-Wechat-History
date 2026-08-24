from __future__ import annotations

from pathlib import Path
from typing import Any

from .local_backend import ReaderUnavailableError
from .runtime_state import RuntimeState


class HistoryInitializer:
    def __init__(self, backend: Any, state_root: str | Path | None = None):
        self.backend = backend
        self.state = RuntimeState(state_root)

    @staticmethod
    def _response(
        status: str,
        *,
        db_dir: str = "",
        snapshot: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "db_dir": db_dir,
            "key_status": "ready" if status == "ok" else status,
            "snapshot": snapshot or {},
            "error": error,
        }

    def initialize(self, *, db_dir: str = "", discover: bool = False) -> dict[str, Any]:
        requested = str(db_dir or "").strip()
        if not requested and not discover:
            return self._response(
                "configuration_missing",
                error="Provide db_dir or explicitly set discover=true for controlled discovery.",
            )

        configured_path: Path | None = None
        if requested:
            configured_path = Path(requested).expanduser()
            if not configured_path.is_dir():
                return self._response(
                    "configuration_invalid",
                    db_dir=str(configured_path),
                    error=f"The WeChat database path does not exist: {configured_path}",
                )

        try:
            if configured_path is not None:
                snapshot = self.backend.prepare(
                    configured_db_dir=str(configured_path),
                    allow_discovery=False,
                    allow_key_scan=True,
                )
                selected = configured_path
            else:
                snapshot = self.backend.prepare(allow_discovery=True, allow_key_scan=True)
                selected = getattr(self.backend, "db_dir", None)
                selected = Path(selected).resolve() if selected else None
            selected_text = str(selected) if selected else self.state.load().db_dir
            if selected_text:
                self.state.save_db_dir(selected_text)
            return self._response("ok", db_dir=selected_text, snapshot=snapshot)
        except ReaderUnavailableError as exc:
            selected_text = str(configured_path) if configured_path else self.state.load().db_dir
            return self._response(exc.status, db_dir=selected_text, error=str(exc))
        except Exception as exc:
            selected_text = str(configured_path) if configured_path else self.state.load().db_dir
            return self._response("error", db_dir=selected_text, error=str(exc))
