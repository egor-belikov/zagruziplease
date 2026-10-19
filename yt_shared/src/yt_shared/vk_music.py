"""Minimal VK API client for VK Music, shared by the bot (search) and worker (download).

VK serves music only to signed-in users, has no public audio API, and yt-dlp has
no VK audio extractor. So we act as the VK web client: with the browser session
cookies from VK_MUSIC_COOKIES ("remixsid=...; p=...") login.vk.ru issues a
short-lived web token (app 6287487) that the audio.* methods accept. The token
is cached and re-issued from the cookies when it expires.
"""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Final

API_URL: Final[str] = 'https://api.vk.ru/method/'
API_VERSION: Final[str] = '5.274'
USER_AGENT: Final[str] = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
)
_WEB_APP_ID: Final[str] = '6287487'
_WEB_TOKEN_URL: Final[str] = 'https://login.vk.ru/?act=web_token'
_TIMEOUT: Final[int] = 30
_TOKEN_REFRESH_MARGIN: Final[int] = 120
_AUTH_ERROR_CODES: Final[frozenset[int]] = frozenset({5, 1114, 1117})

_VK_HOSTS: Final[frozenset[str]] = frozenset(
    {'vk.com', 'vk.ru', 'm.vk.com', 'm.vk.ru', 'www.vk.com', 'www.vk.ru'}
)
_PLAYLIST_RES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r'audio_playlist(?P<owner>-?\d+)_(?P<id>\d+)(?:[_/](?P<key>[0-9a-f]+))?'),
    re.compile(
        r'/music/(?:playlist|album)/(?P<owner>-?\d+)_(?P<id>\d+)(?:_(?P<key>[0-9a-f]+))?'
    ),
)
_TRACK_RE: Final[re.Pattern[str]] = re.compile(
    r'^/audio(?P<owner>-?\d+)_(?P<id>\d+)(?:_(?P<key>[0-9a-f]+))?/?$'
)


class VkApiError(Exception):
    pass


def vk_music_proxy() -> str | None:
    """RU_PROXY: из-за рубежа ВК прячет лицензионный каталог и не отдаёт mp3."""
    return (os.environ.get('RU_PROXY') or '').strip() or None


def _opener() -> urllib.request.OpenerDirector:
    proxy = vk_music_proxy()
    if not proxy:
        return urllib.request.build_opener()
    import socks  # noqa: PLC0415
    from sockshandler import SocksiPyHandler  # noqa: PLC0415

    p = urllib.parse.urlsplit(proxy)
    return urllib.request.build_opener(
        SocksiPyHandler(
            socks.SOCKS5,
            p.hostname,
            p.port or 1080,
            True,
            urllib.parse.unquote(p.username or '') or None,
            urllib.parse.unquote(p.password or '') or None,
        )
    )


def _post_json(url: str, params: dict, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(  # noqa: S310
        url,
        data=urllib.parse.urlencode(params).encode(),
        headers={'User-Agent': USER_AGENT, 'Origin': 'https://vk.ru', **headers},
    )
    with _opener().open(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))


class _WebToken:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self) -> str:
        with self._lock:
            if self._token is None or time.time() >= self._expires_at:
                self._token, self._expires_at = self._issue()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    @staticmethod
    def _issue() -> tuple[str, float]:
        cookies = (os.environ.get('VK_MUSIC_COOKIES') or '').strip()
        if not cookies:
            raise VkApiError(
                'VK Музыка: не задан VK_MUSIC_COOKIES в envs/secrets.env '
                '(см. scripts/vk_music_cookies.py).'
            )
        data = _post_json(
            _WEB_TOKEN_URL,
            {'version': 1, 'app_id': _WEB_APP_ID},
            {'Referer': 'https://vk.ru/', 'Cookie': cookies},
        )
        if data.get('type') != 'okay':
            raise VkApiError(
                'VK Музыка: сессия ВК недействительна, обнови VK_MUSIC_COOKIES '
                f'(scripts/vk_music_cookies.py). Ответ ВК: {data.get("error_info")}'
            )
        token = data['data']['access_token']
        expires = float(data['data'].get('expires') or time.time() + 600)
        return token, expires - _TOKEN_REFRESH_MARGIN


_web_token = _WebToken()


def _call(method: str, params: dict[str, str | int]) -> dict:
    return _post_json(
        f'{API_URL}{method}?client_id={_WEB_APP_ID}',
        {**params, 'access_token': _web_token.get(), 'v': API_VERSION},
        {},
    )


def vk_api(method: str, params: dict[str, str | int]) -> dict | list:
    """Blocking VK API call; returns the `response` field or raises VkApiError."""
    data = _call(method, params)
    if data.get('error', {}).get('error_code') in _AUTH_ERROR_CODES:
        _web_token.invalidate()
        data = _call(method, params)
    if 'error' in data:
        err = data['error']
        raise VkApiError(
            f'VK API {method}: {err.get("error_msg")} (код {err.get("error_code")})'
        )
    return data['response']


def _is_vk_host(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme in ('http', 'https') and parsed.netloc.lower() in _VK_HOSTS


def parse_vk_playlist(url: str) -> tuple[str, str, str | None] | None:
    """Return (owner_id, playlist_id, access_key) for a VK playlist URL."""
    if not _is_vk_host(url):
        return None
    unquoted = urllib.parse.unquote(url)
    for regex in _PLAYLIST_RES:
        m = regex.search(unquoted)
        if m:
            key = m.group('key')
            if not key:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                key = (query.get('access_key') or [None])[0]
            return m.group('owner'), m.group('id'), key
    return None


def parse_vk_track(url: str) -> str | None:
    """Return 'owner_id_key' audio id for audio.getById, or None."""
    if not _is_vk_host(url):
        return None
    m = _TRACK_RE.match(urllib.parse.urlsplit(url).path)
    if not m:
        return None
    return '_'.join(p for p in (m.group('owner'), m.group('id'), m.group('key')) if p)


def is_vk_music_url(url: str) -> bool:
    return parse_vk_playlist(url) is not None or parse_vk_track(url) is not None


def vk_track_url(item: dict) -> str:
    """Page URL of a track that the worker resolves via audio.getById."""
    key = item.get('access_key')
    suffix = f'_{key}' if key else ''
    return f'https://vk.com/audio{item["owner_id"]}_{item["id"]}{suffix}'


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return '?:??'
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'
