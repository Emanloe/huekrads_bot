import importlib
import logging
import re

import pytest


def test_runtime_modules_import():
    for name in (
        "config",
        "database",
        "handlers.commands",
        "handlers.game",
        "handlers.past_pizda",
        "handlers.triggers",
        "handlers.utils",
        "handlers.weather",
        "handlers.duel",
        "bot",
    ):
        assert importlib.import_module(name)


def test_bot_public_import_contracts():
    import bot

    names = (
        "init_db", "set_boss_enabled", "start_command", "donate_command", "top_command",
        "force_pidor_command", "set_bday_command", "toggle_forward_reply_command",
        "toggle_autodelete_command", "daily_beauty_job", "schedule_past_pizda_job",
        "respond_trigger", "get_file_id_handler", "error_handler", "weather_inline_query",
        "weather_chosen_inline_result", "weather_stub_callback", "duel_command",
        "duel_select_callback", "duel_action_callback", "duel_stats_command",
        "duel_top_command", "duel_delete_command", "boss_daily_job", "boss_callback",
        "boss_command", "boss_reg_command", "hyperboreic_huy_daily_job",
        "hyperboreic_huy_callback",
    )
    for name in names:
        assert callable(getattr(bot, name))


def test_bot_suppresses_http_client_info_logs():
    import bot  # noqa: F401

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_bot_command_menu_preserves_descriptions_and_order():
    import bot

    assert [(command.command, command.description) for command in bot.BOT_COMMANDS] == [
        ("start", "Запустить бота"),
        ("help", "Хелп по командам"),
        ("donate", "Поддержать проект"),
        ("top", "Топ пидоров"),
        ("force_pidor", "Назначить пидора"),
        ("setbday", "Установить день рождения"),
        ("toggle_forward", "Переключить пересылку"),
        ("toggle_autodelete", "Автоудаление сообщений"),
        ("duel", "Гномья дуэль на ножах"),
        ("duel_stats", "Статистика дуэлей"),
        ("duel_top", "Топ дуэлянтов"),
        ("duel_delete", "Удалить игрока дуэлей"),
        ("boss", "Запустить босса"),
        ("boss_reg", "Записаться на босса"),
    ]


@pytest.mark.asyncio
async def test_donate_command_handler_is_registered(monkeypatch):
    from telegram.ext import CommandHandler
    import bot

    registered_handlers = []

    class FakeApplication:
        job_queue = None

        def add_handler(self, handler):
            registered_handlers.append(handler)

        def add_error_handler(self, _handler):
            pass

        async def run_polling(self, **_kwargs):
            pass

    class FakeBuilder:
        def __init__(self):
            self.application = FakeApplication()

        def token(self, _token):
            return self

        def post_init(self, _callback):
            return self

        def build(self):
            return self.application

    monkeypatch.setattr(bot.nest_asyncio, "apply", lambda: None)
    monkeypatch.setattr(bot, "init_db", lambda: None)
    monkeypatch.setattr(bot.Application, "builder", lambda: FakeBuilder())

    await bot.main()

    handler = next(
        item
        for item in registered_handlers
        if isinstance(item, CommandHandler) and item.callback is bot.donate_command
    )
    assert handler.commands == frozenset({"donate"})


def test_callback_handler_patterns_are_stable():
    patterns = {
        "wx": r"^wx",
        "start_duel_tester": r"^start_duel_",
        "duel_strike_head_1": r"^duel_(strike|block)_",
        "duel_block_dick_99": r"^duel_(strike|block)_",
        "boss_join": r"^boss_(join|attack_|block_)",
        "boss_attack_body_3": r"^boss_(join|attack_|block_)",
        "boss_block_dick_3": r"^boss_(join|attack_|block_)",
        "hyperboreic_huy": r"^hyperboreic_huy(?:_(?:self|other))?$",
        "hyperboreic_huy_self": r"^hyperboreic_huy(?:_(?:self|other))?$",
        "hyperboreic_huy_other": r"^hyperboreic_huy(?:_(?:self|other))?$",
    }
    for payload, pattern in patterns.items():
        assert re.search(pattern, payload)


@pytest.mark.asyncio
async def test_registered_hyperborean_callback_handler_routes_all_supported_payloads(monkeypatch):
    from telegram import CallbackQuery, Update, User
    from telegram.ext import CallbackQueryHandler
    import bot

    registered_handlers = []

    class FakeApplication:
        job_queue = None

        def add_handler(self, handler):
            registered_handlers.append(handler)

        def add_error_handler(self, _handler):
            pass

        async def run_polling(self, **_kwargs):
            pass

    class FakeBuilder:
        def __init__(self):
            self.application = FakeApplication()

        def token(self, _token):
            return self

        def post_init(self, _callback):
            return self

        def build(self):
            return self.application

    monkeypatch.setattr(bot.nest_asyncio, "apply", lambda: None)
    monkeypatch.setattr(bot, "init_db", lambda: None)
    monkeypatch.setattr(bot.Application, "builder", lambda: FakeBuilder())

    await bot.main()

    handler = next(
        item
        for item in registered_handlers
        if isinstance(item, CallbackQueryHandler)
        and item.callback is bot.hyperboreic_huy_callback
    )
    handler_index = registered_handlers.index(handler)

    def update_with(data):
        return Update(
            update_id=1,
            callback_query=CallbackQuery(
                id="test-callback",
                from_user=User(id=1, first_name="Tester", is_bot=False),
                chat_instance="test-chat",
                data=data,
            ),
        )

    for payload in (
        "hyperboreic_huy",
        "hyperboreic_huy_self",
        "hyperboreic_huy_other",
    ):
        assert handler.check_update(update_with(payload))
        assert not any(
            item.check_update(update_with(payload))
            for item in registered_handlers[:handler_index]
            if isinstance(item, CallbackQueryHandler)
        )

    assert not handler.check_update(update_with("hyperboreic_huy_unknown"))
    assert not handler.check_update(update_with("duel_strike_head_1"))
