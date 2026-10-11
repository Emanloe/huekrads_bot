import logging

from telegram import Update, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes


AUTO_DELETE_DELAY = 60
_MESSAGE_TO_DELETE_NOT_FOUND = "message to delete not found"


def is_message_to_delete_not_found(error: Exception) -> bool:
    return (
        isinstance(error, BadRequest)
        and str(error).strip().casefold() == _MESSAGE_TO_DELETE_NOT_FOUND
    )


async def delete_messages_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data.get("chat_id")
    message_ids = job_data.get("message_ids", [])
    battle_id = job_data.get("battle_id")
    round_num = job_data.get("round")
    prefix = "BATTLE_MESSAGE" if battle_id is not None else "TEMP_MESSAGE"

    for msg_id in message_ids:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=msg_id,
            )
            logging.info(
                "%s_DELETE_OK chat_id=%s battle_id=%s round=%s message_id=%s",
                prefix, chat_id, battle_id, round_num, msg_id,
            )
        except BadRequest as exc:
            if is_message_to_delete_not_found(exc):
                logging.info(
                    "%s_DELETE_ALREADY_ABSENT chat_id=%s battle_id=%s round=%s message_id=%s",
                    prefix, chat_id, battle_id, round_num, msg_id,
                )
                continue
            logging.exception(
                "%s_DELETE_FAILED chat_id=%s battle_id=%s round=%s message_id=%s",
                prefix, chat_id, battle_id, round_num, msg_id,
            )
        except Exception:
            logging.exception(
                "%s_DELETE_FAILED chat_id=%s battle_id=%s round=%s message_id=%s",
                prefix, chat_id, battle_id, round_num, msg_id,
            )


def schedule_auto_delete(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_ids: list[int],
    delay: int = AUTO_DELETE_DELAY,
    *,
    battle_id: str | None = None,
    round_num: int | None = None,
):
    prefix = "BATTLE_MESSAGE" if battle_id is not None else "TEMP_MESSAGE"
    if context.job_queue:
        data = {"chat_id": chat_id, "message_ids": list(message_ids)}
        if battle_id is not None:
            data.update({"battle_id": battle_id, "round": round_num})
        context.job_queue.run_once(
            delete_messages_job,
            when=delay,
            data=data,
        )
        for message_id in message_ids:
            logging.info(
                "%s_DELETE_SCHEDULED chat_id=%s battle_id=%s round=%s message_id=%s delay=%s",
                prefix, chat_id, battle_id, round_num, message_id, delay,
            )
    else:
        logging.warning(
            "%s_DELETE_UNAVAILABLE chat_id=%s battle_id=%s round=%s message_ids=%s",
            prefix, chat_id, battle_id, round_num, list(message_ids),
        )


async def send_and_schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: InlineKeyboardMarkup = None,
    parse_mode: str = "HTML",
):
    chat_id = update.effective_chat.id

    msg_id_to_delete = (
        update.message.message_id
        if update.message
        else None
    )

    try:
        if update.message:
            bot_msg = await update.message.reply_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        else:
            bot_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )

    except Exception:
        bot_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )

    to_delete = [bot_msg.message_id]

    if msg_id_to_delete:
        to_delete.append(msg_id_to_delete)

    schedule_auto_delete(
        context,
        chat_id=chat_id,
        message_ids=to_delete,
    )
