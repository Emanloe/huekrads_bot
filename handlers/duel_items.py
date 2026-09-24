"""Shared duel-item catalog, inventory presentation, and chat item events."""

import json
import logging
import random
import re
from collections import Counter
from html import escape
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import DUEL_ITEM_EVENT_CHANCE, HUEGRYZ_CHANCE
from database import (
    claim_duel_item_event,
    create_duel_item_event,
    discard_unpublished_duel_item_event,
    format_user_title,
    get_duel_dwarf_name,
    get_duel_item_event,
    get_duel_item_event_chat_ids,
    set_duel_item_event_message,
)
from text_resources import get_text, get_text_list, get_text_mapping


_DUEL_ITEMS_PATH = Path(__file__).resolve().parent.parent / "data" / "duel_items.json"
_ITEM_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
DUEL_ITEM_EVENT_CALLBACK_PREFIX = "duel_item_claim_"
BASE_DUEL_ITEM_IDS = ("oiled_vest", "knife")


def _load_duel_items(path=_DUEL_ITEMS_PATH) -> tuple[dict[str, str], ...]:
    try:
        with open(path, encoding="utf-8") as items_file:
            items = json.load(items_file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in duel item catalog: {path}") from exc

    if not isinstance(items, list):
        raise ValueError("duel_items.json must contain a list of items")
    if not items:
        raise ValueError("duel_items.json must contain at least one item")

    normalized = []
    ids = set()
    names = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "name"}:
            raise ValueError("Every duel item must contain only id and name")
        item_id = item["id"]
        name = item["name"]
        if not isinstance(item_id, str) or not _ITEM_ID_PATTERN.fullmatch(item_id):
            raise ValueError(f"Invalid duel item id: {item_id!r}")
        if item_id in BASE_DUEL_ITEM_IDS:
            raise ValueError(f"Base duel item id cannot be collectible: {item_id!r}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Invalid duel item name for {item_id!r}")
        if item_id in ids or name in names:
            raise ValueError("Duel item ids and names must be unique")
        ids.add(item_id)
        names.add(name)
        normalized.append({"id": item_id, "name": name})

    return tuple(normalized)


DUEL_ITEMS = _load_duel_items()
_BASE_DUEL_ITEM_NAMES = get_text_mapping("duel.inventory.base_items")
if tuple(_BASE_DUEL_ITEM_NAMES) != BASE_DUEL_ITEM_IDS:
    raise ValueError("Base duel item ids or order do not match the permanent inventory")
BASE_DUEL_ITEMS = tuple(
    {"id": item_id, "name": _BASE_DUEL_ITEM_NAMES[item_id]}
    for item_id in BASE_DUEL_ITEM_IDS
)
DUEL_ITEM_NAMES = {
    **_BASE_DUEL_ITEM_NAMES,
    **{item["id"]: item["name"] for item in DUEL_ITEMS},
}
_DUEL_ITEM_ORDER = {item["id"]: index for index, item in enumerate(DUEL_ITEMS)}


def get_duel_item_name(item_id: str) -> str:
    return DUEL_ITEM_NAMES.get(item_id, get_text("duel.inventory.unknown_item"))


def format_duel_display_inventory(collectible_instances: list[dict]) -> str:
    """Format permanent base items followed by stored collectible instances."""
    counts = Counter(
        instance["item_id"]
        for instance in collectible_instances
        if instance["item_id"] not in BASE_DUEL_ITEM_IDS
    )
    item_ids = sorted(
        counts,
        key=lambda item_id: (
            _DUEL_ITEM_ORDER.get(item_id, len(_DUEL_ITEM_ORDER)),
            item_id,
        ),
    )
    formatted = [escape(item["name"]) for item in BASE_DUEL_ITEMS]
    for item_id in item_ids:
        name = escape(get_duel_item_name(item_id))
        count = counts[item_id]
        formatted.append(
            get_text("duel.inventory.counted_item", item=name, count=count)
            if count > 1
            else name
        )
    return ", ".join(formatted)


def get_droppable_duel_inventory(instances: list[dict]) -> list[dict]:
    """Return only stored collectible instances eligible for duel loss."""
    if all(instance["item_id"] not in BASE_DUEL_ITEM_IDS for instance in instances):
        return instances
    return [
        instance
        for instance in instances
        if instance["item_id"] not in BASE_DUEL_ITEM_IDS
    ]


# Compatibility name for callers that only need inventory presentation.
format_duel_inventory = format_duel_display_inventory


async def _spawn_duel_item_event(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
):
    if random.random() >= DUEL_ITEM_EVENT_CHANCE:
        return

    event_id = create_duel_item_event(chat_id)
    if event_id is None:
        return

    intro = random.choice(get_text_list("duel.item_event.intros"))
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                get_text("duel.item_event.button"),
                callback_data=f"{DUEL_ITEM_EVENT_CALLBACK_PREFIX}{event_id}",
            )
        ]]
    )

    try:
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=intro,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        discard_unpublished_duel_item_event(event_id)
        logging.exception("Не удалось отправить item event в чат %s", chat_id)
        return

    if not set_duel_item_event_message(event_id, message.message_id, intro):
        logging.error("Could not bind published item event %s", event_id)


async def duel_item_event_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_ids = get_duel_item_event_chat_ids()
    except Exception:
        logging.exception("Не удалось получить чаты для item event")
        return

    for chat_id in chat_ids:
        try:
            await _spawn_duel_item_event(context, chat_id)
        except Exception:
            logging.exception("Ошибка item event в чате %s", chat_id)


async def duel_item_event_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return

    try:
        event_id = int(query.data.removeprefix(DUEL_ITEM_EVENT_CALLBACK_PREFIX))
    except ValueError:
        await query.answer(get_text("duel.item_event.already_claimed"), show_alert=True)
        return

    chat_id = update.effective_chat.id
    event = get_duel_item_event(event_id)
    if not event or event["chat_id"] != chat_id or event["claimed"]:
        await query.answer(get_text("duel.item_event.already_claimed"), show_alert=True)
        return

    status, instance = claim_duel_item_event(
        event_id,
        chat_id,
        query.from_user.id,
        lambda: random.choice(DUEL_ITEMS)["id"],
        lambda: random.random() < HUEGRYZ_CHANCE,
    )
    if status == "not_registered":
        await query.answer(get_text("duel.item_event.not_registered"), show_alert=True)
        return
    if status != "claimed":
        await query.answer(get_text("duel.item_event.already_claimed"), show_alert=True)
        return

    await query.answer()
    title = format_user_title(
        {
            "username": query.from_user.username,
            "display_name": query.from_user.first_name,
            "dwarf_name": get_duel_dwarf_name(chat_id, query.from_user.id),
        }
    )
    item_name = escape(get_duel_item_name(instance["item_id"]))
    text = get_text(
        "duel.item_event.claimed",
        user=title,
        item=item_name,
    )
    if instance["huegryz_outcome"] is not None:
        text += "\n\n" + get_text(
            f"duel.item_event.huegryz.{instance['huegryz_outcome']}"
        )

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=event["message_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        logging.exception("Не удалось обновить claimed item event %s", event_id)
