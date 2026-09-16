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
from text_resources import get_text


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
    await reply_or_send(update, context, get_text("commands.start.greeting"))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    if not update.message:
        return

    schedule_auto_delete(context, update.message)
    await reply_or_send(
        update,
        context,
        get_text("commands.help.link"),
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
            get_text("commands.top.empty"),
        )
        return

    text = get_text("commands.top.header")
    for idx, (username, count) in enumerate(top_list, 1):
        clean_username = username.lstrip("@") if username else get_text("commands.top.anonymous")
        text += get_text("commands.top.item", index=idx, username=clean_username, count=count)

    await reply_or_send(update, context, text, parse_mode="HTML")


async def force_pidor_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск выбора пидора дня"""
    if not update.message or not update.message.from_user:
        return

    schedule_auto_delete(context, update.message)
    if not is_admin(update.message.from_user.id):
        await reply_or_send(
            update, context, get_text("commands.admin.only")
        )
        return

    chat_id = update.message.chat_id
    result = pick_beauty_of_the_day(chat_id)
    if not result:
        await reply_or_send(update, context, get_text("commands.force_pidor.no_candidates"))
        return

    winner_tag, count = result
    clean_tag = winner_tag.lstrip("@") if winner_tag else get_text("commands.top.anonymous")
    await reply_or_send(
        update,
        context,
        get_text("commands.force_pidor.winner", username=clean_tag, count=count),
    )


async def set_bday_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка дня рождения пользователя: /setbday @username DD.MM"""
    if not update.message or not update.message.from_user:
        return

    schedule_auto_delete(context, update.message)
    if not is_admin(update.message.from_user.id):
        await reply_or_send(
            update, context, get_text("commands.admin.only")
        )
        return

    args = context.args
    if len(args) < 2:
        await reply_or_send(
            update,
            context,
            get_text("commands.birthday.usage"),
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
            get_text("commands.birthday.saved", username=username, birthdate=bday_str),
        )
    else:
        await reply_or_send(
            update,
            context,
            get_text("commands.birthday.missing", username=username),
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
            update, context, get_text("commands.admin.only")
        )
        return

    chat_id = update.message.chat_id
    current_state = is_forward_reply_enabled(chat_id)
    new_state = not current_state

    set_forward_reply_enabled(chat_id, new_state)
    status = get_text("commands.toggle_forward.status.enabled") if new_state else get_text("commands.toggle_forward.status.disabled")
    await reply_or_send(
        update, context, get_text("commands.toggle_forward.message", status=status)
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
            update, context, get_text("commands.admin.only")
        )
        return

    chat_id = update.message.chat_id
    current_state = is_auto_delete_enabled(chat_id)
    new_state = not current_state

    set_auto_delete_enabled(chat_id, new_state)
    status = get_text("commands.toggle_autodelete.status.enabled") if new_state else get_text("commands.toggle_autodelete.status.disabled")
    await reply_or_send(update, context, get_text("commands.toggle_autodelete.message", status=status))
