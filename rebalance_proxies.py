#!/usr/bin/env python3
"""
Перебалансировка session_proxy_bindings.json.

Примеры:
  python rebalance_proxies.py --report-only
  python rebalance_proxies.py --dry-run
  python rebalance_proxies.py --max-per-proxy 5
  python rebalance_proxies.py --apply
"""

from __future__ import annotations

import argparse
import sys
from contextlib import suppress

import config
import proxy_layout


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        with suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Показать раскладку sticky-прокси и/или перебалансировать привязки аккаунтов.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Только отчёт, без изменений (по умолчанию если не указан --apply)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что изменится, без записи в session_proxy_bindings.json",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Применить перебалансировку",
    )
    parser.add_argument(
        "--max-per-proxy",
        type=int,
        default=None,
        help=f"Макс. аккаунтов на один IP (по умолчанию {config.PROXY_MAX_ACCOUNTS_PER_IP} или PROXY_MAX_ACCOUNTS_PER_IP из .env)",
    )
    args = parser.parse_args()

    max_per = args.max_per_proxy if args.max_per_proxy is not None else config.PROXY_MAX_ACCOUNTS_PER_IP

    print(proxy_layout.format_layout_report_text(max_per_proxy=max_per, html=False))
    print()

    if args.report_only and not args.dry_run and not args.apply:
        return 0

    dry_run = args.dry_run or not args.apply
    if not args.apply and not args.dry_run:
        print("Подсказка: добавьте --dry-run для предпросмотра или --apply для записи.")
        return 0

    result = proxy_layout.rebalance_session_proxies(max_per_proxy=max_per, dry_run=dry_run)
    print(proxy_layout.format_rebalance_result_text(result, html=False))

    if result.get("warnings"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
