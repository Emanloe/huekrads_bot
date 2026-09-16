"""Hyperborean huy and Arthur event mechanics."""

import logging
import random
import sqlite3
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import format_user_title, get_all_chats, get_or_create_duel_user

HYPERBOREAN_HUY_CHANCE = 0.04
HYPERBOREAN_HUY_CHECK_MINUTES = 15
ACTIVE_HYPERBOREAN_EVENTS = {}
_HYPERBOREAN_DB_PATH = Path(__file__).resolve().parent.parent / "bot_database.db"

async def _spawn_hyperboreic_huy(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
):
    """
    С вероятностью HYPERBOREAN_HUY_CHANCE создаёт одно из двух событий:

    1. 🍆 Гиперборейский хуй
    2. ⚔️ Хуй Короля Артура

    Тип события выбирается случайно при каждом успешном появлении.
    """

    if chat_id in ACTIVE_HYPERBOREAN_EVENTS:
        return

    if random.random() >= HYPERBOREAN_HUY_CHANCE:
        return

    event_type = random.choice(
        [
            "hyperboreic",
            "arthur",
        ]
    )

    if event_type == "arthur":
        event_text = (
            "⚔️ <b>ОБНАРУЖЕН ХУЙ КОРОЛЯ АРТУРА</b>\n\n"
            "Кто осмелится вытащить его из камня?\n\n"
            "Один хуй тебе или два другому?"
        )
    else:
        event_text = (
            "⚠️ <b>ОБНАРУЖЕН ГИПЕРБОРЕЙСКИЙ ХУЙ</b>\n\n"
            "Кто первый схватит — тому решать судьбу своего хуя.\n\n"
            "Один хуй тебе или два другому?"
        )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Один мне", callback_data="hyperboreic_huy_self"
                ),
                InlineKeyboardButton(
                    "Два другому", callback_data="hyperboreic_huy_other"
                ),
            ]
        ]
    )

    try:
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=event_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        logging.exception(
            "Не удалось создать событие %s в чате %s",
            event_type,
            chat_id,
        )
        return

    ACTIVE_HYPERBOREAN_EVENTS[chat_id] = {
        "message_id": message.message_id,
        "event_type": event_type,
    }


async def hyperboreic_huy_daily_job(
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Независимый генератор события.

    Проверяется каждый чат, где бот уже зарегистрирован.
    На каждой проверке вероятность появления события = 4%.

    Если событие появилось, это случайно либо:
        - Гиперборейский хуй
        - Хуй Короля Артура
    """

    try:
        chats = get_all_chats()
    except Exception:
        logging.exception(
            "Не удалось получить список чатов для "
            "гиперборейского хуя"
        )
        return

    for chat_id in chats:
        try:
            await _spawn_hyperboreic_huy(
                context,
                chat_id,
            )
        except Exception:
            logging.exception(
                "Ошибка проверки гиперборейского хуя "
                "для чата %s",
                chat_id,
            )


def _claim_hyperboreic_huy(
    chat_id: int,
    tg_user,
):
    """
    Атомарно разрешает событие в БД.

    Возвращает:
        "restored" — у гнома не было хуя, он его вернул;
        "exploded" — хуй был, гном умер;
        "missing" — пользователя ещё нет в БД;
        "error" — ошибка БД.
    """

    user = get_or_create_duel_user(
        tg_user,
        chat_id,
    )

    user_id = int(user["user_id"])

    db_path = (
        _HYPERBOREAN_DB_PATH
    )

    try:
        with sqlite3.connect(
            str(db_path),
            timeout=10,
        ) as conn:
            cursor = conn.execute(
                """
                SELECT
                    dick_stolen_today,
                    points
                FROM duel_users
                WHERE chat_id = ?
                  AND user_id = ?
                """,
                (
                    chat_id,
                    user_id,
                ),
            )

            row = cursor.fetchone()

            if not row:
                return "missing"

            had_no_dick = bool(row[0])

            if had_no_dick:
                conn.execute(
                    """
                    UPDATE duel_users
                    SET dick_stolen_today = 0
                    WHERE chat_id = ?
                      AND user_id = ?
                    """,
                    (
                        chat_id,
                        user_id,
                    ),
                )
                return "restored"

            conn.execute(
                """
                UPDATE duel_users
                SET
                    points = 0,
                    dick_stolen_today = 1
                WHERE chat_id = ?
                  AND user_id = ?
                """,
                (
                    chat_id,
                    user_id,
                ),
            )
            return "exploded"

    except Exception:
        logging.exception(
            "Ошибка разрешения события гиперборейского хуя "
            "для user_id=%s chat_id=%s",
            user_id,
            chat_id,
        )
        return "error"


def _claim_hyperboreic_huy_for_other(
    chat_id: int,
    tg_user,
):
    """Resolve the "two for another" action for one user of this chat."""
    try:
        # This preserves the existing claim behaviour: the player pressing a
        # button is registered before an event can be resolved.
        get_or_create_duel_user(tg_user, chat_id)

        with sqlite3.connect(str(_HYPERBOREAN_DB_PATH), timeout=10) as conn:
            candidates = conn.execute(
                """
                SELECT user_id, username, display_name, points, dick_stolen_today
                FROM duel_users
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchall()

            if not candidates:
                return "missing", None, False

            selected = random.choice(candidates)
            user_id, username, display_name, points, dick_stolen_today = selected
            had_no_dick = bool(dick_stolen_today)
            selected_user = {
                "user_id": user_id,
                "chat_id": chat_id,
                "username": username,
                "display_name": display_name,
                "points": points,
                "dick_stolen_today": had_no_dick,
            }

            if had_no_dick:
                conn.execute(
                    """
                    UPDATE duel_users
                    SET points = 0, dick_stolen_today = 1
                    WHERE chat_id = ? AND user_id = ?
                    """,
                    (chat_id, user_id),
                )
                selected_user["points"] = 0
                return "exploded", selected_user, True

            return "unchanged", selected_user, False
    except Exception:
        logging.exception(
            "Ошибка выбора игрока для гиперборейского хуя в чате %s",
            chat_id,
        )
        return "error", None, False


async def hyperboreic_huy_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query or query.data not in {
        "hyperboreic_huy",
        "hyperboreic_huy_self",
        "hyperboreic_huy_other",
    }:
        return

    chat_id = update.effective_chat.id
    event = ACTIVE_HYPERBOREAN_EVENTS.get(chat_id)

    if not event:
        await query.answer(
            "Хуй уже унесли.",
            show_alert=True,
        )
        return

    # Сразу блокируем событие в памяти.
    # Только один игрок сможет его забрать.
    ACTIVE_HYPERBOREAN_EVENTS.pop(chat_id, None)

    event_type = event.get("event_type", "hyperboreic")

    action = "other" if query.data == "hyperboreic_huy_other" else "self"
    selected_user = None

    if action == "other":
        result, selected_user, _had_no_dick = _claim_hyperboreic_huy_for_other(
            chat_id,
            query.from_user,
        )
    else:
        result = _claim_hyperboreic_huy(
            chat_id,
            query.from_user,
        )

    if result == "error":
        ACTIVE_HYPERBOREAN_EVENTS[chat_id] = event

        await query.answer(
            "Хуй отказался определяться. Попробуй ещё раз.",
            show_alert=True,
        )
        return

    if result == "missing":
        ACTIVE_HYPERBOREAN_EVENTS[chat_id] = event

        await query.answer(
            "Гном ещё не зарегистрирован в этом чате.",
            show_alert=True,
        )
        return

    if action == "other":
        title = format_user_title(selected_user)

        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=event["message_id"],
                reply_markup=None,
            )
        except Exception:
            logging.exception(
                "Не удалось убрать кнопки события в чате %s",
                chat_id,
            )

        if result == "exploded":
            if event_type == "arthur":
                text = (
                    f"⚔️ Выбор пал на <b>{title}</b>.\n\n"
                    "Хуй Короля Артура увидел, что у гнома уже нет хуя, "
                    "и разорвал его на величественные хуйные молекулы.\n\n"
                    "💀 Очки: <b>0 / 100</b>\n"
                    "🍆 Хуй: <b>потерян</b>"
                )
            else:
                text = (
                    f"🍆 Выбор пал на <b>{title}</b>.\n\n"
                    "У гнома уже не было хуя, поэтому гиперборейский хуй "
                    "разорвал его на хуйные молекулы.\n\n"
                    "💀 Очки: <b>0 / 100</b>\n"
                    "🍆 Хуй: <b>потерян</b>"
                )
            await query.answer("Два другому. Выбор сделан.")
        else:
            if event_type == "arthur":
                text = (
                    f"⚔️ Выбор пал на <b>{title}</b>, но ничего не произошло.\n\n"
                    "Хуй Короля Артура остался в камне."
                )
            else:
                text = (
                    f"🍆 Выбор пал на <b>{title}</b>, но ничего не произошло.\n\n"
                    "Гиперборейский хуй молча исчез."
                )
            await query.answer("Два другому. Ничего не произошло.")

        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception:
            logging.exception(
                "Не удалось отправить результат выбора другого игрока в чате %s",
                chat_id,
            )
        return

    title = format_user_title(
        get_or_create_duel_user(
            query.from_user,
            chat_id,
        )
    )

    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=event["message_id"],
            reply_markup=None,
        )
    except Exception:
        logging.exception(
            "Не удалось убрать кнопку события "
            "в чате %s",
            chat_id,
        )

    # ========================================================
    # ХУЙ БЫЛ УКРАДЕН — ИГРОК ПЫТАЕТСЯ ВЫТАЩИТЬ ЕГО
    # ========================================================

    if result == "restored":

        if event_type == "arthur":
            await query.answer(
                "НЕ СМОГ ВЫТАЩИТЬ ХУЙ КОРОЛЯ АРТУРА. НО ОН ВСЁ РАВНО ВЕРНУЛСЯ! 🍆",
                show_alert=True,
            )

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⚔️ <b>{title}</b> попытался вытащить "
                        f"<b>ХУЙ КОРОЛЯ АРТУРА</b>.\n\n"
                        "❌ Не смог вытащить хуй.\n\n"
                        "Но легендарный хуй каким-то образом "
                        "сам вернулся к своему владельцу.\n\n"
                        "🍆 <b>ХУЙ ВСЁ РАВНО ВОЗВРАЩЁН.</b>"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                logging.exception(
                    "Не удалось отправить сообщение о возвращении "
                    "хуя Короля Артура в чате %s",
                    chat_id,
                )

            return

        await query.answer(
            "ХУЙ ВОЗВРАЩЁН! 🍆",
            show_alert=True,
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🍆 <b>{title}</b> схватил гиперборейский хуй "
                    f"и вернул себе свой собственный."
                ),
                parse_mode="HTML",
            )
        except Exception:
            logging.exception(
                "Не удалось отправить сообщение о возвращении "
                "гиперборейского хуя в чате %s",
                chat_id,
            )

        return

    # ========================================================
    # У ИГРОКА УЖЕ ЕСТЬ ХУЙ — ХУЙ РАЗРЫВАЕТ ЕГО НА МОЛЕКУЛЫ
    # ========================================================

    if event_type == "arthur":
        await query.answer(
            "НЕ СМОГ ВЫТАЩИТЬ ХУЙ КОРОЛЯ АРТУРА. ТЕБЯ РАЗОРВАЛО НА ВЕЛИЧЕСТВЕННЫЕ ХУЙНЫЕ МОЛЕКУЛЫ.",
            show_alert=True,
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚔️ <b>{title}</b> попытался вытащить "
                    f"<b>ХУЙ КОРОЛЯ АРТУРА</b>.\n\n"
                    "❌ Не смог вытащить хуй.\n\n"
                    "💥 Но Хуй Короля Артура не потерпел "
                    "такого надругательства над своим величием.\n\n"
                    "Тело гнома разорвало на "
                    "<b>величественные хуйные молекулы</b>.\n\n"
                    "💀 Очки: <b>0 / 100</b>\n"
                    "🍆 Хуй: <b>УНИЧТОЖЕН</b>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            logging.exception(
                "Не удалось отправить сообщение о взрыве "
                "от хуя Короля Артура в чате %s",
                chat_id,
            )

        return

    # ========================================================
    # ОБЫЧНЫЙ ГИПЕРБОРЕЙСКИЙ ХУЙ
    # ========================================================

    await query.answer(
        "ТЕБЯ РАЗОРВАЛО НА ХУЙНЫЕ МОЛЕКУЛЫ.",
        show_alert=True,
    )

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"💥 <b>{title}</b> попытался схватить "
                f"гиперборейский хуй.\n\n"
                "От передозировки хуев гнома разорвало "
                "на хуйные молекулы.\n\n"
                "💀 Очки: <b>0 / 100</b>\n"
                "🍆 Хуй: <b>потерян</b>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        logging.exception(
            "Не удалось отправить сообщение о взрыве "
            "гнома в чате %s",
            chat_id,
        )
