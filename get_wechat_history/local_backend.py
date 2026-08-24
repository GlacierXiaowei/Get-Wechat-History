from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import legacy_reader
from .find_all_keys_windows import extract_all_keys
from .image_key_scanner import find_key_for_dat, try_key
from .key_utils import get_key_info
from .runtime_state import RuntimeState


MESSAGE_DB_RE = re.compile(r"^message_(\d+)\.db$", re.IGNORECASE)


class ReaderUnavailableError(RuntimeError):
    def __init__(self, message: str, *, status: str = "error"):
        super().__init__(message)
        self.status = status


class RuntimePaths:
    def __init__(self, state_root: str | os.PathLike[str] | None = None):
        local_app_data = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        self.root = Path(state_root) if state_root else Path(local_app_data) / "GetWechatHistory"
        self.config = self.root / "config.json"
        self.keys = self.root / "all_keys.json"
        self.decrypted = self.root / "decrypted"
        self.cache = self.root / "cache"
        self.exports = self.root / "exports"
        self.decoded_images = self.root / "decoded_images"

    def ensure(self) -> None:
        for path in (self.root, self.decrypted, self.cache, self.exports, self.decoded_images):
            path.mkdir(parents=True, exist_ok=True)


def _normalize_rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def discover_message_db_keys(db_dir: str | os.PathLike[str]) -> list[str]:
    message_dir = Path(db_dir) / "message"
    if not message_dir.is_dir():
        return []
    matches = [path for path in message_dir.iterdir() if path.is_file() and MESSAGE_DB_RE.match(path.name)]
    matches.sort(key=lambda path: int(MESSAGE_DB_RE.match(path.name).group(1)))
    return [_normalize_rel(path, Path(db_dir)) for path in matches]


def discover_encrypted_databases(db_dir: str | os.PathLike[str]) -> list[Path]:
    root = Path(db_dir)
    if not root.is_dir():
        return []
    return sorted(
        (path for path in root.rglob("*.db") if path.is_file()),
        key=lambda path: path.as_posix().casefold(),
    )


def source_signature(db_paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for db_path in sorted((Path(path) for path in db_paths), key=lambda path: str(path).casefold()):
        for candidate in (db_path, Path(str(db_path) + "-wal")):
            try:
                stat = candidate.stat()
            except OSError:
                continue
            digest.update(str(candidate).encode("utf-8", errors="surrogatepass"))
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest()


def count_top_level_repeated_field(data: bytes | None, field_number: int) -> int | None:
    if not data:
        return None
    position = 0
    count = 0
    length = len(data)
    try:
        while position < length:
            tag = 0
            shift = 0
            while position < length:
                byte = data[position]
                position += 1
                tag |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
                if shift > 63:
                    return None
            current_field = tag >> 3
            wire_type = tag & 0x07
            if current_field == field_number:
                count += 1
            if wire_type == 0:
                while position < length and data[position] & 0x80:
                    position += 1
                position += 1
            elif wire_type == 1:
                position += 8
            elif wire_type == 2:
                value_length = 0
                shift = 0
                while position < length:
                    byte = data[position]
                    position += 1
                    value_length |= (byte & 0x7F) << shift
                    if not byte & 0x80:
                        break
                    shift += 7
                    if shift > 63:
                        return None
                position += value_length
            elif wire_type == 5:
                position += 4
            else:
                return None
            if position > length:
                return None
    except (IndexError, TypeError):
        return None
    return count


def merge_message_page(
    entries: Iterable[dict[str, Any]],
    *,
    limit: int,
    before: dict[str, Any] | None = None,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    def rank(item):
        source_db = item.get("source_db") or str(item["message_id"]).rsplit(":", 1)[0]
        return item["timestamp_unix"], source_db, item.get("local_id", 0)

    ranked = sorted(entries, key=rank, reverse=True)
    if before:
        anchor = (before["timestamp_unix"], before["source_db"], before["local_id"])
        ranked = [item for item in ranked if rank(item) < anchor]
    selected = ranked[offset : offset + limit]
    selected.sort(key=rank)
    return selected, len(ranked) > offset + limit


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _candidate_activity(path: Path) -> int:
    targets = [path / "message", path / "session" / "session.db"]
    values = []
    for target in targets:
        try:
            values.append(target.stat().st_mtime_ns)
        except OSError:
            pass
    return max(values, default=0)


def detect_db_dir(
    paths: RuntimePaths,
    *,
    allow_discovery: bool = True,
    configured_db_dir: str | os.PathLike[str] | None = None,
    persist_discovery: bool = True,
) -> Path:
    configured = str(configured_db_dir or "").strip()
    if not configured:
        configured = os.environ.get("GET_WECHAT_HISTORY_DB_DIR", "").strip()
    if not configured:
        configured = str(_read_json(paths.config).get("db_dir", "")).strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_dir():
            return configured_path.resolve()
        raise ReaderUnavailableError(
            f"The configured WeChat database path is unavailable: {configured_path}",
            status="configuration_invalid",
        )

    if not allow_discovery:
        raise ReaderUnavailableError(
            "No WeChat database path is configured. Run initialize_wechat_history first.",
            status="configuration_missing",
        )

    appdata = os.environ.get("APPDATA", "")
    config_dir = Path(appdata) / "Tencent" / "xwechat" / "config"
    roots: list[Path] = []
    if config_dir.is_dir():
        for ini_path in config_dir.glob("*.ini"):
            for encoding in ("utf-8", "gbk"):
                try:
                    content = ini_path.read_text(encoding=encoding).strip()
                    break
                except UnicodeDecodeError:
                    continue
                except OSError:
                    content = ""
                    break
            else:
                content = ""
            if content and not any(character in content for character in "\r\n\x00"):
                root = Path(content)
                if root.is_dir():
                    roots.append(root)

    candidates: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        for candidate in (root / "xwechat_files").glob("*/db_storage"):
            normalized = os.path.normcase(str(candidate.resolve()))
            if candidate.is_dir() and normalized not in seen:
                candidates.append(candidate.resolve())
                seen.add(normalized)

    if not candidates:
        raise ReaderUnavailableError(
            "No local WeChat database was detected. Open and sign in to WeChat once, then retry.",
            status="configuration_missing",
        )
    selected = max(candidates, key=_candidate_activity)
    if persist_discovery:
        RuntimeState(paths.root).save_db_dir(selected)
    return selected


class LocalHistoryBackend:
    def __init__(
        self,
        paths: RuntimePaths | None = None,
        *,
        reader: Any = legacy_reader,
        key_scanner: Callable[[str, str], Any] = extract_all_keys,
    ):
        self.paths = paths or RuntimePaths()
        self.reader = reader
        self.key_scanner = key_scanner
        self.db_dir: Path | None = None
        self._last_signature = ""

    def prepare(
        self,
        *,
        configured_db_dir: str | os.PathLike[str] | None = None,
        allow_discovery: bool = False,
        allow_key_scan: bool = False,
    ) -> dict[str, Any]:
        self.paths.ensure()
        self.db_dir = detect_db_dir(
            self.paths,
            allow_discovery=allow_discovery,
            configured_db_dir=configured_db_dir,
            persist_discovery=False,
        )
        databases = discover_encrypted_databases(self.db_dir)
        if not databases:
            raise ReaderUnavailableError(
                f"No encrypted WeChat databases were found under {self.db_dir}.",
                status="database_missing",
            )

        keys = _read_json(self.paths.keys)
        missing = [
            path
            for path in databases
            if get_key_info(keys, _normalize_rel(path, self.db_dir)) is None
        ]
        if missing and not allow_key_scan:
            names = ", ".join(_normalize_rel(path, self.db_dir) for path in missing[:5])
            raise ReaderUnavailableError(
                f"Missing decryption keys for: {names}. Initialize while desktop WeChat is running.",
                status="keys_missing",
            )
        if missing:
            try:
                self.key_scanner(str(self.db_dir), str(self.paths.keys))
            except Exception as exc:
                message = str(exc)
                status = "wechat_not_running" if "未运行" in message or "not running" in message.casefold() else "keys_missing"
                raise ReaderUnavailableError(
                    "WeChat database keys are unavailable. Keep desktop WeChat signed in and retry. "
                    f"Key refresh failed: {exc}",
                    status=status,
                ) from exc
            keys = _read_json(self.paths.keys)
            missing = [
                path
                for path in databases
                if get_key_info(keys, _normalize_rel(path, self.db_dir)) is None
            ]
            if missing:
                names = ", ".join(_normalize_rel(path, self.db_dir) for path in missing[:5])
                raise ReaderUnavailableError(
                    f"Missing decryption keys for: {names}",
                    status="keys_missing",
                )

        try:
            self.reader.configure_reader(
                str(self.db_dir),
                str(self.paths.keys),
                str(self.paths.decrypted),
                str(self.paths.decoded_images),
                str(self.paths.cache),
            )
        except Exception as exc:
            raise ReaderUnavailableError(
                "The cached WeChat keys do not match this database path. Reinitialize this account.",
                status="key_mismatch",
            ) from exc
        if allow_discovery:
            RuntimeState(self.paths.root).save_db_dir(self.db_dir)
        signature = source_signature(databases)
        changed = signature != self._last_signature
        self._last_signature = signature
        return {
            "snapshot_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_changed": changed,
            "source_signature": signature,
            "database_count": len(databases),
        }

    def snapshot_is_current(self, snapshot: dict[str, Any]) -> bool:
        if self.db_dir is None:
            return False
        databases = discover_encrypted_databases(self.db_dir)
        return source_signature(databases) == snapshot.get("source_signature")

    def _group_member_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        db_path = self.reader._get_contact_db_path()
        if not db_path:
            return result
        try:
            with closing(sqlite3.connect(db_path)) as connection:
                rows = connection.execute("SELECT username, ext_buffer FROM chat_room").fetchall()
        except sqlite3.Error:
            return result
        for username, ext_buffer in rows:
            count = count_top_level_repeated_field(ext_buffer, 1)
            if count is not None:
                result[username] = count
        return result

    def find_chat_candidates(self, query: str) -> list[dict[str, Any]]:
        query_folded = query.casefold()
        contacts = self.reader.get_contact_full()
        member_counts = self._group_member_counts()
        candidates: list[tuple[bool, dict[str, Any]]] = []
        seen: set[str] = set()

        for contact in contacts:
            username = contact.get("username", "")
            nick_name = contact.get("nick_name", "")
            remark = contact.get("remark", "")
            display_name = remark or nick_name or username
            fields = [username, display_name, nick_name, remark]
            exact = any(value and value.casefold() == query_folded for value in fields)
            fuzzy = any(value and query_folded in value.casefold() for value in fields)
            if not (exact or fuzzy) or username in seen:
                continue
            seen.add(username)
            candidates.append(
                (
                    exact,
                    {
                        "id": username,
                        "display_name": display_name,
                        "nick_name": nick_name,
                        "remark": remark,
                        "is_group": "@chatroom" in username,
                        "member_count": member_counts.get(username),
                    },
                )
            )

        if query not in seen and (query.startswith("wxid_") or "@chatroom" in query):
            candidates.append(
                (
                    True,
                    {
                        "id": query,
                        "display_name": self.reader.get_contact_names().get(query, query),
                        "nick_name": "",
                        "remark": "",
                        "is_group": "@chatroom" in query,
                        "member_count": member_counts.get(query),
                    },
                )
            )

        exact_candidates = [candidate for exact, candidate in candidates if exact]
        selected = exact_candidates or [candidate for _, candidate in candidates]
        selected.sort(key=lambda candidate: (candidate["display_name"].casefold(), candidate["id"]))
        return selected

    def _message_tables_for_chat(self, chat_id: str) -> list[dict[str, str]]:
        table_name = f"Msg_{hashlib.md5(chat_id.encode()).hexdigest()}"
        matches = []
        for rel_key in discover_message_db_keys(self.db_dir):
            db_path = self.reader._cache.get(rel_key)
            if not db_path:
                continue
            try:
                with closing(sqlite3.connect(db_path)) as connection:
                    exists = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
                    ).fetchone()
            except sqlite3.Error:
                exists = None
            if exists:
                matches.append({"rel_key": rel_key, "db_path": db_path, "table_name": table_name})
        return matches

    @staticmethod
    def _message_type(local_type: int) -> str:
        base_type, _ = legacy_reader._split_msg_type(local_type)
        return {
            1: "text",
            3: "image",
            34: "voice",
            42: "contact_card",
            43: "video",
            47: "sticker",
            48: "location",
            49: "app_or_file",
            50: "call",
            10000: "system",
            10002: "recalled",
        }.get(base_type, f"type_{base_type}")

    def _record_from_row(
        self,
        row: tuple[Any, ...],
        *,
        rel_key: str,
        chat_id: str,
        chat_name: str,
        is_group: bool,
        names: dict[str, str],
        id_to_username: dict[int, str],
    ) -> dict[str, Any] | None:
        local_id, local_type, create_time, real_sender_id, content, content_type = row
        raw_content = self.reader._decompress_content(content, content_type)
        if raw_content is None:
            raw_content = ""
        if not isinstance(raw_content, str):
            raw_content = str(raw_content)
        sender_from_content, text = self.reader._format_message_text(
            local_id, local_type, raw_content, is_group, chat_id, chat_name, names
        )
        sender_id = id_to_username.get(real_sender_id, "") or sender_from_content
        sender_name = self.reader._resolve_sender_label(
            real_sender_id,
            sender_from_content,
            is_group,
            chat_id,
            chat_name,
            names,
            id_to_username,
        )
        timestamp_unix = int(create_time or 0)
        return {
            "message_id": f"{rel_key}:{local_id}",
            "source_db": rel_key,
            "local_id": int(local_id),
            "timestamp": datetime.fromtimestamp(timestamp_unix).astimezone().isoformat(timespec="seconds"),
            "timestamp_unix": timestamp_unix,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "is_group": is_group,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "type": self._message_type(local_type),
            "type_id": int(local_type),
            "text": text or "",
            "raw_content": raw_content,
        }

    def _records_from_table(
        self,
        table: dict[str, str],
        *,
        chat_id: str,
        chat_name: str,
        is_group: bool,
        keyword: str,
        start_time: str,
        end_time: str,
        candidate_limit: int | None,
        before: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        start_ts, end_ts = self.reader._parse_time_range(start_time, end_time)
        names = self.reader.get_contact_names()
        with closing(sqlite3.connect(table["db_path"])) as connection:
            id_to_username = self.reader._load_name2id_maps(connection)
            clauses, params = self.reader._build_message_filters(start_ts, end_ts, keyword)
            if before:
                anchor_time = before["timestamp_unix"]
                anchor_source = before["source_db"]
                if table["rel_key"] < anchor_source:
                    clauses.append("create_time <= ?")
                    params.append(anchor_time)
                elif table["rel_key"] == anchor_source:
                    clauses.append("(create_time < ? OR (create_time = ? AND local_id < ?))")
                    params.extend((anchor_time, anchor_time, before["local_id"]))
                else:
                    clauses.append("create_time < ?")
                    params.append(anchor_time)
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"""
                SELECT local_id, local_type, create_time, real_sender_id, message_content,
                       WCDB_CT_message_content
                FROM [{table['table_name']}]
                {where_sql}
                ORDER BY create_time DESC, local_id DESC
            """
            if candidate_limit is not None:
                sql += "\nLIMIT ?"
                params.append(candidate_limit)
            rows = connection.execute(sql, params).fetchall()
        records = []
        for row in rows:
            record = self._record_from_row(
                row,
                rel_key=table["rel_key"],
                chat_id=chat_id,
                chat_name=chat_name,
                is_group=is_group,
                names=names,
                id_to_username=id_to_username,
            )
            if record is not None:
                records.append(record)
        return records

    def read_chat_records(
        self,
        chat_id: str,
        *,
        limit: int,
        before: dict[str, Any] | None,
        keyword: str,
        start_time: str,
        end_time: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        names = self.reader.get_contact_names()
        chat_name = names.get(chat_id, chat_id)
        entries = []
        for table in self._message_tables_for_chat(chat_id):
            entries.extend(
                self._records_from_table(
                    table,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    is_group="@chatroom" in chat_id,
                    keyword=keyword,
                    start_time=start_time,
                    end_time=end_time,
                    candidate_limit=limit + 1,
                    before=before,
                )
            )
        return merge_message_page(entries, limit=limit, before=before)

    def iter_chat_records(self, chat_id: str, *, start_time: str, end_time: str):
        names = self.reader.get_contact_names()
        chat_name = names.get(chat_id, chat_id)
        entries = []
        for table in self._message_tables_for_chat(chat_id):
            entries.extend(
                self._records_from_table(
                    table,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    is_group="@chatroom" in chat_id,
                    keyword="",
                    start_time=start_time,
                    end_time=end_time,
                    candidate_limit=None,
                    before=None,
                )
            )
        entries.sort(key=lambda item: (item["timestamp_unix"], item["message_id"]))
        yield from entries

    def read_recent_records(
        self,
        *,
        limit: int,
        before: dict[str, Any] | None,
        keyword: str,
        start_time: str,
        end_time: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        if not keyword and not start_time and not end_time:
            session_page = self._read_unfiltered_recent_from_sessions(limit=limit, before=before)
            if session_page is not None:
                return session_page

        names = self.reader.get_contact_names()
        entries = []
        candidate_limit = limit + 1
        for rel_key in discover_message_db_keys(self.db_dir):
            db_path = self.reader._cache.get(rel_key)
            if not db_path:
                continue
            with closing(sqlite3.connect(db_path)) as connection:
                contexts = self.reader._load_search_contexts_from_db(connection, db_path, names)
            for context in contexts:
                table = {"rel_key": rel_key, "db_path": db_path, "table_name": context["table_name"]}
                entries.extend(
                    self._records_from_table(
                        table,
                        chat_id=context["username"] or context["table_name"],
                        chat_name=context["display_name"],
                        is_group=context["is_group"],
                        keyword=keyword,
                        start_time=start_time,
                        end_time=end_time,
                        candidate_limit=candidate_limit,
                        before=before,
                    )
                )
        return merge_message_page(entries, limit=limit, before=before)

    def _read_unfiltered_recent_from_sessions(
        self, *, limit: int, before: dict[str, Any] | None
    ) -> tuple[list[dict[str, Any]], bool] | None:
        session_path = self.reader._cache.get("session/session.db")
        if not session_path:
            return None
        try:
            with closing(sqlite3.connect(session_path)) as connection:
                sessions = connection.execute(
                    "SELECT username, last_timestamp FROM SessionTable "
                    "WHERE last_timestamp > 0 ORDER BY last_timestamp DESC"
                ).fetchall()
        except sqlite3.Error:
            return None

        names = self.reader.get_contact_names()
        contexts_by_username: dict[str, list[dict[str, str]]] = {}
        for rel_key in discover_message_db_keys(self.db_dir):
            db_path = self.reader._cache.get(rel_key)
            if not db_path:
                continue
            try:
                with closing(sqlite3.connect(db_path)) as connection:
                    contexts = self.reader._load_search_contexts_from_db(connection, db_path, names)
            except sqlite3.Error:
                continue
            for context in contexts:
                username = context.get("username", "")
                if not username:
                    continue
                contexts_by_username.setdefault(username, []).append(
                    {
                        "rel_key": rel_key,
                        "db_path": db_path,
                        "table_name": context["table_name"],
                        "display_name": context["display_name"],
                        "is_group": context["is_group"],
                    }
                )

        entries: list[dict[str, Any]] = []
        candidate_limit = limit + 1
        for username, last_timestamp in sessions:
            if len(entries) >= candidate_limit:
                ranked = sorted(
                    entries,
                    key=lambda item: (item["timestamp_unix"], item["message_id"]),
                    reverse=True,
                )
                threshold = ranked[candidate_limit - 1]["timestamp_unix"]
                if int(last_timestamp) < threshold:
                    break

            for table in contexts_by_username.get(username, []):
                entries.extend(
                    self._records_from_table(
                        table,
                        chat_id=username,
                        chat_name=table["display_name"],
                        is_group=bool(table["is_group"]),
                        keyword="",
                        start_time="",
                        end_time="",
                        candidate_limit=candidate_limit,
                        before=before,
                    )
                )

        return merge_message_page(entries, limit=limit, before=before)

    def decode_image(self, chat_id: str, message_id: str, output_dir: str | None = None) -> dict[str, Any]:
        try:
            source_db, local_id_text = str(message_id).rsplit(":", 1)
            local_id = int(local_id_text)
        except ValueError as exc:
            raise ValueError("message_id must come from a read result") from exc
        if source_db not in discover_message_db_keys(self.db_dir):
            raise ValueError("message_id references an unknown message database")

        server_id = None
        db_path = self.reader._cache.get(source_db)
        table_name = f"Msg_{hashlib.md5(chat_id.encode()).hexdigest()}"
        if db_path:
            try:
                with closing(sqlite3.connect(db_path)) as connection:
                    row = connection.execute(
                        f"SELECT server_id FROM [{table_name}] WHERE local_id = ? LIMIT 1",
                        (local_id,),
                    ).fetchone()
                if row:
                    server_id = row[0]
            except sqlite3.Error:
                server_id = None
        resolver = self.reader._image_resolver
        if output_dir:
            from .decode_image import ImageResolver

            Path(output_dir).mkdir(parents=True, exist_ok=True)
            resolver = ImageResolver(self.reader.WECHAT_BASE_DIR, output_dir, self.reader._cache)
        decoded = resolver.decode_image(
            chat_id,
            local_id,
            server_id=server_id,
            key_provider=self._provide_image_key,
        )
        if decoded.get("success"):
            return {
                "status": "ok",
                "message_id": message_id,
                "path": decoded.get("path", ""),
                "format": decoded.get("format", ""),
                "size": decoded.get("size", 0),
                "md5": decoded.get("md5", ""),
                "error": None,
            }
        return {
            "status": "error",
            "message_id": message_id,
            "path": "",
            "error": decoded.get("error", "Image decode failed."),
        }

    def _provide_image_key(self, dat_path: str) -> tuple[str | None, int]:
        with open(dat_path, "rb") as handle:
            header = handle.read(31)
        ciphertext = header[15:31] if len(header) >= 31 else b""
        config = _read_json(self.paths.config)
        existing_key = config.get("image_aes_key")
        existing_xor = int(config.get("image_xor_key", 0x88))
        if existing_key and ciphertext and try_key(existing_key.encode("ascii")[:16], ciphertext):
            return existing_key, existing_xor

        key, xor_key = find_key_for_dat(dat_path)
        if key:
            config["image_aes_key"] = key
            config["image_xor_key"] = xor_key
            _write_json_atomic(self.paths.config, config)
        return key, xor_key
