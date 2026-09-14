"""Authenticated local key recovery for modern and legacy Windows Weixin."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback

from .key_scan_common import collect_db_files, cross_verify_keys, save_results, scan_memory_for_keys
from .modern_keys_windows import ProcessMemory, candidates, recover_modern_keys


def scanner_build_id() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for name in ("find_all_keys_windows.py", "modern_keys_windows.py", "key_scan_common.py"):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()[:16]


SCANNER_BUILD_ID = scanner_build_id()


class ScanDiagnostics:
    """Persist only fixed stage names, counts, PIDs, and system error codes."""

    def __init__(self, directory: str | os.PathLike[str]):
        self.path = Path(directory) / "key_scan_diagnostics.json"
        self.events: list[dict[str, object]] = []

    def __call__(self, stage: str, **fields):
        event = {"stage": stage, **fields}
        self.events.append(event)
        if os.environ.get("GET_WECHAT_HISTORY_DEBUG") == "1":
            print(json.dumps(event, ensure_ascii=True), file=sys.stderr, flush=True)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"build_id": SCANNER_BUILD_ID, "events": self.events}, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _quiet(*args, **kwargs):
    """Do not expose salts, keys, addresses, or database paths in diagnostics."""
    return None


def _legacy_scan(db_files, salts, keys, remaining, report) -> None:
    pattern = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
    for pid, name, _ in candidates():
        matches = 0
        before = len(keys)
        try:
            with ProcessMemory(pid, report) as process:
                for base, data in process.chunks(private_only=False, overlap=255):
                    matches += scan_memory_for_keys(
                        data,
                        pattern,
                        db_files,
                        salts,
                        keys,
                        remaining,
                        base,
                        pid,
                        _quiet,
                    )
                    if not remaining:
                        break
            report(
                "legacy_scan",
                pid=pid,
                name=name,
                matches=matches,
                verified=len(keys) - before,
            )
        except OSError as exc:
            report(
                "legacy_failure",
                pid=pid,
                exception=type(exc).__name__,
                error=exc.errno,
            )
        if not remaining:
            break
    cross_verify_keys(db_files, salts, keys, _quiet)


def extract_all_keys(db_dir: str, out_file: str) -> None:
    """Try modern authenticated recovery, then retain the legacy fallback."""
    report = ScanDiagnostics(Path(out_file).parent)
    started = time.monotonic()
    try:
        report("start", build_id=SCANNER_BUILD_ID)
        db_files, salts = collect_db_files(db_dir)
        report("database_collection", count=len(db_files), salts=len(salts))

        keys = {}
        try:
            keys = recover_modern_keys(db_files, report)
        except Exception as exc:
            report("modern_failure", exception=type(exc).__name__)

        remaining = set(salts) - set(keys)
        if remaining:
            try:
                _legacy_scan(db_files, salts, keys, remaining, report)
            except Exception as exc:
                report("legacy_failure", exception=type(exc).__name__)

        report(
            "complete",
            verified=len(keys),
            total=len(salts),
            elapsed_seconds=round(time.monotonic() - started, 2),
        )
        if not keys:
            raise RuntimeError(
                "No authenticated database key found. "
                "See sanitized key_scan_diagnostics.json for candidate stages."
            )
        save_results(db_files, salts, keys, db_dir, out_file, _quiet)
    except Exception as exc:
        report(
            "exception",
            exception=type(exc).__name__,
            frames=[
                {
                    "file": Path(frame.filename).name,
                    "line": frame.lineno,
                    "function": frame.name,
                }
                for frame in traceback.extract_tb(exc.__traceback__)
            ],
        )
        raise
    finally:
        report.save()


if __name__ == "__main__":
    raise SystemExit("Use initialize_wechat_history to recover keys for a chosen database path.")
