import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


CHAT_ID = -5151


def test_only_virtual_base_inventory_skips_drop_roll_and_choice(monkeypatch):
    from handlers import duel

    roll = Mock(side_effect=AssertionError("empty inventory consumed drop roll"))
    choice = Mock(side_effect=AssertionError("empty inventory selected item"))
    monkeypatch.setattr(duel, "get_duel_inventory", lambda *_args: [])
    monkeypatch.setattr(duel.random, "random", roll)
    monkeypatch.setattr(duel.random, "choice", choice)

    assert duel._maybe_drop_loser_inventory_item(CHAT_ID, 2) is None
    roll.assert_not_called()
    choice.assert_not_called()


def test_base_item_rows_are_never_drop_candidates_or_rng_triggers(monkeypatch):
    from handlers import duel

    inventory = [
        {"id": 1, "item_id": "oiled_vest"},
        {"id": 2, "item_id": "knife"},
    ]
    roll = Mock(side_effect=AssertionError("base items consumed drop roll"))
    choice = Mock(side_effect=AssertionError("base item was selected"))
    monkeypatch.setattr(duel, "get_duel_inventory", lambda *_args: inventory)
    monkeypatch.setattr(duel.random, "random", roll)
    monkeypatch.setattr(duel.random, "choice", choice)

    assert duel._maybe_drop_loser_inventory_item(CHAT_ID, 2) is None
    roll.assert_not_called()
    choice.assert_not_called()


def test_drop_choice_receives_only_collectible_instances(monkeypatch):
    from handlers import duel

    collectible = [
        {"id": 3, "item_id": "vevangel_wing"},
        {"id": 4, "item_id": "formangnome_whisker"},
    ]
    inventory = [
        {"id": 1, "item_id": "oiled_vest"},
        collectible[0],
        {"id": 2, "item_id": "knife"},
        collectible[1],
    ]
    choice = Mock(return_value=collectible[1])
    create_event = Mock(return_value={"event_id": 8, "item_id": "formangnome_whisker"})
    monkeypatch.setattr(duel, "get_duel_inventory", lambda *_args: inventory)
    monkeypatch.setattr(duel.random, "random", Mock(return_value=0.0))
    monkeypatch.setattr(duel.random, "choice", choice)
    monkeypatch.setattr(duel, "create_duel_item_event_from_inventory", create_event)

    assert duel._maybe_drop_loser_inventory_item(CHAT_ID, 2) == create_event.return_value
    choice.assert_called_once_with(collectible)
    create_event.assert_called_once_with(CHAT_ID, 2, 4)


def test_nonempty_inventory_miss_does_not_choose_or_delete(monkeypatch):
    from handlers import duel

    inventory = [{"id": 7, "item_id": "vevangel_wing"}]
    choice = Mock(side_effect=AssertionError("miss selected item"))
    create_event = Mock(side_effect=AssertionError("miss created event"))
    monkeypatch.setattr(duel, "get_duel_inventory", lambda *_args: inventory)
    monkeypatch.setattr(duel.random, "random", Mock(return_value=duel.DUEL_ITEM_DROP_CHANCE))
    monkeypatch.setattr(duel.random, "choice", choice)
    monkeypatch.setattr(duel, "create_duel_item_event_from_inventory", create_event)

    assert duel._maybe_drop_loser_inventory_item(CHAT_ID, 2) is None
    choice.assert_not_called()
    create_event.assert_not_called()


def test_drop_removes_one_loser_duplicate_only_and_creates_exact_event(temp_database):
    import database as db
    from handlers import duel

    winner_id = 1
    loser_id = 2
    db.add_duel_inventory_item(CHAT_ID, winner_id, "rat_knuckle")
    selected = db.add_duel_inventory_item(CHAT_ID, loser_id, "vevangel_wing")
    db.add_duel_inventory_item(CHAT_ID, loser_id, "vevangel_wing")
    db.add_duel_inventory_item(CHAT_ID, loser_id, "formangnome_whisker")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(duel.random, "random", Mock(return_value=0.0))
        monkeypatch.setattr(duel.random, "choice", lambda values: values[0])
        drop = duel._maybe_drop_loser_inventory_item(CHAT_ID, loser_id)
        assert drop["instance_id"] == selected["id"]
        assert drop["item_id"] == "vevangel_wing"

    assert [item["item_id"] for item in db.get_duel_inventory(CHAT_ID, loser_id)] == [
        "vevangel_wing",
        "formangnome_whisker",
    ]
    assert [item["item_id"] for item in db.get_duel_inventory(CHAT_ID, winner_id)] == [
        "rat_knuckle"
    ]
    with sqlite3.connect(temp_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM duel_item_events").fetchone()[0] == 1
    assert db.get_duel_item_event(drop["event_id"])["item_id"] == "vevangel_wing"


def test_losing_last_collectible_leaves_virtual_base_inventory(temp_database):
    import database as db
    from handlers import duel, duel_items

    loser_id = 22
    db.add_duel_inventory_item(CHAT_ID, loser_id, "vevangel_wing")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(duel.random, "random", Mock(return_value=0.0))
        monkeypatch.setattr(duel.random, "choice", lambda values: values[0])
        assert duel._maybe_drop_loser_inventory_item(CHAT_ID, loser_id)["item_id"] == "vevangel_wing"

    remaining = db.get_duel_inventory(CHAT_ID, loser_id)
    assert remaining == []
    assert duel_items.format_duel_inventory(remaining) == "Промасленная жилетка, Нож"


@pytest.mark.asyncio
async def test_finish_duel_sends_pickup_after_result_without_extra_rng(
    monkeypatch,
    fake_context,
):
    from handlers import duel, duel_text

    winner = {"user_id": 1, "username": "winner", "points": 20}
    loser = {
        "user_id": 2,
        "username": "loser",
        "points": 20,
        "daily_wins": 0,
    }
    inventory = [{"id": 99, "item_id": "vevangel_wing"}]
    post_catalog = ("post <message> & tail",)
    events = []
    rolls = iter(
        (
            ("regular_steal_roll", 0.99),
            ("berserk_roll", 0.0),
            ("post_message_roll", 0.0),
            ("item_drop_roll", 0.0),
        )
    )

    def random_roll():
        name, value = next(rolls)
        events.append(name)
        return value

    def choose(values):
        if values is post_catalog:
            events.append("post_message_choice")
            return values[0]
        if values is inventory:
            events.append("item_instance_choice")
            return values[0]
        if isinstance(values, tuple):
            events.append("berserker_choice")
            return values[0]
        if values is duel_text.BERSERK_TRIGGERS:
            events.append("berserk_trigger_choice")
            return values[0]
        if values is duel_text.BERSERK_RESULTS:
            events.append("berserk_result_choice")
            return values[0]
        events.append("round_flavor_choice")
        return values[0]

    monkeypatch.setattr(duel, "DUEL_POST_MESSAGES", post_catalog)
    monkeypatch.setattr(duel.random, "random", random_roll)
    monkeypatch.setattr(duel.random, "choice", choose)
    monkeypatch.setattr(duel, "apply_duel_result_plan", lambda *_args: (30, 15))
    monkeypatch.setattr(
        duel,
        "apply_duel_berserk",
        lambda *_args: events.append("berserk_applied") or True,
    )
    monkeypatch.setattr(duel, "get_duel_inventory", lambda *_args: inventory)
    create_event = Mock(return_value={
        "event_id": 17,
        "chat_id": CHAT_ID,
        "user_id": loser["user_id"],
        "instance_id": 99,
        "item_id": "vevangel_wing",
    })
    monkeypatch.setattr(duel, "create_duel_item_event_from_inventory", create_event)
    bind_message = Mock(return_value=True)
    monkeypatch.setattr(duel, "set_duel_item_event_message", bind_message)
    fake_context.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=808))

    await duel._finish_duel(
        fake_context,
        CHAT_ID,
        winner,
        loser,
        custom_text="Финал.\n",
    )

    assert events == [
        "regular_steal_roll",
        "round_flavor_choice",
        "berserk_roll",
        "berserker_choice",
        "berserk_applied",
        "berserk_trigger_choice",
        "berserk_result_choice",
        "post_message_roll",
        "post_message_choice",
        "item_drop_roll",
        "item_instance_choice",
    ]
    create_event.assert_called_once_with(CHAT_ID, loser["user_id"], 99)
    assert fake_context.bot.send_message.await_count == 2
    output = fake_context.bot.send_message.await_args_list[0].kwargs["text"]
    assert "post &lt;message&gt; &amp; tail" in output
    assert "Карман порвался" not in output
    pickup = fake_context.bot.send_message.await_args_list[1].kwargs
    assert pickup["text"] == "<b>Карман порвался, выпало:</b> Крыло Вевангела"
    assert pickup["reply_markup"].inline_keyboard[0][0].text == "Подобрать"
    assert pickup["reply_markup"].inline_keyboard[0][0].callback_data == "duel_item_claim_17"
    bind_message.assert_called_once_with(17, 808, pickup["text"])


def test_active_event_keeps_selected_item_and_existing_event(temp_database):
    import database as db

    old_id = db.create_duel_item_event(CHAT_ID)
    instance = db.add_duel_inventory_item(CHAT_ID, 2, "vevangel_wing")
    assert db.create_duel_item_event_from_inventory(CHAT_ID, 2, instance["id"]) is None
    assert db.get_duel_inventory(CHAT_ID, 2)[0]["id"] == instance["id"]
    assert db.get_duel_item_event(old_id)["item_id"] is None


def test_event_insert_failure_rolls_back_inventory(temp_database):
    import database as db

    instance = db.add_duel_inventory_item(CHAT_ID, 2, "vevangel_wing")
    with sqlite3.connect(temp_database) as conn:
        conn.execute("""
            CREATE TRIGGER reject_drop BEFORE INSERT ON duel_item_events
            BEGIN SELECT RAISE(ABORT, 'event insert failed'); END
        """)
    with pytest.raises(sqlite3.IntegrityError):
        db.create_duel_item_event_from_inventory(CHAT_ID, 2, instance["id"])
    assert db.get_duel_inventory(CHAT_ID, 2)[0]["id"] == instance["id"]
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_item_events").fetchone()[0] == 0


def test_inventory_delete_failure_rolls_back_created_event(temp_database):
    import database as db

    instance = db.add_duel_inventory_item(CHAT_ID, 2, "vevangel_wing")
    with sqlite3.connect(temp_database) as conn:
        conn.execute("""
            CREATE TRIGGER reject_drop_delete BEFORE DELETE ON duel_inventory
            BEGIN SELECT RAISE(ABORT, 'inventory delete failed'); END
        """)
    with pytest.raises(sqlite3.IntegrityError):
        db.create_duel_item_event_from_inventory(CHAT_ID, 2, instance["id"])
    assert db.get_duel_inventory(CHAT_ID, 2)[0]["id"] == instance["id"]
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_item_events").fetchone()[0] == 0


def test_missing_exact_instance_never_deletes_another(temp_database, monkeypatch):
    import database as db
    from handlers import duel

    selected = db.add_duel_inventory_item(CHAT_ID, 2, "vevangel_wing")
    alternative = db.add_duel_inventory_item(CHAT_ID, 2, "formangnome_whisker")
    snapshot = db.get_duel_inventory(CHAT_ID, 2)
    assert db.remove_duel_inventory_instance(CHAT_ID, 2, selected["id"])
    monkeypatch.setattr(duel, "get_duel_inventory", lambda *_args: snapshot)
    monkeypatch.setattr(duel.random, "random", Mock(return_value=0.0))
    choice = Mock(return_value=snapshot[0])
    monkeypatch.setattr(duel.random, "choice", choice)

    assert duel._maybe_drop_loser_inventory_item(CHAT_ID, 2) is None
    choice.assert_called_once()
    assert db.get_duel_inventory(CHAT_ID, 2)[0]["id"] == alternative["id"]
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_item_events").fetchone()[0] == 0


def test_two_concurrent_drops_share_one_active_slot(temp_database):
    import database as db

    first = db.add_duel_inventory_item(CHAT_ID, 2, "vevangel_wing")
    second = db.add_duel_inventory_item(CHAT_ID, 3, "formangnome_whisker")
    with ThreadPoolExecutor(max_workers=2) as pool:
        drops = list(pool.map(
            lambda pair: db.create_duel_item_event_from_inventory(CHAT_ID, *pair),
            ((2, first["id"]), (3, second["id"])),
        ))
    assert sum(drop is not None for drop in drops) == 1
    assert sum(len(db.get_duel_inventory(CHAT_ID, user)) for user in (2, 3)) == 1
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_item_events WHERE claimed = 0").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_send_failure_restores_same_instance_and_frees_slot(temp_database, fake_context):
    import database as db
    from handlers import duel

    instance = db.add_duel_inventory_item(CHAT_ID, 2, "vevangel_wing")
    drop = db.create_duel_item_event_from_inventory(CHAT_ID, 2, instance["id"])
    fake_context.bot.send_message.side_effect = RuntimeError("Telegram unavailable")
    await duel._publish_duel_drop(fake_context, drop)

    assert db.get_duel_inventory(CHAT_ID, 2)[0]["id"] == instance["id"]
    assert db.get_duel_item_event(drop["event_id"]) is None
    assert db.create_duel_item_event(CHAT_ID) is not None


@pytest.mark.asyncio
async def test_message_binding_failure_deletes_button_and_restores_item(
    temp_database, fake_context, monkeypatch,
):
    import database as db
    from handlers import duel

    instance = db.add_duel_inventory_item(CHAT_ID, 2, "vevangel_wing")
    drop = db.create_duel_item_event_from_inventory(CHAT_ID, 2, instance["id"])
    monkeypatch.setattr(duel, "set_duel_item_event_message", Mock(return_value=False))
    await duel._publish_duel_drop(fake_context, drop)

    fake_context.bot.delete_message.assert_awaited_once()
    assert db.get_duel_inventory(CHAT_ID, 2)[0]["id"] == instance["id"]
    assert db.get_duel_item_event(drop["event_id"]) is None


@pytest.mark.asyncio
async def test_message_binding_exception_after_commit_still_restores_item(
    temp_database, fake_context, monkeypatch,
):
    import database as db
    from handlers import duel

    instance = db.add_duel_inventory_item(CHAT_ID, 2, "vevangel_wing")
    drop = db.create_duel_item_event_from_inventory(CHAT_ID, 2, instance["id"])

    def bind_then_fail(event_id, message_id, text):
        assert db.set_duel_item_event_message(event_id, message_id, text)
        raise RuntimeError("late acknowledgement failure")

    monkeypatch.setattr(duel, "set_duel_item_event_message", bind_then_fail)
    await duel._publish_duel_drop(fake_context, drop)

    fake_context.bot.delete_message.assert_awaited_once()
    assert db.get_duel_inventory(CHAT_ID, 2)[0]["id"] == instance["id"]
    assert db.get_duel_item_event(drop["event_id"]) is None


@pytest.mark.asyncio
async def test_dropped_item_uses_existing_claim_callback_and_frees_slot(
    temp_database, fake_context, monkeypatch,
):
    import database as db
    from handlers import duel_items
    from tests.test_duel_items import item_event_update, make_user

    loser = make_user(2, "loser")
    collector = make_user(3, "collector")
    outsider = make_user(4, "outsider")
    db.get_or_create_duel_user(loser, CHAT_ID)
    db.get_or_create_duel_user(collector, CHAT_ID)
    instance = db.add_duel_inventory_item(CHAT_ID, loser.id, "vevangel_wing")
    drop = db.create_duel_item_event_from_inventory(CHAT_ID, loser.id, instance["id"])
    event_id = drop["event_id"]
    choice = Mock(side_effect=AssertionError("fixed drop used random catalog"))
    monkeypatch.setattr(duel_items.random, "choice", choice)

    # A guessed callback cannot claim an item before its message is published.
    update, query = item_event_update(CHAT_ID, collector, event_id)
    await duel_items.duel_item_event_callback(update, fake_context)
    assert db.get_duel_item_event(event_id)["claimed"] is False
    assert db.get_duel_inventory(CHAT_ID, collector.id) == []
    assert db.set_duel_item_event_message(event_id, 777)

    update, query = item_event_update(CHAT_ID, outsider, event_id)
    await duel_items.duel_item_event_callback(update, fake_context)
    query.answer.assert_awaited_once_with("Сначала стань гномом.", show_alert=True)

    update, query = item_event_update(CHAT_ID, collector, event_id)
    await duel_items.duel_item_event_callback(update, fake_context)
    query.answer.assert_awaited_once_with()
    assert [item["item_id"] for item in db.get_duel_inventory(CHAT_ID, collector.id)] == [
        "vevangel_wing"
    ]
    assert db.get_duel_inventory(CHAT_ID, loser.id) == []
    assert db.get_duel_item_event(event_id)["claimed"] is True
    choice.assert_not_called()

    update, query = item_event_update(CHAT_ID, loser, event_id)
    await duel_items.duel_item_event_callback(update, fake_context)
    query.answer.assert_awaited_once_with("Уже утащили.", show_alert=True)
    assert db.create_duel_item_event(CHAT_ID) is not None


@pytest.mark.asyncio
async def test_loser_may_claim_own_drop(temp_database, fake_context):
    import database as db
    from handlers import duel_items
    from tests.test_duel_items import item_event_update, make_user

    loser = make_user(2, "loser")
    db.get_or_create_duel_user(loser, CHAT_ID)
    instance = db.add_duel_inventory_item(CHAT_ID, loser.id, "rat_knuckle")
    drop = db.create_duel_item_event_from_inventory(CHAT_ID, loser.id, instance["id"])
    db.set_duel_item_event_message(drop["event_id"], 777)
    update, query = item_event_update(CHAT_ID, loser, drop["event_id"])
    await duel_items.duel_item_event_callback(update, fake_context)
    query.answer.assert_awaited_once_with()
    assert [item["item_id"] for item in db.get_duel_inventory(CHAT_ID, loser.id)] == [
        "rat_knuckle"
    ]


@pytest.mark.asyncio
async def test_scheduled_event_sees_active_duel_drop(temp_database, fake_context, monkeypatch):
    import database as db
    from handlers import duel_items
    from tests.test_duel_items import make_user

    db.get_or_create_duel_user(make_user(2, "loser"), CHAT_ID)
    instance = db.add_duel_inventory_item(CHAT_ID, 2, "vevangel_wing")
    drop = db.create_duel_item_event_from_inventory(CHAT_ID, 2, instance["id"])
    monkeypatch.setattr(duel_items.random, "random", Mock(return_value=0.0))
    choice = Mock(side_effect=AssertionError("scheduled event chose intro"))
    monkeypatch.setattr(duel_items.random, "choice", choice)
    await duel_items._spawn_duel_item_event(fake_context, CHAT_ID)
    choice.assert_not_called()
    fake_context.bot.send_message.assert_not_awaited()
    assert db.get_duel_item_event(drop["event_id"])["claimed"] is False
