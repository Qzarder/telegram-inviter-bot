"""Раскладка sticky-прокси по аккаунтам и перебалансировка session_proxy_bindings.json."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Optional

import config
import inviter


@dataclass
class SessionProxyRow:
    session_name: str
    country: str
    pool_key: str
    proxy_index: int
    pool_size: int
    proxy_endpoint: str
    has_binding: bool


@dataclass
class ProxyRebalanceChange:
    session_name: str
    pool_key: str
    old_index: Optional[int]
    new_index: int


def _proxy_endpoint(pool_key: str, index: int, pools: dict[str, list[dict]]) -> str:
    pool = pools.get(pool_key, [])
    if not pool or index < 0 or index >= len(pool):
        return "?"
    item = pool[index]
    return f"{item['type']}://{item['addr']}:{item['port']}"


def collect_session_proxy_rows() -> list[SessionProxyRow]:
    pools = config.load_country_proxy_pools()
    bindings = config.load_session_proxy_bindings()
    rows: list[SessionProxyRow] = []

    for session_path in inviter.get_session_files():
        session_name = str(session_path.relative_to(config.SESSIONS_DIR)).replace("\\", "/")
        session_key = config.normalize_session_key(session_name)
        country = inviter.detect_session_country(session_path)
        pool_key = config._resolve_pool_key_for_country(country, pools)

        if not pool_key:
            rows.append(
                SessionProxyRow(
                    session_name=session_name,
                    country=country,
                    pool_key="",
                    proxy_index=-1,
                    pool_size=0,
                    proxy_endpoint="нет пула",
                    has_binding=False,
                )
            )
            continue

        pool_size = len(pools[pool_key])
        binding = bindings.get(session_key, {})
        has_binding = session_key in bindings

        if has_binding and str(binding.get("pool_key", "")).lower() == pool_key:
            try:
                proxy_index = int(binding.get("index", 0))
            except (TypeError, ValueError):
                proxy_index = config._stable_proxy_index(session_key, pool_size)
        else:
            proxy_index = config._stable_proxy_index(session_key, pool_size)

        if proxy_index < 0 or proxy_index >= pool_size:
            proxy_index = proxy_index % pool_size if pool_size else 0

        rows.append(
            SessionProxyRow(
                session_name=session_name,
                country=country,
                pool_key=pool_key,
                proxy_index=proxy_index,
                pool_size=pool_size,
                proxy_endpoint=_proxy_endpoint(pool_key, proxy_index, pools),
                has_binding=has_binding,
            )
        )

    return rows


def _distribution_counts(rows: list[SessionProxyRow]) -> dict[tuple[str, int], int]:
    counts: dict[tuple[str, int], int] = defaultdict(int)
    for row in rows:
        if row.pool_key:
            counts[(row.pool_key, row.proxy_index)] += 1
    return counts


def format_layout_report_text(
    *,
    max_per_proxy: Optional[int] = None,
    html: bool = False,
) -> str:
    max_per = max_per_proxy if max_per_proxy is not None else config.PROXY_MAX_ACCOUNTS_PER_IP
    rows = collect_session_proxy_rows()
    if not rows:
        return "Нет session-файлов в папке sessions/."

    counts = _distribution_counts(rows)
    pools = config.load_country_proxy_pools()
    lines: list[str] = []

    def fmt(text: str) -> str:
        return f"<code>{escape(text)}</code>" if html else text

    title = "🌐 Раскладка sticky-прокси"
    lines.append(f"<b>{title}</b>" if html else title)
    lines.append("")
    lines.append(f"Лимит рекомендации: {max_per} акк. / IP")
    lines.append(f"Sticky: {'вкл' if config.SESSION_PROXY_STICKY_ENABLED else 'выкл'}")
    lines.append("")

    by_pool: dict[str, list[SessionProxyRow]] = defaultdict(list)
    for row in rows:
        key = row.pool_key or "_none"
        by_pool[key].append(row)

    warnings: list[str] = []

    for pool_key in sorted(by_pool.keys(), key=lambda k: (k == "_none", k)):
        pool_rows = by_pool[pool_key]
        if pool_key == "_none":
            lines.append(f"⚠️ Без пула прокси: {len(pool_rows)}")
            for row in pool_rows[:8]:
                lines.append(f"  • {fmt(row.session_name)}")
            if len(pool_rows) > 8:
                lines.append(f"  … и ещё {len(pool_rows) - 8}")
            lines.append("")
            continue

        pool_size = len(pools.get(pool_key, []))
        lines.append(f"<b>Пул [{pool_key}]</b> — {len(pool_rows)} акк., прокси: {pool_size}" if html else f"Пул [{pool_key}] — {len(pool_rows)} акк., прокси: {pool_size}")

        for idx in range(pool_size):
            bucket = [r for r in pool_rows if r.proxy_index == idx]
            cnt = len(bucket)
            endpoint = _proxy_endpoint(pool_key, idx, pools)
            marker = " ⚠️ ПЕРЕГРУЗ" if cnt > max_per else (" ✓" if cnt > 0 else "")
            if cnt > max_per:
                warnings.append(f"{pool_key} #{idx + 1}: {cnt} аккаунтов (лимит {max_per})")
            lines.append(f"  [{idx + 1}/{pool_size}] {fmt(endpoint)} — <b>{cnt}</b> акк.{marker}" if html else f"  [{idx + 1}/{pool_size}] {endpoint} — {cnt} акк.{marker}")
            for row in sorted(bucket, key=lambda r: r.session_name)[:6]:
                bind_mark = "" if row.has_binding else " (auto)"
                lines.append(f"      · {fmt(row.session_name)}{bind_mark}")
            if cnt > 6:
                lines.append(f"      … +{cnt - 6}")

        unassigned = [r for r in pool_rows if r.proxy_index < 0 or r.proxy_index >= pool_size]
        if unassigned:
            lines.append(f"  ⚠️ Некорректный index: {len(unassigned)}")
        lines.append("")

    if warnings:
        lines.append("<b>Предупреждения</b>" if html else "Предупреждения")
        for w in warnings:
            lines.append(f"  ⚠️ {w}")
        lines.append("")

    lines.append(f"Всего аккаунтов: {len(rows)}")
    return "\n".join(lines)


def rebalance_session_proxies(
    *,
    max_per_proxy: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """
    Равномерно распределяет аккаунты по индексам пула (least-loaded, cap max_per_proxy).
    Возвращает словарь с changes, distribution, warnings.
    """
    max_per = max_per_proxy if max_per_proxy is not None else config.PROXY_MAX_ACCOUNTS_PER_IP
    pools = config.load_country_proxy_pools()
    bindings = config.load_session_proxy_bindings()
    new_bindings = dict(bindings)
    changes: list[ProxyRebalanceChange] = []
    warnings: list[str] = []

    by_pool: dict[str, list[str]] = defaultdict(list)
    for session_path in inviter.get_session_files():
        session_name = str(session_path.relative_to(config.SESSIONS_DIR)).replace("\\", "/")
        session_key = config.normalize_session_key(session_name)
        country = inviter.detect_session_country(session_path)
        pool_key = config._resolve_pool_key_for_country(country, pools)
        if pool_key:
            by_pool[pool_key].append(session_key)

    final_counts: dict[tuple[str, int], int] = defaultdict(int)

    for pool_key, session_keys in sorted(by_pool.items()):
        pool = pools.get(pool_key, [])
        if not pool:
            continue
        pool_size = len(pool)
        counts = [0] * pool_size

        for session_key in sorted(session_keys):
            candidates = list(range(pool_size))
            candidates.sort(key=lambda i: (counts[i], i))
            chosen = candidates[0]
            if counts[chosen] >= max_per:
                warnings.append(
                    f"Пул {pool_key}: все IP заполнены (лимит {max_per}), "
                    f"{session_key} → #{chosen + 1} ({counts[chosen] + 1} акк.)",
                )
            old_binding = bindings.get(session_key, {})
            old_index: Optional[int] = None
            if isinstance(old_binding, dict) and str(old_binding.get("pool_key", "")).lower() == pool_key:
                try:
                    old_index = int(old_binding.get("index", -1))
                except (TypeError, ValueError):
                    old_index = None

            if old_index != chosen:
                short_name = session_key
                changes.append(
                    ProxyRebalanceChange(
                        session_name=short_name,
                        pool_key=pool_key,
                        old_index=old_index,
                        new_index=chosen,
                    )
                )

            new_bindings[session_key] = {"pool_key": pool_key, "index": chosen}
            counts[chosen] += 1
            final_counts[(pool_key, chosen)] += 1

    if not dry_run:
        config.save_session_proxy_bindings(new_bindings)

    return {
        "dry_run": dry_run,
        "max_per_proxy": max_per,
        "changes": changes,
        "warnings": warnings,
        "distribution": dict(final_counts),
        "changed_count": len(changes),
    }


def format_rebalance_result_text(result: dict, *, html: bool = False) -> str:
    def fmt(s: str) -> str:
        return f"<code>{escape(s)}</code>" if html else s

    mode = "dry-run" if result.get("dry_run") else "применено"
    lines = [
        f"<b>🔀 Перебалансировка прокси</b> ({mode})" if html else f"🔀 Перебалансировка прокси ({mode})",
        f"Лимит: {result.get('max_per_proxy')} акк. / IP",
        f"Изменено привязок: {result.get('changed_count', 0)}",
        "",
    ]

    dist = result.get("distribution", {})
    if dist:
        lines.append("Распределение после:")
        for (pool_key, idx), cnt in sorted(dist.items(), key=lambda x: (x[0][0], x[0][1])):
            lines.append(f"  {pool_key} #{idx + 1}: {cnt} акк.")
        lines.append("")

    changes: list[ProxyRebalanceChange] = result.get("changes", [])
    if changes:
        lines.append("Изменения:" if not html else "<b>Изменения</b>")
        for change in changes[:25]:
            old = "—" if change.old_index is None else str(change.old_index + 1)
            lines.append(
                f"  {fmt(change.session_name)}: IP {old} → {change.new_index + 1}",
            )
        if len(changes) > 25:
            lines.append(f"  … и ещё {len(changes) - 25}")
        lines.append("")

    warnings = result.get("warnings", [])
    if warnings:
        lines.append("Предупреждения:" if not html else "<b>Предупреждения</b>")
        for w in warnings[:10]:
            lines.append(f"  ⚠️ {w}")
        if len(warnings) > 10:
            lines.append(f"  … и ещё {len(warnings) - 10}")

    if not result.get("dry_run") and result.get("changed_count", 0) > 0:
        lines.append("Перезапустите инвайт/прогрев, чтобы аккаунты переподключились с новыми IP.")

    return "\n".join(lines)
