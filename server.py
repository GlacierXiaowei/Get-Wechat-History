"""Compatibility entrypoint for older local plugin installations."""

from get_wechat_history.mcp_server import (
    decode_image,
    doctor_wechat_history,
    ensure_wechat_history_fresh,
    export_chat_history,
    initialize_wechat_history,
    mcp,
    read_chat_history,
    read_recent_messages,
    safe_call,
    search_chats,
)


if __name__ == "__main__":
    mcp.run(transport="stdio")
