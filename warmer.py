from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from telethon import TelegramClient, types
from telethon.errors import InviteRequestSentError, UserAlreadyParticipantError
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.functions.messages import SendReactionRequest, SendVoteRequest
from telethon.tl.functions.photos import GetUserPhotosRequest, UploadProfilePhotoRequest
from telethon.tl.types import InputPhoneContact, ReactionEmoji

import config
import session_meta
import lolz_reauth

logger = logging.getLogger("telegram_inviter_bot.warmer")


def _setup_pretty_logger() -> None:
    for handler in logger.handlers:
        if getattr(handler, "_warmer_pretty", False):
            logger.propagate = False
            logger.setLevel(logging.INFO)
            return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._warmer_pretty = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)


_setup_pretty_logger()

COUNTRY_UTC_OFFSET = {
    "af": 4.5, "al": 1, "dz": 1, "ad": 1, "ao": 1, "ar": -3, "am": 4, "au": 10,
    "at": 1, "az": 4, "bh": 3, "bd": 6, "by": 3, "be": 1, "bz": -6, "bj": 1,
    "bt": 6, "bo": -4, "ba": 1, "bw": 2, "br": -3, "bn": 8, "bg": 2, "bf": 0,
    "bi": 2, "kh": 7, "cm": 1, "ca": -5, "cv": -1, "cf": 1, "td": 1, "cl": -4,
    "cn": 8, "co": -5, "km": 3, "cg": 1, "cr": -6, "ci": 0, "hr": 1, "cu": -5,
    "cy": 2, "cz": 1, "cd": 1, "dk": 1, "dj": 3, "do": -4, "ec": -5, "eg": 2,
    "sv": -6, "gq": 1, "er": 3, "ee": 2, "et": 3, "fj": 12, "fi": 2, "fr": 1,
    "ga": 1, "gm": 0, "ge": 4, "de": 1, "gh": 0, "gr": 2, "gt": -6, "gn": 0,
    "gw": 0, "gy": -4, "ht": -5, "hn": -6, "hu": 1, "is": 0, "in": 5.5, "id": 7,
    "ir": 3.5, "iq": 3, "ie": 0, "il": 2, "it": 1, "jm": -5, "jp": 9, "jo": 2,
    "kz": 5, "ke": 3, "ki": 12, "kw": 3, "kg": 6, "la": 7, "lv": 2, "lb": 2,
    "ls": 2, "lr": 0, "ly": 2, "li": 1, "lt": 2, "lu": 1, "mg": 3, "mw": 2,
    "my": 8, "mv": 5, "ml": 0, "mt": 1, "mh": 12, "mr": 0, "mu": 4, "mx": -6,
    "fm": 10, "md": 2, "mc": 1, "mn": 8, "me": 1, "ma": 0, "mz": 2, "mm": 6.5,
    "na": 2, "nr": 12, "np": 5.75, "nl": 1, "nz": 12, "ni": -6, "ne": 1, "ng": 1,
    "kp": 8.5, "mk": 1, "no": 1, "om": 4, "pk": 5, "pw": 9, "ps": 2, "pa": -5,
    "pg": 10, "py": -4, "pe": -5, "ph": 8, "pl": 1, "pt": 0, "qa": 3, "ro": 2,
    "ru": 3, "rw": 2, "ws": 13, "sm": 1, "st": 0, "sa": 3, "sn": 0, "rs": 1,
    "sc": 4, "sl": 0, "sg": 8, "sk": 1, "si": 1, "sb": 11, "so": 3, "za": 2,
    "kr": 9, "es": 1, "lk": 5.5, "sd": 2, "sr": -3, "sz": 2, "se": 1, "ch": 1,
    "sy": 2, "tj": 5, "tz": 3, "th": 7, "tl": 9, "tg": 0, "to": 13, "tt": -4,
    "tn": 1, "tr": 3, "tm": 5, "tv": 12, "ug": 3, "ua": 2, "ae": 4, "gb": 0,
    "us": -5, "uy": -3, "uz": 5, "vu": 11, "va": 1, "ve": -4, "vn": 7,
    "ye": 3, "zm": 2, "zw": 2,
    "tw": 8, "hk": 8, "mo": 8, "pr": -4, "xk": 1,
}

_service_lock = asyncio.Lock()
_active_cycle_count = 0
_idle_event = asyncio.Event()
_idle_event.set()
# Защищает запись warmer_state.json при параллельной работе нескольких аккаунтов
_state_lock = asyncio.Lock()


def _is_account_quiet_hours(country: str) -> bool:
    offset = COUNTRY_UTC_OFFSET.get(country.lower(), 0)
    utc_now = _utc_now()
    local_hour = (utc_now.hour + offset + 24) % 24

    start = config.WARMER_QUIET_HOURS_START
    end = config.WARMER_QUIET_HOURS_END

    if start <= end:
        return start <= local_hour < end
    else:
        return local_hour >= start or local_hour < end


def _local_time_for_country(country: str) -> datetime:
    """Возвращает локальное время аккаунта по сдвигу страны."""
    offset_hours = COUNTRY_UTC_OFFSET.get(country.lower(), 0)
    utc_now = _utc_now()
    # offset_hours может быть float (например 5.5 для Индии)
    return utc_now + timedelta(hours=offset_hours)


def _generate_daily_schedule(session_name: str, country: str, local_date: str) -> list[dict]:
    """Генерирует персональное суточное расписание окон активности.

    Алгоритм:
    - 3-5 окон в течение дня, разбросанных по разным частям дня (утро/день/вечер).
    - Длина окна: 20-90 минут.
    - В quiet hours (по умолчанию 2-7 по местному) — никаких окон.
    - Окна не перекрываются.
    - Детерминированный seed для одного дня одного аккаунта — стабильное расписание.

    Возвращает список словарей {"start_min": int, "end_min": int} в локальном времени.
    """
    rng = random.Random(f"sched|{session_name.lower()}|{local_date}")

    quiet_start = config.WARMER_QUIET_HOURS_START
    quiet_end = config.WARMER_QUIET_HOURS_END

    def in_quiet(minute: int) -> bool:
        hour = minute // 60
        if quiet_start <= quiet_end:
            return quiet_start <= hour < quiet_end
        else:
            return hour >= quiet_start or hour < quiet_end

    # 3-5 окон
    n_windows = rng.randint(3, 5)

    # Разделим день на n_windows ~равных слотов и в каждом возьмём окно
    day_minutes = 24 * 60
    slot = day_minutes // n_windows

    schedule: list[dict] = []
    for i in range(n_windows):
        slot_start = i * slot
        slot_end = (i + 1) * slot

        # Длина окна 20-90 минут
        window_len = rng.randint(20, 90)
        # Не больше слота
        window_len = min(window_len, slot - 5)

        # Случайное положение начала внутри слота
        max_start = max(slot_start, slot_end - window_len)
        if max_start <= slot_start:
            start_min = slot_start
        else:
            start_min = rng.randint(slot_start, max_start)
        end_min = min(start_min + window_len, day_minutes - 1)

        # Если попало в quiet — сдвигаем
        # Простая стратегия: попробуем найти ближайший НЕ-quiet слот
        if in_quiet(start_min) or in_quiet(end_min - 1):
            # Сдвинем окно на ~1.5 часа вперёд, если попадает в quiet
            shifted_start = start_min + 90
            shifted_end = shifted_start + (end_min - start_min)
            if shifted_end < day_minutes and not in_quiet(shifted_start) and not in_quiet(shifted_end - 1):
                start_min = shifted_start
                end_min = shifted_end
            else:
                # Пропускаем это окно
                continue

        schedule.append({"start_min": start_min, "end_min": end_min})

    # Сортируем и удаляем перекрытия
    schedule.sort(key=lambda w: w["start_min"])
    cleaned: list[dict] = []
    for w in schedule:
        if cleaned and w["start_min"] < cleaned[-1]["end_min"]:
            continue
        cleaned.append(w)

    return cleaned


def _ensure_daily_schedule(entry: dict, session_name: str, country: str) -> bool:
    """Если расписание устарело (другая дата по гео) — генерирует новое.

    Возвращает True, если расписание изменилось (state нужно сохранить).
    """
    local_now = _local_time_for_country(country)
    local_date = local_now.strftime("%Y-%m-%d")

    if entry.get("schedule_for_date") == local_date and entry.get("daily_schedule"):
        return False

    schedule = _generate_daily_schedule(session_name, country, local_date)
    entry["daily_schedule"] = schedule
    entry["schedule_for_date"] = local_date
    logger.info(
        "📅 %s: новое расписание на %s (%d окон): %s",
        session_name,
        local_date,
        len(schedule),
        ", ".join(f"{w['start_min']//60:02d}:{w['start_min']%60:02d}-{w['end_min']//60:02d}:{w['end_min']%60:02d}" for w in schedule),
    )
    return True


def _is_in_schedule_window(entry: dict, country: str) -> tuple[bool, int]:
    """Проверяет, в каком статусе аккаунт согласно его расписанию.

    Возвращает (in_window, seconds_until_next_event):
    - in_window=True: сейчас активное окно, seconds_until_next_event = до конца окна
    - in_window=False: сейчас вне окна, seconds_until_next_event = до начала следующего окна (или 0 если их больше нет сегодня)
    """
    schedule = entry.get("daily_schedule", [])
    if not isinstance(schedule, list) or not schedule:
        return False, 0

    local_now = _local_time_for_country(country)
    current_min = local_now.hour * 60 + local_now.minute

    for window in schedule:
        start = int(window.get("start_min", 0))
        end = int(window.get("end_min", 0))
        if start <= current_min < end:
            # В окне; до конца:
            seconds_to_end = (end - current_min) * 60 - local_now.second
            return True, max(60, seconds_to_end)

    # Найти ближайшее будущее окно
    future = [int(w.get("start_min", 0)) for w in schedule if int(w.get("start_min", 0)) > current_min]
    if not future:
        return False, 0  # нет окон сегодня
    next_start = min(future)
    seconds_to_start = (next_start - current_min) * 60 - local_now.second
    return False, max(60, seconds_to_start)


def _should_account_rest(session_name: str) -> bool:
    if config.WARMER_REST_CHANCE <= 0:
        return False
    rng = random.Random(f"rest|{session_name.lower()}|{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H')}")
    return rng.randint(1, 100) <= config.WARMER_REST_CHANCE


def _mark_cycle_started() -> None:
    global _active_cycle_count
    _active_cycle_count += 1
    _idle_event.clear()


def _mark_cycle_finished() -> None:
    global _active_cycle_count
    _active_cycle_count = max(0, _active_cycle_count - 1)
    if _active_cycle_count == 0:
        _idle_event.set()


async def wait_until_idle(timeout_seconds: float = 90) -> bool:
    if _active_cycle_count == 0:
        return True
    try:
        await asyncio.wait_for(_idle_event.wait(), timeout=max(1.0, float(timeout_seconds)))
        return True
    except asyncio.TimeoutError:
        return False


def is_busy_now() -> bool:
    return _active_cycle_count > 0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc_iso(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_wait_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, _ = divmod(rem, 60)
    if hours > 0:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м"


def _short_error(exc: Exception, limit: int = 200) -> str:
    text = str(exc).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _humanize_error_text(raw_text: str) -> str:
    text = (raw_text or "").replace("\n", " ").strip()
    lowered = text.lower()

    if "no user has" in lowered or "nobody is using this username" in lowered:
        return "username не существует или указан неверно"
    if "username is unacceptable" in lowered:
        return "username недопустимого формата"
    if "could not find the input entity for peeruser" in lowered:
        return "не удалось найти пользователя для добавления в контакты"
    if "you can't write in this chat" in lowered or "chat write forbidden" in lowered:
        return "нет прав писать в этот чат"
    if "not a mutual contact" in lowered:
        return "пользователь не является взаимным контактом"
    if "invite request sent" in lowered or "join request sent" in lowered:
        return "заявка отправлена, жду одобрения"
    if "database is locked" in lowered:
        return "session сейчас занят другим процессом"
    if "peer flood" in lowered:
        return "инвайт-ограничение (PeerFlood)"
    if "flood wait" in lowered or "too many requests" in lowered:
        return "слишком много запросов, нужен перерыв"
    if "proxy" in lowered or "timed out" in lowered or "connection refused" in lowered:
        return "проблема с прокси или сетью"
    if "uploadcontactprofilephotorequest" in lowered and "markup" in lowered:
        return "старый формат установки эмодзи-аватара (несовместимая версия)"

    if not text:
        return "неизвестная ошибка"
    if len(text) > 160:
        return text[:157] + "..."
    return text


def _humanize_error(exc: Exception) -> str:
    return _humanize_error_text(_short_error(exc))


def _default_session_state() -> dict:
    return {
        "avatar_done": False,
        "contact_done": False,
        "contact_target": "",
        "contact_targets_done": [],
        "contact_scan_cursor": 1,
        "planned_channels": [],
        "plan_started_at": "",
        "joined_channels": [],
        "failed_channels": [],
        "manual_enabled": False,
        "cycle_completed": False,
        "last_status": "new",
        "last_error": "",
        "schedule_wait_marker": "",
        "last_run_at": "",
        "last_success_at": "",
        "session_fingerprint": "",
        # Персональное суточное расписание (см. _ensure_daily_schedule).
        # Список окон активности на сегодня в локальном времени аккаунта (по гео).
        # Каждое окно: {"start_min": int, "end_min": int}, минуты от 00:00.
        "daily_schedule": [],
        "schedule_for_date": "",  # YYYY-MM-DD по гео
        # Сверка с реальным состоянием Telegram: сколько каналов/групп реально у юзера.
        # -1 = не синкали ещё. После первого sync — реальное число.
        "real_channels_count": -1,
        # True если у аккаунта много чатов с прошлого владельца (не = готов к инвайту).
        "pre_warmed": False,
        # Действия прогрева на ТЕКУЩЕМ прокси (join, реакции, DM).
        "proxy_warmup_actions": 0,
        # Готов к инвайту только после proxy_warmup на вашем IP.
        "proxy_warmup_done": False,
        "proxy_warmup_at": "",
    }


def _normalize_channel(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""

    if value.startswith("https://"):
        value = value.removeprefix("https://")
    elif value.startswith("http://"):
        value = value.removeprefix("http://")

    if value.startswith("t.me/"):
        value = value.removeprefix("t.me/")

    value = value.lstrip("@")

    return value.strip().rstrip("/")


async def _sync_real_channel_state(
    client: TelegramClient,
    entry: dict,
    planned_channels: list[str],
    session_name: str,
) -> bool:
    """Один раз при первом запуске синкает joined_channels с РЕАЛЬНЫМ
    состоянием Telegram.

    - Запрашивает все диалоги (каналы и супергруппы) аккаунта.
    - Помечает joined_channels те, что есть в planned_channels.
    - Сохраняет real_channels_count в entry.
    - Если real_channels_count >= WARMER_PREWARMED_THRESHOLD —
      помечает аккаунт как pre_warmed (cycle_completed=True), сразу
      готов к инвайту, минуя цикл прогрева.

    Возвращает True если state изменился.
    """
    if entry.get("real_state_synced"):
        return False

    real_channels: list[str] = []
    try:
        async for dialog in client.iter_dialogs(limit=500):
            if dialog is None:
                continue
            try:
                is_channel = bool(getattr(dialog, "is_channel", False))
                is_group = bool(getattr(dialog, "is_group", False))
            except Exception:
                continue
            if not (is_channel or is_group):
                continue
            entity = getattr(dialog, "entity", None)
            if entity is None:
                continue
            username = getattr(entity, "username", None) or ""
            if username:
                real_channels.append(username.lower())
            else:
                real_channels.append(f"_private_{getattr(entity, 'id', 0)}")
    except Exception as exc:
        logger.warning(
            "  ⚠️ %s: не удалось получить список каналов: %s",
            session_name,
            _short_error(exc),
        )
        return False

    real_count = len(real_channels)
    entry["real_channels_count"] = real_count
    entry["real_state_synced"] = True

    planned_keys = {
        _normalize_channel(ch).lower()
        for ch in planned_channels
        if _normalize_channel(ch)
    }
    real_keys = set(real_channels)
    matched = planned_keys & real_keys

    if matched:
        existing_joined = [
            _normalize_channel(c).lower()
            for c in entry.get("joined_channels", [])
            if isinstance(c, str)
        ]
        joined_set = set(existing_joined)
        for ch in matched:
            if ch not in joined_set:
                existing_joined.append(ch)
                joined_set.add(ch)
        entry["joined_channels"] = existing_joined
        logger.info(
            "  🔗 %s: найдено %d каналов из planned уже подписан → засчитано",
            session_name,
            len(matched),
        )

    threshold = max(1, int(config.WARMER_PREWARMED_THRESHOLD))
    if real_count >= threshold:
        if not entry.get("pre_warmed"):
            entry["pre_warmed"] = True
            entry["last_status"] = "pre_warmed"
            entry["last_error"] = ""
            logger.info(
                "  ⚡ %s: внешняя история (%d каналов) → нужен proxy_warmup перед инвайтом",
                session_name,
                real_count,
            )
    else:
        logger.info(
            "  📊 %s: реальных каналов в Telegram: %d (порог зрелости: %d)",
            session_name,
            real_count,
            threshold,
        )

    return True


# ============================================================
# Расширенный warmer: реальные действия для роста Telegram-доверия
# ============================================================

_REACTION_EMOJI_POOL = [
    "👍", "👍", "👍", "❤️", "❤️", "🔥", "🔥", "🥰", "👏", "😁",
    "🎉", "🤔", "👌", "💯", "😢", "👎", "🤯", "🙏", "😱",
]
_DM_MESSAGE_POOL_RU = [
    "привет", "как дела?", "норм", "ок", "ага", "понял", "ясно",
    "хорошо", "спасибо", "👍", "👌", "ок, до встречи", "пиши",
]
_DM_MESSAGE_POOL_EN = [
    "hey", "what's up", "good", "ok", "thanks", "lol", "👍", "got it",
    "fine", "alright", "cool", "see ya", "yeah",
]
_DM_MESSAGE_POOL_HI = [
    "नमस्ते", "क्या हाल है?", "ठीक है", "हाँ", "धन्यवाद", "अच्छा",
    "ओके", "बाद में बात करते हैं", "अच्छा भाई",
]


def _pick_dm_message(country: str) -> str:
    if country.lower() in ("in",):
        pool = _DM_MESSAGE_POOL_HI + _DM_MESSAGE_POOL_EN
    elif country.lower() in ("ru", "by", "ua", "kz"):
        pool = _DM_MESSAGE_POOL_RU
    else:
        pool = _DM_MESSAGE_POOL_EN
    return random.choice(pool)


async def react_to_random_posts(
    client: TelegramClient,
    channel: str,
    *,
    count_range: tuple[int, int] = (1, 3),
) -> int:
    """Ставит реакции на N случайных свежих постов в канале.

    Возвращает количество поставленных реакций.
    """
    placed = 0
    try:
        target_count = random.randint(*count_range)
        scanned = 0
        async for msg in client.iter_messages(channel, limit=20):
            scanned += 1
            if placed >= target_count:
                break
            if not msg or not getattr(msg, "id", 0):
                continue
            # ~12% шанс реакции на пост — реальный юзер реагирует редко.
            # 40% (как было) — явный бот-паттерн "лайкает почти всё".
            if random.random() > 0.12:
                continue
            try:
                emoji = random.choice(_REACTION_EMOJI_POOL)
                await client(SendReactionRequest(
                    peer=channel,
                    msg_id=msg.id,
                    reaction=[ReactionEmoji(emoticon=emoji)],
                ))
                placed += 1
                # Пауза 2-6 сек между реакциями — естественный темп
                await asyncio.sleep(random.uniform(2, 6))
            except Exception:
                continue
    except Exception as exc:
        logger.debug("react_to_random_posts(%s): %s", channel, _short_error(exc))
    return placed


async def forward_post_to_saved(
    client: TelegramClient,
    channel: str,
) -> bool:
    """Пересылает один свежий пост из канала в Saved Messages."""
    try:
        messages = []
        async for msg in client.iter_messages(channel, limit=15):
            if msg and getattr(msg, "id", 0):
                messages.append(msg)
        if not messages:
            return False
        target_msg = random.choice(messages[:10])
        # 'me' = Saved Messages
        await client.forward_messages("me", target_msg)
        return True
    except Exception as exc:
        logger.debug("forward_post_to_saved(%s): %s", channel, _short_error(exc))
        return False


async def vote_in_polls(
    client: TelegramClient,
    channel: str,
) -> int:
    """Если в канале есть посты с опросами — голосует за случайный вариант."""
    voted = 0
    try:
        async for msg in client.iter_messages(channel, limit=30):
            if not msg or not getattr(msg, "poll", None):
                continue
            poll = msg.poll
            if getattr(poll.poll, "closed", False):
                continue
            options = getattr(poll.poll, "answers", [])
            if not options:
                continue
            choice = random.choice(options)
            try:
                await client(SendVoteRequest(
                    peer=channel,
                    msg_id=msg.id,
                    options=[choice.option],
                ))
                voted += 1
                if voted >= 1:  # один голос за вызов
                    break
                await asyncio.sleep(random.uniform(3, 8))
            except Exception:
                continue
    except Exception as exc:
        logger.debug("vote_in_polls(%s): %s", channel, _short_error(exc))
    return voted


async def exchange_dm_with_peer(
    client: TelegramClient,
    session_paths: list,
    current_session_name: str,
    country: str,
) -> bool:
    """Отправляет одну короткую DM другому своему аккаунту.

    Это самый сильный trust-сигнал — реальная переписка.
    Берёт случайный другой акк из списка, его phone из state/сессии, шлёт DM.
    """
    if len(session_paths) < 2:
        return False

    # Выбираем целевой акк (не себя)
    others = [
        sp for sp in session_paths
        if get_session_name(sp) != current_session_name
    ]
    if not others:
        return False
    target_path = random.choice(others)

    # Получаем phone цели — из имени файла (XXX_telethon, где XXX = phone или item_id)
    target_phone = None
    try:
        # Попытка через прямой connect к целевой сессии (быстро, без аутентификации)
        target_client = session_meta.build_client(target_path)
        await asyncio.wait_for(target_client.connect(), timeout=15)
        if await target_client.is_user_authorized():
            me = await target_client.get_me()
            if me and getattr(me, "phone", None):
                target_phone = "+" + str(me.phone).lstrip("+")
        await target_client.disconnect()
    except Exception:
        pass

    if not target_phone:
        return False

    # Импортируем контакт и шлём DM
    try:
        await client(ImportContactsRequest(contacts=[
            InputPhoneContact(
                client_id=random.randint(100_000, 999_999_999),
                phone=target_phone,
                first_name=f"Friend_{random.randint(10, 99)}",
                last_name="",
            )
        ]))
        await asyncio.sleep(random.uniform(2, 5))
        text = _pick_dm_message(country)
        await client.send_message(target_phone, text)
        return True
    except Exception as exc:
        logger.debug("exchange_dm_with_peer: %s", _short_error(exc))
        return False


async def perform_natural_actions(
    client: TelegramClient,
    entry: dict,
    session_name: str,
    session_paths: list,
    country: str,
    *,
    stop_event=None,
) -> dict:
    """Цикл "реальных" действий за один проход warmer.

    Что делает (вероятностно, не всё каждый раз):
    - Reactions: 70% chance — ставит 1-3 реакции на свежие посты в случайном канале
    - Forward: 30% chance — пересылает интересный пост в Saved
    - Polls: 20% chance — голосует если есть опросы
    - DM: 25% chance — отправляет короткое сообщение своему другому аккаунту

    Возвращает счётчики выполненных действий.
    """
    counters = {"reactions": 0, "forwards": 0, "polls": 0, "dms": 0}
    joined = [c for c in entry.get("joined_channels", []) if isinstance(c, str)]
    if not joined:
        return counters

    # Реакции (самый частый и безопасный сигнал)
    if random.random() < 0.7:
        channel = random.choice(joined)
        n = await react_to_random_posts(client, channel, count_range=(1, 3))
        counters["reactions"] = n
        if n > 0:
            logger.info("  💬 %s: %d реакций в @%s", session_name, n, channel)
        if stop_event and stop_event.is_set():
            return counters
        await asyncio.sleep(random.uniform(5, 15))

    # Forward в Saved
    if random.random() < 0.3:
        channel = random.choice(joined)
        if await forward_post_to_saved(client, channel):
            counters["forwards"] = 1
            logger.info("  📌 %s: forward из @%s в Saved", session_name, channel)
        if stop_event and stop_event.is_set():
            return counters
        await asyncio.sleep(random.uniform(3, 8))

    # Голосование в опросах
    if random.random() < 0.2:
        channel = random.choice(joined)
        n = await vote_in_polls(client, channel)
        counters["polls"] = n
        if n > 0:
            logger.info("  🗳 %s: голос в опросе @%s", session_name, channel)
        if stop_event and stop_event.is_set():
            return counters
        await asyncio.sleep(random.uniform(2, 6))

    # DM-обмен (самый сильный, но и самый рисковый — могут попасть в спам)
    if config.WARMER_DM_ENABLED and random.random() < 0.25:
        ok = await exchange_dm_with_peer(client, session_paths, session_name, country)
        if ok:
            counters["dms"] = 1
            logger.info("  💌 %s: DM отправлен своему акку", session_name)

    return counters


def detect_session_country(session_path: Path) -> str:
    try:
        rel = session_path.relative_to(config.SESSIONS_DIR)
    except ValueError:
        return "default"

    if len(rel.parts) >= 2:
        return rel.parts[0].strip().lower()

    return "default"


def get_session_name(session_path: Path) -> str:
    try:
        return str(session_path.relative_to(config.SESSIONS_DIR)).replace("\\", "/")
    except ValueError:
        return session_path.name


def _session_sort_key(session_path: Path) -> tuple[int, int, str]:
    session_name = get_session_name(session_path)
    parts = Path(session_name).parts
    country = parts[0].strip().lower() if len(parts) >= 2 else "default"
    # Предпочитаем страновые папки перед корнем.
    is_default = 1 if country == "default" else 0
    depth_penalty = 0 if len(parts) >= 2 else 1
    return (is_default, depth_penalty, session_name.lower())


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
        return "auth:" + hashlib.sha1(auth_key).hexdigest()
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
        return "file:" + digest.hexdigest()
    except Exception:
        return None


def get_session_fingerprint(session_path: Path) -> str:
    return _session_auth_fingerprint(session_path) or _session_file_fingerprint(session_path) or (
        "path:" + get_session_name(session_path).lower()
    )


def _deduplicate_session_files(session_files: list[Path]) -> list[Path]:
    ordered = sorted(session_files, key=_session_sort_key)
    unique: list[Path] = []
    seen: set[str] = set()
    for session_path in ordered:
        fp = get_session_fingerprint(session_path)
        if fp in seen:
            continue
        seen.add(fp)
        unique.append(session_path)
    return unique


def get_session_files() -> list[Path]:
    return _deduplicate_session_files(sorted(config.SESSIONS_DIR.rglob("*.session")))


CHANNEL_BUCKET_ORDER = ("global", "russian", "indian")
CHANNEL_RATIO = {"global": 25, "russian": 15, "indian": 60}


def _empty_channel_catalog() -> dict[str, list[str]]:
    return {key: [] for key in CHANNEL_BUCKET_ORDER}


def _parse_channel_line(raw_line: str) -> Optional[tuple[str, str]]:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None

    bucket = "global"
    value = line
    if ":" in line:
        prefix, tail = line.split(":", 1)
        maybe_bucket = prefix.strip().lower()
        if maybe_bucket in CHANNEL_BUCKET_ORDER:
            bucket = maybe_bucket
            value = tail.strip()

    channel = _normalize_channel(value)
    if not channel:
        return None

    return bucket, channel


def load_channel_catalog() -> dict[str, list[str]]:
    catalog = _empty_channel_catalog()
    if not config.WARMER_CHANNELS_FILE.exists():
        return catalog

    seen: dict[str, set[str]] = {key: set() for key in CHANNEL_BUCKET_ORDER}
    for line in config.WARMER_CHANNELS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        parsed = _parse_channel_line(line)
        if not parsed:
            continue
        bucket, channel = parsed
        key = channel.lower()
        if key in seen[bucket]:
            continue
        seen[bucket].add(key)
        catalog[bucket].append(channel)

    return catalog


def flatten_channel_catalog(catalog: dict[str, list[str]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for bucket in CHANNEL_BUCKET_ORDER:
        for channel in catalog.get(bucket, []):
            normalized = _normalize_channel(channel)
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(normalized)
    return result


def _target_bucket_counts(total: int) -> dict[str, int]:
    total = max(0, int(total))
    counts = {
        "global": int(total * CHANNEL_RATIO["global"] / 100),
        "russian": int(total * CHANNEL_RATIO["russian"] / 100),
        "indian": int(total * CHANNEL_RATIO["indian"] / 100),
    }
    remainder = total - sum(counts.values())
    # При остатке докидываем сначала в самый большой сегмент, чтобы не ломать пропорцию.
    for bucket in ("indian", "global", "russian"):
        if remainder <= 0:
            break
        counts[bucket] += 1
        remainder -= 1
    return counts


def build_planned_channels(
    session_name: str,
    catalog: dict[str, list[str]],
    channels_per_account: int,
) -> list[str]:
    flat = flatten_channel_catalog(catalog)
    if not flat:
        return []

    target_total = min(max(1, int(channels_per_account)), len(flat))
    target_counts = _target_bucket_counts(target_total)
    rng = random.Random(f"{session_name.lower()}|{len(flat)}")

    bucket_values: dict[str, list[str]] = {}
    for bucket in CHANNEL_BUCKET_ORDER:
        values = list(catalog.get(bucket, []))
        rng.shuffle(values)
        bucket_values[bucket] = values

    selected: list[str] = []
    used: set[str] = set()

    for bucket in CHANNEL_BUCKET_ORDER:
        need = target_counts.get(bucket, 0)
        if need <= 0:
            continue
        added = 0
        for channel in bucket_values.get(bucket, []):
            key = channel.lower()
            if key in used:
                continue
            selected.append(channel)
            used.add(key)
            added += 1
            if added >= need:
                break

    if len(selected) < target_total:
        remaining = [channel for channel in flat if channel.lower() not in used]
        rng.shuffle(remaining)
        selected.extend(remaining[: target_total - len(selected)])

    rng.shuffle(selected)
    return selected[:target_total]


def ensure_planned_channels(
    entry: dict,
    session_name: str,
    catalog: dict[str, list[str]],
) -> tuple[list[str], bool]:
    changed = False

    flat = flatten_channel_catalog(catalog)
    target_total = min(max(1, int(config.WARMER_CHANNELS_PER_ACCOUNT)), len(flat)) if flat else 0

    planned_raw = entry.get("planned_channels", [])
    planned: list[str] = []
    seen: set[str] = set()
    if isinstance(planned_raw, list):
        for channel in planned_raw:
            if not isinstance(channel, str):
                continue
            normalized = _normalize_channel(channel)
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            planned.append(normalized)

    if target_total > 0 and len(planned) < target_total:
        generated = build_planned_channels(
            session_name=session_name,
            catalog=catalog,
            channels_per_account=target_total,
        )
        for channel in generated:
            key = channel.lower()
            if key in seen:
                continue
            seen.add(key)
            planned.append(channel)
            if len(planned) >= target_total:
                break

    if target_total > 0 and len(planned) < target_total:
        rng = random.Random(f"plan-fill|{session_name.lower()}|{len(flat)}")
        rest = [channel for channel in flat if channel.lower() not in seen]
        rng.shuffle(rest)
        for channel in rest:
            key = channel.lower()
            if key in seen:
                continue
            seen.add(key)
            planned.append(channel)
            if len(planned) >= target_total:
                break

    if target_total > 0 and len(planned) > target_total:
        planned = planned[:target_total]

    if planned != planned_raw:
        entry["planned_channels"] = planned
        entry["schedule_wait_marker"] = ""
        changed = True

    plan_start = _parse_utc_iso(entry.get("plan_started_at", ""))
    if plan_start is None:
        now = _utc_now()
        done_count = _count_done_channels_in_plan(entry, planned)
        total_planned = max(1, len(planned))
        window_seconds = max(1, int(config.WARMER_CHANNEL_DISTRIBUTION_DAYS) * 86400)
        done_fraction = min(1.0, done_count / total_planned)
        plan_start = now - timedelta(seconds=window_seconds * done_fraction)
        entry["plan_started_at"] = plan_start.isoformat()
        changed = True

    return planned, changed


def _done_channel_keys(entry: dict) -> set[str]:
    joined = {
        _normalize_channel(channel).lower()
        for channel in entry.get("joined_channels", [])
        if isinstance(channel, str) and _normalize_channel(channel)
    }
    failed = {
        _normalize_channel(channel).lower()
        for channel in entry.get("failed_channels", [])
        if isinstance(channel, str) and _normalize_channel(channel)
    }
    return joined.union(failed)


def _count_done_channels_in_plan(entry: dict, channels: list[str]) -> int:
    plan_keys = {_normalize_channel(channel).lower() for channel in channels if _normalize_channel(channel)}
    if not plan_keys:
        return 0
    return len(plan_keys.intersection(_done_channel_keys(entry)))


def _get_allowed_channels_now(entry: dict, total_channels: int) -> tuple[int, Optional[datetime], bool]:
    if total_channels <= 0:
        return 0, None, False

    changed = False
    start_at = _parse_utc_iso(entry.get("plan_started_at", ""))
    now = _utc_now()

    if start_at is None:
        start_at = now
        entry["plan_started_at"] = start_at.isoformat()
        changed = True

    if start_at > now:
        start_at = now
        entry["plan_started_at"] = start_at.isoformat()
        changed = True

    window_seconds = max(1, int(config.WARMER_CHANNEL_DISTRIBUTION_DAYS) * 86400)
    elapsed_seconds = max(0.0, (now - start_at).total_seconds())

    if elapsed_seconds >= window_seconds:
        return total_channels, None, changed

    progress = elapsed_seconds / window_seconds
    allowed = max(1, int(progress * total_channels) + 1)
    allowed = min(total_channels, allowed)

    if allowed >= total_channels:
        return total_channels, None, changed

    next_ratio = min(1.0, allowed / total_channels)
    next_due = start_at + timedelta(seconds=window_seconds * next_ratio)
    return allowed, next_due, changed


def load_warmer_state() -> dict:
    if not config.WARMER_STATE_FILE.exists():
        return {"sessions": {}}

    try:
        raw = json.loads(config.WARMER_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("⚠️ Не удалось прочитать %s: %s", config.WARMER_STATE_FILE.name, exc)
        return {"sessions": {}}

    # DEBUG: чтобы поймать виновника, кто стирает pre_warmed
    try:
        pw_count = sum(
            1 for s in raw.get("sessions", {}).values()
            if isinstance(s, dict) and s.get("pre_warmed")
        )
        if pw_count > 0:
            import traceback
            stack = "".join(traceback.format_stack(limit=5))
            logger.info(
                "📂 load_warmer_state: прочитано pre_warmed=%d, вызвано из:\n%s",
                pw_count,
                stack,
            )
    except Exception:
        pass

    if not isinstance(raw, dict):
        return {"sessions": {}}

    sessions_raw = raw.get("sessions", {})
    if not isinstance(sessions_raw, dict):
        sessions_raw = {}

    sessions: dict[str, dict] = {}
    for session_name, payload in sessions_raw.items():
        if not isinstance(session_name, str):
            continue

        entry = _default_session_state()
        if isinstance(payload, dict):
            entry["avatar_done"] = bool(payload.get("avatar_done", False))
            entry["contact_done"] = bool(payload.get("contact_done", False))
            entry["contact_target"] = str(payload.get("contact_target", "")).strip()
            raw_cursor = payload.get("contact_scan_cursor", 1)
            try:
                entry["contact_scan_cursor"] = int(raw_cursor)
            except Exception:
                entry["contact_scan_cursor"] = 1
            raw_contact_targets = payload.get("contact_targets_done", [])
            normalized_targets: list[str] = []
            seen_targets: set[str] = set()
            if isinstance(raw_contact_targets, list):
                for target_name in raw_contact_targets:
                    if not isinstance(target_name, str):
                        continue
                    normalized_name = target_name.strip()
                    if not normalized_name:
                        continue
                    key = normalized_name.lower()
                    if key in seen_targets:
                        continue
                    seen_targets.add(key)
                    normalized_targets.append(normalized_name)
            legacy_target = entry["contact_target"]
            if legacy_target:
                key = legacy_target.lower()
                if key not in seen_targets:
                    normalized_targets.append(legacy_target)
            entry["contact_targets_done"] = normalized_targets
            entry["manual_enabled"] = bool(payload.get("manual_enabled", False))
            entry["plan_started_at"] = str(payload.get("plan_started_at", "")).strip()
            entry["schedule_wait_marker"] = str(payload.get("schedule_wait_marker", "")).strip()
            planned_channels = payload.get("planned_channels", [])
            if isinstance(planned_channels, list):
                normalized_plan = []
                seen_plan: set[str] = set()
                for channel in planned_channels:
                    if not isinstance(channel, str):
                        continue
                    normalized = _normalize_channel(channel)
                    if not normalized:
                        continue
                    key = normalized.lower()
                    if key in seen_plan:
                        continue
                    seen_plan.add(key)
                    normalized_plan.append(normalized)
                entry["planned_channels"] = normalized_plan
            joined_channels = payload.get("joined_channels", [])
            if isinstance(joined_channels, list):
                normalized_channels = []
                seen: set[str] = set()
                for channel in joined_channels:
                    if not isinstance(channel, str):
                        continue
                    normalized = _normalize_channel(channel)
                    if not normalized:
                        continue
                    key = normalized.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    normalized_channels.append(normalized)
                entry["joined_channels"] = normalized_channels
            failed_channels = payload.get("failed_channels", [])
            if isinstance(failed_channels, list):
                normalized_failed = []
                seen_failed: set[str] = set()
                for channel in failed_channels:
                    if not isinstance(channel, str):
                        continue
                    normalized = _normalize_channel(channel)
                    if not normalized:
                        continue
                    key = normalized.lower()
                    if key in seen_failed:
                        continue
                    seen_failed.add(key)
                    normalized_failed.append(normalized)
                entry["failed_channels"] = normalized_failed
            entry["cycle_completed"] = bool(payload.get("cycle_completed", False))
            entry["last_status"] = str(payload.get("last_status", "new")).strip() or "new"
            entry["last_error"] = str(payload.get("last_error", "")).strip()
            entry["last_run_at"] = str(payload.get("last_run_at", "")).strip()
            entry["last_success_at"] = str(payload.get("last_success_at", "")).strip()
            entry["session_fingerprint"] = str(payload.get("session_fingerprint", "")).strip()
            # Сверка с реальным состоянием Telegram (см. _sync_real_channel_state)
            try:
                entry["real_channels_count"] = int(payload.get("real_channels_count", -1))
            except (ValueError, TypeError):
                entry["real_channels_count"] = -1
            entry["real_state_synced"] = bool(payload.get("real_state_synced", False))
            entry["pre_warmed"] = bool(payload.get("pre_warmed", False))
            try:
                entry["proxy_warmup_actions"] = int(payload.get("proxy_warmup_actions", 0))
            except (TypeError, ValueError):
                entry["proxy_warmup_actions"] = 0
            entry["proxy_warmup_done"] = bool(payload.get("proxy_warmup_done", False))
            entry["proxy_warmup_at"] = str(payload.get("proxy_warmup_at", "")).strip()

        sessions[session_name] = entry

    return {"sessions": sessions}


def save_warmer_state(state: dict) -> None:
    # DEBUG: stack-trace, если pre_warmed становится меньше чем было
    try:
        new_pw = sum(
            1 for s in state.get("sessions", {}).values()
            if isinstance(s, dict) and s.get("pre_warmed")
        )
        if config.WARMER_STATE_FILE.exists():
            try:
                old_raw = json.loads(config.WARMER_STATE_FILE.read_text(encoding="utf-8"))
                old_pw = sum(
                    1 for s in old_raw.get("sessions", {}).values()
                    if isinstance(s, dict) and s.get("pre_warmed")
                )
                if new_pw < old_pw:
                    import traceback
                    stack = "".join(traceback.format_stack(limit=8))
                    logger.warning(
                        "🚨 save_warmer_state СБРАСЫВАЕТ pre_warmed: было %d, станет %d. Stack:\n%s",
                        old_pw,
                        new_pw,
                        stack,
                    )
            except Exception:
                pass
    except Exception:
        pass

    payload = {"sessions": state.get("sessions", {})}
    temp_path = config.WARMER_STATE_FILE.with_suffix(config.WARMER_STATE_FILE.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(config.WARMER_STATE_FILE)


def ensure_session_state(state: dict, session_name: str) -> dict:
    sessions = state.setdefault("sessions", {})
    if session_name not in sessions or not isinstance(sessions[session_name], dict):
        sessions[session_name] = _default_session_state()
    return sessions[session_name]


def _channels_set(channels: list[str]) -> set[str]:
    return {_normalize_channel(channel).lower() for channel in channels if _normalize_channel(channel)}


def _read_contact_targets_done(entry: dict, *, self_name: str = "", known_names: Optional[set[str]] = None) -> list[str]:
    raw_targets = entry.get("contact_targets_done", [])
    normalized_targets: list[str] = []
    seen: set[str] = set()
    self_key = self_name.strip().lower() if self_name else ""
    known_keys = {name.lower() for name in known_names} if known_names else None

    if isinstance(raw_targets, list):
        for value in raw_targets:
            if not isinstance(value, str):
                continue
            normalized = value.strip()
            if not normalized:
                continue
            key = normalized.lower()
            if self_key and key == self_key:
                continue
            if known_keys is not None and key not in known_keys:
                continue
            if key in seen:
                continue
            seen.add(key)
            normalized_targets.append(normalized)

    legacy_target = str(entry.get("contact_target", "")).strip()
    if legacy_target:
        key = legacy_target.lower()
        if (not self_key or key != self_key) and (known_keys is None or key in known_keys) and key not in seen:
            normalized_targets.append(legacy_target)

    return normalized_targets


def calculate_warming_progress_percent(entry: dict, channels: list[str]) -> int:
    # Pre-warmed (определён через _sync_real_channel_state) — сразу 100%
    if entry.get("pre_warmed"):
        return 100
    channels_set = _channels_set(channels)
    total_channels = len(channels_set)
    if total_channels <= 0:
        return 0

    joined = {
        _normalize_channel(channel).lower()
        for channel in entry.get("joined_channels", [])
        if isinstance(channel, str) and _normalize_channel(channel)
    }
    failed = {
        _normalize_channel(channel).lower()
        for channel in entry.get("failed_channels", [])
        if isinstance(channel, str) and _normalize_channel(channel)
    }
    done_channels = len(channels_set.intersection(joined.union(failed)))

    raw_percent = int(round((done_channels / total_channels) * 100))

    plan_started_at = _parse_utc_iso(entry.get("plan_started_at", ""))
    now = _utc_now()
    window_days = max(1, int(config.WARMER_CHANNEL_DISTRIBUTION_DAYS))

    if plan_started_at and done_channels > 0:
        done_fraction = done_channels / total_channels
        estimated_end = plan_started_at + timedelta(seconds=window_days * 86400 * done_fraction)
        days_elapsed = max(0.0, (now - plan_started_at).total_seconds() / 86400)
        days_since_last_join = max(0.0, (now - estimated_end).total_seconds() / 86400)

        if days_elapsed <= 1.5:
            time_factor = 0.7
        elif days_elapsed <= 2.5:
            time_factor = 0.85
        elif days_since_last_join <= 0.5:
            time_factor = 0.9
        else:
            time_factor = 1.0
    else:
        time_factor = 1.0

    effective = int(round(raw_percent * time_factor))
    return max(0, min(100, effective))


def _is_cycle_completed(entry: dict, channels: list[str]) -> bool:
    channels_set = {_normalize_channel(channel).lower() for channel in channels if _normalize_channel(channel)}
    joined = {
        _normalize_channel(channel).lower()
        for channel in entry.get("joined_channels", [])
        if isinstance(channel, str)
    }
    failed = {
        _normalize_channel(channel).lower()
        for channel in entry.get("failed_channels", [])
        if isinstance(channel, str)
    }
    return channels_set.issubset(joined.union(failed))


def _migrate_proxy_warmup_legacy(entry: dict, channels: list[str]) -> None:
    """Аккаунты, полностью прогретые у нас до введения proxy_warmup_done."""
    if entry.get("proxy_warmup_done"):
        return
    if _is_cycle_completed(entry, channels) and str(entry.get("last_success_at", "")).strip():
        entry["proxy_warmup_done"] = True
        entry["proxy_warmup_at"] = str(entry.get("last_success_at", "")).strip()


def _record_proxy_warmup_actions(entry: dict, count: int) -> None:
    if count <= 0:
        return
    current = int(entry.get("proxy_warmup_actions", 0))
    entry["proxy_warmup_actions"] = current + count
    if entry["proxy_warmup_actions"] >= config.WARMER_PROXY_WARMUP_MIN_ACTIONS:
        entry["proxy_warmup_done"] = True
        entry["proxy_warmup_at"] = _utc_now().isoformat()


def _is_entry_in_work(entry: dict, channels: list[str]) -> bool:
    _migrate_proxy_warmup_legacy(entry, channels)
    if not entry.get("proxy_warmup_done", False):
        return False
    if _is_cycle_completed(entry, channels):
        return True
    return bool(entry.get("manual_enabled", False))


def _load_state_with_sessions(session_paths: list[Path]) -> tuple[dict, bool]:
    state = load_warmer_state()
    changed = _sync_state_with_sessions(state, session_paths)
    return state, changed


def get_warming_status_rows() -> list[dict]:
    session_paths = get_session_files()
    if not session_paths:
        return []

    catalog = load_channel_catalog()
    state, changed = _load_state_with_sessions(session_paths)

    rows: list[dict] = []
    for session_path in session_paths:
        session_name = get_session_name(session_path)
        entry = ensure_session_state(state, session_name)
        planned_channels, plan_changed = ensure_planned_channels(entry, session_name, catalog)
        if plan_changed:
            changed = True

        progress = calculate_warming_progress_percent(entry, planned_channels)
        auto_in_work = _is_cycle_completed(entry, planned_channels)
        manual_enabled = bool(entry.get("manual_enabled", False))
        in_work = _is_entry_in_work(entry, planned_channels)
        can_manual_start = (
            (not in_work)
            and progress >= config.WARMER_MANUAL_START_PERCENT
        )

        rows.append(
            {
                "session_name": session_name,
                "progress_percent": progress,
                "auto_in_work": auto_in_work,
                "manual_enabled": manual_enabled,
                "in_work": in_work,
                "can_manual_start": can_manual_start,
                "pre_warmed": bool(entry.get("pre_warmed", False)),
                "real_channels_count": int(entry.get("real_channels_count", -1)),
                "last_status": str(entry.get("last_status", "")).strip(),
                "last_error": str(entry.get("last_error", "")).strip(),
            }
        )

    if changed:
        save_warmer_state(state)

    return rows


def get_invite_ready_session_files(session_paths: Optional[list[Path]] = None) -> list[Path]:
    source_paths = session_paths if session_paths is not None else get_session_files()
    if not source_paths:
        return []

    catalog = load_channel_catalog()
    state, changed = _load_state_with_sessions(source_paths)

    result: list[Path] = []
    for session_path in source_paths:
        session_name = get_session_name(session_path)
        entry = ensure_session_state(state, session_name)
        planned_channels, plan_changed = ensure_planned_channels(entry, session_name, catalog)
        if plan_changed:
            changed = True
        if _is_entry_in_work(entry, planned_channels):
            result.append(session_path)

    if changed:
        save_warmer_state(state)

    return result


def enable_session_manual_work(session_name: str) -> tuple[bool, str]:
    normalized_name = session_name.replace("\\", "/").strip()
    if not normalized_name:
        return False, "Пустое имя аккаунта."

    session_paths = get_session_files()
    if not session_paths:
        return False, "Session-файлы не найдены."

    state, changed = _load_state_with_sessions(session_paths)
    catalog = load_channel_catalog()
    sessions_map: dict[str, dict] = state.setdefault("sessions", {})

    if normalized_name not in sessions_map:
        return False, f"Аккаунт {normalized_name} не найден."

    entry = ensure_session_state(state, normalized_name)
    planned_channels, plan_changed = ensure_planned_channels(entry, normalized_name, catalog)
    if plan_changed:
        changed = True

    progress = calculate_warming_progress_percent(entry, planned_channels)

    if _is_cycle_completed(entry, planned_channels):
        if changed:
            save_warmer_state(state)
        return False, f"{normalized_name}: уже в работе автоматически (100%)."

    if bool(entry.get("manual_enabled", False)):
        if changed:
            save_warmer_state(state)
        return False, f"{normalized_name}: уже запущен в работу вручную."

    if progress < config.WARMER_MANUAL_START_PERCENT:
        if changed:
            save_warmer_state(state)
        return (
            False,
            f"{normalized_name}: прогрев {progress}% (нужно минимум {config.WARMER_MANUAL_START_PERCENT}%).",
        )

    entry["manual_enabled"] = True
    entry["last_status"] = "manual_in_work"
    changed = True

    if changed:
        save_warmer_state(state)

    return True, f"{normalized_name}: добавлен в работу вручную ({progress}%)."


async def _sleep_with_stop(
    seconds: float,
    *,
    stop_event: Optional[asyncio.Event] = None,
) -> bool:
    if seconds <= 0:
        return bool(stop_event and stop_event.is_set())

    if stop_event is None:
        await asyncio.sleep(seconds)
        return False

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def _random_delay(
    min_seconds: int,
    max_seconds: int,
    *,
    stop_event: Optional[asyncio.Event] = None,
    log_message: Optional[str] = None,
) -> bool:
    if max_seconds <= 0:
        return bool(stop_event and stop_event.is_set())

    if min_seconds > max_seconds:
        min_seconds = max_seconds

    delay = random.uniform(min_seconds, max_seconds)
    if log_message:
        logger.info(log_message, max(1, int(round(delay))))
    return await _sleep_with_stop(delay, stop_event=stop_event)


async def simulate_reading(client: TelegramClient, channel: str) -> tuple[bool, int, str]:
    try:
        # Человек читает разное кол-во постов: иногда 3, иногда 15
        weights = [1, 2, 3, 3, 2, 1, 1, 1, 1, 1, 1, 1, 1]
        requested_count = random.choices(range(3, 16), weights=weights, k=1)[0]
        seen_count = 0
        max_seen_id = 0
        async for message in client.iter_messages(channel, limit=requested_count):
            if message and getattr(message, "id", 0):
                max_seen_id = max(max_seen_id, int(message.id))
                seen_count += 1
            # Варьируем паузу: короткий скролл или долгое чтение
            if random.random() < 0.2:
                await _random_delay(8, 20)  # иногда долго читает один пост
            else:
                await _random_delay(1, 6)

        if max_seen_id > 0:
            await client.send_read_acknowledge(channel, max_id=max_seen_id)

        # Иногда делает паузу после прочтения (задумался)
        if random.random() < 0.3:
            await _random_delay(3, 10)

        return True, seen_count, ""
    except Exception as exc:
        return False, 0, _short_error(exc)


REACTION_EMOJIS = [
    "👍", "👍", "❤️", "❤️", "🔥", "👏", "😄", "🎉", "🤩", "👌",
    "💯", "✨", "😢", "👎", "🤔", "🙏",
]

_PROFILE_BIOS = [
    "Just living my life 😊",
    "Work hard, stay humble",
    "Day by day",
    "Explorer at heart",
    "Coffee lover ☕",
    "Simple life, happy life",
    "Making the most of every day",
    "Positive vibes only",
    "Life is what you make it",
    "Just here enjoying life",
    "Always learning, always growing",
    "Good vibes every day",
    "Living and loving it",
    "Every day is a new start",
    "Chasing dreams, one step at a time",
]

_PROFILE_BIOS_HINDI = [
    "जिंदगी जी रहे हैं 😊",
    "हर दिन नई शुरुआत",
    "सपने देखो, मेहनत करो",
    "खुश रहो, खुशियाँ बाँटो",
    "अच्छा सोचो, अच्छा होगा",
]

_PROFILE_BIOS_SPANISH = [
    "Viviendo la vida 😊",
    "Día a día",
    "Soñando en grande",
    "Positive siempre",
    "La vida es bella",
]


def _pick_bio_for_country(country: str) -> str:
    country_key = (country or "").strip().lower()
    if country_key in ("in", "bd", "pk", "np"):
        pool = _PROFILE_BIOS_HINDI
    elif country_key in ("co", "mx", "ar", "es", "pe", "ve", "cl"):
        pool = _PROFILE_BIOS_SPANISH
    else:
        pool = _PROFILE_BIOS
    return random.choice(pool)


async def setup_account_profile(
    client: TelegramClient,
    session_name: str,
    country: str,
    *,
    stop_event: Optional[asyncio.Event] = None,
) -> tuple[bool, str]:
    """Устанавливает аватар и bio если они ещё не заданы. Возвращает (changed, detail)."""
    if not config.WARMER_PROFILE_SETUP_ENABLED:
        return False, "profile setup disabled"

    changed = False
    details: list[str] = []

    try:
        me = await client.get_me()
        if not me:
            return False, "не удалось получить профиль"

        # --- Аватар ---
        if not getattr(me, "photo", None):
            photo_dir = config.WARMER_PROFILE_PHOTOS_DIR
            if photo_dir.is_dir():
                photos = [
                    f for f in photo_dir.iterdir()
                    if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and f.is_file()
                ]
                if photos:
                    chosen = random.choice(photos)
                    try:
                        uploaded = await client.upload_file(str(chosen))
                        await client(UploadProfilePhotoRequest(file=uploaded))
                        details.append(f"аватар установлен ({chosen.name})")
                        changed = True
                        await _random_delay(3, 7, stop_event=stop_event)
                    except Exception as exc:
                        details.append(f"аватар: ошибка ({_short_error(exc)})")
                else:
                    details.append("аватар: нет фото в profile_photos/")
            else:
                details.append("аватар: папка profile_photos/ не найдена")
        else:
            details.append("аватар уже есть")

        # --- Bio ---
        if config.WARMER_PROFILE_BIO_ENABLED:
            current_bio = (getattr(me, "about", None) or "").strip()
            if not current_bio:
                bio_text = _pick_bio_for_country(country)
                try:
                    await client(UpdateProfileRequest(about=bio_text))
                    details.append(f"bio установлен: «{bio_text}»")
                    changed = True
                    await _random_delay(2, 5, stop_event=stop_event)
                except Exception as exc:
                    details.append(f"bio: ошибка ({_short_error(exc)})")
            else:
                details.append("bio уже есть")

    except Exception as exc:
        return False, f"ошибка настройки профиля: {_short_error(exc)}"

    return changed, " | ".join(details)


async def add_reaction_to_message(client: TelegramClient, channel: str, message_id: int) -> bool:
    try:
        from telethon.tl import functions, types
        emoji = random.choice(REACTION_EMOJIS)
        await client(
            functions.messages.SetReactionRequest(
                peer=channel,
                msg_id=message_id,
                reaction=[types.ReactionEmoji(emoji=emoji)],
            )
        )
        return True
    except Exception:
        return False


async def simulate_reading_with_reactions(client: TelegramClient, channel: str) -> tuple[bool, int, int, str]:
    try:
        requested_count = random.randint(5, 10)
        seen_count = 0
        reaction_count = 0
        max_seen_id = 0
        messages_to_react: list[int] = []

        async for message in client.iter_messages(channel, limit=requested_count):
            if message and getattr(message, "id", 0):
                max_seen_id = max(max_seen_id, int(message.id))
                seen_count += 1
                if random.random() < 0.12:
                    messages_to_react.append(message.id)
            await _random_delay(2, 5)

        # Не всегда дочитываем до самого конца и не всегда ack — как живой юзер
        if max_seen_id > 0 and random.random() < 0.75:
            await client.send_read_acknowledge(channel, max_id=max_seen_id)

        if messages_to_react:
            for msg_id in messages_to_react[:2]:
                if await add_reaction_to_message(client, channel, msg_id):
                    reaction_count += 1
                await _random_delay(1, 3)

        return True, seen_count, reaction_count, ""
    except Exception as exc:
        return False, 0, 0, _short_error(exc)


async def join_channel(client: TelegramClient, channel: str) -> tuple[bool, bool, str]:
    try:
        await client(JoinChannelRequest(channel))
        return True, True, ""
    except UserAlreadyParticipantError:
        return True, False, ""
    except InviteRequestSentError:
        return False, False, "заявка отправлена, жду одобрение"
    except Exception as exc:
        error_text = str(exc).lower()
        if "already participant" in error_text:
            return True, False, ""
        return False, False, _short_error(exc)


async def fetch_identity_from_session(session_path: Path) -> tuple[Optional[tuple[int, str]], str]:
    session_name = get_session_name(session_path)
    country = detect_session_country(session_path)
    try:
        proxy, _ = config.resolve_proxy_for_session_with_desc(session_name, country)
    except Exception as exc:
        return None, f"ошибка прокси: {_short_error(exc)}"

    client: Optional[TelegramClient] = None
    try:
        client = session_meta.build_client(session_path, proxy=proxy)
        await client.connect()
        if not await client.is_user_authorized():
            return None, "соседний аккаунт не авторизован"

        me = await client.get_me()
        if not me:
            return None, "не удалось получить профиль соседнего аккаунта"

        phone = str(getattr(me, "phone", "") or "").strip()
        if not phone:
            return None, "у соседнего аккаунта нет телефона"

        return (int(me.id), phone), ""
    except Exception as exc:
        return None, _short_error(exc)
    finally:
        try:
            if client:
                await client.disconnect()
        except Exception:
            pass


async def add_next_account_as_contact(
    client: TelegramClient,
    session_paths: list[Path],
    current_index: int,
    *,
    current_session_name: str,
    already_added_targets: set[str],
    start_cursor: int,
) -> tuple[bool, str, str, int]:
    if len(session_paths) <= 1:
        return True, "в списке один аккаунт", "", 1

    total = len(session_paths)
    cursor = max(1, min(total - 1, int(start_cursor or 1)))
    shifts = list(range(cursor, total)) + list(range(1, cursor))
    session_name_key = current_session_name.strip().lower()

    attempted = 0
    skipped_already_added = 0
    unauthorized_count = 0
    last_details: list[str] = []

    for shift in shifts:
        next_cursor = shift + 1 if shift + 1 < total else 1
        next_index = (current_index + shift) % total
        if next_index == current_index:
            continue

        next_session = session_paths[next_index]
        target_name = get_session_name(next_session)
        target_key = target_name.lower()

        if target_key == session_name_key:
            continue

        if target_key in already_added_targets:
            skipped_already_added += 1
            continue

        attempted += 1
        identity, detail = await fetch_identity_from_session(next_session)
        if not identity:
            if "не авторизован" in detail.lower():
                unauthorized_count += 1
            last_details.append(f"{target_name}: {detail}")
            continue

        _, friend_phone = identity
        try:
            await client(
                ImportContactsRequest(
                    contacts=[
                        InputPhoneContact(
                            client_id=random.randint(100_000, 999_999_999),
                            phone=friend_phone,
                            first_name=f"Partner_{random.randint(10, 99)}",
                            last_name="",
                        )
                    ]
                )
            )
            return True, f"добавлен контакт {target_name}", target_name, next_cursor
        except Exception as exc:
            last_details.append(f"{target_name}: {_short_error(exc)}")
            continue

    if attempted == 0:
        return True, "все доступные аккаунты уже добавлены в контакты", "", cursor

    if unauthorized_count >= attempted:
        return False, "контакт не добавлен (другие аккаунты не авторизованы)", "", cursor

    if last_details:
        return False, f"контакт не добавлен ({last_details[-1]})", "", cursor

    return False, "контакт не добавлен (подходящий аккаунт не найден)", "", cursor


def _get_item_id_from_session_name(session_name: str) -> Optional[int]:
    session_dir = config.SESSIONS_DIR / session_name.replace(".session", "")
    if session_dir.is_dir():
        item_id_file = session_dir / "item_id.txt"
        if item_id_file.exists():
            try:
                return int(item_id_file.read_text(encoding="utf-8").strip())
            except Exception:
                pass
    return None


async def _try_reauthorize_via_lolz(
    client: TelegramClient,
    session_name: str,
    item_id: int,
) -> tuple[bool, str]:
    ok, code = await lolz_reauth.get_lolz_login_code(item_id, config.LOLZ_API_TOKEN)
    if not ok:
        return False, code

    logger.info("  📱 Получен код: %s", code)

    try:
        await client.sign_in(phone=None, code=code)
        return True, "авторизация успешна"
    except Exception as exc:
        error_text = str(exc).lower()
        if "code is invalid" in error_text or "invalid code" in error_text:
            return False, "неверный код"
        if "session password needed" in error_text:
            return False, "требуется 2FA пароль (не поддерживается)"
        return False, f"ошибка авторизации: {_short_error(exc)}"


async def process_account_cycle(
    session_path: Path,
    session_paths: list[Path],
    index_map: dict[str, int],
    channels: list[str],
    state: dict,
    *,
    stop_event: Optional[asyncio.Event] = None,
) -> bool:
    session_name = get_session_name(session_path)
    entry = ensure_session_state(state, session_name)
    entry["last_run_at"] = _utc_now_iso()
    entry["last_status"] = "in_progress"
    entry["last_error"] = ""

    country = detect_session_country(session_path)
    try:
        proxy, proxy_desc = config.resolve_proxy_for_session_with_desc(session_name, country)
    except Exception as exc:
        entry["cycle_completed"] = False
        entry["last_status"] = "error"
        entry["last_error"] = f"ошибка прокси: {_short_error(exc)}"
        logger.warning("🔥 Прогрев: %s", session_name)
        logger.warning("  ❌ Не удалось подобрать прокси: %s", _humanize_error(exc))
        return True

    try:
        client = session_meta.build_client(session_path, proxy=proxy)
    except Exception as exc:
        entry["cycle_completed"] = False
        entry["last_status"] = "error"
        entry["last_error"] = f"ошибка открытия session: {_short_error(exc)}"
        logger.warning("🔥 Прогрев: %s", session_name)
        logger.warning("  ❌ Не удалось открыть session: %s", _humanize_error(exc))
        return True

    _mark_cycle_started()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            entry["cycle_completed"] = False
            entry["last_status"] = "unauthorized"
            entry["last_error"] = "аккаунт не авторизован"
            logger.warning("🔥 Прогрев: %s", session_name)
            logger.warning("  ⚠️ Аккаунт не авторизован")

            if config.LOLZ_API_TOKEN:
                logger.info("  🔄 Пытаюсь восстановить через lolzmarket API...")
                item_id = _get_item_id_from_session_name(session_name)
                if item_id:
                    ok, detail = await _try_reauthorize_via_lolz(client, session_name, item_id)
                    if ok:
                        logger.info("  ✅ Авторизация восстановлена!")
                        entry["last_status"] = "reauthorized"
                        entry["last_error"] = ""
                        if await client.is_user_authorized():
                            entry["cycle_completed"] = False
                            entry["last_status"] = "in_progress"
                            entry["last_error"] = ""
                            logger.info("  🌟 Авторизация подтверждена, продолжаю прогрев...")
                        else:
                            logger.warning("  ⚠️ Восстановление не удалось подтвердить")
                    else:
                        logger.warning("  ❌ Восстановление не удалось: %s", detail)
                else:
                    logger.warning("  ⚠️ item_id не найден в метаданных аккаунта")
            else:
                logger.info("  💡 Для автовосстановления добавь LOLZ_API_TOKEN в .env")

            return True

        logger.info("🔥 Прогрев: %s", session_name)
        logger.info("  🌍 Страна: %s | Прокси: %s", country, proxy_desc)

        # Один раз: сверка с реальным состоянием Telegram.
        # Если аккаунт уже сидит в куче каналов (например, продавец прогрел) —
        # помечаем как pre_warmed и пропускаем warmer-действия.
        sync_changed = await _sync_real_channel_state(
            client, entry, channels, session_name
        )
        if sync_changed:
            # Если пометили pre_warmed — НЕ заканчиваем цикл, а делаем
            # "реальные действия" (реакции, DM, forward) для роста доверия.
            # Они нужны pre_warmed аккам — подписки на каналы Telegram не считает
            # сильным сигналом, нужно активное поведение.
            if entry.get("pre_warmed"):
                logger.info("  🎯 %s: pre_warmed → запуск natural-actions цикла", session_name)
                counters = await perform_natural_actions(
                    client, entry, session_name, session_paths, country,
                    stop_event=stop_event,
                )
                total = sum(counters.values())
                _record_proxy_warmup_actions(entry, total)
                if total > 0:
                    logger.info(
                        "  ✨ %s: реальных действий: реакции=%d forward=%d опросы=%d DM=%d",
                        session_name, counters["reactions"], counters["forwards"],
                        counters["polls"], counters["dms"],
                    )
                entry["last_run_at"] = _utc_now_iso()
                return True

        # Настройка профиля (аватар + bio) — только если ещё не сделана
        if not entry.get("avatar_done", False):
            profile_changed, profile_detail = await setup_account_profile(
                client, session_name, country, stop_event=stop_event
            )
            logger.info("  👤 Профиль: %s", profile_detail)
            entry["avatar_done"] = True
            if profile_changed:
                await _random_delay(
                    config.WARMER_STEP_DELAY_MIN,
                    config.WARMER_STEP_DELAY_MAX,
                    stop_event=stop_event,
                )

        known_session_names = {get_session_name(path) for path in session_paths}
        stored_targets = _read_contact_targets_done(entry, self_name=session_name, known_names=known_session_names)
        if stored_targets != entry.get("contact_targets_done", []):
            entry["contact_targets_done"] = stored_targets

        target_keys_done = {name.lower() for name in stored_targets}
        contacts_required = max(0, len(session_paths) - 1)
        if contacts_required == 0 and not bool(entry.get("contact_done", False)):
            entry["contact_done"] = True

        need_more_contacts = len(target_keys_done) < contacts_required
        if not need_more_contacts and not bool(entry.get("contact_done", False)):
            entry["contact_done"] = True

        if need_more_contacts:
            current_index = index_map.get(session_name, 0)
            raw_cursor = entry.get("contact_scan_cursor", 1)
            try:
                cursor = int(raw_cursor)
            except Exception:
                cursor = 1
            ok, detail, target_name, next_cursor = await add_next_account_as_contact(
                client,
                session_paths,
                current_index,
                current_session_name=session_name,
                already_added_targets=target_keys_done,
                start_cursor=cursor,
            )
            entry["contact_scan_cursor"] = next_cursor
            if not ok:
                entry["last_error"] = f"контакт: {detail}"
                logger.warning("  ⚠️ Контакт не добавлен: %s", _humanize_error_text(detail))
            else:
                if target_name:
                    target_key = target_name.lower()
                    if target_key not in target_keys_done:
                        stored_targets.append(target_name)
                        entry["contact_targets_done"] = stored_targets
                        target_keys_done.add(target_key)
                    entry["contact_target"] = target_name
                    entry["contact_done"] = True
                    logger.info("  ✅ %s", detail)
                    if await _random_delay(
                        config.WARMER_STEP_DELAY_MIN,
                        config.WARMER_STEP_DELAY_MAX,
                        stop_event=stop_event,
                    ):
                        return True
                else:
                    # Список контактов полностью обработан (добавлять больше некого).
                    if stored_targets or contacts_required == 0:
                        entry["contact_done"] = True

        joined_channels = {
            _normalize_channel(channel).lower()
            for channel in entry.get("joined_channels", [])
            if isinstance(channel, str)
        }
        joined_channels_ordered = [
            _normalize_channel(channel)
            for channel in entry.get("joined_channels", [])
            if isinstance(channel, str) and _normalize_channel(channel)
        ]
        failed_channels = {
            _normalize_channel(channel).lower()
            for channel in entry.get("failed_channels", [])
            if isinstance(channel, str)
        }
        failed_channels_ordered = [
            _normalize_channel(channel)
            for channel in entry.get("failed_channels", [])
            if isinstance(channel, str) and _normalize_channel(channel)
        ]

        total_channels = len({_normalize_channel(channel).lower() for channel in channels if _normalize_channel(channel)})
        done_before = len(
            {_normalize_channel(channel).lower() for channel in channels if _normalize_channel(channel)}.intersection(
                joined_channels.union(failed_channels)
            )
        )

        allowed_channels, next_due, schedule_changed = _get_allowed_channels_now(entry, total_channels)
        if schedule_changed:
            entry["schedule_wait_marker"] = ""

        remaining_budget = max(0, allowed_channels - done_before)
        pass_budget = min(max(1, int(config.WARMER_MAX_CHANNELS_PER_PASS)), remaining_budget)

        if total_channels > 0 and remaining_budget <= 0 and not _is_cycle_completed(entry, channels):
            wait_seconds = (next_due - _utc_now()).total_seconds() if next_due else 0
            wait_text = _format_wait_duration(wait_seconds) if wait_seconds > 0 else "скоро"
            marker = f"{allowed_channels}:{done_before}:{int(max(0, wait_seconds))}"
            if entry.get("schedule_wait_marker", "") != marker:
                logger.info(
                    "  ⏳ На сейчас лимит прогрева выполнен: %s/%s каналов. Следующий шаг через ~%s.",
                    done_before,
                    total_channels,
                    wait_text,
                )
                entry["schedule_wait_marker"] = marker
            entry["last_status"] = "scheduled_wait"
            entry["last_error"] = ""
            return True

        for channel in channels:
            if pass_budget <= 0:
                break
            channel_norm = _normalize_channel(channel)
            if not channel_norm:
                continue
            channel_key = channel_norm.lower()
            if channel_key in joined_channels or channel_key in failed_channels:
                continue

            join_ok, joined_now, join_detail = await join_channel(client, channel_norm)
            if not join_ok:
                failed_channels.add(channel_key)
                failed_channels_ordered.append(channel_norm)
                entry["failed_channels"] = failed_channels_ordered
                logger.warning(
                    "  ❌ Ошибка вступления в %s: %s",
                    channel_norm,
                    _humanize_error_text(join_detail),
                )
                pass_budget -= 1
                if await _random_delay(config.WARMER_STEP_DELAY_MIN, config.WARMER_STEP_DELAY_MAX, stop_event=stop_event):
                    return True
                continue

            if joined_now:
                logger.info("  ✅ Вступил в %s", channel_norm)
            else:
                logger.info("  ✅ Уже в канале %s", channel_norm)

            read_ok, read_count, reaction_count, read_detail = await simulate_reading_with_reactions(client, channel_norm)
            if not read_ok:
                failed_channels.add(channel_key)
                failed_channels_ordered.append(channel_norm)
                entry["failed_channels"] = failed_channels_ordered
                logger.warning(
                    "  ⚠️ Ошибка чтения в %s: %s",
                    channel_norm,
                    _humanize_error_text(read_detail),
                )
                pass_budget -= 1
                if await _random_delay(config.WARMER_STEP_DELAY_MIN, config.WARMER_STEP_DELAY_MAX, stop_event=stop_event):
                    return True
                continue

            reaction_note = f", +{reaction_count} реакций" if reaction_count > 0 else ""
            logger.info("  📖 Пролистал %s постов в %s%s", read_count, channel_norm, reaction_note)
            joined_channels.add(channel_key)
            joined_channels_ordered.append(channel_norm)
            entry["joined_channels"] = joined_channels_ordered
            _record_proxy_warmup_actions(entry, 1)
            pass_budget -= 1

            if await _random_delay(config.WARMER_STEP_DELAY_MIN, config.WARMER_STEP_DELAY_MAX, stop_event=stop_event):
                return True

        entry["cycle_completed"] = _is_cycle_completed(entry, channels)
        if entry["cycle_completed"]:
            entry["proxy_warmup_done"] = True
            entry["proxy_warmup_at"] = _utc_now().isoformat()
            if failed_channels:
                entry["last_status"] = "completed_with_warnings"
                entry["last_error"] = f"пропущено каналов: {len(failed_channels)}"
                logger.warning("  ⚠️ Цикл завершен с предупреждениями (пропущено каналов: %s)", len(failed_channels))
            else:
                entry["last_status"] = "completed"
                entry["last_error"] = ""
                logger.info("  ✅ Цикл завершен")
            entry["last_success_at"] = _utc_now_iso()
            entry["schedule_wait_marker"] = ""
        else:
            done_after = len(
                {_normalize_channel(channel).lower() for channel in channels if _normalize_channel(channel)}.intersection(
                    joined_channels.union(failed_channels)
                )
            )
            if total_channels > 0 and done_after >= allowed_channels:
                wait_seconds = (next_due - _utc_now()).total_seconds() if next_due else 0
                wait_text = _format_wait_duration(wait_seconds) if wait_seconds > 0 else "скоро"
                marker = f"{allowed_channels}:{done_after}:{int(max(0, wait_seconds))}"
                if entry.get("schedule_wait_marker", "") != marker:
                    logger.info(
                        "  ⏳ Прогрев идет по графику: %s/%s каналов. Следующий шаг через ~%s.",
                        done_after,
                        total_channels,
                        wait_text,
                    )
                    entry["schedule_wait_marker"] = marker
                entry["last_status"] = "scheduled_wait"
                entry["last_error"] = ""
            else:
                entry["last_status"] = "in_progress"
                entry["schedule_wait_marker"] = ""

        return True
    except Exception as exc:
        entry["cycle_completed"] = False
        entry["last_status"] = "error"
        entry["last_error"] = _short_error(exc)
        logger.error("  ❌ Критическая ошибка цикла для %s: %s", session_name, _humanize_error(exc))
        return True
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        _mark_cycle_finished()


def _sync_state_with_sessions(state: dict, session_paths: list[Path]) -> bool:
    changed = False
    sessions = state.setdefault("sessions", {})

    known_names = [get_session_name(path) for path in session_paths]
    known_name_set = set(known_names)
    fp_by_name = {
        get_session_name(path): get_session_fingerprint(path)
        for path in session_paths
    }

    # Карта fingerprint -> старое имя из state (если есть), чтобы мигрировать прогресс
    # при переносе/переименовании session-файла.
    stale_name_by_fp: dict[str, str] = {}
    stale_name_by_basename: dict[str, str] = {}
    for state_name, payload in list(sessions.items()):
        if not isinstance(payload, dict):
            sessions[state_name] = _default_session_state()
            payload = sessions[state_name]
            changed = True

        if state_name in known_name_set:
            continue

        fp = str(payload.get("session_fingerprint", "")).strip()
        if fp and fp not in stale_name_by_fp:
            stale_name_by_fp[fp] = state_name

        base = Path(state_name).name.lower()
        if base and base not in stale_name_by_basename:
            stale_name_by_basename[base] = state_name

    for session_name in known_names:
        if session_name not in sessions:
            migrated_name = ""
            fp = fp_by_name.get(session_name, "")
            if fp:
                migrated_name = stale_name_by_fp.get(fp, "")
            if not migrated_name:
                migrated_name = stale_name_by_basename.get(Path(session_name).name.lower(), "")

            if migrated_name and migrated_name in sessions and migrated_name not in known_name_set:
                sessions[session_name] = sessions.pop(migrated_name)
                changed = True
            else:
                sessions[session_name] = _default_session_state()
                changed = True

        entry = sessions.get(session_name)
        if not isinstance(entry, dict):
            sessions[session_name] = _default_session_state()
            entry = sessions[session_name]
            changed = True

        fp_now = fp_by_name.get(session_name, "")
        if str(entry.get("session_fingerprint", "")).strip() != fp_now:
            entry["session_fingerprint"] = fp_now
            changed = True

    stale_names = [name for name in list(sessions.keys()) if name not in known_name_set]
    for stale_name in stale_names:
        del sessions[stale_name]
        changed = True

    return changed


async def warm_up_once(
    *,
    stop_event: Optional[asyncio.Event] = None,
    is_busy: Optional[Callable[[], bool]] = None,
) -> None:
    if stop_event and stop_event.is_set():
        return

    session_paths = get_session_files()
    if not session_paths:
        return

    catalog = load_channel_catalog()
    all_channels = flatten_channel_catalog(catalog)
    if not all_channels:
        logger.warning(
            "⚠️ channels.txt пустой или отсутствует. Добавь каналы (можно с префиксами indian:/russian:/global:)."
        )

    state = load_warmer_state()
    changed = _sync_state_with_sessions(state, session_paths)

    index_map = {get_session_name(path): idx for idx, path in enumerate(session_paths)}

    if config.WARMER_PARALLEL_MODE:
        # Параллельный режим: каждый аккаунт обрабатывается в своём таске.
        changed = await _warm_up_parallel(
            session_paths=session_paths,
            catalog=catalog,
            state=state,
            index_map=index_map,
            initial_changed=changed,
            stop_event=stop_event,
            is_busy=is_busy,
        )
    else:
        for idx, session_path in enumerate(session_paths):
            if stop_event and stop_event.is_set():
                break

            if is_busy and is_busy():
                break

            session_changed = await _process_one_session(
                session_path=session_path,
                session_paths=session_paths,
                catalog=catalog,
                state=state,
                index_map=index_map,
                stop_event=stop_event,
            )
            if session_changed:
                changed = True
                async with _state_lock:
                    save_warmer_state(state)

            if await _random_delay(
                config.WARMER_ACCOUNT_PAUSE_MIN,
                config.WARMER_ACCOUNT_PAUSE_MAX,
                stop_event=stop_event,
                log_message="⏳ Ожидание %s сек. перед следующим аккаунтом...",
            ):
                break

    if changed:
        async with _state_lock:
            save_warmer_state(state)


async def _process_one_session(
    *,
    session_path: Path,
    session_paths: list[Path],
    catalog,
    state: dict,
    index_map: dict,
    stop_event: Optional[asyncio.Event],
) -> bool:
    """Обрабатывает один аккаунт. Возвращает True, если state изменился.

    Извлечено из старого `warm_up_once` чтобы можно было запускать параллельно.
    """
    session_name = get_session_name(session_path)
    entry = ensure_session_state(state, session_name)
    planned_channels, plan_changed = ensure_planned_channels(entry, session_name, catalog)
    changed = plan_changed

    if _is_cycle_completed(entry, planned_channels):
        if not entry.get("cycle_completed"):
            entry["cycle_completed"] = True
            entry["last_status"] = "completed"
            changed = True
        return changed

    last_run = entry.get("last_run_at", "")
    is_first_cycle = not last_run or last_run == ""

    if is_first_cycle and config.WARMER_STAGGERED_START_MIN > 0:
        stagger_delay = random.uniform(
            config.WARMER_STAGGERED_START_MIN,
            config.WARMER_STAGGERED_START_MAX,
        )
        logger.info(
            "⏳ Staggered start: аккаунт %s первый запуск, ожидание %s сек...",
            session_name,
            int(stagger_delay),
        )
        if await _sleep_with_stop(stagger_delay, stop_event=stop_event):
            return changed

    country = detect_session_country(session_path)

    if config.WARMER_PERSONAL_SCHEDULE_ENABLED:
        if _ensure_daily_schedule(entry, session_name, country):
            changed = True
        in_window, _seconds = _is_in_schedule_window(entry, country)
        if not in_window:
            entry["last_status"] = "off_schedule"
            entry["last_error"] = ""
            return changed
    else:
        if _is_account_quiet_hours(country):
            entry["last_status"] = "quiet_hours"
            entry["last_error"] = ""
            return changed

    if _should_account_rest(session_name):
        entry["last_status"] = "resting"
        entry["last_error"] = ""
        logger.info("😴 %s: отдыхает (rest chance)", session_name)
        return changed

    cycle_changed = await process_account_cycle(
        session_path=session_path,
        session_paths=session_paths,
        index_map=index_map,
        channels=planned_channels,
        state=state,
        stop_event=stop_event,
    )
    return bool(cycle_changed) or changed


async def _warm_up_parallel(
    *,
    session_paths: list[Path],
    catalog,
    state: dict,
    index_map: dict,
    initial_changed: bool,
    stop_event: Optional[asyncio.Event],
    is_busy: Optional[Callable[[], bool]],
) -> bool:
    """Параллельная обработка аккаунтов. С ограничением concurrency.

    Каждый аккаунт обрабатывается своим таском, но не больше
    WARMER_PARALLEL_CONCURRENCY одновременно (защита от рейт-лимитов Telegram
    и от перегрузки CPU/памяти).
    """
    changed = initial_changed
    semaphore = asyncio.Semaphore(max(1, int(config.WARMER_PARALLEL_CONCURRENCY)))

    async def worker(session_path: Path) -> None:
        nonlocal changed
        async with semaphore:
            if stop_event and stop_event.is_set():
                return
            if is_busy and is_busy():
                return
            try:
                session_changed = await _process_one_session(
                    session_path=session_path,
                    session_paths=session_paths,
                    catalog=catalog,
                    state=state,
                    index_map=index_map,
                    stop_event=stop_event,
                )
                if session_changed:
                    changed = True
                    async with _state_lock:
                        save_warmer_state(state)
            except Exception:
                logger.exception(
                    "❌ Ошибка обработки аккаунта в параллельном режиме: %s",
                    get_session_name(session_path),
                )

            # Небольшая пауза перед освобождением слота — рассинхронизация
            await _random_delay(
                max(1, config.WARMER_ACCOUNT_PAUSE_MIN // 4),
                max(2, config.WARMER_ACCOUNT_PAUSE_MAX // 4),
                stop_event=stop_event,
            )

    tasks = [asyncio.create_task(worker(p)) for p in session_paths]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return changed


async def run_warmer_service(
    *,
    stop_event: Optional[asyncio.Event] = None,
    is_busy: Optional[Callable[[], bool]] = None,
) -> None:
    async with _service_lock:
        logger.info("🚀 Сервис прогрева запущен")
        while True:
            if stop_event and stop_event.is_set():
                break

            if not config.WARMER_ENABLED:
                if await _sleep_with_stop(config.WARMER_SCAN_INTERVAL, stop_event=stop_event):
                    break
                continue

            if is_busy and is_busy():
                if await _sleep_with_stop(5, stop_event=stop_event):
                    break
                continue

            try:
                await warm_up_once(stop_event=stop_event, is_busy=is_busy)
            except Exception:
                logger.exception("❌ Критическая ошибка прохода прогрева")

            if await _sleep_with_stop(config.WARMER_SCAN_INTERVAL, stop_event=stop_event):
                break

        logger.info("🛑 Сервис прогрева остановлен")


if __name__ == "__main__":
    asyncio.run(run_warmer_service())

