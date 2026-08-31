from __future__ import annotations

from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from .exporter import HistoryExporter
from .local_backend import LocalHistoryBackend, ReaderUnavailableError, RuntimePaths
from .service import HistoryService


paths = RuntimePaths()
service = HistoryService(
    LocalHistoryBackend(paths),
    HistoryExporter(paths.exports),
    state_root=paths.root,
)
mcp = FastMCP(
    "get-wechat-history",
    instructions=(
        "Read local WeChat history through the configured database path. "
        "Initialize once when setup is missing, use doctor for environment checks, "
        "check freshness once per task, search chats before reading messages, "
        "and use paged reads for compact results."
    ),
)


def _error_result(scope: str, status: str, error: str) -> dict[str, Any]:
    result = {
        "status": status,
        "scope": scope,
        "chat": None,
        "messages": [],
        "count": 0,
        "next_cursor": "",
        "has_more": False,
        "candidates": [],
        "snapshot_at": "",
        "cache_status": "",
        "source_changed": False,
        "error": error,
    }
    if scope == "chat_search":
        result["chats"] = []
    if scope == "freshness":
        result.update(
            {
                "cache_status": "",
                "refreshed": False,
                "source_changed": False,
                "snapshot": {},
            }
        )
    return result


def safe_call(scope: str, callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return callback()
    except ReaderUnavailableError as exc:
        return _error_result(scope, exc.status, str(exc))
    except Exception as exc:
        return _error_result(scope, "error", str(exc))


@mcp.tool()
def initialize_wechat_history(db_dir: str = "", discover: bool = False) -> dict[str, Any]:
    """Prepare the local history reader and persist the selected database path.

    Args:
        db_dir: Known WeChat db_storage directory. Leave empty only when discover is true.
        discover: Permit one controlled local WeChat data-directory discovery during initialization.
    """
    return safe_call(
        "initialize",
        lambda: service.initialize_wechat_history(db_dir=db_dir, discover=discover),
    )


@mcp.tool()
def doctor_wechat_history() -> dict[str, Any]:
    """Check Python, saved path, desktop WeChat process, and cached-key compatibility."""
    return safe_call("doctor", service.doctor_wechat_history)


@mcp.tool()
def ensure_wechat_history_fresh(force: bool = False) -> dict[str, Any]:
    """Check whether the local history snapshot is fresh and refresh changed source state when needed.

    Args:
        force: Check the source now even when the one-hour cache window has not elapsed.
    """
    return safe_call(
        "freshness",
        lambda: service.ensure_wechat_history_fresh(force=force),
    )


@mcp.tool()
def search_chats(
    query: str,
    limit: int = 20,
    member_count: int | None = None,
    min_member_count: int | None = None,
) -> dict[str, Any]:
    """Find likely group chats by fuzzy name, nickname, remark, or chat id.

    Member counts are optional hints used for ranking only; they never filter out a group.
    """
    return safe_call(
        "chat_search",
        lambda: service.search_chats(
            query,
            limit=limit,
            member_count=member_count,
            min_member_count=min_member_count,
        ),
    )


@mcp.tool()
def read_recent_messages(
    limit: int = 100,
    cursor: str = "",
    keyword: str = "",
    start_time: str = "",
    end_time: str = "",
    include_raw_content: bool = False,
) -> dict[str, Any]:
    """Read an actual page of the newest local messages across chats."""
    return safe_call(
        "recent",
        lambda: service.read_recent_messages(
            limit=limit,
            cursor=cursor,
            keyword=keyword,
            start_time=start_time,
            end_time=end_time,
            include_raw_content=include_raw_content,
        ),
    )


@mcp.tool()
def read_chat_history(
    chat: str,
    limit: int = 100,
    cursor: str = "",
    keyword: str = "",
    start_time: str = "",
    end_time: str = "",
    member_count: int | None = None,
    min_member_count: int | None = None,
    include_raw_content: bool = False,
) -> dict[str, Any]:
    """Read a page from one resolved person or group without silently choosing duplicates."""
    return safe_call(
        "chat",
        lambda: service.read_chat_history(
            chat,
            limit=limit,
            cursor=cursor,
            keyword=keyword,
            start_time=start_time,
            end_time=end_time,
            member_count=member_count,
            min_member_count=min_member_count,
            include_raw_content=include_raw_content,
        ),
    )


@mcp.tool()
def export_chat_history(
    chat: str,
    member_count: int | None = None,
    min_member_count: int | None = None,
    start_time: str = "",
    end_time: str = "",
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Export all locally available records for one resolved chat."""
    return safe_call(
        "export",
        lambda: service.export_chat_history(
            chat,
            member_count=member_count,
            min_member_count=min_member_count,
            start_time=start_time,
            end_time=end_time,
            output_dir=output_dir,
        ),
    )


@mcp.tool()
def decode_image(
    chat: str,
    message_id: str,
    output_dir: str | None = None,
    member_count: int | None = None,
    min_member_count: int | None = None,
) -> dict[str, Any]:
    """Decode one image message returned by a history read."""
    return safe_call(
        "image",
        lambda: service.decode_image(
            chat,
            message_id,
            output_dir=output_dir,
            member_count=member_count,
            min_member_count=min_member_count,
        ),
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
