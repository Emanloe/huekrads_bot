"""One-shot, per-chat/per-user answers from the elite knowledge ball."""

import random

from telegram import InlineKeyboardButton, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from text_resources import get_text, get_text_list


ELITE_BALL_CALLBACK_DATA = "elite_ball_ask"
_WAITING_KEY = "elite_ball_waiting"


def elite_ball_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        get_text("elite_ball.button"), callback_data=ELITE_BALL_CALLBACK_DATA,
    )


async def elite_ball_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if (
        query is None or query.data != ELITE_BALL_CALLBACK_DATA
        or query.from_user is None or chat is None
        or getattr(query.from_user, "is_bot", False)
    ):
        return

    await query.answer()
    context.bot_data.setdefault(_WAITING_KEY, set()).add((chat.id, query.from_user.id))
    await context.bot.send_message(chat_id=chat.id, text=get_text("elite_ball.waiting"))


async def elite_ball_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = update.effective_chat
    if (
        message is None or chat is None or message.from_user is None
        or getattr(message.from_user, "is_bot", False)
        or not isinstance(message.text, str) or not message.text.strip()
        or message.text.lstrip().startswith("/")
    ):
        return

    waiting = context.bot_data.get(_WAITING_KEY)
    key = (chat.id, message.from_user.id)
    if not waiting or key not in waiting:
        return

    waiting.remove(key)
    answer = random.choice(get_text_list("elite_ball.answers"))
    await message.reply_text(answer, reply_to_message_id=message.message_id)
    # The ordinary text trigger is in group 0; this answered question is consumed.
    raise ApplicationHandlerStop
