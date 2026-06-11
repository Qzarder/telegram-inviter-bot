import asyncio
import hashlib
import json
import logging
import random
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    InviteRequestSentError,
    PeerFloodError,
    RPCError,
    UserAlreadyParticipantError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.channels import (
    GetParticipantRequest,
    InviteToChannelRequest,
    JoinChannelRequest,
)
from telethon.tl.types import InputUser

import config
import session_meta
import warmer

logger = logging.getLogger("telegram_inviter_bot.inviter")

UserReportCallback = Callable[[str, str, str], Awaitable[None]]
TextReportCallback = Callable[[str], Awaitable[None]]


def _account_delay_multiplier(session_name: str) -> float:
    digest = int(hashlib.md5(session_name.encode()).hexdigest(), 16)
    return 0.7 + (digest % 101) / 100.0 * 0.6


# ============================================================
# App-like behavior: имитация что юзер пользуется Telegram-приложением,
# а не дёргает API. Снижает score "бот-like" поведения.
# ============================================================


async def _app_session_open(
    client: TelegramClient,
    session_name: str,
) -> None:
    """Имитирует "юзер открыл Telegram":
    - выставляет онлайн-статус
    - открывает список диалогов (как реальный клиент при старте)
    - "залипает" 3-10 сек как будто читает уведомления
    """
    try:
        from telethon.tl.functions.account import UpdateStatusRequest
        try:
            await client(UpdateStatusRequest(offline=False))
        except Exception:
            pass

        # Открыл список диалогов — как реальный клиент
        dialog_count = 0
        async for dialog in client.iter_dialogs(limit=random.randint(10, 25)):
            if dialog:
                dialog_count += 1
            # короткие микро-паузы — глаз скроллит
            if random.random() < 0.3:
                await asyncio.sleep(random.uniform(0.5, 1.5))
        logger.info("  📱 %s: app-open, увидел %d диалогов", session_name, dialog_count)

        # "Залип" на главном экране
        await asyncio.sleep(random.uniform(3, 10))
    except Exception as exc:
        logger.debug("app_session_open(%s): %s", session_name, short_error(exc))


async def _app_session_pre_invite(
    client: TelegramClient,
    target_group: str,
    session_name: str,
) -> None:
    """Пауза "юзер открыл список участников, ввёл имя, выбрал, нажал Добавить".

    Typing-индикатор УБРАН: добавление участника не печатает в чат,
    SetTypingRequest был неестественным сигналом.
    """
    try:
        await asyncio.sleep(random.uniform(3, 9))
    except Exception as exc:
        logger.debug("app_session_pre_invite(%s): %s", session_name, short_error(exc))


async def _app_session_post_invite(
    client: TelegramClient,
    target_group: str,
    session_name: str,
) -> None:
    """Имитация "юзер ещё посидел в приложении после действия":
    - продолжает читать группу
    - открывает другой случайный канал
    - в конце — устанавливает offline (50% шанс)
    """
    try:
        # Продолжил читать целевую группу
        post_invite_read = 0
        async for msg in client.iter_messages(target_group, limit=random.randint(2, 6)):
            if msg and getattr(msg, "id", 0):
                post_invite_read += 1
            await asyncio.sleep(random.uniform(2, 8))

        # 40% шанс — открыл другой случайный канал из подписок
        if random.random() < 0.4:
            try:
                dialogs = []
                async for d in client.iter_dialogs(limit=30):
                    if d and (d.is_channel or d.is_group):
                        dialogs.append(d)
                if dialogs:
                    other = random.choice(dialogs)
                    other_read = 0
                    async for msg in client.iter_messages(other.entity, limit=random.randint(2, 5)):
                        if msg and getattr(msg, "id", 0):
                            other_read += 1
                        await asyncio.sleep(random.uniform(1.5, 5))
                    logger.info("  📱 %s: после инвайта посмотрел ещё @%s (%d постов)",
                                session_name, getattr(other.entity, "username", "?"), other_read)
            except Exception:
                pass

        # В половине случаев — закрыл приложение
        if random.random() < 0.5:
            try:
                from telethon.tl.functions.account import UpdateStatusRequest
                await client(UpdateStatusRequest(offline=True))
            except Exception:
                pass
    except Exception as exc:
        logger.debug("app_session_post_invite(%s): %s", session_name, short_error(exc))


async def pre_invite_engagement(
    client: TelegramClient,
    target_group: str,
    session_name: str,
) -> tuple[bool, str]:
    try:
        # Намеренно не сидируем RNG — каждый раз поведение должно быть разным
        posts_to_read = random.randint(5, 15)
        read_count = 0
        max_seen_id = 0

        logger.info("  👁 Pre-invite: читаю %s постов группы @%s...", posts_to_read, target_group)

        async for message in client.iter_messages(target_group, limit=posts_to_read):
            if message and getattr(message, "id", 0):
                max_seen_id = max(max_seen_id, int(message.id))
                read_count += 1
            # Варьируем паузы: иногда быстро листает, иногда задерживается на посте
            if random.random() < 0.15:
                await asyncio.sleep(random.uniform(8, 20))  # долго читает
            else:
                await asyncio.sleep(random.uniform(1.5, 7.0))

        if max_seen_id > 0:
            await client.send_read_acknowledge(target_group, max_id=max_seen_id)

        # Иногда человек немного "залипает" перед тем как что-то сделать
        if random.random() < 0.4:
            await asyncio.sleep(random.uniform(3, 12))

        return True, f"прочитано {read_count} постов"
    except Exception as exc:
        return False, short_error(exc)


def _invite_history_key(session_name: str) -> str:
    return config.normalize_session_key(session_name)


def _load_invite_history_raw() -> dict:
    ramp_file = config.BASE_DIR / "invite_history.json"
    try:
        raw = json.loads(ramp_file.read_text(encoding="utf-8")) if ramp_file.exists() else {}
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _save_invite_history_raw(raw: dict) -> None:
    ramp_file = config.BASE_DIR / "invite_history.json"
    try:
        ramp_file.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _get_invite_history_entry(session_name: str, raw: Optional[dict] = None) -> dict:
    source = raw if raw is not None else _load_invite_history_raw()
    key = _invite_history_key(session_name)
    entry = source.get(key, {})
    if isinstance(entry, dict) and entry.get("first_invite_date"):
        return entry

    # Миграция старых ключей (phone.session vs path/session)
    basename = Path(session_name.replace("\\", "/")).name.lower()
    for alt_key, alt_val in source.items():
        if not isinstance(alt_val, dict):
            continue
        if alt_key == key or Path(str(alt_key)).name.lower() == basename:
            if alt_val.get("first_invite_date"):
                return alt_val
    return entry if isinstance(entry, dict) else {}


def _daily_invite_limit_for_account(session_name: str) -> int:
    if not config.INVITE_RAMP_UP_ENABLED:
        return _apply_flood_backoff_to_limit(session_name, config.INVITE_MAX_PER_SESSION)

    raw = _load_invite_history_raw()
    key = _invite_history_key(session_name)
    account_data = _get_invite_history_entry(session_name, raw)
    if not isinstance(account_data, dict):
        account_data = {}

    first_date_str = str(account_data.get("first_invite_date", "")).strip()
    now = datetime.now().strftime("%Y-%m-%d")

    if not first_date_str:
        account_data["first_invite_date"] = now
        raw[key] = account_data
        _save_invite_history_raw(raw)
        first_date_str = now

    try:
        first_date = datetime.strptime(first_date_str, "%Y-%m-%d")
        now_dt = datetime.strptime(now, "%Y-%m-%d")
        day_number = (now_dt - first_date).days + 1
    except Exception:
        day_number = 1

    # Консервативный рамп-ап: первые 3 дня полная тишина, потом медленный разгон.
    # Telegram смотрит на историю аккаунта за последние недели.
    if day_number <= 3:
        base = 0  # первые 3 дня — только прогрев, никаких инвайтов
    elif day_number <= 7:
        base = 1
    elif day_number <= 14:
        base = min(2, config.INVITE_MAX_PER_SESSION)
    else:
        base = config.INVITE_MAX_PER_SESSION
    return _apply_flood_backoff_to_limit(session_name, base)


def _should_skip_pre_engage(session_name: str) -> bool:
    return _get_flood_incidents(session_name) > 0


async def _resolve_invite_target(
    client: TelegramClient,
    target_user: str,
) -> tuple[object, Optional[int]]:
    """Резолв цели без глобального SearchRequest (по умолчанию)."""
    clean_name = target_user.lstrip("@")
    target_user_id: Optional[int] = None

    if config.INVITE_ENTITY_CACHE_ENABLED:
        cached = config.get_cached_entity(clean_name)
        if cached:
            target_user_id = cached["id"]
            return (
                InputUser(cached["id"], cached["access_hash"]),
                target_user_id,
            )

    if config.INVITE_USE_SEARCH_REQUEST:
        from telethon.tl.functions.contacts import SearchRequest

        search_res = await client(SearchRequest(q=clean_name, limit=3))
        for user in getattr(search_res, "users", []):
            if (getattr(user, "username", "") or "").lower() == clean_name.lower():
                target_user_id = getattr(user, "id", None)
                access_hash = getattr(user, "access_hash", None)
                if target_user_id is not None and access_hash is not None:
                    config.set_cached_entity(clean_name, int(target_user_id), int(access_hash))
                return user, target_user_id

    user_entity = await client.get_entity(target_user)
    target_user_id = getattr(user_entity, "id", None)
    access_hash = getattr(user_entity, "access_hash", None)
    if target_user_id is not None and access_hash is not None:
        config.set_cached_entity(clean_name, int(target_user_id), int(access_hash))
    return user_entity, target_user_id


def _defer_current_user(target_user: str, *, reason: str) -> None:
    config.defer_username(
        target_user,
        seconds=config.INVITE_DEFER_USER_HOURS * 3600,
        reason=reason,
    )


def _get_first_invite_delay(session_name: str) -> float:
    rng = random.Random(f"first_delay|{session_name.lower()}")
    return rng.randint(config.INVITE_FIRST_DELAY_MIN, config.INVITE_FIRST_DELAY_MAX)


# ============================================================
# Privacy blacklist: юзеры с UserPrivacyRestricted сохраняются и
# больше не пытаемся их инвайтить — сжигает дневной лимит впустую
# ============================================================

_PRIVACY_BLACKLIST_FILE = config.BASE_DIR / "privacy_blacklist.txt"


def _load_privacy_blacklist() -> set[str]:
    if not _PRIVACY_BLACKLIST_FILE.exists():
        return set()
    try:
        lines = _PRIVACY_BLACKLIST_FILE.read_text(encoding="utf-8").splitlines()
        return {line.strip().lower().lstrip("@") for line in lines if line.strip()}
    except Exception:
        return set()


def _add_to_privacy_blacklist(username: str) -> None:
    name = username.strip().lower().lstrip("@")
    if not name:
        return
    try:
        existing = _load_privacy_blacklist()
        if name in existing:
            return
        with _PRIVACY_BLACKLIST_FILE.open("a", encoding="utf-8") as f:
            f.write(name + "\n")
    except Exception as exc:
        logger.debug("blacklist append failed: %s", exc)


# ============================================================
# Backoff после FloodWait: повторные инциденты на одном акке
# снижают дневной лимит вдвое
# ============================================================


def _get_flood_incidents(session_name: str) -> int:
    """Сколько раз акк ловил FloodWait/PeerFlood за последние 7 дней."""
    try:
        if not config.ACCOUNT_COOLDOWNS_FILE.exists():
            return 0
        data = json.loads(config.ACCOUNT_COOLDOWNS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return 0
        key = config.normalize_session_key(session_name)
        record = data.get(key, {})
        if not isinstance(record, dict):
            return 0
        return int(record.get("flood_incidents", 0))
    except Exception:
        return 0


def _increment_flood_incidents(session_name: str) -> int:
    """Увеличивает счётчик FloodWait для аккаунта. Возвращает новое значение."""
    try:
        data = {}
        if config.ACCOUNT_COOLDOWNS_FILE.exists():
            try:
                data = json.loads(config.ACCOUNT_COOLDOWNS_FILE.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
        key = config.normalize_session_key(session_name)
        record = data.get(key, {})
        if not isinstance(record, dict):
            record = {}
        current = int(record.get("flood_incidents", 0))
        record["flood_incidents"] = current + 1
        record["last_flood_at"] = datetime.now().isoformat()
        data[key] = record
        config.ACCOUNT_COOLDOWNS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return current + 1
    except Exception as exc:
        logger.debug("flood incident increment failed: %s", exc)
        return 0


def _apply_flood_backoff_to_limit(session_name: str, base_limit: int) -> int:
    """Снижает дневной лимит вдвое за каждый FloodWait инцидент."""
    incidents = _get_flood_incidents(session_name)
    if incidents <= 0:
        return base_limit
    # Каждый инцидент уменьшает лимит вдвое
    reduced = max(0, int(base_limit * (0.5 ** incidents)))
    return reduced


def normalize_user(raw_value: str) -> str:
    value = raw_value.strip()
    if value.startswith("https://"):
        value = value.removeprefix("https://")
    elif value.startswith("http://"):
        value = value.removeprefix("http://")

    if value.startswith("t.me/"):
        value = value.removeprefix("t.me/")

    if value.startswith("@"):
        value = value.removeprefix("@")

    return value.strip()


def is_terminal_user_error(error_text: str) -> bool:
    terminal_patterns = [
        "no user has",
        "nobody is using this username",
        "username is unacceptable",
        "not a mutual contact",
        "user already participant",
        "user privacy restricted",
    ]
    return any(pattern in error_text for pattern in terminal_patterns)


def is_permission_error(error_text: str) -> bool:
    permission_patterns = [
        "you can't write in this chat",
        "chat write forbidden",
    ]
    return any(pattern in error_text for pattern in permission_patterns)


def is_invite_ban_error(error_text: str) -> bool:
    invite_ban_patterns = [
        "peer flood",
        "too many requests",
        "flood test phone wait",
        "invite request sent too much",
    ]
    return any(pattern in error_text for pattern in invite_ban_patterns)


def is_proxy_related_error(error_text: str) -> bool:
    proxy_patterns = [
        "proxy",
        "socks",
        "connection refused",
        "timed out",
        "network is unreachable",
        "host is unreachable",
        "connection reset",
        "connection aborted",
        "cannot connect",
        "all connection attempts failed",
    ]
    return any(pattern in error_text for pattern in proxy_patterns)


def extract_proxy_endpoint(proxy: object) -> tuple[str, int]:
    if not isinstance(proxy, (tuple, list)) or len(proxy) < 3:
        raise ValueError("Некорректный формат прокси")

    host = str(proxy[1]).strip()
    port = int(proxy[2])
    if not host:
        raise ValueError("Пустой адрес прокси")
    if port <= 0:
        raise ValueError("Некорректный порт прокси")

    return host, port


async def is_proxy_reachable(proxy: object, timeout_seconds: int) -> tuple[bool, str]:
    writer = None
    try:
        host, port = extract_proxy_endpoint(proxy)
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=max(1, timeout_seconds),
        )
        return True, ""
    except Exception as exc:
        return False, short_error(exc)
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def format_duration(seconds: int) -> str:
    total = max(0, int(seconds))
    if total == 0:
        return "0с"

    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}м")
    if secs and not parts:
        parts.append(f"{secs}с")

    return " ".join(parts) if parts else "0с"


def classify_spambot_text(raw_text: str) -> tuple[str, str]:
    text = raw_text.strip()
    lowered = text.lower()

    ok_patterns = [
        "good news, no limits",
        "no limits are currently applied",
        "you can use telegram freely",
        "ограничений не обнаружено",
        "ограничений на ваш аккаунт нет",
        "в данный момент у вашего аккаунта нет ограничений",
        "свободен от каких-либо ограничений",
    ]
    limited_patterns = [
        "you are limited",
        "we have found limitations",
        "sending too many messages",
        "temporary limitation",
        "temporarily limited",
        "this account is limited",
        "на ваш аккаунт наложены ограничения",
        "ваш аккаунт ограничен",
        "спам",
    ]

    if any(pattern in lowered for pattern in ok_patterns):
        return "ok", "ограничений нет"
    if any(pattern in lowered for pattern in limited_patterns):
        return "limited", "есть ограничения"

    preview = text.replace("\n", " ").strip()
    if len(preview) > 160:
        preview = preview[:157] + "..."
    return "unknown", f"непонятный ответ: {preview or 'пусто'}"


async def check_spambot_status(client: TelegramClient) -> tuple[str, str]:
    try:
        async with client.conversation("SpamBot", timeout=config.SPAMBOT_TIMEOUT_SECONDS) as conv:
            await conv.send_message("/start")
            response = await conv.get_response()
        raw_text = (response.raw_text or "").strip()
        return classify_spambot_text(raw_text)
    except Exception as exc:
        return "unknown", f"не удалось проверить: {short_error(exc)}"


async def is_account_in_group(
    client: TelegramClient,
    target_group: str,
    participant_id: int,
) -> bool:
    try:
        await client(GetParticipantRequest(channel=target_group, participant=participant_id))
        return True
    except UserNotParticipantError:
        return False
    except Exception as exc:
        error_text = str(exc).lower()
        if "user not participant" in error_text or "participant_id_invalid" in error_text:
            return False
        raise


async def request_access_to_group(client: TelegramClient, target_group: str) -> str:
    """
    Возвращает:
    - "joined": аккаунт уже в группе или успешно вошел
    - "requested": отправлена заявка, ждем одобрение
    """
    try:
        await client(JoinChannelRequest(target_group))
        return "joined"
    except UserAlreadyParticipantError:
        return "joined"
    except InviteRequestSentError:
        return "requested"
    except Exception as exc:
        error_text = str(exc).lower()
        if "already participant" in error_text:
            return "joined"
        if "invite request sent" in error_text or "join request sent" in error_text:
            return "requested"
        raise


def append_processed_user(username: str, processed_users: set[str]) -> None:
    key = username.lower()
    if key in processed_users:
        return

    processed_users.add(key)
    with config.PROCESSED_USERS_FILE.open("a", encoding="utf-8") as file:
        file.write(f"{username}\n")


def short_error(exc: Exception, limit: int = 200) -> str:
    text = str(exc).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


async def safe_report_user(
    report_user: Optional[UserReportCallback],
    username: str,
    status: str,
    detail: str,
) -> None:
    if not report_user:
        return
    try:
        await report_user(username, status, detail)
    except Exception as exc:
        logger.warning("Не удалось отправить user-отчет: %s", exc)


async def safe_report_text(report_text: Optional[TextReportCallback], text: str) -> None:
    if not report_text:
        return
    try:
        await report_text(text)
    except Exception as exc:
        logger.warning("Не удалось отправить текстовый отчет: %s", exc)


def detect_session_country(session_path: Path) -> str:
    try:
        rel = session_path.relative_to(config.SESSIONS_DIR)
    except ValueError:
        return "default"

    if len(rel.parts) >= 2:
        return rel.parts[0].strip().lower()

    return "default"


def _session_relative_name(session_path: Path) -> str:
    try:
        return str(session_path.relative_to(config.SESSIONS_DIR)).replace("\\", "/")
    except ValueError:
        return session_path.name


def _session_sort_key(session_path: Path) -> tuple[int, int, str]:
    rel = _session_relative_name(session_path)
    parts = Path(rel).parts
    country = parts[0].strip().lower() if len(parts) >= 2 else "default"
    is_default = 1 if country == "default" else 0
    # Предпочитаем страновые папки перед корнем, чтобы прокси-гео выбирался корректно.
    depth_penalty = 0 if len(parts) >= 2 else 1
    return (is_default, depth_penalty, rel.lower())


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


def _session_auth_fingerprint(session_path: Path) -> Optional[str]:
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
        return hashlib.sha1(auth_key).hexdigest()
    except Exception:
        return None
    finally:
        if conn:
            conn.close()


def _session_file_fingerprint(session_path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha1()
        with session_path.open("rb") as file:
            while True:
                chunk = file.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def _session_identity_key(session_path: Path) -> str:
    auth_fp = _session_auth_fingerprint(session_path)
    if auth_fp:
        return f"auth:{auth_fp}"
    file_fp = _session_file_fingerprint(session_path)
    if file_fp:
        return f"file:{file_fp}"
    return f"path:{_session_relative_name(session_path).lower()}"


def deduplicate_session_files(session_files: list[Path]) -> list[Path]:
    ordered = sorted(session_files, key=_session_sort_key)
    seen_keys: set[str] = set()
    unique: list[Path] = []
    for session_path in ordered:
        identity = _session_identity_key(session_path)
        if identity in seen_keys:
            continue
        seen_keys.add(identity)
        unique.append(session_path)
    return unique


def get_session_files(*, include_duplicates: bool = False) -> list[Path]:
    all_files = sorted(config.SESSIONS_DIR.rglob("*.session"))
    if include_duplicates:
        return all_files
    return deduplicate_session_files(all_files)


async def validate_sessions(
    *,
    report_text: Optional[TextReportCallback] = None,
) -> None:
    all_session_files = get_session_files(include_duplicates=True)
    session_files = deduplicate_session_files(all_session_files)
    if not session_files:
        msg = f"Нет session-файлов в папке {config.SESSIONS_DIR}"
        logger.error(msg)
        await safe_report_text(report_text, msg)
        return

    ok_count = 0
    noauth_count = 0
    error_count = 0
    total = len(session_files)
    duplicates_skipped = max(0, len(all_session_files) - len(session_files))
    cooldowns = config.load_account_cooldowns()

    await safe_report_text(report_text, f"Старт проверки аккаунтов: {total} шт.")
    if duplicates_skipped > 0:
        await safe_report_text(
            report_text,
            f"Найдено дублей session-файлов: {duplicates_skipped}. Проверяю только уникальные аккаунты.",
        )

    for idx, session_path in enumerate(session_files, start=1):
        session_name = str(session_path.relative_to(config.SESSIONS_DIR)).replace("\\", "/")
        country = detect_session_country(session_path)
        try:
            proxy, proxy_desc = config.resolve_proxy_for_session_with_desc(session_name, country)
        except Exception as exc:
            error_count += 1
            await safe_report_text(
                report_text,
                f"[{idx}/{total}] Ошибка прокси для {session_path.name}: {short_error(exc)}",
            )
            continue

        if config.USE_PROXY and proxy is None:
            error_count += 1
            await safe_report_text(
                report_text,
                f"[{idx}/{total}] {session_path.name}: нет прокси для страны {country}.",
            )
            continue

        if config.USE_PROXY:
            proxy_ok, proxy_error = await is_proxy_reachable(proxy, config.PROXY_CHECK_TIMEOUT)
            if not proxy_ok:
                error_count += 1
                await safe_report_text(
                    report_text,
                    f"[{idx}/{total}] {session_name}: прокси недоступен ({proxy_error})",
                )
                continue

        cooldown_note = ""
        cooldown = cooldowns.get(config.normalize_session_key(session_name))
        if cooldown:
            cooldown_left = config.get_cooldown_seconds_left(cooldown)
            if cooldown_left > 0:
                reason = str(cooldown.get("reason", "")).strip()
                reason_text = f" ({reason})" if reason else ""
                cooldown_note = f" | отлежка:{format_duration(cooldown_left)}{reason_text}"
        client: Optional[TelegramClient] = None
        try:
            client = session_meta.build_client(session_path, proxy=proxy)
            await client.connect()
            if not await client.is_user_authorized():
                noauth_count += 1
                await safe_report_text(
                    report_text,
                    f"[{idx}/{total}] {session_name}: НЕ валиден (нужно заново войти в аккаунт).",
                )
                continue

            me = await client.get_me()
            if me and me.username:
                account_label = f"@{me.username}"
            elif me and getattr(me, "phone", None):
                account_label = f"+{me.phone}"
            elif me:
                account_label = f"id:{me.id}"
            else:
                account_label = "без данных профиля"

            spambot_note = ""
            if config.SPAMBOT_CHECK_ENABLED:
                spam_status, spam_detail = await check_spambot_status(client)
                spambot_note = f" | spambot:{spam_status} ({spam_detail})"

            ok_count += 1
            await safe_report_text(
                report_text,
                f"[{idx}/{total}] {session_name}: OK ({account_label}) | страна:{country} | прокси:{proxy_desc}{cooldown_note}{spambot_note}",
            )
        except Exception as exc:
            error_count += 1
            await safe_report_text(
                report_text,
                f"[{idx}/{total}] {session_name}: ошибка проверки ({short_error(exc)}).",
            )
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        # Пауза между сессиями — asocks/residential прокси не любят серию
        # быстрых коннектов через один порт, начинают возвращать "Invalid
        # proxy response". 15 сек надёжно даёт asocks "забыть" предыдущую серию.
        if idx < total:
            await asyncio.sleep(15)

    await safe_report_text(
        report_text,
        "Проверка завершена.\n"
        f"- Всего: {total}\n"
        f"- Рабочих: {ok_count}\n"
        f"- Не авторизованы: {noauth_count}\n"
        f"- Ошибок: {error_count}",
    )


async def _handle_account_limit_hit(
    *,
    loop: asyncio.AbstractEventLoop,
    session_name: str,
    target_user: str,
    limit_incident_times: list[float],
    report_text: Optional[TextReportCallback],
    reason_label: str,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Откладывает юзера, сдвигает очередь, пауза задачи + circuit breaker.

    Пауза прерывается через stop_event — Stop button в боте срабатывает
    даже во время 1-часового circuit breaker pause.
    """
    _defer_current_user(target_user, reason=reason_label)
    limit_incident_times.append(loop.time())
    window = config.INVITE_CIRCUIT_BREAKER_WINDOW_SECONDS
    cutoff = loop.time() - window
    recent = [t for t in limit_incident_times if t >= cutoff]
    limit_incident_times[:] = recent

    pause_seconds = config.INVITE_CASCADE_PAUSE_SECONDS
    if len(recent) >= config.INVITE_CIRCUIT_BREAKER_THRESHOLD:
        pause_seconds = max(pause_seconds, config.INVITE_CIRCUIT_BREAKER_PAUSE_SECONDS)
        await safe_report_text(
            report_text,
            f"Circuit breaker: {len(recent)} лимитных сбоя за {window}с. "
            f"Пауза задачи {config.format_duration(pause_seconds)}.",
        )
    else:
        await safe_report_text(
            report_text,
            f"{session_name}: {reason_label}. Пропускаю @{target_user}, "
            f"пауза {config.format_duration(pause_seconds)} (анти-каскад).",
        )
    if pause_seconds > 0:
        # stop_event-aware sleep: Stop button в боте прерывает паузу мгновенно
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=pause_seconds)
                # Если дождались — stop_event сработал
            except asyncio.TimeoutError:
                pass  # обычная пауза истекла
        else:
            await asyncio.sleep(pause_seconds)


async def run_invite_task(
    target_group: str,
    users_list: List[str],
    *,
    stop_event: Optional[asyncio.Event] = None,
    report_user: Optional[UserReportCallback] = None,
    report_text: Optional[TextReportCallback] = None,
) -> None:
    all_session_files = get_session_files(include_duplicates=True)
    unique_session_files = deduplicate_session_files(all_session_files)
    duplicates_skipped = max(0, len(all_session_files) - len(unique_session_files))
    if not all_session_files:
        msg = f"Нет session-файлов в папке {config.SESSIONS_DIR}"
        logger.error(msg)
        await safe_report_text(report_text, msg)
        return

    session_files = warmer.get_invite_ready_session_files(unique_session_files)
    if duplicates_skipped > 0:
        await safe_report_text(
            report_text,
            f"Найдено дублей session-файлов: {duplicates_skipped}. В работу беру только уникальные аккаунты.",
        )
    if not session_files:
        await safe_report_text(
            report_text,
            "Нет аккаунтов в работе для инвайта.\n"
            f"Открой раздел «🔥 Прогрев» и запусти вручную аккаунты от {config.WARMER_MANUAL_START_PERCENT}% или дождись 100%.",
        )
        return

    processed_users = config.load_processed_usernames()
    cooldowns = config.load_account_cooldowns()
    deferred_users = config.load_deferred_usernames()

    available_at_start = 0
    for session_path in session_files:
        session_name = str(session_path.relative_to(config.SESSIONS_DIR)).replace("\\", "/")
        cd = cooldowns.get(config.normalize_session_key(session_name))
        if cd and config.get_cooldown_seconds_left(cd) > 0:
            continue
        available_at_start += 1

    if available_at_start < config.INVITE_MIN_READY_ACCOUNTS:
        await safe_report_text(
            report_text,
            f"Pre-flight: доступно аккаунтов {available_at_start}, нужно минимум "
            f"{config.INVITE_MIN_READY_ACCOUNTS}. Дождитесь окончания отлежки или добавьте аккаунты.",
        )
        return

    user_idx = 0
    total_users = len(users_list)
    added_count = 0
    skipped_count = 0
    failed_count = 0
    cooldown_added_count = 0
    cooldown_start_skipped = 0
    approval_waiting_start_count = 0
    approval_approved_count = 0
    proxy_check_failed_count = 0
    spambot_limited_skipped = 0
    spambot_unknown_count = 0
    deferred_skipped_count = 0
    cascade_pause_count = 0
    circuit_breaker_triggers = 0
    accounts_burned_on_same_user = 0
    limit_incident_times: list[float] = []
    proxy_emergency_reason = ""
    stopped_by_user = False
    loop = asyncio.get_running_loop()
    last_waiting_notice_at = 0.0
    workers: list[dict] = []
    privacy_blacklist = _load_privacy_blacklist()

    # Stagger connect: рассинхронизация первых TCP-коннектов через прокси,
    # чтобы все воркеры не стучались одновременно. Защита от:
    # - "wrong session ID" (asocks + параллель)
    # - синхронного флуда на стороне Telegram
    is_first_worker = True
    for session_path in session_files:
        if not is_first_worker:
            stagger = random.uniform(config.INVITE_STAGGER_MIN, config.INVITE_STAGGER_MAX)
            await asyncio.sleep(stagger)
        is_first_worker = False

        country = detect_session_country(session_path)
        try:
            proxy, proxy_desc = config.resolve_proxy_for_session_with_desc(session_name, country)
        except Exception as exc:
            logger.error("Ошибка конфигурации прокси для страны %s: %s", country, exc)
            await safe_report_text(
                report_text,
                f"Ошибка конфигурации прокси для страны {country}: {short_error(exc)}",
            )
            continue

        if config.USE_PROXY and proxy is None:
            await safe_report_text(
                report_text,
                f"Для страны {country} прокси не найден. Аккаунт {session_path.name} пропущен.",
            )
            continue

        session_name = str(session_path.relative_to(config.SESSIONS_DIR)).replace("\\", "/")
        if config.USE_PROXY:
            proxy_ok, proxy_error = await is_proxy_reachable(proxy, config.PROXY_CHECK_TIMEOUT)
            if not proxy_ok:
                proxy_check_failed_count += 1
                proxy_emergency_reason = (
                    f"{session_name}: прокси {proxy_desc} недоступен ({proxy_error})"
                )
                await safe_report_text(
                    report_text,
                    f"ЭКСТРЕННАЯ ОСТАНОВКА: {proxy_emergency_reason}",
                )
                break

        session_key = config.normalize_session_key(session_name)
        cooldown = cooldowns.get(session_key)
        if cooldown:
            cooldown_left = config.get_cooldown_seconds_left(cooldown)
            if cooldown_left > 0:
                cooldown_start_skipped += 1
                reason = str(cooldown.get("reason", "")).strip()
                reason_text = f" ({reason})" if reason else ""
                await safe_report_text(
                    report_text,
                    f"Аккаунт {session_name} в отлежке еще {format_duration(cooldown_left)}{reason_text}. Пропускаю.",
                )
                continue

        client: Optional[TelegramClient] = None
        try:
            client = session_meta.build_client(session_path, proxy=proxy)
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning("Аккаунт %s не авторизован, пропускаю.", session_name)
                await safe_report_text(
                    report_text,
                    f"Аккаунт {session_name} не авторизован, пропускаю.",
                )
                await client.disconnect()
                continue

            me = await client.get_me()
            if not me:
                await safe_report_text(
                    report_text,
                    f"Аккаунт {session_name}: не удалось получить данные профиля, пропускаю.",
                )
                await client.disconnect()
                continue

            spam_status = "disabled"
            spam_detail = "проверка выключена"
            if config.SPAMBOT_CHECK_ENABLED:
                spam_status, spam_detail = await check_spambot_status(client)
                if spam_status == "limited":
                    spambot_limited_skipped += 1
                    cooldown_seconds = config.build_spambot_limited_cooldown_seconds()
                    cooldown_record = config.set_account_cooldown(
                        session_name=session_name,
                        seconds=cooldown_seconds,
                        reason="spambot-limited",
                    )
                    cooldowns[config.normalize_session_key(session_name)] = cooldown_record
                    await safe_report_text(
                        report_text,
                        f"{session_name}: @SpamBot показал ограничения ({spam_detail}), аккаунт в отлежке на {format_duration(cooldown_seconds)}.",
                    )
                    await client.disconnect()
                    continue

                if spam_status == "unknown":
                    spambot_unknown_count += 1
                    await safe_report_text(
                        report_text,
                        f"{session_name}: @SpamBot ответ неясный ({spam_detail}), аккаунт оставляю в работе.",
                    )

            logger.info("Аккаунт %s подключен.", session_name)
            await safe_report_text(
                report_text,
                f"Аккаунт активен: {session_name} | страна: {country} | прокси: {proxy_desc} | spambot:{spam_status}",
            )

            try:
                is_member = await is_account_in_group(client, target_group, me.id)
            except Exception as exc:
                error_text = str(exc).lower()
                if config.USE_PROXY and is_proxy_related_error(error_text):
                    proxy_check_failed_count += 1
                    proxy_emergency_reason = (
                        f"{session_name}: потеряно соединение с прокси во время проверки группы ({short_error(exc)})"
                    )
                    await safe_report_text(
                        report_text,
                        f"ЭКСТРЕННАЯ ОСТАНОВКА: {proxy_emergency_reason}",
                    )
                    await client.disconnect()
                    break
                await safe_report_text(
                    report_text,
                    f"{session_name}: не удалось проверить участие в @{target_group}: {short_error(exc)}",
                )
                is_member = False

            awaiting_approval = False
            if not is_member:
                try:
                    request_result = await request_access_to_group(client, target_group)
                    if request_result == "joined":
                        await asyncio.sleep(random.randint(2, 5))
                except Exception as exc:
                    error_text = str(exc).lower()
                    if config.USE_PROXY and is_proxy_related_error(error_text):
                        proxy_check_failed_count += 1
                        proxy_emergency_reason = (
                            f"{session_name}: потеряно соединение с прокси во время подачи заявки ({short_error(exc)})"
                        )
                        await safe_report_text(
                            report_text,
                            f"ЭКСТРЕННАЯ ОСТАНОВКА: {proxy_emergency_reason}",
                        )
                        await client.disconnect()
                        break
                    await safe_report_text(
                        report_text,
                        f"{session_name}: не удалось отправить заявку/вступить в @{target_group}: {short_error(exc)}",
                    )
                    await client.disconnect()
                    continue

                try:
                    is_member = await is_account_in_group(client, target_group, me.id)
                except Exception as exc:
                    error_text = str(exc).lower()
                    if config.USE_PROXY and is_proxy_related_error(error_text):
                        proxy_check_failed_count += 1
                        proxy_emergency_reason = (
                            f"{session_name}: потеряно соединение с прокси во время подтверждения вступления ({short_error(exc)})"
                        )
                        await safe_report_text(
                            report_text,
                            f"ЭКСТРЕННАЯ ОСТАНОВКА: {proxy_emergency_reason}",
                        )
                        await client.disconnect()
                        break
                    await safe_report_text(
                        report_text,
                        f"{session_name}: не удалось подтвердить участие в @{target_group}: {short_error(exc)}",
                    )
                    is_member = False

                if not is_member:
                    awaiting_approval = True
                    approval_waiting_start_count += 1
                    await safe_report_text(
                        report_text,
                        f"{session_name}: заявка в @{target_group} отправлена, жду одобрения админом.",
                    )

            workers.append(
                {
                    "client": client,
                    "session_name": session_name,
                    "country": country,
                    "proxy": proxy,
                    "proxy_desc": proxy_desc,
                    "participant_id": me.id,
                    "active": is_member,
                    "awaiting_approval": awaiting_approval,
                    "next_approval_check": loop.time() + config.APPROVAL_CHECK_INTERVAL,
                    "next_proxy_check": loop.time() + config.PROXY_CHECK_INTERVAL,
                    "next_ready": loop.time() + _get_first_invite_delay(session_name) if is_member else float("inf"),
                    "invites_done": 0,
                    "invites_sent_total": 0,
                    "warmup_done": False,
                    "pre_engage_done": False,
                    "last_natural_action": loop.time(),
                    "delay_multiplier": _account_delay_multiplier(session_name),
                }
            )
        except Exception as exc:
            error_text = str(exc).lower()
            if config.USE_PROXY and is_proxy_related_error(error_text):
                proxy_check_failed_count += 1
                proxy_emergency_reason = (
                    f"{session_name}: потеряно соединение с прокси при подключении ({short_error(exc)})"
                )
                await safe_report_text(
                    report_text,
                    f"ЭКСТРЕННАЯ ОСТАНОВКА: {proxy_emergency_reason}",
                )
                try:
                    if client:
                        await client.disconnect()
                except Exception:
                    pass
                break
            logger.error("Ошибка подключения %s: %s", session_name, exc)
            await safe_report_text(report_text, f"Ошибка подключения {session_name}: {short_error(exc)}")
            try:
                if client:
                    await client.disconnect()
            except Exception:
                pass

    if not workers and not proxy_emergency_reason:
        await safe_report_text(report_text, "Нет рабочих аккаунтов для старта инвайта.")
        return

    while user_idx < total_users and not proxy_emergency_reason:
        if stop_event and stop_event.is_set():
            stopped_by_user = True
            break

        if config.USE_PROXY:
            now = loop.time()
            for worker in workers:
                if worker["invites_done"] >= config.INVITES_PER_ACC:
                    continue
                if not worker.get("active") and not worker.get("awaiting_approval"):
                    continue
                if worker["next_proxy_check"] > now:
                    continue

                proxy_ok, proxy_error = await is_proxy_reachable(
                    worker["proxy"],
                    config.PROXY_CHECK_TIMEOUT,
                )
                worker["next_proxy_check"] = loop.time() + config.PROXY_CHECK_INTERVAL
                if not proxy_ok:
                    proxy_check_failed_count += 1
                    proxy_emergency_reason = (
                        f"{worker['session_name']}: прокси {worker['proxy_desc']} недоступен ({proxy_error})"
                    )
                    await safe_report_text(
                        report_text,
                        f"ЭКСТРЕННАЯ ОСТАНОВКА: {proxy_emergency_reason}",
                    )
                    break

            if proxy_emergency_reason:
                break

        now = loop.time()
        for worker in workers:
            if not worker.get("awaiting_approval"):
                continue

            if worker["invites_done"] >= config.INVITES_PER_ACC:
                continue

            if worker["next_approval_check"] > now:
                continue

            client: TelegramClient = worker["client"]
            session_name: str = worker["session_name"]
            participant_id: int = worker["participant_id"]

            try:
                approved = await is_account_in_group(client, target_group, participant_id)
            except Exception as exc:
                error_text = str(exc).lower()
                if config.USE_PROXY and is_proxy_related_error(error_text):
                    proxy_check_failed_count += 1
                    proxy_emergency_reason = (
                        f"{session_name}: потеряно соединение с прокси во время ожидания одобрения ({short_error(exc)})"
                    )
                    await safe_report_text(
                        report_text,
                        f"ЭКСТРЕННАЯ ОСТАНОВКА: {proxy_emergency_reason}",
                    )
                    break
                worker["next_approval_check"] = loop.time() + config.APPROVAL_CHECK_INTERVAL
                logger.warning("[%s] Ошибка проверки одобрения заявки: %s", session_name, short_error(exc))
                continue

            if approved:
                worker["awaiting_approval"] = False
                worker["active"] = True
                worker["next_ready"] = loop.time()
                approval_approved_count += 1
                await safe_report_text(
                    report_text,
                    f"{session_name}: заявка одобрена админом, аккаунт подключен к инвайту.",
                )
            else:
                worker["next_approval_check"] = loop.time() + config.APPROVAL_CHECK_INTERVAL

        if proxy_emergency_reason:
            break

        active_workers = [
            worker
            for worker in workers
            if worker["active"] and worker["invites_done"] < config.INVITES_PER_ACC
        ]
        if not active_workers:
            awaiting_workers = [
                worker
                for worker in workers
                if worker.get("awaiting_approval") and worker["invites_done"] < config.INVITES_PER_ACC
            ]

            if awaiting_workers:
                now = loop.time()
                if now - last_waiting_notice_at >= 60:
                    await safe_report_text(
                        report_text,
                        f"Жду одобрения админом для {len(awaiting_workers)} акка��нтов.",
                    )
                    last_waiting_notice_at = now

                nearest = min(worker["next_approval_check"] for worker in awaiting_workers)
                sleep_seconds = max(0.5, min(nearest - now, 10.0))
                await asyncio.sleep(sleep_seconds)
                continue

            await safe_report_text(
                report_text,
                "Нет доступных аккаунтов для продолжения. Останавливаю задачу.",
            )
            break

        if config.INVITE_MAX_CONCURRENT < len(active_workers):
            overflow = len(active_workers) - config.INVITE_MAX_CONCURRENT
            if overflow > 0:
                for worker in active_workers[:overflow]:
                    worker["active"] = False
                active_workers = active_workers[overflow:]
                await safe_report_text(
                    report_text,
                    f"Лимит одновременных аккаунтов ({config.INVITE_MAX_CONCURRENT}). {overflow} аккаунтов приостановлены.",
                )

        # Тестовый инвайт убран: он сам по себе провоцирует PeerFlood на свежих аккаунтах,
        # потому что является первым действием типа invite без предшествующей истории.
        # Рамп-ап через _daily_invite_limit_for_account обеспечивает плавный вход.
        for worker in active_workers:
            if not worker.get("warmup_done"):
                worker["warmup_done"] = True

        now = loop.time()
        ready_workers = [worker for worker in active_workers if worker["next_ready"] <= now]

        if not ready_workers:
            nearest = min(worker["next_ready"] for worker in active_workers)
            wait_sec = max(0.5, min(nearest - now, 5.0))
            if now - last_waiting_notice_at >= 60:
                logger.info("⏳ Жду готовности workers: ближайший через %.0f сек", max(0, nearest - now))
                last_waiting_notice_at = now
            await asyncio.sleep(wait_sec)
            continue

        # Если у ВСЕХ ready_workers daily_cap=0 или они исчерпали лимит,
        # дальше делать нечего — выходим из задачи с понятным сообщением.
        all_capped = True
        capped_details: list[str] = []
        for w in ready_workers:
            sname = w["session_name"]
            cap = min(_daily_invite_limit_for_account(sname), config.INVITE_MAX_PER_SESSION)
            done = w["invites_done"]
            if cap >= 1 and done < cap:
                all_capped = False
                break
            capped_details.append(f"{sname}: cap={cap} done={done}")
        if all_capped:
            sample = "; ".join(capped_details[:3])
            await safe_report_text(
                report_text,
                f"Все ready-аккаунты исчерпали дневной лимит инвайтов (ramp-up или backoff). "
                f"Пример: {sample}. Останавливаю задачу — попробуй завтра или измени INVITE_MAX_PER_SESSION."
            )
            logger.info("📊 Все ready_workers с daily_cap=0 или done>=cap — задача завершается")
            break

        # За одну итерацию — один аккаунт на одного юзера (анти-каскад).
        invite_attempt_done = False
        for worker in ready_workers[:1]:
            if stop_event and stop_event.is_set():
                stopped_by_user = True
                break

            if user_idx >= total_users:
                break

            session_name: str = worker["session_name"]
            daily_cap = min(_daily_invite_limit_for_account(session_name), config.INVITE_MAX_PER_SESSION)
            if daily_cap < 1 or worker["invites_done"] >= daily_cap:
                continue

            target_user = normalize_user(users_list[user_idx])

            if not target_user:
                user_idx += 1
                skipped_count += 1
                invite_attempt_done = True
                break

            if target_user.lower() in processed_users:
                user_idx += 1
                skipped_count += 1
                await safe_report_user(report_user, target_user, "skipped", "уже обработан ранее")
                invite_attempt_done = True
                break

            is_deferred, defer_reason = config.is_username_deferred(target_user)
            if is_deferred:
                user_idx += 1
                deferred_skipped_count += 1
                await safe_report_user(report_user, target_user, "skipped", defer_reason)
                invite_attempt_done = True
                break

            if target_user.lower() in privacy_blacklist:
                user_idx += 1
                skipped_count += 1
                await safe_report_user(report_user, target_user, "skipped", "приватность (blacklist)")
                invite_attempt_done = True
                break

            client: TelegramClient = worker["client"]

            # App-like behavior: имитируем "юзер открыл Telegram" один раз за сессию
            if config.INVITE_APP_LIKE_ENABLED and not worker.get("app_opened"):
                worker["app_opened"] = True
                await _app_session_open(client, session_name)

            if (
                config.INVITE_PRE_ENGAGE_ENABLED
                and not worker.get("pre_engage_done")
                and not _should_skip_pre_engage(session_name)
            ):
                worker["pre_engage_done"] = True
                ok, detail = await pre_invite_engagement(client, target_group, session_name)
                if ok:
                    await safe_report_text(report_text, f"{session_name}: {detail} в @{target_group} перед инвайтом.")
                else:
                    logger.warning("  ⚠️ pre-engage не удался: %s", detail)
            elif config.INVITE_PRE_ENGAGE_ENABLED and not worker.get("pre_engage_done"):
                worker["pre_engage_done"] = True
                logger.info("[%s] pre-engage пропущен (ранее был FloodWait)", session_name)

            # App-like: typing-индикатор перед нажатием "Добавить"
            if config.INVITE_APP_LIKE_ENABLED:
                await _app_session_pre_invite(client, target_group, session_name)

            daily_limit = _daily_invite_limit_for_account(session_name)
            if daily_limit < 1:
                continue
            if worker["invites_done"] >= daily_limit:
                continue

            try:
                user_entity = None
                target_user_id = None
                if config.INVITE_VERIFY_ENABLED or config.INVITE_ENTITY_CACHE_ENABLED:
                    try:
                        user_entity, target_user_id = await _resolve_invite_target(client, target_user)
                    except Exception:
                        pass

                invite_target = user_entity if user_entity is not None else target_user
                result = await client(InviteToChannelRequest(target_group, [invite_target]))

                # Проверяем, что пользователь реально попал в result.users
                if config.INVITE_VERIFY_ENABLED and target_user_id is not None:
                    invited_ids = {getattr(u, "id", None) for u in getattr(result, "users", [])}
                    if target_user_id not in invited_ids:
                        # API принял запрос, но пользователь не появился в ответе —
                        # скорее всего мягкий отказ из-за настроек приватности
                        user_idx += 1
                        skipped_count += 1
                        append_processed_user(target_user, processed_users)
                        await safe_report_user(
                            report_user,
                            target_user,
                            "skipped",
                            "инвайт не подтверждён (настройки приватности)",
                        )
                        worker["next_ready"] = loop.time() + random.randint(5, 15)
                        invite_attempt_done = True
                        break

                worker["invites_done"] += 1
                worker["invites_sent_total"] += 1
                user_idx += 1
                added_count += 1
                append_processed_user(target_user, processed_users)
                await safe_report_user(report_user, target_user, "added", "успешно добавлен")

                # App-like: после инвайта "юзер продолжает сидеть в Telegram"
                if config.INVITE_APP_LIKE_ENABLED:
                    asyncio.create_task(
                        _app_session_post_invite(client, target_group, session_name)
                    )

                multiplier = worker.get("delay_multiplier", 1.0)
                wait_seconds = int(random.randint(config.MIN_DELAY, config.MAX_DELAY) * multiplier)
                worker["next_ready"] = loop.time() + wait_seconds
                worker["last_natural_action"] = loop.time()
                logger.info("[%s] Добавлен %s, пауза %s сек.", session_name, target_user, wait_seconds)
                await safe_report_text(
                    report_text,
                    f"{session_name}: пауза {wait_seconds} сек, переключаюсь на другие аккаунты.",
                )

                if config.INVITE_NATURAL_READING_ENABLED and worker["invites_done"] < config.INVITE_MAX_PER_SESSION:
                    # Запускаем чтение в фоне — не блокируем основной цикл,
                    # чтобы другие аккаунты могли работать параллельно.
                    async def _natural_reading_background(
                        bg_client: TelegramClient = client,
                        bg_session_name: str = session_name,
                        bg_target_group: str = target_group,
                    ) -> None:
                        try:
                            await asyncio.sleep(random.uniform(2, 8))
                            read_count = 0
                            async for msg in bg_client.iter_messages(
                                bg_target_group, limit=random.randint(2, 6)
                            ):
                                if msg and getattr(msg, "id", 0):
                                    read_count += 1
                                await asyncio.sleep(random.uniform(1, 4))
                            if read_count > 0:
                                logger.info(
                                    "  📖 %s: естественное чтение %s постов (фон)",
                                    bg_session_name,
                                    read_count,
                                )
                        except Exception:
                            pass

                    bg_task = asyncio.create_task(_natural_reading_background())
                    # Сохраняем ссылку чтобы task не упал в GC
                    worker.setdefault("bg_tasks", set()).add(bg_task)
                    bg_task.add_done_callback(lambda t, w=worker: w["bg_tasks"].discard(t))
                invite_attempt_done = True
                break
            except UserPrivacyRestrictedError:
                user_idx += 1
                skipped_count += 1
                append_processed_user(target_user, processed_users)
                _add_to_privacy_blacklist(target_user)
                privacy_blacklist.add(target_user.lower())
                await safe_report_user(
                    report_user,
                    target_user,
                    "skipped",
                    "у пользователя закрыты приглашения (в blacklist)",
                )
                invite_attempt_done = True
                break
            except FloodWaitError as exc:
                worker["active"] = False
                _increment_flood_incidents(session_name)
                cooldown_seconds = config.build_floodwait_cooldown_seconds(exc.seconds)
                cooldown_record = config.set_account_cooldown(
                    session_name=session_name,
                    seconds=cooldown_seconds,
                    reason=f"floodwait:{exc.seconds}",
                )
                cooldowns[config.normalize_session_key(session_name)] = cooldown_record
                cooldown_added_count += 1
                accounts_burned_on_same_user += 1
                logger.warning(
                    "[%s] FloodWait %s сек. Аккаунт в отлежке %s.",
                    session_name,
                    exc.seconds,
                    format_duration(cooldown_seconds),
                )
                await safe_report_text(
                    report_text,
                    f"{session_name}: FloodWait {exc.seconds} сек, аккаунт в отлежке на {format_duration(cooldown_seconds)}.",
                )
                user_idx += 1
                cascade_pause_count += 1
                await _handle_account_limit_hit(
                    loop=loop,
                    session_name=session_name,
                    target_user=target_user,
                    limit_incident_times=limit_incident_times,
                    report_text=report_text,
                    reason_label=f"FloodWait {exc.seconds}с",
                    stop_event=stop_event,
                )
                if len(limit_incident_times) >= config.INVITE_CIRCUIT_BREAKER_THRESHOLD:
                    circuit_breaker_triggers += 1
                invite_attempt_done = True
                break
            except PeerFloodError:
                worker["active"] = False
                _increment_flood_incidents(session_name)
                cooldown_seconds = config.build_invite_ban_cooldown_seconds()
                cooldown_record = config.set_account_cooldown(
                    session_name=session_name,
                    seconds=cooldown_seconds,
                    reason="invite-ban:peer-flood",
                )
                cooldowns[config.normalize_session_key(session_name)] = cooldown_record
                cooldown_added_count += 1
                accounts_burned_on_same_user += 1
                logger.warning(
                    "[%s] Инвайт-бан (PeerFlood). Аккаунт в отлежке %s.",
                    session_name,
                    format_duration(cooldown_seconds),
                )
                await safe_report_text(
                    report_text,
                    f"{session_name}: инвайт-бан (PeerFlood), аккаунт в отлежке на {format_duration(cooldown_seconds)}.",
                )
                user_idx += 1
                cascade_pause_count += 1
                await _handle_account_limit_hit(
                    loop=loop,
                    session_name=session_name,
                    target_user=target_user,
                    limit_incident_times=limit_incident_times,
                    report_text=report_text,
                    reason_label="PeerFlood",
                    stop_event=stop_event,
                )
                if len(limit_incident_times) >= config.INVITE_CIRCUIT_BREAKER_THRESHOLD:
                    circuit_breaker_triggers += 1
                invite_attempt_done = True
                break
            except Exception as exc:
                # Сначала проверка серверных ошибок Telegram (RPCError -500/-503/429 и т.п.)
                if isinstance(exc, RPCError):
                    code = getattr(exc, "code", None) or getattr(exc, "rpc_code", None)
                    if isinstance(code, int) and (code < 0 or code in (429, 500, 502, 503, 504)):
                        cooldown = random.uniform(60, 180)
                        logger.warning(
                            "[%s] Telegram server error %s: %s. Ждём %.0f сек и пробуем дальше.",
                            session_name, code, short_error(exc), cooldown,
                        )
                        await safe_report_text(
                            report_text,
                            f"{session_name}: серверный сбой Telegram ({code}). Пауза {int(cooldown)} сек.",
                        )
                        await asyncio.sleep(cooldown)
                        invite_attempt_done = True
                        break
                # Дальше — обычная обработка инвайт-ошибок:
                error_text = str(exc).lower()
                if config.USE_PROXY and is_proxy_related_error(error_text):
                    proxy_check_failed_count += 1
                    proxy_emergency_reason = (
                        f"{session_name}: потеряно соединение с прокси во время инвайта ({short_error(exc)})"
                    )
                    await safe_report_text(
                        report_text,
                        f"ЭКСТРЕННАЯ ОСТАНОВКА: {proxy_emergency_reason}",
                    )
                    break

                if is_permission_error(error_text):
                    worker["active"] = False
                    logger.error("[%s] Нет прав приглашать в эту группу. Отключаю аккаунт.", session_name)
                    await safe_report_text(
                        report_text,
                        f"{session_name}: нет прав приглашать в эту группу, аккаунт отключен.",
                    )
                    continue

                if is_invite_ban_error(error_text):
                    worker["active"] = False
                    _increment_flood_incidents(session_name)
                    cooldown_seconds = config.build_invite_ban_cooldown_seconds()
                    cooldown_record = config.set_account_cooldown(
                        session_name=session_name,
                        seconds=cooldown_seconds,
                        reason="invite-ban:detected-text",
                    )
                    cooldowns[config.normalize_session_key(session_name)] = cooldown_record
                    cooldown_added_count += 1
                    accounts_burned_on_same_user += 1
                    logger.warning(
                        "[%s] Похоже на инвайт-бан: %s. Отлежка %s.",
                        session_name,
                        short_error(exc),
                        format_duration(cooldown_seconds),
                    )
                    await safe_report_text(
                        report_text,
                        f"{session_name}: похоже на инвайт-бан ({short_error(exc)}), аккаунт в отлежке на {format_duration(cooldown_seconds)}.",
                    )
                    user_idx += 1
                    cascade_pause_count += 1
                    await _handle_account_limit_hit(
                        loop=loop,
                        session_name=session_name,
                        target_user=target_user,
                        limit_incident_times=limit_incident_times,
                        report_text=report_text,
                        reason_label=short_error(exc),
                        stop_event=stop_event,
                    )
                    if len(limit_incident_times) >= config.INVITE_CIRCUIT_BREAKER_THRESHOLD:
                        circuit_breaker_triggers += 1
                    invite_attempt_done = True
                    break

                user_idx += 1
                if is_terminal_user_error(error_text):
                    skipped_count += 1
                    append_processed_user(target_user, processed_users)
                    await safe_report_user(
                        report_user,
                        target_user,
                        "skipped",
                        short_error(exc),
                    )
                else:
                    failed_count += 1
                    await safe_report_user(
                        report_user,
                        target_user,
                        "failed",
                        short_error(exc),
                    )
                    worker["next_ready"] = loop.time() + 3
                invite_attempt_done = True
                break

        if proxy_emergency_reason:
            break

    # Дождёмся фоновых задач (естественное чтение и т.п.) перед disconnect,
    # чтобы не оборвать активные сетевые операции.
    pending_bg_tasks = []
    for worker in workers:
        pending_bg_tasks.extend(list(worker.get("bg_tasks", set())))
    if pending_bg_tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending_bg_tasks, return_exceptions=True),
                timeout=30,
            )
        except asyncio.TimeoutError:
            for t in pending_bg_tasks:
                if not t.done():
                    t.cancel()

    for worker in workers:
        client: TelegramClient = worker["client"]
        try:
            await client.disconnect()
        except Exception:
            pass

    if proxy_emergency_reason:
        status_text = "экстренно остановлена (проблема с прокси)"
    elif stopped_by_user:
        status_text = "остановлена по кнопке"
    else:
        status_text = "завершена"

    summary = (
        f"Задача {status_text}.\n"
        f"Обработано: {added_count + skipped_count + failed_count}/{total_users}\n"
        f"Добавлено: {added_count}\n"
        f"Пропущено: {skipped_count}\n"
        f"Ошибок: {failed_count}\n"
        f"Проблем с прокси: {proxy_check_failed_count}\n"
        f"Новых аккаунтов в отлежке: {cooldown_added_count}\n"
        f"Пропущено из-за отлежки на старте: {cooldown_start_skipped}\n"
        f"Пропущено по @SpamBot: {spambot_limited_skipped}\n"
        f"Неясный ответ @SpamBot: {spambot_unknown_count}\n"
        f"Ждали одобрение админа: {approval_waiting_start_count}\n"
        f"Одобрены во время задачи: {approval_approved_count}\n"
        f"Пропущено (отложенные юзеры): {deferred_skipped_count}\n"
        f"Анти-каскад пауз: {cascade_pause_count}\n"
        f"Circuit breaker срабатываний: {circuit_breaker_triggers}\n"
        f"Аккаунтов сожжено на одном юзере (лимит): {accounts_burned_on_same_user}"
    )
    if proxy_emergency_reason:
        summary += f"\nПричина экстренной остановки: {proxy_emergency_reason}"
    logger.info(summary.replace("\n", " | "))
    await safe_report_text(report_text, summary)

