import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from telethon import TelegramClient

import config
import session_meta

logger = logging.getLogger("telegram_inviter_bot.session_checker")


def _short_error_text(text: str, limit: int = 200) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _extract_auth_key_bytes(value: object) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytearray):
        value = bytes(value)
    if isinstance(value, bytes):
        return value if len(value) >= 32 else None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            data = bytes.fromhex(raw)
            if len(data) >= 32:
                return data
        except Exception:
            pass
        data = raw.encode("utf-8", errors="ignore")
        return data if len(data) >= 32 else None
    return None


def get_session_auth_fingerprint(session_path: Path) -> Optional[str]:
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"file:{session_path.as_posix()}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
        if not cur.fetchone():
            return None
        cur.execute("SELECT auth_key FROM sessions LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        auth_key = _extract_auth_key_bytes(row[0])
        if not auth_key:
            return None
        return "auth:" + hashlib.sha1(auth_key).hexdigest()
    except Exception:
        return None
    finally:
        if conn:
            conn.close()


def get_session_relative_name(session_path: Path) -> str:
    try:
        return str(session_path.relative_to(config.SESSIONS_DIR)).replace("\\", "/")
    except ValueError:
        return session_path.name


def is_account_dead(error_text: str) -> bool:
    lowered = error_text.lower()
    dead_markers = [
        "user_deactivated",
        "user_deactivated_ban",
        "phone_number_banned",
        "user_banned",
        "banned",
        "deleted",
        "phone_number_invalid",
        "phone_code_expired",
        "phone_code_hash_expired",
        "auth_key_invalid",
        "auth_key_perm_empty",
        "auth_key_not_found",
        "security check failed",
        "privacy restricted",
    ]
    return any(marker in lowered for marker in dead_markers)


async def check_session_health(
    session_path: Path,
    proxy: object = None,
) -> tuple[str, str]:
    session_name = get_session_relative_name(session_path)
    client: Optional[TelegramClient] = None
    try:
        client = session_meta.build_client(session_path, proxy=proxy)
        await client.connect()
        if not await client.is_user_authorized():
            return "unauthorized", "аккаунт не авторизован"
        me = await client.get_me()
        if me:
            label = f"@{getattr(me, 'username', '')}" if getattr(me, 'username', '') else f"id:{me.id}"
            return "alive", label
        return "alive", "ок"
    except Exception as exc:
        error_text = _short_error_text(str(exc))
        if is_account_dead(error_text):
            return "dead", error_text
        return "unknown", error_text
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


async def cleanup_sessions(
    *,
    dry_run: bool = False,
    remove_duplicates: bool = True,
    remove_dead: bool = True,
) -> dict:
    all_sessions = sorted(config.SESSIONS_DIR.rglob("*.session"))
    if not all_sessions:
        return {"total": 0, "dead": 0, "duplicates": 0, "kept": 0}

    result = {"total": len(all_sessions), "dead": 0, "duplicates": 0, "kept": 0}

    dead_sessions: list[Path] = []
    alive_sessions: list[Path] = []

    for session_path in all_sessions:
        name = get_session_relative_name(session_path)
        status, detail = await check_session_health(session_path)
        if status == "dead":
            dead_sessions.append(session_path)
            logger.warning("  💀 Мертвый: %s — %s", name, detail)
        elif status == "unauthorized":
            alive_sessions.append(session_path)
            logger.info("  🔒 Не авторизован (можно восстановить): %s", name)
        else:
            alive_sessions.append(session_path)

    result["dead"] = len(dead_sessions)

    for dead_path in dead_sessions:
        if remove_dead and not dry_run:
            _delete_session_files(dead_path)
            logger.info("  🗑 Удален: %s", get_session_relative_name(dead_path))

    if remove_duplicates:
        seen_fingerprints: dict[str, Path] = {}
        for session_path in alive_sessions:
            fp = get_session_auth_fingerprint(session_path)
            if not fp:
                continue
            if fp in seen_fingerprints:
                duplicate_path = session_path
                keeper_path = seen_fingerprints[fp]
                keeper_size = keeper_path.stat().st_size if keeper_path.exists() else 0
                dup_size = duplicate_path.stat().st_size if duplicate_path.exists() else 0
                if dup_size > keeper_size:
                    duplicate_path, keeper_path = keeper_path, duplicate_path
                    seen_fingerprints[fp] = keeper_path

                logger.warning(
                    "  👯 Дубликат: %s ← %s",
                    get_session_relative_name(duplicate_path),
                    get_session_relative_name(keeper_path),
                )
                result["duplicates"] += 1
                if not dry_run:
                    _delete_session_files(duplicate_path)
            else:
                seen_fingerprints[fp] = session_path

    result["kept"] = max(0, result["total"] - result["dead"] - result["duplicates"])
    return result


def _delete_session_files(session_path: Path) -> None:
    for candidate in [session_path, Path(str(session_path) + "-journal"), Path(str(session_path) + "-wal"), Path(str(session_path) + "-shm")]:
        try:
            if candidate.exists():
                candidate.unlink()
        except Exception:
            pass


def deduplicate_warmer_state(state_file: Path) -> None:
    if not state_file.exists():
        return

    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return

    sessions = state.get("sessions", {})
    if not isinstance(sessions, dict):
        return

    all_session_paths = sorted(config.SESSIONS_DIR.rglob("*.session"))
    known_names = {get_session_relative_name(p) for p in all_session_paths}

    stale = [name for name in list(sessions.keys()) if name not in known_names]
    for name in stale:
        del sessions[name]

    if stale:
        try:
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("🎯 Очищено %s устаревших записей из warmer_state.json", len(stale))
        except Exception:
            pass
