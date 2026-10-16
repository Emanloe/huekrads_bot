"""Exercise Telegram button ownership against real duel storage."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import BadRequest

import database
from duel_session_repository import get_current_duel_session, get_duel_session
from handlers import duel, duel_service
from handlers.persistent_duel_publisher import recover_persistent_duel_chat
from text_resources import get_text


CHAT = -98221


def user(uid, name):
    return SimpleNamespace(id=uid, username=name, first_name=name, last_name=None, is_bot=False)


def callback(actor, message, data="start_duel_opponent", chat_id=CHAT):
    query = SimpleNamespace(from_user=actor, message=message, data=data, answer=AsyncMock())
    return SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=chat_id))


def snapshot():
    with database.get_db() as conn:
        return tuple(conn.iterdump())


@pytest.fixture
def players(temp_database):
    actors = user(1, "owner"), user(2, "opponent"), user(3, "outsider")
    for actor in actors:
        database.get_or_create_duel_user(actor, CHAT)
    database.add_duel_inventory_item(CHAT, 1, "vevangel_wing")
    database.add_duel_inventory_item(CHAT, 2, "formangnome_whisker")
    return actors


async def start(context, owner, open_duel_selection):
    message = await open_duel_selection(context, owner, CHAT)
    await duel.duel_select_callback(callback(owner, message), context)
    return get_current_duel_session(CHAT)


async def test_foreign_click_preserves_menu_database_and_author_choice(
    players, fake_context, open_duel_selection, monkeypatch,
):
    owner, opponent, outsider = players
    message = await open_duel_selection(fake_context, owner, CHAT)
    before, pending = snapshot(), deepcopy(fake_context.bot_data)
    jobs = fake_context.job_queue.calls[:]
    rng = Mock(side_effect=AssertionError("unauthorized RNG"))
    monkeypatch.setattr(duel_service.random, "choice", rng)
    for actor in (opponent, outsider, user(4, "unregistered")):
        update = callback(actor, message)
        await duel.duel_select_callback(update, fake_context)
        update.callback_query.answer.assert_awaited_once_with(get_text("duel.selection.wrong_actor"))
    assert snapshot() == before
    assert fake_context.bot_data == pending
    assert fake_context.job_queue.calls == jobs
    message.delete.assert_not_awaited()
    fake_context.bot.send_message.assert_not_awaited()
    fake_context.bot.edit_message_text.assert_not_awaited()
    rng.assert_not_called()

    monkeypatch.setattr(duel_service.random, "choice", lambda values: values[0])
    await duel.duel_select_callback(callback(owner, message), fake_context)
    session = get_current_duel_session(CHAT)
    assert (session["player1_user_id"], session["player2_user_id"]) == (owner.id, opponent.id)


@pytest.mark.parametrize("actors", [(1, 1), (3, 1), (1, 3)])
async def test_simultaneous_and_repeated_clicks_start_only_once(
    players, fake_context, open_duel_selection, monkeypatch, actors,
):
    owner, _, _ = players
    message = await open_duel_selection(fake_context, owner, CHAT)
    choose = Mock(side_effect=lambda values: values[0])
    monkeypatch.setattr(duel_service.random, "choice", choose)
    queries = [callback(players[uid - 1], message) for uid in actors]
    await asyncio.gather(*(duel.duel_select_callback(q, fake_context) for q in queries))
    session = get_current_duel_session(CHAT)
    await duel.duel_select_callback(callback(owner, message), fake_context)
    assert get_current_duel_session(CHAT)["id"] == session["id"]
    assert (session["player1_user_id"], session["player2_user_id"]) == (1, 2)
    message.delete.assert_awaited_once()
    choose.assert_called_once_with([True, False])


@pytest.mark.parametrize("invalid", [
    "chat", "message", "expired", "restart", "replaced", "missing_message",
    "inaccessible", "deleted", "tampered", "forged_owner", "unoffered",
    "username_reassigned", "ineligible_opponent", "deleted_owner",
])
async def test_stale_and_forged_selection_cannot_start_or_change_game_data(
    players, fake_context, open_duel_selection, monkeypatch, invalid,
):
    owner, _, _ = players
    message = await open_duel_selection(fake_context, owner, CHAT)
    update = callback(owner, message)
    if invalid == "chat":
        update.effective_chat.id -= 1
    elif invalid == "message":
        update.callback_query.message = SimpleNamespace(message_id=999, delete=AsyncMock())
    elif invalid == "expired":
        fake_context.bot_data["duel_selections"][(CHAT, message.message_id)]["expires_at"] = 0
    elif invalid == "restart":
        fake_context.bot_data.clear()
    elif invalid == "replaced":
        await open_duel_selection(fake_context, owner, CHAT)
    elif invalid == "missing_message":
        update.callback_query.message = None
    elif invalid == "inaccessible":
        message.is_accessible = False
    elif invalid == "deleted":
        message.delete.side_effect = BadRequest("Message to delete not found")
    elif invalid == "tampered":
        update.callback_query.data = "start_duel_owner"
    elif invalid == "forged_owner":
        update.callback_query.from_user = players[2]
        update.callback_query.data = "start_duel_3_opponent"
    elif invalid == "unoffered":
        database.get_or_create_duel_user(user(4, "new_opponent"), CHAT)
        update.callback_query.data = "start_duel_new_opponent"
    elif invalid == "username_reassigned":
        with database.get_db() as conn:
            conn.execute("UPDATE duel_users SET username = 'renamed' WHERE user_id = 2")
            conn.execute("UPDATE duel_users SET username = 'opponent' WHERE user_id = 3")
    elif invalid == "ineligible_opponent":
        with database.get_db() as conn:
            conn.execute("UPDATE duel_users SET dick_stolen_today = 1 WHERE user_id = 2")
    elif invalid == "deleted_owner":
        database.delete_gnome(owner.id, owner.username)
    before = snapshot()
    jobs = fake_context.job_queue.calls[:]
    monkeypatch.setattr(duel_service.random, "choice", Mock(side_effect=AssertionError("RNG")))
    await duel.duel_select_callback(update, fake_context)
    assert get_current_duel_session(CHAT) is None
    assert snapshot() == before
    assert fake_context.job_queue.calls == jobs
    fake_context.bot.send_message.assert_not_awaited()
    fake_context.bot.edit_message_text.assert_not_awaited()
    update.callback_query.answer.assert_awaited_once()
    if invalid != "deleted":
        message.delete.assert_not_awaited()


@pytest.mark.parametrize("when", ["before_click", "during_delete"])
async def test_other_duel_invalidates_menu_even_after_that_duel_finishes(
    players, fake_context, open_duel_selection, monkeypatch, when,
):
    owner, opponent, _ = players
    monkeypatch.setattr(duel_service.random, "choice", lambda values: values[0])
    monkeypatch.setattr(duel_service.random, "random", lambda: 0.5)
    message = await open_duel_selection(fake_context, owner, CHAT)

    async def play_other_duel():
        session = duel_service.start_persistent_duel(CHAT, owner.id, opponent.id).session
        await recover_persistent_duel_chat(CHAT, fake_context.bot)
        duel_service.submit_persistent_duel_attack(CHAT, session["id"], owner.id, 1, "head")
        await recover_persistent_duel_chat(CHAT, fake_context.bot)
        duel_service.submit_persistent_duel_block(CHAT, session["id"], opponent.id, 2, "body")
        await recover_persistent_duel_chat(CHAT, fake_context.bot)
        assert get_duel_session(CHAT, session["id"])["status"] == "finished"

    if when == "before_click":
        await play_other_duel()
    else:
        message.delete.side_effect = play_other_duel
    await duel.duel_select_callback(callback(owner, message), fake_context)
    with database.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_sessions").fetchone()[0] == 1
    assert get_current_duel_session(CHAT) is None


@pytest.mark.parametrize("phase", ["strike", "block"])
@pytest.mark.parametrize("invalid", ["outsider", "other_participant", "message", "chat", "turn", "duel", "missing_message"])
async def test_round_buttons_reject_foreign_and_stale_actions_without_side_effects(
    players, fake_context, open_duel_selection, monkeypatch, phase, invalid,
):
    owner, opponent, outsider = players
    monkeypatch.setattr(duel_service.random, "choice", lambda values: values[0])
    session = await start(fake_context, owner, open_duel_selection)
    duel_id = session["id"]
    if phase == "block":
        duel_service.submit_persistent_duel_attack(CHAT, duel_id, owner.id, 1, "head")
        await recover_persistent_duel_chat(CHAT, fake_context.bot)
        session = get_duel_session(CHAT, duel_id)
    actor = owner if phase == "strike" else opponent
    if invalid == "outsider":
        actor = outsider
    elif invalid == "other_participant":
        actor = opponent if phase == "strike" else owner
    message = SimpleNamespace(message_id=session["message_id"], delete=AsyncMock())
    update = callback(actor, message, f"duel_{phase}_head_{duel_id}_{session['turn_id']}")
    if invalid == "message":
        message.message_id += 1
    elif invalid == "chat":
        update.effective_chat.id -= 1
    elif invalid == "turn":
        update.callback_query.data = f"duel_{phase}_head_{duel_id}_{session['turn_id'] + 1}"
    elif invalid == "duel":
        update.callback_query.data = f"duel_{phase}_head_{duel_id + 1}_{session['turn_id']}"
    elif invalid == "missing_message":
        update.callback_query.message = None
    before, jobs = snapshot(), fake_context.job_queue.calls[:]
    fake_context.bot.send_message.reset_mock()
    fake_context.bot.edit_message_text.reset_mock()
    monkeypatch.setattr(duel_service.random, "random", Mock(side_effect=AssertionError("RNG")))
    monkeypatch.setattr(duel_service.random, "choice", Mock(side_effect=AssertionError("RNG")))
    await duel.persistent_duel_action_callback(update, fake_context)
    assert snapshot() == before
    assert get_duel_session(CHAT, duel_id) == session
    assert fake_context.job_queue.calls == jobs
    fake_context.bot.send_message.assert_not_awaited()
    fake_context.bot.edit_message_text.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert update.callback_query.answer.await_args.kwargs == {"show_alert": True}


async def test_rejected_buttons_preserve_timeouts_rounds_and_participants(
    players, fake_context, open_duel_selection, monkeypatch,
):
    owner, opponent, outsider = players
    monkeypatch.setattr(duel_service.random, "choice", lambda values: values[0])
    monkeypatch.setattr(duel_service.random, "random", lambda: 0.5)
    session = await start(fake_context, owner, open_duel_selection)
    duel_id = session["id"]
    for action in ("strike", "block"):
        message = SimpleNamespace(message_id=session["message_id"])
        await duel.persistent_duel_action_callback(
            callback(outsider, message, f"duel_{action}_head_{duel_id}_{session['turn_id']}"),
            fake_context,
        )
        assert get_duel_session(CHAT, duel_id) == session
        await recover_persistent_duel_chat(CHAT, fake_context.bot, now_ms=session["deadline_at"])
        session = get_duel_session(CHAT, duel_id)
        assert (session["player1_user_id"], session["player2_user_id"]) == (owner.id, opponent.id)
    assert session["round_no"] == 2
    assert session["phase"] == "attack"
    assert session["attacker_user_id"] == opponent.id
    assert session["defender_user_id"] == owner.id
