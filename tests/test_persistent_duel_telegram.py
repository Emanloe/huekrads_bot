"""Production Telegram adapter and recovery integration."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database
from duel_outbox_repository import claim_duel_publication, get_duel_publication, list_retryable_duel_publications
from duel_session_repository import get_current_duel_session, get_duel_session, utc_unix_milliseconds
from handlers import duel, duel_service
from handlers import persistent_duel_publisher as publisher
from handlers.persistent_duel_publisher import recover_persistent_duel_chat, recover_persistent_duels


CHAT = -9701


def user(uid, name):
    return SimpleNamespace(id=uid, username=name, first_name=name, last_name=None, is_bot=False)


def callback(data, actor, chat_id=CHAT):
    query = SimpleNamespace(data=data, from_user=actor, answer=AsyncMock(),
                            message=SimpleNamespace(delete=AsyncMock()))
    return SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=chat_id)), query


def register(chat_id=CHAT):
    users = (user(1, "attacker"), user(2, "defender"))
    for actor in users:
        database.get_or_create_duel_user(actor, chat_id)
    return users


@pytest.mark.asyncio
async def test_telegram_selection_actions_final_and_stale_buttons(temp_database, fake_context, monkeypatch):
    attacker, defender = register()
    monkeypatch.setattr(duel_service.random, "choice", lambda values: values[0])
    monkeypatch.setattr(duel_service.random, "random", lambda: 0.5)
    selection, query = callback("start_duel_defender", attacker)
    await duel.duel_select_callback(selection, fake_context)
    query.answer.assert_awaited_once_with()
    session = get_current_duel_session(CHAT)
    assert session["status"] == "active" and session["phase"] == "attack"
    assert not duel.ACTIVE_DUELS
    duel_id = session["id"]
    markup = fake_context.bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == f"duel_strike_head_{duel_id}_1"

    old, old_query = callback("duel_strike_head_1", attacker)
    await duel.persistent_duel_action_callback(old, fake_context)
    assert old_query.answer.await_args.kwargs == {"show_alert": True}
    assert get_duel_session(CHAT, duel_id)["phase"] == "attack"

    strike, strike_query = callback(f"duel_strike_head_{duel_id}_1", attacker)
    await duel.persistent_duel_action_callback(strike, fake_context)
    strike_query.answer.assert_awaited_once_with()
    assert get_duel_session(CHAT, duel_id)["phase"] == "block"
    block, block_query = callback(f"duel_block_body_{duel_id}_2", defender)
    await duel.persistent_duel_action_callback(block, fake_context)
    block_query.answer.assert_awaited_once_with()
    finished = get_duel_session(CHAT, duel_id)
    assert finished["status"] == "finished"
    assert finished["result"]["kind"] == "finalized"
    assert finished["pocket_done_at"] is not None
    assert not duel.ACTIVE_DUELS
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key())["duels"] == 1
    await duel.persistent_duel_action_callback(block, fake_context)
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key())["duels"] == 1


@pytest.mark.parametrize("entry", ("direct", "selection"))
@pytest.mark.parametrize("zero_player", ("initiator", "opponent"))
@pytest.mark.asyncio
async def test_telegram_entry_allows_zero_point_player_and_uses_persistent_session(
    temp_database, fake_context, monkeypatch, entry, zero_player,
):
    attacker, defender = register()
    zero_id = attacker.id if zero_player == "initiator" else defender.id
    with database.get_db() as conn:
        conn.execute("UPDATE duel_users SET points = 0 WHERE chat_id = ? AND user_id = ?",
                     (CHAT, zero_id))
    monkeypatch.setattr(duel_service.random, "choice", lambda values: values[0])
    monkeypatch.setattr(duel_service.random, "random",
                        lambda: (_ for _ in ()).throw(AssertionError("start used random.random")))

    if entry == "direct":
        message = SimpleNamespace(
            from_user=attacker, chat=SimpleNamespace(id=CHAT), chat_id=CHAT,
            message_id=91, text="/duel @defender",
        )
        fake_context.args = ["@defender"]
        await duel.duel_command(SimpleNamespace(message=message), fake_context)
    else:
        selection, query = callback("start_duel_defender", attacker)
        await duel.duel_select_callback(selection, fake_context)
        query.answer.assert_awaited_once_with()

    session = get_current_duel_session(CHAT)
    assert session["status"] == "active"
    assert session["player1_snapshot"]["points"] == (0 if zero_player == "initiator" else 20)
    assert session["player2_snapshot"]["points"] == (0 if zero_player == "opponent" else 20)
    assert not duel.ACTIVE_DUELS


@pytest.mark.asyncio
async def test_restart_recovers_committed_start_and_terminal_without_reroll(
    temp_database, fake_context, monkeypatch,
):
    attacker, defender = register()
    calls = []

    def choose(values):
        calls.append("choice")
        return values[0]

    def roll():
        calls.append("random")
        return 0.5

    monkeypatch.setattr(duel_service.random, "choice", choose)
    monkeypatch.setattr(duel_service.random, "random", roll)
    started = duel_service.start_persistent_duel(CHAT, attacker.id, defender.id)
    duel_id = started.session["id"]
    assert get_duel_session(CHAT, duel_id)["status"] == "publishing"
    await recover_persistent_duels(fake_context.bot)
    assert get_duel_session(CHAT, duel_id)["status"] == "active"
    start_trace = calls[:]
    await recover_persistent_duels(fake_context.bot)
    assert calls == start_trace

    state = get_duel_session(CHAT, duel_id)
    duel_service.submit_persistent_duel_attack(CHAT, duel_id, attacker.id,
                                               state["turn_id"], "head")
    await recover_persistent_duels(fake_context.bot)
    state = get_duel_session(CHAT, duel_id)
    assert state["phase"] == "block" and state["status"] == "active"
    duel_service.submit_persistent_duel_block(CHAT, duel_id, defender.id,
                                              state["turn_id"], "body")
    checkpoint = get_duel_session(CHAT, duel_id)
    assert checkpoint["result"]["kind"] == "terminal_resolution"
    await recover_persistent_duels(fake_context.bot)
    finished = get_duel_session(CHAT, duel_id)
    assert finished["status"] == "finished"
    assert finished["pocket_done_at"] is not None
    stats = database.get_monthly_chat_stats(CHAT, database.moscow_month_key())
    trace = calls[:]
    await recover_persistent_duels(fake_context.bot)
    assert calls == trace
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key()) == stats


@pytest.mark.asyncio
async def test_restart_recovers_overdue_turn_and_isolates_chats(temp_database, fake_context, monkeypatch):
    attacker, defender = register()
    register(CHAT - 1)
    monkeypatch.setattr(duel_service.random, "choice", lambda values: values[0])
    monkeypatch.setattr(duel_service.random, "random", lambda: 0.5)
    one = duel_service.start_persistent_duel(CHAT, attacker.id, defender.id).session
    two = duel_service.start_persistent_duel(CHAT - 1, attacker.id, defender.id).session
    await recover_persistent_duels(fake_context.bot)
    one = get_duel_session(CHAT, one["id"])
    two = get_duel_session(CHAT - 1, two["id"])
    assert one["status"] == two["status"] == "active"
    from handlers.persistent_duel_publisher import recover_persistent_duel_chat
    await recover_persistent_duel_chat(CHAT, fake_context.bot, now_ms=one["deadline_at"])
    assert get_duel_session(CHAT, one["id"])["phase"] == "block"
    assert get_duel_session(CHAT - 1, two["id"])["phase"] == "attack"
    foreign, answer = callback(f"duel_strike_head_{one['id']}_1", attacker, CHAT - 1)
    await duel.persistent_duel_action_callback(foreign, fake_context)
    assert answer.answer.await_args.kwargs == {"show_alert": True}
    assert get_duel_session(CHAT - 1, two["id"])["phase"] == "attack"


@pytest.mark.parametrize("loser_points", (20, 0))
@pytest.mark.asyncio
async def test_production_telegram_rng_order_and_duplicate_is_rng_free(
    temp_database, fake_context, monkeypatch, loser_points,
):
    attacker, defender = register()
    with database.get_db() as conn:
        conn.execute("UPDATE duel_users SET points = ? WHERE chat_id = ? AND user_id = ?",
                     (loser_points, CHAT, defender.id))
    database.add_duel_inventory_item(CHAT, defender.id, "vevangel_wing")
    database.add_duel_inventory_item(CHAT, defender.id, "formangnome_whisker")

    class Rng:
        def __init__(self):
            self.values = iter((0.5, 0.5, 0.0, 0.0, 1.0, 1.0, 0.0)
                               if loser_points else (0.5, 0.5, 0.0, 1.0, 1.0, 0.0))
            self.trace = []

        def random(self):
            self.trace.append("random")
            return next(self.values)

        def choice(self, values):
            self.trace.append("choice")
            return values[0]

    rng = Rng()
    monkeypatch.setattr(duel_service, "random", rng)
    selection, _ = callback("start_duel_defender", attacker)
    await duel.duel_select_callback(selection, fake_context)
    duel_id = get_current_duel_session(CHAT)["id"]
    assert rng.trace == ["choice"]
    strike, _ = callback(f"duel_strike_head_{duel_id}_1", attacker)
    await duel.persistent_duel_action_callback(strike, fake_context)
    assert rng.trace == ["choice"]
    block, _ = callback(f"duel_block_body_{duel_id}_2", defender)
    await duel.persistent_duel_action_callback(block, fake_context)
    expected = [
        "choice",                    # initial attacker
        "random", "random",         # suicide, miss
        "choice", "choice",         # hit phrase, attack phrase
        "random", "random", "choice", # dick and item steal
        "choice", "choice",         # round flavor, dwarf fact
        "random", "random",         # berserk, post-message
        "random", "choice",         # pocket after final publication
    ]
    if loser_points == 0:
        expected.pop(5)  # Guaranteed steal skips only the dick decision roll.
    assert rng.trace == expected
    final = get_duel_session(CHAT, duel_id)
    assert final["pocket_done_at"] is not None
    assert final["result"]["is_dick_stolen"] is True
    assert final["result"]["loser_points"] == max(0, loser_points - 5)
    assert final["result"]["stolen_item"] is not None
    trace = rng.trace[:]
    await duel.persistent_duel_action_callback(block, fake_context)
    await recover_persistent_duels(fake_context.bot)
    assert rng.trace == trace
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key())["duels"] == 1


@pytest.mark.asyncio
async def test_deleted_prompt_falls_back_and_original_command_is_cleaned(
    temp_database, fake_context, monkeypatch,
):
    attacker, defender = register()
    monkeypatch.setattr(duel_service.random, "choice", lambda values: values[0])
    await duel._process_persistent_duel_fight(fake_context, attacker, defender.username,
                                               CHAT, original_msg_id=77)
    session = get_current_duel_session(CHAT)
    fake_context.bot.delete_message.assert_any_await(chat_id=CHAT, message_id=77)
    fake_context.bot.edit_message_text.side_effect = RuntimeError("message deleted")
    fake_context.bot.send_message.return_value = SimpleNamespace(message_id=202)
    action, _ = callback(f"duel_strike_head_{session['id']}_1", attacker)
    await duel.persistent_duel_action_callback(action, fake_context)
    state = get_duel_session(CHAT, session["id"])
    assert state["status"] == "active" and state["phase"] == "block"
    assert state["message_id"] == 202


@pytest.mark.asyncio
async def test_expired_outbox_lease_recovers_without_new_start_rng(
    temp_database, fake_context, monkeypatch,
):
    attacker, defender = register()
    choices = []

    def choose(values):
        choices.append(1)
        return values[0]

    monkeypatch.setattr(duel_service.random, "choice", choose)
    session = duel_service.start_persistent_duel(CHAT, attacker.id, defender.id).session
    now = utc_unix_milliseconds()
    publication = list_retryable_duel_publications(CHAT, now)[0]
    leased = claim_duel_publication(CHAT, publication["id"], now_ms=now, lease_ms=100)
    await recover_persistent_duel_chat(CHAT, fake_context.bot, now_ms=now + 99)
    assert get_duel_session(CHAT, session["id"])["status"] == "publishing"
    await recover_persistent_duel_chat(CHAT, fake_context.bot, now_ms=now + 100)
    assert get_duel_session(CHAT, session["id"])["status"] == "active"
    assert get_duel_publication(CHAT, publication["id"])["status"] == "delivered"
    assert choices == [1]


@pytest.mark.asyncio
async def test_double_start_and_double_clicks_use_one_transition(
    temp_database, fake_context, monkeypatch,
):
    attacker, defender = register()
    choices = []

    def choose(values):
        choices.append(1)
        return values[0]

    monkeypatch.setattr(duel_service.random, "choice", choose)
    monkeypatch.setattr(duel_service.random, "random", lambda: 0.5)
    first, _ = callback("start_duel_defender", attacker)
    second, _ = callback("start_duel_defender", attacker)
    await duel.duel_select_callback(first, fake_context)
    session = get_current_duel_session(CHAT)
    await duel.duel_select_callback(second, fake_context)
    assert get_current_duel_session(CHAT)["id"] == session["id"]
    assert choices == [1]
    action = f"duel_strike_head_{session['id']}_1"
    a1, q1 = callback(action, attacker)
    a2, q2 = callback(action, attacker)
    await asyncio.gather(duel.persistent_duel_action_callback(a1, fake_context),
                         duel.persistent_duel_action_callback(a2, fake_context))
    assert sum(q.answer.await_args.kwargs == {} for q in (q1, q2)) == 1
    assert get_duel_session(CHAT, session["id"])["phase"] == "block"
    action = f"duel_block_body_{session['id']}_2"
    b1, q1 = callback(action, defender)
    b2, q2 = callback(action, defender)
    await asyncio.gather(duel.persistent_duel_action_callback(b1, fake_context),
                         duel.persistent_duel_action_callback(b2, fake_context))
    assert sum(q.answer.await_args.kwargs == {} for q in (q1, q2)) == 1
    assert get_duel_session(CHAT, session["id"])["status"] == "finished"
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key())["duels"] == 1


@pytest.mark.asyncio
async def test_restart_after_final_ack_and_after_pocket_commit(
    temp_database, fake_context, monkeypatch,
):
    attacker, defender = register()
    database.add_duel_inventory_item(CHAT, defender.id, "vevangel_wing")
    monkeypatch.setattr(duel_service.random, "choice", lambda values: values[0])
    monkeypatch.setattr(duel_service.random, "random", lambda: 0.5)
    monkeypatch.setattr(duel_service, "DUEL_ITEM_DROP_CHANCE", 1.0)
    session = duel_service.start_persistent_duel(CHAT, attacker.id, defender.id).session
    duel_id = session["id"]
    await recover_persistent_duels(fake_context.bot)
    duel_service.submit_persistent_duel_attack(CHAT, duel_id, attacker.id, 1, "head")
    await recover_persistent_duels(fake_context.bot)
    duel_service.submit_persistent_duel_block(CHAT, duel_id, defender.id, 2, "body")

    real_pocket = publisher.process_persistent_duel_pocket_drop
    monkeypatch.setattr(publisher, "process_persistent_duel_pocket_drop",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("restart")))
    await recover_persistent_duels(fake_context.bot)
    state = get_duel_session(CHAT, duel_id)
    assert state["status"] == "finished" and state["pocket_done_at"] is None
    stats = database.get_monthly_chat_stats(CHAT, database.moscow_month_key())
    final_send_count = fake_context.bot.send_message.await_count
    monkeypatch.setattr(publisher, "process_persistent_duel_pocket_drop", real_pocket)

    fake_context.bot.send_message.side_effect = RuntimeError("drop send failed")
    await recover_persistent_duels(fake_context.bot)
    assert get_duel_session(CHAT, duel_id)["pocket_done_at"] is not None
    pending = list_retryable_duel_publications(CHAT, utc_unix_milliseconds())
    assert len(pending) == 1 and pending[0]["kind"] == "pocket_drop"
    fake_context.bot.send_message.side_effect = None
    await recover_persistent_duels(fake_context.bot)
    assert get_duel_publication(CHAT, pending[0]["id"])["status"] == "delivered"
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key()) == stats
    assert fake_context.bot.send_message.await_count >= final_send_count + 2


@pytest.mark.asyncio
async def test_restart_after_finalize_and_after_final_send_before_ack(
    temp_database, fake_context, monkeypatch,
):
    attacker, defender = register()
    monkeypatch.setattr(duel_service.random, "choice", lambda values: values[0])
    monkeypatch.setattr(duel_service.random, "random", lambda: 0.5)
    duel_id = duel_service.start_persistent_duel(CHAT, attacker.id, defender.id).session["id"]
    await recover_persistent_duels(fake_context.bot)
    duel_service.submit_persistent_duel_attack(CHAT, duel_id, attacker.id, 1, "head")
    await recover_persistent_duels(fake_context.bot)
    duel_service.submit_persistent_duel_block(CHAT, duel_id, defender.id, 2, "body")
    finalized = duel_service.finalize_persistent_duel(CHAT, duel_id)
    assert finalized.reason == "finalized"
    assert get_duel_session(CHAT, duel_id)["pocket_done_at"] is None
    final_publication = list_retryable_duel_publications(CHAT, utc_unix_milliseconds())[0]
    assert final_publication["kind"] == "final_result"

    real_ack = publisher.acknowledge_persistent_duel_publication
    monkeypatch.setattr(publisher, "acknowledge_persistent_duel_publication",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("restart")))
    await recover_persistent_duels(fake_context.bot)
    leased = get_duel_publication(CHAT, final_publication["id"])
    assert leased["status"] == "leased"
    assert get_duel_session(CHAT, duel_id)["pocket_done_at"] is None
    monkeypatch.setattr(publisher, "acknowledge_persistent_duel_publication", real_ack)
    await recover_persistent_duel_chat(CHAT, fake_context.bot,
                                       now_ms=leased["lease_until"])
    assert get_duel_publication(CHAT, final_publication["id"])["status"] == "delivered"
    assert get_duel_session(CHAT, duel_id)["pocket_done_at"] is not None
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key())["duels"] == 1
