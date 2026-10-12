"""Author-only voluntary game deletion and administrator-only return."""

import asyncio
import logging
import re
import secrets
from contextlib import AsyncExitStack
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

import config
from database import (delete_gnome, find_deleted_gnome_by_username,
                      format_user_title_plain, is_deleted_user, return_gnome,
                      return_gnome_by_id)
from text_resources import get_text


CALLBACK_PREFIX = "dickpukku:"
_confirmations = {}
_confirmation_lock = asyncio.Lock()
_GAME_COMMANDS = frozenset({
    "duel", "app", "duel_app", "name", "dig", "lure", "ball", "me", "duel_stats", "inspect",
    "duel_top", "duel_delete", "boss", "boss_reg", "gnomed",
})
_GAME_CALLBACK_PREFIXES = (
    "start_duel_", "duel_strike_", "duel_block_", "duel_item_claim_",
    "huecrab_tame_", "boss_", "hyperboreic_huy", "moss_choice:",
    "elite_ball_", "ebi:", "dickpukku:",
)


async def guard_deleted_game_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop only gnome actions; weather, help, settings and other bot uses remain usable."""
    user = update.effective_user
    if user is None:
        return
    query = update.callback_query
    if query is not None:
        if ((query.data or "").startswith(_GAME_CALLBACK_PREFIXES)
                and is_deleted_user(user.id)):
            await query.answer(get_text("gnome_deletion.deleted"), show_alert=True)
            raise ApplicationHandlerStop
        return
    message = update.message
    if message is not None and message.text and message.text.startswith("/"):
        command = message.text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()
        if command in _GAME_COMMANDS and is_deleted_user(user.id):
            await message.reply_text(get_text("gnome_deletion.deleted"))
            raise ApplicationHandlerStop


@dataclass
class Confirmation:
    owner_id: int
    chat_id: int
    message_id: int
    stage: int = 1


async def dickpukku_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return
    if is_deleted_user(user.id):
        await message.reply_text(get_text("gnome_deletion.deleted"))
        return
    token = secrets.token_urlsafe(16)
    sent = await message.reply_text(
        get_text("gnome_deletion.first_button"),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(get_text("gnome_deletion.first_button"),
                                 callback_data=f"{CALLBACK_PREFIX}{token}:start")
        ]]),
    )
    _confirmations[token] = Confirmation(user.id, message.chat_id, sent.message_id)


async def dickpukku_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return
    match = re.fullmatch(r"dickpukku:([A-Za-z0-9_-]{22}):(start|yes|no)", query.data or "")
    if match is None:
        await query.answer(get_text("gnome_deletion.stale"), show_alert=True)
        return
    token, action = match.groups()
    async with _confirmation_lock:
        state = _confirmations.get(token)
        if (state is None or query.message is None or update.effective_chat is None
                or state.chat_id != update.effective_chat.id
                or state.message_id != query.message.message_id):
            await query.answer(get_text("gnome_deletion.stale"), show_alert=True)
            return
        if state.owner_id != query.from_user.id:
            await query.answer(get_text("gnome_deletion.foreign"), show_alert=True)
            return
        if action == "start" and state.stage == 1:
            try:
                await query.edit_message_text(
                    get_text("gnome_deletion.second_prompt"),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(get_text("gnome_deletion.yes"),
                                             callback_data=f"{CALLBACK_PREFIX}{token}:yes"),
                        InlineKeyboardButton(get_text("gnome_deletion.no"),
                                             callback_data=f"{CALLBACK_PREFIX}{token}:no"),
                    ]]),
                )
            except Exception:
                logging.exception("Could not edit gnome deletion confirmation stage")
                await query.answer(get_text("gnome_deletion.error"), show_alert=True)
                return
            state.stage = 2
            await query.answer()
            return
        if state.stage != 2 or action == "start":
            await query.answer(get_text("gnome_deletion.stale"), show_alert=True)
            return
        if action == "no":
            await query.answer()
            return
        try:
            from handlers.duel import ACTIVE_BOSS_BATTLES
            async with AsyncExitStack() as stack:
                battles = [battle for _, battle in sorted(ACTIVE_BOSS_BATTLES.items())
                           if state.owner_id in battle["participants"]]
                for battle in battles:
                    await stack.enter_async_context(battle["lock"])
                delete_gnome(state.owner_id, query.from_user.username)
                for battle in battles:
                    battle["participants"].pop(state.owner_id, None)
        except Exception:
            logging.exception("Gnome deletion failed for user %s", state.owner_id)
            await query.answer(get_text("gnome_deletion.error"), show_alert=True)
            return
        for key, pending in list(_confirmations.items()):
            if pending.owner_id == state.owner_id:
                del _confirmations[key]
        try:
            name = format_user_title_plain(
                {"username": query.from_user.username,
                 "display_name": query.from_user.full_name},
                include_dwarf_name=False,
            )
            await query.edit_message_text(
                get_text("gnome_deletion.final", name=name), reply_markup=None,
            )
        except Exception:
            logging.exception("Could not edit gnome deletion confirmation")
        await query.answer()


async def return_gnome_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return
    if user.id not in config.ADMIN_IDS:
        await message.reply_text(get_text("gnome_deletion.return.denied"))
        return
    args = getattr(context, "args", None) or []
    if len(args) != 1 or not (re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", args[0])
                              or re.fullmatch(r"[1-9][0-9]{0,19}", args[0])):
        await message.reply_text(get_text("gnome_deletion.return.usage"))
        return
    if args[0].isdigit():
        status = return_gnome_by_id(int(args[0]))
    else:
        status, candidate_id = find_deleted_gnome_by_username(args[0])
        if status == "candidate":
            try:
                current = await context.bot.get_chat(chat_id=candidate_id)
            except Exception:
                logging.exception("Could not verify current username of deleted user %s", candidate_id)
                status = "verification_failed"
            else:
                requested = args[0].lstrip("@").casefold()
                if (current.id != candidate_id or current.type != "private"
                        or not current.username or current.username.casefold() != requested):
                    status = "verification_failed"
                else:
                    status = return_gnome(args[0], verified_user_id=candidate_id)
    await message.reply_text(get_text(f"gnome_deletion.return.{status}"))
