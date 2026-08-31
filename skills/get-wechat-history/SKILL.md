---
name: get-wechat-history
description: Read, initialize, diagnose, search, or export local WeChat chat history through the plugin MCP tools.
---

# Get WeChat History

Requires desktop WeChat and Python 3.10+. This is a Codex Skill + MCP server. On首次使用, its entrypoint creates or reuses the private virtual environment and installs runtime dependencies automatically; users normally do not run a script manually.

Use only the plugin MCP tools. Never use shell commands, scan folders, or substitute an unrelated tool.

## Setup

- For setup or reconfiguration, call `initialize_wechat_history`. Pass a known `db_dir`; use `discover=true` only after explicit permission for one discovery. Keep desktop WeChat logged in during the first initialization so account-specific 密钥 can be obtained. Different accounts or data directories need different keys.
- Call `doctor_wechat_history` for environment questions or failed reads. It checks Python, the saved path, the WeChat process, and cached-key compatibility without changing configuration or keys.
- `configuration_missing` asks for a path or discovery permission. `configuration_invalid` means 路径不可用 and requires a new path. `wechat_not_running` asks the user to open and sign in to desktop WeChat. `key_mismatch` requires initialization for the current account/data directory.
- After `status=ok`, report readiness and wait for the next instruction; do not read history until requested.

## Reading

At the beginning of each new Codex task that needs WeChat history, call `ensure_wechat_history_fresh(force=true)` once. This establishes or checks the task snapshot. Do not call it before every page or every message query.

- The default freshness window is one hour. Later reads in the same task reuse the in-process reader, decrypted database cache, contact metadata, and query results when the request is identical.
- When the window expires, the plugin checks database and WAL signatures, invalidates changed metadata only, and preserves the reader cache for unchanged shards.
- If the user asks for “latest”, “just now”, “refresh”, “recheck”, or not to use cache, call `ensure_wechat_history_fresh(force=true)` again. `force` checks the source immediately; it does not imply rebuilding every cache.

For a vague or approximate group name, search before reading messages:

- Call `search_chats(query)` first. It performs fuzzy matching over group name, nickname, remark, and chat id, with punctuation, spacing, and case normalized.
- `member_count` and `min_member_count` are optional ranking hints only. They must never filter out a group because the user may have supplied an approximate or incorrect count.
- If the search returns one strong match, read it with the returned `chat_id`. If it returns multiple plausible matches, show the minimal choice list and ask which one to use; do not silently select the first weak match.
- Once the target group is known, call `read_chat_history` by `chat_id` instead of running a global keyword scan with `read_recent_messages`.

- `read_recent_messages`: latest messages across chats; the default limit is 100. Continue with `next_cursor` only when needed.
- `read_chat_history`: a named person or group with time, keyword, and page filters. `member_count` is optional and never a hard identity constraint. If `ambiguous`, use `search_chats` and an exact `chat_id`; never select the first weak match silently.
- Ordinary reads return compact message fields: `message_id`, `timestamp`, `sender_name`, `type`, and `text`. Set `include_raw_content=true` only when raw XML or payload evidence is specifically needed. Exports retain the full raw record.
- `export_chat_history`: all locally available records for one resolved chat; omit time bounds for a complete export and return paths/metadata.
- `decode_image`: an image message returned by a history read.

Preserve unknown identities and distinguish message evidence from inference.
