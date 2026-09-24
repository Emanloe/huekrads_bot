"""Trusted group entry point for a chat-bound Telegram Mini App launch."""

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from handlers.duel_service import get_duel_profile
from miniapp_sessions import create_launch_token


_BOT_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}\Z")


async def duel_app_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("Откройте /duel_app в группе, где идёт игра.")
        return
    if get_duel_profile(chat.id, user.id, read_only=True) is None:
        await message.reply_text("Сначала зарегистрируйтесь в дуэлях этой группы через /duel.")
        return
    username = getattr(context.bot, "username", None)
    if not isinstance(username, str) or not _BOT_USERNAME.fullmatch(username):
        me = await context.bot.get_me()
        username = me.username
    if not isinstance(username, str) or not _BOT_USERNAME.fullmatch(username):
        logging.error("Cannot create Mini App launch link without bot username")
        await message.reply_text("Ссылка на приложение временно недоступна.")
        return
    # Telegram routes this startapp parameter to the bot's configured Main Mini App.
    token = create_launch_token(chat.id, user.id)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Открыть дуэли", url=f"https://t.me/{username}?startapp={token}"),
    ]])
    await message.reply_text("Мини-приложение для этого чата:", reply_markup=keyboard)
