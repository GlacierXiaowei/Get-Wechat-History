# Get Wechat History

想要让AI能够快速获取微信聊天记录？其他开源项目操作繁琐，配置困难？
那就使用Get Wechat History！只需安装该插件，就可以一键获取！

Get Wechat History是一个 Codex 插件，用于搜索并读取本机微信聊天记录、查询指定会话、导出记录，以及解码读取结果中的图片。插件会复用一小时内的本地读取缓存，并在需要时只检查发生变化的数据库/WAL。

## 使用前提

- Windows 桌面版微信。
- Python 3.10 或更高版本。如果系统没有 Python，MCP 启动器会在错误输出中提示安装要求。
- 首次初始化时需要保持微信登录并运行，从而获取读取聊天记录所需密钥。

## 安装与首次启动

请下载或克隆完整的仓库

将完整的插件目录放到 Codex 的个人插件目录中：

`%USERPROFILE%\.codex\plugins\get-wechat-history`

例如，Windows 用户名为 `Xiaowei` 时，完整路径就是：

`C:\Users\Xiaowei\.codex\plugins\get-wechat-history`

如果 `.codex\plugins` 文件夹不存在，请先在资源管理器地址栏输入 `%USERPROFILE%`，打开当前用户目录后，再创建 `.codex\plugins\get-wechat-history` 文件夹。

插件目录的根目录必须直接包含以下内容：

- `.codex-plugin`
- `.mcp.json`
- `skills`
- `get_wechat_history`
- `scripts`

文件放置完成后，在 Codex 的插件管理中启用 Get Wechat History。该插件应该登记在个人的插件市场中）。

`codex plugin add get-wechat-history@personal`

插件启用后，用户不需要手动运行 MCP 服务，也不需要手动执行插件目录中的启动脚本。第一次使用时，插件会自动创建自己的 Python 虚拟环境，并准备运行依赖。

## 快速开始

第一次使用前，请确认 Windows 桌面版微信已经安装并登录，并且电脑中已经安装 Python 3.10 或更高版本。

启用插件后，直接告诉 Codex：

“初始化微信聊天记录读取功能。”

如果你知道微信聊天记录所在的目录，可以同时告诉 Codex：

“初始化微信聊天记录读取功能，聊天记录路径是：你的 db_storage 路径。”

如果你不知道聊天记录路径，可以告诉 Codex：

“初始化微信聊天记录读取功能，请自动寻找聊天记录路径。”

第一次初始化时，请保持 Windows 微信处于登录状态。初始化成功后，插件会保存当前配置，之后不需要每次重新查找路径。

初始化完成后，可以直接提出读取需求，例如：

- 读取我最近的微信消息；
- 按模糊群名搜索群聊；
- 读取某个联系人最近的聊天记录；
- 读取某个群聊最近的聊天记录；
- 导出某个群聊的聊天记录；
- 查看某条消息中的图片。

如果更换了微信账号、移动了聊天记录目录，或者插件提示当前配置不可用，需要重新进行初始化。

## 数据与隐私

运行时目录、缓存、解密后的临时文件、导出文件和密钥都保存在 `%LOCALAPPDATA%\GetWechatHistory`，插件本身完全不会上传任何内容至云端。

---

开发相关信息⬇️

---

## 初始化与持久化路径

首次使用或需要更换微信数据目录时，用户需要告知该插件进行初始化，插件将会调用相关方法。

- 初次使用需要定位微信聊天记录所在位置。用户可自行告诉插件或者让插件自行寻找。
- 初始化成功后，相关信息将会保存到本地 `%LOCALAPPDATA%\GetWechatHistory\config.json`
- 每个微信账号或数据目录对应自己的数据库密钥。切换账号、迁移数据目录或出现密钥不匹配时，需要重新初始化。

## 环境与故障检查

用户提出“检查环境”、读取失败，或怀疑路径变化时，插件将会调用 `doctor_wechat_history`。检查配置信息如下：

- Python 版本；
- 已保存路径是否仍然存在；
- 桌面微信进程是否运行；
- 当前缓存密钥是否能用于保存的数据库目录。

常见状态：

- `configuration_missing`：尚未配置路径；请提供 `db_dir`，或明确允许一次发现。
- `configuration_invalid`：保存的路径不可用；请让用户重新配置路径。
- `wechat_not_running`：打开并登录桌面微信，然后重新初始化。
- `keys_missing`：当前目录还没有可用的数据库密钥。
- `key_mismatch`：密钥属于其他账号或数据目录，需要对当前目录重新初始化。
- `ready`：环境和已保存配置可以开始读取。 

## MCP 工具

- `initialize_wechat_history(db_dir="", discover=false)`：首次配置或重新配置。
- `doctor_wechat_history()`：检查环境和当前配置。
- `ensure_wechat_history_fresh(force=false)`：让插件判断缓存是否需要刷新；新任务首次读取时使用 `force=true` 做一次检查。
- `search_chats(query, limit=20, member_count=null, min_member_count=null)`：按群名称、昵称、备注或 id 模糊搜索群聊。人数只用于排序提示，不会过滤结果。
- `read_recent_messages(...)`：读取最新消息分页，默认返回精简字段。
- `read_chat_history(...)`：读取指定联系人或群聊分页；建议先搜索群，再用返回的 `chat_id` 读取。
- `export_chat_history(...)`：导出指定会话的本地记录。
- `decode_image(...)`：解码读取结果中的图片消息。

普通读取默认只返回 `message_id`、`timestamp`、`sender_name`、`type`、`text`。需要核对 XML 或底层载荷时，再传 `include_raw_content=true`；导出仍保留完整原始记录。

读取工具默认复用一小时内的快照、解密缓存和查询结果。用户明确要求“最新消息”“刷新”“重新核验”或“不使用缓存”时，调用 `ensure_wechat_history_fresh(force=true)`。
