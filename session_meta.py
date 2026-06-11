"""Device-фингерпринт аккаунта: app_id / app_hash / device / app_version / lang.

КРИТИЧНО для анти-детекта. Аккаунты с lolz/tdata регистрировались на
официальном Telegram Desktop (app_id=2040). Если подключаться к ним с чужим
api_id и generic-устройством Telethon — Telegram видит "аккаунт угнали/продали"
и мгновенно метит (FloodWait/PeerFlood на первом действии).

Этот модуль достаёт родной фингерпринт из JSON-бандла рядом с сессией и
строит TelegramClient так, чтобы он выглядел как родной клиент аккаунта.

Sidecar: рядом с <session>.session кладётся <session>.meta.json с фингерпринтом.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from telethon import TelegramClient

import config

logger = logging.getLogger("telegram_inviter_bot.session_meta")

# Дефолты под Telegram Desktop (если поля в бандле нет)
_DEFAULT_DEVICE = "Desktop"
_DEFAULT_SYSTEM = "Windows 10"
_DEFAULT_APP_VERSION = "5.13.1 x64"
_DEFAULT_LANG = "en"


def _meta_from_bundle_json(data: dict) -> dict:
    """Извлекает интересующие поля из JSON-бандла lolz/tdata."""
    app_id = None
    raw_app_id = data.get("app_id") or data.get("api_id")
    try:
        if raw_app_id:
            app_id = int(raw_app_id)
    except (ValueError, TypeError):
        app_id = None

    app_hash = str(data.get("app_hash") or data.get("api_hash") or "").strip() or None

    return {
        "app_id": app_id,
        "app_hash": app_hash,
        "device_model": str(data.get("device") or "").strip() or _DEFAULT_DEVICE,
        "system_version": str(data.get("sdk") or "").strip() or _DEFAULT_SYSTEM,
        "app_version": str(data.get("app_version") or "").strip() or _DEFAULT_APP_VERSION,
        "lang_code": str(data.get("lang_code") or "").strip() or _DEFAULT_LANG,
        "system_lang_code": (
            str(data.get("system_lang_code") or data.get("system_lang_pack") or "").strip()
            or _DEFAULT_LANG
        ),
    }


def _session_stem_id(session_path: Path) -> str:
    """225969186_telethon.session -> 225969186 (для матчинга с JSON-бандлом)."""
    stem = session_path.stem  # без .session
    # убираем суффикс _telethon / _pyrogram
    for suffix in ("_telethon", "_pyrogram"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem


def _find_bundle_json(session_path: Path) -> Optional[dict]:
    """Ищет JSON-бандл аккаунта в account_import по совпадению id в имени."""
    stem_id = _session_stem_id(session_path)
    if not stem_id:
        return None

    import_dir = config.ACCOUNT_IMPORT_DIR
    if not import_dir.exists() or not import_dir.is_dir():
        return None

    # Ищем <stem_id>.json в любой подпапке account_import
    for json_path in import_dir.rglob(f"{stem_id}.json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, dict) and (data.get("app_id") or data.get("api_id")):
                return data
        except Exception:
            continue
    return None


def _meta_sidecar_path(session_path: Path) -> Path:
    return session_path.with_suffix(".meta.json")


def load_session_meta(session_path: Path) -> dict:
    """Возвращает фингерпринт для сессии.

    Порядок:
    1. Sidecar <session>.meta.json — если есть, читаем его.
    2. JSON-бандл в account_import — извлекаем, сохраняем sidecar, возвращаем.
    3. Дефолты Desktop (app_id=None → caller возьмёт config.API_ID).
    """
    sidecar = _meta_sidecar_path(session_path)
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    bundle = _find_bundle_json(session_path)
    if bundle:
        meta = _meta_from_bundle_json(bundle)
        try:
            sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(
                "📇 %s: device-фингерпринт сохранён (app_id=%s, device=%s, app=%s)",
                session_path.name, meta.get("app_id"), meta.get("device_model"), meta.get("app_version"),
            )
        except Exception as exc:
            logger.debug("не удалось сохранить meta sidecar: %s", exc)
        return meta

    # Дефолты — хотя бы прикинемся Desktop, а не Telethon/Linux
    return {
        "app_id": None,
        "app_hash": None,
        "device_model": _DEFAULT_DEVICE,
        "system_version": _DEFAULT_SYSTEM,
        "app_version": _DEFAULT_APP_VERSION,
        "lang_code": _DEFAULT_LANG,
        "system_lang_code": _DEFAULT_LANG,
    }


def save_meta_from_bundle(session_path: Path, bundle_json_path: Path) -> bool:
    """Вызывается при импорте: сразу пишет sidecar из конкретного JSON-бандла."""
    try:
        data = json.loads(bundle_json_path.read_text(encoding="utf-8", errors="ignore"))
        if not isinstance(data, dict):
            return False
        meta = _meta_from_bundle_json(data)
        _meta_sidecar_path(session_path).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except Exception as exc:
        logger.debug("save_meta_from_bundle: %s", exc)
        return False


def build_client(session_path, proxy=None) -> TelegramClient:
    """Главная фабрика. Все места проекта должны создавать клиент через неё.

    Использует РОДНОЙ app_id/app_hash аккаунта (обычно 2040 = Telegram Desktop)
    и его device-фингерпринт. Это делает подключение неотличимым от того
    клиента, на котором аккаунт жил, — снимает сигнал "сменили клиент".
    """
    session_path = Path(session_path)
    meta = load_session_meta(session_path)

    api_id = meta.get("app_id") or config.API_ID
    api_hash = meta.get("app_hash") or config.API_HASH

    return TelegramClient(
        str(session_path),
        api_id,
        api_hash,
        proxy=proxy,
        device_model=meta.get("device_model") or _DEFAULT_DEVICE,
        system_version=meta.get("system_version") or _DEFAULT_SYSTEM,
        app_version=meta.get("app_version") or _DEFAULT_APP_VERSION,
        lang_code=meta.get("lang_code") or _DEFAULT_LANG,
        system_lang_code=meta.get("system_lang_code") or _DEFAULT_LANG,
    )
