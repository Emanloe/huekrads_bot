from telegram import Update
from telegram.ext import ContextTypes
from database import (
    get_top_beauties,
    pick_beauty_of_the_day,
    save_or_update_user,
    save_custom_birthdate,
    is_forward_reply_enabled,
    set_forward_reply_enabled,
    is_auto_delete_enabled,
    set_auto_delete_enabled,
)
from handlers.utils import is_admin, reply_or_send, delete_messages_job


def schedule_auto_delete(context: ContextTypes.DEFAULT_TYPE, message):
    """Вспомогательная функция для планирования автоудаления сообщения пользователя."""
    if not message or not message.chat_id:
        return
    if is_auto_delete_enabled(message.chat_id) and context.job_queue:
        context.job_queue.run_once(
            delete_messages_job,
            when=3,
            data={"chat_id": message.chat_id, "message_id": message.message_id},
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    if not update.message or not update.message.from_user:
        return

    save_or_update_user(update.message.from_user, update.message.chat_id)
    await reply_or_send(update, context, "Свобода. Равенство. Пошёл нахуй.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    if not update.message:
        return

    schedule_auto_delete(context, update.message)
    await reply_or_send(
        update,
        context,
        "Хуекрады здесь — https://t.me/huehuehuekrads",
    )


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /top — статистика пидоров дня"""
    if not update.message:
        return

    chat_id = update.message.chat_id
    schedule_auto_delete(context, update.message)

    top_list = get_top_beauties(chat_id, limit=10)
    if not top_list:
        await reply_or_send(
            update,
            context,
            "В этом чате пока нет участников или никто ещё не побеждал.",
        )
        return

    text = "🏆 <b>Топ пидоров чата:</b>\n\n"
    for idx, (username, count) in enumerate(top_list, 1):
        clean_username = username.lstrip("@") if username else "Аноним"
        text += f"{idx}. <b>{clean_username}</b> — {count} раз(а)\n"

    await reply_or_send(update, context, text, parse_mode="HTML")


async def force_pidor_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск выбора пидора дня"""
    if not update.message or not update.message.from_user:
        return

    schedule_auto_delete(context, update.message)
    if not is_admin(update.message.from_user.id):
        await reply_or_send(
            update, context, "⛔ Эта команда доступна только администраторам."
        )
        return

    chat_id = update.message.chat_id
    result = pick_beauty_of_the_day(chat_id)
    if not result:
        await reply_or_send(update, context, "Нет кандидатов для проведения игры.")
        return

    winner_tag, count = result
    clean_tag = winner_tag.lstrip("@") if winner_tag else "Аноним"
    await reply_or_send(
        update,
        context,
        f"Пидор дня — {clean_tag}! Он был пидором уже {count} раз(а).",
    )


async def set_bday_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка дня рождения пользователя: /setbday @username DD.MM"""
    if not update.message or not update.message.from_user:
        return

    schedule_auto_delete(context, update.message)
    if not is_admin(update.message.from_user.id):
        await reply_or_send(
            update, context, "⛔ Эта команда доступна только администраторам."
        )
        return

    args = context.args
    if len(args) < 2:
        await reply_or_send(
            update,
            context,
            "Использование: `/setbday @username DD.MM`",
            parse_mode="Markdown",
        )
        return

    username = args[0].lstrip("@")
    bday_str = args[1]

    updated = save_custom_birthdate(update.message.chat_id, username, bday_str)
    if updated:
        await reply_or_send(
            update,
            context,
            f"День рождения для {username} успешно сохранён ({bday_str}).",
        )
    else:
        await reply_or_send(
            update,
            context,
            f"Пользователь {username} не найден в базе данных этого чата.",
        )


async def toggle_forward_reply_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Переключение реакции 'Форвардни себе за щеку'"""
    if not update.message or not update.message.from_user:
        return

    schedule_auto_delete(context, update.message)

    # ПРОВЕРКА НА АДМИНА
    if not is_admin(update.message.from_user.id):
        await reply_or_send(
            update, context, "⛔ Эта команда доступна только администраторам."
        )
        return

    chat_id = update.message.chat_id
    current_state = is_forward_reply_enabled(chat_id)
    new_state = not current_state

    set_forward_reply_enabled(chat_id, new_state)
    status = "включен" if new_state else "выключен"
    await reply_or_send(
        update, context, f"Ответ 'Форвардни себе за щеку' {status}."
    )


# Алиас для поддержания совместимости, если где-то зарегистрировано короткое название
toggle_forward_command = toggle_forward_reply_command


async def toggle_autodelete_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Переключение автоудаления команд бота"""
    if not update.message or not update.message.from_user:
        return

    schedule_auto_delete(context, update.message)

    # ПРОВЕРКА НА АДМИНА
    if not is_admin(update.message.from_user.id):
        await reply_or_send(
            update, context, "⛔ Эта команда доступна только администраторам."
        )
        return

    chat_id = update.message.chat_id
    current_state = is_auto_delete_enabled(chat_id)
    new_state = not current_state

    set_auto_delete_enabled(chat_id, new_state)
    status = "включено" if new_state else "выключено"
    await reply_or_send(update, context, f"Автоудаление команд {status}.")