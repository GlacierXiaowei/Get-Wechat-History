from __future__ import annotations

import base64
import json
from typing import Any


MAX_PAGE_SIZE = 500


def validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    if limit > MAX_PAGE_SIZE:
        raise ValueError(f"limit must be at most {MAX_PAGE_SIZE}")
    return limit


def make_cursor(anchor: dict[str, Any]) -> str:
    normalized = {
        "timestamp_unix": int(anchor["timestamp_unix"]),
        "source_db": str(anchor["source_db"]),
        "local_id": int(anchor["local_id"]),
    }
    payload = json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def parse_cursor(cursor: str | None) -> dict[str, Any] | None:
    if cursor in (None, ""):
        return None
    try:
        if not isinstance(cursor, str):
            raise ValueError
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
        if set(value) != {"timestamp_unix", "source_db", "local_id"}:
            raise ValueError
        if (
            isinstance(value["timestamp_unix"], bool)
            or not isinstance(value["timestamp_unix"], int)
            or value["timestamp_unix"] < 0
            or not isinstance(value["source_db"], str)
            or not value["source_db"]
            or isinstance(value["local_id"], bool)
            or not isinstance(value["local_id"], int)
            or value["local_id"] < 0
        ):
            raise ValueError
    except (TypeError, ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    return value


def resolve_candidate(
    candidates: list[dict[str, Any]],
    *,
    member_count: int | None = None,
    min_member_count: int | None = None,
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    remaining = list(candidates)
    if member_count is not None:
        remaining = [
            candidate
            for candidate in remaining
            if candidate.get("member_count") is not None
            and abs(int(candidate["member_count"]) - member_count) <= 2
        ]
    if min_member_count is not None:
        remaining = [
            candidate
            for candidate in remaining
            if candidate.get("member_count") is not None
            and int(candidate["member_count"]) >= min_member_count
        ]

    if not remaining:
        return "not_found", None, []
    if len(remaining) > 1:
        return "ambiguous", None, remaining
    return "ok", remaining[0], []
