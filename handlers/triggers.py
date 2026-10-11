import json
import re
import random
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from config import (
    AMIGO_REPLY_GIF_ID,
    AMIGO_REPLY_TARGET_USER_ID,
    AMIGO_REPLY_TARGET_USERNAME,
    AMIGO_USER_ID,
    OREL_GIF_IDS,
    BIRTHDAY_GIF_ID,
    LET_DO_STICKER_IDS,
    DUEL_TIMEZONE,
)
from database import (
    save_or_update_user,
    set_user_birthdate_with_cooldown,
    get_user_birthdate_from_db,
    mark_pizda_candidate_used,
    is_forward_reply_enabled,
)
from handlers.past_pizda import match_yes_no, remember_pizda_candidate
from text_resources import get_text
from module_settings import is_module_enabled

_LET_DO_PHRASES_PATH = Path(__file__).resolve().parent.parent / "data" / "let_do_phrases.json"

with open(_LET_DO_PHRASES_PATH, encoding="utf-8") as _phrases_file:
    LET_DO_PHRASES = tuple(json.load(_phrases_file))

from handlers.past_pizda import match_yes_no

logger = logging.getLogger(__name__)

# Реакция на "трясущиеся" слова
_SHAKING_PATH = Path(__file__).resolve().parent.parent / "data" / "shaking.json"

with open(_SHAKING_PATH, encoding="utf-8") as _shaking_file:
    SHAKING_DATA = json.load(_shaking_file)

SHAKING_GIF_ID = SHAKING_DATA.get("shaking_gif_id")

SHAKING_RE = re.compile(
    r"(?<!\w)(?:"
    r"тряска|"
    r"трясок|"
    r"трясет|"
    r"трясёт|"
    r"трясешься|"
    r"трясёшься|"
    r"трясусь|"
    r"трясутся|"
    r"трясся|"
    r"тряслась|"
    r"трясись|"
    r"трястись|"
    r"трясись|"
    r"тряской|"
    r"трясучка|"
    r"трясучий|"
    r"трясучая|"
    r"трясучее|"
    r"трясуче|"
    r"трясучки|"
    r"трясучкой|"
    r"трясказаебал|"
    r"трясказаебала|"
    r"трясказаебали|"
    r"трясказаебался|"
    r"трясказаебалась|"
    r"трясказаебись|"
    r"трясануло|"
    r"трясонуло|"
    r"трясуч|"
    r"трясун|"
    r"трясунья"
    r")(?!\w)",
    re.IGNORECASE,
)


def is_amigo_reply_to_rost(message) -> bool:
    """Return whether the message replies to the configured user."""
    replied_message = getattr(message, "reply_to_message", None)
    original_sender = getattr(replied_message, "from_user", None)
    return getattr(original_sender, "id", None) == AMIGO_REPLY_TARGET_USER_ID


def _entity_text(text, entity) -> str | None:
    """Read Telegram's UTF-16 entity range without trusting plain-text search."""
    if not isinstance(text, str):
        return None
    offset = getattr(entity, "offset", None)
    length = getattr(entity, "length", None)
    if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length < 0:
        return None
    try:
        encoded = text.encode("utf-16-le")
        return encoded[offset * 2:(offset + length) * 2].decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def has_rost_mention(message) -> bool:
    """Match only Telegram mention entities in text and media captions."""
    sources = (
        (getattr(message, "text", None), getattr(message, "entities", None)),
        (getattr(message, "caption", None), getattr(message, "caption_entities", None)),
    )
    for text, entities in sources:
        for entity in entities or ():
            entity_type = getattr(getattr(entity, "type", None), "value", getattr(entity, "type", None))
            if entity_type == "text_mention":
                if getattr(getattr(entity, "user", None), "id", None) == AMIGO_REPLY_TARGET_USER_ID:
                    return True
            elif entity_type == "mention":
                entity_value = _entity_text(text, entity)
                if entity_value is not None and entity_value.casefold() == f"@{AMIGO_REPLY_TARGET_USERNAME}".casefold():
                    return True
    return False


def is_amigo_rost_message(message) -> bool:
    """Return whether Amigo replied to Rost or mentioned Rost via an entity."""
    return (
        getattr(getattr(message, "from_user", None), "id", None) == AMIGO_USER_ID
        and (is_amigo_reply_to_rost(message) or has_rost_mention(message))
    )


async def amigo_reply_to_rost_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the configured GIF when Amigo replies to the configured user."""
    message = getattr(update, "message", None)
    if message is None or not is_amigo_rost_message(message):
        return

    await message.reply_animation(
        animation=AMIGO_REPLY_GIF_ID,
        reply_to_message_id=message.message_id,
    )


async def respond_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    text_raw = (update.message.text or "").strip()

    save_or_update_user(update.message.from_user, chat_id)

    # The dedicated rule is handled in the earlier narrow MessageHandler.
    # Avoid adding the old random Amigo replies to its guaranteed GIF.
    if is_amigo_rost_message(update.message):
        return

    # Запись ДР по шаблону @username 26.01.1994
    bday_pattern = r"^(@\w+)\s+(\d{2}\.\d{2}(?:\.\d{4})?)$"
    match = re.match(bday_pattern, text_raw)

    if match:
        target_username, bday_str = match.groups()

        if target_username[1:].casefold() != (update.message.from_user.username or "").casefold():
            response = get_text("triggers.birthday.registration.self_only")
        else:
            status, next_change = set_user_birthdate_with_cooldown(chat_id, user_id, bday_str)
            if status == "success":
                response = get_text(
                    "triggers.birthday.registration.saved",
                    target_username=target_username,
                    bday_str=bday_str,
                )
            elif status == "cooldown":
                next_moscow = next_change.astimezone(ZoneInfo(DUEL_TIMEZONE))
                if next_moscow.microsecond:
                    next_moscow = (next_moscow + timedelta(seconds=1)).replace(microsecond=0)
                response = get_text(
                    "commands.birthday.cooldown",
                    date=next_moscow.strftime("%d.%m.%Y %H:%M:%S"),
                )
            elif status == "invalid":
                response = get_text("triggers.birthday.registration.invalid")
            else:
                response = get_text(
                    "triggers.birthday.registration.missing_user",
                    target_username=target_username,
                )

        await update.message.reply_text(
            response,
            reply_to_message_id=update.message.message_id,
        )

        return

    reactions_enabled = is_module_enabled(chat_id, "chat_reactions")
    now = datetime.now()
    last_responded = context.user_data.get(user_id)

    # Реакция на пересланные сообщения
    # Проверяем тумблер из БД
    if reactions_enabled and update.message.forward_origin is not None and is_forward_reply_enabled(chat_id):
        if random.random() < 0.3:
            await update.message.reply_text(
                get_text("triggers.responses.forwarded"),
                reply_to_message_id=update.message.message_id,
            )

    # Дни рождения
    is_bday_today = False

    try:
        chat = await context.bot.get_chat(user_id)

        if hasattr(chat, "birthdate") and chat.birthdate:
            bday = chat.birthdate

            if bday.day == now.day and bday.month == now.month:
                is_bday_today = True

    except Exception as e:
        logging.debug(
            f"Не удалось проверить ДР через API Telegram для {user_id}: {e}"
        )

    if not is_bday_today:
        db_bday = get_user_birthdate_from_db(user_id, chat_id)

        if db_bday:
            try:
                parts = db_bday.split(".")

                if int(parts[0]) == now.day and int(parts[1]) == now.month:
                    is_bday_today = True

            except (ValueError, IndexError):
                pass

    if is_bday_today and is_module_enabled(chat_id, "birthday_greetings"):
        congrat_key = f"bday_{now.year}"

        if not context.user_data.get(congrat_key):
            context.user_data[congrat_key] = True

            user_name = update.message.from_user.first_name
            text = get_text("triggers.birthday.greeting", user_name=user_name)

            if BIRTHDAY_GIF_ID:
                await update.message.reply_animation(
                    animation=BIRTHDAY_GIF_ID,
                    caption=text,
                )
            else:
                await update.message.reply_text(text)

    reactions_enabled = is_module_enabled(chat_id, "chat_reactions")
    past_pizda_enabled = is_module_enabled(chat_id, "past_pizda")

    # Реакция на фразы let_do
    if (
        reactions_enabled
        and any(phrase in text_raw.lower() for phrase in LET_DO_PHRASES)
        and random.random() < 0.05
    ):
        if LET_DO_STICKER_IDS:
            await update.message.reply_sticker(
                sticker=random.choice(LET_DO_STICKER_IDS),
                reply_to_message_id=update.message.message_id,
            )

    # Реакция на "трясущиеся" слова
    if reactions_enabled and SHAKING_GIF_ID and SHAKING_RE.search(text_raw):
        if random.random() < 0.05:
            await update.message.reply_animation(
                animation=SHAKING_GIF_ID,
                reply_to_message_id=update.message.message_id,
            )

    yes_no = match_yes_no(text_raw)

    if past_pizda_enabled and yes_no == "да":
        remember_pizda_candidate(
            chat_id,
            update.message.message_id,
            update.message.date,
        )

        # Ответы "Да/Нет" и троллинг Amigo
    response_chance = 0.09

    if not reactions_enabled and not past_pizda_enabled:
        return
    roll = random.random()

    logger.info(
        "YES_NO: chat_id=%s user_id=%s text=%r matched=%r roll=%.4f chance=%.2f",
        chat_id,
        user_id,
        text_raw,
        yes_no,
        roll,
        response_chance,
    )

    if last_responded is None or roll < response_chance:
        if past_pizda_enabled and yes_no == "да":
            await update.message.reply_text(
                get_text("past_pizda.messages.reply"),
                reply_to_message_id=update.message.message_id,
            )

            mark_pizda_candidate_used(
                chat_id,
                update.message.message_id,
            )

        elif reactions_enabled and yes_no == "нет":
            await update.message.reply_text(
                get_text("triggers.responses.no"),
                reply_to_message_id=update.message.message_id,
            )

        user_first_name = (
            update.message.from_user.first_name or ""
        ).lower()

        if reactions_enabled and is_module_enabled(chat_id, "chat_reactions") and user_first_name == "amigo":
            if random.random() < 0.3:
                await update.message.reply_text(
                    get_text("triggers.responses.amigo"),
                    reply_to_message_id=update.message.message_id,
                )

            if random.random() < 0.3 and OREL_GIF_IDS:
                await update.message.reply_animation(
                    animation=random.choice(OREL_GIF_IDS),
                    reply_to_message_id=update.message.message_id,
                )

        context.user_data[user_id] = now
