from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
from contextlib import closing
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

from . import legacy_reader
from .find_all_keys_windows import extract_all_keys
from .image_key_scanner import find_key_for_dat, try_key
from .contracts import validate_limit
from .key_utils import get_key_info
from .key_scan_common import PAGE_SZ, verify_enc_key
from .runtime_state import RuntimeState


MESSAGE_DB_RE = re.compile(r"^message_(\d+)\.db$", re.IGNORECASE)
DEFAULT_CACHE_AGE_SECONDS = 60 * 60


def normalize_chat_search_text(value: Any) -> str:
    """Normalize human chat-name clues without making punctuation significant."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and not unicodedata.category(character).startswith(("P", "S"))
    )


def _chat_match_score(query: str, value: str, field_priority: int) -> float:
    normalized_value = normalize_chat_search_text(value)
    if not query or not normalized_value:
        return 0.0
    if normalized_value == query:
        return 1000.0 - field_priority * 10
    if normalized_value.startswith(query):
        return 850.0 - field_priority * 10
    if query in normalized_value:
        return 700.0 - field_priority * 10
    if len(query) >= 2:
        query_position = 0
        for character in normalized_value:
            if character == query[query_position]:
                query_position += 1
                if query_position == len(query):
                    return 520.0 - field_priority * 10
        ratio = SequenceMatcher(None, query, normalized_value).ratio()
        if ratio >= 0.55:
            return 350.0 * ratio - field_priority * 10
    return 0.0


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
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
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
        RuntimeState(paths.root).save_pending_db_dir(selected)
    return selected


class LocalHistoryBackend:
    def __init__(
        self,
        paths: RuntimePaths | None = None,
        *,
        reader: Any = legacy_reader,
        key_scanner: Callable[[str, str], Any] = extract_all_keys,
        clock: Callable[[], float] = time.monotonic,
        cache_age_seconds: float = DEFAULT_CACHE_AGE_SECONDS,
    ):
        self.paths = paths or RuntimePaths()
        self.reader = reader
        self.key_scanner = key_scanner
        self.clock = clock
        self.cache_age_seconds = float(cache_age_seconds)
        self.db_dir: Path | None = None
        self._last_signature = ""
        self._last_refresh_monotonic: float | None = None
        self._snapshot: dict[str, Any] = {}
        self._reader_config_key: tuple[str, str, str, str, str] | None = None
        self._reader_keys_signature = ""
        self._message_db_keys: list[str] = []
        self._candidate_cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        self._group_member_counts_cache: dict[str, int] | None = None
        self._message_tables_cache: dict[str, list[dict[str, str]]] = {}
        self._search_context_cache: dict[str, list[dict[str, Any]]] = {}

    def prepare(
        self,
        *,
        configured_db_dir: str | os.PathLike[str] | None = None,
        allow_discovery: bool = False,
        allow_key_scan: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        previous_db_dir = self.db_dir
        try:
            return self._prepare(
                configured_db_dir=configured_db_dir,
                allow_discovery=allow_discovery,
                allow_key_scan=allow_key_scan,
                force=force,
            )
        except Exception:
            self.db_dir = previous_db_dir
            raise

    def _prepare(
        self,
        *,
        configured_db_dir: str | os.PathLike[str] | None = None,
        allow_discovery: bool = False,
        allow_key_scan: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        explicit_configuration = configured_db_dir is not None or allow_discovery or allow_key_scan
        force = force or explicit_configuration
        now = self.clock()
        if (
            not force
            and self._snapshot
            and self._last_refresh_monotonic is not None
            and now - self._last_refresh_monotonic < self.cache_age_seconds
        ):
            snapshot = dict(self._snapshot)
            snapshot.update(
                {
                    "cache_status": "reused",
                    "cache_age_seconds": max(0.0, now - self._last_refresh_monotonic),
                    "source_changed": False,
                }
            )
            return snapshot

        self.paths.ensure()
        if configured_db_dir is None and not allow_discovery and self.db_dir is not None:
            configured_db_dir = self.db_dir
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
        if allow_key_scan:
            RuntimeState(self.paths.root).save_pending_db_dir(self.db_dir)

        keys = _read_json(self.paths.keys)
        missing, invalid = self._validate_keys(databases, keys)
        if (missing or invalid) and not allow_key_scan:
            self._raise_key_validation(missing, invalid)
        staged_keys: Path | None = None
        validation: dict[str, int] = {}
        if missing or invalid:
            descriptor, temporary = tempfile.mkstemp(
                prefix="keys-pending-",
                suffix=".json",
                dir=self.paths.root,
            )
            os.close(descriptor)
            staged_keys = Path(temporary)
            try:
                self.key_scanner(str(self.db_dir), str(staged_keys))
            except Exception as exc:
                staged_keys.unlink(missing_ok=True)
                message = str(exc)
                status = "wechat_not_running" if "未运行" in message or "not running" in message.casefold() else "keys_missing"
                raise ReaderUnavailableError(
                    f"Key refresh failed ({type(exc).__name__}); no existing keys were replaced.",
                    status=status,
                ) from exc
        try:
            if staged_keys is not None:
                keys = _read_json(staged_keys)
                missing, invalid = self._validate_keys(databases, keys)
                if missing or invalid:
                    self._raise_key_validation(missing, invalid)
            if allow_key_scan:
                validation = self.validate_databases(databases, keys)
            if staged_keys is not None:
                staged_keys.replace(self.paths.keys)
        finally:
            if staged_keys is not None:
                staged_keys.unlink(missing_ok=True)

        signature = source_signature(databases)
        keys_signature = hashlib.sha256(
            json.dumps(keys, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        account_cache = hashlib.sha256(
            (os.path.normcase(str(self.db_dir)) + "\0" + keys_signature).encode("utf-8")
        ).hexdigest()[:24]
        reader_config_key = (
            str(self.db_dir),
            str(self.paths.keys),
            str(self.paths.decrypted / account_cache),
            str(self.paths.decoded_images / account_cache),
            str(self.paths.cache / account_cache),
        )
        source_changed = signature != self._last_signature
        keys_changed = keys_signature != self._reader_keys_signature
        first_configuration = self._reader_config_key is None
        try:
            if first_configuration or reader_config_key != self._reader_config_key:
                self.reader.configure_reader(*reader_config_key)
                self._reader_config_key = reader_config_key
            elif source_changed or keys_changed:
                self._refresh_reader_metadata()
        except Exception as exc:
            raise ReaderUnavailableError(
                "The cached WeChat keys do not match this database path. Reinitialize this account.",
                status="key_mismatch",
            ) from exc

        if first_configuration or source_changed or keys_changed:
            self._clear_query_metadata_caches(
                invalidate_reader_cache=not first_configuration and (source_changed or keys_changed)
            )
        self._message_db_keys = discover_message_db_keys(self.db_dir)
        self._last_signature = signature
        self._reader_keys_signature = keys_signature
        self._last_refresh_monotonic = now
        self._snapshot = {
            "snapshot_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_changed": source_changed,
            "source_signature": signature,
            "database_count": len(databases),
            "key_validated_count": len(databases),
            **validation,
            "cache_status": "refreshed",
            "cache_age_seconds": 0.0,
            "refresh_reason": "forced" if force else "expired_or_initial",
        }
        return dict(self._snapshot)

    def _validate_keys(self, databases: list[Path], keys: dict[str, Any]) -> tuple[int, int]:
        missing = invalid = 0
        for index, path in enumerate(databases, 1):
            info = get_key_info(keys, _normalize_rel(path, self.db_dir))
            if info is None:
                missing += 1
                continue
            try:
                enc_key = bytes.fromhex(info["enc_key"])
                if len(enc_key) != 32:
                    raise ValueError("invalid key length")
            except (ValueError, TypeError, KeyError):
                invalid += 1
                continue
            try:
                with path.open("rb") as handle:
                    first_page = handle.read(PAGE_SZ)
            except OSError as exc:
                raise ReaderUnavailableError(
                    f"Database {index} could not be read during key validation ({type(exc).__name__}).",
                    status="database_unavailable",
                ) from exc
            if len(first_page) != PAGE_SZ or not verify_enc_key(enc_key, first_page):
                invalid += 1
        return missing, invalid

    @staticmethod
    def _raise_key_validation(missing: int, invalid: int) -> None:
        raise ReaderUnavailableError(
            f"Database key validation failed: missing={missing}, invalid={invalid}. Reinitialize this account.",
            status="key_mismatch" if invalid else "keys_missing",
        )

    def validate_databases(self, databases: list[Path], keys: dict[str, Any]) -> dict[str, int]:
        """Prove fresh decryption opens as SQLite without querying chat rows."""
        opened = 0
        with tempfile.TemporaryDirectory(prefix="validate-", dir=self.paths.root) as temporary:
            for index, path in enumerate(databases, 1):
                destination = Path(temporary) / f"database-{index}.db"
                info = get_key_info(keys, _normalize_rel(path, self.db_dir))
                try:
                    legacy_reader.full_decrypt(
                        str(path),
                        str(destination),
                        bytes.fromhex(info["enc_key"]),
                    )
                    with closing(
                        sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)
                    ) as connection:
                        connection.execute("PRAGMA schema_version").fetchone()
                        connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
                    opened += 1
                except (OSError, ValueError, sqlite3.Error) as exc:
                    code = getattr(exc, "sqlite_errorcode", None)
                    raise ReaderUnavailableError(
                        f"Database {index} failed fresh SQLite validation ({type(exc).__name__}, sqlite_code={code}).",
                        status="database_open_failed",
                    ) from exc
                finally:
                    destination.unlink(missing_ok=True)
        return {"database_opened_count": opened}

    def _refresh_reader_metadata(self) -> None:
        refresh = getattr(self.reader, "refresh_reader", None)
        if callable(refresh):
            refresh(str(self.paths.keys))
            return
        # Keep compatibility with older reader implementations. The bundled
        # reader has refresh_reader(), so normal refreshes preserve its DBCache.
        self.reader.configure_reader(
            str(self.db_dir),
            str(self.paths.keys),
            str(self.paths.decrypted),
            str(self.paths.decoded_images),
            str(self.paths.cache),
        )

    def _clear_query_metadata_caches(self, *, invalidate_reader_cache: bool = False) -> None:
        self._candidate_cache.clear()
        self._group_member_counts_cache = None
        self._message_tables_cache.clear()
        self._search_context_cache.clear()
        if invalidate_reader_cache:
            invalidate = getattr(self.reader, "invalidate_contact_cache", None)
            if callable(invalidate):
                invalidate()

    def snapshot_is_current(self, snapshot: dict[str, Any]) -> bool:
        if self.db_dir is None:
            return False
        databases = discover_encrypted_databases(self.db_dir)
        return source_signature(databases) == snapshot.get("source_signature")

    def _group_member_counts(self) -> dict[str, int]:
        if self._group_member_counts_cache is not None:
            return dict(self._group_member_counts_cache)
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
        self._group_member_counts_cache = dict(result)
        return result

    def find_chat_candidates(
        self,
        query: str,
        *,
        groups_only: bool = False,
        member_count: int | None = None,
        min_member_count: int | None = None,
    ) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        query_folded = normalize_chat_search_text(query)
        if not query_folded:
            return []
        cache_key = (query_folded, groups_only, member_count, min_member_count)
        cached = self._candidate_cache.get(cache_key)
        if cached is not None:
            return [dict(candidate) for candidate in cached]

        if query.startswith("wxid_") or "@chatroom" in query:
            is_group = "@chatroom" in query
            if groups_only and not is_group:
                return []
            member_counts = self._group_member_counts() if is_group else {}
            candidate = {
                "id": query,
                "display_name": self.reader.get_contact_names().get(query, query),
                "nick_name": "",
                "remark": "",
                "is_group": is_group,
                "member_count": member_counts.get(query),
            }
            self._candidate_cache[cache_key] = [dict(candidate)]
            return [candidate]

        contacts = self.reader.get_contact_full()
        member_counts = self._group_member_counts()
        candidates: list[tuple[float, dict[str, Any]]] = []
        seen: set[str] = set()

        for contact in contacts:
            username = contact.get("username", "")
            nick_name = contact.get("nick_name", "")
            remark = contact.get("remark", "")
            display_name = remark or nick_name or username
            is_group = "@chatroom" in username
            if groups_only and not is_group:
                continue
            fields = [display_name, nick_name, remark, username]
            score = max(
                (_chat_match_score(query_folded, value, priority) for priority, value in enumerate(fields)),
                default=0.0,
            )
            if not score or username in seen:
                continue
            seen.add(username)
            candidates.append(
                (
                    score,
                    {
                        "id": username,
                        "display_name": display_name,
                        "nick_name": nick_name,
                        "remark": remark,
                        "is_group": is_group,
                        "member_count": member_counts.get(username),
                    },
                )
            )

        def count_rank(item: tuple[float, dict[str, Any]]) -> tuple[int, int, float, str]:
            score, candidate = item
            actual = candidate.get("member_count")
            if actual is None:
                return (1, 10**9, -score, candidate["id"])
            try:
                actual_count = int(actual)
            except (TypeError, ValueError):
                return (1, 10**9, -score, candidate["id"])
            min_penalty = 0 if min_member_count is None or actual_count >= min_member_count else 1
            distance = abs(actual_count - member_count) if member_count is not None else 0
            return (min_penalty, distance, -score, candidate["id"])

        candidates.sort(key=count_rank)
        exact_candidates = [candidate for score, candidate in candidates if score >= 990.0]
        selected = exact_candidates or [candidate for _, candidate in candidates]
        self._candidate_cache[cache_key] = [dict(candidate) for candidate in selected]
        return selected

    def search_chats(
        self,
        query: str,
        *,
        limit: int = 20,
        member_count: int | None = None,
        min_member_count: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.find_chat_candidates(
            query,
            groups_only=True,
            member_count=member_count,
            min_member_count=min_member_count,
        )[: validate_limit(limit)]

    def _message_tables_for_chat(self, chat_id: str) -> list[dict[str, str]]:
        cached = self._message_tables_cache.get(chat_id)
        if cached is not None:
            return [dict(table) for table in cached]
        table_name = f"Msg_{hashlib.md5(chat_id.encode()).hexdigest()}"
        matches = []
        rel_keys = self._message_db_keys or discover_message_db_keys(self.db_dir)
        for rel_key in rel_keys:
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
        self._message_tables_cache[chat_id] = [dict(table) for table in matches]
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
        include_raw_content: bool = False,
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
        record = {
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
        }
        if include_raw_content:
            record["raw_content"] = raw_content
        return record

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
        include_raw_content: bool = False,
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
                include_raw_content=include_raw_content,
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
        include_raw_content: bool = False,
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
                    include_raw_content=include_raw_content,
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
                    include_raw_content=True,
                )
            )
        entries.sort(key=lambda item: (item["timestamp_unix"], item["message_id"]))
        yield from entries

    def _search_contexts_for_db(
        self,
        rel_key: str,
        db_path: str,
        names: dict[str, str],
        connection: sqlite3.Connection,
    ) -> list[dict[str, Any]]:
        cached = self._search_context_cache.get(rel_key)
        if cached is not None:
            return [dict(context) for context in cached]
        contexts = self.reader._load_search_contexts_from_db(connection, db_path, names)
        self._search_context_cache[rel_key] = [dict(context) for context in contexts]
        return contexts

    def read_recent_records(
        self,
        *,
        limit: int,
        before: dict[str, Any] | None,
        keyword: str,
        start_time: str,
        end_time: str,
        include_raw_content: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        if not keyword and not start_time and not end_time:
            session_page = self._read_unfiltered_recent_from_sessions(
                limit=limit,
                before=before,
                include_raw_content=include_raw_content,
            )
            if session_page is not None:
                return session_page

        names = self.reader.get_contact_names()
        entries = []
        candidate_limit = limit + 1
        rel_keys = self._message_db_keys or discover_message_db_keys(self.db_dir)
        for rel_key in rel_keys:
            db_path = self.reader._cache.get(rel_key)
            if not db_path:
                continue
            with closing(sqlite3.connect(db_path)) as connection:
                contexts = self._search_contexts_for_db(rel_key, db_path, names, connection)
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
                        include_raw_content=include_raw_content,
                    )
                )
        return merge_message_page(entries, limit=limit, before=before)

    def _read_unfiltered_recent_from_sessions(
        self,
        *,
        limit: int,
        before: dict[str, Any] | None,
        include_raw_content: bool = False,
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
        rel_keys = self._message_db_keys or discover_message_db_keys(self.db_dir)
        for rel_key in rel_keys:
            db_path = self.reader._cache.get(rel_key)
            if not db_path:
                continue
            try:
                with closing(sqlite3.connect(db_path)) as connection:
                    contexts = self._search_contexts_for_db(rel_key, db_path, names, connection)
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
                        include_raw_content=include_raw_content,
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
