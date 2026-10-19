"""VK Music: expand playlists into per-track tasks and resolve track stream URLs.

yt-dlp has no VK audio extractor and VK serves music only to signed-in users,
so we go through the VK API (yt_shared.vk_music). Each playlist track becomes
a separate task with a synthetic URL https://vk.com/audio{owner}_{id}_{key};
the stream URL is resolved right before download because VK links expire quickly.
"""

from __future__ import annotations

import logging  # noqa: TC003
import os
from dataclasses import dataclass
from typing import NoReturn

from yt_shared.vk_music import (
    USER_AGENT,
    VkApiError,
    is_vk_music_url,
    parse_vk_playlist,
    parse_vk_track,
    vk_api,
    vk_track_url,
)

__all__ = (
    'is_vk_music_url',
    'is_vk_playlist_url',
    'list_vk_playlist_track_urls',
    'resolve_if_vk_audio',
)

_PAGE_SIZE = 200
_DEFAULT_MAX_TRACKS = 200


def _raise_dl(msg: str) -> NoReturn:
    from worker.core.exceptions import MediaDownloaderError  # noqa: PLC0415

    raise MediaDownloaderError(msg)


def _max_tracks() -> int:
    raw = (os.environ.get('VK_MUSIC_MAX_TRACKS') or '').strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else _DEFAULT_MAX_TRACKS


def _api(method: str, params: dict[str, str | int]) -> dict | list:
    try:
        return vk_api(method, params)
    except VkApiError as err:
        _raise_dl(str(err))


def is_vk_playlist_url(url: str) -> bool:
    return parse_vk_playlist(url) is not None


def list_vk_playlist_track_urls(url: str, log: logging.Logger) -> list[str]:
    parsed = parse_vk_playlist(url)
    if parsed is None:
        return []
    owner_id, playlist_id, access_key = parsed
    limit = _max_tracks()
    urls: list[str] = []
    offset = 0
    while len(urls) < limit:
        params: dict[str, str | int] = {
            'owner_id': owner_id,
            'album_id': playlist_id,
            'count': _PAGE_SIZE,
            'offset': offset,
        }
        if access_key:
            params['access_key'] = access_key
        resp = _api('audio.get', params)
        items = resp.get('items') or []
        for item in items:
            if item.get('url'):
                urls.append(vk_track_url(item))
            else:
                log.info(
                    'VK track %s - %s unavailable, skipped',
                    item.get('artist'),
                    item.get('title'),
                )
        offset += len(items)
        if not items or offset >= int(resp.get('count') or 0):
            break
    if not urls:
        _raise_dl('VK Музыка: в плейлисте нет доступных треков.')
    log.info('VK playlist %s_%s: %s track(s)', owner_id, playlist_id, len(urls))
    return urls[:limit]


@dataclass(frozen=True)
class VkAudioResolved:
    direct_url: str
    artist: str
    title: str
    duration: float | None
    http_headers: dict[str, str]

    @property
    def full_title(self) -> str:
        return f'{self.artist} - {self.title}' if self.artist else self.title


def resolve_if_vk_audio(url: str, log: logging.Logger) -> VkAudioResolved | None:
    audio_id = parse_vk_track(url)
    if audio_id is None:
        return None
    resp = _api('audio.getById', {'audios': audio_id})
    item = resp[0] if resp else None
    if not item or not item.get('url'):
        _raise_dl('VK Музыка: трек недоступен (удалён или закрыт правообладателем).')
    log.info('VK audio %s resolved', audio_id)
    duration = item.get('duration')
    return VkAudioResolved(
        direct_url=item['url'],
        artist=(item.get('artist') or '').strip(),
        title=(item.get('title') or 'VK audio').strip(),
        duration=float(duration) if duration else None,
        http_headers={'User-Agent': USER_AGENT},
    )
