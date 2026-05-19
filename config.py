from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
SESSIONS_DIR = BASE_DIR / "sessions"
TARGETS_FILE = BASE_DIR / "targets.txt"
PROCESSED_USERS_FILE = BASE_DIR / "processed_usernames.txt"
SAVED_TARGET_GROUP_FILE = BASE_DIR / "saved_target_group.txt"
COUNTRY_PROXIES_FILE = BASE_DIR / "country_proxies.json"
PROXY_POOL_FILE = BASE_DIR / "proxy_pool.json"
ACCOUNT_COOLDOWNS_FILE = BASE_DIR / "account_cooldowns.json"
DEFERRED_USERS_FILE = BASE_DIR / "deferred_usernames.json"
SESSION_PROXY_BINDINGS_FILE = BASE_DIR / "session_proxy_bindings.json"
ENTITY_CACHE_FILE = BASE_DIR / "entity_cache.json"
WARMER_STATE_FILE = BASE_DIR / "warmer_state.json"
WARMER_CHANNELS_FILE = BASE_DIR / "channels.txt"

_proxy_rotation_state: dict[str, int] = {}
_proxy_last_selected_index: dict[str, int] = {}


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _get_str(name: str, default: Optional[str] = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and (value is None or value == ""):
        raise ValueError(f"Переменная {name} не заполнена. Добавь ее в .env")
    return value or ""


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default

    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть числом, сейчас: {raw!r}") from exc


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default

    return raw.strip().lower() in {"1", "true", "yes", "on"}


_load_env_file(ENV_PATH)

BOT_TOKEN = _get_str("BOT_TOKEN", required=True)
API_ID = _get_int("API_ID", 0)
API_HASH = _get_str("API_HASH", required=True)
ACCOUNT_IMPORT_DIR = Path(_get_str("ACCOUNT_IMPORT_DIR", "account_import"))
if not ACCOUNT_IMPORT_DIR.is_absolute():
    ACCOUNT_IMPORT_DIR = BASE_DIR / ACCOUNT_IMPORT_DIR

MIN_DELAY = _get_int("MIN_DELAY", 120)
MAX_DELAY = _get_int("MAX_DELAY", 300)
INVITES_PER_ACC = _get_int("INVITES_PER_ACC", 10)
INVITE_BAN_COOLDOWN_HOURS = _get_int("INVITE_BAN_COOLDOWN_HOURS", 24)
INVITE_WARMUP_ENABLED = _get_bool("INVITE_WARMUP_ENABLED", True)
INVITE_WARMUP_TEST_ONLY = _get_bool("INVITE_WARMUP_TEST_ONLY", False)
INVITE_MAX_PER_SESSION = _get_int("INVITE_MAX_PER_SESSION", 3)
INVITE_MAX_CONCURRENT = _get_int("INVITE_MAX_CONCURRENT", 1)
INVITE_CASCADE_PAUSE_SECONDS = _get_int("INVITE_CASCADE_PAUSE_SECONDS", 60)
INVITE_CIRCUIT_BREAKER_THRESHOLD = _get_int("INVITE_CIRCUIT_BREAKER_THRESHOLD", 2)
INVITE_CIRCUIT_BREAKER_WINDOW_SECONDS = _get_int("INVITE_CIRCUIT_BREAKER_WINDOW_SECONDS", 600)
INVITE_CIRCUIT_BREAKER_PAUSE_SECONDS = _get_int("INVITE_CIRCUIT_BREAKER_PAUSE_SECONDS", 3600)
INVITE_MIN_READY_ACCOUNTS = _get_int("INVITE_MIN_READY_ACCOUNTS", 3)
INVITE_DEFER_USER_HOURS = _get_int("INVITE_DEFER_USER_HOURS", 48)
INVITE_USE_SEARCH_REQUEST = _get_bool("INVITE_USE_SEARCH_REQUEST", False)
INVITE_ENTITY_CACHE_ENABLED = _get_bool("INVITE_ENTITY_CACHE_ENABLED", True)
SESSION_PROXY_STICKY_ENABLED = _get_bool("SESSION_PROXY_STICKY_ENABLED", True)
PROXY_MAX_ACCOUNTS_PER_IP = _get_int("PROXY_MAX_ACCOUNTS_PER_IP", 5)
INVITE_PAUSE_BETWEEN_MIN = _get_int("INVITE_PAUSE_BETWEEN_MIN", 30)   # deprecated
INVITE_PAUSE_BETWEEN_MAX = _get_int("INVITE_PAUSE_BETWEEN_MAX", 180)   # deprecated
INVITE_NATURAL_READING_ENABLED = _get_bool("INVITE_NATURAL_READING_ENABLED", True)
INVITE_PRE_ENGAGE_ENABLED = _get_bool("INVITE_PRE_ENGAGE_ENABLED", True)
INVITE_PRE_ENGAGE_MIN_TIME = _get_int("INVITE_PRE_ENGAGE_MIN_TIME", 180)
INVITE_PRE_ENGAGE_MAX_TIME = _get_int("INVITE_PRE_ENGAGE_MAX_TIME", 600)
INVITE_FIRST_DELAY_MIN = _get_int("INVITE_FIRST_DELAY_MIN", 60)
INVITE_FIRST_DELAY_MAX = _get_int("INVITE_FIRST_DELAY_MAX", 600)
# Stagger между TCP-коннектами при старте инвайта — рассинхронизирует
# подключения чтобы asocks/Telegram не получали burst одновременных запросов
INVITE_STAGGER_MIN = _get_int("INVITE_STAGGER_MIN", 3)
INVITE_STAGGER_MAX = _get_int("INVITE_STAGGER_MAX", 10)
INVITE_RAMP_UP_ENABLED = _get_bool("INVITE_RAMP_UP_ENABLED", True)
FLOOD_WAIT_EXTRA_SECONDS = _get_int("FLOOD_WAIT_EXTRA_SECONDS", 60)
APPROVAL_CHECK_INTERVAL = _get_int("APPROVAL_CHECK_INTERVAL", 120)
SPAMBOT_CHECK_ENABLED = _get_bool("SPAMBOT_CHECK_ENABLED", True)
SPAMBOT_TIMEOUT_SECONDS = _get_int("SPAMBOT_TIMEOUT_SECONDS", 25)
SPAMBOT_LIMITED_COOLDOWN_HOURS = _get_int("SPAMBOT_LIMITED_COOLDOWN_HOURS", 24)

USE_PROXY = _get_bool("USE_PROXY", False)
PROXY_TYPE = _get_str("PROXY_TYPE", "socks5")
PROXY_ADDR = _get_str("PROXY_ADDR", "")
PROXY_PORT = _get_int("PROXY_PORT", 1080)
PROXY_USER = _get_str("PROXY_USER", "")
PROXY_PASS = _get_str("PROXY_PASS", "")
PROXY_CHECK_INTERVAL = _get_int("PROXY_CHECK_INTERVAL", 30)
PROXY_CHECK_TIMEOUT = _get_int("PROXY_CHECK_TIMEOUT", 8)
WARMER_ENABLED = _get_bool("WARMER_ENABLED", True)
WARMER_SCAN_INTERVAL = _get_int("WARMER_SCAN_INTERVAL", 45)
WARMER_STEP_DELAY_MIN = _get_int("WARMER_STEP_DELAY_MIN", 4)
WARMER_STEP_DELAY_MAX = _get_int("WARMER_STEP_DELAY_MAX", 12)
WARMER_ACCOUNT_PAUSE_MIN = _get_int("WARMER_ACCOUNT_PAUSE_MIN", 20)
WARMER_ACCOUNT_PAUSE_MAX = _get_int("WARMER_ACCOUNT_PAUSE_MAX", 60)
WARMER_QUIET_HOURS_START = _get_int("WARMER_QUIET_HOURS_START", 2)
WARMER_QUIET_HOURS_END = _get_int("WARMER_QUIET_HOURS_END", 7)
WARMER_REST_CHANCE = _get_int("WARMER_REST_CHANCE", 30)
WARMER_MANUAL_START_PERCENT = _get_int("WARMER_MANUAL_START_PERCENT", 80)
WARMER_CHANNELS_PER_ACCOUNT = _get_int("WARMER_CHANNELS_PER_ACCOUNT", 25)
WARMER_CHANNEL_DISTRIBUTION_DAYS = _get_int("WARMER_CHANNEL_DISTRIBUTION_DAYS", 3)
WARMER_MAX_CHANNELS_PER_PASS = _get_int("WARMER_MAX_CHANNELS_PER_PASS", 2)
WARMER_STAGGERED_START_MIN = _get_int("WARMER_STAGGERED_START_MIN", 3600)
WARMER_STAGGERED_START_MAX = _get_int("WARMER_STAGGERED_START_MAX", 21600)
AUTO_IMPORT_SYNC_ENABLED = _get_bool("AUTO_IMPORT_SYNC_ENABLED", True)
AUTO_IMPORT_SYNC_INTERVAL = _get_int("AUTO_IMPORT_SYNC_INTERVAL", 300)
AUTO_CLEANUP_ENABLED = _get_bool("AUTO_CLEANUP_ENABLED", True)
AUTO_CLEANUP_INTERVAL = _get_int("AUTO_CLEANUP_INTERVAL", 3600)
LOLZ_API_TOKEN = _get_str("LOLZ_API_TOKEN", "")

WARMER_PERSONAL_SCHEDULE_ENABLED = _get_bool("WARMER_PERSONAL_SCHEDULE_ENABLED", True)
WARMER_PARALLEL_MODE = _get_bool("WARMER_PARALLEL_MODE", True)
WARMER_PARALLEL_CONCURRENCY = _get_int("WARMER_PARALLEL_CONCURRENCY", 5)
# Аккаунт с большой историей чатов у продавца помечается pre_warmed,
# но в инвайт идёт только после proxy_warmup_done на вашем прокси.
WARMER_PREWARMED_THRESHOLD = _get_int("WARMER_PREWARMED_THRESHOLD", 15)
WARMER_PROXY_WARMUP_MIN_ACTIONS = _get_int("WARMER_PROXY_WARMUP_MIN_ACTIONS", 5)
# Включает DM-обмен между своими аккаунтами в warmer-цикле.
# DM — самый сильный trust-сигнал для Telegram. Но если перебор —
# можно попасть в "Spam" фильтр. Дефолт = on.
WARMER_DM_ENABLED = _get_bool("WARMER_DM_ENABLED", True)
WARMER_PROFILE_SETUP_ENABLED = _get_bool("WARMER_PROFILE_SETUP_ENABLED", True)
_profile_photos_raw = _get_str("WARMER_PROFILE_PHOTOS_DIR", "profile_photos")
WARMER_PROFILE_PHOTOS_DIR = Path(_profile_photos_raw) if Path(_profile_photos_raw).is_absolute() else BASE_DIR / _profile_photos_raw
WARMER_PROFILE_BIO_ENABLED = _get_bool("WARMER_PROFILE_BIO_ENABLED", True)
INVITE_VERIFY_ENABLED = _get_bool("INVITE_VERIFY_ENABLED", False)

if API_ID <= 0:
    raise ValueError("API_ID должен быть положительным числом")

if MIN_DELAY < 0 or MAX_DELAY < 0:
    raise ValueError("MIN_DELAY и MAX_DELAY не могут быть отрицательными")

if MIN_DELAY > MAX_DELAY:
    raise ValueError("MIN_DELAY не может быть больше MAX_DELAY")

if INVITES_PER_ACC <= 0:
    raise ValueError("INVITES_PER_ACC должен быть больше нуля")

if INVITE_BAN_COOLDOWN_HOURS <= 0:
    raise ValueError("INVITE_BAN_COOLDOWN_HOURS должен быть больше нуля")

if FLOOD_WAIT_EXTRA_SECONDS < 0:
    raise ValueError("FLOOD_WAIT_EXTRA_SECONDS не может быть отрицательным")

if APPROVAL_CHECK_INTERVAL <= 0:
    raise ValueError("APPROVAL_CHECK_INTERVAL должен быть больше нуля")

if SPAMBOT_TIMEOUT_SECONDS <= 0:
    raise ValueError("SPAMBOT_TIMEOUT_SECONDS должен быть больше нуля")

if SPAMBOT_LIMITED_COOLDOWN_HOURS <= 0:
    raise ValueError("SPAMBOT_LIMITED_COOLDOWN_HOURS должен быть больше нуля")

if PROXY_CHECK_INTERVAL <= 0:
    raise ValueError("PROXY_CHECK_INTERVAL должен быть больше нуля")

if PROXY_CHECK_TIMEOUT <= 0:
    raise ValueError("PROXY_CHECK_TIMEOUT должен быть больше нуля")

if WARMER_SCAN_INTERVAL <= 0:
    raise ValueError("WARMER_SCAN_INTERVAL должен быть больше нуля")

if WARMER_STEP_DELAY_MIN < 0 or WARMER_STEP_DELAY_MAX < 0:
    raise ValueError("WARMER_STEP_DELAY_MIN и WARMER_STEP_DELAY_MAX не могут быть отрицательными")

if WARMER_STEP_DELAY_MIN > WARMER_STEP_DELAY_MAX:
    raise ValueError("WARMER_STEP_DELAY_MIN не может быть больше WARMER_STEP_DELAY_MAX")

if WARMER_ACCOUNT_PAUSE_MIN < 0 or WARMER_ACCOUNT_PAUSE_MAX < 0:
    raise ValueError("WARMER_ACCOUNT_PAUSE_MIN и WARMER_ACCOUNT_PAUSE_MAX не могут быть отрицательными")

if WARMER_ACCOUNT_PAUSE_MIN > WARMER_ACCOUNT_PAUSE_MAX:
    raise ValueError("WARMER_ACCOUNT_PAUSE_MIN не может быть больше WARMER_ACCOUNT_PAUSE_MAX")

if WARMER_MANUAL_START_PERCENT < 1 or WARMER_MANUAL_START_PERCENT > 99:
    raise ValueError("WARMER_MANUAL_START_PERCENT должен быть в диапазоне 1..99")

if WARMER_CHANNELS_PER_ACCOUNT <= 0:
    raise ValueError("WARMER_CHANNELS_PER_ACCOUNT должен быть больше нуля")

if WARMER_CHANNEL_DISTRIBUTION_DAYS <= 0:
    raise ValueError("WARMER_CHANNEL_DISTRIBUTION_DAYS должен быть больше нуля")

if WARMER_MAX_CHANNELS_PER_PASS <= 0:
    raise ValueError("WARMER_MAX_CHANNELS_PER_PASS должен быть больше нуля")

if INVITE_MAX_PER_SESSION <= 0:
    raise ValueError("INVITE_MAX_PER_SESSION должен быть больше нуля")

if INVITE_MAX_CONCURRENT <= 0:
    raise ValueError("INVITE_MAX_CONCURRENT должен быть больше нуля")

if INVITE_CASCADE_PAUSE_SECONDS < 0:
    raise ValueError("INVITE_CASCADE_PAUSE_SECONDS не может быть отрицательным")

if INVITE_CIRCUIT_BREAKER_THRESHOLD <= 0:
    raise ValueError("INVITE_CIRCUIT_BREAKER_THRESHOLD должен быть больше нуля")

if INVITE_CIRCUIT_BREAKER_WINDOW_SECONDS <= 0:
    raise ValueError("INVITE_CIRCUIT_BREAKER_WINDOW_SECONDS должен быть больше нуля")

if INVITE_CIRCUIT_BREAKER_PAUSE_SECONDS <= 0:
    raise ValueError("INVITE_CIRCUIT_BREAKER_PAUSE_SECONDS должен быть больше нуля")

if INVITE_MIN_READY_ACCOUNTS <= 0:
    raise ValueError("INVITE_MIN_READY_ACCOUNTS должен быть больше нуля")

if INVITE_DEFER_USER_HOURS <= 0:
    raise ValueError("INVITE_DEFER_USER_HOURS должен быть больше нуля")

if WARMER_PROXY_WARMUP_MIN_ACTIONS <= 0:
    raise ValueError("WARMER_PROXY_WARMUP_MIN_ACTIONS должен быть больше нуля")

if PROXY_MAX_ACCOUNTS_PER_IP <= 0:
    raise ValueError("PROXY_MAX_ACCOUNTS_PER_IP должен быть больше нуля")

if INVITE_PAUSE_BETWEEN_MIN < 0 or INVITE_PAUSE_BETWEEN_MAX < 0:
    raise ValueError("INVITE_PAUSE_BETWEEN_MIN и INVITE_PAUSE_BETWEEN_MAX не могут быть отрицательными")

if INVITE_PAUSE_BETWEEN_MIN > INVITE_PAUSE_BETWEEN_MAX:
    raise ValueError("INVITE_PAUSE_BETWEEN_MIN не может быть больше INVITE_PAUSE_BETWEEN_MAX")

if WARMER_STAGGERED_START_MIN < 0 or WARMER_STAGGERED_START_MAX < 0:
    raise ValueError("WARMER_STAGGERED_START_MIN и WARMER_STAGGERED_START_MAX не могут быть отрицательными")

if WARMER_STAGGERED_START_MIN > WARMER_STAGGERED_START_MAX:
    raise ValueError("WARMER_STAGGERED_START_MIN не может быть больше WARMER_STAGGERED_START_MAX")

if WARMER_QUIET_HOURS_START < 0 or WARMER_QUIET_HOURS_START > 23:
    raise ValueError("WARMER_QUIET_HOURS_START должен быть 0..23")

if WARMER_QUIET_HOURS_END < 0 or WARMER_QUIET_HOURS_END > 23:
    raise ValueError("WARMER_QUIET_HOURS_END должен быть 0..23")

if WARMER_REST_CHANCE < 0 or WARMER_REST_CHANCE > 100:
    raise ValueError("WARMER_REST_CHANCE должен быть 0..100")


def build_proxy() -> Optional[Tuple]:
    if not USE_PROXY:
        return None

    if not PROXY_ADDR:
        raise ValueError("USE_PROXY=true, но не заполнен PROXY_ADDR")

    if PROXY_USER and PROXY_PASS:
        return (PROXY_TYPE, PROXY_ADDR, PROXY_PORT, True, PROXY_USER, PROXY_PASS)

    return (PROXY_TYPE, PROXY_ADDR, PROXY_PORT)


def _build_proxy_tuple(proxy_type: str, addr: str, port: int, user: str, password: str) -> Tuple:
    if user and password:
        return (proxy_type, addr, port, True, user, password)
    return (proxy_type, addr, port)


def _normalize_proxy_item(value: object) -> Optional[dict]:
    if not isinstance(value, dict):
        return None

    addr = str(value.get("addr", "")).strip()
    port_raw = value.get("port")
    if not addr or port_raw is None:
        return None

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        return None

    if port <= 0:
        return None

    proxy_type = str(value.get("type", "socks5")).strip().lower() or "socks5"
    user = str(value.get("user", "")).strip()
    password = str(value.get("pass", "")).strip()

    return {
        "type": proxy_type,
        "addr": addr,
        "port": port,
        "user": user,
        "pass": password,
    }


def _normalize_proxy_bucket(value: object) -> list[dict]:
    if isinstance(value, list):
        normalized: list[dict] = []
        for item in value:
            normalized_item = _normalize_proxy_item(item)
            if normalized_item:
                normalized.append(normalized_item)
        return normalized

    normalized_item = _normalize_proxy_item(value)
    if normalized_item:
        return [normalized_item]

    return []


def _get_proxy_config_source_file() -> Path:
    if PROXY_POOL_FILE.exists():
        return PROXY_POOL_FILE
    return COUNTRY_PROXIES_FILE


def load_country_proxy_pools() -> dict[str, list[dict]]:
    source_file = _get_proxy_config_source_file()
    if not source_file.exists():
        return {}

    try:
        raw = json.loads(source_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Не удалось прочитать {source_file.name}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{source_file.name} должен быть JSON-объектом.")

    result: dict[str, list[dict]] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue

        bucket = _normalize_proxy_bucket(value)
        if not bucket:
            continue

        result[key.strip().lower()] = bucket

    return result


def load_country_proxies() -> dict[str, dict]:
    pools = load_country_proxy_pools()
    result: dict[str, dict] = {}
    for key, bucket in pools.items():
        if bucket:
            result[key] = bucket[0]
    return result


def _select_proxy_from_pool(pool_key: str, pool: list[dict], *, advance_rotation: bool) -> tuple[dict, int]:
    if not pool:
        raise ValueError("Пустой пул прокси")

    if advance_rotation:
        index = _proxy_rotation_state.get(pool_key, 0) % len(pool)
        _proxy_rotation_state[pool_key] = index + 1
        _proxy_last_selected_index[pool_key] = index
        return pool[index], index

    last_index = _proxy_last_selected_index.get(pool_key)
    if last_index is not None and 0 <= last_index < len(pool):
        return pool[last_index], last_index

    preview_index = _proxy_rotation_state.get(pool_key, 0) % len(pool)
    return pool[preview_index], preview_index


def _format_proxy_desc(proxy_item: dict, *, country_key: str, selected_index: int, pool_size: int) -> str:
    return (
        f"{proxy_item['type']}://{proxy_item['addr']}:{proxy_item['port']} "
        f"(geo:{country_key} {selected_index + 1}/{pool_size})"
    )


def resolve_proxy_for_country_with_desc(
    country: str,
    *,
    advance_rotation: bool = True,
) -> tuple[Optional[Tuple], str]:
    if not USE_PROXY:
        return None, "выключен"

    country_key = country.strip().lower() or "default"
    pools_map = load_country_proxy_pools()
    if pools_map:
        selected_pool_key = country_key if country_key in pools_map else "default" if "default" in pools_map else ""
        if selected_pool_key:
            selected_item, selected_index = _select_proxy_from_pool(
                selected_pool_key,
                pools_map[selected_pool_key],
                advance_rotation=advance_rotation,
            )
            proxy_tuple = _build_proxy_tuple(
                proxy_type=selected_item["type"],
                addr=selected_item["addr"],
                port=selected_item["port"],
                user=selected_item["user"],
                password=selected_item["pass"],
            )
            desc = _format_proxy_desc(
                selected_item,
                country_key=selected_pool_key,
                selected_index=selected_index,
                pool_size=len(pools_map[selected_pool_key]),
            )
            return proxy_tuple, desc

        if PROXY_ADDR:
            return build_proxy(), f"{PROXY_TYPE}://{PROXY_ADDR}:{PROXY_PORT} (global)"
        return None, "не найден"

    if PROXY_ADDR:
        return build_proxy(), f"{PROXY_TYPE}://{PROXY_ADDR}:{PROXY_PORT} (global)"
    return None, "не найден"


def resolve_proxy_for_country(country: str) -> Optional[Tuple]:
    proxy_tuple, _ = resolve_proxy_for_country_with_desc(country, advance_rotation=True)
    return proxy_tuple


def describe_proxy_for_country(country: str) -> str:
    _, desc = resolve_proxy_for_country_with_desc(country, advance_rotation=False)
    return desc


def _stable_proxy_index(session_key: str, pool_size: int) -> int:
    if pool_size <= 0:
        return 0
    digest = hashlib.md5(session_key.encode("utf-8")).hexdigest()
    return int(digest, 16) % pool_size


def load_session_proxy_bindings() -> dict[str, dict]:
    if not SESSION_PROXY_BINDINGS_FILE.exists():
        return {}
    try:
        raw = json.loads(SESSION_PROXY_BINDINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): v for k, v in raw.items() if isinstance(v, dict)}


def save_session_proxy_bindings(bindings: dict[str, dict]) -> None:
    normalized: dict[str, dict] = {}
    for session_name, payload in bindings.items():
        if not isinstance(payload, dict):
            continue
        key = normalize_session_key(session_name)
        if not key:
            continue
        pool_key = str(payload.get("pool_key", "")).strip().lower()
        try:
            index = int(payload.get("index", 0))
        except (TypeError, ValueError):
            continue
        if not pool_key or index < 0:
            continue
        normalized[key] = {"pool_key": pool_key, "index": index}
    SESSION_PROXY_BINDINGS_FILE.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _resolve_pool_key_for_country(country: str, pools_map: dict[str, list[dict]]) -> str:
    country_key = country.strip().lower() or "default"
    if country_key in pools_map:
        return country_key
    if "default" in pools_map:
        return "default"
    return ""


def resolve_proxy_for_session_with_desc(
    session_name: str,
    country: str,
    *,
    advance_rotation: bool = False,
) -> tuple[Optional[Tuple], str]:
    """Sticky-прокси: один session → один индекс в пуле страны."""
    if not USE_PROXY:
        return None, "выключен"

    if not SESSION_PROXY_STICKY_ENABLED:
        return resolve_proxy_for_country_with_desc(country, advance_rotation=advance_rotation)

    pools_map = load_country_proxy_pools()
    pool_key = _resolve_pool_key_for_country(country, pools_map)
    if not pool_key:
        if PROXY_ADDR:
            return build_proxy(), f"{PROXY_TYPE}://{PROXY_ADDR}:{PROXY_PORT} (global)"
        return None, "не найден"

    pool = pools_map[pool_key]
    session_key = normalize_session_key(session_name)
    bindings = load_session_proxy_bindings()

    if session_key in bindings:
        stored = bindings[session_key]
        stored_pool = str(stored.get("pool_key", "")).strip().lower()
        try:
            stored_index = int(stored.get("index", -1))
        except (TypeError, ValueError):
            stored_index = -1
        if stored_pool == pool_key and 0 <= stored_index < len(pool):
            selected_item = pool[stored_index]
            selected_index = stored_index
        else:
            selected_index = _stable_proxy_index(session_key, len(pool))
            selected_item = pool[selected_index]
            bindings[session_key] = {"pool_key": pool_key, "index": selected_index}
            save_session_proxy_bindings(bindings)
    else:
        selected_index = _stable_proxy_index(session_key, len(pool))
        selected_item = pool[selected_index]
        bindings[session_key] = {"pool_key": pool_key, "index": selected_index}
        save_session_proxy_bindings(bindings)

    proxy_tuple = _build_proxy_tuple(
        proxy_type=selected_item["type"],
        addr=selected_item["addr"],
        port=selected_item["port"],
        user=selected_item["user"],
        password=selected_item["pass"],
    )
    desc = (
        f"{_format_proxy_desc(selected_item, country_key=pool_key, selected_index=selected_index, pool_size=len(pool))}"
        f" [sticky:{session_key}]"
    )
    return proxy_tuple, desc


def resolve_proxy_for_session(session_name: str, country: str) -> Optional[Tuple]:
    proxy_tuple, _ = resolve_proxy_for_session_with_desc(session_name, country)
    return proxy_tuple


def load_entity_cache() -> dict[str, dict]:
    if not ENTITY_CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(ENTITY_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def save_entity_cache(cache: dict[str, dict]) -> None:
    ENTITY_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_cached_entity(username: str) -> Optional[dict]:
    key = username.strip().lower().lstrip("@")
    if not key:
        return None
    entry = load_entity_cache().get(key)
    if not isinstance(entry, dict):
        return None
    user_id = entry.get("id")
    access_hash = entry.get("access_hash")
    if user_id is None or access_hash is None:
        return None
    try:
        return {"id": int(user_id), "access_hash": int(access_hash)}
    except (TypeError, ValueError):
        return None


def set_cached_entity(username: str, user_id: int, access_hash: int) -> None:
    key = username.strip().lower().lstrip("@")
    if not key:
        return
    cache = load_entity_cache()
    cache[key] = {
        "id": int(user_id),
        "access_hash": int(access_hash),
        "cached_at": _utc_now().isoformat(),
    }
    save_entity_cache(cache)


def load_deferred_usernames() -> dict[str, dict]:
    if not DEFERRED_USERS_FILE.exists():
        return {}
    try:
        raw = json.loads(DEFERRED_USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}

    now = _utc_now()
    active: dict[str, dict] = {}
    changed = False
    for username, payload in raw.items():
        key = str(username).strip().lower().lstrip("@")
        if not key:
            changed = True
            continue
        if not isinstance(payload, dict):
            changed = True
            continue
        until_dt = _parse_datetime(payload.get("until"))
        if not until_dt or until_dt <= now:
            changed = True
            continue
        active[key] = {
            "until": until_dt.isoformat(),
            "reason": str(payload.get("reason", "")).strip(),
            "attempts": int(payload.get("attempts", 1)),
        }
    if changed:
        save_deferred_usernames(active)
    return active


def save_deferred_usernames(deferred: dict[str, dict]) -> None:
    normalized: dict[str, dict] = {}
    for username, payload in deferred.items():
        key = str(username).strip().lower().lstrip("@")
        if not key or not isinstance(payload, dict):
            continue
        until_dt = _parse_datetime(payload.get("until"))
        if not until_dt:
            continue
        normalized[key] = {
            "until": until_dt.isoformat(),
            "reason": str(payload.get("reason", "")).strip(),
            "attempts": int(payload.get("attempts", 1)),
        }
    DEFERRED_USERS_FILE.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def defer_username(username: str, *, seconds: int, reason: str = "") -> None:
    key = username.strip().lower().lstrip("@")
    if not key:
        return
    deferred = load_deferred_usernames()
    prev_attempts = 0
    if key in deferred:
        prev_attempts = int(deferred[key].get("attempts", 0))
    until_dt = _utc_now() + timedelta(seconds=max(1, int(seconds)))
    deferred[key] = {
        "until": until_dt.isoformat(),
        "reason": reason.strip(),
        "attempts": prev_attempts + 1,
    }
    save_deferred_usernames(deferred)


def is_username_deferred(username: str) -> tuple[bool, str]:
    key = username.strip().lower().lstrip("@")
    if not key:
        return False, ""
    entry = load_deferred_usernames().get(key)
    if not entry:
        return False, ""
    until_dt = _parse_datetime(entry.get("until"))
    if not until_dt:
        return False, ""
    seconds_left = int((until_dt - _utc_now()).total_seconds())
    if seconds_left <= 0:
        return False, ""
    reason = str(entry.get("reason", "")).strip()
    return True, reason or f"отложен на {format_duration(seconds_left)}"


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


def load_processed_usernames() -> set[str]:
    if not PROCESSED_USERS_FILE.exists():
        return set()

    processed: set[str] = set()
    for line in PROCESSED_USERS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        username = line.strip().removeprefix("@")
        if username:
            processed.add(username.lower())
    return processed


def load_saved_target_group() -> str:
    if not SAVED_TARGET_GROUP_FILE.exists():
        return ""

    value = SAVED_TARGET_GROUP_FILE.read_text(encoding="utf-8", errors="ignore").strip()
    value = value.removeprefix("@").rstrip("/")
    return value


def save_target_group(group: str) -> None:
    normalized = group.strip().removeprefix("@").rstrip("/")
    SAVED_TARGET_GROUP_FILE.write_text(normalized, encoding="utf-8")


def normalize_session_key(session_name: str) -> str:
    return session_name.replace("\\", "/").strip().lower()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: object) -> Optional[datetime]:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None

    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None

    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def save_account_cooldowns(cooldowns: dict[str, dict]) -> None:
    normalized: dict[str, dict] = {}
    for session_name, payload in cooldowns.items():
        if not isinstance(payload, dict):
            continue

        key = normalize_session_key(session_name)
        if not key:
            continue

        until_dt = _parse_datetime(payload.get("until"))
        if not until_dt:
            continue

        reason = str(payload.get("reason", "")).strip()
        record: dict = {"until": until_dt.isoformat(), "reason": reason}
        if isinstance(payload, dict):
            if "flood_incidents" in payload:
                try:
                    record["flood_incidents"] = int(payload["flood_incidents"])
                except (TypeError, ValueError):
                    pass
            last_flood = str(payload.get("last_flood_at", "")).strip()
            if last_flood:
                record["last_flood_at"] = last_flood
        normalized[key] = record

    ACCOUNT_COOLDOWNS_FILE.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_account_cooldowns() -> dict[str, dict]:
    if not ACCOUNT_COOLDOWNS_FILE.exists():
        return {}

    try:
        raw = json.loads(ACCOUNT_COOLDOWNS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

    if not isinstance(raw, dict):
        return {}

    now = _utc_now()
    active: dict[str, dict] = {}
    changed = False

    for session_name, payload in raw.items():
        key = normalize_session_key(str(session_name))
        if not key:
            changed = True
            continue

        reason = ""
        until_dt: Optional[datetime] = None

        if isinstance(payload, dict):
            reason = str(payload.get("reason", "")).strip()
            until_dt = _parse_datetime(payload.get("until"))
        else:
            until_dt = _parse_datetime(payload)

        if not until_dt:
            changed = True
            continue

        if until_dt <= now:
            changed = True
            continue

        record: dict = {"until": until_dt.isoformat(), "reason": reason}
        if isinstance(payload, dict):
            if "flood_incidents" in payload:
                try:
                    record["flood_incidents"] = int(payload["flood_incidents"])
                except (TypeError, ValueError):
                    pass
            last_flood = str(payload.get("last_flood_at", "")).strip()
            if last_flood:
                record["last_flood_at"] = last_flood
        active[key] = record

    if changed or len(active) != len(raw):
        save_account_cooldowns(active)

    return active


def count_invite_block_accounts(cooldowns: Optional[dict[str, dict]] = None) -> int:
    source = cooldowns if cooldowns is not None else load_account_cooldowns()
    count = 0
    for payload in source.values():
        if not isinstance(payload, dict):
            continue
        reason = str(payload.get("reason", "")).strip().lower()
        if reason.startswith("invite-ban"):
            count += 1
    return count


def get_account_cooldown(session_name: str) -> Optional[dict]:
    cooldowns = load_account_cooldowns()
    return cooldowns.get(normalize_session_key(session_name))


def get_cooldown_seconds_left(cooldown: dict) -> int:
    if not isinstance(cooldown, dict):
        return 0

    until_dt = _parse_datetime(cooldown.get("until"))
    if not until_dt:
        return 0

    seconds_left = int((until_dt - _utc_now()).total_seconds())
    return max(0, seconds_left)


def set_account_cooldown(session_name: str, seconds: int, reason: str = "") -> dict:
    key = normalize_session_key(session_name)
    if not key:
        raise ValueError("session_name не может быть пустым")

    cooldown_seconds = max(1, int(seconds))
    until_dt = _utc_now() + timedelta(seconds=cooldown_seconds)
    record: dict = {"until": until_dt.isoformat(), "reason": reason.strip()}

    cooldowns = load_account_cooldowns()
    existing = cooldowns.get(key, {})
    if isinstance(existing, dict):
        if "flood_incidents" in existing:
            record["flood_incidents"] = existing["flood_incidents"]
        if existing.get("last_flood_at"):
            record["last_flood_at"] = existing["last_flood_at"]
    cooldowns[key] = record
    save_account_cooldowns(cooldowns)
    return record


def build_invite_ban_cooldown_seconds() -> int:
    return max(1, INVITE_BAN_COOLDOWN_HOURS * 3600)


def build_floodwait_cooldown_seconds(wait_seconds: int) -> int:
    total = max(1, int(wait_seconds)) + FLOOD_WAIT_EXTRA_SECONDS
    return max(1, total)


def build_spambot_limited_cooldown_seconds() -> int:
    return max(1, SPAMBOT_LIMITED_COOLDOWN_HOURS * 3600)
