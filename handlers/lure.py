"""Discard a spare inventory item to make one chance at a wild Huecrab event."""

import logging
import random
from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from config import LURE_HUECRAB_CHANCE
from database import consume_random_unequipped_duel_inventory_item
from handlers.duel_items import get_duel_item_name
from handlers.duel_messaging import send_and_schedule
from handlers.huecrab import spawn_huecrab_event
from text_resources import get_text


async def _send_lure_response(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        await send_and_schedule(update, context, text)
    except Exception:
        logging.exception(
            "Could not publish /lure response in chat %s", update.effective_chat.id,
        )


async def lure_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_user is None:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    try:
        status, item = consume_random_unequipped_duel_inventory_item(
            chat_id, user_id, random.choice,
        )
    except Exception:
        logging.exception(
            "Could not consume /lure inventory item in chat %s for %s", chat_id, user_id,
        )
        return

    if status == "not_registered":
        await _send_lure_response(update, context, get_text("duel.item_event.not_registered"))
        return
    if status == "no_item":
        await _send_lure_response(update, context, get_text("lure.no_item"))
        return

    await _send_lure_response(
        update,
        context,
        get_text("lure.thrown", item=escape(get_duel_item_name(item["item_id"]))),
    )
    if random.random() < LURE_HUECRAB_CHANCE:
        await spawn_huecrab_event(context, chat_id, roll_for_spawn=False)
