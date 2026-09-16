import importlib
import re


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
        "init_db", "set_boss_enabled", "start_command", "top_command",
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
