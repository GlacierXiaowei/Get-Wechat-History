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

For every new read request, call a tool again so it checks the current encrypted databases and WAL. Do not reuse an earlier tool response as fresh data.

- `read_recent_messages`: latest messages across chats; choose a practical positive `limit` and continue with `next_cursor` only when needed.
- `read_chat_history`: a named person or group with time, keyword, and page filters. If `ambiguous`, show candidates and use a member-count clue or exact id; never select the first match silently.
- `export_chat_history`: all locally available records for one resolved chat; omit time bounds for a complete export and return paths/metadata.
- `decode_image`: an image message returned by a history read.

Preserve unknown identities and distinguish message evidence from inference.
