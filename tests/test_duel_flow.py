import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


CHAT_ID = -4242


class FakeTask:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


def make_user(user_id, username):
    return SimpleNamespace(
        id=user_id,
        username=username,
        first_name=username.title(),
        last_name=None,
        is_bot=False,
    )


def install_fake_tasks(monkeypatch, duel):
    tasks = []

    def create_task(coroutine):
        coroutine.close()
        task = FakeTask()
        tasks.append(task)
        return task

    monkeypatch.setattr(duel.asyncio, "create_task", create_task)
    return tasks


def callback_update(data, user, message=None):
    query = SimpleNamespace(
        data=data,
        from_user=user,
        answer=AsyncMock(),
        message=message,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=CHAT_ID),
    )
    return update, query


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


@pytest.mark.asyncio
async def test_selected_duel_runs_callbacks_through_round_change_to_result(
    monkeypatch,
    fixed_duel_database,
    fake_context,
):
    import database
    from handlers import duel

    attacker_tg = make_user(1, "attacker")
    defender_tg = make_user(2, "defender")
    database.get_or_create_duel_user(attacker_tg, CHAT_ID)
    database.get_or_create_duel_user(defender_tg, CHAT_ID)

    tasks = install_fake_tasks(monkeypatch, duel)
    monkeypatch.setattr(duel.random, "choice", lambda values: values[0])
    random_values = iter((0.5, 0.5, 0.5, 0.5, 0.0, 1.0))
    monkeypatch.setattr(duel.random, "random", lambda: next(random_values))

    selection_message = SimpleNamespace(delete=AsyncMock())
    update, query = callback_update(
        "start_duel_defender",
        attacker_tg,
        selection_message,
    )
    await duel.duel_select_callback(update, fake_context)

    query.answer.assert_awaited_once_with()
    selection_message.delete.assert_awaited_once_with()
    state = duel.ACTIVE_DUELS[CHAT_ID]
    assert {
        "phase": state["phase"],
        "attack_zone": state["attack_zone"],
        "round": state["round"],
        "turn_id": state["turn_id"],
        "message_id": state["message_id"],
        "original_msg_id": state["original_msg_id"],
    } == {
        "phase": "attack",
        "attack_zone": None,
        "round": 1,
        "turn_id": 1,
        "message_id": 101,
        "original_msg_id": None,
    }
    assert state["attacker_tg"].id == attacker_tg.id
    assert state["defender_tg"].id == defender_tg.id
    assert state["turn_task"] is tasks[0]

    strike_update, strike_query = callback_update(
        "duel_strike_head_1",
        attacker_tg,
    )
    await duel.duel_strike_callback(strike_update, fake_context)

    strike_query.answer.assert_awaited_once_with()
    assert tasks[0].cancelled is True
    assert state["phase"] == "block"
    assert state["attack_zone"] == "head"
    assert state["round"] == 1
    assert state["turn_id"] == 2
    assert state["turn_task"] is tasks[1]

    first_block_update, first_block_query = callback_update(
        "duel_block_head_2",
        defender_tg,
    )
    await duel.duel_strike_callback(first_block_update, fake_context)

    first_block_query.answer.assert_awaited_once_with()
    assert tasks[1].cancelled is True
    assert state["phase"] == "attack"
    assert state["attack_zone"] is None
    assert state["round"] == 2
    assert state["turn_id"] == 3
    assert state["attacker_tg"].id == defender_tg.id
    assert state["defender_tg"].id == attacker_tg.id
    assert state["turn_task"] is tasks[2]

    stale_update, stale_query = callback_update(
        "duel_strike_body_1",
        defender_tg,
    )
    await duel.duel_strike_callback(stale_update, fake_context)

    stale_query.answer.assert_awaited_once()
    assert stale_query.answer.await_args.kwargs == {"show_alert": True}
    assert state["phase"] == "attack"
    assert state["attack_zone"] is None
    assert state["round"] == 2
    assert state["turn_id"] == 3

    second_strike_update, second_strike_query = callback_update(
        "duel_strike_body_3",
        defender_tg,
    )
    await duel.duel_strike_callback(second_strike_update, fake_context)

    second_strike_query.answer.assert_awaited_once_with()
    assert tasks[2].cancelled is True
    assert state["phase"] == "block"
    assert state["attack_zone"] == "body"
    assert state["round"] == 2
    assert state["turn_id"] == 4
    assert state["turn_task"] is tasks[3]

    final_block_update, final_block_query = callback_update(
        "duel_block_head_4",
        attacker_tg,
    )
    await duel.duel_strike_callback(final_block_update, fake_context)

    final_block_query.answer.assert_awaited_once_with()
    assert tasks[3].cancelled is True
    assert CHAT_ID not in duel.ACTIVE_DUELS

    winner = database.get_duel_user_by_username("defender", CHAT_ID)
    loser = database.get_duel_user_by_username("attacker", CHAT_ID)
    assert {
        "points": winner["points"],
        "wins": winner["wins"],
        "losses": winner["losses"],
        "daily_wins": winner["daily_wins"],
        "stolen_dicks_count": winner["stolen_dicks_count"],
    } == {
        "points": 30,
        "wins": 1,
        "losses": 0,
        "daily_wins": 1,
        "stolen_dicks_count": 1,
    }
    assert {
        "points": loser["points"],
        "wins": loser["wins"],
        "losses": loser["losses"],
        "dick_stolen_count": loser["dick_stolen_count"],
        "dick_stolen_today": loser["dick_stolen_today"],
        "last_stolen_by": loser["last_stolen_by"],
    } == {
        "points": 15,
        "wins": 0,
        "losses": 1,
        "dick_stolen_count": 1,
        "dick_stolen_today": True,
        "last_stolen_by": "defender",
    }


@pytest.mark.asyncio
async def test_suicide_skips_miss_roll_and_awards_defender(
    monkeypatch,
    fixed_duel_database,
    fake_context,
):
    import database
    from handlers import duel

    attacker_tg = make_user(31, "suicidal_attacker")
    defender_tg = make_user(32, "suicide_winner")
    attacker = database.get_or_create_duel_user(attacker_tg, CHAT_ID)
    defender = database.get_or_create_duel_user(defender_tg, CHAT_ID)
    tasks = install_fake_tasks(monkeypatch, duel)
    monkeypatch.setattr(duel.random, "choice", lambda values: values[0])
    pending_rolls = iter((0.0, 1.0, 1.0))
    observed_rolls = []

    def random_roll():
        value = next(pending_rolls)
        observed_rolls.append(value)
        return value

    real_apply_result_plan = duel.apply_duel_result_plan
    persistence_calls = []

    def apply_result_plan(chat_id, result_plan):
        assert observed_rolls == [0.0, 1.0]
        persistence_calls.append((chat_id, result_plan))
        return real_apply_result_plan(chat_id, result_plan)

    monkeypatch.setattr(duel.random, "random", random_roll)
    monkeypatch.setattr(duel, "apply_duel_result_plan", apply_result_plan)

    await duel._start_interactive_fight(
        fake_context,
        CHAT_ID,
        attacker_tg,
        defender_tg,
        attacker,
        defender,
    )
    strike_update, _ = callback_update("duel_strike_head_1", attacker_tg)
    await duel.duel_strike_callback(strike_update, fake_context)
    block_update, _ = callback_update("duel_block_body_2", defender_tg)
    await duel.duel_strike_callback(block_update, fake_context)

    assert observed_rolls == [0.0, 1.0, 1.0]
    assert len(persistence_calls) == 1
    assert persistence_calls[0][0] == CHAT_ID
    result_plan = persistence_calls[0][1]
    assert result_plan["winner"]["user_id"] == defender_tg.id
    assert result_plan["loser"]["user_id"] == attacker_tg.id
    assert result_plan["is_dick_stolen"] is False
    assert tasks[1].cancelled is True
    assert CHAT_ID not in duel.ACTIVE_DUELS

    refreshed_attacker = database.get_duel_user_by_username(
        "suicidal_attacker",
        CHAT_ID,
    )
    refreshed_defender = database.get_duel_user_by_username(
        "suicide_winner",
        CHAT_ID,
    )
    assert (
        refreshed_attacker["points"],
        refreshed_attacker["wins"],
        refreshed_attacker["losses"],
        refreshed_attacker["dick_stolen_today"],
        refreshed_attacker["dick_stolen_count"],
        refreshed_attacker["last_stolen_by"],
    ) == (15, 0, 1, False, 0, None)
    assert (
        refreshed_defender["points"],
        refreshed_defender["wins"],
        refreshed_defender["losses"],
        refreshed_defender["daily_wins"],
        refreshed_defender["stolen_dicks_count"],
    ) == (30, 1, 0, 1, 0)


@pytest.mark.asyncio
async def test_miss_advances_round_without_changing_combat_stats(
    monkeypatch,
    fixed_duel_database,
    fake_context,
):
    import database
    from handlers import duel

    attacker_tg = make_user(41, "missing_attacker")
    defender_tg = make_user(42, "miss_defender")
    attacker = database.get_or_create_duel_user(attacker_tg, CHAT_ID)
    defender = database.get_or_create_duel_user(defender_tg, CHAT_ID)
    tasks = install_fake_tasks(monkeypatch, duel)
    monkeypatch.setattr(duel.random, "choice", lambda values: values[0])
    pending_rolls = iter((0.5, 0.0))
    observed_rolls = []

    def random_roll():
        value = next(pending_rolls)
        observed_rolls.append(value)
        return value

    monkeypatch.setattr(duel.random, "random", random_roll)

    await duel._start_interactive_fight(
        fake_context,
        CHAT_ID,
        attacker_tg,
        defender_tg,
        attacker,
        defender,
    )
    original_state = duel.ACTIVE_DUELS[CHAT_ID]
    strike_update, _ = callback_update("duel_strike_head_1", attacker_tg)
    await duel.duel_strike_callback(strike_update, fake_context)
    block_update, _ = callback_update("duel_block_body_2", defender_tg)
    await duel.duel_strike_callback(block_update, fake_context)

    state = duel.ACTIVE_DUELS[CHAT_ID]
    assert observed_rolls == [0.5, 0.0]
    assert state is original_state
    assert state["attacker_tg"] is defender_tg
    assert state["defender_tg"] is attacker_tg
    assert state["attacker_data"] is defender
    assert state["defender_data"] is attacker
    assert state["phase"] == "attack"
    assert state["attack_zone"] is None
    assert state["round"] == 2
    assert state["turn_id"] == 3
    assert len(tasks) == 3
    assert tasks[1].cancelled is True
    assert state["turn_task"] is tasks[2]
    assert tasks[2].cancelled is False

    refreshed_attacker = database.get_duel_user_by_username(
        "missing_attacker",
        CHAT_ID,
    )
    refreshed_defender = database.get_duel_user_by_username(
        "miss_defender",
        CHAT_ID,
    )
    for participant in (refreshed_attacker, refreshed_defender):
        assert (
            participant["points"],
            participant["wins"],
            participant["losses"],
            participant["daily_wins"],
            participant["dick_stolen_today"],
            participant["stolen_dicks_count"],
            participant["dick_stolen_count"],
            participant["last_stolen_by"],
        ) == (20, 0, 0, 0, False, 0, 0, None)


@pytest.mark.asyncio
async def test_attack_timeout_moves_to_block_and_stale_timer_is_ignored(
    monkeypatch,
    fixed_duel_database,
    fake_context,
):
    import database
    from handlers import duel

    attacker_tg = make_user(11, "slow_attacker")
    defender_tg = make_user(12, "waiting_defender")
    attacker = database.get_or_create_duel_user(attacker_tg, CHAT_ID)
    defender = database.get_or_create_duel_user(defender_tg, CHAT_ID)
    tasks = install_fake_tasks(monkeypatch, duel)
    sleep = AsyncMock()
    monkeypatch.setattr(duel.asyncio, "sleep", sleep)
    choices = []

    def choose(values):
        choices.append(values)
        return "dick"

    monkeypatch.setattr(duel.random, "choice", choose)

    await duel._start_interactive_fight(
        fake_context,
        CHAT_ID,
        attacker_tg,
        defender_tg,
        attacker,
        defender,
        original_msg_id=88,
    )
    await duel._auto_move_timer(fake_context, CHAT_ID, 1, "attack", 1)

    state = duel.ACTIVE_DUELS[CHAT_ID]
    assert sleep.await_count == 1
    assert len(choices) == 1
    assert state["phase"] == "block"
    assert state["attack_zone"] == "dick"
    assert state["round"] == 1
    assert state["turn_id"] == 2
    assert state["turn_task"] is tasks[1]

    await duel._auto_move_timer(fake_context, CHAT_ID, 1, "attack", 1)

    assert sleep.await_count == 2
    assert len(choices) == 1
    assert state["phase"] == "block"
    assert state["attack_zone"] == "dick"
    assert state["turn_id"] == 2


@pytest.mark.parametrize(
    ("blocked_user", "field", "value"),
    [
        ("initiator", "dick_stolen_today", 1),
        ("initiator", "points", 0),
        ("opponent", "dick_stolen_today", 1),
        ("opponent", "points", 0),
    ],
)
@pytest.mark.asyncio
async def test_duel_command_rejects_ineligible_participant(
    monkeypatch,
    fixed_duel_database,
    fake_context,
    blocked_user,
    field,
    value,
):
    import database
    from handlers import duel

    initiator_tg = make_user(21, "initiator")
    opponent_tg = make_user(22, "opponent")
    database.get_or_create_duel_user(initiator_tg, CHAT_ID)
    database.get_or_create_duel_user(opponent_tg, CHAT_ID)
    blocked_id = initiator_tg.id if blocked_user == "initiator" else opponent_tg.id
    with sqlite3.connect(fixed_duel_database) as connection:
        connection.execute(
            f"UPDATE duel_users SET {field} = ? WHERE user_id = ? AND chat_id = ?",
            (value, blocked_id, CHAT_ID),
        )

    start_fight = AsyncMock()
    monkeypatch.setattr(duel, "_start_interactive_fight", start_fight)
    message = SimpleNamespace(
        from_user=initiator_tg,
        chat=SimpleNamespace(id=CHAT_ID),
        chat_id=CHAT_ID,
        message_id=303,
        text="/duel @opponent",
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=304)),
    )
    update = SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=CHAT_ID),
    )
    fake_context.args = ["@opponent"]

    await duel.duel_command(update, fake_context)

    start_fight.assert_not_awaited()
    assert CHAT_ID not in duel.ACTIVE_DUELS
    assert message.reply_text.await_count + fake_context.bot.send_message.await_count == 1


def admission_user(user_id, username, *, points=20, dick_stolen_today=False):
    return {
        "user_id": user_id,
        "username": username,
        "points": points,
        "dick_stolen_today": dick_stolen_today,
    }


def install_admission_spies(monkeypatch, duel):
    initiator_lookup = Mock()
    target_lookup = Mock()
    choice = Mock()
    start_fight = AsyncMock()
    monkeypatch.setattr(duel, "get_or_create_duel_user", initiator_lookup)
    monkeypatch.setattr(duel, "get_duel_user_by_username", target_lookup)
    monkeypatch.setattr(duel.random, "choice", choice)
    monkeypatch.setattr(duel, "_start_interactive_fight", start_fight)
    return initiator_lookup, target_lookup, choice, start_fight


def assert_admission_did_not_start(duel, choice, start_fight):
    choice.assert_not_called()
    start_fight.assert_not_awaited()
    assert CHAT_ID not in duel.ACTIVE_DUELS


def make_block_phase_duel(*, strike_zone="head", round_num=3, turn_id=8):
    attacker_tg = make_user(601, "block_attacker")
    defender_tg = make_user(602, "block_defender")
    attacker_data = {"user_id": attacker_tg.id, "username": "Block Attacker"}
    defender_data = {"user_id": defender_tg.id, "username": "Block Defender"}
    return {
        "attacker_tg": attacker_tg,
        "defender_tg": defender_tg,
        "attacker_data": attacker_data,
        "defender_data": defender_data,
        "phase": "block",
        "attack_zone": strike_zone,
        "round": round_num,
        "turn_id": turn_id,
        "message_id": 601,
        "turn_task": None,
        "lock": asyncio.Lock(),
        "original_msg_id": None,
    }


@pytest.mark.asyncio
async def test_process_block_choice_successful_block_preserves_rng_state_and_timer_contract(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    state = make_block_phase_duel()
    duel.ACTIVE_DUELS[CHAT_ID] = state
    original_state = state
    attacker_tg = state["attacker_tg"]
    defender_tg = state["defender_tg"]
    attacker_data = state["attacker_data"]
    defender_data = state["defender_data"]
    tasks = install_fake_tasks(monkeypatch, duel)
    finish_duel = AsyncMock()
    random_rolls = iter((0.5, 0.5))
    observed_rolls = []
    choice_inputs = []
    rng_events = []

    def random_roll():
        value = next(random_rolls)
        observed_rolls.append(value)
        rng_events.append(("random", value))
        return value

    def choose(values):
        choice_inputs.append(values)
        if values is duel.BLOCK_PHRASES:
            rng_events.append(("choice", "block"))
            return duel.BLOCK_PHRASES[0]
        assert values is duel.ATTACK_PHRASES
        rng_events.append(("choice", "attack"))
        return duel.ATTACK_PHRASES[0]

    monkeypatch.setattr(duel, "_finish_duel", finish_duel)
    monkeypatch.setattr(
        duel,
        "random",
        SimpleNamespace(
            random=random_roll,
            choice=choose,
        ),
    )

    await duel._process_block_choice(fake_context, CHAT_ID, "head")

    assert observed_rolls == [0.5, 0.5]
    assert rng_events == [
        ("random", 0.5),
        ("random", 0.5),
        ("choice", "block"),
        ("choice", "attack"),
    ]
    assert choice_inputs[0] is duel.BLOCK_PHRASES
    assert choice_inputs[1] is duel.ATTACK_PHRASES
    assert len(choice_inputs) == 2
    finish_duel.assert_not_awaited()
    assert duel.ACTIVE_DUELS[CHAT_ID] is original_state
    assert state["attacker_tg"] is defender_tg
    assert state["defender_tg"] is attacker_tg
    assert state["attacker_data"] is defender_data
    assert state["defender_data"] is attacker_data
    assert state["attack_zone"] is None
    assert state["phase"] == "attack"
    assert state["round"] == 4
    assert state["turn_id"] == 9
    assert state["turn_task"] is tasks[0]
    fake_context.bot.edit_message_text.assert_awaited_once()
    edit_kwargs = fake_context.bot.edit_message_text.await_args.kwargs
    assert edit_kwargs["chat_id"] == CHAT_ID
    assert edit_kwargs["message_id"] == 601
    assert edit_kwargs["parse_mode"] == "HTML"
    assert "БЛОК СРАБОТАЛ" in edit_kwargs["text"]
    assert edit_kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "duel_strike_head_9"
    fake_context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_block_choice_successful_hit_passes_current_snapshots_to_finish(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    state = make_block_phase_duel(strike_zone="head")
    duel.ACTIVE_DUELS[CHAT_ID] = state
    tasks = install_fake_tasks(monkeypatch, duel)
    finish_duel = AsyncMock()
    random_rolls = iter((0.5, 0.5))
    observed_rolls = []
    choice_inputs = []
    rng_events = []

    def random_roll():
        value = next(random_rolls)
        observed_rolls.append(value)
        rng_events.append(("random", value))
        return value

    def choose(values):
        choice_inputs.append(values)
        if values is duel.HIT_PHRASES:
            rng_events.append(("choice", "hit"))
            return duel.HIT_PHRASES[0]
        assert values is duel.ATTACK_PHRASES
        rng_events.append(("choice", "attack"))
        return duel.ATTACK_PHRASES[0]

    monkeypatch.setattr(duel, "_finish_duel", finish_duel)
    monkeypatch.setattr(
        duel,
        "random",
        SimpleNamespace(
            random=random_roll,
            choice=choose,
        ),
    )

    await duel._process_block_choice(fake_context, CHAT_ID, "body")

    assert observed_rolls == [0.5, 0.5]
    assert rng_events == [
        ("random", 0.5),
        ("random", 0.5),
        ("choice", "hit"),
        ("choice", "attack"),
    ]
    assert choice_inputs[0] is duel.HIT_PHRASES
    assert choice_inputs[1] is duel.ATTACK_PHRASES
    assert len(choice_inputs) == 2
    finish_duel.assert_awaited_once()
    finish_args = finish_duel.await_args
    assert finish_args.args == (fake_context, CHAT_ID)
    assert finish_args.kwargs["winner"] is state["attacker_data"]
    assert finish_args.kwargs["loser"] is state["defender_data"]
    assert finish_args.kwargs["strike_zone"] == "head"
    assert finish_args.kwargs["block_zone"] == "body"
    assert duel.HIT_PHRASES[0] in finish_args.kwargs["custom_text"]
    assert duel.ATTACK_PHRASES[0] in finish_args.kwargs["custom_text"]
    assert state["attacker_tg"].id == 601
    assert state["defender_tg"].id == 602
    assert state["round"] == 3
    assert state["turn_id"] == 8
    assert state["phase"] == "block"
    assert state["attack_zone"] == "head"
    assert tasks == []


@pytest.mark.asyncio
async def test_auto_move_timer_dispatches_matching_block_turn(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    state = make_block_phase_duel(round_num=7, turn_id=12)
    duel.ACTIVE_DUELS[CHAT_ID] = state
    sleep = AsyncMock()
    auto_choice = Mock(return_value="dick")
    process_block = AsyncMock()
    monkeypatch.setattr(duel.asyncio, "sleep", sleep)
    monkeypatch.setattr(duel.random, "choice", auto_choice)
    monkeypatch.setattr(duel, "_process_block_choice", process_block)

    await duel._auto_move_timer(fake_context, CHAT_ID, 7, "block", 12)

    sleep.assert_awaited_once_with(duel.MOVE_TIMEOUT)
    auto_choice.assert_called_once_with(["head", "body", "dick"])
    process_block.assert_awaited_once_with(fake_context, CHAT_ID, "dick")
    assert state["phase"] == "block"
    assert state["round"] == 7
    assert state["turn_id"] == 12
    fake_context.bot.send_message.assert_awaited_once()


@pytest.mark.parametrize(
    ("round_num", "phase", "turn_id"),
    [
        (6, "block", 12),
        (7, "block", 11),
        (7, "attack", 12),
    ],
    ids=("stale-round", "stale-turn-id", "wrong-phase"),
)
@pytest.mark.asyncio
async def test_auto_move_timer_ignores_stale_block_turn(
    monkeypatch,
    fake_context,
    round_num,
    phase,
    turn_id,
):
    from handlers import duel

    state = make_block_phase_duel(round_num=7, turn_id=12)
    duel.ACTIVE_DUELS[CHAT_ID] = state
    before = {
        key: value
        for key, value in state.items()
        if key != "lock"
    }
    sleep = AsyncMock()
    auto_choice = Mock()
    process_block = AsyncMock()
    monkeypatch.setattr(duel.asyncio, "sleep", sleep)
    monkeypatch.setattr(duel.random, "choice", auto_choice)
    monkeypatch.setattr(duel, "_process_block_choice", process_block)

    await duel._auto_move_timer(
        fake_context,
        CHAT_ID,
        round_num,
        phase,
        turn_id,
    )

    sleep.assert_awaited_once_with(duel.MOVE_TIMEOUT)
    auto_choice.assert_not_called()
    process_block.assert_not_awaited()
    assert {
        key: value
        for key, value in state.items()
        if key != "lock"
    } == before
    fake_context.bot.send_message.assert_not_awaited()
    assert fake_context.job_queue.calls == []


@pytest.mark.asyncio
async def test_process_duel_fight_active_duel_precedes_all_other_admission_checks(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    initiator = make_user(501, "initiator")
    existing_duel = {"phase": "attack", "marker": "unchanged"}
    duel.ACTIVE_DUELS[CHAT_ID] = existing_duel
    before = dict(existing_duel)
    initiator_lookup, target_lookup, choice, start_fight = install_admission_spies(
        monkeypatch, duel
    )

    await duel._process_duel_fight(
        fake_context,
        initiator,
        "initiator",
        CHAT_ID,
    )

    initiator_lookup.assert_not_called()
    target_lookup.assert_not_called()
    choice.assert_not_called()
    start_fight.assert_not_awaited()
    assert duel.ACTIVE_DUELS[CHAT_ID] is existing_duel
    assert existing_duel == before
    assert "уже идет дуэль" in fake_context.bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_process_duel_fight_username_self_target_precedes_eligibility_lookup(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    initiator = make_user(502, "CaseSensitiveName")
    initiator_lookup, target_lookup, choice, start_fight = install_admission_spies(
        monkeypatch, duel
    )

    await duel._process_duel_fight(
        fake_context,
        initiator,
        "casesensitivename",
        CHAT_ID,
    )

    initiator_lookup.assert_not_called()
    target_lookup.assert_not_called()
    assert_admission_did_not_start(duel, choice, start_fight)
    assert "самого себя" in fake_context.bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_process_duel_fight_initiator_no_dick_precedes_zero_points(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    initiator_tg = make_user(503, "blocked_initiator")
    initiator_lookup, target_lookup, choice, start_fight = install_admission_spies(
        monkeypatch, duel
    )
    initiator_lookup.return_value = admission_user(
        initiator_tg.id,
        initiator_tg.username,
        points=0,
        dick_stolen_today=True,
    )

    await duel._process_duel_fight(
        fake_context,
        initiator_tg,
        "opponent",
        CHAT_ID,
    )

    initiator_lookup.assert_called_once_with(initiator_tg, CHAT_ID)
    target_lookup.assert_not_called()
    assert_admission_did_not_start(duel, choice, start_fight)
    denial = fake_context.bot.send_message.await_args.args[1]
    assert "без хуя" in denial
    assert "0 очков" not in denial


@pytest.mark.asyncio
async def test_process_duel_fight_opponent_no_dick_precedes_zero_points(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    initiator_tg = make_user(504, "eligible_initiator")
    initiator_lookup, target_lookup, choice, start_fight = install_admission_spies(
        monkeypatch, duel
    )
    initiator_lookup.return_value = admission_user(
        initiator_tg.id,
        initiator_tg.username,
    )
    target_lookup.return_value = admission_user(
        505,
        "blocked_opponent",
        points=0,
        dick_stolen_today=True,
    )

    await duel._process_duel_fight(
        fake_context,
        initiator_tg,
        "blocked_opponent",
        CHAT_ID,
    )

    target_lookup.assert_called_once_with("blocked_opponent", CHAT_ID)
    assert_admission_did_not_start(duel, choice, start_fight)
    denial = fake_context.bot.send_message.await_args.args[1]
    assert "без хуя" in denial
    assert "0 очков" not in denial


@pytest.mark.asyncio
async def test_process_duel_fight_user_id_self_target_precedes_opponent_eligibility(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    initiator_tg = make_user(506, "initiator_username")
    initiator_lookup, target_lookup, choice, start_fight = install_admission_spies(
        monkeypatch, duel
    )
    initiator_lookup.return_value = admission_user(
        initiator_tg.id,
        initiator_tg.username,
    )
    target_lookup.return_value = admission_user(
        initiator_tg.id,
        "different_target_name",
        points=0,
        dick_stolen_today=True,
    )

    await duel._process_duel_fight(
        fake_context,
        initiator_tg,
        "different_target_name",
        CHAT_ID,
    )

    target_lookup.assert_called_once_with("different_target_name", CHAT_ID)
    assert_admission_did_not_start(duel, choice, start_fight)
    denial = fake_context.bot.send_message.await_args.args[1]
    assert "самого себя" in denial
    assert "без хуя" not in denial


@pytest.mark.asyncio
async def test_process_duel_fight_unknown_target_denies_before_rng_or_start(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    initiator_tg = make_user(507, "eligible_initiator")
    initiator_lookup, target_lookup, choice, start_fight = install_admission_spies(
        monkeypatch, duel
    )
    initiator_lookup.return_value = admission_user(
        initiator_tg.id,
        initiator_tg.username,
    )
    target_lookup.return_value = None

    await duel._process_duel_fight(
        fake_context,
        initiator_tg,
        "unknown_target",
        CHAT_ID,
    )

    target_lookup.assert_called_once_with("unknown_target", CHAT_ID)
    assert_admission_did_not_start(duel, choice, start_fight)
    assert "не найден" in fake_context.bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_duel_command_no_dick_fast_fails_before_target_resolution(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    initiator_tg = make_user(508, "command_blocked")
    initiator_lookup = Mock(
        return_value=admission_user(
            initiator_tg.id,
            initiator_tg.username,
            dick_stolen_today=True,
        )
    )
    process_fight = AsyncMock()
    extract_username = Mock(side_effect=AssertionError("target parsing must not run"))
    top_lookup = Mock(side_effect=AssertionError("top lookup must not run"))
    send_and_schedule = AsyncMock()
    monkeypatch.setattr(duel, "get_or_create_duel_user", initiator_lookup)
    monkeypatch.setattr(duel, "_process_duel_fight", process_fight)
    monkeypatch.setattr(duel, "_extract_username", extract_username)
    monkeypatch.setattr(duel, "get_duel_top", top_lookup)
    monkeypatch.setattr(duel, "send_and_schedule", send_and_schedule)
    message = SimpleNamespace(
        from_user=initiator_tg,
        chat=SimpleNamespace(id=CHAT_ID),
        chat_id=CHAT_ID,
        message_id=508,
        text="/duel @ignored_target",
    )
    update = SimpleNamespace(message=message)

    await duel.duel_command(update, fake_context)

    initiator_lookup.assert_called_once_with(initiator_tg, CHAT_ID)
    extract_username.assert_not_called()
    top_lookup.assert_not_called()
    process_fight.assert_not_awaited()
    send_and_schedule.assert_awaited_once()
    assert send_and_schedule.await_args.args[:2] == (update, fake_context)
    assert "Ты сегодня уже без хуя" in send_and_schedule.await_args.args[2]
    assert CHAT_ID not in duel.ACTIVE_DUELS


@pytest.mark.asyncio
async def test_duel_stats_command_preserves_yaml_backed_output(monkeypatch, fake_context):
    from handlers import duel

    user_tg = make_user(601, "stats_user")
    update = SimpleNamespace(
        message=SimpleNamespace(
            from_user=user_tg,
            chat=SimpleNamespace(id=CHAT_ID),
            chat_id=CHAT_ID,
        )
    )
    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    user_data = {
        "points": 37,
        "wins": 12,
        "losses": 8,
        "stolen_dicks_count": 3,
        "dick_stolen_today": False,
    }
    monkeypatch.setattr(
        duel,
        "get_or_create_duel_user",
        Mock(return_value=user_data),
    )
    monkeypatch.setattr(duel, "format_user_title", lambda _user: "<b>Статист</b>")
    monkeypatch.setattr(duel, "get_bosses_defeated", lambda **_kwargs: 4)
    monkeypatch.setattr(duel, "get_win_title", lambda _count: "🍆 Победитель")
    monkeypatch.setattr(duel, "get_loss_title", lambda _count: "💀 Лузер")
    monkeypatch.setattr(duel, "get_stolen_dicks_title", lambda _count: "🔪 Вор")

    await duel.duel_stats_command(update, fake_context)

    sent.assert_awaited_once_with(
        update,
        fake_context,
        "📊 <b>Статистика дуэлей: <b>Статист</b></b>\n\n"
        "Очки: <b>37 / 100</b>\n"
        "Побед: <b>12</b>\n"
        "Поражений: <b>8</b>\n"
        "Хуяние:\n<b>🍆 Победитель (12)\n💀 Лузер (8)\n🔪 Вор (3)</b>\n"
        "👹 Побеждено боссов: <b>4</b>\n"
        "Статус на сегодня: <b>С хуем 🍆</b>",
    )

    sent.reset_mock()
    user_data["dick_stolen_today"] = True
    monkeypatch.setattr(duel, "get_win_title", lambda _count: None)
    monkeypatch.setattr(duel, "get_loss_title", lambda _count: None)
    monkeypatch.setattr(duel, "get_stolen_dicks_title", lambda _count: None)

    await duel.duel_stats_command(update, fake_context)

    assert sent.await_args.args[2].endswith(
        "Хуяние:\n<b>Нет званий</b>\n"
        "👹 Побеждено боссов: <b>4</b>\n"
        "Статус на сегодня: <b>Без хуя 💀</b>"
    )


@pytest.mark.asyncio
async def test_duel_top_command_preserves_rows_and_uses_common_default_title(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    update = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=CHAT_ID), chat_id=CHAT_ID)
    )
    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    monkeypatch.setattr(duel, "TOP_SORT_BY", "points")
    monkeypatch.setattr(
        duel,
        "get_duel_top",
        Mock(return_value=[("alpha", "@Альфа", 7, 2, 65), (None, None, 1, 9, 5)]),
    )

    await duel.duel_top_command(update, fake_context)

    sent.assert_awaited_once_with(
        update,
        fake_context,
        "🏆 <b>Топ-10 гномьих дуэлянтов чата (по очкам):</b>\n\n"
        "1. <b>Альфа</b> — 65 очков (7W / 2L)\n"
        "2. <b>Гном</b> — 5 очков (1W / 9L)\n",
    )

    sent.reset_mock()
    monkeypatch.setattr(duel, "get_duel_top", Mock(return_value=[]))

    await duel.duel_top_command(update, fake_context)

    sent.assert_awaited_once_with(
        update,
        fake_context,
        "🏆 Таблица лидеров чата пока пуста.",
    )


@pytest.mark.asyncio
async def test_duel_delete_command_preserves_presentation_branches(monkeypatch, fake_context):
    from handlers import duel

    def update_for(user_id):
        return SimpleNamespace(
            message=SimpleNamespace(
                from_user=make_user(user_id, f"user_{user_id}"),
                chat=SimpleNamespace(id=CHAT_ID),
                chat_id=CHAT_ID,
            )
        )

    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    monkeypatch.setattr(duel, "ADMIN_IDS", {1})

    denied_update = update_for(2)
    await duel.duel_delete_command(denied_update, fake_context)
    sent.assert_awaited_once_with(denied_update, fake_context, "⛔ Недостаточно прав.")

    sent.reset_mock()
    monkeypatch.setattr(duel, "_extract_username", lambda *_args: None)
    usage_update = update_for(1)
    await duel.duel_delete_command(usage_update, fake_context)
    sent.assert_awaited_once_with(
        usage_update,
        fake_context,
        "⚠️ Укажите ник: <code>/duel_delete username</code>",
    )

    sent.reset_mock()
    monkeypatch.setattr(duel, "_extract_username", lambda *_args: "@victim")
    delete_user = Mock(return_value=True)
    monkeypatch.setattr(duel, "delete_duel_user_by_username", delete_user)
    success_update = update_for(1)
    await duel.duel_delete_command(success_update, fake_context)
    delete_user.assert_called_once_with("@victim", CHAT_ID)
    sent.assert_awaited_once_with(
        success_update,
        fake_context,
        "✅ Пользователь victim удален из базы дуэлей этого чата.",
    )

    sent.reset_mock()
    delete_user.reset_mock(return_value=True)
    delete_user.return_value = False
    missing_update = update_for(1)
    await duel.duel_delete_command(missing_update, fake_context)
    sent.assert_awaited_once_with(
        missing_update,
        fake_context,
        "❌ Пользователь victim не найден в базе этого чата.",
    )


@pytest.mark.asyncio
async def test_duel_selection_ui_preserves_text_labels_and_callback_data(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    initiator_tg = make_user(701, "initiator")
    update = SimpleNamespace(
        message=SimpleNamespace(
            from_user=initiator_tg,
            chat=SimpleNamespace(id=CHAT_ID),
            chat_id=CHAT_ID,
            message_id=701,
            text="/duel",
        )
    )
    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    monkeypatch.setattr(
        duel,
        "get_or_create_duel_user",
        Mock(return_value=admission_user(initiator_tg.id, initiator_tg.username)),
    )
    monkeypatch.setattr(duel, "_extract_username", lambda *_args: None)
    monkeypatch.setattr(
        duel,
        "get_duel_top",
        Mock(return_value=[("initiator", "Initiator", 0, 0, 20), ("opponent", "@Оппонент", 0, 0, 20)]),
    )
    monkeypatch.setattr(
        duel,
        "get_duel_user_by_username",
        lambda username, _chat_id: admission_user(702, username),
    )

    await duel.duel_command(update, fake_context)

    sent.assert_awaited_once()
    sent_args = sent.await_args.args
    assert sent_args[:2] == (update, fake_context)
    assert sent_args[2] == "🗡️ <b>Выберите соперника для дуэли:</b>"
    keyboard = sent.await_args.kwargs["reply_markup"].inline_keyboard
    assert [(button.text, button.callback_data) for row in keyboard for button in row] == [
        ("⚔️ Оппонент", "start_duel_opponent"),
    ]

    sent.reset_mock()
    monkeypatch.setattr(duel, "get_duel_top", Mock(return_value=[]))
    await duel.duel_command(update, fake_context)
    sent.assert_awaited_once_with(
        update,
        fake_context,
        "❌ В чате нет доступных соперников для дуэли (все без очков или без хуев).",
    )


@pytest.mark.asyncio
async def test_duel_action_alerts_preserve_all_invalid_callback_outputs(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    attacker = make_user(801, "attacker")
    defender = make_user(802, "defender")

    def battle(phase, turn_id=7):
        return {
            "lock": asyncio.Lock(),
            "phase": phase,
            "attacker_tg": attacker,
            "defender_tg": defender,
            "turn_id": turn_id,
            "turn_task": None,
        }

    cases = [
        (None, "duel_strike_head_7", attacker, "Дуэль не найдена или уже завершена."),
        (battle("block"), "duel_strike_head_7", attacker, "Сейчас не ваш ход. Ждите защиты соперника."),
        (battle("attack"), "duel_strike_head_7", defender, "Сейчас не ваш ход для атаки!"),
        (battle("attack"), "duel_strike_head", attacker, "Эта кнопка устарела."),
        (battle("attack"), "duel_strike_head_x", attacker, "Эта кнопка устарела."),
        (battle("attack"), "duel_strike_head_6", attacker, "Этот ход уже закончился."),
        (battle("attack"), "duel_strike_unknown_7", attacker, "Неизвестная зона атаки."),
        (battle("attack"), "duel_block_head_7", defender, "Сейчас не ваш ход. Ждите атаки соперника."),
        (battle("block"), "duel_block_head_7", attacker, "Сейчас не ваш ход для защиты!"),
        (battle("block"), "duel_block_head", defender, "Эта кнопка устарела."),
        (battle("block"), "duel_block_head_x", defender, "Эта кнопка устарела."),
        (battle("block"), "duel_block_head_6", defender, "Этот ход уже закончился."),
        (battle("block"), "duel_block_unknown_7", defender, "Неизвестная зона защиты."),
    ]

    for active_battle, callback_data, user, expected_text in cases:
        duel.ACTIVE_DUELS.clear()
        if active_battle is not None:
            duel.ACTIVE_DUELS[CHAT_ID] = active_battle
        query = SimpleNamespace(data=callback_data, from_user=user, answer=AsyncMock())
        update = SimpleNamespace(
            callback_query=query,
            effective_chat=SimpleNamespace(id=CHAT_ID),
        )

        await duel.duel_strike_callback(update, fake_context)

        query.answer.assert_awaited_once_with(expected_text, show_alert=True)

    duel.ACTIVE_DUELS[CHAT_ID] = battle("attack")
    process_attack = AsyncMock()
    monkeypatch.setattr(duel, "_process_attack_choice", process_attack)
    query = SimpleNamespace(
        data="duel_strike_head_7",
        from_user=attacker,
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=CHAT_ID),
    )

    await duel.duel_strike_callback(update, fake_context)

    query.answer.assert_awaited_once_with()
    process_attack.assert_awaited_once_with(fake_context, CHAT_ID, "head")


@pytest.mark.asyncio
async def test_live_duel_templates_preserve_start_transition_and_timeout_output(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    attacker = make_user(901, "attacker")
    defender = make_user(902, "defender")
    attacker_data = {"user_id": attacker.id, "title": "Атакующий"}
    defender_data = {"user_id": defender.id, "title": "Защитник"}
    monkeypatch.setattr(duel, "format_user_title", lambda user: user["title"])
    tasks = install_fake_tasks(monkeypatch, duel)

    await duel._start_interactive_fight(
        fake_context,
        CHAT_ID,
        attacker,
        defender,
        attacker_data,
        defender_data,
    )

    start_kwargs = fake_context.bot.send_message.await_args.kwargs
    assert start_kwargs["text"] == (
        "🗡️ <b>Гномья дуэль начинается!</b>\n\n"
        "⚔️ Атакует: <b>Атакующий</b>\n"
        "🛡️ Защищается: <b>Защитник</b>\n\n"
        "⏳ У <b>Атакующий</b> есть 10 секунд, чтобы выбрать точку удара:"
    )
    assert [button.callback_data for button in start_kwargs["reply_markup"].inline_keyboard[0]] == [
        "duel_strike_head_1", "duel_strike_body_1", "duel_strike_dick_1"
    ]

    await duel._process_attack_choice(fake_context, CHAT_ID, "head")

    state = duel.ACTIVE_DUELS[CHAT_ID]
    transition_kwargs = fake_context.bot.edit_message_text.await_args.kwargs
    assert transition_kwargs["text"] == (
        "🗡️ <b>Гномья дуэль! Раунд 1</b>\n\n"
        "⚔️ <b>Атакующий</b> наносит замах!\n"
        "🛡️ <b>Защитник</b>, выберите зону защиты!\n\n"
        "⏳ У <b>Защитник</b> есть 10 секунд на выбор блока:"
    )
    assert state["phase"] == "block"
    assert state["attack_zone"] == "head"
    assert state["turn_id"] == 2
    assert state["turn_task"] is tasks[1]

    fake_context.bot.send_message.reset_mock()
    sleep = AsyncMock()
    auto_choice = Mock(return_value="dick")
    process_block = AsyncMock()
    monkeypatch.setattr(duel.asyncio, "sleep", sleep)
    monkeypatch.setattr(duel.random, "choice", auto_choice)
    monkeypatch.setattr(duel, "_process_block_choice", process_block)

    await duel._auto_move_timer(fake_context, CHAT_ID, 1, "block", 2)

    auto_choice.assert_called_once_with(["head", "body", "dick"])
    process_block.assert_awaited_once_with(fake_context, CHAT_ID, "dick")
    assert fake_context.bot.send_message.await_args.kwargs["text"] == (
        "⏰ <b>Защитник</b> зазевался! Гномий синедрион делает случайный выбор блока..."
    )


@pytest.mark.asyncio
async def test_live_duel_outcome_wrappers_preserve_exact_text_and_rng_order(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    state = make_block_phase_duel(strike_zone="head", turn_id=7)
    state["attacker_data"] = {"user_id": 601, "title": "Атакующий"}
    state["defender_data"] = {"user_id": 602, "title": "Защитник"}
    duel.ACTIVE_DUELS[CHAT_ID] = state
    monkeypatch.setattr(duel, "format_user_title", lambda user: user["title"])
    finish = AsyncMock()
    events = []

    def suicide_random():
        events.append("suicide_roll")
        return 0.0

    def suicide_choice(values):
        events.append("suicide_phrase")
        assert values is duel.SUICIDE_PHRASES
        return "споткнулся"

    monkeypatch.setattr(duel, "_finish_duel", finish)
    monkeypatch.setattr(duel, "random", SimpleNamespace(random=suicide_random, choice=suicide_choice))
    await duel._process_block_choice(fake_context, CHAT_ID, "body")

    assert events == ["suicide_roll", "suicide_phrase"]
    assert finish.await_args.kwargs["custom_text"] == (
        "💥 <b>НЕВЕРОЯТНЫЙ ИСХОД!</b>\n\n"
        "<b>Атакующий</b> споткнулся\n\n"
        "🏆 <b>Защитник</b> победил, просто не мешая сопернику убить себя."
    )

    state = make_block_phase_duel(strike_zone="head", turn_id=7)
    state["attacker_data"] = {"user_id": 601, "title": "Атакующий"}
    state["defender_data"] = {"user_id": 602, "title": "Защитник"}
    duel.ACTIVE_DUELS[CHAT_ID] = state
    finish.reset_mock()
    events.clear()
    rolls = iter((0.5, 0.5))

    def hit_random():
        events.append("roll")
        return next(rolls)

    def hit_choice(values):
        if values is duel.HIT_PHRASES:
            events.append("hit_phrase")
            return "попал"
        events.append("attack_phrase")
        assert values is duel.ATTACK_PHRASES
        return "атакует"

    monkeypatch.setattr(duel, "random", SimpleNamespace(random=hit_random, choice=hit_choice))
    await duel._process_block_choice(fake_context, CHAT_ID, "body")

    assert events == ["roll", "roll", "hit_phrase", "attack_phrase"]
    assert finish.await_args.kwargs["custom_text"] == (
        "💥 <b>ТОЧНЫЙ УДАР!</b>\n"
        "<b>Атакующий</b> атакует в зону (Голова 🧠), а <b>Защитник</b> блокировал (Торс 🛡️).\n"
        "<b>Атакующий</b> попал\n"
    )
