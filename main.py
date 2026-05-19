import asyncio
import hashlib
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import inviter
import recovery
import session_checker
import session_recover
import warmer

from logging.handlers import RotatingFileHandler

_log_formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

# Логи дублируются в файл /app/bot.log (последние 5 MB, 3 ротации).
# Это нужно для команды /logs в боте — Telegram-чат показывает последние строки.
try:
    _file_handler = RotatingFileHandler(
        "/app/bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    _file_handler.setFormatter(_log_formatter)
    _file_handler.setLevel(logging.INFO)
except Exception:
    _file_handler = None

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_formatter)
_stream_handler.setLevel(logging.INFO)

_root_handlers = [_stream_handler]
if _file_handler is not None:
    _root_handlers.append(_file_handler)

logging.basicConfig(level=logging.INFO, handlers=_root_handlers)

logger = logging.getLogger("telegram_inviter_bot")
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("telethon.network").setLevel(logging.WARNING)
logging.getLogger("telegram_inviter_bot.session_recover").setLevel(logging.WARNING)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
active_tasks: set[asyncio.Task] = set()
waiting_for_target_users: set[int] = set()
account_check_task: Optional[asyncio.Task] = None
session_recover_task: Optional[asyncio.Task] = None
recover_lock = asyncio.Lock()
auto_import_last_signature = ""

# === Recovery (lolz auto-reauth) state ===
session_recovery_task: Optional[asyncio.Task] = None
last_recovery_scan: list[recovery.SessionStatus] = []

PUBLIC_GROUP_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
WARMER_PAGE_SIZE = 20


@dataclass
class InviteJobState:
    task: Optional[asyncio.Task]
    stop_event: asyncio.Event
    chat_id: int
    user_id: int
    target_group: str
    total: int
    processed: int = 0
    added: int = 0
    skipped: int = 0
    failed: int = 0
    started_at: datetime = field(default_factory=datetime.now)


current_job: Optional[InviteJobState] = None

def build_main_panel() -> tuple[str, InlineKeyboardMarkup]:
    """Динамическая главная панель — статус + кнопки зависят от текущего состояния."""
    sessions_count = len(inviter.get_session_files())
    pending_count = len(build_pending_targets())
    cooldowns = config.load_account_cooldowns()
    invite_block_count = config.count_invite_block_accounts(cooldowns)
    saved_group = get_saved_group()
    warming_rows = warmer.get_warming_status_rows()
    in_work_count = sum(1 for row in warming_rows if row.get("in_work"))

    lines: list[str] = ["<b>🤖 Inviter · Панель управления</b>", ""]

    if is_job_running() and current_job:
        elapsed = format_elapsed(current_job.started_at)
        pct = int(current_job.processed / current_job.total * 100) if current_job.total else 0
        bar_filled = int(pct / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        lines += [
            f"🟢 <b>Задача выполняется</b>  ·  {elapsed}",
            f"🎯 Группа: <code>@{current_job.target_group}</code>",
            f"[{bar}] {pct}%  ({current_job.processed}/{current_job.total})",
            f"   ✅ {current_job.added}   ⏭ {current_job.skipped}   ❌ {current_job.failed}",
        ]
    elif is_check_running():
        lines += ["🔍 <b>Проверка аккаунтов…</b>"]
    elif is_recover_running():
        lines += ["📥 <b>Импорт сессий…</b>"]
    else:
        group_str = f"<code>@{saved_group}</code>" if saved_group else "<i>не задана</i>"
        lines += [
            "⚪ <b>Ожидание</b>",
            f"🎯 Группа: {group_str}",
        ]

    lines += [
        "",
        f"👥 Аккаунтов: <b>{sessions_count}</b>   🔥 В работе: <b>{in_work_count}</b>",
        f"📋 Осталось: <b>{pending_count}</b>   🚫 Бан: <b>{invite_block_count}</b>",
    ]

    text = "\n".join(lines)

    kb: list[list[InlineKeyboardButton]] = []

    if is_job_running():
        kb.append([InlineKeyboardButton(text="⏹  Остановить задачу", callback_data="stop_invite")])
        kb.append([InlineKeyboardButton(text="🔄  Обновить статус", callback_data="refresh_panel")])
    else:
        kb.append([InlineKeyboardButton(text="▶️  Запустить инвайт", callback_data="start_invite")])
        kb.append([
            InlineKeyboardButton(text="✅  Аккаунты", callback_data="check_accounts"),
            InlineKeyboardButton(text="📥  Импорт", callback_data="recover_sessions"),
        ])
        group_btn_label = (f"🎯  @{saved_group}") if saved_group else "🎯  Задать группу"
        kb.append([
            InlineKeyboardButton(text=group_btn_label, callback_data="change_group"),
            InlineKeyboardButton(text="🔄  Обновить", callback_data="refresh_panel"),
        ])

    kb.append([
        InlineKeyboardButton(text="🔥  Прогрев", callback_data="warm_open"),
        InlineKeyboardButton(text="📊  Статистика", callback_data="stats"),
    ])

    if not is_job_running():
        kb.append([
            InlineKeyboardButton(text="🔧  Восстановить сессии", callback_data="recover_open"),
        ])

    return text, InlineKeyboardMarkup(inline_keyboard=kb)


async def send_main_panel(chat_id: int) -> None:
    text, markup = build_main_panel()
    await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


def normalize_target_group(raw_value: str) -> str:
    value = raw_value.strip()

    if value.startswith("https://"):
        value = value.removeprefix("https://")
    elif value.startswith("http://"):
        value = value.removeprefix("http://")

    if value.startswith("t.me/"):
        value = value.removeprefix("t.me/")

    if value.startswith("@"):
        value = value.removeprefix("@")

    return value.strip().rstrip("/")


def load_targets() -> list[str]:
    if not config.TARGETS_FILE.exists():
        return []

    users = []
    for line in config.TARGETS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        user = line.strip().removeprefix("@")
        if user:
            users.append(user)
    return users


def build_pending_targets() -> list[str]:
    processed = config.load_processed_usernames()
    seen: set[str] = set()
    pending: list[str] = []

    for user in load_targets():
        key = user.lower()
        if key in seen:
            continue
        seen.add(key)

        if key not in processed:
            pending.append(user)

    return pending


def is_job_running() -> bool:
    return bool(current_job and current_job.task and not current_job.task.done())


def is_check_running() -> bool:
    return bool(account_check_task and not account_check_task.done())


def is_recover_running() -> bool:
    return recover_lock.locked() or bool(session_recover_task and not session_recover_task.done())


def is_session_recovery_running() -> bool:
    return bool(session_recovery_task and not session_recovery_task.done())


def is_system_busy() -> bool:
    return (
        is_job_running()
        or is_check_running()
        or is_recover_running()
        or is_session_recovery_running()
    )


def build_import_signature(source_dir: Path) -> str:
    if not source_dir.exists() or not source_dir.is_dir():
        return ""

    digest = hashlib.sha1()
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(source_dir).as_posix().lower()
            stat = path.stat()
        except OSError:
            continue

        digest.update(rel.encode("utf-8", errors="ignore"))
        digest.update(b"|")
        digest.update(str(stat.st_size).encode("ascii", errors="ignore"))
        digest.update(b"|")
        digest.update(str(stat.st_mtime_ns).encode("ascii", errors="ignore"))
        digest.update(b"\n")
    return digest.hexdigest()


async def sleep_or_stop(stop_event: asyncio.Event, seconds: int) -> bool:
    timeout = max(1, int(seconds))
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def run_auto_import_sync(stop_event: asyncio.Event) -> None:
    global auto_import_last_signature

    source_dir = config.ACCOUNT_IMPORT_DIR
    interval = max(10, int(config.AUTO_IMPORT_SYNC_INTERVAL))

    while not stop_event.is_set():
        try:
            if (
                is_job_running()
                or is_check_running()
                or recover_lock.locked()
                or not source_dir.exists()
                or not source_dir.is_dir()
            ):
                pass
            else:
                signature = build_import_signature(source_dir)
                if signature and signature != auto_import_last_signature:
                    bundles = session_recover.iter_leaf_bundles(source_dir)
                    if not bundles:
                        auto_import_last_signature = signature
                    else:
                        if config.WARMER_ENABLED and warmer.is_busy_now():
                            idle = await warmer.wait_until_idle(timeout_seconds=120)
                            if not idle:
                                stopped = await sleep_or_stop(stop_event, min(30, interval))
                                if stopped:
                                    break
                                continue

                        if is_job_running() or is_check_running() or recover_lock.locked():
                            stopped = await sleep_or_stop(stop_event, min(30, interval))
                            if stopped:
                                break
                            continue

                        logger.info(
                            "Автоимпорт: найдено %s новых/обновленных папок аккаунтов. Запускаю синхронизацию.",
                            len(bundles),
                        )
                        async with recover_lock:
                            exit_code = await session_recover.run(
                                source=source_dir,
                                use_proxy=False,
                                force=False,
                                verbose=True,
                                preserve_existing=True,
                            )

                        if exit_code in (0, 2):
                            auto_import_last_signature = signature
                            if exit_code == 0:
                                logger.info("Автоимпорт: синхронизация завершена без ошибок.")
                            else:
                                logger.warning("Автоимпорт: синхронизация завершена частично.")
                        else:
                            logger.warning("Автоимпорт: синхронизация завершилась с ошибкой.")
        except Exception:
            logger.exception("Ошибка фоновой синхронизации account_import")

        stopped = await sleep_or_stop(stop_event, interval)
        if stopped:
            break


def format_elapsed(started_at: datetime) -> str:
    total = int((datetime.now() - started_at).total_seconds())
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours}ч {minutes}м {seconds}с"
    return f"{minutes}м {seconds}с"


def get_callback_chat_id(callback: types.CallbackQuery) -> Optional[int]:
    if callback.message:
        return callback.message.chat.id
    if callback.from_user:
        return callback.from_user.id
    return None


def get_saved_group() -> str:
    saved = normalize_target_group(config.load_saved_target_group())
    if saved and PUBLIC_GROUP_RE.fullmatch(saved):
        return saved
    return ""


def _parse_non_negative_int(raw: str, default: int = 0) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _short_session_label(session_name: str, limit: int = 22) -> str:
    value = session_name.strip()
    if len(value) <= limit:
        return value
    return "..." + value[-(limit - 3) :]


def _build_warming_status_label(row: dict) -> str:
    if row.get("pre_warmed"):
        cnt = row.get("real_channels_count", 0)
        return f"⚡ Пред-прогрет ({cnt} каналов) → инвайт"
    if row.get("auto_in_work"):
        return "✅ Уже в работе"
    if row.get("manual_enabled"):
        return "▶ В работе (вручную)"
    if row.get("can_manual_start"):
        return f"🟡 Можно пустить в работу (от {config.WARMER_MANUAL_START_PERCENT}%)"
    return "⏳ Прогревается"


def build_warming_view(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    rows = warmer.get_warming_status_rows()
    total = len(rows)
    in_work_count = sum(1 for row in rows if row.get("in_work"))
    auto_count = sum(1 for row in rows if row.get("auto_in_work"))
    manual_count = sum(1 for row in rows if row.get("manual_enabled") and not row.get("auto_in_work"))
    can_manual_count = sum(1 for row in rows if row.get("can_manual_start"))

    if total == 0:
        text = (
            "🔥 Прогрев аккаунтов\n\n"
            "Session-файлы не найдены.\n"
            f"Положи .session в папку: {config.SESSIONS_DIR}"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="warm_page:0")],
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="warm_back")],
            ]
        )
        return text, keyboard

    total_pages = max(1, (total + WARMER_PAGE_SIZE - 1) // WARMER_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    start = page * WARMER_PAGE_SIZE
    end = min(start + WARMER_PAGE_SIZE, total)
    page_rows = rows[start:end]

    lines = [
        "🔥 Прогрев аккаунтов",
        f"Порог ручного запуска: {config.WARMER_MANUAL_START_PERCENT}%",
        "",
        f"В работе: {in_work_count}/{total} (авто: {auto_count}, вручную: {manual_count})",
        f"Можно запустить вручную сейчас: {can_manual_count}",
        f"Страница: {page + 1}/{total_pages}",
        "",
    ]

    for offset, row in enumerate(page_rows, start=1):
        idx = start + offset
        session_name = str(row.get("session_name", "")).strip()
        progress = int(row.get("progress_percent", 0))
        status_label = _build_warming_status_label(row)
        lines.append(f"{idx}. {session_name}")
        lines.append(f"   Прогрев: {progress}% | {status_label}")
        last_error = str(row.get("last_error", "")).strip()
        if last_error:
            if len(last_error) > 120:
                last_error = last_error[:117] + "..."
            lines.append(f"   ⚠️ {last_error}")
        lines.append("")

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for local_idx, row in enumerate(page_rows):
        if not row.get("can_manual_start"):
            continue
        global_index = start + local_idx
        session_name = str(row.get("session_name", "")).strip()
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"▶ Пустить: {_short_session_label(session_name)}",
                    callback_data=f"warm_run:{global_index}:{page}",
                )
            ]
        )

    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"warm_page:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(text="🔄 Обновить", callback_data=f"warm_page:{page}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"warm_page:{page + 1}"))
    keyboard_rows.append(nav_buttons)
    if can_manual_count > 1:
        keyboard_rows.append(
            [InlineKeyboardButton(text=f"🔥 Пустить всех ({can_manual_count})", callback_data="warm_start_all")]
        )
    keyboard_rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="warm_back")])

    return "\n".join(lines).strip(), InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


async def safe_edit_callback_message(
    callback: types.CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception as exc:
        logger.warning("Не удалось обновить сообщение раздела прогрева: %s", exc)
        await callback.message.answer(text, reply_markup=markup)


def _on_task_done(task: asyncio.Task) -> None:
    global current_job

    active_tasks.discard(task)
    if current_job and current_job.task is task:
        current_job = None

    try:
        task.result()
    except Exception:
        logger.exception("Фоновая задача завершилась с ошибкой")


async def start_invite_job(chat_id: int, user_id: int, target_group: str) -> None:
    global current_job

    if is_check_running():
        await bot.send_message(chat_id, "Сейчас идет проверка аккаунтов. Дождись завершения.")
        return

    if is_recover_running():
        await bot.send_message(chat_id, "Сейчас идет восстановление сессий. Дождись завершения.")
        return

    if is_job_running():
        await bot.send_message(chat_id, "Сейчас уже есть активная задача.")
        return

    if config.WARMER_ENABLED and warmer.is_busy_now():
        await bot.send_message(chat_id, "Жду, пока прогрев освободит session-файлы...")
        idle = await warmer.wait_until_idle(timeout_seconds=120)
        if not idle:
            await bot.send_message(
                chat_id,
                "Прогрев все еще занят. Попробуй запустить инвайт через минуту.",
            )
            return

    users = build_pending_targets()
    if not users:
        await bot.send_message(
            chat_id,
            "Новых username для обработки нет.\n"
            "Либо targets.txt пустой, либо все уже есть в processed_usernames.txt.",
        )
        return

    if not config.SESSIONS_DIR.exists():
        await bot.send_message(chat_id, "Папка sessions не найдена.")
        return

    stop_event = asyncio.Event()
    job = InviteJobState(
        task=None,
        stop_event=stop_event,
        chat_id=chat_id,
        user_id=user_id,
        target_group=target_group,
        total=len(users),
    )
    current_job = job

    async def report_text(text: str) -> None:
        try:
            await bot.send_message(job.chat_id, text)
        except Exception as exc:
            logger.warning("Не удалось отправить отчет в Telegram: %s", exc)

    async def report_user(username: str, status: str, detail: str) -> None:
        safe_username = username.removeprefix("@")

        job.processed += 1
        if status == "added":
            job.added += 1
            prefix = "Добавлен"
        elif status == "skipped":
            job.skipped += 1
            prefix = "Пропуск"
        else:
            job.failed += 1
            prefix = "Ошибка"

        await report_text(f"{prefix}: @{safe_username} - {detail}")

    await bot.send_message(
        chat_id,
        f"▶️ Инвайт запущен\n🎯 Группа: @{target_group}\n📋 Username в очереди: {len(users)}",
    )

    async def run_job() -> None:
        try:
            await inviter.run_invite_task(
                target_group=target_group,
                users_list=users,
                stop_event=stop_event,
                report_user=report_user,
                report_text=report_text,
            )
        except Exception as exc:
            logger.exception("Ошибка выполнения задачи инвайта")
            await report_text(f"Ошибка выполнения задачи инвайта: {exc}")
        finally:
            try:
                await send_main_panel(job.chat_id)
            except Exception as exc:
                logger.warning("Не удалось отправить панель после завершения задачи: %s", exc)

    task = asyncio.create_task(run_job())
    job.task = task
    task.add_done_callback(_on_task_done)
    active_tasks.add(task)


@dp.message(Command("start"))
async def start(message: types.Message) -> None:
    if message.chat.id:
        await send_main_panel(message.chat.id)


@dp.message(Command("logs"))
async def cmd_logs(message: types.Message) -> None:
    """Возвращает последние ошибки/WARNING из логов бота прямо в Telegram."""
    import subprocess
    if not message.chat.id:
        return
    try:
        # Читаем последние 2000 строк docker логов (если контейнер достижим)
        # либо стандартный stderr через journalctl — fallback на текущий буфер.
        # В Docker логи доступны только снаружи; внутри контейнера python
        # пишет в stdout. Достаём через /proc/1/fd/1 (последнее не работает),
        # поэтому используем хранилище в файле.
        log_path = Path("/app/bot.log")
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            # Берём последние 80 строк
            lines = text.splitlines()[-80:]
            content = "\n".join(lines)
        else:
            content = "Лог-файл /app/bot.log не найден. Используется только stdout — смотри docker logs через SSH."

        # Telegram message limit 4096 chars
        if len(content) > 3800:
            content = content[-3800:]
            content = "…(обрезано)\n" + content
        if not content.strip():
            content = "Логи пустые."
        await message.answer(f"<pre>{content}</pre>", parse_mode="HTML")
    except Exception as exc:
        await message.answer(f"Ошибка чтения логов: {exc}")


@dp.message(Command("errors"))
async def cmd_errors(message: types.Message) -> None:
    """Только ошибки и WARNING из логов."""
    if not message.chat.id:
        return
    try:
        log_path = Path("/app/bot.log")
        if not log_path.exists():
            await message.answer("Лог-файл отсутствует. См. /logs.")
            return
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        error_lines = [
            ln for ln in text.splitlines()
            if "ERROR" in ln or "WARNING" in ln or "❌" in ln or "Critical" in ln
        ]
        if not error_lines:
            await message.answer("Ошибок не найдено в логах.")
            return
        recent = "\n".join(error_lines[-50:])
        if len(recent) > 3800:
            recent = recent[-3800:]
            recent = "…(обрезано)\n" + recent
        await message.answer(f"<pre>{recent}</pre>", parse_mode="HTML")
    except Exception as exc:
        await message.answer(f"Ошибка: {exc}")


@dp.callback_query(F.data == "check_accounts")
async def check_accounts(callback: types.CallbackQuery) -> None:
    global account_check_task

    await callback.answer()

    if is_job_running():
        await callback.message.answer("Сейчас идет инвайт. Сначала останови его или дождись завершения.")
        return

    if is_check_running():
        await callback.message.answer("Проверка уже запущена. Подожди, отчет придет в этот чат.")
        return

    if is_recover_running():
        await callback.message.answer("Сейчас идет восстановление сессий. Дождись завершения.")
        return

    if config.WARMER_ENABLED and warmer.is_busy_now():
        await callback.message.answer("Жду, пока прогрев освободит session-файлы...")
        idle = await warmer.wait_until_idle(timeout_seconds=120)
        if not idle:
            await callback.message.answer(
                "Прогрев все еще занят. Повтори проверку через минуту.",
            )
            return

    chat_id = get_callback_chat_id(callback)
    if chat_id is None:
        return

    async def report_text(text: str) -> None:
        try:
            await bot.send_message(chat_id, text)
        except Exception as exc:
            logger.warning("Не удалось отправить отчет проверки: %s", exc)

    async def run_check() -> None:
        global account_check_task
        try:
            await inviter.validate_sessions(report_text=report_text)
        except Exception as exc:
            logger.exception("Проверка аккаунтов завершилась с ошибкой")
            await report_text(f"Ошибка проверки аккаунтов: {exc}")
        finally:
            account_check_task = None
            try:
                await send_main_panel(chat_id)
            except Exception as exc:
                logger.warning("Не удалось отправить панель после проверки аккаунтов: %s", exc)

    account_check_task = asyncio.create_task(run_check())
    if callback.message:
        text, markup = build_main_panel()
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await callback.message.answer("Проверка запущена. Отчёт появится в этом чате.")


@dp.callback_query(F.data == "recover_sessions")
async def recover_sessions(callback: types.CallbackQuery) -> None:
    global session_recover_task

    await callback.answer()

    if is_job_running():
        await callback.message.answer("Сейчас идет инвайт. Сначала останови его или дождись завершения.")
        return

    if is_check_running():
        await callback.message.answer("Сейчас идет проверка аккаунтов. Дождись завершения.")
        return

    if is_recover_running():
        await callback.message.answer("Восстановление сессий уже запущено. Подожди отчет.")
        return

    chat_id = get_callback_chat_id(callback)
    if chat_id is None:
        return

    source_dir = config.ACCOUNT_IMPORT_DIR
    if not source_dir.exists() or not source_dir.is_dir():
        await callback.message.answer(
            "Папка для импорта не найдена.\n"
            f"Создай ее и положи туда папки аккаунтов: {source_dir}\n"
            "Потом нажми кнопку еще раз."
        )
        return

    if config.WARMER_ENABLED and warmer.is_busy_now():
        await callback.message.answer("Жду, пока прогрев освободит session-файлы...")
        idle = await warmer.wait_until_idle(timeout_seconds=180)
        if not idle:
            await callback.message.answer("Прогрев все еще занят. Повтори запуск восстановления через минуту.")
            return

    async def run_recover() -> None:
        global session_recover_task
        try:
            await bot.send_message(
                chat_id,
                "Запускаю восстановление сессий.\n"
                f"Источник: {source_dir}\n"
                f"Назначение: {config.SESSIONS_DIR}",
            )
            async with recover_lock:
                exit_code = await session_recover.run(
                    source=source_dir,
                    use_proxy=False,
                    force=False,
                    verbose=True,
                )
            if exit_code == 0:
                text = "Восстановление завершено без ошибок."
            elif exit_code == 2:
                text = (
                    "Восстановление завершено частично.\n"
                    "Часть аккаунтов не удалось восстановить автоматически."
                )
            else:
                text = "Восстановление завершилось с ошибкой."

            await bot.send_message(chat_id, text)
            await send_main_panel(chat_id)
        except Exception as exc:
            logger.exception("Ошибка восстановления сессий")
            await bot.send_message(chat_id, f"Ошибка восстановления сессий: {exc}")
            await send_main_panel(chat_id)
        finally:
            session_recover_task = None

    session_recover_task = asyncio.create_task(run_recover())
    if callback.message:
        text, markup = build_main_panel()
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await callback.message.answer("Принято. Импорт запущен, это может занять несколько минут.")


# ============================================================
# === Recovery (lolz auto-reauth) — UI и обработчики ===========
# ============================================================


def build_recovery_panel() -> tuple[str, InlineKeyboardMarkup]:
    """Главная панель восстановления."""
    if is_session_recovery_running():
        text = "<b>🔧 Восстановление сессий</b>\n\n⏳ Идёт сканирование / восстановление…"
        kb = [
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="recover_open")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="refresh_panel")],
        ]
        return text, InlineKeyboardMarkup(inline_keyboard=kb)

    token_set = bool(config.LOLZ_API_TOKEN)

    lines = ["<b>🔧 Восстановление сессий</b>", ""]
    if not token_set:
        lines += [
            "⚠️ <b>LOLZ_API_TOKEN не задан в .env</b>",
            "",
            "Без токена восстановление через lolz API недоступно.",
            "Можно только просканировать сессии и увидеть какие мёртвые.",
            "",
        ]
    else:
        lines += ["LOLZ API: <b>подключен</b> ✅", ""]

    if last_recovery_scan:
        lines.append(recovery.summarize_scan(last_recovery_scan))
    else:
        lines.append("<i>Сканирование не выполнялось.</i>")

    text = "\n".join(lines)

    kb: list[list[InlineKeyboardButton]] = []
    kb.append([InlineKeyboardButton(text="🔍 Сканировать все", callback_data="recover_scan")])

    if last_recovery_scan:
        dead_count = sum(1 for s in last_recovery_scan if s.needs_recovery)
        recoverable = sum(1 for s in last_recovery_scan if s.needs_recovery and s.item_id is not None)
        permanently_dead = sum(
            1 for s in last_recovery_scan
            if s.recovery_attempted and not s.recovery_success
            and any(m in (s.recovery_message or "").lower()
                    for m in ("недействительна", "logout", "2fa", "banned", "deactivated", "нет доступа"))
        )
        if recoverable > 0 and token_set:
            kb.append([
                InlineKeyboardButton(
                    text=f"🔧 Восстановить все ({recoverable})",
                    callback_data="recover_run_all",
                )
            ])
        if dead_count > 0:
            kb.append([
                InlineKeyboardButton(
                    text=f"📋 Показать мёртвые ({dead_count})",
                    callback_data="recover_show_dead",
                )
            ])
        if permanently_dead > 0:
            kb.append([
                InlineKeyboardButton(
                    text=f"🗑 Удалить безнадёжно мёртвые ({permanently_dead})",
                    callback_data="recover_cleanup_dead",
                )
            ])

    kb.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="refresh_panel")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


async def _edit_or_send(callback: types.CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    chat_id = get_callback_chat_id(callback)
    if chat_id is not None:
        await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data == "recover_open")
async def recover_open(callback: types.CallbackQuery) -> None:
    await callback.answer()
    text, markup = build_recovery_panel()
    await _edit_or_send(callback, text, markup)


@dp.callback_query(F.data == "recover_scan")
async def recover_scan(callback: types.CallbackQuery) -> None:
    global session_recovery_task, last_recovery_scan

    await callback.answer()

    if is_session_recovery_running():
        await callback.message.answer("Сканирование уже идёт, дождись.")
        return

    if is_job_running():
        await callback.message.answer("Сейчас идёт инвайт. Останови задачу сначала.")
        return

    if config.WARMER_ENABLED and warmer.is_busy_now():
        await callback.message.answer("Жду пока прогрев освободит session-файлы…")
        idle = await warmer.wait_until_idle(timeout_seconds=120)
        if not idle:
            await callback.message.answer("Прогрев всё ещё занят, повтори через минуту.")
            return

    chat_id = get_callback_chat_id(callback)
    if chat_id is None:
        return

    async def report_text(text: str) -> None:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("recovery report failed: %s", exc)

    async def run_scan() -> None:
        global session_recovery_task, last_recovery_scan
        try:
            statuses = await recovery.scan_all_sessions(reporter=report_text)
            last_recovery_scan = statuses
            await report_text(recovery.summarize_scan(statuses))
        except Exception as exc:
            logger.exception("Recovery scan failed")
            await report_text(f"❌ Ошибка сканирования: {exc}")
        finally:
            session_recovery_task = None
            try:
                text, markup = build_recovery_panel()
                await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass

    session_recovery_task = asyncio.create_task(run_scan())
    text, markup = build_recovery_panel()
    await _edit_or_send(callback, text, markup)


@dp.callback_query(F.data == "recover_run_all")
async def recover_run_all(callback: types.CallbackQuery) -> None:
    global session_recovery_task, last_recovery_scan

    await callback.answer()

    if not last_recovery_scan:
        await callback.message.answer("Сначала запусти сканирование.")
        return

    if not config.LOLZ_API_TOKEN:
        await callback.message.answer("LOLZ_API_TOKEN не задан в .env — восстановление невозможно.")
        return

    if is_session_recovery_running():
        await callback.message.answer("Уже идёт сканирование/восстановление, дождись.")
        return

    if is_job_running():
        await callback.message.answer("Сейчас идёт инвайт. Останови задачу сначала.")
        return

    if config.WARMER_ENABLED and warmer.is_busy_now():
        await callback.message.answer("Жду пока прогрев освободит session-файлы…")
        idle = await warmer.wait_until_idle(timeout_seconds=120)
        if not idle:
            await callback.message.answer("Прогрев всё ещё занят, повтори через минуту.")
            return

    chat_id = get_callback_chat_id(callback)
    if chat_id is None:
        return

    async def report_text(text: str) -> None:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("recovery report failed: %s", exc)

    async def run_recovery() -> None:
        global session_recovery_task, last_recovery_scan
        try:
            await recovery.recover_sessions(last_recovery_scan, reporter=report_text)
            # После восстановления — повторное сканирование чтобы обновить статусы
            await report_text("🔄 Перепроверяю сессии…")
            last_recovery_scan = await recovery.scan_all_sessions(reporter=None)
            await report_text(recovery.summarize_scan(last_recovery_scan))
        except Exception as exc:
            logger.exception("Recovery run failed")
            await report_text(f"❌ Ошибка восстановления: {exc}")
        finally:
            session_recovery_task = None
            try:
                text, markup = build_recovery_panel()
                await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass

    session_recovery_task = asyncio.create_task(run_recovery())
    text, markup = build_recovery_panel()
    await _edit_or_send(callback, text, markup)


@dp.callback_query(F.data == "recover_show_dead")
async def recover_show_dead(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not last_recovery_scan:
        await callback.message.answer("Сначала запусти сканирование.")
        return
    text = recovery.format_dead_list(last_recovery_scan, limit=50)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="recover_open")],
    ])
    await _edit_or_send(callback, text, kb)


@dp.callback_query(F.data == "recover_cleanup_dead")
async def recover_cleanup_dead(callback: types.CallbackQuery) -> None:
    global session_recovery_task, last_recovery_scan
    await callback.answer()

    if not last_recovery_scan:
        await callback.message.answer("Сначала запусти сканирование и восстановление.")
        return

    if is_session_recovery_running():
        await callback.message.answer("Сейчас идёт восстановление, дождись завершения.")
        return

    chat_id = get_callback_chat_id(callback)
    if chat_id is None:
        return

    async def report_text(text: str) -> None:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("cleanup report failed: %s", exc)

    async def run_cleanup() -> None:
        global session_recovery_task, last_recovery_scan
        try:
            result = await recovery.cleanup_dead_sessions(
                last_recovery_scan, reporter=report_text
            )
            moved = result.get("moved", [])
            await report_text(
                f"\n✅ Перемещено в архив: <b>{len(moved)}</b>\n"
                f"Файлы в <code>sessions_archive/dead_lolz/</code>"
            )
            # Перепроверим после очистки
            last_recovery_scan = await recovery.scan_all_sessions(reporter=None)
            await report_text(recovery.summarize_scan(last_recovery_scan))
        except Exception as exc:
            logger.exception("Cleanup failed")
            await report_text(f"❌ Ошибка очистки: {exc}")
        finally:
            session_recovery_task = None
            try:
                text, markup = build_recovery_panel()
                await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass

    session_recovery_task = asyncio.create_task(run_cleanup())
    text, markup = build_recovery_panel()
    await _edit_or_send(callback, text, markup)


@dp.callback_query(F.data == "stats")
async def stats(callback: types.CallbackQuery) -> None:
    await callback.answer()
    sessions_total_files = len(inviter.get_session_files(include_duplicates=True))
    sessions_count = len(inviter.get_session_files())
    sessions_duplicates = max(0, sessions_total_files - sessions_count)
    users_count = len(load_targets())
    processed_count = len(config.load_processed_usernames())
    pending_count = len(build_pending_targets())
    cooldowns = config.load_account_cooldowns()
    cooldown_count = len(cooldowns)
    invite_block_count = config.count_invite_block_accounts(cooldowns)
    saved_group = get_saved_group()
    warming_rows = warmer.get_warming_status_rows()
    in_work_count = sum(1 for row in warming_rows if row.get("in_work"))
    auto_work_count = sum(1 for row in warming_rows if row.get("auto_in_work"))
    manual_work_count = sum(
        1 for row in warming_rows if row.get("manual_enabled") and not row.get("auto_in_work")
    )
    can_manual_count = sum(1 for row in warming_rows if row.get("can_manual_start"))
    group_str = f"@{saved_group}" if saved_group else "не задана"

    lines = [
        "<b>📊 Статистика</b>",
        "",
        "<b>Аккаунты</b>",
        f"  👥 Уникальных: <b>{sessions_count}</b>  (файлов: {sessions_total_files}, дублей: {sessions_duplicates})",
        f"  🔥 В работе: <b>{in_work_count}</b>  (авто: {auto_work_count}, вручную: {manual_work_count})",
        f"  ⏳ Готовы к запуску: <b>{can_manual_count}</b>",
        f"  🚫 В отлёжке: <b>{cooldown_count}</b>  (инвайт-бан: {invite_block_count})",
        "",
        "<b>Инвайты</b>",
        f"  🎯 Группа: <b>{group_str}</b>",
        f"  📋 Всего в targets: <b>{users_count}</b>",
        f"  ✅ Обработано: <b>{processed_count}</b>",
        f"  ⏭ Осталось: <b>{pending_count}</b>",
    ]

    if is_job_running() and current_job:
        elapsed = format_elapsed(current_job.started_at)
        pct = int(current_job.processed / current_job.total * 100) if current_job.total else 0
        lines += [
            "",
            "<b>Активная задача</b>",
            f"  🎯 @{current_job.target_group}  ·  {elapsed}",
            f"  📈 {current_job.processed}/{current_job.total} ({pct}%)",
            f"  ✅ {current_job.added}   ⏭ {current_job.skipped}   ❌ {current_job.failed}",
        ]

    import_status = f"каждые {config.AUTO_IMPORT_SYNC_INTERVAL} с" if config.AUTO_IMPORT_SYNC_ENABLED else "выкл"
    lines += [
        "",
        f"<b>Автоимпорт:</b> {import_status}",
    ]

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="stats")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="refresh_panel")],
    ])

    if callback.message:
        try:
            await callback.message.edit_text("\n".join(lines), reply_markup=markup, parse_mode="HTML")
        except Exception:
            await callback.message.answer("\n".join(lines), reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data == "warm_open")
async def warm_open(callback: types.CallbackQuery) -> None:
    await callback.answer()
    text, markup = build_warming_view(page=0)
    await callback.message.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("warm_page:"))
async def warm_page(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not callback.data:
        return

    _, raw_page = callback.data.split(":", 1)
    page = _parse_non_negative_int(raw_page, default=0)
    text, markup = build_warming_view(page=page)
    await safe_edit_callback_message(callback, text, markup)


@dp.callback_query(F.data.startswith("warm_run:"))
async def warm_run(callback: types.CallbackQuery) -> None:
    if not callback.data:
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        return

    row_index = _parse_non_negative_int(parts[1], default=-1)
    page = _parse_non_negative_int(parts[2], default=0)
    rows = warmer.get_warming_status_rows()
    if row_index < 0 or row_index >= len(rows):
        await callback.answer("Список изменился, обнови раздел «Прогрев».", show_alert=True)
        text, markup = build_warming_view(page=page)
        await safe_edit_callback_message(callback, text, markup)
        return

    row = rows[row_index]
    session_name = str(row.get("session_name", "")).strip()
    ok, message = warmer.enable_session_manual_work(session_name)
    await callback.answer(message, show_alert=not ok)

    text, markup = build_warming_view(page=page)
    await safe_edit_callback_message(callback, text, markup)


@dp.callback_query(F.data == "warm_start_all")
async def warm_start_all(callback: types.CallbackQuery) -> None:
    rows = warmer.get_warming_status_rows()
    started = 0
    skipped = 0
    messages: list[str] = []

    for row in rows:
        if not row.get("can_manual_start"):
            continue
        session_name = str(row.get("session_name", "")).strip()
        if not session_name:
            skipped += 1
            continue
        ok, message = warmer.enable_session_manual_work(session_name)
        if ok:
            started += 1
            if started <= 5:
                messages.append(f"  ✅ {session_name}")
        else:
            skipped += 1
            if skipped <= 3:
                messages.append(f"  ⚠️ {session_name}: {message}")

    text_parts = [f"🔥 Запущено в работу: {started}"]
    if messages:
        text_parts.append("\n".join(messages))
    if skipped > 0:
        text_parts.append(f"  Пропущено: {skipped}")

    await callback.answer("\n".join(text_parts), show_alert=True)
    text, markup = build_warming_view(page=0)
    await safe_edit_callback_message(callback, text, markup)


@dp.callback_query(F.data == "warm_back")
async def warm_back(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        text, markup = build_main_panel()
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data == "refresh_panel")
async def refresh_panel(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    text, markup = build_main_panel()
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        pass


@dp.callback_query(F.data == "stop_invite")
async def stop_invite(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not is_job_running() or not current_job:
        await callback.message.answer("Сейчас нет задачи, которую можно остановить.")
        return

    if callback.from_user and callback.from_user.id != current_job.user_id:
        await callback.message.answer("Остановить задачу может только тот, кто ее запустил.")
        return

    current_job.stop_event.set()
    if callback.message:
        text, markup = build_main_panel()
        try:
            await callback.message.edit_text(
                text + "\n\n⏹ <i>Остановка задачи…</i>",
                reply_markup=markup,
                parse_mode="HTML",
            )
        except Exception:
            await callback.message.answer("Принято. Останавливаю задачу, дождись финального отчёта.")


@dp.callback_query(F.data == "change_group")
async def change_group(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if is_check_running():
        await callback.message.answer("Сейчас идет проверка аккаунтов. Дождись завершения.")
        return

    if is_job_running():
        await callback.message.answer(
            "Сейчас идет задача. Сначала останови ее или дождись завершения."
        )
        return

    if callback.from_user:
        waiting_for_target_users.add(callback.from_user.id)

    await callback.message.answer(
        "Отправь новую ссылку или username группы.\n"
        "Пример: t.me/my_group или @my_group"
    )


@dp.callback_query(F.data == "start_invite")
async def start_invite(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if is_check_running():
        await callback.message.answer("Сейчас идет проверка аккаунтов. Дождись завершения.")
        return

    if is_job_running():
        await callback.message.answer(
            "Сейчас уже идет задача. Останови ее кнопкой «Остановить» или дождись завершения."
        )
        return

    chat_id = get_callback_chat_id(callback)
    user_id = callback.from_user.id if callback.from_user else None
    if chat_id is None or user_id is None:
        return

    saved_group = get_saved_group()
    if saved_group:
        await start_invite_job(chat_id=chat_id, user_id=user_id, target_group=saved_group)
        if callback.message:
            text, markup = build_main_panel()
            try:
                await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass
        return

    waiting_for_target_users.add(user_id)
    await callback.message.answer(
        "Сохраненной группы пока нет.\n"
        "Отправь ссылку или username группы, куда нужно пригласить людей."
    )


@dp.message(F.text)
async def run_inviting(message: types.Message) -> None:
    if not message.text:
        return

    if not message.from_user:
        return

    if message.from_user.id not in waiting_for_target_users:
        return

    waiting_for_target_users.discard(message.from_user.id)

    if is_job_running():
        await message.answer("Сейчас уже есть активная задача. Сначала останови ее или дождись конца.")
        return

    target_group = normalize_target_group(message.text)
    if not target_group:
        await message.answer("Не понял группу. Отправь ссылку вида t.me/group или @group.")
        return
    if not PUBLIC_GROUP_RE.fullmatch(target_group):
        await message.answer(
            "Нужна публичная группа формата @group или t.me/group.\n"
            "Приватные ссылки-приглашения здесь не поддерживаются."
        )
        return

    config.save_target_group(target_group)
    await message.answer(f"Группа сохранена: @{target_group}")

    await start_invite_job(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        target_group=target_group,
    )


async def run_auto_cleanup(stop_event: asyncio.Event) -> None:
    interval = max(60, int(config.AUTO_CLEANUP_INTERVAL))
    while not stop_event.is_set():
        if stop_event.is_set():
            break
        if is_job_running() or is_check_running():
            await sleep_or_stop(stop_event, 30)
            continue
        try:
            logger.info("🧹 Автоочистка: запуск проверки сессий...")
            result = await session_checker.cleanup_sessions(dry_run=False)
            logger.info(
                "🧹 Автоочистка: всего=%s, мертвых=%s, дубликатов=%s, оставлено=%s",
                result["total"],
                result["dead"],
                result["duplicates"],
                result["kept"],
            )
            session_checker.deduplicate_warmer_state(config.WARMER_STATE_FILE)
        except Exception:
            logger.exception("Ошибка автоочистки сессий")

        await sleep_or_stop(stop_event, interval)


async def main() -> None:
    warmer_stop_event = asyncio.Event()
    warmer_task: Optional[asyncio.Task] = None
    auto_import_stop_event = asyncio.Event()
    auto_import_task: Optional[asyncio.Task] = None
    auto_cleanup_stop_event = asyncio.Event()
    auto_cleanup_task: Optional[asyncio.Task] = None

    if config.WARMER_ENABLED:
        warmer_task = asyncio.create_task(
            warmer.run_warmer_service(
                stop_event=warmer_stop_event,
                is_busy=is_system_busy,
            )
        )
        logger.info("Фоновый warmer запущен.")

    if config.AUTO_IMPORT_SYNC_ENABLED:
        auto_import_task = asyncio.create_task(run_auto_import_sync(auto_import_stop_event))
        logger.info(
            "Фоновая синхронизация account_import запущена (интервал: %s сек).",
            config.AUTO_IMPORT_SYNC_INTERVAL,
        )

    if config.AUTO_CLEANUP_ENABLED:
        auto_cleanup_task = asyncio.create_task(run_auto_cleanup(auto_cleanup_stop_event))
        logger.info(
            "Фоновая очистка сессий запущена (интервал: %s сек).",
            config.AUTO_CLEANUP_INTERVAL,
        )

    try:
        await dp.start_polling(bot)
    finally:
        for stop_ev, task in [
            (auto_import_stop_event, auto_import_task),
            (auto_cleanup_stop_event, auto_cleanup_task),
            (warmer_stop_event, warmer_task),
        ]:
            stop_ev.set()
            if task:
                try:
                    await asyncio.wait_for(task, timeout=10)
                except asyncio.TimeoutError:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


if __name__ == "__main__":
    asyncio.run(main())

