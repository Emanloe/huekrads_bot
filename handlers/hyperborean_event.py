"""Hyperborean huy and Arthur event mechanics."""

import logging
import random
import sqlite3
from datetime import date
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import format_user_title, get_all_chats, get_or_create_duel_user
from text_resources import get_text

HYPERBOREAN_HUY_CHANCE = 0.05
HYPERBOREAN_HUY_CHECK_MINUTES = 60
HYPERBOREAN_HUY_DAILY_LIMIT = 5
ACTIVE_HYPERBOREAN_EVENTS = {}
HYPERBOREAN_HUY_DAILY_SPAWNS = {}
_HYPERBOREAN_DB_PATH = Path(__file__).resolve().parent.parent / "bot_database.db"


def _current_date():
    return date.today()

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

    today = _current_date()
    last_spawn_date, daily_count = HYPERBOREAN_HUY_DAILY_SPAWNS.get(
        chat_id,
        (today, 0),
    )
    if last_spawn_date != today:
        daily_count = 0
    if daily_count >= HYPERBOREAN_HUY_DAILY_LIMIT:
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
        event_text = get_text("hyperborean.spawn.arthur")
    else:
        event_text = get_text("hyperborean.spawn.hyperboreic")

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    get_text("hyperborean.buttons.self"), callback_data="hyperboreic_huy_self"
                ),
                InlineKeyboardButton(
                    get_text("hyperborean.buttons.other"), callback_data="hyperboreic_huy_other"
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
    HYPERBOREAN_HUY_DAILY_SPAWNS[chat_id] = (today, daily_count + 1)


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
            get_text("hyperborean.alerts.taken"),
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
            get_text("hyperborean.alerts.error"),
            show_alert=True,
        )
        return

    if result == "missing":
        ACTIVE_HYPERBOREAN_EVENTS[chat_id] = event

        await query.answer(
            get_text("hyperborean.alerts.missing"),
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
                text = get_text("hyperborean.other.exploded.arthur", title=title)
            else:
                text = get_text("hyperborean.other.exploded.hyperboreic", title=title)
            await query.answer(get_text("hyperborean.other.answer.exploded"))
        else:
            if event_type == "arthur":
                text = get_text("hyperborean.other.unchanged.arthur", title=title)
            else:
                text = get_text("hyperborean.other.unchanged.hyperboreic", title=title)
            await query.answer(get_text("hyperborean.other.answer.unchanged"))

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
                get_text("hyperborean.self.restored.arthur.alert"),
                show_alert=True,
            )

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=get_text("hyperborean.self.restored.arthur.message", title=title),
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
            get_text("hyperborean.self.restored.hyperboreic.alert"),
            show_alert=True,
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=get_text("hyperborean.self.restored.hyperboreic.message", title=title),
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
            get_text("hyperborean.self.exploded.arthur.alert"),
            show_alert=True,
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=get_text("hyperborean.self.exploded.arthur.message", title=title),
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
        get_text("hyperborean.self.exploded.hyperboreic.alert"),
        show_alert=True,
    )

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=get_text("hyperborean.self.exploded.hyperboreic.message", title=title),
            parse_mode="HTML",
        )
    except Exception:
        logging.exception(
            "Не удалось отправить сообщение о взрыве "
            "гнома в чате %s",
            chat_id,
        )
