from __future__ import annotations

import copy
import json
import threading
from collections import OrderedDict
from typing import Any

from .contracts import make_cursor, parse_cursor, resolve_candidate, validate_limit
from .doctor import HistoryDoctor
from .initializer import HistoryInitializer


class HistoryService:
    def __init__(self, backend: Any, exporter: Any, *, state_root: Any = None):
        self.backend = backend
        self.exporter = exporter
        self.state_root = state_root
        self._lock = threading.RLock()
        self._result_cache: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        self._result_cache_limit = 64

    def initialize_wechat_history(self, db_dir: str = "", *, discover: bool = False) -> dict[str, Any]:
        with self._lock:
            result = HistoryInitializer(self.backend, self.state_root).initialize(
                db_dir=db_dir,
                discover=discover,
            )
            self._result_cache.clear()
            return result

    def doctor_wechat_history(self, *, process_probe: Any = None) -> dict[str, Any]:
        with self._lock:
            kwargs = {}
            if process_probe is not None:
                kwargs["process_probe"] = process_probe
            return HistoryDoctor(self.backend, self.state_root, **kwargs).check()

    def _prepare(self, **kwargs: Any) -> dict[str, Any]:
        snapshot = self.backend.prepare(**kwargs)
        if snapshot.get("cache_status") == "refreshed":
            self._result_cache.clear()
        return snapshot

    @staticmethod
    def _cache_key(scope: str, params: dict[str, Any], snapshot: dict[str, Any]) -> tuple[str, str, str]:
        source = str(snapshot.get("source_signature") or snapshot.get("snapshot_at") or "")
        encoded = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        return scope, source, encoded

    def _cached(self, key: tuple[str, str, str], callback: Any) -> dict[str, Any]:
        cached = self._result_cache.get(key)
        if cached is not None:
            self._result_cache.move_to_end(key)
            return copy.deepcopy(cached)
        result = callback()
        self._result_cache[key] = copy.deepcopy(result)
        self._result_cache.move_to_end(key)
        while len(self._result_cache) > self._result_cache_limit:
            self._result_cache.popitem(last=False)
        return result

    @staticmethod
    def _compact_messages(
        messages: list[dict[str, Any]],
        *,
        include_raw_content: bool,
    ) -> list[dict[str, Any]]:
        fields = ("message_id", "timestamp", "sender_name", "type", "text")
        compacted = []
        for message in messages:
            compact = {field: message[field] for field in fields if field in message}
            if include_raw_content and "raw_content" in message:
                compact["raw_content"] = message["raw_content"]
            compacted.append(compact)
        return compacted

    def ensure_wechat_history_fresh(self, *, force: bool = False) -> dict[str, Any]:
        """Check freshness once for a task and refresh only changed source state."""
        with self._lock:
            snapshot = self._prepare(force=force)
            refreshed = snapshot.get("cache_status") == "refreshed"
            return {
                "status": "ok",
                "scope": "freshness",
                "cache_status": snapshot.get("cache_status", ""),
                "refreshed": refreshed,
                "source_changed": bool(snapshot.get("source_changed")),
                "snapshot_at": snapshot.get("snapshot_at", ""),
                "snapshot": snapshot,
                "error": None,
            }

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
            "cache_status": snapshot.get("cache_status", ""),
            "source_changed": bool(snapshot.get("source_changed")),
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

    def search_chats(
        self,
        query: str,
        *,
        limit: int = 20,
        member_count: int | None = None,
        min_member_count: int | None = None,
    ) -> dict[str, Any]:
        if not query or not query.strip():
            raise ValueError("query must not be empty")
        page_size = validate_limit(limit)
        with self._lock:
            snapshot = self._prepare()
            params = {
                "query": query.strip(),
                "limit": page_size,
                "member_count": member_count,
                "min_member_count": min_member_count,
            }
            key = self._cache_key("chat_search", params, snapshot)

            def build() -> dict[str, Any]:
                candidates = self.backend.search_chats(
                    query.strip(),
                    limit=page_size,
                    member_count=member_count,
                    min_member_count=min_member_count,
                )
                chats = [
                    {
                        "chat_id": candidate["id"],
                        "display_name": candidate.get("display_name", candidate["id"]),
                        "member_count": candidate.get("member_count"),
                    }
                    for candidate in candidates
                ]
                return {
                    "status": "ok",
                    "scope": "chat_search",
                    "query": query.strip(),
                    "chats": chats,
                    "count": len(chats),
                    "snapshot_at": snapshot.get("snapshot_at", ""),
                    "cache_status": snapshot.get("cache_status", ""),
                    "source_changed": bool(snapshot.get("source_changed")),
                    "error": None,
                }

            return self._cached(key, build)

    def read_recent_messages(
        self,
        *,
        limit: int = 100,
        cursor: str = "",
        keyword: str = "",
        start_time: str = "",
        end_time: str = "",
        include_raw_content: bool = False,
    ) -> dict[str, Any]:
        page_size = validate_limit(limit)
        before = parse_cursor(cursor)
        with self._lock:
            snapshot = self._prepare()
            params = {
                "limit": page_size,
                "cursor": cursor,
                "keyword": keyword,
                "start_time": start_time,
                "end_time": end_time,
                "include_raw_content": include_raw_content,
            }
            key = self._cache_key("recent", params, snapshot)

            def build() -> dict[str, Any]:
                result = self._base_result(snapshot, "recent")
                messages, has_more = self.backend.read_recent_records(
                    limit=page_size,
                    before=before,
                    keyword=keyword,
                    start_time=start_time,
                    end_time=end_time,
                    include_raw_content=include_raw_content,
                )
                result.update(
                    messages=self._compact_messages(
                        messages,
                        include_raw_content=include_raw_content,
                    ),
                    count=len(messages),
                    has_more=has_more,
                    next_cursor=self._next_cursor(messages, has_more),
                )
                return result

            return self._cached(key, build)

    def read_chat_history(
        self,
        chat: str,
        *,
        limit: int = 100,
        cursor: str = "",
        keyword: str = "",
        start_time: str = "",
        end_time: str = "",
        member_count: int | None = None,
        min_member_count: int | None = None,
        include_raw_content: bool = False,
    ) -> dict[str, Any]:
        page_size = validate_limit(limit)
        before = parse_cursor(cursor)
        with self._lock:
            snapshot = self._prepare()
            params = {
                "chat": chat.strip(),
                "limit": page_size,
                "cursor": cursor,
                "keyword": keyword,
                "start_time": start_time,
                "end_time": end_time,
                "member_count": member_count,
                "min_member_count": min_member_count,
                "include_raw_content": include_raw_content,
            }
            key = self._cache_key("chat", params, snapshot)

            def build() -> dict[str, Any]:
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
                            "Multiple chats match; use search_chats to choose a chat_id."
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
                    include_raw_content=include_raw_content,
                )
                result.update(
                    chat=selected,
                    messages=self._compact_messages(
                        messages,
                        include_raw_content=include_raw_content,
                    ),
                    count=len(messages),
                    has_more=has_more,
                    next_cursor=self._next_cursor(messages, has_more),
                )
                return result

            return self._cached(key, build)

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
        with self._lock:
            snapshot = self._prepare()
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
                        "Multiple chats match; use search_chats to choose a chat_id."
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
        with self._lock:
            snapshot = self._prepare()
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
