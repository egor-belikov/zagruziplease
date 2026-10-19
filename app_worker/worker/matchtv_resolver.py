"""Resolve matchtv.ru video pages to a direct HLS playlist URL.

Страница → playerUrl (iframe video.matchtv.ru) → data-config фида Webcaster
(bl.video.matchtv.ru/feed/…) → media XML → <iphone><track> master m3u8.
bl.video.matchtv.ru отдаёт media/плейлисты только российским IP — нужен RU_PROXY.
Сегменты с CDN (cdn-vod.video.matchtv.ru) гео-блока не имеют.
"""

from __future__ import annotations

import html
import logging  # noqa: TC003
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import unquote, urlsplit

import yt_dlp
from yt_dlp.networking import Request as YtdlpRequest
from yt_dlp.networking.exceptions import HTTPError as YtdlpHTTPError
from yt_dlp.networking.exceptions import RequestError as YtdlpRequestError

from worker.ru_proxy import get_ru_proxy

_REFERER = 'https://matchtv.ru/'
_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

_IFRAME_RE = re.compile(
    r'https?:(?://|\\u002F\\u002F)video\.matchtv\.ru(?:/|\\u002F)iframe'
    r'(?:/|\\u002F)[^"\'\s<>]+'
)
_CONFIG_RE = re.compile(
    r'data-config=["\']config=(?P<url>https?://bl\.video\.matchtv\.ru/feed/[^"\']+)'
)


def _raise_dl(msg: str, cause: BaseException | None = None) -> NoReturn:
    from worker.core.exceptions import MediaDownloaderError  # noqa: PLC0415

    if cause is None:
        raise MediaDownloaderError(msg)
    raise MediaDownloaderError(msg) from cause


def _host(url: str) -> str:
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')


def is_matchtv_video_url(url: str) -> bool:
    host = _host(url)
    path = urlsplit(url).path or ''
    if host == 'matchtv.ru':
        return 'matchtvvideo_' in path
    return host == 'video.matchtv.ru' and path.startswith('/iframe/')


@dataclass(frozen=True)
class MatchTvResolved:
    direct_url: str
    http_headers: dict[str, str]
    proxy: str | None
    page_title: str | None = None


def _fetch(ydl: yt_dlp.YoutubeDL, url: str, what: str) -> str:
    req = YtdlpRequest(url, headers={'User-Agent': _USER_AGENT, 'Referer': _REFERER})
    try:
        with ydl.urlopen(req) as resp:
            return resp.read().decode('utf-8', 'replace')
    except YtdlpHTTPError as exc:
        body = ''
        try:
            body = exc.response.read().decode('utf-8', 'replace')
        except Exception:  # noqa: BLE001
            pass
        if 'территории' in body:
            _raise_dl(_geo_error(), cause=exc)
        _raise_dl(f'Матч ТВ: {what} недоступен (HTTP {exc.status}).', cause=exc)
    except YtdlpRequestError as exc:
        _raise_dl(f'Матч ТВ: ошибка сети ({what}): {exc}', cause=exc)


def _geo_error() -> str:
    if get_ru_proxy():
        return 'Матч ТВ: видео недоступно даже через российский прокси (RU_PROXY).'
    return 'Матч ТВ отдаёт видео только на российские IP — не задан RU_PROXY.'


def _xml_text(root: ET.Element, path: str) -> str | None:
    el = root.find(path)
    if el is None or not (el.text or '').strip():
        return None
    return el.text.strip()


def resolve_if_matchtv(url: str, log: logging.Logger) -> MatchTvResolved | None:
    """If this is a matchtv.ru video page — return HLS master URL, else None."""
    if not is_matchtv_video_url(url):
        return None

    proxy = get_ru_proxy()
    with yt_dlp.YoutubeDL({'proxy': proxy or '', 'quiet': True}) as ydl:
        iframe_url = url
        if _host(url) == 'matchtv.ru':
            page = _fetch(ydl, url, 'страница')
            m = _IFRAME_RE.search(page)
            if not m:
                _raise_dl('Матч ТВ: на странице не найден видеоплеер.')
            iframe_url = m.group(0).replace('\\u002F', '/').replace('\\u0026', '&')
            iframe_url = html.unescape(iframe_url)

        iframe = _fetch(ydl, iframe_url, 'плеер')
        m = _CONFIG_RE.search(iframe)
        if not m:
            _raise_dl('Матч ТВ: не найден конфиг плеера.')
        feed_url = unquote(html.unescape(m.group('url')))
        media_url = feed_url.replace('/feed/start/', '/media/start/', 1)

        log.info('Resolving Match TV media %s via proxy=%s', media_url, bool(proxy))
        xml_text = _fetch(ydl, media_url, 'медиа-фид')

    try:
        root = ET.fromstring(xml_text.encode())
    except ET.ParseError as exc:
        _raise_dl('Матч ТВ: не удалось разобрать ответ плеера.', cause=exc)

    if root.tag == 'warning':
        msg = _xml_text(root, 'message') or ''
        if 'территории' in msg:
            _raise_dl(_geo_error())
        _raise_dl(f'Матч ТВ: {msg or "видео недоступно"}')

    direct_url = _xml_text(root, './/iphone/track')
    if not direct_url:
        _raise_dl('Матч ТВ: в ответе плеера нет ссылки на видео.')

    log.info('Match TV resolved %s → %s', url, direct_url)
    return MatchTvResolved(
        direct_url=direct_url,
        http_headers={'Referer': _REFERER, 'User-Agent': _USER_AGENT},
        proxy=proxy,
        page_title=_xml_text(root, 'event_name'),
    )
