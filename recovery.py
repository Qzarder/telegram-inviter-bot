"""Recovery: обнаружение и восстановление мёртвых сессий через lolz.market API.

Модуль отвечает за:
1. Сканирование всех сессий в SESSIONS_DIR.
2. Проверку каждой через TelegramClient.is_user_authorized().
3. Сбор item_id для каждой сессии (из item_id.txt рядом).
4. Восстановление через lolz API (запрос кода → sign_in).
5. Bulk-проверку владения, если нужно.

Используется как из main.py (по кнопкам в боте), так и фоновым recovery-таском.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    SessionPasswordNeededError,
    UserDeactivatedBanError,
)

import config
import lolz_reauth

logger = logging.getLogger("telegram_inviter_bot.recovery")

TextReporter = Optional[Callable[[str], Awaitable[None]]]


@dataclass
class SessionStatus:
    session_path: Path
    session_name: str
    country: str
    item_id: Optional[int] = None
    is_authorized: bool = False
    needs_recovery: bool = False
    error: str = ""
    # Поля после попытки восстановления:
    recovery_attempted: bool = False
    recovery_success: bool = False
    recovery_message: str = ""


def _detect_country(session_path: Path) -> str:
    try:
        rel = session_path.relative_to(config.SESSIONS_DIR)
    except ValueError:
        return "default"
    if len(rel.parts) >= 2:
        return rel.parts[0].strip().lower()
    return "default"


def _session_name(session_path: Path) -> str:
    try:
        return str(session_path.relative_to(config.SESSIONS_DIR)).replace("\\", "/")
    except ValueError:
        return session_path.name


_ITEM_ID_FROM_NAME_RE = re.compile(r"^(\d{6,12})(?:_|\.)")


def _read_item_id(session_path: Path) -> Optional[int]:
    """Определяет item_id для сессии.

    Источники (по приоритету):
    1. item_id.txt рядом с сессией (явный override от пользователя).
    2. Извлечение из имени файла — lolz обычно называет сессии типа
       "225969186_telethon.session", где число перед "_" — это item_id.
    """
    stem = session_path.stem  # имя без .session
    candidates = [
        session_path.parent / stem / "item_id.txt",
        session_path.parent / "item_id.txt",
        session_path.with_suffix(".item_id.txt"),
    ]
    for path in candidates:
        if path.exists():
            try:
                raw = path.read_text(encoding="utf-8").strip()
                return int(raw)
            except (ValueError, OSError):
                continue

    # Fallback: извлечь из имени файла
    match = _ITEM_ID_FROM_NAME_RE.match(stem)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return None


def _save_item_id(session_path: Path, item_id: int) -> None:
    """Сохраняет item_id рядом с сессией для будущего использования (ускоряет recovery)."""
    target = session_path.with_suffix(".item_id.txt")
    try:
        target.write_text(str(item_id), encoding="utf-8")
    except OSError as exc:
        logger.warning("Не удалось сохранить item_id для %s: %s", session_path.name, exc)


async def _safe_report(reporter: TextReporter, text: str) -> None:
    if not reporter:
        return
    try:
        await reporter(text)
    except Exception as exc:
        logger.debug("reporter failed: %s", exc)


async def _check_session(session_path: Path) -> SessionStatus:
    """Проверяет одну сессию. Возвращает SessionStatus с заполненными полями.

    НЕ восстанавливает — только диагностика.

    Идёт БЕЗ прокси — нам важно знать реальное состояние сессии
    (а не "прокси сломан"). После recovery сессия будет работать через прокси
    в обычном пайплайне warmer/inviter.
    """
    country = _detect_country(session_path)
    status = SessionStatus(
        session_path=session_path,
        session_name=_session_name(session_path),
        country=country,
        item_id=_read_item_id(session_path),
    )

    client: Optional[TelegramClient] = None
    try:
        client = TelegramClient(
            str(session_path),
            config.API_ID,
            config.API_HASH,
            # БЕЗ прокси — проверка реального статуса auth_key, а не сети
        )
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await client.is_user_authorized():
            status.is_authorized = False
            status.needs_recovery = True
            status.error = "not authorized"
        else:
            status.is_authorized = True
            status.needs_recovery = False
    except AuthKeyUnregisteredError:
        status.is_authorized = False
        status.needs_recovery = True
        status.error = "AuthKeyUnregistered"
    except UserDeactivatedBanError:
        status.is_authorized = False
        status.needs_recovery = False  # бан, восстановление не поможет
        status.error = "account banned"
    except Exception as exc:
        status.error = f"connect: {exc}"
        status.needs_recovery = True
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    return status


async def scan_all_sessions(
    *,
    reporter: TextReporter = None,
    concurrency: int = 5,
) -> List[SessionStatus]:
    """Сканирует все session-файлы. Возвращает список статусов.

    Параллельно проверяет до `concurrency` сессий одновременно.
    """
    from inviter import get_session_files  # ленивый импорт чтобы избежать циклов

    session_files = get_session_files()
    if not session_files:
        await _safe_report(reporter, "Нет session-файлов для проверки.")
        return []

    await _safe_report(reporter, f"🔍 Сканирую {len(session_files)} сессий…")

    semaphore = asyncio.Semaphore(concurrency)
    results: List[SessionStatus] = []
    completed = 0
    total = len(session_files)
    lock = asyncio.Lock()

    async def worker(path: Path) -> None:
        nonlocal completed
        async with semaphore:
            status = await _check_session(path)
            async with lock:
                results.append(status)
                completed += 1
                # Прогресс каждые 10 или на конце
                if completed % 10 == 0 or completed == total:
                    await _safe_report(
                        reporter,
                        f"…проверено {completed}/{total}",
                    )

    tasks = [asyncio.create_task(worker(p)) for p in session_files]
    await asyncio.gather(*tasks, return_exceptions=True)

    results.sort(key=lambda s: s.session_name)
    return results


async def _recover_one(
    status: SessionStatus,
    *,
    api: lolz_reauth.LolzMarketAPI,
    reporter: TextReporter = None,
) -> None:
    """Восстанавливает одну сессию через lolz API. Обновляет поля status in-place.

    Алгоритм:
    1. Получаем инфо о лоте через GET /{item_id} → phone и состояние lolz-сессии.
    2. Если lolz-сессия валидна — есть смысл идти дальше.
    3. Создаём чистый Telethon-клиент, вызываем send_code_request(phone)
       → Telegram отправляет SMS на телефон, который держит lolz.
    4. Через несколько секунд читаем код из lolz API: GET /{item_id}/telegram-login-code.
       Берём самый свежий код (по timestamp), который пришёл ПОСЛЕ нашего send_code_request.
    5. Вызываем client.sign_in(phone, code, phone_code_hash).
    """
    status.recovery_attempted = True

    if status.item_id is None:
        status.recovery_success = False
        status.recovery_message = "не найден item_id"
        return

    if not config.LOLZ_API_TOKEN:
        status.recovery_success = False
        status.recovery_message = "LOLZ_API_TOKEN не задан"
        return

    # 1. Получаем инфо о лоте: phone + проверяем что сессия на стороне lolz валидна
    info_ok, info_msg, info_data = await api.get_item(status.item_id)
    if not info_ok:
        status.recovery_success = False
        status.recovery_message = f"get item: {info_msg}"
        return

    item = info_data.get("item", {}) if isinstance(info_data, dict) else {}
    phone_raw = item.get("telegram_phone") or item.get("telegram_formatted_phone", "")
    if not phone_raw:
        status.recovery_success = False
        status.recovery_message = "у лота нет telegram_phone"
        return

    phone = str(phone_raw).strip().replace(" ", "").replace("(", "").replace(")", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone

    # 2. ВАЖНО: recovery идёт БЕЗ прокси — прямое подключение к Telegram.
    # Причины:
    #   - прокси могут быть дохлыми (как раз сейчас они дохлые и есть)
    #   - для самого sign_in IP не важен; Telegram не банит за вход с любого IP
    #   - после recovery нормальные операции (warmer/inviter) пойдут через прокси,
    #     это нормально — auth_key переносим между IP

    # Удаляем старый session-файл и его journal/wal — auth_key битый,
    # нужен чистый старт. Старая сессия идёт в .session.bak на случай отката.
    for suffix in ("", "-journal", "-wal", "-shm"):
        old = Path(str(status.session_path) + suffix)
        if old.exists():
            try:
                bak = Path(str(status.session_path) + suffix + ".bak")
                if bak.exists():
                    bak.unlink()
                old.rename(bak)
            except Exception:
                try:
                    old.unlink()
                except Exception:
                    pass

    client: Optional[TelegramClient] = None
    try:
        client = TelegramClient(
            str(status.session_path),
            config.API_ID,
            config.API_HASH,
            # БЕЗ прокси — recovery работает по прямому соединению
        )
        await asyncio.wait_for(client.connect(), timeout=30)

        # 3. Просим Telegram прислать код на этот номер
        try:
            sent = await client.send_code_request(phone)
        except Exception as exc:
            status.recovery_success = False
            status.recovery_message = f"send_code_request: {_short(exc)}"
            return

        # 4. Ждём ~12 сек и читаем код из lolz API.
        # lolz получает SMS на своей стороне (продавец сдал нам номер для гарантии).
        # Берём самый свежий код, пришедший за последнюю минуту.
        await asyncio.sleep(12)

        code = await _fetch_fresh_code(api, status.item_id, after_unix=_now_unix() - 60)
        if not code:
            # Подождём ещё немного
            await _safe_report(reporter, f"⏳ {status.session_name}: жду код ещё 15 сек…")
            await asyncio.sleep(15)
            code = await _fetch_fresh_code(api, status.item_id, after_unix=_now_unix() - 90)

        if not code:
            status.recovery_success = False
            status.recovery_message = "свежий код не пришёл (SMS не получено)"
            return

        await _safe_report(reporter, f"📱 {status.session_name}: код {code[:2]}**")

        # 5. Sign-in
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
            status.recovery_success = True
            status.recovery_message = "успешно восстановлен"
            if status.item_id is not None:
                _save_item_id(status.session_path, status.item_id)
        except SessionPasswordNeededError:
            status.recovery_success = False
            status.recovery_message = "требуется 2FA пароль"
        except Exception as exc:
            err_text = str(exc).lower()
            if "code is invalid" in err_text or "phone_code_invalid" in err_text:
                status.recovery_message = "код невалидный"
            elif "code is expired" in err_text or "phone_code_expired" in err_text:
                status.recovery_message = "код просрочен (запросим заново)"
            else:
                status.recovery_message = f"sign_in: {_short(exc)}"
            status.recovery_success = False
    except Exception as exc:
        # Логируем полный traceback для диагностики
        logger.exception("Recovery failed for %s", status.session_name)
        status.recovery_success = False
        status.recovery_message = f"connect: {_short(exc)}"
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


def _short(exc: Exception, limit: int = 120) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _now_unix() -> int:
    import time
    return int(time.time())


async def _fetch_fresh_code(
    api: lolz_reauth.LolzMarketAPI,
    item_id: int,
    *,
    after_unix: int,
) -> Optional[str]:
    """Получает самый свежий код, пришедший после `after_unix`."""
    ok, _msg, raw = await api.get_login_code(item_id)
    if not ok:
        return None

    # raw содержит либо {"codes": {"code": "...", "date": ...}}, либо
    # {"codes": [{"code": "...", "date": ...}, ...]}
    codes_field = raw.get("codes") if isinstance(raw, dict) else None
    candidates: list[tuple[int, str]] = []
    if isinstance(codes_field, dict):
        code = codes_field.get("code")
        date = codes_field.get("date", 0)
        if code:
            candidates.append((int(date or 0), str(code)))
    elif isinstance(codes_field, list):
        for entry in codes_field:
            if not isinstance(entry, dict):
                continue
            code = entry.get("code")
            date = entry.get("date", 0)
            if code:
                candidates.append((int(date or 0), str(code)))

    if not candidates:
        return None

    # Берём самый свежий
    candidates.sort(key=lambda t: t[0], reverse=True)
    newest_date, newest_code = candidates[0]
    if newest_date < after_unix:
        return None  # код старый, SMS ещё не пришёл
    return newest_code


async def recover_sessions(
    statuses: List[SessionStatus],
    *,
    reporter: TextReporter = None,
    concurrency: int = 1,
) -> List[SessionStatus]:
    # ВАЖНО: lolz API лимит = 20 req/min (3 сек между запросами).
    # Параллельные запросы всё равно сериализуются через rate_limit lock,
    # поэтому concurrency=1 не теряет скорость, но логи получаются последовательнее.
    """Прогоняет восстановление по всем сессиям из списка.

    Только те, у которых needs_recovery=True, будут обработаны.
    """
    candidates = [s for s in statuses if s.needs_recovery]
    if not candidates:
        await _safe_report(reporter, "Нет сессий для восстановления.")
        return statuses

    if not config.LOLZ_API_TOKEN:
        await _safe_report(
            reporter,
            "⚠️ LOLZ_API_TOKEN не задан. Восстановление невозможно.\nДобавь токен в .env и перезапусти бота.",
        )
        return statuses

    await _safe_report(
        reporter,
        f"🔧 Запускаю восстановление {len(candidates)} сессий…",
    )

    api = lolz_reauth.LolzMarketAPI(config.LOLZ_API_TOKEN)
    semaphore = asyncio.Semaphore(concurrency)

    async def worker(status: SessionStatus) -> None:
        async with semaphore:
            await _recover_one(status, api=api, reporter=reporter)
            emoji = "✅" if status.recovery_success else "❌"
            await _safe_report(
                reporter,
                f"{emoji} {status.session_name}: {status.recovery_message}",
            )

    try:
        await asyncio.gather(
            *[asyncio.create_task(worker(s)) for s in candidates],
            return_exceptions=True,
        )
    finally:
        await api.close()

    success = sum(1 for s in candidates if s.recovery_success)
    failed = len(candidates) - success
    await _safe_report(
        reporter,
        f"\n📊 Итог восстановления: ✅ {success}  ❌ {failed}",
    )
    return statuses


def summarize_scan(statuses: List[SessionStatus]) -> str:
    """Текстовая сводка после сканирования (для бота)."""
    total = len(statuses)
    ok = sum(1 for s in statuses if s.is_authorized)
    dead = [s for s in statuses if s.needs_recovery]
    no_item_id = sum(1 for s in dead if s.item_id is None)
    has_item_id = len(dead) - no_item_id

    lines = [
        f"<b>📋 Результат сканирования</b>",
        f"Всего сессий: <b>{total}</b>",
        f"✅ Рабочих: <b>{ok}</b>",
        f"❌ Требуют восстановления: <b>{len(dead)}</b>",
    ]
    if dead:
        lines.append(f"   ├─ с item_id (можно восстановить): <b>{has_item_id}</b>")
        lines.append(f"   └─ без item_id (ручной режим): <b>{no_item_id}</b>")

    return "\n".join(lines)


async def cleanup_dead_sessions(
    statuses: List[SessionStatus],
    *,
    reporter: TextReporter = None,
) -> dict:
    """Перемещает безнадёжно мёртвые сессии в sessions_archive/.

    Безнадёжно мёртвая = recovery_attempted и recovery_success=False
    и сообщение явно говорит что lolz сессия недействительна или 2FA.

    Возвращает {"moved": [list], "skipped": [list]}.
    """
    archive_dir = config.SESSIONS_DIR.parent / "sessions_archive" / "dead_lolz"
    archive_dir.mkdir(parents=True, exist_ok=True)

    moved: list[dict] = []
    skipped: list[str] = []

    for status in statuses:
        # Считаем безнадёжной только если попытка восстановления была И провалилась
        # с признаком что аккаунт не починить (а не временная ошибка сети).
        if not status.recovery_attempted:
            continue
        if status.recovery_success:
            continue

        msg = (status.recovery_message or "").lower()
        permanent_markers = (
            "недействительна",
            "logout",
            "2fa",
            "banned",
            "deactivated",
            "phone_number_banned",
            "phone_number_invalid",
            "нет доступа",
        )
        if not any(m in msg for m in permanent_markers):
            skipped.append(f"{status.session_name}: пропущен ({status.recovery_message})")
            continue

        # Перемещаем все файлы сессии: .session, -journal, -wal, -shm, .item_id.txt, .bak
        src_base = status.session_path
        country = status.country
        dst_country_dir = archive_dir / country
        dst_country_dir.mkdir(parents=True, exist_ok=True)

        moved_files = []
        for suffix in ("", "-journal", "-wal", "-shm", ".bak"):
            src = Path(str(src_base) + suffix)
            if not src.exists():
                continue
            dst = dst_country_dir / src.name
            try:
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
                moved_files.append(src.name)
            except Exception as exc:
                logger.warning("Не удалось переместить %s: %s", src, exc)

        # Также .item_id.txt
        item_id_src = src_base.with_suffix(".item_id.txt")
        if item_id_src.exists():
            try:
                item_id_dst = dst_country_dir / item_id_src.name
                if item_id_dst.exists():
                    item_id_dst.unlink()
                item_id_src.rename(item_id_dst)
            except Exception:
                pass

        moved.append({
            "session_name": status.session_name,
            "item_id": status.item_id,
            "reason": status.recovery_message,
            "country": country,
            "files": moved_files,
        })
        await _safe_report(reporter, f"🗑 {status.session_name} → archive ({status.recovery_message})")

    # Сохраняем сводку и список item_id для claims
    if moved:
        from datetime import datetime
        report_path = archive_dir / f"dead_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        try:
            lines = ["# Безнадёжно мёртвые аккаунты", ""]
            lines.append(f"# Дата: {datetime.now().isoformat()}")
            lines.append(f"# Всего: {len(moved)}")
            lines.append("")
            lines.append("## Item ID для открытия жалоб на lolz.market")
            lines.append("")
            for m in moved:
                if m["item_id"]:
                    lines.append(f"https://lzt.market/{m['item_id']}  # {m['session_name']} — {m['reason']}")
            lines.append("")
            lines.append("## Детали")
            for m in moved:
                lines.append(f"- {m['session_name']} (item_id={m['item_id']}, страна={m['country']})")
                lines.append(f"  причина: {m['reason']}")
                lines.append(f"  файлы: {', '.join(m['files'])}")
            report_path.write_text("\n".join(lines), encoding="utf-8")
            await _safe_report(reporter, f"📝 Отчёт о мёртвых: <code>{report_path}</code>")
        except Exception as exc:
            logger.warning("Не удалось сохранить отчёт: %s", exc)

    return {"moved": moved, "skipped": skipped}


def format_dead_list(statuses: List[SessionStatus], limit: int = 30) -> str:
    """Списком вывести мёртвые сессии (для бота)."""
    dead = [s for s in statuses if s.needs_recovery]
    if not dead:
        return "Нет мёртвых сессий."

    lines = [f"<b>❌ Мёртвые сессии ({len(dead)}):</b>"]
    for s in dead[:limit]:
        item_tag = f"id:{s.item_id}" if s.item_id else "<i>no item_id</i>"
        lines.append(f"• <code>{s.session_name}</code> · {item_tag}")
    if len(dead) > limit:
        lines.append(f"…и ещё {len(dead) - limit}")
    return "\n".join(lines)
