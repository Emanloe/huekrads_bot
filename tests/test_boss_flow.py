import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest


class FakeTask:
    def __init__(self, coroutine_name="task", coroutine_locals=None):
        self.coroutine_name = coroutine_name
        self.coroutine_locals = coroutine_locals or {}
        self.cancel_calls = 0
        self.is_done = False

    def done(self):
        return self.is_done

    def cancel(self):
        self.cancel_calls += 1


class TaskRecorder:
    def __init__(self):
        self.tasks = []

    def __call__(self, coroutine):
        frame = coroutine.cr_frame
        coroutine_locals = dict(frame.f_locals) if frame is not None else {}
        task = FakeTask(coroutine.cr_code.co_name, coroutine_locals)
        self.tasks.append(task)
        coroutine.close()
        return task


def make_participant(
    user_id,
    *,
    alive=True,
    attack=None,
    block=None,
    hits=0,
    misses=0,
    blocks=0,
    rounds_survived=0,
):
    return {
        "tg_user": SimpleNamespace(id=user_id, username=f"user{user_id}"),
        "data": {"username": f"@user{user_id}"},
        "attack": attack,
        "block": block,
        "alive": alive,
        "hits": hits,
        "misses": misses,
        "blocks": blocks,
        "rounds_survived": rounds_survived,
        "death_round": None,
        "death_by_zone": None,
        "death_defended_zone": None,
        "death_attack_zone": None,
    }


def make_battle(
    participants,
    *,
    phase="attack",
    round_num=1,
    hits=0,
    phase_task=None,
    boss_attack="head",
    boss_block="body",
    message_id=700,
):
    return {
        "boss": {"name": "Тестовый Босс", "emoji": "👹"},
        "participants": {p["tg_user"].id: p for p in participants},
        "hits": hits,
        "round": round_num,
        "phase": phase,
        "message_id": message_id,
        "phase_task": phase_task,
        "lock": asyncio.Lock(),
        "boss_attack": boss_attack,
        "boss_block": boss_block,
    }


def make_callback_update(chat_id, user_id, data, *, alive_user=None):
    from_user = alive_user or SimpleNamespace(
        id=user_id,
        username=f"user{user_id}",
        first_name=f"User {user_id}",
        last_name=None,
    )
    query = SimpleNamespace(
        data=data,
        from_user=from_user,
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=chat_id),
    )
    return update, query


def relevant_snapshot(battle):
    return {
        "boss": copy.deepcopy(battle["boss"]),
        "participants": copy.deepcopy(battle["participants"]),
        "hits": battle["hits"],
        "round": battle["round"],
        "phase": battle["phase"],
        "message_id": battle["message_id"],
        "phase_task": battle["phase_task"],
        "lock": battle["lock"],
        "boss_attack": battle["boss_attack"],
        "boss_block": battle["boss_block"],
    }


@pytest.mark.asyncio
async def test_boss_join_callback_inserts_current_participant_snapshot(
    monkeypatch, fake_context
):
    from handlers import duel

    chat_id = -714
    tg_user = SimpleNamespace(
        id=314,
        username="joiner",
        first_name="Join",
        last_name="User",
    )
    update, query = make_callback_update(
        chat_id, tg_user.id, "boss_join", alive_user=tg_user
    )
    battle = make_battle(
        [],
        phase="join",
        round_num=0,
        boss_attack=None,
        boss_block=None,
        message_id=714,
    )
    battle["unrelated"] = {"preserve": True}
    battle_identity = battle
    participants_identity = battle["participants"]
    unrelated_before = copy.deepcopy(battle["unrelated"])
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle

    user_snapshot = {
        "user_id": tg_user.id,
        "username": "@stored_joiner",
        "points": 17,
        "custom_snapshot_value": "must stay identical",
    }
    db_lookup = Mock(return_value=user_snapshot)
    monkeypatch.setattr(duel, "get_or_create_duel_user", db_lookup)

    await duel.boss_callback(update, fake_context)

    db_lookup.assert_called_once_with(tg_user, chat_id)
    participant = battle["participants"][tg_user.id]
    assert set(participant) == {
        "tg_user",
        "data",
        "attack",
        "block",
        "alive",
        "hits",
        "misses",
        "blocks",
        "rounds_survived",
        "death_round",
        "death_by_zone",
        "death_defended_zone",
        "death_attack_zone",
    }
    assert participant["tg_user"] is tg_user
    assert participant["data"] is user_snapshot
    assert participant["attack"] is None
    assert participant["block"] is None
    assert participant["alive"] is True
    assert participant["hits"] == 0
    assert participant["misses"] == 0
    assert participant["blocks"] == 0
    assert participant["rounds_survived"] == 0
    assert participant["death_round"] is None
    assert participant["death_by_zone"] is None
    assert participant["death_defended_zone"] is None
    assert participant["death_attack_zone"] is None
    assert battle is battle_identity
    assert battle["participants"] is participants_identity
    assert battle["phase"] == "join"
    assert battle["unrelated"] == unrelated_before

    query.answer.assert_awaited_once_with("Ты вступил в битву! ⚔️")
    fake_context.bot.edit_message_text.assert_awaited_once()
    edit_kwargs = fake_context.bot.edit_message_text.await_args.kwargs
    assert edit_kwargs["chat_id"] == chat_id
    assert edit_kwargs["message_id"] == battle["message_id"]
    assert edit_kwargs["parse_mode"] == "HTML"
    assert edit_kwargs["text"] == (
        "💀 <b>Тестовый Босс</b>\n\n"
        "👹 Босс готов к битве!\n\n"
        "👥 Участников: <b>1</b>\n\n"
        "⚔️ Присоединяйтесь к бойне."
    )
    assert edit_kwargs["reply_markup"].inline_keyboard[0][0].text == (
        "⚔️ Присоединиться"
    )
    assert edit_kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "boss_join"


@pytest.mark.asyncio
async def test_boss_join_callback_rejects_duplicate_without_db_or_state_change(
    monkeypatch, fake_context
):
    from handlers import duel

    chat_id = -715
    existing = make_participant(315)
    battle = make_battle(
        [existing],
        phase="join",
        round_num=0,
        boss_attack=None,
        boss_block=None,
        message_id=715,
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    before = relevant_snapshot(battle)
    participant_identity = battle["participants"][315]
    update, query = make_callback_update(chat_id, 315, "boss_join")
    db_lookup = Mock(side_effect=AssertionError("duplicate join must not query DB"))
    monkeypatch.setattr(duel, "get_or_create_duel_user", db_lookup)

    await duel.boss_callback(update, fake_context)

    db_lookup.assert_not_called()
    assert relevant_snapshot(battle) == before
    assert battle["participants"][315] is participant_identity
    query.answer.assert_awaited_once_with("Ты уже участвуешь.", show_alert=True)
    fake_context.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_boss_callback_reports_finished_battle_alert():
    from handlers import duel

    update, query = make_callback_update(-718, 1, "boss_join")

    await duel.boss_callback(update, SimpleNamespace())

    query.answer.assert_awaited_once_with(
        "Битва уже закончилась.",
        show_alert=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "participants", "callback_data", "expected"),
    [
        ("attack", [], "boss_join", "Битва уже началась."),
        ("block", [], "boss_attack_head_4", "Сейчас фаза защиты."),
        ("attack", [], "boss_attack_head_4", "Ты не участвуешь в битве."),
        ("attack", [make_participant(1, alive=False)], "boss_attack_head_4", "Ты уже погиб."),
        ("attack", [make_participant(1)], "boss_attack_head", "Устаревшая кнопка."),
        ("attack", [make_participant(1)], "boss_attack_head_old", "Устаревшая кнопка."),
        ("attack", [make_participant(1)], "boss_attack_head_3", "Этот раунд уже закончился."),
        ("attack", [make_participant(1)], "boss_attack_arm_4", "Неизвестная зона."),
        ("attack", [make_participant(1)], "boss_block_head_4", "Сначала все должны выбрать атаку."),
    ],
)
async def test_boss_callback_validation_alerts_are_exact(
    phase,
    participants,
    callback_data,
    expected,
):
    from handlers import duel

    chat_id = -716
    battle = make_battle(participants, phase=phase, round_num=4)
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    update, query = make_callback_update(chat_id, 1, callback_data)

    await duel.boss_callback(update, SimpleNamespace())

    query.answer.assert_awaited_once_with(expected, show_alert=True)


@pytest.mark.asyncio
async def test_boss_callback_reports_exact_action_acknowledgements(monkeypatch):
    from handlers import duel

    chat_id = -717
    first = make_participant(1)
    second = make_participant(2)
    battle = make_battle([first, second], phase="attack", round_num=4)
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    monkeypatch.setattr(duel, "_boss_render_phase", AsyncMock())

    update, attack_query = make_callback_update(chat_id, 1, "boss_attack_head_4")
    await duel.boss_callback(update, SimpleNamespace())
    attack_query.answer.assert_awaited_once_with("Атака: Голова ⚔️")

    battle["phase"] = "block"
    update, block_query = make_callback_update(chat_id, 1, "boss_block_body_4")
    await duel.boss_callback(update, SimpleNamespace())
    block_query.answer.assert_awaited_once_with("Защита: Торс 🛡")


@pytest.fixture(autouse=True)
def clear_active_boss_battles():
    from handlers import duel

    duel.ACTIVE_BOSS_BATTLES.clear()
    yield
    duel.ACTIVE_BOSS_BATTLES.clear()


@pytest.mark.asyncio
async def test_start_boss_battle_creates_complete_join_state(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    chat_id = -701
    boss = {"name": "Выбранный Босс", "emoji": "💀", "description": "desc"}
    choose = Mock(return_value=boss)
    tasks = TaskRecorder()
    registrations = Mock(side_effect=AssertionError("registrations were read"))
    monkeypatch.setattr(duel.random, "choice", choose)
    monkeypatch.setattr(duel, "_boss_get_registered_users", registrations)
    monkeypatch.setattr(duel.asyncio, "create_task", tasks)

    started = await duel._start_boss_battle(
        fake_context,
        chat_id,
        include_registrations=False,
    )

    assert started is True
    battle = duel.ACTIVE_BOSS_BATTLES[chat_id]
    assert battle["boss"] is boss
    assert battle["participants"] == {}
    assert battle["hits"] == 0
    assert battle["round"] == 0
    assert battle["phase"] == "join"
    assert battle["message_id"] == 101
    assert battle["phase_task"] is tasks.tasks[0]
    assert isinstance(battle["lock"], asyncio.Lock)
    assert battle["boss_attack"] is None
    assert battle["boss_block"] is None
    assert tasks.tasks[0].coroutine_name == "_boss_join_timer"
    assert tasks.tasks[0].coroutine_locals["chat_id"] == chat_id
    choose.assert_called_once_with(duel.BOSSES)
    registrations.assert_not_called()
    send_kwargs = fake_context.bot.send_message.await_args.kwargs
    assert send_kwargs["text"] == (
        "💀 <b>Выбранный Босс</b>\n\n"
        "👹 В чат явился босс!\n\n"
        "🎯 Его нужно поразить <b>5 раз</b>.\n"
        "💀 Босс убивает с одного удара, если игрок не заблокировал нужную зону.\n\n"
        "⚔️ Нажимайте кнопку ниже, чтобы присоединиться."
    )
    assert send_kwargs["reply_markup"].inline_keyboard[0][0].text == "⚔️ Присоединиться"
    assert send_kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "boss_join"


@pytest.mark.asyncio
async def test_start_boss_battle_preserves_pre_registered_join_presentation(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    chat_id = -721
    boss = {"name": "Выбранный Босс", "emoji": "💀", "description": "desc"}
    tasks = TaskRecorder()
    monkeypatch.setattr(duel.random, "choice", Mock(return_value=boss))
    monkeypatch.setattr(duel, "_boss_get_registered_users", Mock(return_value=[(1,)]))
    monkeypatch.setattr(
        duel,
        "_boss_tg_user_from_registration",
        lambda _: SimpleNamespace(id=1),
    )
    monkeypatch.setattr(
        duel,
        "_boss_make_participant",
        lambda *_: {"registered": True},
    )
    monkeypatch.setattr(duel.asyncio, "create_task", tasks)

    assert await duel._start_boss_battle(
        fake_context,
        chat_id,
        include_registrations=True,
    ) is True

    fake_context.bot.edit_message_text.assert_awaited_once()
    edit_kwargs = fake_context.bot.edit_message_text.await_args.kwargs
    assert edit_kwargs["text"] == (
        "💀 <b>Выбранный Босс</b>\n\n"
        "👹 Босс явился в подземелье!\n\n"
        "⚔️ Заранее записались: <b>1</b>\n\n"
        "Другие храбрецы ещё могут вступить в бой."
    )
    button = edit_kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "⚔️ Присоединиться"
    assert button.callback_data == "boss_join"


@pytest.mark.asyncio
async def test_boss_join_timeout_removes_empty_battle_and_edits_message(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    chat_id = -702
    battle = make_battle(
        [],
        phase="join",
        round_num=0,
        boss_attack=None,
        boss_block=None,
        message_id=702,
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    monkeypatch.setattr(duel.asyncio, "sleep", AsyncMock())

    await duel._boss_join_timer(fake_context, chat_id)

    assert chat_id not in duel.ACTIVE_BOSS_BATTLES
    fake_context.bot.edit_message_text.assert_awaited_once_with(
        chat_id=chat_id,
        message_id=702,
        text=(
            "💀 <b>Тестовый Босс</b>\n\n"
            "Никто не осмелился вступить в битву.\n\n"
            "Босс ушёл ждать более храбрых гномов."
        ),
        parse_mode="HTML",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "field", "zone", "expected"),
    [
        ("attack", "attack", "head", "⚔️ <b>user1</b> зазевался. Нож сам пошёл в <b>Голова</b>."),
        ("block", "block", "body", "🛡 <b>user1</b> зазевался. Рука сама прикрыла <b>Торс</b>."),
    ],
)
async def test_boss_auto_choice_timeout_presentation_is_exact(
    monkeypatch,
    fake_context,
    phase,
    field,
    zone,
    expected,
):
    from handlers import duel

    participant = make_participant(1)
    auto_zone = Mock(return_value=zone)
    monkeypatch.setattr(duel, "_boss_auto_zone", auto_zone)
    monkeypatch.setattr(duel, "schedule_auto_delete", Mock())

    await duel._boss_auto_choose_for_zazevasha(
        fake_context,
        -719,
        {},
        participant,
        phase,
    )

    assert participant[field] == zone
    auto_zone.assert_called_once_with()
    fake_context.bot.send_message.assert_awaited_once_with(
        chat_id=-719,
        text=expected,
        parse_mode="HTML",
    )


@pytest.mark.asyncio
async def test_boss_join_timeout_starts_round_and_attack_timer_in_rng_order(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    chat_id = -703
    participant = make_participant(1, attack="dick", block="head")
    join_task = FakeTask("_boss_join_timer")
    battle = make_battle(
        [participant],
        phase="join",
        round_num=0,
        phase_task=join_task,
        boss_attack=None,
        boss_block=None,
        message_id=703,
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    choose = Mock(side_effect=["body", "dick"])
    tasks = TaskRecorder()
    monkeypatch.setattr(duel.random, "choice", choose)
    monkeypatch.setattr(duel.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(duel.asyncio, "current_task", Mock(return_value=join_task))
    monkeypatch.setattr(duel.asyncio, "create_task", tasks)

    await duel._boss_join_timer(fake_context, chat_id)

    assert duel.ACTIVE_BOSS_BATTLES[chat_id] is battle
    assert battle["round"] == 1
    assert battle["phase"] == "attack"
    assert battle["boss_attack"] == "body"
    assert battle["boss_block"] == "dick"
    assert participant["attack"] is None
    assert participant["block"] is None
    assert choose.call_args_list == [call(duel.BOSS_ZONES), call(duel.BOSS_ZONES)]
    assert join_task.cancel_calls == 0
    assert battle["phase_task"] is tasks.tasks[0]
    assert tasks.tasks[0].coroutine_name == "_boss_phase_timer"
    assert tasks.tasks[0].coroutine_locals["round_num"] == 1
    assert tasks.tasks[0].coroutine_locals["phase"] == "attack"


@pytest.mark.asyncio
async def test_boss_callback_preserves_choice_overwrite_and_phase_progression(
    monkeypatch,
):
    from handlers import duel

    chat_id = -704
    attack_timer = FakeTask("attack_timer")
    first = make_participant(1, block="head")
    second = make_participant(2, block="dick")
    dead = make_participant(3, alive=False)
    battle = make_battle(
        [first, second, dead],
        phase="attack",
        round_num=4,
        phase_task=attack_timer,
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    render = AsyncMock()
    tasks = TaskRecorder()
    monkeypatch.setattr(duel, "_boss_render_phase", render)
    monkeypatch.setattr(duel.asyncio, "create_task", tasks)

    update, _ = make_callback_update(chat_id, 1, "boss_attack_head_4")
    await duel.boss_callback(update, SimpleNamespace())
    assert first["attack"] == "head"
    assert battle["phase"] == "attack"

    update, _ = make_callback_update(chat_id, 1, "boss_attack_body_4")
    await duel.boss_callback(update, SimpleNamespace())
    assert first["attack"] == "body"

    before_invalid = relevant_snapshot(battle)
    update, stale_query = make_callback_update(chat_id, 2, "boss_attack_head_3")
    await duel.boss_callback(update, SimpleNamespace())
    assert relevant_snapshot(battle) == before_invalid
    stale_query.answer.assert_awaited_once_with(
        "Этот раунд уже закончился.",
        show_alert=True,
    )

    update, outsider_query = make_callback_update(chat_id, 99, "boss_attack_head_4")
    await duel.boss_callback(update, SimpleNamespace())
    assert relevant_snapshot(battle) == before_invalid
    outsider_query.answer.assert_awaited_once_with(
        "Ты не участвуешь в битве.",
        show_alert=True,
    )

    update, dead_query = make_callback_update(chat_id, 3, "boss_attack_head_4")
    await duel.boss_callback(update, SimpleNamespace())
    assert relevant_snapshot(battle) == before_invalid
    dead_query.answer.assert_awaited_once_with("Ты уже погиб.", show_alert=True)

    update, _ = make_callback_update(chat_id, 2, "boss_attack_dick_4")
    await duel.boss_callback(update, SimpleNamespace())
    assert second["attack"] == "dick"
    assert battle["phase"] == "block"
    assert first["block"] is None
    assert second["block"] is None
    assert attack_timer.cancel_calls == 1
    block_timer = battle["phase_task"]
    assert block_timer.coroutine_name == "_boss_phase_timer"
    assert block_timer.coroutine_locals["phase"] == "block"

    update, _ = make_callback_update(chat_id, 1, "boss_block_head_4")
    await duel.boss_callback(update, SimpleNamespace())
    assert first["block"] == "head"

    update, _ = make_callback_update(chat_id, 1, "boss_block_body_4")
    await duel.boss_callback(update, SimpleNamespace())
    assert first["block"] == "body"
    assert battle["phase"] == "block"

    update, _ = make_callback_update(chat_id, 2, "boss_block_dick_4")
    await duel.boss_callback(update, SimpleNamespace())
    assert second["block"] == "dick"
    assert battle["phase"] == "block"
    assert battle["phase_task"] is None
    assert block_timer.cancel_calls == 1
    assert tasks.tasks[-1].coroutine_name == "_boss_resolve_round"


@pytest.mark.asyncio
async def test_repeated_boss_resolver_does_not_apply_same_round_twice(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    chat_id = -705
    participant = make_participant(1, attack="head", block="head")
    battle = make_battle(
        [participant],
        phase="block",
        boss_attack="head",
        boss_block="body",
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    start_round = AsyncMock()
    auto_zone = Mock(side_effect=AssertionError("valid choices used fallback RNG"))
    monkeypatch.setattr(duel, "_boss_start_round", start_round)
    monkeypatch.setattr(duel, "_boss_auto_zone", auto_zone)
    monkeypatch.setattr(duel.asyncio, "sleep", AsyncMock())

    await duel._boss_resolve_round(fake_context, chat_id)
    after_first = relevant_snapshot(battle)
    await duel._boss_resolve_round(fake_context, chat_id)

    assert relevant_snapshot(battle) == after_first
    assert battle["hits"] == 1
    assert participant["hits"] == 1
    assert participant["blocks"] == 1
    assert battle["phase"] == "resolving"
    start_round.assert_awaited_once_with(fake_context, chat_id)
    auto_zone.assert_not_called()


@pytest.mark.asyncio
async def test_boss_attack_phase_timer_auto_chooses_only_missing_alive_players(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    chat_id = -706
    timer_task = FakeTask("attack_timer")
    chosen = make_participant(1, attack="body")
    first_missing = make_participant(2)
    dead_missing = make_participant(3, alive=False)
    second_missing = make_participant(4)
    battle = make_battle(
        [chosen, first_missing, dead_missing, second_missing],
        phase="attack",
        round_num=2,
        phase_task=timer_task,
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    choose = Mock(side_effect=["head", "dick"])
    tasks = TaskRecorder()
    monkeypatch.setattr(duel.random, "choice", choose)
    monkeypatch.setattr(duel.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(duel.asyncio, "current_task", Mock(return_value=timer_task))
    monkeypatch.setattr(duel.asyncio, "create_task", tasks)

    await duel._boss_phase_timer(fake_context, chat_id, 2, "attack")

    assert chosen["attack"] == "body"
    assert first_missing["attack"] == "head"
    assert dead_missing["attack"] is None
    assert second_missing["attack"] == "dick"
    assert choose.call_args_list == [call(duel.BOSS_ZONES), call(duel.BOSS_ZONES)]
    assert battle["phase"] == "block"
    assert battle["phase_task"] is tasks.tasks[0]
    assert tasks.tasks[0].coroutine_name == "_boss_phase_timer"
    assert tasks.tasks[0].coroutine_locals["round_num"] == 2
    assert tasks.tasks[0].coroutine_locals["phase"] == "block"
    assert fake_context.bot.send_message.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_kind", ["task_identity", "round"])
async def test_boss_phase_timer_stale_guard_preserves_complete_state(
    monkeypatch,
    fake_context,
    stale_kind,
):
    from handlers import duel

    chat_id = -707
    stored_task = FakeTask("stored_timer")
    current_task = stored_task if stale_kind == "round" else FakeTask("stale_timer")
    battle = make_battle(
        [make_participant(1)],
        phase="attack",
        round_num=3,
        phase_task=stored_task,
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    before = relevant_snapshot(battle)
    choose = Mock(side_effect=AssertionError("stale timer used RNG"))
    create_task = Mock(side_effect=AssertionError("stale timer created a task"))
    monkeypatch.setattr(duel.random, "choice", choose)
    monkeypatch.setattr(duel.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(duel.asyncio, "current_task", Mock(return_value=current_task))
    monkeypatch.setattr(duel.asyncio, "create_task", create_task)
    requested_round = 2 if stale_kind == "round" else 3

    await duel._boss_phase_timer(
        fake_context,
        chat_id,
        requested_round,
        "attack",
    )

    assert relevant_snapshot(battle) == before
    choose.assert_not_called()
    create_task.assert_not_called()
    fake_context.bot.send_message.assert_not_awaited()
    fake_context.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_boss_block_phase_timer_auto_chooses_then_awaits_resolver(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    chat_id = -708
    timer_task = FakeTask("block_timer")
    chosen = make_participant(1, attack="head", block="head")
    missing = make_participant(2, attack="body")
    dead = make_participant(3, alive=False, attack="dick")
    battle = make_battle(
        [chosen, missing, dead],
        phase="block",
        round_num=5,
        phase_task=timer_task,
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    choose = Mock(return_value="dick")
    resolve = AsyncMock()
    monkeypatch.setattr(duel.random, "choice", choose)
    monkeypatch.setattr(duel, "_boss_resolve_round", resolve)
    monkeypatch.setattr(duel.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(duel.asyncio, "current_task", Mock(return_value=timer_task))

    await duel._boss_phase_timer(fake_context, chat_id, 5, "block")

    assert chosen["block"] == "head"
    assert missing["block"] == "dick"
    assert dead["block"] is None
    choose.assert_called_once_with(duel.BOSS_ZONES)
    assert battle["phase"] == "block"
    assert battle["phase_task"] is None
    resolve.assert_awaited_once_with(fake_context, chat_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "initial_hits", "participants", "expected_hits"),
    [
        (
            "continue",
            0,
            [
                make_participant(1, attack="head", block="head"),
                make_participant(2, attack="body", block="body"),
            ],
            1,
        ),
        (
            "victory",
            4,
            [make_participant(1, attack="head", block="head")],
            5,
        ),
        (
            "defeat",
            0,
            [make_participant(1, attack="body", block="body")],
            0,
        ),
        (
            "victory_and_defeat",
            4,
            [make_participant(1, attack="head", block="body")],
            5,
        ),
    ],
)
async def test_boss_resolve_round_mutations_and_outcome_precedence(
    monkeypatch,
    fake_context,
    outcome,
    initial_hits,
    participants,
    expected_hits,
):
    from handlers import duel

    chat_id = -710
    battle = make_battle(
        participants,
        phase="block",
        round_num=6,
        hits=initial_hits,
        boss_attack="head",
        boss_block="body",
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    finish_victory = AsyncMock()
    finish_defeat = AsyncMock()
    start_round = AsyncMock()
    auto_zone = Mock(side_effect=AssertionError("valid choices used fallback RNG"))
    monkeypatch.setattr(duel, "_boss_finish_victory", finish_victory)
    monkeypatch.setattr(duel, "_boss_finish_defeat", finish_defeat)
    monkeypatch.setattr(duel, "_boss_start_round", start_round)
    monkeypatch.setattr(duel, "_boss_auto_zone", auto_zone)
    monkeypatch.setattr(duel.asyncio, "sleep", AsyncMock())

    await duel._boss_resolve_round(fake_context, chat_id)

    assert battle["hits"] == expected_hits
    assert battle["round"] == 6
    assert battle["phase"] == "resolving"
    auto_zone.assert_not_called()

    if outcome == "continue":
        survivor, killed = participants
        assert survivor["hits"] == 1
        assert survivor["misses"] == 0
        assert survivor["blocks"] == 1
        assert survivor["rounds_survived"] == 1
        assert survivor["alive"] is True
        assert survivor["attack"] == "head"
        assert survivor["block"] == "head"
        assert killed["hits"] == 0
        assert killed["misses"] == 1
        assert killed["blocks"] == 0
        assert killed["rounds_survived"] == 0
        assert killed["alive"] is False
        assert killed["death_round"] == 6
        assert killed["death_by_zone"] == "head"
        assert killed["death_defended_zone"] == "body"
        assert killed["death_attack_zone"] == "body"
        start_round.assert_awaited_once_with(fake_context, chat_id)
        finish_victory.assert_not_awaited()
        finish_defeat.assert_not_awaited()
    elif outcome == "victory":
        participant = participants[0]
        assert participant["hits"] == 1
        assert participant["blocks"] == 1
        assert participant["rounds_survived"] == 1
        assert participant["alive"] is True
        finish_victory.assert_awaited_once_with(fake_context, chat_id)
        finish_defeat.assert_not_awaited()
        start_round.assert_not_awaited()
    elif outcome == "defeat":
        participant = participants[0]
        assert participant["misses"] == 1
        assert participant["alive"] is False
        assert participant["death_round"] == 6
        assert participant["death_by_zone"] == "head"
        assert participant["death_defended_zone"] == "body"
        assert participant["death_attack_zone"] == "body"
        finish_defeat.assert_awaited_once_with(fake_context, chat_id)
        finish_victory.assert_not_awaited()
        start_round.assert_not_awaited()
    else:
        participant = participants[0]
        assert participant["hits"] == 1
        assert participant["alive"] is False
        assert participant["death_round"] == 6
        finish_victory.assert_awaited_once_with(fake_context, chat_id)
        finish_defeat.assert_not_awaited()
        start_round.assert_not_awaited()


@pytest.mark.asyncio
async def test_boss_round_resolution_presentation_is_exact(monkeypatch, fake_context):
    from handlers import duel

    chat_id = -720
    battle = make_battle(
        [make_participant(1, attack="head", block="head")],
        phase="block",
        round_num=6,
        boss_attack="head",
        boss_block="body",
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    monkeypatch.setattr(duel, "_boss_start_round", AsyncMock())
    monkeypatch.setattr(duel.asyncio, "sleep", AsyncMock())

    await duel._boss_resolve_round(fake_context, chat_id)

    fake_context.bot.edit_message_text.assert_awaited_once_with(
        chat_id=chat_id,
        message_id=battle["message_id"],
        text=(
            "💥 <b>РАУНД 6 — РЕЗУЛЬТАТ</b>\n\n"
            "👹 Босс атаковал: <b>Голова</b>\n"
            "🛡 Босс защищал: <b>Торс</b>\n\n"
            "<b>user1</b>\n"
            "⚔️ Голова → 💥 ПОПАДАНИЕ\n"
            "🛡 Голова → 🛡 ЗАБЛОКИРОВАЛ\n\n"
            "🎯 Урон боссу: <b>1 / 5</b>\n"
            "👥 В живых: <b>1</b> / <b>1</b>"
        ),
        parse_mode="HTML",
    )


@pytest.mark.asyncio
async def test_boss_finish_victory_cleans_state_rewards_only_alive_and_reports(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    chat_id = -711
    timer = FakeTask("phase_timer")
    alive = make_participant(1)
    dead = make_participant(2, alive=False)
    battle = make_battle([alive, dead], phase="resolving", phase_task=timer)
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle

    def reward(*, user_id, chat_id):
        assert chat_id not in duel.ACTIVE_BOSS_BATTLES
        return True

    reward_mock = Mock(side_effect=reward)

    async def report(context, report_chat_id, report_battle, *, victory):
        assert report_chat_id not in duel.ACTIVE_BOSS_BATTLES
        assert report_battle is battle
        assert victory is True

    report_mock = AsyncMock(side_effect=report)
    monkeypatch.setattr(duel, "reward_boss_victory", reward_mock)
    monkeypatch.setattr(duel, "_boss_send_final_report", report_mock)

    await duel._boss_finish_victory(fake_context, chat_id)

    assert chat_id not in duel.ACTIVE_BOSS_BATTLES
    assert timer.cancel_calls == 1
    reward_mock.assert_called_once_with(user_id=1, chat_id=chat_id)
    report_mock.assert_awaited_once_with(
        fake_context,
        chat_id,
        battle,
        victory=True,
    )


@pytest.mark.asyncio
async def test_boss_finish_victory_continues_after_reward_failure(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    chat_id = -712
    timer = FakeTask("phase_timer")
    battle = make_battle(
        [make_participant(1), make_participant(2)],
        phase="resolving",
        phase_task=timer,
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    attempted = []

    def reward(*, user_id, chat_id):
        assert chat_id not in duel.ACTIVE_BOSS_BATTLES
        attempted.append(user_id)
        if user_id == 1:
            raise RuntimeError("first reward failed")
        return True

    async def report(context, report_chat_id, report_battle, *, victory):
        assert report_chat_id not in duel.ACTIVE_BOSS_BATTLES
        assert victory is True

    report_mock = AsyncMock(side_effect=report)
    monkeypatch.setattr(duel, "reward_boss_victory", Mock(side_effect=reward))
    monkeypatch.setattr(duel, "_boss_send_final_report", report_mock)

    await duel._boss_finish_victory(fake_context, chat_id)

    assert attempted == [1, 2]
    assert chat_id not in duel.ACTIVE_BOSS_BATTLES
    assert timer.cancel_calls == 1
    report_mock.assert_awaited_once_with(
        fake_context,
        chat_id,
        battle,
        victory=True,
    )


@pytest.mark.asyncio
async def test_boss_finish_defeat_cleans_state_without_rewards_even_if_report_fails(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    chat_id = -713
    timer = FakeTask("phase_timer")
    battle = make_battle(
        [make_participant(1, alive=False)],
        phase="resolving",
        phase_task=timer,
    )
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    reward = Mock(side_effect=AssertionError("defeat issued a reward"))

    async def failing_report(context, report_chat_id, report_battle, *, victory):
        assert report_chat_id not in duel.ACTIVE_BOSS_BATTLES
        assert report_battle is battle
        assert victory is False
        raise RuntimeError("report failed")

    report = AsyncMock(side_effect=failing_report)
    monkeypatch.setattr(duel, "reward_boss_victory", reward)
    monkeypatch.setattr(duel, "_boss_send_final_report", report)

    await duel._boss_finish_defeat(fake_context, chat_id)

    assert chat_id not in duel.ACTIVE_BOSS_BATTLES
    assert timer.cancel_calls == 1
    reward.assert_not_called()
    report.assert_awaited_once_with(
        fake_context,
        chat_id,
        battle,
        victory=False,
    )
