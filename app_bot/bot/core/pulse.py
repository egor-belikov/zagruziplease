"""Сообщение-«пульс» долгой загрузки («⏳ Ещё качаю…»): одно на задачу.

Бот правит его на месте вместо новых сообщений и удаляет, когда задача
завершилась (успех или ошибка) — иначе в чате остаётся устаревший прогресс.
"""

import logging
import uuid

from pyrogram import Client

_log = logging.getLogger(__name__)

# task_id → (chat_id, message_id). Живёт в памяти процесса бота; после
# рестарта бота старый пульс не удалится, но и новых дублей не будет.
_PULSE_MESSAGES: dict[str, tuple[int, int]] = {}


def get_pulse_message(task_id: uuid.UUID | str) -> tuple[int, int] | None:
    return _PULSE_MESSAGES.get(str(task_id))


def remember_pulse_message(task_id: uuid.UUID | str, chat_id: int, message_id: int) -> None:
    _PULSE_MESSAGES[str(task_id)] = (chat_id, message_id)


async def delete_pulse_message(bot: Client, task_id: uuid.UUID | str | None) -> None:
    if task_id is None:
        return
    entry = _PULSE_MESSAGES.pop(str(task_id), None)
    if not entry:
        return
    chat_id, message_id = entry
    try:
        await bot.delete_messages(chat_id=chat_id, message_ids=message_id)
    except Exception:
        _log.debug('Pulse message delete failed', exc_info=True)
