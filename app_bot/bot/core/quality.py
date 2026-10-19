import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from pyrogram.enums import ParseMode
from pyrogram.errors import MessageIdInvalid, MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from yt_shared.enums import DownMediaType, VideoQuality
from yt_shared.schemas.url import URL

from bot.bot.client import VideoBotClient
from bot.core.queue_status import download_workflow_backlog_count
from bot.core.service import UrlService

CALLBACK_PREFIX: Final[str] = 'q:'
CALLBACK_REGEX: Final[str] = r'^q:'

_AUDIO_CHOICE: Final[str] = 'audio'
_CHOICE_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_QUALITY: Final[VideoQuality] = VideoQuality.P1080

_LOG_TITLE: Final[str] = '<b>Лог скачивания и обработки</b>'


def _is_rutube_url(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower()
    return host == 'rutube.ru' or host.endswith('.rutube.ru')


def _is_vk_music_url(url: str) -> bool:
    """VK Музыка: воркер сам качает такие ссылки как звук (см. vk_music_resolver)."""
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if not (host in ('vk.com', 'vk.ru') or host.endswith(('.vk.com', '.vk.ru'))):
        return False
    return 'audio' in url or parts.path.startswith('/music/')


def needs_quality_choice(url: URL) -> bool:
    if url.download_media_type not in (DownMediaType.VIDEO, DownMediaType.AUDIO_VIDEO):
        return False
    return not _is_vk_music_url(url.url)


def choice_prompt_html() -> str:
    return (
        f'{_LOG_TITLE}\n'
        f'<pre>🎞 Выберите качество '
        f'(через {int(_CHOICE_TIMEOUT_SECONDS)} с — {DEFAULT_QUALITY.label})</pre>'
    )


def queued_log_html(url: URL) -> str:
    if url.download_media_type is DownMediaType.AUDIO:
        head = '🎵 Только звук'
    elif url.video_quality is not None:
        head = f'🎞 Качество: {url.video_quality.label}'
    else:
        head = ''
    if _is_rutube_url(url.url):
        tail = '📺 Rutube: определяю длительность и качество…'
    else:
        tail = '⏳ Журнал обновляется по этапам…'
    body = f'{head}\n{tail}' if head else tail
    return f'{_LOG_TITLE}\n<pre>{body}</pre>'


def _keyboard(token: str) -> InlineKeyboardMarkup:
    def btn(text: str, choice: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text, callback_data=f'{CALLBACK_PREFIX}{token}:{choice}')

    return InlineKeyboardMarkup(
        [
            [
                btn('360p', VideoQuality.P360.value),
                btn('480p', VideoQuality.P480.value),
                btn('720p', VideoQuality.P720.value),
            ],
            [
                btn('1080p', VideoQuality.P1080.value),
                btn('Лучшее', VideoQuality.BEST.value),
                btn('🎵 Звук', _AUDIO_CHOICE),
            ],
        ]
    )


@dataclass
class _Pending:
    url: URL
    chat_id: int
    ack_message_id: int
    ack_text: str
    timeout_task: asyncio.Task | None = field(default=None)


class QualityPicker:
    """Holds URLs waiting for a quality choice and dispatches them to the worker.

    State is in-memory: after a bot restart old buttons answer "send again".
    """

    def __init__(self, client: VideoBotClient) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._client = client
        self._url_service = UrlService()
        self._pending: dict[str, _Pending] = {}
        self._timeout_tasks: set[asyncio.Task] = set()

    @staticmethod
    def keyboard_for(token: str) -> InlineKeyboardMarkup:
        return _keyboard(token)

    @staticmethod
    def new_token() -> str:
        return secrets.token_hex(6)

    def register(
        self, *, token: str, url: URL, chat_id: int, ack_message: Message, ack_text: str
    ) -> None:
        pending = _Pending(
            url=url,
            chat_id=chat_id,
            ack_message_id=ack_message.id,
            ack_text=ack_text,
        )
        self._pending[token] = pending
        task = asyncio.create_task(
            self._dispatch_on_timeout(token), name=f'quality_timeout_{token}'
        )
        self._timeout_tasks.add(task)
        task.add_done_callback(self._timeout_tasks.discard)
        pending.timeout_task = task

    async def _dispatch_on_timeout(self, token: str) -> None:
        await asyncio.sleep(_CHOICE_TIMEOUT_SECONDS)
        pending = self._pending.pop(token, None)
        if pending is None:
            return
        url = pending.url.model_copy(update={'video_quality': DEFAULT_QUALITY})
        try:
            await self.dispatch(
                url=url,
                chat_id=pending.chat_id,
                ack_message_id=pending.ack_message_id,
                ack_text=pending.ack_text,
            )
        except Exception:
            self._log.exception('Failed to dispatch URL %s after timeout', url.url)

    async def on_callback(self, client: VideoBotClient, query: CallbackQuery) -> None:  # noqa: ARG002
        data = query.data or ''
        try:
            token, choice = data.removeprefix(CALLBACK_PREFIX).split(':', 1)
        except ValueError:
            await query.answer()
            return

        pending = self._pending.get(token)
        if pending is None:
            await query.answer(
                'Кнопки устарели — пришлите ссылку ещё раз', show_alert=True
            )
            return
        owner_id = pending.url.from_user_id
        if owner_id is not None and query.from_user and query.from_user.id != owner_id:
            await query.answer('Качество выбирает тот, кто прислал ссылку')
            return

        if choice == _AUDIO_CHOICE:
            update = {'download_media_type': DownMediaType.AUDIO, 'video_quality': None}
        else:
            try:
                update = {'video_quality': VideoQuality(choice)}
            except ValueError:
                await query.answer()
                return

        del self._pending[token]
        if pending.timeout_task is not None:
            pending.timeout_task.cancel()
        url = pending.url.model_copy(update=update)
        await query.answer('Отправил на скачивание')
        await self.dispatch(
            url=url,
            chat_id=pending.chat_id,
            ack_message_id=pending.ack_message_id,
            ack_text=pending.ack_text,
        )

    async def dispatch(
        self, *, url: URL, chat_id: int, ack_message_id: int, ack_text: str
    ) -> None:
        """Mark the log message as queued, publish to worker, update queue hint."""
        if url.pipeline_log_message_id is not None:
            try:
                # Без reply_markup Telegram убирает кнопки выбора.
                await self._client.edit_message_text(
                    chat_id=chat_id,
                    message_id=url.pipeline_log_message_id,
                    text=queued_log_html(url),
                    parse_mode=ParseMode.HTML,
                )
            except (MessageNotModified, MessageIdInvalid):
                pass
            except Exception:
                self._log.debug('Failed to edit pipeline log message', exc_info=True)

        await self._url_service.process_urls([url])

        try:
            n = await download_workflow_backlog_count()
            await self._client.edit_message_text(
                chat_id=chat_id,
                message_id=ack_message_id,
                text=f'{ack_text}\n\n📊 <b>Очередь</b>: сейчас ~{n} задач(и) впереди (включая эту)',
                parse_mode=ParseMode.HTML,
            )
        except MessageNotModified:
            pass
        except Exception:
            self._log.exception('Failed to update queue hint on ack message')
