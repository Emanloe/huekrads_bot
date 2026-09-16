from types import SimpleNamespace
from unittest.mock import AsyncMock

import sqlite3
from datetime import date, timedelta

import pytest


def test_hyperborean_public_reexports():
    from handlers import duel, hyperborean_event

    assert duel.hyperboreic_huy_daily_job is hyperborean_event.hyperboreic_huy_daily_job
    assert duel.hyperboreic_huy_callback is hyperborean_event.hyperboreic_huy_callback
    assert duel.ACTIVE_HYPERBOREAN_EVENTS is hyperborean_event.ACTIVE_HYPERBOREAN_EVENTS


@pytest.mark.parametrize("event_type", ["hyperboreic", "arthur"])
async def test_spawn_respects_state_chance_and_event_type(monkeypatch, fake_context, event_type):
    from handlers import hyperborean_event as event

    event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    event.HYPERBOREAN_HUY_DAILY_SPAWNS.clear()
    event.ACTIVE_HYPERBOREAN_EVENTS[-1] = {"message_id": 1, "event_type": event_type}
    await event._spawn_hyperboreic_huy(fake_context, -1)
    fake_context.bot.send_message.assert_not_awaited()

    event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    monkeypatch.setattr(event.random, "random", lambda: 1.0)
    await event._spawn_hyperboreic_huy(fake_context, -1)
    fake_context.bot.send_message.assert_not_awaited()

    monkeypatch.setattr(event.random, "random", lambda: 0.0)
    monkeypatch.setattr(event.random, "choice", lambda values: event_type)
    await event._spawn_hyperboreic_huy(fake_context, -1)
    assert event.ACTIVE_HYPERBOREAN_EVENTS[-1] == {"message_id": 101, "event_type": event_type}
    keyboard = fake_context.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [button.callback_data for button in keyboard] == [
        "hyperboreic_huy_self",
        "hyperboreic_huy_other",
    ]
    assert "Один хуй тебе или два другому?" in fake_context.bot.send_message.await_args.kwargs["text"]


def test_hyperborean_spawn_frequency_constants():
    from handlers import hyperborean_event as event

    assert event.HYPERBOREAN_HUY_CHANCE == 0.05
    assert event.HYPERBOREAN_HUY_CHECK_MINUTES == 60
    assert event.HYPERBOREAN_HUY_DAILY_LIMIT == 5


async def test_daily_limit_blocks_sixth_successful_spawn(monkeypatch, fake_context):
    from handlers import hyperborean_event as event

    event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    event.HYPERBOREAN_HUY_DAILY_SPAWNS.clear()
    monkeypatch.setattr(event, "_current_date", lambda: date(2026, 9, 16))
    monkeypatch.setattr(event.random, "random", lambda: 0.0)
    monkeypatch.setattr(event.random, "choice", lambda _values: "hyperboreic")

    for _ in range(event.HYPERBOREAN_HUY_DAILY_LIMIT):
        await event._spawn_hyperboreic_huy(fake_context, -1)
        event.ACTIVE_HYPERBOREAN_EVENTS.clear()

    await event._spawn_hyperboreic_huy(fake_context, -1)

    assert fake_context.bot.send_message.await_count == 5
    assert event.HYPERBOREAN_HUY_DAILY_SPAWNS[-1] == (date(2026, 9, 16), 5)


async def test_daily_limit_resets_per_chat_on_next_calendar_day(monkeypatch, fake_context):
    from handlers import hyperborean_event as event

    event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    event.HYPERBOREAN_HUY_DAILY_SPAWNS.clear()
    today = date(2026, 9, 16)
    monkeypatch.setattr(event, "_current_date", lambda: today)
    monkeypatch.setattr(event.random, "random", lambda: 0.0)
    monkeypatch.setattr(event.random, "choice", lambda _values: "arthur")

    for _ in range(event.HYPERBOREAN_HUY_DAILY_LIMIT):
        await event._spawn_hyperboreic_huy(fake_context, -1)
        event.ACTIVE_HYPERBOREAN_EVENTS.clear()

    today += timedelta(days=1)
    await event._spawn_hyperboreic_huy(fake_context, -1)

    assert fake_context.bot.send_message.await_count == 6
    assert event.HYPERBOREAN_HUY_DAILY_SPAWNS[-1] == (today, 1)


async def test_daily_limits_are_independent_per_chat(monkeypatch, fake_context):
    from handlers import hyperborean_event as event

    event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    event.HYPERBOREAN_HUY_DAILY_SPAWNS.clear()
    today = date(2026, 9, 16)
    monkeypatch.setattr(event, "_current_date", lambda: today)
    monkeypatch.setattr(event.random, "random", lambda: 0.0)
    monkeypatch.setattr(event.random, "choice", lambda _values: "hyperboreic")

    for _ in range(event.HYPERBOREAN_HUY_DAILY_LIMIT):
        await event._spawn_hyperboreic_huy(fake_context, -1)
        event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    await event._spawn_hyperboreic_huy(fake_context, -2)

    assert fake_context.bot.send_message.await_count == 6
    assert event.HYPERBOREAN_HUY_DAILY_SPAWNS[-1] == (today, 5)
    assert event.HYPERBOREAN_HUY_DAILY_SPAWNS[-2] == (today, 1)


async def test_active_event_and_failed_roll_do_not_increment_daily_count(monkeypatch, fake_context):
    from handlers import hyperborean_event as event

    event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    event.HYPERBOREAN_HUY_DAILY_SPAWNS.clear()
    today = date(2026, 9, 16)
    monkeypatch.setattr(event, "_current_date", lambda: today)
    event.HYPERBOREAN_HUY_DAILY_SPAWNS[-1] = (today, 2)
    event.ACTIVE_HYPERBOREAN_EVENTS[-1] = {"message_id": 1, "event_type": "arthur"}

    await event._spawn_hyperboreic_huy(fake_context, -1)
    event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    monkeypatch.setattr(event.random, "random", lambda: 1.0)
    await event._spawn_hyperboreic_huy(fake_context, -1)

    assert fake_context.bot.send_message.await_count == 0
    assert event.HYPERBOREAN_HUY_DAILY_SPAWNS[-1] == (today, 2)


async def test_failed_message_creation_does_not_increment_daily_count(monkeypatch, fake_context):
    from handlers import hyperborean_event as event

    event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    event.HYPERBOREAN_HUY_DAILY_SPAWNS.clear()
    monkeypatch.setattr(event, "_current_date", lambda: date(2026, 9, 16))
    monkeypatch.setattr(event.random, "random", lambda: 0.0)
    monkeypatch.setattr(event.random, "choice", lambda _values: "hyperboreic")
    fake_context.bot.send_message.side_effect = RuntimeError("Telegram unavailable")

    await event._spawn_hyperboreic_huy(fake_context, -1)

    assert -1 not in event.HYPERBOREAN_HUY_DAILY_SPAWNS
    assert -1 not in event.ACTIVE_HYPERBOREAN_EVENTS


def test_claim_uses_temporary_database(monkeypatch, temp_database, tg_user):
    from handlers import hyperborean_event as event

    monkeypatch.setattr(event, "_HYPERBOREAN_DB_PATH", temp_database)
    assert event._claim_hyperboreic_huy(-99, tg_user) == "exploded"
    assert event._claim_hyperboreic_huy(-99, tg_user) == "restored"


def _duel_user(user_id, username):
    return SimpleNamespace(
        id=user_id,
        username=username,
        first_name=username,
        last_name=None,
        is_bot=False,
    )


def _set_dick_state(db_path, chat_id, user_id, *, points, dick_stolen_today):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE duel_users SET points = ?, dick_stolen_today = ? WHERE chat_id = ? AND user_id = ?",
            (points, dick_stolen_today, chat_id, user_id),
        )


def _get_dick_state(db_path, chat_id, user_id):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT points, dick_stolen_today FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()


def test_other_claim_uses_only_current_chat_and_explodes_selected_user(monkeypatch, temp_database, tg_user):
    from database import get_or_create_duel_user
    from handlers import hyperborean_event as event

    monkeypatch.setattr(event, "_HYPERBOREAN_DB_PATH", temp_database)
    other_chat_user = _duel_user(2002, "other_chat")
    same_chat_user = _duel_user(2003, "same_chat")
    get_or_create_duel_user(other_chat_user, -100)
    get_or_create_duel_user(same_chat_user, -99)
    _set_dick_state(temp_database, -99, same_chat_user.id, points=73, dick_stolen_today=1)
    _set_dick_state(temp_database, -100, other_chat_user.id, points=66, dick_stolen_today=1)
    monkeypatch.setattr(event.random, "choice", lambda users: next(user for user in users if user[0] == same_chat_user.id))

    result, selected, had_no_dick = event._claim_hyperboreic_huy_for_other(-99, tg_user)

    assert (result, selected["user_id"], had_no_dick) == ("exploded", same_chat_user.id, True)
    assert _get_dick_state(temp_database, -99, same_chat_user.id) == (0, 1)
    assert _get_dick_state(temp_database, -100, other_chat_user.id) == (66, 1)


def test_other_claim_can_select_clicking_user(monkeypatch, temp_database, tg_user):
    from handlers import hyperborean_event as event

    monkeypatch.setattr(event, "_HYPERBOREAN_DB_PATH", temp_database)
    event._claim_hyperboreic_huy(-99, tg_user)  # Register, then leave the user without a dick.
    monkeypatch.setattr(event.random, "choice", lambda users: next(user for user in users if user[0] == tg_user.id))

    result, selected, had_no_dick = event._claim_hyperboreic_huy_for_other(-99, tg_user)

    assert (result, selected["user_id"], had_no_dick) == ("exploded", tg_user.id, True)
    assert _get_dick_state(temp_database, -99, tg_user.id) == (0, 1)


def test_other_claim_leaves_user_with_dick_unchanged_and_rolls_once(monkeypatch, temp_database, tg_user):
    from handlers import hyperborean_event as event

    monkeypatch.setattr(event, "_HYPERBOREAN_DB_PATH", temp_database)
    calls = []

    def choose_once(users):
        calls.append(users)
        return next(user for user in users if user[0] == tg_user.id)

    monkeypatch.setattr(event.random, "choice", choose_once)
    result, selected, had_no_dick = event._claim_hyperboreic_huy_for_other(-99, tg_user)

    assert (result, selected["user_id"], had_no_dick) == ("unchanged", tg_user.id, False)
    assert _get_dick_state(temp_database, -99, tg_user.id) == (20, 0)
    assert len(calls) == 1


async def test_callback_restores_event_state_after_claim_error(monkeypatch, fake_context, tg_user):
    from handlers import hyperborean_event as event

    event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    saved = {"message_id": 77, "event_type": "hyperboreic"}
    event.ACTIVE_HYPERBOREAN_EVENTS[-1] = saved
    monkeypatch.setattr(event, "_claim_hyperboreic_huy", lambda *_args: "error")
    query = SimpleNamespace(data="hyperboreic_huy", from_user=tg_user, answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=-1))

    await event.hyperboreic_huy_callback(update, fake_context)

    assert event.ACTIVE_HYPERBOREAN_EVENTS[-1] == saved


@pytest.mark.parametrize("event_type", ["hyperboreic", "arthur"])
async def test_other_callback_consumes_event_once(monkeypatch, fake_context, tg_user, event_type):
    from handlers import hyperborean_event as event

    event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    event.ACTIVE_HYPERBOREAN_EVENTS[-1] = {"message_id": 77, "event_type": event_type}
    fake_context.bot.edit_message_reply_markup = AsyncMock()
    claims = []

    def claim_other(*_args):
        claims.append(1)
        return "unchanged", {"user_id": tg_user.id, "username": "tester", "display_name": "Tester"}, False

    monkeypatch.setattr(event, "_claim_hyperboreic_huy_for_other", claim_other)
    query = SimpleNamespace(data="hyperboreic_huy_other", from_user=tg_user, answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=-1))

    await event.hyperboreic_huy_callback(update, fake_context)
    await event.hyperboreic_huy_callback(update, fake_context)

    assert claims == [1]
    fake_context.bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=-1, message_id=77, reply_markup=None
    )
    assert event.ACTIVE_HYPERBOREAN_EVENTS == {}
