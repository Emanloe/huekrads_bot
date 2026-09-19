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
        "handlers.duel_items",
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
        "duel_item_event_job", "duel_item_event_callback",
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
        "duel_item_claim_123": r"^duel_item_claim_\d+$",
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
@pytest.mark.parametrize("already_registered", [False, True])
async def test_duel_item_periodic_job_registration_is_named_and_not_duplicated(
    monkeypatch,
    already_registered,
):
    import bot

    repeating = []

    class FakeJobQueue:
        def run_daily(self, *_args, **_kwargs):
            pass

        def run_repeating(self, callback, **kwargs):
            repeating.append((callback, kwargs))

        def get_jobs_by_name(self, name):
            if name == "duel_item_event_job" and already_registered:
                return [object()]
            return []

    class FakeApplication:
        job_queue = FakeJobQueue()

        def add_handler(self, _handler):
            pass

        def add_error_handler(self, _handler):
            pass

        async def run_polling(self, **_kwargs):
            pass

    class FakeBuilder:
        def token(self, _token):
            return self

        def post_init(self, _callback):
            return self

        def build(self):
            return FakeApplication()

    monkeypatch.setattr(bot.nest_asyncio, "apply", lambda: None)
    monkeypatch.setattr(bot, "init_db", lambda: None)
    monkeypatch.setattr(bot.Application, "builder", lambda: FakeBuilder())
    monkeypatch.setattr(bot, "schedule_past_pizda_job", lambda _queue: None)
    await bot.main()

    item_jobs = [entry for entry in repeating if entry[0] is bot.duel_item_event_job]
    assert len(item_jobs) == (0 if already_registered else 1)
    if item_jobs:
        assert item_jobs[0][1] == {
            "interval": bot.DUEL_ITEM_EVENT_CHECK_MINUTES * 60,
            "first": 120,
            "name": "duel_item_event_job",
        }


@pytest.mark.asyncio
async def test_duel_item_callback_handler_is_registered(monkeypatch):
    from telegram.ext import CallbackQueryHandler
    import bot

    handlers = []

    class FakeApplication:
        job_queue = None

        def add_handler(self, handler):
            handlers.append(handler)

        def add_error_handler(self, _handler):
            pass

        async def run_polling(self, **_kwargs):
            pass

    class FakeBuilder:
        def token(self, _token):
            return self

        def post_init(self, _callback):
            return self

        def build(self):
            return FakeApplication()

    monkeypatch.setattr(bot.nest_asyncio, "apply", lambda: None)
    monkeypatch.setattr(bot, "init_db", lambda: None)
    monkeypatch.setattr(bot.Application, "builder", lambda: FakeBuilder())
    await bot.main()

    item_handler = next(
        handler
        for handler in handlers
        if isinstance(handler, CallbackQueryHandler)
        and handler.callback is bot.duel_item_event_callback
    )
    assert item_handler.pattern.pattern == r"^duel_item_claim_\d+$"


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
