"""One-time, chat-scoped names for registered duel participants."""

import re
import unicodedata
from html import escape

from database import is_duel_user_registered, set_duel_dwarf_name_once
from handlers.duel_messaging import send_and_schedule
from text_resources import get_text


MAX_DWARF_NAME_LENGTH = 40


def normalize_dwarf_name(raw: str) -> tuple[str | None, str | None]:
    if not raw.strip():
        return None, "missing"
    if any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in raw):
        return None, "invalid"
    name = " ".join(raw.split())
    if len(name) > MAX_DWARF_NAME_LENGTH:
        return None, "too_long"
    return name, None


async def name_command(update, context):
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    if user is None or chat is None or message is None:
        return

    if not is_duel_user_registered(chat.id, user.id):
        await send_and_schedule(update, context, get_text("duel.name.not_registered"))
        return

    command = re.match(r"/\S+", message.text or "")
    raw = (message.text or "")[command.end():] if command else ""
    name, error = normalize_dwarf_name(raw)
    if error:
        await send_and_schedule(update, context, get_text(f"duel.name.{error}"))
        return

    status, stored_name = set_duel_dwarf_name_once(chat.id, user.id, name)
    if status == "set":
        key = "duel.name.set"
    elif status == "already_named":
        key = "duel.name.already_named"
    else:
        await send_and_schedule(update, context, get_text("duel.name.not_registered"))
        return
    await send_and_schedule(
        update, context, get_text(key, dwarf_name=escape(stored_name)),
    )
