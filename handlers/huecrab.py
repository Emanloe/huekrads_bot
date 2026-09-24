"""Wild Huecrab events and recoverable Telegram item auto-loot."""

import logging
import random
from datetime import date
from html import escape
from time import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import (
    HUECRAB_AUTOLOOT_DELAY_SECONDS, HUECRAB_EVENT_CHANCE,
    HUECRAB_EVENT_DAILY_LIMIT, HUECRAB_TAME_CHANCE,
)
from database import (
    bind_huecrab_event, claim_due_item_for_huecrab, create_huecrab_event,
    discard_unpublished_huecrab_event, format_user_title,
    get_duel_dwarf_name, get_duel_item_event_chat_ids, has_active_huecrab_event,
    list_due_huecrab_item_events, list_unannounced_huecrab_claims,
    mark_huecrab_claim_announced, tame_huecrab_event,
)
from handlers.duel_items import DUEL_ITEMS, get_duel_item_name
from text_resources import get_text


HUECRAB_CALLBACK_PREFIX = "huecrab_tame_"
HUECRAB_CHECK_MINUTES = 60
HUECRAB_DAILY_SPAWNS: dict[int, tuple[date, int]] = {}


async def spawn_huecrab_event(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    today = date.today()
    last_date, count = HUECRAB_DAILY_SPAWNS.get(chat_id, (today, 0))
    if last_date != today:
        count = 0
    if count >= HUECRAB_EVENT_DAILY_LIMIT or has_active_huecrab_event(chat_id):
        return
    if random.random() >= HUECRAB_EVENT_CHANCE:
        return
    event_id = create_huecrab_event(chat_id)
    if event_id is None:
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            get_text("huecrab.button"),
            callback_data=f"{HUECRAB_CALLBACK_PREFIX}{event_id}",
        )
    ]])
    try:
        message = await context.bot.send_message(
            chat_id=chat_id, text=get_text("huecrab.spawn"),
            parse_mode="HTML", reply_markup=keyboard,
        )
        if not bind_huecrab_event(event_id, message.message_id):
            raise RuntimeError("Could not bind Huecrab event to Telegram message")
    except Exception:
        discard_unpublished_huecrab_event(event_id)
        logging.exception("Could not publish Huecrab event %s in chat %s", event_id, chat_id)
        return
    HUECRAB_DAILY_SPAWNS[chat_id] = (today, count + 1)


async def huecrab_event_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        chats = get_duel_item_event_chat_ids()
    except Exception:
        logging.exception("Could not enumerate Huecrab event chats")
        return
    for chat_id in chats:
        try:
            await spawn_huecrab_event(context, chat_id)
        except Exception:
            logging.exception("Huecrab event failed in chat %s", chat_id)


async def huecrab_tame_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message or not update.effective_chat:
        return
    try:
        event_id = int(query.data.removeprefix(HUECRAB_CALLBACK_PREFIX))
    except (AttributeError, ValueError):
        await query.answer(get_text("huecrab.alerts.taken"), show_alert=True)
        return
    chat_id = update.effective_chat.id
    try:
        outcome = tame_huecrab_event(
            event_id, chat_id, query.from_user.id, query.message.message_id,
            lambda: random.random() < HUECRAB_TAME_CHANCE,
        )
    except Exception:
        logging.exception("Huecrab tame failed for event %s", event_id)
        await query.answer(get_text("huecrab.alerts.taken"), show_alert=True)
        return
    if outcome in {"taken", "not_registered", "already_owned"}:
        await query.answer(get_text(f"huecrab.alerts.{outcome}"), show_alert=True)
        return
    await query.answer()
    title = format_user_title({
        "username": query.from_user.username,
        "display_name": query.from_user.first_name,
        "dwarf_name": get_duel_dwarf_name(chat_id, query.from_user.id),
    })
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=query.message.message_id,
            text=get_text(f"huecrab.result.{outcome}", user=title),
            parse_mode="HTML", reply_markup=None,
        )
    except Exception:
        logging.exception("Could not edit consumed Huecrab event %s", event_id)


def _autoloot_text(claim: dict) -> str:
    result = get_text(
        "huecrab.autoloot", user=format_user_title(claim["owner"]),
        item=escape(get_duel_item_name(claim["item_id"])),
    )
    origin = claim.get("origin_text")
    return f"{origin}\n\n{result}" if origin else result


async def huecrab_autoloot_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        due = list_due_huecrab_item_events(time(), HUECRAB_AUTOLOOT_DELAY_SECONDS)
    except Exception:
        logging.exception("Could not enumerate due Huecrab items")
        return
    for event_id in due:
        try:
            claim_due_item_for_huecrab(
                event_id, time(), HUECRAB_AUTOLOOT_DELAY_SECONDS,
                lambda: random.choice(DUEL_ITEMS)["id"], random.choice,
            )
        except Exception:
            logging.exception("Huecrab auto-loot claim failed for item event %s", event_id)
    try:
        pending = list_unannounced_huecrab_claims()
    except Exception:
        logging.exception("Could not enumerate pending Huecrab announcements")
        return
    for claim in pending:
        try:
            await context.bot.edit_message_text(
                chat_id=claim["chat_id"], message_id=claim["message_id"],
                text=_autoloot_text(claim), parse_mode="HTML", reply_markup=None,
            )
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                logging.exception("Could not announce Huecrab claim %s", claim["event_id"])
                continue
        try:
            mark_huecrab_claim_announced(claim["event_id"])
        except Exception:
            logging.exception("Could not acknowledge Huecrab announcement %s", claim["event_id"])
