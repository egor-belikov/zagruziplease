"""/music <query>: VK Music search with pages, sorting and /musicN download links.

State is in-memory, one search per chat: /musicN always refers to the numbering
of the last search message in that chat. After a bot restart old buttons answer
"search again".
"""

import asyncio
import html
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Final

from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from yt_shared.enums import DownMediaType, TelegramChatType
from yt_shared.schemas.url import URL
from yt_shared.vk_music import VkApiError, format_duration, vk_api, vk_track_url

from bot.bot.client import VideoBotClient
from bot.core.quality import QualityPicker, queued_log_html
from bot.core.utils import bold

CALLBACK_PREFIX: Final[str] = 'mu:'
CALLBACK_REGEX: Final[str] = r'^mu:'
DOWNLOAD_REGEX: Final[str] = r'^/music(\d+)(?:@\w+)?\s*$'

_SEARCH_COUNT: Final[int] = 200
_PAGE_SIZE: Final[int] = 10

_SORT_RELEVANCE: Final[str] = 'rel'
_SORT_LONG: Final[str] = 'long'
_SORT_SHORT: Final[str] = 'short'
_SORT_LABELS: Final[dict[str, str]] = {
    _SORT_RELEVANCE: '🎯 Релевантность',
    _SORT_LONG: '⏱ Длинные',
    _SORT_SHORT: '⏱ Короткие',
}


@dataclass(frozen=True)
class _Track:
    artist: str
    title: str
    duration: int
    url: str

    @property
    def full_title(self) -> str:
        return f'{self.artist} — {self.title}' if self.artist else self.title


@dataclass
class _Search:
    token: str
    query: str
    tracks: list[_Track]
    sort: str = _SORT_RELEVANCE
    page: int = 0

    def view(self) -> list[_Track]:
        if self.sort == _SORT_LONG:
            return sorted(self.tracks, key=lambda t: t.duration, reverse=True)
        if self.sort == _SORT_SHORT:
            return sorted(self.tracks, key=lambda t: t.duration)
        return self.tracks

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.tracks) // _PAGE_SIZE))


def _search_vk(query: str) -> list[_Track]:
    resp = vk_api('audio.search', {'q': query, 'count': _SEARCH_COUNT})
    return [
        _Track(
            artist=(item.get('artist') or '').strip(),
            title=(item.get('title') or '').strip() or 'Без названия',
            duration=int(item.get('duration') or 0),
            url=vk_track_url(item),
        )
        for item in resp.get('items') or []
        if item.get('url')
    ]


def _render(search: _Search) -> str:
    view = search.view()
    start = search.page * _PAGE_SIZE
    lines = [
        f'🔎 {bold("VK Музыка:")} {html.escape(search.query)}',
        f'Найдено: {len(view)} · {_SORT_LABELS[search.sort]}',
        '',
    ]
    for num, track in enumerate(view[start : start + _PAGE_SIZE], start=start + 1):
        artist = f'{bold(html.escape(track.artist))} — ' if track.artist else ''
        lines.append(
            f'{num}. {artist}{html.escape(track.title)} '
            f'<i>({format_duration(track.duration)})</i> /music{num}'
        )
    lines += ['', 'Чтобы скачать трек, нажми команду справа от него.']
    return '\n'.join(lines)


def _keyboard(search: _Search) -> InlineKeyboardMarkup:
    def btn(text: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text, callback_data=f'{CALLBACK_PREFIX}{search.token}:{action}'
        )

    last = search.pages - 1
    nav = [
        btn('⏮', 'p:0'),
        btn('◀️', f'p:{max(search.page - 1, 0)}'),
        btn(f'{search.page + 1}/{search.pages}', 'noop'),
        btn('▶️', f'p:{min(search.page + 1, last)}'),
        btn('⏭', f'p:{last}'),
    ]
    sorts = [
        btn(f'✓ {label}' if mode == search.sort else label, f's:{mode}')
        for mode, label in _SORT_LABELS.items()
    ]
    return InlineKeyboardMarkup([nav, sorts])


class MusicSearch:
    def __init__(self, quality_picker: QualityPicker) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._picker = quality_picker
        self._by_chat: dict[int, _Search] = {}

    async def on_search(self, client: VideoBotClient, message: Message) -> None:  # noqa: ARG002
        query = ' '.join(message.command[1:]).strip() if message.command else ''
        if not query:
            await message.reply(
                f'{bold("Поиск музыки ВК:")} /music название песни',
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message.id,
            )
            return
        try:
            tracks = await asyncio.to_thread(_search_vk, query)
        except VkApiError as err:
            await message.reply(f'🛑 {html.escape(str(err))}', reply_to_message_id=message.id)
            return
        except Exception:
            self._log.exception('VK music search failed for %r', query)
            await message.reply('🛑 Поиск ВК не удался, попробуй ещё раз', reply_to_message_id=message.id)
            return
        if not tracks:
            await message.reply('Ничего не нашлось 🤷', reply_to_message_id=message.id)
            return

        search = _Search(token=secrets.token_hex(4), query=query, tracks=tracks)
        self._by_chat[message.chat.id] = search
        await message.reply(
            _render(search),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.id,
            reply_markup=_keyboard(search),
            disable_web_page_preview=True,
        )

    async def on_callback(self, client: VideoBotClient, query: CallbackQuery) -> None:  # noqa: ARG002
        try:
            token, action = (query.data or '').removeprefix(CALLBACK_PREFIX).split(':', 1)
        except ValueError:
            await query.answer()
            return
        search = self._by_chat.get(query.message.chat.id) if query.message else None
        if search is None or search.token != token:
            await query.answer('Поиск устарел — набери /music ещё раз', show_alert=True)
            return

        kind, _, value = action.partition(':')
        if kind == 'p' and value.isdigit():
            search.page = min(int(value), search.pages - 1)
        elif kind == 's' and value in _SORT_LABELS:
            search.sort = value
            search.page = 0
        else:
            await query.answer()
            return
        await query.answer()
        try:
            await query.message.edit_text(
                _render(search),
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(search),
                disable_web_page_preview=True,
            )
        except MessageNotModified:
            pass

    async def on_download(self, client: VideoBotClient, message: Message) -> None:  # noqa: ARG002
        match = re.match(DOWNLOAD_REGEX, message.text or '')
        search = self._by_chat.get(message.chat.id)
        if not match or search is None:
            await message.reply(
                'Сначала найди трек: /music название песни',
                reply_to_message_id=message.id,
            )
            return
        view = search.view()
        num = int(match.group(1))
        if not 1 <= num <= len(view):
            await message.reply(
                f'Нет трека №{num}, в поиске их {len(view)}',
                reply_to_message_id=message.id,
            )
            return
        track = view[num - 1]

        ack_text = f'🎵 {bold("Скачиваю:")} {html.escape(track.full_title)}'
        ack_message = await message.reply(
            ack_text, parse_mode=ParseMode.HTML, reply_to_message_id=message.id
        )
        url = URL(
            url=track.url,
            original_url=track.url,
            from_chat_id=message.chat.id,
            from_chat_type=TelegramChatType(message.chat.type.value),
            from_user_id=message.from_user.id if message.from_user else None,
            message_id=message.id,
            ack_message_id=ack_message.id,
            save_to_storage=False,
            download_media_type=DownMediaType.AUDIO,
        )
        log_msg = await message.reply(
            queued_log_html(url),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.id,
        )
        await self._picker.dispatch(
            url=url.model_copy(update={'pipeline_log_message_id': log_msg.id}),
            chat_id=message.chat.id,
            ack_message_id=ack_message.id,
            ack_text=ack_text,
        )
