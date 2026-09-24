import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from text_resources import get_text


CHAT_ID = -4343


def make_user(user_id, username):
    return SimpleNamespace(
        id=user_id,
        username=username,
        first_name=username.title(),
        last_name=None,
        is_bot=False,
    )


def set_duel_stats(
    database_path,
    user_id,
    *,
    points,
    wins,
    losses,
    daily_wins,
    stolen_dicks_count,
    dick_stolen_count,
    dick_stolen_today,
    last_stolen_by,
):
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE duel_users
            SET points = ?, wins = ?, losses = ?, daily_wins = ?,
                stolen_dicks_count = ?, dick_stolen_count = ?,
                dick_stolen_today = ?, last_stolen_by = ?
            WHERE user_id = ? AND chat_id = ?
            """,
            (
                points,
                wins,
                losses,
                daily_wins,
                stolen_dicks_count,
                dick_stolen_count,
                dick_stolen_today,
                last_stolen_by,
                user_id,
                CHAT_ID,
            ),
        )


@pytest.fixture(autouse=True)
def clean_active_duels():
    from handlers import duel

    duel.ACTIVE_DUELS.clear()
    yield
    duel.ACTIVE_DUELS.clear()


@pytest.fixture
def fixed_duel_database(monkeypatch, temp_database):
    import database

    monkeypatch.setattr(database, "_get_today_date_str", lambda: "2030-01-02")
    return temp_database


def test_finish_yaml_templates_preserve_exact_output_structure():
    result = get_text(
        "duel.finish.result",
        custom_text="Финал.\n",
        winner_title="Победитель",
        loser_title="Проигравший",
        winner_points=50,
        loser_points=15,
    )
    stats = get_text(
        "duel.finish.stats",
        rounds_count=2,
        rounds_label="раунда",
        round_flavor="Вкус дуэли.",
    )
    stolen = get_text(
        "duel.finish.stolen",
        loser_title="Проигравший",
        stats_text=stats,
        fact="Факт.",
    )

    assert get_text("duel.finish.error") == "⚠️ Ошибка проведения дуэли. Попробуйте снова."
    assert result == (
        "Финал.\n\n"
        "🗡️ <b>Результаты дуэли:</b>\n\n"
        "Победитель: <b>Победитель</b>\n"
        "Проигравший: <b>Проигравший</b>\n\n"
        "<b>Победитель</b>: +10 очков (50/100)\n"
        "<b>Проигравший</b>: -5 очков (15/100)\n"
    )
    assert stats == "\n📊 Длительность: <b>2</b> раунда\nВкус дуэли.\n"
    assert stolen == (
        "\n💀 <b>И ВДОБАВОК У НЕГО УКРАЛИ ХУЙ.</b>\n\n"
        "Сегодня Проигравший больше не может драться.\n"
        "\n📊 Длительность: <b>2</b> раунда\nВкус дуэли.\n\n"
        "📖 <i>Факт.</i>"
    )
    assert get_text(
        "duel.finish.max_points_caption",
        winner_title="Победитель",
        max_daily_points=100,
    ) == "🏆 <b>Победитель</b> набрал 100 очков!"
    assert get_text("duel.finish.post_message.prefix") == (
        "На теле проигравшего обнаружили записку:"
    )


@pytest.mark.asyncio
async def test_finish_duel_no_steal_caps_points_and_completes_after_delete_error(
    monkeypatch,
    fixed_duel_database,
    fake_context,
):
    import database
    from handlers import duel

    winner_tg = make_user(101, "cap_winner")
    loser_tg = make_user(102, "floor_loser")
    database.get_or_create_duel_user(winner_tg, CHAT_ID)
    database.get_or_create_duel_user(loser_tg, CHAT_ID)
    set_duel_stats(
        fixed_duel_database,
        winner_tg.id,
        points=95,
        wins=4,
        losses=2,
        daily_wins=3,
        stolen_dicks_count=5,
        dick_stolen_count=1,
        dick_stolen_today=0,
        last_stolen_by="old_winner_thief",
    )
    set_duel_stats(
        fixed_duel_database,
        loser_tg.id,
        points=3,
        wins=2,
        losses=6,
        daily_wins=0,
        stolen_dicks_count=4,
        dick_stolen_count=7,
        dick_stolen_today=0,
        last_stolen_by="old_loser_thief",
    )
    winner = database.get_duel_user_by_username("cap_winner", CHAT_ID)
    loser = database.get_duel_user_by_username("floor_loser", CHAT_ID)
    duel.ACTIVE_DUELS[CHAT_ID] = {
        "round": 4,
        "message_id": 701,
        "original_msg_id": 702,
    }

    steal_roll = Mock(
        side_effect=(0.99, duel.BERSERK_CHANCE, duel.DUEL_POST_MESSAGE_CHANCE)
    )
    choices = []

    def choose(values):
        choices.append(values)
        return values[0]

    monkeypatch.setattr(duel.random, "random", steal_roll)
    monkeypatch.setattr(duel.random, "choice", choose)
    fake_context.bot.delete_message = AsyncMock(
        side_effect=RuntimeError("old duel message is already gone")
    )
    fake_context.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=703)
    )
    fake_context.bot.send_animation = AsyncMock()

    await duel._finish_duel(
        fake_context,
        CHAT_ID,
        winner,
        loser,
        custom_text="Детерминированный финал.\n",
    )

    refreshed_winner = database.get_duel_user_by_username("cap_winner", CHAT_ID)
    refreshed_loser = database.get_duel_user_by_username("floor_loser", CHAT_ID)
    assert (
        refreshed_winner["points"],
        refreshed_winner["wins"],
        refreshed_winner["losses"],
        refreshed_winner["daily_wins"],
        refreshed_winner["stolen_dicks_count"],
        refreshed_winner["dick_stolen_count"],
        refreshed_winner["dick_stolen_today"],
        refreshed_winner["last_stolen_by"],
    ) == (100, 5, 2, 4, 5, 1, False, "old_winner_thief")
    assert (
        refreshed_loser["points"],
        refreshed_loser["wins"],
        refreshed_loser["losses"],
        refreshed_loser["daily_wins"],
        refreshed_loser["stolen_dicks_count"],
        refreshed_loser["dick_stolen_count"],
        refreshed_loser["dick_stolen_today"],
        refreshed_loser["last_stolen_by"],
    ) == (0, 2, 7, 0, 4, 7, False, "old_loser_thief")
    assert CHAT_ID not in duel.ACTIVE_DUELS
    assert steal_roll.call_count == 3
    assert len(choices) == 1

    fake_context.bot.delete_message.assert_awaited_once_with(
        chat_id=CHAT_ID,
        message_id=701,
    )
    fake_context.bot.send_message.assert_awaited_once()
    final_call = fake_context.bot.send_message.await_args
    assert final_call.kwargs["chat_id"] == CHAT_ID
    assert final_call.kwargs["parse_mode"] == "HTML"
    assert "Детерминированный финал." in final_call.kwargs["text"]
    assert "🗡️ <b>Результаты дуэли:</b>" in final_call.kwargs["text"]
    assert "(100/100)" in final_call.kwargs["text"]
    assert "(0/100)" in final_call.kwargs["text"]
    assert "БЕРСЕРК" not in final_call.kwargs["text"]
    assert "На теле проигравшего обнаружили записку:" not in final_call.kwargs["text"]
    assert duel.DUEL_POST_MESSAGES[0] not in final_call.kwargs["text"]
    assert fake_context.job_queue.calls == [
        (
            duel.delete_messages_job,
            duel.AUTO_DELETE_DELAY,
            {"data": {"chat_id": CHAT_ID, "message_ids": [703, 702]}},
        )
    ]
    fake_context.bot.send_animation.assert_awaited_once_with(
        chat_id=CHAT_ID,
        animation=duel.WINNER_100_PTS_GIF,
        caption="🏆 <b>cap_winner</b> набрал 100 очков!",
        parse_mode="HTML",
    )


@pytest.mark.asyncio
async def test_finish_duel_steal_keeps_final_message_and_schedules_original_only(
    monkeypatch,
    fixed_duel_database,
    fake_context,
):
    import database
    from handlers import duel

    winner_tg = make_user(201, "steal_winner")
    loser_tg = make_user(202, "steal_loser")
    database.get_or_create_duel_user(winner_tg, CHAT_ID)
    database.get_or_create_duel_user(loser_tg, CHAT_ID)
    set_duel_stats(
        fixed_duel_database,
        winner_tg.id,
        points=40,
        wins=2,
        losses=1,
        daily_wins=1,
        stolen_dicks_count=3,
        dick_stolen_count=0,
        dick_stolen_today=0,
        last_stolen_by=None,
    )
    set_duel_stats(
        fixed_duel_database,
        loser_tg.id,
        points=20,
        wins=5,
        losses=4,
        daily_wins=2,
        stolen_dicks_count=1,
        dick_stolen_count=6,
        dick_stolen_today=0,
        last_stolen_by=None,
    )
    winner = database.get_duel_user_by_username("steal_winner", CHAT_ID)
    loser = database.get_duel_user_by_username("steal_loser", CHAT_ID)
    duel.ACTIVE_DUELS[CHAT_ID] = {
        "round": 2,
        "message_id": 801,
        "original_msg_id": 802,
    }

    steal_roll = Mock(
        side_effect=(0.0, duel.BERSERK_CHANCE, duel.DUEL_POST_MESSAGE_CHANCE)
    )
    choices = []

    def choose(values):
        choices.append(values)
        return values[0]

    monkeypatch.setattr(duel.random, "random", steal_roll)
    monkeypatch.setattr(duel.random, "choice", choose)
    fake_context.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=803)
    )
    fake_context.bot.send_animation = AsyncMock()

    await duel._finish_duel(
        fake_context,
        CHAT_ID,
        winner,
        loser,
        custom_text="Результат с кражей.\n",
    )

    refreshed_winner = database.get_duel_user_by_username("steal_winner", CHAT_ID)
    refreshed_loser = database.get_duel_user_by_username("steal_loser", CHAT_ID)
    assert (
        refreshed_winner["points"],
        refreshed_winner["wins"],
        refreshed_winner["losses"],
        refreshed_winner["daily_wins"],
        refreshed_winner["stolen_dicks_count"],
    ) == (50, 3, 1, 2, 4)
    assert (
        refreshed_loser["points"],
        refreshed_loser["wins"],
        refreshed_loser["losses"],
        refreshed_loser["daily_wins"],
        refreshed_loser["dick_stolen_count"],
        refreshed_loser["dick_stolen_today"],
        refreshed_loser["last_stolen_by"],
    ) == (15, 5, 5, 2, 7, True, "steal_winner")
    assert CHAT_ID not in duel.ACTIVE_DUELS
    assert steal_roll.call_count == 3
    assert len(choices) == 2
    assert choices[1] is duel.DWARFS_FACTS

    fake_context.bot.delete_message.assert_awaited_once_with(
        chat_id=CHAT_ID,
        message_id=801,
    )
    fake_context.bot.send_message.assert_awaited_once()
    final_call = fake_context.bot.send_message.await_args
    assert final_call.kwargs["parse_mode"] == "HTML"
    assert "И ВДОБАВОК У НЕГО УКРАЛИ ХУЙ" in final_call.kwargs["text"]
    assert duel.DWARFS_FACTS[0] in final_call.kwargs["text"]
    assert fake_context.job_queue.calls == [
        (
            duel.delete_messages_job,
            duel.AUTO_DELETE_DELAY,
            {"data": {"chat_id": CHAT_ID, "message_ids": [802]}},
        )
    ]
    assert 803 not in fake_context.job_queue.calls[0][2]["data"]["message_ids"]
    fake_context.bot.send_animation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("berserker_role", ["winner", "loser"])
async def test_finish_duel_berserk_can_select_either_participant_without_changing_outcome(
    monkeypatch,
    fixed_duel_database,
    fake_context,
    berserker_role,
):
    import database
    from handlers import duel, duel_text

    winner_tg = make_user(401, "berserk_winner")
    loser_tg = make_user(402, "berserk_loser")
    database.get_or_create_duel_user(winner_tg, CHAT_ID)
    database.get_or_create_duel_user(loser_tg, CHAT_ID)
    set_duel_stats(
        fixed_duel_database,
        winner_tg.id,
        points=40,
        wins=2,
        losses=1,
        daily_wins=1,
        stolen_dicks_count=3,
        dick_stolen_count=2,
        dick_stolen_today=0,
        last_stolen_by=None,
    )
    set_duel_stats(
        fixed_duel_database,
        loser_tg.id,
        points=20,
        wins=5,
        losses=4,
        daily_wins=2,
        stolen_dicks_count=1,
        dick_stolen_count=6,
        dick_stolen_today=0,
        last_stolen_by=None,
    )
    winner = database.get_duel_user_by_username("berserk_winner", CHAT_ID)
    loser = database.get_duel_user_by_username("berserk_loser", CHAT_ID)
    rolls = Mock(
        side_effect=(
            0.99,
            duel.BERSERK_CHANCE - 0.000001,
            duel.DUEL_POST_MESSAGE_CHANCE,
        )
    )
    choice_inputs = []

    def choose(values):
        choice_inputs.append(values)
        if isinstance(values, tuple):
            return values[0] if berserker_role == "winner" else values[1]
        return values[0]

    monkeypatch.setattr(duel.random, "random", rolls)
    monkeypatch.setattr(duel.random, "choice", choose)
    fake_context.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=1001)
    )
    fake_context.bot.send_animation = AsyncMock()

    await duel._finish_duel(
        fake_context,
        CHAT_ID,
        winner,
        loser,
        custom_text="Обычный исход уже определён.\n",
    )

    refreshed_winner = database.get_duel_user_by_username("berserk_winner", CHAT_ID)
    refreshed_loser = database.get_duel_user_by_username("berserk_loser", CHAT_ID)
    assert (
        refreshed_winner["points"],
        refreshed_winner["wins"],
        refreshed_winner["losses"],
        refreshed_winner["daily_wins"],
    ) == (50, 3, 1, 2)
    assert (
        refreshed_loser["points"],
        refreshed_loser["wins"],
        refreshed_loser["losses"],
        refreshed_loser["daily_wins"],
    ) == (15, 5, 5, 2)

    if berserker_role == "winner":
        assert refreshed_winner["stolen_dicks_count"] == 4
        assert refreshed_loser["dick_stolen_count"] == 7
        assert refreshed_loser["dick_stolen_today"] is True
        assert refreshed_loser["last_stolen_by"] == "berserk_winner"
    else:
        assert refreshed_loser["stolen_dicks_count"] == 2
        assert refreshed_winner["dick_stolen_count"] == 3
        assert refreshed_winner["dick_stolen_today"] is True
        assert refreshed_winner["last_stolen_by"] == "berserk_loser"

    assert rolls.call_count == 3
    assert choice_inputs[1] == (winner, loser)
    assert choice_inputs[2] is duel_text.BERSERK_TRIGGERS
    assert choice_inputs[3] is duel_text.BERSERK_RESULTS
    final_call = fake_context.bot.send_message.await_args
    assert final_call.kwargs["parse_mode"] == "HTML"
    assert "Обычный исход уже определён." in final_call.kwargs["text"]
    assert " <b>БЕРСЕРК</b>" in final_call.kwargs["text"]
    expected_berserker = "berserk_winner" if berserker_role == "winner" else "berserk_loser"
    assert f"<b>{expected_berserker}</b>" in final_call.kwargs["text"]


@pytest.mark.asyncio
async def test_finish_duel_berserk_already_stolen_has_no_reroll_or_extra_stat(
    monkeypatch,
    fixed_duel_database,
    fake_context,
):
    import database
    from handlers import duel, duel_text

    winner_tg = make_user(501, "fallback_winner")
    loser_tg = make_user(502, "fallback_loser")
    database.get_or_create_duel_user(winner_tg, CHAT_ID)
    database.get_or_create_duel_user(loser_tg, CHAT_ID)
    set_duel_stats(
        fixed_duel_database,
        winner_tg.id,
        points=40,
        wins=2,
        losses=1,
        daily_wins=1,
        stolen_dicks_count=3,
        dick_stolen_count=0,
        dick_stolen_today=0,
        last_stolen_by=None,
    )
    set_duel_stats(
        fixed_duel_database,
        loser_tg.id,
        points=20,
        wins=5,
        losses=4,
        daily_wins=2,
        stolen_dicks_count=1,
        dick_stolen_count=6,
        dick_stolen_today=0,
        last_stolen_by=None,
    )
    winner = database.get_duel_user_by_username("fallback_winner", CHAT_ID)
    loser = database.get_duel_user_by_username("fallback_loser", CHAT_ID)
    rolls = Mock(side_effect=(0.0, 0.0, duel.DUEL_POST_MESSAGE_CHANCE))
    choice_inputs = []

    def choose(values):
        choice_inputs.append(values)
        return values[0]

    monkeypatch.setattr(duel.random, "random", rolls)
    monkeypatch.setattr(duel.random, "choice", choose)
    fake_context.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=1002)
    )
    fake_context.bot.send_animation = AsyncMock()

    await duel._finish_duel(
        fake_context,
        CHAT_ID,
        winner,
        loser,
        custom_text="Обычная кража перед берсерком.\n",
    )

    refreshed_winner = database.get_duel_user_by_username("fallback_winner", CHAT_ID)
    refreshed_loser = database.get_duel_user_by_username("fallback_loser", CHAT_ID)
    assert refreshed_winner["stolen_dicks_count"] == 4
    assert refreshed_loser["dick_stolen_count"] == 7
    assert refreshed_loser["dick_stolen_today"] is True
    assert refreshed_loser["last_stolen_by"] == "fallback_winner"
    assert sum(values == (winner, loser) for values in choice_inputs) == 1
    assert choice_inputs[-1] is duel_text.BERSERK_ALREADY_STOLEN
    assert "отсутствие объекта нападения" not in fake_context.bot.send_message.await_args.kwargs["text"]
    assert "до него здесь уже кто-то побывал" in fake_context.bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_finish_duel_post_message_is_escaped_and_appended_last(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    winner = {"user_id": 551, "username": "post_winner", "points": 20}
    loser = {
        "user_id": 552,
        "username": "post_loser",
        "points": 20,
        "daily_wins": 0,
    }
    catalog = ("raw <tag> & message",)
    choices = []

    def choose(values):
        choices.append(values)
        return values[0]

    monkeypatch.setattr(duel, "DUEL_POST_MESSAGES", catalog)
    monkeypatch.setattr(
        duel.random,
        "random",
        Mock(side_effect=(0.99, duel.BERSERK_CHANCE, 0.099999)),
    )
    monkeypatch.setattr(duel.random, "choice", choose)
    monkeypatch.setattr(duel, "apply_duel_result_plan", lambda *_: (30, 15))
    fake_context.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=1004)
    )

    await duel._finish_duel(
        fake_context,
        CHAT_ID,
        winner,
        loser,
        custom_text="Обычный финал.\n",
    )

    output = fake_context.bot.send_message.await_args.kwargs["text"]
    assert choices.count(catalog) == 1
    assert catalog[0] not in output
    assert output.endswith(
        "\n\nНа теле проигравшего обнаружили записку:\n"
        "raw &lt;tag&gt; &amp; message"
    )


@pytest.mark.asyncio
async def test_finish_duel_rng_orders_berserk_after_all_existing_finish_rng(
    monkeypatch,
    fake_context,
):
    from handlers import duel, duel_text

    winner = {"user_id": 601, "username": "order_winner", "points": 20}
    loser = {
        "user_id": 602,
        "username": "order_loser",
        "points": 20,
        "daily_wins": 0,
    }
    events = []
    rolls = iter((0.0, 0.0, 0.0))
    roll_names = iter(("regular_steal_roll", "berserk_roll", "post_message_roll"))

    def random_roll():
        value = next(rolls)
        events.append(next(roll_names))
        return value

    def choose(values):
        if values is duel.DWARFS_FACTS:
            events.append("ordinary_fact_choice")
        elif values is duel.DUEL_POST_MESSAGES:
            events.append("post_message_choice")
        elif isinstance(values, tuple):
            events.append("berserker_choice")
        elif values is duel_text.BERSERK_TRIGGERS:
            events.append("berserk_trigger_choice")
        elif values is duel_text.BERSERK_RESULTS:
            events.append("berserk_result_choice")
        else:
            events.append("ordinary_round_flavor_choice")
        return values[0]

    def apply_result(*_args):
        events.append("ordinary_result_applied")
        return 30, 15

    def apply_berserk(*_args):
        events.append("berserk_applied")
        return True

    monkeypatch.setattr(duel.random, "random", random_roll)
    monkeypatch.setattr(duel.random, "choice", choose)
    monkeypatch.setattr(duel, "apply_duel_result_plan", apply_result)
    monkeypatch.setattr(duel, "apply_duel_berserk", apply_berserk)
    fake_context.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=1003)
    )

    await duel._finish_duel(
        fake_context,
        CHAT_ID,
        winner,
        loser,
        custom_text="Проверка RNG.\n",
    )

    assert events == [
        "regular_steal_roll",
        "ordinary_result_applied",
        "ordinary_round_flavor_choice",
        "ordinary_fact_choice",
        "berserk_roll",
        "berserker_choice",
        "berserk_applied",
        "berserk_trigger_choice",
        "berserk_result_choice",
        "post_message_roll",
        "post_message_choice",
    ]
    assert fake_context.bot.send_message.await_args.kwargs["text"].endswith(
        "\n\nНа теле проигравшего обнаружили записку:\n"
        f"{duel.DUEL_POST_MESSAGES[0]}"
    )


@pytest.mark.asyncio
async def test_zero_point_loser_has_guaranteed_steal_without_decision_rng(
    monkeypatch, fixed_duel_database, fake_context,
):
    import database
    from handlers import duel

    winner_tg = make_user(701, "zero_winner")
    loser_tg = make_user(702, "zero_loser")
    database.get_or_create_duel_user(winner_tg, CHAT_ID)
    database.get_or_create_duel_user(loser_tg, CHAT_ID)
    set_duel_stats(
        fixed_duel_database, winner_tg.id,
        points=0, wins=2, losses=1, daily_wins=0,
        stolen_dicks_count=3, dick_stolen_count=0,
        dick_stolen_today=0, last_stolen_by=None,
    )
    set_duel_stats(
        fixed_duel_database, loser_tg.id,
        points=0, wins=1, losses=4, daily_wins=0,
        stolen_dicks_count=0, dick_stolen_count=5,
        dick_stolen_today=0, last_stolen_by=None,
    )
    database.add_duel_inventory_item(CHAT_ID, loser_tg.id, "vevangel_wing")
    winner = database.get_duel_user_by_username("zero_winner", CHAT_ID)
    loser = database.get_duel_user_by_username("zero_loser", CHAT_ID)
    events = []
    rolls = iter((
        ("item_steal_roll", duel.DUEL_ITEM_STEAL_CHANCE - 0.001),
        ("berserk_roll", duel.BERSERK_CHANCE),
        ("post_message_roll", duel.DUEL_POST_MESSAGE_CHANCE - 0.001),
    ))
    post_messages = ("post message",)

    def random_roll():
        name, value = next(rolls)
        events.append(name)
        return value

    def choose(values):
        if values and isinstance(values[0], dict):
            events.append("item_steal_choice")
        elif values is duel.DWARFS_FACTS:
            events.append("dwarf_fact_choice")
        elif values is post_messages:
            events.append("post_message_choice")
        else:
            events.append("round_flavor_choice")
        return values[0]

    monkeypatch.setattr(duel, "DUEL_POST_MESSAGES", post_messages)
    monkeypatch.setattr(duel.random, "random", random_roll)
    monkeypatch.setattr(duel.random, "choice", choose)
    fake_context.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=1701)
    )

    await duel._finish_duel(fake_context, CHAT_ID, winner, loser, "Final.\n")

    assert events == [
        "item_steal_roll",
        "item_steal_choice",
        "round_flavor_choice",
        "dwarf_fact_choice",
        "berserk_roll",
        "post_message_roll",
        "post_message_choice",
    ]
    refreshed_winner = database.get_duel_user_by_username("zero_winner", CHAT_ID)
    refreshed_loser = database.get_duel_user_by_username("zero_loser", CHAT_ID)
    assert (refreshed_winner["points"], refreshed_winner["wins"],
            refreshed_winner["daily_wins"], refreshed_winner["stolen_dicks_count"]) == (10, 3, 1, 4)
    assert (refreshed_loser["points"], refreshed_loser["losses"],
            refreshed_loser["dick_stolen_count"], refreshed_loser["dick_stolen_today"],
            refreshed_loser["last_stolen_by"]) == (0, 5, 6, True, "zero_winner")
    assert database.get_monthly_chat_stats(CHAT_ID, database.moscow_month_key()) == {
        "dicks_stolen": 1, "duels": 1, "bosses_killed": 0,
    }
    assert [item["item_id"] for item in database.get_duel_inventory(CHAT_ID, winner_tg.id)] == ["vevangel_wing"]
    assert database.get_duel_inventory(CHAT_ID, loser_tg.id) == []
    output = fake_context.bot.send_message.await_args.kwargs["text"]
    assert get_text("duel.finish.item_stolen", item_name=duel.get_duel_item_name("vevangel_wing")) in output
    assert duel.DWARFS_FACTS[0] in output
    assert output.endswith("post message")


@pytest.mark.asyncio
async def test_finish_duel_transaction_error_clears_state_and_schedules_error(
    monkeypatch,
    fixed_duel_database,
    fake_context,
):
    import database
    from handlers import duel

    winner_tg = make_user(301, "error_winner")
    loser_tg = make_user(302, "error_loser")
    winner = database.get_or_create_duel_user(winner_tg, CHAT_ID)
    loser = database.get_or_create_duel_user(loser_tg, CHAT_ID)
    duel.ACTIVE_DUELS[CHAT_ID] = {
        "round": 3,
        "message_id": 901,
        "original_msg_id": 902,
    }

    steal_roll = Mock(return_value=0.99)
    presentation_choice = Mock(side_effect=AssertionError("presentation continued"))
    monkeypatch.setattr(duel.random, "random", steal_roll)
    monkeypatch.setattr(duel.random, "choice", presentation_choice)
    monkeypatch.setattr(
        duel,
        "apply_duel_result_plan",
        Mock(side_effect=RuntimeError("database failed")),
    )
    fake_context.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=903)
    )
    fake_context.bot.send_animation = AsyncMock()

    await duel._finish_duel(
        fake_context,
        CHAT_ID,
        winner,
        loser,
        custom_text="Этот финал не должен отправиться.\n",
    )

    assert CHAT_ID not in duel.ACTIVE_DUELS
    steal_roll.assert_called_once_with()
    presentation_choice.assert_not_called()
    fake_context.bot.send_message.assert_awaited_once_with(
        CHAT_ID,
        "⚠️ Ошибка проведения дуэли. Попробуйте снова.",
    )
    fake_context.bot.delete_message.assert_not_awaited()
    fake_context.bot.send_animation.assert_not_awaited()
    assert fake_context.job_queue.calls == [
        (
            duel.delete_messages_job,
            duel.AUTO_DELETE_DELAY,
            {"data": {"chat_id": CHAT_ID, "message_ids": [903]}},
        )
    ]
