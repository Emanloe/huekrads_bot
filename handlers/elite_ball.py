"""One-shot, per-chat/per-user answers from the elite knowledge ball."""

import logging
import random

from telegram import InlineKeyboardButton, InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from handlers.duel_messaging import schedule_auto_delete
from text_resources import get_text, get_text_list


ELITE_BALL_CALLBACK_DATA = "elite_ball_ask"
ELITE_BALL_INLINE_RESULT_ID = "elite_ball_inline"
ELITE_BALL_PHOTO_FILE_ID = "AgACAgIAAxkBAAPYarOx_Ot9KPfYe1lKYZsAAaKkq7kLAAIsIGsbFCShScvsbybBp2qPAQADAgADeAADPQQ"
BALL_COMMAND_DELETE_DELAY = 10
_WAITING_KEY = "elite_ball_waiting"


def elite_ball_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        get_text("elite_ball.button"), callback_data=ELITE_BALL_CALLBACK_DATA,
    )


def build_elite_ball_inline_result() -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=ELITE_BALL_INLINE_RESULT_ID,
        title=get_text("elite_ball.button"),
        input_message_content=InputTextMessageContent(get_text("elite_ball.inline_message")),
    )


async def _activate_ball(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    context.bot_data.setdefault(_WAITING_KEY, set()).add((chat_id, user_id))
    await context.bot.send_message(chat_id=chat_id, text=get_text("elite_ball.waiting"))


async def ball_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Activate from a real chat update after the user chooses an inline result."""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or getattr(user, "is_bot", False):
        return
    await _activate_ball(context, chat.id, user.id)
    if update.message is not None:
        try:
            schedule_auto_delete(
                context, chat.id, [update.message.message_id],
                delay=BALL_COMMAND_DELETE_DELAY,
            )
        except Exception:
            logging.exception("Could not schedule /ball message deletion in chat %s", chat.id)


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
    await _activate_ball(context, chat.id, query.from_user.id)


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
    try:
        await message.reply_photo(
            photo=ELITE_BALL_PHOTO_FILE_ID,
            caption=answer,
            reply_to_message_id=message.message_id,
        )
    except Exception:
        logging.exception("Could not send elite ball answer in chat %s", chat.id)
    # The ordinary text trigger is in group 0; this answered question is consumed.
    raise ApplicationHandlerStop
