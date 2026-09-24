"""Paid daily digging for a shared duel-item pickup event."""

import logging
import random
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import (
    cancel_unpublished_duel_dig,
    format_user_title,
    set_duel_item_event_message,
    try_duel_dig,
)
from handlers.duel_items import (
    DUEL_ITEMS,
    DUEL_ITEM_EVENT_CALLBACK_PREFIX,
    get_duel_item_name,
)
from handlers.duel_messaging import schedule_auto_delete
from text_resources import get_text


DIG_MESSAGE_DELETE_DELAY = 10


def _schedule_cleanup(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        schedule_auto_delete(context, chat_id, [message_id], delay=DIG_MESSAGE_DELETE_DELAY)
    except Exception:
        logging.exception("Could not schedule /dig message deletion in chat %s", chat_id)


async def dig_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_user is None:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if update.message is not None:
        _schedule_cleanup(context, chat_id, update.message.message_id)
    try:
        status, dig = try_duel_dig(
            chat_id, user_id, random.random,
            lambda: random.choice(DUEL_ITEMS)["id"],
        )
    except Exception:
        logging.exception("Не удалось провести раскопку в чате %s для %s", chat_id, user_id)
        return

    if status == "not_registered":
        text = get_text("duel.item_event.not_registered")
    elif status == "insufficient_points":
        text = get_text("dig.insufficient_points")
    elif status == "daily_limit":
        text = get_text("dig.daily_limit")
    elif status == "active_event":
        text = get_text("dig.active_event")
    elif status == "miss":
        text = get_text("dig.miss", remaining=dig["remaining"], points=dig["points"])
    else:
        await _publish_dig_find(context, dig)
        return

    response = await context.bot.send_message(chat_id=chat_id, text=text)
    _schedule_cleanup(context, chat_id, response.message_id)


async def _publish_dig_find(context: ContextTypes.DEFAULT_TYPE, dig: dict) -> None:
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
        get_text("duel.item_event.button"),
        callback_data=f"{DUEL_ITEM_EVENT_CALLBACK_PREFIX}{dig['event_id']}",
    )]])
    message = None
    try:
        text = get_text(
            "dig.found",
            user=format_user_title(dig["user"]),
            item=escape(get_duel_item_name(dig["item_id"])),
            remaining=dig["remaining"],
            points=dig["points"],
        )
        message = await context.bot.send_message(
            chat_id=dig["chat_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        if not set_duel_item_event_message(dig["event_id"], message.message_id, text):
            raise RuntimeError("Could not bind dig item to pickup message")
    except Exception:
        logging.exception("Не удалось опубликовать находку /dig в чате %s", dig["chat_id"])
        refunded = False
        try:
            refunded = cancel_unpublished_duel_dig(
                dig, message.message_id if message is not None else None,
            )
            if not refunded:
                logging.error("Не удалось компенсировать находку /dig %s", dig["event_id"])
        except Exception:
            logging.exception("Ошибка компенсации находки /dig %s", dig["event_id"])
        if refunded and message is not None:
            try:
                await context.bot.delete_message(dig["chat_id"], message.message_id)
            except Exception:
                logging.exception("Не удалось удалить недоступную кнопку /dig %s", dig["event_id"])
