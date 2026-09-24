import datetime
import logging
import os
from zoneinfo import ZoneInfo

import pytz
import uvicorn

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
    DUEL_ITEM_EVENT_CHECK_MINUTES,
)
from database import (
    init_db,
    set_boss_enabled,
)

from handlers.commands import (
    start_command,
    help_command,
    donate_command,
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
from handlers.inline_query import inline_query_dispatch

from handlers.hyperborean_event import HYPERBOREAN_HUY_CHECK_MINUTES
from text_resources import get_text

from handlers.duel import (
    duel_command,
    duel_select_callback,
    persistent_duel_action_callback as duel_action_callback,
    duel_stats_command,
    duel_top_command,
    duel_delete_command,
    gnomed_command,
    boss_daily_job,
    boss_callback,
    boss_command,
    boss_reg_command,
    hyperboreic_huy_daily_job,
    hyperboreic_huy_callback,
)
from handlers.persistent_duel_publisher import recover_persistent_duels, persistent_duel_worker_job
from handlers.duel_items import (
    duel_item_event_callback,
    duel_item_event_job,
)
from handlers.duel_name import name_command
from handlers.miniapp import duel_app_command
from miniapp_api import create_miniapp_api
from handlers.monthly_summary import monthly_summary_job, summary_command
from handlers.elite_ball import ball_command, elite_ball_callback, elite_ball_question, ELITE_BALL_CALLBACK_DATA
from handlers.dig import dig_command
from handlers.huecrab import (
    HUECRAB_CHECK_MINUTES, huecrab_autoloot_job,
    huecrab_event_job, huecrab_tame_callback,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


BOT_COMMANDS = [
    BotCommand("start", get_text("menu.commands.start")),
    BotCommand("help", get_text("menu.commands.help")),
    BotCommand("donate", get_text("menu.commands.donate")),
    BotCommand("top", get_text("menu.commands.top")),
    BotCommand("force_pidor", get_text("menu.commands.force_pidor")),
    BotCommand("setbday", get_text("menu.commands.setbday")),
    BotCommand("toggle_forward", get_text("menu.commands.toggle_forward")),
    BotCommand("toggle_autodelete", get_text("menu.commands.toggle_autodelete")),
    BotCommand("duel", get_text("menu.commands.duel")),
    BotCommand("duel_app", "Дуэли: мини-приложение"),
    BotCommand("name", get_text("menu.commands.name")),
    BotCommand("summary", get_text("menu.commands.summary")),
    BotCommand("dig", get_text("menu.commands.dig")),
    BotCommand("ball", get_text("menu.commands.ball")),
    BotCommand("duel_stats", get_text("menu.commands.duel_stats")),
    BotCommand("duel_top", get_text("menu.commands.duel_top")),
    BotCommand("duel_delete", get_text("menu.commands.duel_delete")),
    BotCommand("boss", get_text("menu.commands.boss")),
    BotCommand("boss_reg", get_text("menu.commands.boss_reg")),
]


async def post_init(application: Application):
    try:
        await application.bot.set_my_commands(
            BOT_COMMANDS,
            scope=BotCommandScopeAllGroupChats(),
        )
    except Exception:
        logger.exception("Не удалось установить команды бота")


    await recover_persistent_duels(application.bot, job_queue=application.job_queue)


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
        application.job_queue.run_repeating(
            persistent_duel_worker_job, interval=2, first=2,
            name="persistent_duel_worker",
        )
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

        # Ежедневный босс в 13:37
        application.job_queue.run_daily(
            boss_daily_job,
            time=datetime.time(
                13,
                37,
                tzinfo=tz,
            ),
            name="boss_daily_job",
        )

        if not application.job_queue.get_jobs_by_name("monthly_summary_job"):
            application.job_queue.run_monthly(
                monthly_summary_job,
                when=datetime.time(10, 0, tzinfo=ZoneInfo(DUEL_TIMEZONE)),
                day=1,
                name="monthly_summary_job",
            )

        # Независимое событие:
        # Шанс и частота проверки задаются в handlers.hyperborean_event.
        application.job_queue.run_repeating(
            hyperboreic_huy_daily_job,
            interval=HYPERBOREAN_HUY_CHECK_MINUTES * 60,
            first=60,
            name="hyperboreic_huy_job",
        )

        if not application.job_queue.get_jobs_by_name("duel_item_event_job"):
            application.job_queue.run_repeating(
                duel_item_event_job,
                interval=DUEL_ITEM_EVENT_CHECK_MINUTES * 60,
                first=120,
                name="duel_item_event_job",
            )

        application.job_queue.run_repeating(
            huecrab_event_job, interval=HUECRAB_CHECK_MINUTES * 60,
            first=180, name="huecrab_event_job",
        )
        application.job_queue.run_repeating(
            huecrab_autoloot_job, interval=5, first=5,
            name="huecrab_autoloot_job",
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
            "donate",
            donate_command,
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

    application.add_handler(
        CommandHandler(
            "gnomed",
            gnomed_command,
        )
    )

    # ============================================================
    # WEATHER
    # ============================================================

    application.add_handler(
        InlineQueryHandler(
            inline_query_dispatch
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

    application.add_handler(
        CallbackQueryHandler(
            elite_ball_callback,
            pattern=rf"^{ELITE_BALL_CALLBACK_DATA}$",
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
    application.add_handler(CommandHandler("duel_app", duel_app_command))

    application.add_handler(CommandHandler("name", name_command))
    application.add_handler(CommandHandler("summary", summary_command))
    application.add_handler(CommandHandler("dig", dig_command))
    application.add_handler(CommandHandler("ball", ball_command))

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
        CallbackQueryHandler(
            duel_item_event_callback,
            pattern=r"^duel_item_claim_\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            huecrab_tame_callback,
            pattern=r"^huecrab_tame_\d+$",
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
            pattern=r"^hyperboreic_huy(?:_(?:self|other))?$",
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
        MessageHandler(filters.TEXT & ~filters.COMMAND, elite_ball_question),
        group=-1,
    )

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

    await run_ptb_and_http(application)


async def run_ptb_and_http(application: Application, http_server=None) -> None:
    """Keep PTB polling, its jobs, and the one ASGI server on one event loop."""
    if http_server is None:
        http_server = uvicorn.Server(uvicorn.Config(
            create_miniapp_api(), host=os.getenv("MINIAPP_HTTP_HOST", "127.0.0.1"),
            port=int(os.getenv("MINIAPP_HTTP_PORT", "8000")),
            workers=1, lifespan="off", access_log=False,
        ))
    initialized = polling = started = False
    try:
        await application.initialize()
        initialized = True
        if application.post_init is not None:
            await application.post_init(application)
        if application.updater is None:
            raise RuntimeError("PTB polling updater is unavailable")
        await application.updater.start_polling(drop_pending_updates=False)
        polling = True
        await application.start()
        started = True
        await http_server.serve()
    finally:
        try:
            if polling:
                await application.updater.stop()
        finally:
            try:
                if started:
                    await application.stop()
            finally:
                if initialized:
                    await application.shutdown()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
