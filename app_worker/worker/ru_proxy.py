"""Российский прокси для гео-блокированных русских источников.

Воркер живёт в NL, а часть русских сайтов (Матч ТВ и т.п.) отдаёт видео только
на российские IP. RU_PROXY — SOCKS5 на egorvps (socks5h://user:pass@host:port).
Через него идут только хосты из _RU_GEO_HOST_SUFFIXES и RU_PROXY_HOSTS.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

_RU_GEO_HOST_SUFFIXES: frozenset[str] = frozenset(
    (
        'matchtv.ru',
        'smotrim.ru',
        '1tv.ru',
        'ntv.ru',
        'sportbox.ru',
    )
)


def get_ru_proxy() -> str | None:
    return (os.environ.get('RU_PROXY') or '').strip() or None


def _extra_hosts() -> set[str]:
    raw = os.environ.get('RU_PROXY_HOSTS') or ''
    return {h.strip().lower().removeprefix('.') for h in raw.split(',') if h.strip()}


def is_ru_geo_url(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower()
    if not host:
        return False
    return any(
        host == suffix or host.endswith('.' + suffix)
        for suffix in _RU_GEO_HOST_SUFFIXES | _extra_hosts()
    )


def ru_proxy_for(*urls: str) -> str | None:
    """RU_PROXY, если хоть один из адресов — русский гео-блокированный источник."""
    proxy = get_ru_proxy()
    if proxy and any(is_ru_geo_url(u) for u in urls if u):
        return proxy
    return None
