import datetime
import logging

import nest_asyncio
import pytz

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from config import (
    BOT_TOKEN,
    GAME_HOUR,
    GAME_MINUTE,
    DUEL_TIMEZONE,
)
from database import (
    init_db,
    set_boss_enabled,
)

from handlers.commands import (
    start_command,
    help_command,
    top_command,
    force_pidor_command,
    set_bday_command,
    toggle_forward_reply_command,
    toggle_autodelete_command,
)

from handlers.game import (
    daily_beauty_job,
)

from handlers.past_pizda import (
    schedule_past_pizda_job,
)

from handlers.triggers import (
    respond_trigger,
)

from handlers.utils import (
    get_file_id_handler,
    error_handler,
)

from handlers.weather import (
    weather_inline_query,
    weather_chosen_inline_result,
    weather_stub_callback,
)

from handlers.duel import (
    duel_command,
    duel_select_callback,
    duel_action_callback,
    duel_stats_command,
    duel_top_command,
    duel_delete_command,
    boss_daily_job,
    boss_callback,
    boss_command,
    boss_reg_command,
    hyperboreic_huy_daily_job,
    hyperboreic_huy_callback,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


BOT_COMMANDS = [
    BotCommand("start", "Запустить бота"),
    BotCommand("help", "Хелп по командам"),
    BotCommand("top", "Топ пидоров"),
    BotCommand("force_pidor", "Назначить пидора"),
    BotCommand("setbday", "Установить день рождения"),
    BotCommand("toggle_forward", "Переключить пересылку"),
    BotCommand("toggle_autodelete", "Автоудаление сообщений"),
    BotCommand("duel", "Гномья дуэль на ножах"),
    BotCommand("duel_stats", "Статистика дуэлей"),
    BotCommand("duel_top", "Топ дуэлянтов"),
    BotCommand("duel_delete", "Удалить игрока дуэлей"),
    BotCommand("boss", "Запустить босса"),
    BotCommand("boss_reg", "Записаться на босса"),
]


async def post_init(application: Application):
    try:
        await application.bot.set_my_commands(
            BOT_COMMANDS,
            scope=BotCommandScopeAllGroupChats(),
        )
    except Exception:
        logger.exception("Не удалось установить команды бота")


async def bot_chat_member_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Отслеживает добавление/удаление бота из чата.

    При добавлении бота включаем ежедневного босса.
    При удалении — выключаем.
    """
    member_update = update.my_chat_member

    if not member_update:
        return

    chat_id = member_update.chat.id

    old_status = member_update.old_chat_member.status
    new_status = member_update.new_chat_member.status

    bot_is_in_chat = new_status in {
        "member",
        "administrator",
    }

    was_in_chat = old_status in {
        "member",
        "administrator",
    }

    if bot_is_in_chat and not was_in_chat:
        set_boss_enabled(chat_id, True)

    elif was_in_chat and not bot_is_in_chat:
        set_boss_enabled(chat_id, False)


async def main():
    nest_asyncio.apply()

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ============================================================
    # JOBS
    # ============================================================

    if application.job_queue:
        tz = pytz.timezone(DUEL_TIMEZONE)

        # Ежедневная игра / красотка
        application.job_queue.run_daily(
            daily_beauty_job,
            time=datetime.time(
                GAME_HOUR,
                GAME_MINUTE,
                tzinfo=tz,
            ),
            name="beauty_daily_job",
        )

        # Ежедневный босс в 18:00
        application.job_queue.run_daily(
            boss_daily_job,
            time=datetime.time(
                18,
                0,
                tzinfo=tz,
            ),
            name="boss_daily_job",
        )

        # Независимое событие:
        # 4% шанс на каждой проверке, проверка каждые 15 минут.
        application.job_queue.run_repeating(
            hyperboreic_huy_daily_job,
            interval=15 * 60,
            first=60,
            name="hyperboreic_huy_job",
        )

        # Старое событие «прошлая пизда»
        schedule_past_pizda_job(
            application.job_queue
        )

    # ============================================================
    # CHAT MEMBER
    # ============================================================

    application.add_handler(
        ChatMemberHandler(
            bot_chat_member_update,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    # ============================================================
    # COMMANDS
    # ============================================================

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "top",
            top_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "force_pidor",
            force_pidor_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "setbday",
            set_bday_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "toggle_forward",
            toggle_forward_reply_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "toggle_autodelete",
            toggle_autodelete_command,
        )
    )

    # ============================================================
    # WEATHER
    # ============================================================

    application.add_handler(
        InlineQueryHandler(
            weather_inline_query
        )
    )

    application.add_handler(
        ChosenInlineResultHandler(
            weather_chosen_inline_result
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            weather_stub_callback,
            pattern=r"^wx",
        )
    )

    # ============================================================
    # DUEL
    # ============================================================

    application.add_handler(
        CommandHandler(
            "duel",
            duel_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            duel_select_callback,
            pattern=r"^start_duel_",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            duel_action_callback,
            pattern=r"^duel_(strike|block)_",
        )
    )

    application.add_handler(
        CommandHandler(
            "duel_stats",
            duel_stats_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "duel_top",
            duel_top_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "duel_delete",
            duel_delete_command,
        )
    )

    # ============================================================
    # BOSS
    # ============================================================

    application.add_handler(
        CommandHandler(
            "boss",
            boss_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "boss_reg",
            boss_reg_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            boss_callback,
            pattern=r"^boss_(join|attack_|block_)",
        )
    )

    # ============================================================
    # HYPERBOREIC HUY
    # ============================================================

    application.add_handler(
        CallbackQueryHandler(
            hyperboreic_huy_callback,
            pattern=r"^hyperboreic_huy$",
        )
    )

    # ============================================================
    # PRIVATE MEDIA
    # ============================================================

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & ~filters.COMMAND,
            get_file_id_handler,
        )
    )

    # ============================================================
    # TEXT TRIGGERS
    # ============================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            respond_trigger,
        )
    )

    # ============================================================
    # ERRORS
    # ============================================================

    application.add_error_handler(
        error_handler
    )

    logger.info("Bot starting...")

    await application.run_polling(
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())