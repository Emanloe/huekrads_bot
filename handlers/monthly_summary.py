"""Current and previous Moscow-calendar-month statistics for duel chats."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from config import DUEL_TIMEZONE
from database import (
    get_duel_item_event_chat_ids as get_registered_duel_chat_ids,
    get_monthly_chat_stats,
    moscow_month_key,
)
from text_resources import get_text


def previous_moscow_month_key(when: datetime | None = None) -> str:
    instant = when if when is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Previous month requires a timezone-aware instant")
    first_of_month = instant.astimezone(ZoneInfo(DUEL_TIMEZONE)).replace(day=1)
    return moscow_month_key(first_of_month - timedelta(days=1))


def format_monthly_summary(chat_id: int, month: str) -> str:
    return get_text("summary.monthly", **get_monthly_chat_stats(chat_id, month))


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id,
        text=format_monthly_summary(chat_id, moscow_month_key()),
    )


async def monthly_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    month = previous_moscow_month_key()
    try:
        chat_ids = get_registered_duel_chat_ids()
    except Exception:
        logging.exception("Не удалось получить чаты для месячной сводки")
        return

    for chat_id in chat_ids:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=format_monthly_summary(chat_id, month),
            )
        except Exception:
            logging.exception("Не удалось отправить месячную сводку в чат %s", chat_id)
