import asyncio
import logging
from typing import Optional, List, Dict, Any

import aiohttp

logger = logging.getLogger("telegram_inviter_bot.lolz_reauth")

LOLZ_API_BASE = "https://prod-api.lzt.market"

# Глобальный rate-limit: 20 req/min = 3 сек между запросами (стандартные запросы lzt.market).
# Берём с запасом 3.5 сек.
# Поисковые эндпоинты — 10/min (6 сек), но мы их не используем.
# Превышение → HTTP 429 и временный бан ключа.
_RATE_LIMIT_DELAY = 3.5
_rate_limit_lock = asyncio.Lock()
_last_request_at: float = 0.0


async def _rate_limit() -> None:
    global _last_request_at
    async with _rate_limit_lock:
        loop = asyncio.get_event_loop()
        now = loop.time()
        delta = now - _last_request_at
        if delta < _RATE_LIMIT_DELAY:
            await asyncio.sleep(_RATE_LIMIT_DELAY - delta)
        _last_request_at = asyncio.get_event_loop().time()


class LolzMarketAPI:
    def __init__(self, token: str):
        self.token = token
        self.session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}"}
            )
        return self.session

    async def _request(self, method: str, url: str, **kwargs) -> tuple[int, dict]:
        """Возвращает (status_code, json_body). Не бросает исключений по статусу.

        При HTTP 429 (rate limit) — ждёт указанное в Retry-After время и
        повторяет запрос (максимум 2 ретрая).
        """
        kwargs.setdefault("timeout", aiohttp.ClientTimeout(total=30))
        for attempt in range(3):
            await _rate_limit()
            session = await self._ensure_session()
            async with session.request(method, url, **kwargs) as resp:
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After", "10")
                    try:
                        wait_seconds = float(retry_after)
                    except (TypeError, ValueError):
                        wait_seconds = 10.0
                    wait_seconds = min(60.0, max(3.0, wait_seconds))
                    logger.warning(
                        "lolz API rate-limit hit (429), жду %.1f сек (попытка %d/3)",
                        wait_seconds,
                        attempt + 1,
                    )
                    await asyncio.sleep(wait_seconds)
                    continue

                try:
                    data = await resp.json()
                except Exception:
                    data = {"_raw_text": await resp.text()}
                return resp.status, data

        return 429, {"_error": "rate-limit exceeded after retries"}

    @staticmethod
    def _extract_error_text(data: dict) -> str:
        """Извлекает читаемое сообщение об ошибке из ответа API."""
        if not isinstance(data, dict):
            return ""
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        if isinstance(errors, str):
            return errors
        msg = data.get("message") or data.get("error")
        if isinstance(msg, str):
            return msg
        return ""

    async def get_login_code(self, item_id: int) -> tuple[bool, str, dict]:
        """Возвращает (ok, code_or_error, raw_body).

        ok=True  -> code_or_error содержит сам код
        ok=False -> code_or_error содержит описание ошибки
                    raw_body содержит сырой ответ для диагностики
        """
        url = f"{LOLZ_API_BASE}/{item_id}/telegram-login-code"
        status, data = await self._request("GET", url)
        if status == 200:
            codes = data.get("codes") or {}
            code = codes.get("code") or data.get("code") or data.get("login_code")
            if code:
                return True, str(code), data
            return False, f"код не найден в ответе: {data}", data

        api_error = self._extract_error_text(data)

        if status == 401:
            return False, f"401: {api_error or 'неверный API токен'}", data
        if status == 403:
            # Может быть: "сессия недействительна", "аккаунт больше не ваш" и т.п.
            return False, f"403: {api_error or 'нет доступа'}", data
        if status == 404:
            return False, "404: аккаунт не найден на маркете", data
        return False, f"API error {status}: {api_error or data}", data

    async def reset_auth(self, item_id: int) -> tuple[bool, str, dict]:
        url = f"{LOLZ_API_BASE}/{item_id}/telegram-reset-authorizations"
        status, data = await self._request("POST", url)
        if status == 200:
            return True, "авторизации сброшены", data
        api_error = self._extract_error_text(data)
        if status == 401:
            return False, f"401: {api_error or 'неверный токен'}", data
        if status == 403:
            return False, f"403: {api_error or 'нет доступа'}", data
        if status == 404:
            return False, "404: аккаунт не найден", data
        return False, f"API error {status}: {api_error or data}", data

    async def check_account_validity(self, item_id: int) -> tuple[bool, str, dict]:
        """Запускает официальную проверку валидности аккаунта на маркете.

        Это заставляет lolz перепроверить аккаунт и (если возможно) обновить сессию,
        что иногда помогает когда продавец недавно вышел из своих устройств.
        """
        url = f"{LOLZ_API_BASE}/{item_id}/check-account"
        status, data = await self._request("POST", url)
        if status == 200:
            return True, "ok", data
        api_error = self._extract_error_text(data)
        if status == 403:
            return False, f"403: {api_error or 'нет доступа'}", data
        if status == 404:
            return False, "404: аккаунт не найден", data
        return False, f"API error {status}: {api_error or data}", data

    async def get_item(self, item_id: int) -> tuple[bool, str, dict]:
        """Получить инфо о конкретном лоте."""
        url = f"{LOLZ_API_BASE}/{item_id}"
        status, data = await self._request("GET", url)
        if status == 200:
            return True, "ok", data
        if status == 403:
            return False, "нет доступа", data
        if status == 404:
            return False, "не найден", data
        return False, f"API error {status}: {data}", data

    async def bulk_items(self, item_ids: List[int]) -> tuple[bool, str, dict]:
        """До 250 item_id за один запрос. Возвращает массив лотов которые мы владеем."""
        url = f"{LOLZ_API_BASE}/bulk/items"
        status, data = await self._request(
            "POST",
            url,
            json={"item_id": item_ids},
        )
        if status == 200:
            return True, "ok", data
        if status == 401:
            return False, "неверный API токен", data
        return False, f"API error {status}: {data}", data

    async def list_owned(self, category_id: Optional[int] = None) -> tuple[bool, str, dict]:
        """Список лотов которыми владеет пользователь."""
        url = f"{LOLZ_API_BASE}/user/items"
        params = {}
        if category_id is not None:
            params["category_id"] = category_id
        status, data = await self._request("GET", url, params=params or None)
        if status == 200:
            return True, "ok", data
        if status == 401:
            return False, "неверный API токен", data
        return False, f"API error {status}: {data}", data

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None


# === Удобные обёртки (обратная совместимость и простой API) ===


async def get_lolz_login_code(
    item_id: int,
    api_token: str,
) -> tuple[bool, str]:
    """Старый API, оставлен для совместимости."""
    if not api_token:
        return False, "lolz API токен не настроен"

    api = LolzMarketAPI(api_token)
    try:
        ok, msg, _raw = await api.get_login_code(item_id)
        return ok, msg
    except Exception as exc:
        return False, str(exc)
    finally:
        await api.close()


async def reset_lolz_account(
    item_id: int,
    api_token: str,
) -> tuple[bool, str]:
    if not api_token:
        return False, "lolz API токен не настроен"

    api = LolzMarketAPI(api_token)
    try:
        ok, msg, _raw = await api.reset_auth(item_id)
        return ok, msg
    except Exception as exc:
        return False, str(exc)
    finally:
        await api.close()


async def bulk_check_ownership(
    item_ids: List[int],
    api_token: str,
) -> Dict[int, bool]:
    """Проверяет владение для списка item_id. Возвращает {item_id: owned_bool}."""
    if not api_token or not item_ids:
        return {}

    result: Dict[int, bool] = {}
    api = LolzMarketAPI(api_token)
    try:
        # Дробим на батчи по 250
        for i in range(0, len(item_ids), 250):
            batch = item_ids[i : i + 250]
            ok, msg, data = await api.bulk_items(batch)
            if not ok:
                logger.warning("bulk_items failed: %s", msg)
                # Не можем определить — помечаем все как unknown=False
                for iid in batch:
                    result[iid] = False
                continue

            items = data.get("items") if isinstance(data, dict) else None
            if isinstance(data, list):
                items = data
            if items is None:
                items = []

            owned_ids = set()
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                iid = entry.get("item_id") or entry.get("id")
                if isinstance(iid, int):
                    owned_ids.add(iid)
                elif isinstance(iid, str) and iid.isdigit():
                    owned_ids.add(int(iid))

            for iid in batch:
                result[iid] = iid in owned_ids
    finally:
        await api.close()

    return result
