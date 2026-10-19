"""Resolve smotrim.ru video pages to a direct HLS playlist URL.

Smotrim перевёл плеер на SPA, и старый API (player.smotrim.ru/iframe/datavideo),
на который рассчитан экстрактор yt-dlp, больше не отдаёт JSON. Новый API:
player-api.smotrim.ru/api/v1/video/<id> → data.streams.m3u8. Гео-блока нет.
"""

from __future__ import annotations

import json
import logging  # noqa: TC003
import re
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.networking import Request as YtdlpRequest
from yt_dlp.networking.exceptions import HTTPError as YtdlpHTTPError
from yt_dlp.networking.exceptions import RequestError as YtdlpRequestError

from worker.ru_proxy import ru_proxy_for

_API_URL = 'https://player-api.smotrim.ru/api/v1/video/{id}'
_ORIGIN = 'https://player.smotrim.ru'
_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)
_VIDEO_PATH_RE = re.compile(r'^(?:/iframe)?/video(?:/id)?/(?P<id>\d+)')


def _raise_dl(msg: str, cause: BaseException | None = None) -> NoReturn:
    from worker.core.exceptions import MediaDownloaderError  # noqa: PLC0415

    if cause is None:
        raise MediaDownloaderError(msg)
    raise MediaDownloaderError(msg) from cause


def _video_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or '').lower().removeprefix('www.')
    if host not in ('smotrim.ru', 'player.smotrim.ru'):
        return None
    m = _VIDEO_PATH_RE.match(parts.path or '')
    return m.group('id') if m else None


@dataclass(frozen=True)
class SmotrimResolved:
    direct_url: str
    http_headers: dict[str, str]
    page_title: str | None = None


def _title(data: dict) -> str | None:
    brand = ((data.get('brand') or {}).get('title') or '').strip()
    episode = ((data.get('episode') or {}).get('title') or '').strip()
    if brand and episode and episode != brand:
        return f'{brand} — {episode}'
    return brand or episode or None


def resolve_if_smotrim(url: str, log: logging.Logger) -> SmotrimResolved | None:
    """If this is a smotrim.ru video page — return HLS playlist URL, else None."""
    video_id = _video_id(url)
    if video_id is None:
        return None

    headers = {'User-Agent': _USER_AGENT, 'Origin': _ORIGIN, 'Referer': _ORIGIN + '/'}
    api_url = _API_URL.format(id=video_id)
    log.info('Resolving Smotrim video %s', video_id)
    with yt_dlp.YoutubeDL({'proxy': ru_proxy_for(url) or '', 'quiet': True}) as ydl:
        try:
            with ydl.urlopen(YtdlpRequest(api_url, headers=headers)) as resp:
                payload = json.loads(resp.read().decode('utf-8', 'replace'))
        except YtdlpHTTPError as exc:
            _raise_dl(f'Smotrim: API недоступен (HTTP {exc.status}).', cause=exc)
        except YtdlpRequestError as exc:
            _raise_dl(f'Smotrim: ошибка сети: {exc}', cause=exc)
        except ValueError as exc:
            _raise_dl('Smotrim: API вернул не JSON.', cause=exc)

    if not isinstance(payload, dict):
        _raise_dl('Smotrim: неожиданный ответ API.')
    if payload.get('status') != 'OK':
        notice = payload.get('notice') or payload.get('status') or 'видео недоступно'
        _raise_dl(f'Smotrim: {notice}')

    data = payload.get('data') or {}
    m3u8 = (data.get('streams') or {}).get('m3u8')
    if not isinstance(m3u8, str) or not m3u8.startswith('http'):
        _raise_dl('Smotrim: в ответе API нет ссылки на видео.')

    log.info('Smotrim resolved %s → %s', url, m3u8)
    return SmotrimResolved(direct_url=m3u8, http_headers=headers, page_title=_title(data))
