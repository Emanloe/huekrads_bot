"""One-shot, per-chat/per-user answers from the elite knowledge ball."""

import logging
import random
from html import escape

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle,
    InputMediaPhoto, InputTextMessageContent, Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from elite_ball_store import (
    activate_ball, consume_chat_ball, consume_inline_action, create_inline_action,
)
from handlers.duel_messaging import schedule_auto_delete
from text_resources import get_text, get_text_list


ELITE_BALL_CALLBACK_DATA = "elite_ball_ask"
ELITE_BALL_INLINE_RESULT_ID = "elite_ball_inline"
ELITE_BALL_INLINE_CALLBACK_PREFIX = "ebi:"
ELITE_BALL_PHOTO_FILE_ID = "AgACAgIAAxkBAAPYarOx_Ot9KPfYe1lKYZsAAaKkq7kLAAIsIGsbFCShScvsbybBp2qPAQADAgADeAADPQQ"
BALL_COMMAND_DELETE_DELAY = 10


def elite_ball_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        get_text("elite_ball.button"), callback_data=ELITE_BALL_CALLBACK_DATA,
    )


def choose_ball_answer() -> str:
    answers = get_text_list("elite_ball.answers")
    if random.random() < 0.05:
        return answers[3]
    return random.choice(answers[:3])


def build_elite_ball_inline_result(user_id: int, question: str) -> InlineQueryResultArticle:
    token = create_inline_action(user_id, question)
    return InlineQueryResultArticle(
        id=f"{ELITE_BALL_INLINE_RESULT_ID}_{token}",
        title=get_text("elite_ball.button"),
        description=get_text("elite_ball.inline_description", question=question),
        input_message_content=InputTextMessageContent(
            get_text("elite_ball.inline_preview", question=escape(question)),
            parse_mode="HTML",
        ),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                get_text("elite_ball.inline_button"),
                callback_data=f"{ELITE_BALL_INLINE_CALLBACK_PREFIX}{token}",
            ),
        ]]),
    )


async def _activate_ball(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    activate_ball(chat_id, user_id)
    await context.bot.send_message(chat_id=chat_id, text=get_text("elite_ball.waiting"))


async def ball_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Activate from a real chat update after the user chooses an inline result."""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or getattr(user, "is_bot", False):
        return
    from database import is_deleted_user
    if is_deleted_user(user.id):
        await update.message.reply_text(get_text("gnome_deletion.deleted"))
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

    from database import is_deleted_user
    if is_deleted_user(query.from_user.id):
        await query.answer(get_text("gnome_deletion.deleted"), show_alert=True)
        return
    await query.answer()
    await _activate_ball(context, chat.id, query.from_user.id)


async def elite_ball_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = update.effective_chat
    if (
        message is None or chat is None or message.from_user is None
        or getattr(message.from_user, "is_bot", False)
        or getattr(message, "via_bot", None) is not None
        or not isinstance(message.text, str) or not message.text.strip()
        or message.text.lstrip().startswith("/")
    ):
        return

    if not consume_chat_ball(chat.id, message.from_user.id):
        return

    answer = choose_ball_answer()
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


async def elite_ball_inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if (
        query is None or not isinstance(query.data, str)
        or not query.data.startswith(ELITE_BALL_INLINE_CALLBACK_PREFIX)
        or query.from_user is None or getattr(query.from_user, "is_bot", False)
    ):
        return
    if not query.inline_message_id:
        await query.answer(get_text("elite_ball.inline_unavailable"), show_alert=True)
        return

    token = query.data[len(ELITE_BALL_INLINE_CALLBACK_PREFIX):]
    result = consume_inline_action(token, query.from_user.id, choose_ball_answer)
    if result.status in ("used", "already_used"):
        final = get_text(
            "elite_ball.inline_final",
            question=escape(result.question), answer=escape(result.answer),
        )
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(
                    media=ELITE_BALL_PHOTO_FILE_ID, caption=final, parse_mode="HTML",
                ),
                reply_markup=None,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logging.exception("Could not edit elite ball inline media")
        except TelegramError:
            logging.exception("Could not edit elite ball inline media")
        await query.answer()
        return
    await query.answer(get_text(f"elite_ball.inline_{result.status}"), show_alert=True)
