"""Regression coverage for the /lure inventory sink."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import database as db
from handlers import huecrab, lure
from handlers.duel_messaging import delete_messages_job
from module_settings import set_module_enabled


CHAT_ID = -941


def user(user_id=1):
    return SimpleNamespace(
        id=user_id,
        username=f"user{user_id}",
        first_name=f"User {user_id}",
        last_name=None,
        is_bot=False,
    )


def register(user_id=1, chat_id=CHAT_ID):
    tg_user = user(user_id)
    db.get_or_create_duel_user(tg_user, chat_id)
    return tg_user


def lure_update(tg_user, chat_id=CHAT_ID):
    reply_text = AsyncMock(return_value=SimpleNamespace(message_id=901))
    message = SimpleNamespace(
        from_user=tg_user,
        chat=SimpleNamespace(id=chat_id),
        chat_id=chat_id,
        message_id=900,
        reply_text=reply_text,
    )
    return SimpleNamespace(
        effective_chat=message.chat,
        effective_user=tg_user,
        message=message,
    ), reply_text


def inventory_ids(chat_id=CHAT_ID, user_id=1):
    return [item["id"] for item in db.get_duel_inventory(chat_id, user_id)]


def test_lure_consumes_exactly_one_random_free_instance(temp_database):
    register()
    first = db.add_duel_inventory_item(CHAT_ID, 1, "vevangel_wing")
    second = db.add_duel_inventory_item(CHAT_ID, 1, "vevangel_wing")
    third = db.add_duel_inventory_item(CHAT_ID, 1, "rat_knuckle")
    selector = Mock(return_value=(second["id"], second["item_id"]))

    status, item = db.consume_random_unequipped_duel_inventory_item(
        CHAT_ID, 1, selector,
    )

    assert status == "consumed"
    assert item == {"id": second["id"], "item_id": "vevangel_wing"}
    assert inventory_ids() == [first["id"], third["id"]]
    selector.assert_called_once()


def test_lure_excludes_equipped_and_permanent_instances(temp_database):
    register()
    equipped = db.add_duel_inventory_item(CHAT_ID, 1, "vevangel_wing")
    free = db.add_duel_inventory_item(CHAT_ID, 1, "rat_knuckle")
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO duel_equipment (chat_id, user_id, slot, inventory_id) VALUES (?, ?, ?, ?)",
            (CHAT_ID, 1, "weapon", equipped["id"]),
        )
        conn.execute(
            "INSERT INTO duel_inventory (chat_id, user_id, item_id) VALUES (?, ?, ?)",
            (CHAT_ID, 1, "knife"),
        )

    status, item = db.consume_random_unequipped_duel_inventory_item(
        CHAT_ID, 1, lambda rows: rows[0],
    )

    assert status == "consumed"
    assert item == {"id": free["id"], "item_id": "rat_knuckle"}
    assert inventory_ids() == [equipped["id"], free["id"] + 1]
    with db.get_db() as conn:
        assert conn.execute(
            "SELECT slot, inventory_id FROM duel_equipment WHERE chat_id = ? AND user_id = ?",
            (CHAT_ID, 1),
        ).fetchall() == [("weapon", equipped["id"])]


def test_lure_no_free_item_never_selects_or_deletes(temp_database):
    register()
    item = db.add_duel_inventory_item(CHAT_ID, 1, "vevangel_wing")
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO duel_equipment (chat_id, user_id, slot, inventory_id) VALUES (?, ?, ?, ?)",
            (CHAT_ID, 1, "weapon", item["id"]),
        )
    selector = Mock(side_effect=AssertionError("empty lure used RNG"))

    assert db.consume_random_unequipped_duel_inventory_item(CHAT_ID, 1, selector) == (
        "no_item", None,
    )
    selector.assert_not_called()
    assert inventory_ids() == [item["id"]]


def test_lure_database_failure_rolls_back_selected_item(temp_database):
    register()
    item = db.add_duel_inventory_item(CHAT_ID, 1, "vevangel_wing")
    with sqlite3.connect(temp_database) as conn:
        conn.execute(
            """CREATE TRIGGER reject_lure_delete BEFORE DELETE ON duel_inventory
               BEGIN SELECT RAISE(ABORT, 'lure blocked'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="lure blocked"):
        db.consume_random_unequipped_duel_inventory_item(CHAT_ID, 1, lambda rows: rows[0])
    assert inventory_ids() == [item["id"]]


def test_concurrent_lures_cannot_consume_one_instance_twice(temp_database):
    register()
    db.add_duel_inventory_item(CHAT_ID, 1, "vevangel_wing")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: db.consume_random_unequipped_duel_inventory_item(
                CHAT_ID, 1, lambda rows: rows[0],
            )[0],
            range(2),
        ))

    assert sorted(results) == ["consumed", "no_item"]
    assert inventory_ids() == []


@pytest.mark.asyncio
async def test_lure_always_announces_destroyed_item_but_spawns_only_on_ten_percent(
    temp_database, fake_context, monkeypatch,
):
    huecrab.HUECRAB_DAILY_SPAWNS.clear()
    tg_user = register()
    db.add_duel_inventory_item(CHAT_ID, 1, "vevangel_wing")
    update, reply_text = lure_update(tg_user)
    monkeypatch.setattr(lure.random, "choice", lambda rows: rows[0])
    monkeypatch.setattr(lure.random, "random", Mock(return_value=0.10))

    await lure.lure_command(update, fake_context)

    assert inventory_ids() == []
    assert "Вы выбрасываете" in reply_text.await_args.args[0]
    fake_context.bot.send_message.assert_not_awaited()
    assert fake_context.job_queue.calls == [
        (delete_messages_job, 60, {"data": {"chat_id": CHAT_ID, "message_ids": [901, 900]}}),
    ]

    db.add_duel_inventory_item(CHAT_ID, 1, "rat_knuckle")
    update, reply_text = lure_update(tg_user)
    monkeypatch.setattr(lure.random, "random", Mock(return_value=0.0))

    await lure.lure_command(update, fake_context)

    assert "Вы выбрасываете" in reply_text.await_args.args[0]
    assert fake_context.bot.send_message.await_count == 1
    event = fake_context.bot.send_message.await_args.kwargs
    assert "Появился дикий хуекраб" in event["text"]
    assert event["reply_markup"] is not None
    assert huecrab.HUECRAB_DAILY_SPAWNS[CHAT_ID] == (date.today(), 1)
    huecrab.HUECRAB_DAILY_SPAWNS.clear()


@pytest.mark.asyncio
async def test_lure_keeps_item_destroyed_when_existing_event_rules_block_spawn(
    temp_database, fake_context, monkeypatch,
):
    huecrab.HUECRAB_DAILY_SPAWNS.clear()
    tg_user = register()
    db.add_duel_inventory_item(CHAT_ID, 1, "vevangel_wing")
    set_module_enabled(CHAT_ID, "duel_random_events", False, 1)
    update, reply_text = lure_update(tg_user)
    monkeypatch.setattr(lure.random, "choice", lambda rows: rows[0])
    monkeypatch.setattr(lure.random, "random", Mock(return_value=0.0))

    await lure.lure_command(update, fake_context)

    assert inventory_ids() == []
    assert "Вы выбрасываете" in reply_text.await_args.args[0]
    fake_context.bot.send_message.assert_not_awaited()
    assert not db.has_active_huecrab_event(CHAT_ID)
    huecrab.HUECRAB_DAILY_SPAWNS.clear()


@pytest.mark.asyncio
async def test_lure_empty_inventory_does_not_roll_or_start_event(
    temp_database, fake_context, monkeypatch,
):
    tg_user = register()
    update, reply_text = lure_update(tg_user)
    roll = Mock(side_effect=AssertionError("empty lure used chance"))
    monkeypatch.setattr(lure.random, "random", roll)

    await lure.lure_command(update, fake_context)

    assert reply_text.await_args.args[0] == "Выбрасывать нечего."
    roll.assert_not_called()
    fake_context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_lure_requires_registered_gnome_before_any_inventory_or_rng_access(
    temp_database, fake_context, monkeypatch,
):
    tg_user = user()
    update, reply_text = lure_update(tg_user)
    choice = Mock(side_effect=AssertionError("unregistered lure selected an item"))
    roll = Mock(side_effect=AssertionError("unregistered lure used chance"))
    monkeypatch.setattr(lure.random, "choice", choice)
    monkeypatch.setattr(lure.random, "random", roll)

    await lure.lure_command(update, fake_context)

    assert reply_text.await_args.args[0] == "Сначала стань гномом."
    choice.assert_not_called()
    roll.assert_not_called()


@pytest.mark.asyncio
async def test_lure_spawn_is_not_lost_if_response_cleanup_cannot_be_scheduled(
    temp_database, fake_context, monkeypatch,
):
    huecrab.HUECRAB_DAILY_SPAWNS.clear()
    tg_user = register()
    db.add_duel_inventory_item(CHAT_ID, 1, "vevangel_wing")
    update, _reply_text = lure_update(tg_user)
    monkeypatch.setattr(lure.random, "choice", lambda rows: rows[0])
    monkeypatch.setattr(lure.random, "random", Mock(return_value=0.0))
    monkeypatch.setattr(lure, "send_and_schedule", AsyncMock(side_effect=RuntimeError("queue unavailable")))

    await lure.lure_command(update, fake_context)

    assert inventory_ids() == []
    assert fake_context.bot.send_message.await_count == 1
    assert db.has_active_huecrab_event(CHAT_ID)
    huecrab.HUECRAB_DAILY_SPAWNS.clear()
