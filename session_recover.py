from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

import config
import session_meta

logger = logging.getLogger("telegram_inviter_bot.session_recover")
logger.setLevel(logging.INFO)


PROD_DC_MAP = {
    1: ("149.154.175.53", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("149.154.171.5", 443),
}

TEST_DC_MAP = {
    1: ("149.154.175.10", 443),
    2: ("149.154.167.40", 443),
    3: ("149.154.175.117", 443),
}

STRING_SESSION_KEYS = {
    "session_string",
    "string_session",
    "telethon_session_string",
    "telethon_string",
    "session_str",
    "session",
}


@dataclass
class Bundle:
    path: Path
    country: str
    target_name: str


def short_error(exc: Exception, limit: int = 200) -> str:
    text = str(exc).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def normalize_country(value: str) -> str:
    raw = value.strip().lower()
    if not raw:
        return "default"
    clean = re.sub(r"[^a-z0-9_-]+", "", raw)
    return clean or "default"


def normalize_session_name(value: str) -> str:
    raw = value.strip()
    if raw.lower().endswith(".session"):
        raw = raw[:-8]
    clean = re.sub(r"[^A-Za-z0-9_+.-]+", "_", raw).strip("._ ")
    if not clean:
        clean = "account"
    return f"{clean}.session"


def has_bundle_artifacts(path: Path) -> bool:
    if not path.is_dir():
        return False
    if any(path.glob("*.session")):
        return True
    if any(path.glob("*.json")):
        return True
    if (path / "tdata").is_dir():
        return True
    if any(path.glob("*tdata*.zip")):
        return True
    return False


def detect_country(bundle_path: Path, source_root: Path) -> str:
    country_txt = bundle_path / "country.txt"
    if country_txt.exists():
        try:
            return normalize_country(country_txt.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            pass

    rel = bundle_path.relative_to(source_root)
    if len(rel.parts) >= 2:
        return normalize_country(rel.parts[0])
    return "default"


def detect_target_name(bundle_path: Path) -> str:
    preferred = sorted(bundle_path.glob("*_telethon.session"))
    if preferred:
        return normalize_session_name(preferred[0].name)

    any_session = sorted(bundle_path.glob("*.session"))
    if any_session:
        return normalize_session_name(any_session[0].name)

    return normalize_session_name(bundle_path.name)


def iter_leaf_bundles(source_root: Path) -> list[Bundle]:
    candidates: list[Path] = []
    all_dirs = [source_root] + [p for p in source_root.rglob("*") if p.is_dir()]
    for directory in all_dirs:
        if not has_bundle_artifacts(directory):
            continue
        child_has_artifacts = any(
            child.is_dir() and has_bundle_artifacts(child)
            for child in directory.iterdir()
        )
        if child_has_artifacts:
            continue
        candidates.append(directory)

    bundles: list[Bundle] = []
    for path in sorted(candidates):
        bundles.append(
            Bundle(
                path=path,
                country=detect_country(path, source_root),
                target_name=detect_target_name(path),
            )
        )
    return bundles


def target_session_path(bundle: Bundle) -> Path:
    if bundle.country == "default":
        return config.SESSIONS_DIR / bundle.target_name
    return config.SESSIONS_DIR / bundle.country / bundle.target_name


def copy_with_sidecars(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    for suffix in ("-journal", "-wal", "-shm"):
        side_src = Path(str(src) + suffix)
        side_dst = Path(str(dst) + suffix)
        if side_src.exists():
            shutil.copy2(side_src, side_dst)


def cleanup_session_files(path: Path) -> None:
    for candidate in [path, Path(str(path) + "-journal"), Path(str(path) + "-wal"), Path(str(path) + "-shm")]:
        try:
            if candidate.exists():
                candidate.unlink()
        except Exception:
            pass


async def validate_session_file(
    session_path: Path,
    country: str,
    *,
    use_proxy: bool,
) -> tuple[bool, str]:
    proxy = None
    if use_proxy:
        try:
            session_name = str(session_path.relative_to(config.SESSIONS_DIR)).replace("\\", "/")
            proxy, _ = config.resolve_proxy_for_session_with_desc(session_name, country)
        except Exception:
            proxy = None

    client: Optional[TelegramClient] = None
    try:
        client = session_meta.build_client(session_path, proxy=proxy)
        await client.connect()
        if not await client.is_user_authorized():
            return False, "не авторизован (слетел вход)"
        me = await client.get_me()
        if me and me.username:
            label = f"@{me.username}"
        elif me and getattr(me, "phone", None):
            label = f"+{me.phone}"
        elif me:
            label = f"id:{me.id}"
        else:
            label = "данные профиля недоступны"
        return True, f"валиден ({label})"
    except Exception as exc:
        return False, short_error(exc)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


def should_attempt_recovery_on_existing_failure(detail: str) -> bool:
    text = (detail or "").lower()
    if "не авторизован" in text or "not authorized" in text:
        return True

    # Явно битая/чужая sqlite-структура session, которую нужно пытаться пересобрать.
    fatal_markers = (
        "no such column: version",
        "database disk image is malformed",
        "file is not a database",
        "unable to open database file",
        "encrypted or is not a database",
        "not a valid sqlite",
    )
    return any(marker in text for marker in fatal_markers)


def try_extract_auth_key(value: object) -> Optional[bytes]:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytearray):
        value = bytes(value)
    if isinstance(value, bytes):
        if len(value) >= 64:
            return value
        return None
    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None
    if re.fullmatch(r"[0-9a-fA-F]+", raw) and len(raw) % 2 == 0:
        try:
            data = bytes.fromhex(raw)
            if len(data) >= 64:
                return data
        except Exception:
            pass
    try:
        import base64

        data = base64.b64decode(raw + "==", validate=False)
        if len(data) >= 64:
            return data
    except Exception:
        pass

    return None


def try_convert_sqlite_to_telethon(src_session: Path, dst_session: Path) -> tuple[bool, str]:
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"file:{src_session.as_posix()}?mode=ro", uri=True)
        cur = conn.cursor()
        tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" not in tables:
            return False, "таблица sessions не найдена"

        cur.execute("SELECT * FROM sessions LIMIT 1")
        row = cur.fetchone()
        if not row:
            return False, "таблица sessions пустая"
        columns = [desc[0] for desc in cur.description]
        payload = {columns[idx]: row[idx] for idx in range(len(columns))}

        dc_id_raw = payload.get("dc_id") or payload.get("dc")
        if dc_id_raw is None:
            return False, "в sessions нет dc_id"
        dc_id = int(dc_id_raw)

        auth_key_raw = payload.get("auth_key") or payload.get("auth")
        auth_key = try_extract_auth_key(auth_key_raw)
        if not auth_key:
            return False, "не удалось извлечь auth_key"

        test_mode = bool(payload.get("test_mode", False))
        server = payload.get("server_address") or payload.get("ip_address") or payload.get("ipv4")
        port = payload.get("port") or 443
        try:
            port = int(port)
        except Exception:
            port = 443

        if not server:
            dc_map = TEST_DC_MAP if test_mode else PROD_DC_MAP
            server, port = dc_map.get(dc_id, ("149.154.167.51", 443))
        server = str(server).strip()

        cleanup_session_files(dst_session)
        sql = SQLiteSession(str(dst_session))
        sql.set_dc(dc_id, server, port)
        sql.auth_key = AuthKey(data=auth_key)
        sql.save()
        sql.close()
        return True, "сконвертировано из sqlite sessions"
    except Exception as exc:
        return False, short_error(exc)
    finally:
        if conn:
            conn.close()


def iter_json_files(bundle_path: Path) -> Iterable[Path]:
    for path in sorted(bundle_path.glob("*.json")):
        if path.is_file():
            yield path


def collect_possible_strings(data: object, *, key_hint: str = "", depth: int = 0) -> Iterable[str]:
    if depth > 8:
        return
    if isinstance(data, dict):
        for key, value in data.items():
            key_lower = str(key).strip().lower()
            if isinstance(value, str) and key_lower in STRING_SESSION_KEYS:
                yield value.strip()
            yield from collect_possible_strings(value, key_hint=key_lower, depth=depth + 1)
        return
    if isinstance(data, list):
        for item in data:
            yield from collect_possible_strings(item, key_hint=key_hint, depth=depth + 1)
        return
    if isinstance(data, str):
        value = data.strip()
        if not value:
            return
        if key_hint in STRING_SESSION_KEYS and len(value) >= 50:
            yield value
            return
        if len(value) >= 200 and re.fullmatch(r"[A-Za-z0-9_\-=:/.+]+", value):
            yield value


def write_telethon_from_string(string_value: str, dst_session: Path) -> tuple[bool, str]:
    try:
        src = StringSession(string_value)
        if not src.auth_key:
            return False, "пустой auth_key в string session"
        cleanup_session_files(dst_session)
        sql = SQLiteSession(str(dst_session))
        sql.set_dc(src.dc_id, src.server_address, src.port)
        sql.auth_key = src.auth_key
        sql.save()
        sql.close()
        return True, "сконвертировано из session string"
    except Exception as exc:
        return False, short_error(exc)


async def try_convert_tdata(bundle_path: Path, dst_session: Path) -> tuple[bool, str]:
    tdata_dirs: list[Path] = []
    extracted_temp_dirs: list[tempfile.TemporaryDirectory] = []
    direct = bundle_path / "tdata"
    if direct.is_dir():
        tdata_dirs.append(direct)
    for child in bundle_path.iterdir():
        if child.is_dir() and child.name.lower() == "tdata":
            tdata_dirs.append(child)
        if child.is_file() and child.suffix.lower() == ".zip" and "tdata" in child.name.lower():
            try:
                temp_dir = tempfile.TemporaryDirectory(prefix="tdata_unzip_")
                extracted_temp_dirs.append(temp_dir)
                out_path = Path(temp_dir.name)
                with zipfile.ZipFile(child, "r") as zip_file:
                    zip_file.extractall(out_path)

                nested_tdata = out_path / "tdata"
                if nested_tdata.is_dir():
                    tdata_dirs.append(nested_tdata)
                elif out_path.is_dir():
                    tdata_dirs.append(out_path)
            except Exception:
                continue
    if not tdata_dirs:
        return False, "tdata не найден"

    try:
        from opentele.api import API  # type: ignore
        from opentele.td import TDesktop  # type: ignore
    except Exception:
        return False, "opentele не установлен (pip install opentele)"

    use_current_session = None
    try:
        from opentele.api import UseCurrentSession  # type: ignore

        use_current_session = UseCurrentSession
    except Exception:
        use_current_session = None

    try:
        for tdata_dir in tdata_dirs:
            try:
                cleanup_session_files(dst_session)
                tdesk = TDesktop(str(tdata_dir))
                if hasattr(tdesk, "isLoaded") and not tdesk.isLoaded():
                    continue

                kwargs = {}
                if use_current_session is not None:
                    kwargs["flag"] = use_current_session
                kwargs["api"] = getattr(API, "TelegramDesktop", API)
                kwargs["session"] = str(dst_session)

                client = await tdesk.ToTelethon(**kwargs)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return True, f"сконвертировано из tdata ({tdata_dir.name})"
            except Exception:
                continue
        return False, "не удалось конвертировать tdata"
    finally:
        for temp_dir in extracted_temp_dirs:
            try:
                temp_dir.cleanup()
            except Exception:
                pass


async def process_bundle(
    bundle: Bundle,
    *,
    use_proxy: bool,
    force: bool,
    preserve_existing: bool = False,
) -> tuple[bool, str]:
    target = target_session_path(bundle)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and not force:
        ok, detail = await validate_session_file(target, bundle.country, use_proxy=use_proxy)
        if ok:
            if preserve_existing:
                return True, f"оставлено без изменений: {target.name}"
            return True, f"уже есть рабочая сессия: {target.name}"

        if preserve_existing:
            # В безопасном автоимпорте трогаем только слетевшие/битые сессии.
            if not should_attempt_recovery_on_existing_failure(detail):
                return True, f"оставлено без изменений (неаварийная ошибка проверки): {target.name}"

    source_sessions = sorted(
        path for path in bundle.path.glob("*.session")
        if path.is_file()
    )

    if target.exists():
        cleanup_session_files(target)

    # 1) Пробуем напрямую любую .session (telethon или совместимую)
    for src in source_sessions:
        try:
            copy_with_sidecars(src, target)
        except Exception as exc:
            cleanup_session_files(target)
            continue

        ok, detail = await validate_session_file(target, bundle.country, use_proxy=use_proxy)
        if ok:
            return True, f"готово из {src.name} -> {target.name}"
        cleanup_session_files(target)

    # 2) Пробуем конвертацию sqlite -> telethon (часто помогает с pyrogram)
    for src in source_sessions:
        conv_ok, conv_detail = try_convert_sqlite_to_telethon(src, target)
        if not conv_ok:
            continue
        ok, detail = await validate_session_file(target, bundle.country, use_proxy=use_proxy)
        if ok:
            return True, f"готово из {src.name} (sqlite) -> {target.name}"
        cleanup_session_files(target)

    # 3) Пробуем вытащить session string из json
    for json_file in iter_json_files(bundle.path):
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue

        for maybe_string in collect_possible_strings(payload):
            conv_ok, conv_detail = write_telethon_from_string(maybe_string, target)
            if not conv_ok:
                continue
            ok, detail = await validate_session_file(target, bundle.country, use_proxy=use_proxy)
            if ok:
                return True, f"готово из {json_file.name} (string session) -> {target.name}"
            cleanup_session_files(target)

    # 4) Пробуем tdata через opentele (если установлен)
    conv_ok, conv_detail = await try_convert_tdata(bundle.path, target)
    if conv_ok:
        ok, detail = await validate_session_file(target, bundle.country, use_proxy=use_proxy)
        if ok:
            return True, f"готово из tdata -> {target.name}"
        cleanup_session_files(target)

    cleanup_session_files(target)
    return False, "не получилось собрать валидную telethon-сессию из файлов папки"


async def run(
    source: Path,
    *,
    use_proxy: bool,
    force: bool,
    verbose: bool = True,
    preserve_existing: bool = False,
) -> int:
    if not source.exists() or not source.is_dir():
        logger.error("❌ Папка не найдена: %s", source)
        return 1

    bundles = iter_leaf_bundles(source)
    if not bundles:
        logger.error("❌ В папке %s не найдены подпапки с данными аккаунтов.", source)
        return 1

    if verbose:
        logger.info("🔎 Найдено папок аккаунтов: %s", len(bundles))
        logger.info("📦 Источник: %s", source)
        logger.info("📁 Цель: %s", config.SESSIONS_DIR)
        logger.info("")

    success_count = 0
    failed_count = 0
    for idx, bundle in enumerate(bundles, start=1):
        if verbose:
            logger.info("[%s/%s] 🔄 %s", idx, len(bundles), bundle.path.name)
            logger.info("  🌍 Страна: %s | Файл: %s", bundle.country, bundle.target_name)
        ok, detail = await process_bundle(
            bundle,
            use_proxy=use_proxy,
            force=force,
            preserve_existing=preserve_existing,
        )
        if ok:
            success_count += 1
            if verbose:
                logger.info("  ✅ %s", detail)
        else:
            failed_count += 1
            if verbose:
                logger.info("  ❌ %s", detail)
        if verbose:
            logger.info("")

    if verbose:
        logger.info("Итог:")
        logger.info("- Успешно: %s", success_count)
        logger.info("- Ошибок: %s", failed_count)
        logger.info("- Всего: %s", len(bundles))

    return 0 if failed_count == 0 else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Авто-восстановление telethon .session из папок аккаунтов "
            "(telethon/pyrogram session, json string session, tdata)."
        )
    )
    parser.add_argument(
        "--source",
        default="account_import",
        help="Папка с подпапками аккаунтов (по умолчанию: account_import)",
    )
    parser.add_argument(
        "--use-proxy",
        action="store_true",
        help="Проверять валидность через прокси из config.py (по умолчанию проверка идет без прокси).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Перезаписывать уже существующие session-файлы.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("telethon.network").setLevel(logging.WARNING)
    args = parse_args()
    exit_code = asyncio.run(
        run(
            source=Path(args.source).resolve(),
            use_proxy=bool(args.use_proxy),
            force=bool(args.force),
        )
    )
    raise SystemExit(exit_code)


