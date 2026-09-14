from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .local_backend import ReaderUnavailableError
from .runtime_state import RuntimeState


def detect_wechat_process() -> bool:
    try:
        completed = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    output = completed.stdout.casefold()
    return "weixin.exe" in output or "wechat.exe" in output


class HistoryDoctor:
    def __init__(
        self,
        backend: Any,
        state_root: str | Path | None = None,
        *,
        process_probe: Callable[[], bool] = detect_wechat_process,
    ):
        self.backend = backend
        self.state = RuntimeState(state_root)
        self.process_probe = process_probe

    @staticmethod
    def _response(
        status: str,
        *,
        db_dir: str = "",
        wechat_running: bool | None = None,
        snapshot: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "db_dir": db_dir,
            "python_ok": sys.version_info >= (3, 10),
            "wechat_running": wechat_running,
            "key_status": "ready" if status == "ready" else status,
            "snapshot": snapshot or {},
            "error": error,
        }

    def check(self) -> dict[str, Any]:
        configuration = self.state.load()
        configured = (configuration.db_dir or configuration.pending_db_dir).strip()
        if not configured:
            return self._response(
                "configuration_missing",
                error="No WeChat database path is configured. Run initialize_wechat_history first.",
            )
        if not Path(configured).is_dir():
            return self._response(
                "configuration_invalid",
                db_dir=configured,
                error=f"The configured WeChat database path is unavailable: {configured}",
            )

        try:
            wechat_running = bool(self.process_probe())
        except Exception:
            wechat_running = False
        try:
            snapshot = self.backend.prepare(
                configured_db_dir=configured,
                allow_discovery=False,
                allow_key_scan=False,
            )
            return self._response(
                "ready",
                db_dir=configured,
                wechat_running=wechat_running,
                snapshot=snapshot,
            )
        except ReaderUnavailableError as exc:
            status = exc.status
            if status == "keys_missing" and not wechat_running:
                status = "wechat_not_running"
            return self._response(
                status,
                db_dir=configured,
                wechat_running=wechat_running,
                error=str(exc),
            )
        except Exception as exc:
            return self._response(
                "error",
                db_dir=configured,
                wechat_running=wechat_running,
                error=str(exc),
            )
