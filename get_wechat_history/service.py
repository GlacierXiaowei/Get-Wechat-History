from __future__ import annotations

from typing import Any

from .contracts import make_cursor, parse_cursor, resolve_candidate, validate_limit
from .doctor import HistoryDoctor
from .initializer import HistoryInitializer


class HistoryService:
    def __init__(self, backend: Any, exporter: Any, *, state_root: Any = None):
        self.backend = backend
        self.exporter = exporter
        self.state_root = state_root

    def initialize_wechat_history(self, db_dir: str = "", *, discover: bool = False) -> dict[str, Any]:
        return HistoryInitializer(self.backend, self.state_root).initialize(db_dir=db_dir, discover=discover)

    def doctor_wechat_history(self, *, process_probe: Any = None) -> dict[str, Any]:
        kwargs = {}
        if process_probe is not None:
            kwargs["process_probe"] = process_probe
        return HistoryDoctor(self.backend, self.state_root, **kwargs).check()

    @staticmethod
    def _base_result(snapshot: dict[str, Any], scope: str) -> dict[str, Any]:
        return {
            "status": "ok",
            "scope": scope,
            "chat": None,
            "messages": [],
            "count": 0,
            "next_cursor": "",
            "has_more": False,
            "candidates": [],
            "snapshot_at": snapshot.get("snapshot_at", ""),
            "error": None,
        }

    @staticmethod
    def _next_cursor(messages: list[dict[str, Any]], has_more: bool) -> str:
        if not has_more or not messages:
            return ""
        oldest = messages[0]
        source_db = oldest.get("source_db")
        if not source_db:
            source_db = str(oldest["message_id"]).rsplit(":", 1)[0]
        return make_cursor(
            {
                "timestamp_unix": oldest["timestamp_unix"],
                "source_db": source_db,
                "local_id": oldest["local_id"],
            }
        )

    def _resolve_chat(
        self,
        query: str,
        *,
        member_count: int | None,
        min_member_count: int | None,
    ) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
        if not query or not query.strip():
            raise ValueError("chat must not be empty")
        candidates = self.backend.find_chat_candidates(query.strip())
        return resolve_candidate(
            candidates,
            member_count=member_count,
            min_member_count=min_member_count,
        )

    def read_recent_messages(
        self,
        *,
        limit: int,
        cursor: str = "",
        keyword: str = "",
        start_time: str = "",
        end_time: str = "",
    ) -> dict[str, Any]:
        page_size = validate_limit(limit)
        before = parse_cursor(cursor)
        snapshot = self.backend.prepare()
        result = self._base_result(snapshot, "recent")
        messages, has_more = self.backend.read_recent_records(
            limit=page_size,
            before=before,
            keyword=keyword,
            start_time=start_time,
            end_time=end_time,
        )
        result.update(
            messages=messages,
            count=len(messages),
            has_more=has_more,
            next_cursor=self._next_cursor(messages, has_more),
        )
        return result

    def read_chat_history(
        self,
        chat: str,
        *,
        limit: int,
        cursor: str = "",
        keyword: str = "",
        start_time: str = "",
        end_time: str = "",
        member_count: int | None = None,
        min_member_count: int | None = None,
    ) -> dict[str, Any]:
        page_size = validate_limit(limit)
        before = parse_cursor(cursor)
        snapshot = self.backend.prepare()
        result = self._base_result(snapshot, "chat")
        status, selected, candidates = self._resolve_chat(
            chat,
            member_count=member_count,
            min_member_count=min_member_count,
        )
        if status != "ok":
            result.update(
                status=status,
                candidates=candidates,
                error=(
                    "Multiple chats match; provide member_count, min_member_count, or an exact chat id."
                    if status == "ambiguous"
                    else "No matching chat was found."
                ),
            )
            return result

        messages, has_more = self.backend.read_chat_records(
            selected["id"],
            limit=page_size,
            before=before,
            keyword=keyword,
            start_time=start_time,
            end_time=end_time,
        )
        result.update(
            chat=selected,
            messages=messages,
            count=len(messages),
            has_more=has_more,
            next_cursor=self._next_cursor(messages, has_more),
        )
        return result

    def export_chat_history(
        self,
        chat: str,
        *,
        member_count: int | None = None,
        min_member_count: int | None = None,
        start_time: str = "",
        end_time: str = "",
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self.backend.prepare()
        status, selected, candidates = self._resolve_chat(
            chat,
            member_count=member_count,
            min_member_count=min_member_count,
        )
        if status != "ok":
            return {
                "status": status,
                "chat": None,
                "candidates": candidates,
                "snapshot_at": snapshot.get("snapshot_at", ""),
                "error": (
                    "Multiple chats match; provide member_count, min_member_count, or an exact chat id."
                    if status == "ambiguous"
                    else "No matching chat was found."
                ),
            }
        return self.exporter.export(
            self.backend,
            selected,
            start_time=start_time,
            end_time=end_time,
            output_dir=output_dir,
            snapshot=snapshot,
        )

    def decode_image(
        self,
        chat: str,
        message_id: str,
        *,
        member_count: int | None = None,
        min_member_count: int | None = None,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self.backend.prepare()
        status, selected, candidates = self._resolve_chat(
            chat,
            member_count=member_count,
            min_member_count=min_member_count,
        )
        if status != "ok":
            return {
                "status": status,
                "chat": None,
                "candidates": candidates,
                "snapshot_at": snapshot.get("snapshot_at", ""),
                "error": "Chat could not be resolved uniquely.",
            }
        result = self.backend.decode_image(selected["id"], message_id, output_dir=output_dir)
        result.setdefault("chat", selected)
        result.setdefault("snapshot_at", snapshot.get("snapshot_at", ""))
        return result
