import html

from pyrogram.enums import ParseMode
from pyrogram.errors import MessageIdInvalid, MessageNotModified
from yt_shared.rabbit.rabbit_config import PROGRESS_QUEUE
from yt_shared.schemas.progress import DownloadProgressPayload

from bot.core.pulse import get_pulse_message, remember_pulse_message
from bot.core.workers.abstract import AbstractDownloadResultWorker
from bot.core.workers.enums import RabbitWorkerType


class ProgressDownloadResultWorker(AbstractDownloadResultWorker):
    TYPE = RabbitWorkerType.PROGRESS
    QUEUE_TYPE = PROGRESS_QUEUE
    SCHEMA_CLS = (DownloadProgressPayload,)

    async def _process_body(self, body: DownloadProgressPayload) -> bool:
        detail = (body.line or '').strip()
        log_block = ''
        if detail:
            safe = html.escape(detail[:3500])
            log_block = f'<b>Лог скачивания и обработки</b>\n<pre>{safe}</pre>'

        if body.pipeline_log_message_id:
            if log_block:
                try:
                    await self._bot.edit_message_text(
                        chat_id=body.from_chat_id,
                        message_id=body.pipeline_log_message_id,
                        text=log_block[:4090],
                        parse_mode=ParseMode.HTML,
                    )
                except MessageNotModified:
                    pass
                except MessageIdInvalid:
                    self._log.debug('Progress log edit skipped (message removed)')
                except Exception:
                    self._log.debug('Progress log edit failed', exc_info=True)
            if body.pulse_text:
                await self._upsert_pulse(body)
        else:
            url = html.escape(body.url[:220])
            if detail:
                block = f'<pre>{html.escape(detail[:3500])}</pre>'
            else:
                block = ''
            text = f'⬇️ <b>Загрузка</b>\n<code>{url}</code>\n{block}'
            try:
                await self._bot.edit_message_text(
                    chat_id=body.from_chat_id,
                    message_id=body.ack_message_id,
                    text=text[:4090],
                    parse_mode=ParseMode.HTML,
                )
            except MessageNotModified:
                pass
            except MessageIdInvalid:
                self._log.debug('Progress edit skipped (message removed)')
            except Exception:
                self._log.debug('Progress edit failed', exc_info=True)
        return True

    async def _upsert_pulse(self, body: DownloadProgressPayload) -> None:
        """Один пульс на задачу: правим прежнее сообщение, новое — только если его нет."""
        text = body.pulse_text[:4090]
        existing = get_pulse_message(body.task_id)
        if existing:
            chat_id, message_id = existing
            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
                return
            except MessageNotModified:
                return
            except Exception:
                self._log.debug('Progress pulse edit failed, sending new', exc_info=True)
        try:
            msg = await self._bot.send_message(
                chat_id=body.from_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=(body.pipeline_log_message_id or body.ack_message_id),
            )
            remember_pulse_message(body.task_id, body.from_chat_id, msg.id)
        except Exception:
            self._log.debug('Progress pulse send failed', exc_info=True)
