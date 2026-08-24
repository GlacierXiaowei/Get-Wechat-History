from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


CSV_FIELDS = (
    "message_id",
    "local_id",
    "timestamp",
    "timestamp_unix",
    "sender_id",
    "sender_name",
    "type",
    "type_id",
    "text",
    "raw_content",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:80] or "wechat-chat"


class HistoryExporter:
    def __init__(self, default_root: str | os.PathLike[str]):
        self.default_root = Path(default_root)

    def export(
        self,
        backend: Any,
        chat: dict[str, Any],
        *,
        start_time: str,
        end_time: str,
        output_dir: str | None,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        root = Path(output_dir) if output_dir else self.default_root
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        final_dir = root / f"{stamp}-{_safe_name(chat['display_name'])}"
        temp_dir = Path(tempfile.mkdtemp(prefix=f".{final_dir.name}-", dir=root))

        jsonl_path = temp_dir / "messages.jsonl"
        csv_path = temp_dir / "messages.csv"
        metadata_path = temp_dir / "metadata.json"
        try:
            records = list(
                backend.iter_chat_records(
                    chat["id"],
                    start_time=start_time,
                    end_time=end_time,
                )
            )
            snapshot_check = getattr(backend, "snapshot_is_current", None)
            if snapshot_check is not None and not snapshot_check(snapshot):
                raise RuntimeError("WeChat source changed during export; retry the export.")

            with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(records)

            content_hashes = {
                "messages.jsonl": _sha256(jsonl_path),
                "messages.csv": _sha256(csv_path),
            }
            metadata = {
                "status": "ok",
                "chat": chat,
                "count": len(records),
                "first_timestamp": records[0].get("timestamp", "") if records else "",
                "last_timestamp": records[-1].get("timestamp", "") if records else "",
                "start_time": start_time,
                "end_time": end_time,
                "snapshot": snapshot,
                "sha256": content_hashes,
            }
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result_hashes = {**content_hashes, "metadata.json": _sha256(metadata_path)}
            temp_dir.replace(final_dir)
        except Exception:
            for child in temp_dir.iterdir():
                child.unlink(missing_ok=True)
            temp_dir.rmdir()
            raise

        return {
            "status": "ok",
            "chat": chat,
            "export_dir": str(final_dir.resolve()),
            "files": ["messages.jsonl", "messages.csv", "metadata.json"],
            "count": len(records),
            "first_timestamp": records[0].get("timestamp", "") if records else "",
            "last_timestamp": records[-1].get("timestamp", "") if records else "",
            "sha256": result_hashes,
            "snapshot": snapshot,
        }
