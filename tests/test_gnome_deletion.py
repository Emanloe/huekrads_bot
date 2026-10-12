"""Voluntary global deletion and the author/admin boundaries."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def _user(user_id=1, username="alice"):
    return SimpleNamespace(id=user_id, username=username, first_name="Alice",
                           last_name=None, full_name="Alice", is_bot=False)


def _command(user, *, chat_id=-100, args=None):
    sent = SimpleNamespace(message_id=101)
    message = SimpleNamespace(chat_id=chat_id, reply_text=AsyncMock(return_value=sent))
    update = SimpleNamespace(message=message, effective_user=user)
    context = SimpleNamespace(args=args or [], bot=SimpleNamespace(
        get_chat=AsyncMock(return_value=SimpleNamespace(
            id=1, type="private", username="alice")),
    ))
    return update, context, message


def _click(data, user, *, chat_id=-100, message_id=101):
    query = SimpleNamespace(data=data, from_user=user,
                            message=SimpleNamespace(message_id=message_id),
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(callback_query=query,
                           effective_chat=SimpleNamespace(id=chat_id)), query


@pytest.mark.asyncio
async def test_two_stage_confirmation_is_author_only_and_no_is_noop(temp_database):
    from database import get_or_create_duel_user, is_deleted_user
    from handlers import gnome_deletion as deletion

    deletion._confirmations.clear()
    get_or_create_duel_user(_user(), -100)
    update, context, message = _command(_user())
    await deletion.dickpukku_command(update, context)
    first = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert first.text == "ВЫ УВЕРЕНЫ???"
    assert not is_deleted_user(1)

    foreign, foreign_query = _click(first.callback_data, _user(2, "other"))
    await deletion.dickpukku_callback(foreign, context)
    foreign_query.answer.assert_awaited_once()
    foreign_query.edit_message_text.assert_not_awaited()
    assert not is_deleted_user(1)

    owner, owner_query = _click(first.callback_data, _user())
    await deletion.dickpukku_callback(owner, context)
    second = owner_query.edit_message_text.await_args.kwargs["reply_markup"]
    assert owner_query.edit_message_text.await_args.args[0] == "ТЫ УВЕРЕН???"
    assert [button.text for button in second.inline_keyboard[0]] == ["Да", "Нет"]
    assert not is_deleted_user(1)

    yes, no = [button.callback_data for button in second.inline_keyboard[0]]
    for data in (yes, no):
        other, other_query = _click(data, _user(2, "other"))
        await deletion.dickpukku_callback(other, context)
        other_query.edit_message_text.assert_not_awaited()
        assert not is_deleted_user(1)
    no_update, no_query = _click(no, _user())
    await deletion.dickpukku_callback(no_update, context)
    no_query.answer.assert_awaited_once_with()
    no_query.edit_message_text.assert_not_awaited()
    assert not is_deleted_user(1)

    from handlers.duel import ACTIVE_BOSS_BATTLES
    battle = {"lock": asyncio.Lock(), "participants": {1: object(), 2: object()}}
    ACTIVE_BOSS_BATTLES[-100] = battle
    yes_update, yes_query = _click(yes, _user())
    await deletion.dickpukku_callback(yes_update, context)
    yes_query.edit_message_text.assert_awaited_once_with(
        "alice воняет слабостью", reply_markup=None,
    )
    assert is_deleted_user(1)
    assert set(battle["participants"]) == {2}
    ACTIVE_BOSS_BATTLES.pop(-100, None)
    repeated, repeated_query = _click(yes, _user())
    await deletion.dickpukku_callback(repeated, context)
    repeated_query.edit_message_text.assert_not_awaited()


def test_deletion_purges_all_chats_and_blocks_autocreate(temp_database):
    import database as db
    from elite_ball_store import activate_ball

    user = _user()
    for chat_id in (-100, -200):
        db.save_or_update_user(user, chat_id)
        db.add_duel_inventory_item(chat_id, user.id, "knife")
        activate_ball(chat_id, user.id)
    with db.get_db() as conn:
        conn.execute("INSERT INTO huecrab_owners (chat_id, user_id) VALUES (-100, 1)")
    assert db.delete_gnome(1, "@alice")
    assert not db.delete_gnome(1, "alice")
    db.save_or_update_user(user, -100)
    with pytest.raises(db.DeletedGnomeError):
        db.get_or_create_duel_user(user, -100)
    db.init_db()
    with sqlite3.connect(temp_database) as conn:
        row = conn.execute("SELECT user_id, username, deleted_at FROM deleted_users").fetchone()
        assert row[0:2] == (1, "alice") and row[2]
        for table, column in (("users", "user_id"), ("duel_users", "user_id"),
                              ("duel_inventory", "user_id"), ("huecrab_owners", "user_id"),
                              ("elite_ball_activations", "user_id")):
            assert conn.execute(f"SELECT 1 FROM {table} WHERE {column} = 1").fetchone() is None


@pytest.mark.asyncio
async def test_return_requires_any_configured_admin_and_creates_fresh_profile(
    temp_database, monkeypatch,
):
    import config
    import database as db
    from handlers import gnome_deletion as deletion

    monkeypatch.setattr(config, "ADMIN_IDS", {11, 22})
    db.save_or_update_user(_user(), -100)
    with db.get_db() as conn:
        conn.execute("UPDATE duel_users SET points = 777, wins = 9 WHERE user_id = 1")
    db.delete_gnome(1, "alice")
    update, context, message = _command(_user(33, "ordinary"), args=["@alice"])
    await deletion.return_gnome_command(update, context)
    assert message.reply_text.await_args.args[0] == "Нет прав."
    assert db.is_deleted_user(1)
    for admin_id in (11, 22):
        if admin_id == 22:
            db.save_or_update_user(_user(), -100)
            db.delete_gnome(1, "alice")
        update, context, message = _command(_user(admin_id, "admin"), args=["@ALICE"])
        await deletion.return_gnome_command(update, context)
        assert message.reply_text.await_args.args[0] == "Гному снова разрешено войти в игру."
        assert not db.is_deleted_user(1)
        fresh = db.get_or_create_duel_user(_user(1, "current_name"), -100)
        assert fresh["points"] == 20 and fresh["wins"] == 0
        assert fresh["username"] == "current_name"


def test_deleted_click_does_not_claim_loot_crab_or_moss(temp_database):
    import database as db
    from handlers.moss_choice_event import _claim_event

    db.get_or_create_duel_user(_user(), -100)
    db.get_or_create_duel_user(_user(2, "other"), -100)
    with db.get_db() as conn:
        event_id = conn.execute(
            "INSERT INTO duel_item_events (chat_id, message_id) VALUES (-100, 101)"
        ).lastrowid
        crab_id = conn.execute(
            "INSERT INTO huecrab_events (chat_id, message_id) VALUES (-100, 102)"
        ).lastrowid
        moss_id = conn.execute(
            """INSERT INTO moss_choice_events (chat_id, event_date, message_id)
               VALUES (-100, '2026-10-06', 103)"""
        ).lastrowid
    db.delete_gnome(1, "alice")
    fail_rng = Mock(side_effect=AssertionError("deleted click used RNG"))
    assert db.claim_duel_item_event(event_id, -100, 1, fail_rng)[0] == "not_registered"
    assert db.tame_huecrab_event(crab_id, -100, 1, 102, fail_rng) == "not_registered"
    assert _claim_event(moss_id, -100, 103, 1, "clever")[0] == "not_registered"
    fail_rng.assert_not_called()
    assert db.claim_duel_item_event(event_id, -100, 2, lambda: "knife")[0] == "claimed"
    assert db.tame_huecrab_event(crab_id, -100, 2, 102, lambda: True) == "tamed"
    assert _claim_event(moss_id, -100, 103, 2, "wise")[0] == "claimed"


def test_boss_queue_and_delayed_reward_exclude_deleted_user(temp_database, monkeypatch):
    import database as db
    from handlers import boss_registration

    monkeypatch.setattr(boss_registration, "_BOSS_REG_DB_PATH", temp_database)
    db.get_or_create_duel_user(_user(), -100)
    db.get_or_create_duel_user(_user(2, "other"), -100)
    assert boss_registration._boss_register_user(-100, _user())
    assert boss_registration._boss_register_user(-100, _user(2, "other"))
    db.delete_gnome(1, "alice")
    assert not boss_registration._boss_register_user(-100, _user())
    assert [row[0] for row in boss_registration._boss_consume_registrations(-100)] == [2]
    assert not db.reward_boss_victory(1, -100)
    assert db.reward_boss_victory(2, -100)
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute(
            "SELECT 1 FROM boss_next_registrations WHERE user_id = 1"
        ).fetchone() is None


def test_return_keeps_old_inline_token_invalid(temp_database):
    import database as db
    from elite_ball_store import create_inline_action, consume_inline_action

    token = create_inline_action(1, "old question")
    db.delete_gnome(1, "alice")
    assert db.return_gnome("@alice", verified_user_id=1) == "returned"
    rng = Mock(side_effect=AssertionError("old callback used RNG"))
    assert consume_inline_action(token, 1, rng).status == "unavailable"
    rng.assert_not_called()


@pytest.mark.asyncio
async def test_restart_makes_old_confirmation_inert(temp_database):
    import database as db
    from handlers import gnome_deletion as deletion

    deletion._confirmations.clear()
    db.get_or_create_duel_user(_user(), -100)
    update, context, message = _command(_user())
    await deletion.dickpukku_command(update, context)
    data = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    deletion._confirmations.clear()  # A process restart drops ephemeral confirmations.
    click, query = _click(data, _user())
    await deletion.dickpukku_callback(click, context)
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()
    assert not db.is_deleted_user(1)


def test_return_of_active_user_is_noop(temp_database):
    import database as db

    db.get_or_create_duel_user(_user(), -100)
    with db.get_db() as conn:
        conn.execute("UPDATE duel_users SET wins = 9 WHERE user_id = 1")
    assert db.return_gnome("@alice", verified_user_id=1) == "already_active"
    assert db.get_duel_user_by_id(-100, 1)["wins"] == 9


@pytest.mark.asyncio
async def test_admin_can_return_username_less_tombstone_by_exact_id(
    temp_database, monkeypatch,
):
    import config
    import database as db
    from handlers.gnome_deletion import return_gnome_command

    monkeypatch.setattr(config, "ADMIN_IDS", {11})
    db.save_or_update_user(_user(4523274, None), -100)
    db.delete_gnome(4523274, None)
    update, context, message = _command(_user(11, "admin"), args=["4523274"])
    await return_gnome_command(update, context)
    assert message.reply_text.await_args.args[0] == "Гному снова разрешено войти в игру."
    assert not db.is_deleted_user(4523274)
    fresh = db.get_or_create_duel_user(_user(4523274, "new_username"), -100)
    assert fresh["username"] == "new_username" and fresh["points"] == 20


@pytest.mark.asyncio
async def test_reassigned_username_cannot_return_previous_owner(
    temp_database, monkeypatch,
):
    import config
    import database as db
    from handlers.gnome_deletion import return_gnome_command

    monkeypatch.setattr(config, "ADMIN_IDS", {11})
    db.delete_gnome(1, "oldname")
    update, context, message = _command(_user(11, "admin"), args=["@oldname"])
    context.bot.get_chat.return_value = SimpleNamespace(
        id=1, type="private", username="newname",
    )
    await return_gnome_command(update, context)
    assert message.reply_text.await_args.args[0].startswith("Не удалось подтвердить")
    assert db.is_deleted_user(1)

    # A second user with the recycled username is also detected if seen in our DB.
    db.save_or_update_user(_user(2, "oldname"), -100)
    status, candidate = db.find_deleted_gnome_by_username("@oldname")
    assert (status, candidate) == ("ambiguous", None)
    assert db.is_deleted_user(1)
    assert not db.is_deleted_user(2)


def test_deletion_revokes_miniapp_tokens_and_blocks_new_launch(temp_database):
    import database as db
    from miniapp_sessions import create_launch_token, exchange_launch_token

    token = create_launch_token(-100, 1)
    db.delete_gnome(1, "alice")
    assert exchange_launch_token(token, 1) is None
    with pytest.raises(ValueError):
        create_launch_token(-100, 1)


def test_old_drop_cannot_restore_into_new_profile(temp_database):
    import database as db

    db.get_or_create_duel_user(_user(), -100)
    item = db.add_duel_inventory_item(-100, 1, "old_item")
    drop = db.create_duel_item_event_from_inventory(-100, 1, item["id"])
    assert drop is not None
    db.delete_gnome(1, "alice")
    assert db.return_gnome("alice", verified_user_id=1) == "returned"
    db.get_or_create_duel_user(_user(1, "fresh"), -100)
    assert not db.restore_unpublished_duel_drop(drop)
    assert db.get_duel_inventory(-100, 1) == []


@pytest.mark.asyncio
async def test_guard_blocks_game_updates_but_leaves_non_game_updates(temp_database):
    import database as db
    from telegram.ext import ApplicationHandlerStop
    from handlers.gnome_deletion import guard_deleted_game_update

    db.delete_gnome(1, "alice")
    game = SimpleNamespace(effective_user=_user(), callback_query=SimpleNamespace(
        data="boss_join", answer=AsyncMock()), message=None)
    with pytest.raises(ApplicationHandlerStop):
        await guard_deleted_game_update(game, None)
    game.callback_query.answer.assert_awaited_once()
    for command in ("/app", "/duel_app", "/me", "/duel_stats"):
        launch = SimpleNamespace(effective_user=_user(), callback_query=None,
                                 message=SimpleNamespace(text=command, reply_text=AsyncMock()))
        with pytest.raises(ApplicationHandlerStop):
            await guard_deleted_game_update(launch, None)
        launch.message.reply_text.assert_awaited_once()
    weather = SimpleNamespace(effective_user=_user(), callback_query=SimpleNamespace(
        data="wx123", answer=AsyncMock()), message=None)
    await guard_deleted_game_update(weather, None)
    weather.callback_query.answer.assert_not_awaited()
    help_update = SimpleNamespace(effective_user=_user(), callback_query=None,
                                  message=SimpleNamespace(text="/help", reply_text=AsyncMock()))
    await guard_deleted_game_update(help_update, None)
    help_update.message.reply_text.assert_not_awaited()
    hidden_command = SimpleNamespace(
        effective_user=_user(), callback_query=None,
        message=SimpleNamespace(text="/mwspukku@BotUsername", reply_text=AsyncMock()),
    )
    await guard_deleted_game_update(hidden_command, None)
    hidden_command.message.reply_text.assert_not_awaited()
    summary_update = SimpleNamespace(
        effective_user=_user(), callback_query=None,
        message=SimpleNamespace(text="/summary", reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=-100),
    )
    await guard_deleted_game_update(summary_update, None)
    from handlers.monthly_summary import summary_command
    summary_bot = SimpleNamespace(send_message=AsyncMock())
    await summary_command(summary_update, SimpleNamespace(bot=summary_bot))
    summary_bot.send_message.assert_awaited_once()
    with db.get_db() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE user_id = 1").fetchone() is None
        assert conn.execute("SELECT 1 FROM duel_users WHERE user_id = 1").fetchone() is None


def test_deleted_guard_requires_initialized_schema(temp_database):
    import sqlite3
    import database as db

    with db.get_db() as conn:
        assert not db.is_deleted_user_in_transaction(conn.cursor(), 1)
    assert db.delete_gnome(1, "alice")
    with db.get_db() as conn:
        assert db.is_deleted_user_in_transaction(conn.cursor(), 1)
        conn.execute("DROP TABLE deleted_users")
    with db.get_db() as conn, pytest.raises(sqlite3.OperationalError, match="deleted_users"):
        db.is_deleted_user_in_transaction(conn.cursor(), 1)
    with pytest.raises(sqlite3.OperationalError, match="deleted_users"):
        db.save_or_update_user(_user(2, "other"), -100)
    with db.get_db() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE user_id = 2").fetchone() is None
        assert conn.execute("SELECT 1 FROM duel_users WHERE user_id = 2").fetchone() is None
