import asyncio
import logging

from telegram.ext import ContextTypes

from database import pick_beauty_of_the_day, get_all_chats
from text_resources import get_text


logger = logging.getLogger(__name__)


def get_plural_raz(count: int) -> str:
    last_two = count % 100
    last_one = count % 10

    if 11 <= last_two <= 19:
        return get_text("game.plural_times.many")

    if last_one in [2, 3, 4]:
        return get_text("game.plural_times.few")

    return get_text("game.plural_times.one")


async def run_pidor_game_in_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
):
    logger.info(
        "Пидор дня: начинаем обработку chat_id=%s",
        chat_id,
    )

    result = pick_beauty_of_the_day(chat_id)

    logger.info(
        "Пидор дня: результат pick_beauty_of_the_day(%s) = %r",
        chat_id,
        result,
    )

    if result:
        username, count = result
        word = get_plural_raz(count)

        await context.bot.send_message(
            chat_id=chat_id,
            text=get_text("game.messages.selecting"),
        )

        await asyncio.sleep(3)

        await context.bot.send_message(
            chat_id=chat_id,
            text=get_text(
                "game.messages.winner",
                username=username,
                count=count,
                word=word,
            ),
        )

        logger.info(
            "Пидор дня: сообщение успешно отправлено в chat_id=%s, username=%s",
            chat_id,
            username,
        )

    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=get_text("game.messages.no_participants"),
        )

        logger.info(
            "Пидор дня: в chat_id=%s нет зарегистрированных участников",
            chat_id,
        )


async def daily_beauty_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("========== daily_beauty_job ЗАПУЩЕН ==========")

    try:
        chats = get_all_chats()

        logger.info(
            "daily_beauty_job: найдено чатов: %d",
            len(chats),
        )

        logger.info(
            "daily_beauty_job: список чатов: %s",
            chats,
        )

        for chat_id in chats:
            try:
                logger.info(
                    "daily_beauty_job: запускаем игру в chat_id=%s",
                    chat_id,
                )

                await run_pidor_game_in_chat(
                    context,
                    chat_id,
                )

            except Exception:
                logger.exception(
                    "daily_beauty_job: ошибка при обработке chat_id=%s",
                    chat_id,
                )

    except Exception:
        logger.exception(
            "daily_beauty_job: критическая ошибка",
        )

    logger.info("========== daily_beauty_job ЗАВЕРШЁН ==========")
