import importlib
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        "duel_top_command", "duel_delete_command", "gnomed_command", "boss_daily_job", "boss_callback",
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

    assert ("duel_app", "Дуэли: мини-приложение") in [
        (command.command, command.description) for command in bot.BOT_COMMANDS
    ]
    assert [(command.command, command.description) for command in bot.BOT_COMMANDS
            if command.command != "duel_app"] == [
        ("start", "Запустить бота"),
        ("help", "Хелп по командам"),
        ("donate", "Поддержать проект"),
        ("top", "Топ пидоров"),
        ("force_pidor", "Назначить пидора"),
        ("setbday", "Установить день рождения"),
        ("toggle_forward", "Переключить пересылку"),
        ("toggle_autodelete", "Автоудаление сообщений"),
        ("duel", "Гномья дуэль на ножах"),
        ("name", "Назвать своего гнома"),
        ("summary", "Месячная статистика чата"),
        ("dig", "Копать за 10 очков"),
        ("ball", "Активировать элитный мячик"),
        ("duel_stats", "Статистика дуэлей"),
        ("duel_top", "Топ дуэлянтов"),
        ("duel_delete", "Удалить игрока дуэлей"),
        ("boss", "Запустить босса"),
        ("boss_reg", "Записаться на босса"),
    ]


@pytest.mark.asyncio
async def test_donate_and_gnomed_command_handlers_are_registered_once(monkeypatch):
    from telegram.ext import CommandHandler
    import bot

    registered_handlers = []

    class FakeApplication:
        job_queue = None

        def add_handler(self, handler, group=0):
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

    monkeypatch.setattr(bot, "run_ptb_and_http", AsyncMock())
    monkeypatch.setattr(bot, "init_db", lambda: None)
    monkeypatch.setattr(bot.Application, "builder", lambda: FakeBuilder())

    await bot.main()

    for callback, command in (
        (bot.donate_command, "donate"),
        (bot.gnomed_command, "gnomed"),
        (bot.duel_app_command, "duel_app"),
        (bot.name_command, "name"),
        (bot.summary_command, "summary"),
        (bot.dig_command, "dig"),
    ):
        handlers = [
            item
            for item in registered_handlers
            if isinstance(item, CommandHandler) and item.callback is callback
        ]
        assert len(handlers) == 1
        assert handlers[0].commands == frozenset({command})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_item_ids", "expected_inventory", "forbidden_inventory"),
    [
        ([], "Промасленная жилетка, Нож", "Инвентарь:</b> пусто"),
        (
            ["vevangel_wing"],
            "Промасленная жилетка, Нож, Крыло Вевангела",
            "Инвентарь:</b> пусто",
        ),
        (
            ["vevangel_wing", "vevangel_wing"],
            "Промасленная жилетка, Нож, Крыло Вевангела ×2",
            "Инвентарь:</b> пусто",
        ),
        (
            ["oiled_vest", "knife"],
            "Промасленная жилетка, Нож",
            "Промасленная жилетка ×2",
        ),
    ],
)
async def test_registered_duel_stats_handler_sends_virtual_base_inventory(
    monkeypatch,
    temp_database,
    fake_context,
    stored_item_ids,
    expected_inventory,
    forbidden_inventory,
):
    from telegram.ext import CommandHandler
    import bot
    import database as db

    registered_handlers = []

    class FakeApplication:
        job_queue = None

        def add_handler(self, handler, group=0):
            registered_handlers.append(handler)

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

    monkeypatch.setattr(bot, "run_ptb_and_http", AsyncMock())
    monkeypatch.setattr(bot, "init_db", lambda: None)
    monkeypatch.setattr(bot.Application, "builder", lambda: FakeBuilder())
    await bot.main()

    handlers = [
        handler
        for handler in registered_handlers
        if isinstance(handler, CommandHandler)
        and handler.commands == frozenset({"duel_stats"})
    ]
    assert len(handlers) == 1
    assert handlers[0].callback is bot.duel_stats_command

    chat_id = -707
    user = SimpleNamespace(
        id=707,
        username="runtime_dwarf",
        first_name="Runtime",
        last_name=None,
        is_bot=False,
    )
    db.get_or_create_duel_user(user, chat_id)
    for item_id in stored_item_ids:
        db.add_duel_inventory_item(chat_id, user.id, item_id)
    if not stored_item_ids:
        assert db.get_duel_inventory(chat_id, user.id) == []

    reply_text = AsyncMock(return_value=SimpleNamespace(message_id=9001))
    message = SimpleNamespace(
        from_user=user,
        chat=SimpleNamespace(id=chat_id),
        chat_id=chat_id,
        message_id=9000,
        reply_text=reply_text,
    )
    update = SimpleNamespace(
        message=message,
        effective_chat=message.chat,
    )

    await handlers[0].callback(update, fake_context)

    reply_text.assert_awaited_once()
    final_output = reply_text.await_args.args[0]
    assert f"<b>Инвентарь:</b> {expected_inventory}" in final_output
    assert forbidden_inventory not in final_output
    assert "Промасленная жилетка ×2" not in final_output
    assert "Нож ×2" not in final_output


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
async def test_boss_daily_job_is_scheduled_at_1337_moscow(monkeypatch):
    import bot

    daily_jobs = []

    class FakeJobQueue:
        def run_daily(self, callback, **kwargs):
            daily_jobs.append((callback, kwargs))

        def run_monthly(self, *_args, **_kwargs):
            pass

        def run_repeating(self, *_args, **_kwargs):
            pass

        def get_jobs_by_name(self, _name):
            return [object()]

    class FakeApplication:
        job_queue = FakeJobQueue()

        def add_handler(self, _handler, group=0):
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

    monkeypatch.setattr(bot, "run_ptb_and_http", AsyncMock())
    monkeypatch.setattr(bot, "init_db", lambda: None)
    monkeypatch.setattr(bot.Application, "builder", lambda: FakeBuilder())
    monkeypatch.setattr(bot, "schedule_past_pizda_job", lambda _queue: None)

    await bot.main()

    boss_jobs = [entry for entry in daily_jobs if entry[0] is bot.boss_daily_job]
    assert len(boss_jobs) == 1
    scheduled_time = boss_jobs[0][1]["time"]
    assert scheduled_time.hour == 13
    assert scheduled_time.minute == 37
    assert scheduled_time.tzinfo is not None
    assert scheduled_time.tzinfo.zone == "Europe/Moscow"
    assert bot.DUEL_TIMEZONE == "Europe/Moscow"
    assert boss_jobs[0][1]["name"] == "boss_daily_job"


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

        def run_monthly(self, *_args, **_kwargs):
            pass

        def run_repeating(self, callback, **kwargs):
            repeating.append((callback, kwargs))

        def get_jobs_by_name(self, name):
            if name == "duel_item_event_job" and already_registered:
                return [object()]
            return []

    class FakeApplication:
        job_queue = FakeJobQueue()

        def add_handler(self, _handler, group=0):
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

    monkeypatch.setattr(bot, "run_ptb_and_http", AsyncMock())
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
    assert [entry for entry in repeating if entry[0] is bot.huecrab_event_job] == [
        (bot.huecrab_event_job, {
            "interval": bot.HUECRAB_CHECK_MINUTES * 60,
            "first": 180,
            "name": "huecrab_event_job",
        })
    ]
    assert [entry for entry in repeating if entry[0] is bot.huecrab_autoloot_job] == [
        (bot.huecrab_autoloot_job, {
            "interval": 5,
            "first": 5,
            "name": "huecrab_autoloot_job",
        })
    ]


@pytest.mark.asyncio
async def test_duel_item_callback_handler_is_registered(monkeypatch):
    from telegram.ext import CallbackQueryHandler
    import bot

    handlers = []

    class FakeApplication:
        job_queue = None

        def add_handler(self, handler, group=0):
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

    monkeypatch.setattr(bot, "run_ptb_and_http", AsyncMock())
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
    pet_handler = next(
        handler for handler in handlers
        if isinstance(handler, CallbackQueryHandler)
        and handler.callback is bot.huecrab_tame_callback
    )
    assert pet_handler.pattern.pattern == r"^huecrab_tame_\d+$"


@pytest.mark.asyncio
async def test_registered_hyperborean_callback_handler_routes_all_supported_payloads(monkeypatch):
    from telegram import CallbackQuery, Update, User
    from telegram.ext import CallbackQueryHandler
    import bot

    registered_handlers = []

    class FakeApplication:
        job_queue = None

        def add_handler(self, handler, group=0):
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

    monkeypatch.setattr(bot, "run_ptb_and_http", AsyncMock())
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
