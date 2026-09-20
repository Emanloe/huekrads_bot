import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from text_resources import get_text


CHAT_ID = -6161
WINNER_ID = 1
LOSER_ID = 2


def duel_players():
    return (
        {"user_id": WINNER_ID, "username": "winner", "points": 20},
        {
            "user_id": LOSER_ID,
            "username": "loser",
            "points": 20,
            "daily_wins": 0,
        },
    )


def inventory_ids(db, user_id):
    return [item["item_id"] for item in db.get_duel_inventory(CHAT_ID, user_id)]


def install_finish_persistence(monkeypatch, duel, events=None):
    def apply_result(*_args):
        if events is not None:
            events.append("ordinary_result_applied")
        return 30, 15

    monkeypatch.setattr(duel, "apply_duel_result_plan", apply_result)


@pytest.mark.asyncio
async def test_no_dick_steal_skips_item_steal_rng_choice_and_transfer(
    monkeypatch,
    temp_database,
    fake_context,
):
    import database as db
    from handlers import duel

    winner, loser = duel_players()
    db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "vevangel_wing")
    events = []
    rolls = iter(
        (
            ("dick_steal_roll", 0.99),
            ("berserk_roll", duel.BERSERK_CHANCE),
            ("post_message_roll", duel.DUEL_POST_MESSAGE_CHANCE),
            ("item_drop_roll", duel.DUEL_ITEM_DROP_CHANCE),
        )
    )

    def random_roll():
        name, value = next(rolls)
        events.append(name)
        return value

    item_steal = Mock(side_effect=AssertionError("item steal branch was entered"))
    monkeypatch.setattr(duel.random, "random", random_roll)
    monkeypatch.setattr(duel.random, "choice", lambda values: values[0])
    monkeypatch.setattr(duel, "_maybe_steal_loser_inventory_item", item_steal)
    install_finish_persistence(monkeypatch, duel, events)
    fake_context.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))

    await duel._finish_duel(fake_context, CHAT_ID, winner, loser, "Финал.\n")

    item_steal.assert_not_called()
    assert events == [
        "dick_steal_roll",
        "ordinary_result_applied",
        "berserk_roll",
        "post_message_roll",
        "item_drop_roll",
    ]
    assert inventory_ids(db, WINNER_ID) == []
    assert inventory_ids(db, LOSER_ID) == ["vevangel_wing"]
    assert "Заодно спиздил:" not in fake_context.bot.send_message.await_args.kwargs["text"]


def test_dick_steal_with_only_base_items_skips_item_roll_and_choice(monkeypatch):
    from handlers import duel

    base_inventory = [
        {"id": 10, "item_id": "oiled_vest"},
        {"id": 11, "item_id": "knife"},
    ]
    roll = Mock(side_effect=AssertionError("base inventory consumed item-steal roll"))
    choice = Mock(side_effect=AssertionError("base item was selected"))
    transfer = Mock(side_effect=AssertionError("base item was transferred"))
    monkeypatch.setattr(duel, "get_duel_inventory", lambda *_args: base_inventory)
    monkeypatch.setattr(duel.random, "random", roll)
    monkeypatch.setattr(duel.random, "choice", choice)
    monkeypatch.setattr(duel, "transfer_duel_inventory_item", transfer)

    assert duel._maybe_steal_loser_inventory_item(CHAT_ID, WINNER_ID, LOSER_ID) is None
    roll.assert_not_called()
    choice.assert_not_called()
    transfer.assert_not_called()


def test_item_steal_miss_consumes_one_roll_without_choice_or_inventory_change(
    monkeypatch,
    temp_database,
):
    import database as db
    from handlers import duel

    db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "vevangel_wing")
    roll = Mock(return_value=duel.DUEL_ITEM_STEAL_CHANCE)
    choice = Mock(side_effect=AssertionError("item-steal miss selected an instance"))
    transfer = Mock(side_effect=AssertionError("item-steal miss transferred an instance"))
    monkeypatch.setattr(duel.random, "random", roll)
    monkeypatch.setattr(duel.random, "choice", choice)
    monkeypatch.setattr(duel, "transfer_duel_inventory_item", transfer)

    assert duel._maybe_steal_loser_inventory_item(CHAT_ID, WINNER_ID, LOSER_ID) is None
    roll.assert_called_once_with()
    choice.assert_not_called()
    transfer.assert_not_called()
    assert inventory_ids(db, WINNER_ID) == []
    assert inventory_ids(db, LOSER_ID) == ["vevangel_wing"]


def test_all_collectibles_and_only_collectibles_are_item_steal_candidates():
    from handlers.duel_items import BASE_DUEL_ITEM_IDS, DUEL_ITEMS, get_droppable_duel_inventory

    instances = [
        {"id": index, "item_id": item["id"]}
        for index, item in enumerate(DUEL_ITEMS, start=1)
    ] + [
        {"id": 1001, "item_id": "oiled_vest"},
        {"id": 1002, "item_id": "knife"},
    ]

    candidates = get_droppable_duel_inventory(instances)
    assert len(candidates) == 40
    assert {item["item_id"] for item in candidates} == {item["id"] for item in DUEL_ITEMS}
    assert set(BASE_DUEL_ITEM_IDS).isdisjoint(item["item_id"] for item in candidates)


def test_atomic_transfer_moves_exactly_one_duplicate_instance(temp_database):
    import database as db

    first = db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "vevangel_wing")
    second = db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "vevangel_wing")

    assert db.transfer_duel_inventory_item(
        CHAT_ID,
        LOSER_ID,
        WINNER_ID,
        first["id"],
    ) is True
    remaining = db.get_duel_inventory(CHAT_ID, LOSER_ID)
    received = db.get_duel_inventory(CHAT_ID, WINNER_ID)
    assert [(item["id"], item["item_id"]) for item in remaining] == [
        (second["id"], "vevangel_wing")
    ]
    assert [item["item_id"] for item in received] == ["vevangel_wing"]
    assert len(remaining) + len(received) == 2


def test_atomic_transfer_rejects_missing_or_wrong_owner_without_copying(temp_database):
    import database as db

    instance = db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "vevangel_wing")

    assert db.transfer_duel_inventory_item(
        CHAT_ID,
        WINNER_ID,
        3,
        instance["id"],
    ) is False
    assert db.transfer_duel_inventory_item(
        CHAT_ID,
        LOSER_ID,
        WINNER_ID,
        instance["id"] + 999,
    ) is False
    assert inventory_ids(db, LOSER_ID) == ["vevangel_wing"]
    assert inventory_ids(db, WINNER_ID) == []


def test_atomic_transfer_rolls_back_delete_when_insert_fails(temp_database):
    import database as db

    instance = db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "vevangel_wing")
    with sqlite3.connect(temp_database) as connection:
        connection.execute(
            f"""
            CREATE TRIGGER reject_duel_item_transfer
            BEFORE INSERT ON duel_inventory
            WHEN NEW.chat_id = {CHAT_ID} AND NEW.user_id = {WINNER_ID}
            BEGIN
                SELECT RAISE(ABORT, 'injected transfer failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected transfer failure"):
        db.transfer_duel_inventory_item(
            CHAT_ID,
            LOSER_ID,
            WINNER_ID,
            instance["id"],
        )

    assert inventory_ids(db, LOSER_ID) == ["vevangel_wing"]
    assert inventory_ids(db, WINNER_ID) == []


@pytest.mark.asyncio
async def test_successful_item_steal_is_immediately_before_dick_steal_and_skips_empty_drop(
    monkeypatch,
    temp_database,
    fake_context,
):
    import database as db
    from handlers import duel

    winner, loser = duel_players()
    db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "vevangel_wing")
    events = []
    rolls = iter(
        (
            ("dick_steal_roll", 0.0),
            ("item_steal_roll", duel.DUEL_ITEM_STEAL_CHANCE - 0.000001),
            ("berserk_roll", duel.BERSERK_CHANCE),
            ("post_message_roll", duel.DUEL_POST_MESSAGE_CHANCE),
        )
    )

    def random_roll():
        name, value = next(rolls)
        events.append(name)
        return value

    def choose(values):
        if values and isinstance(values[0], dict):
            events.append("item_steal_choice")
        elif values is duel.DWARFS_FACTS:
            events.append("ordinary_fact_choice")
        else:
            events.append("round_flavor_choice")
        return values[0]

    monkeypatch.setattr(duel.random, "random", random_roll)
    monkeypatch.setattr(duel.random, "choice", choose)
    install_finish_persistence(monkeypatch, duel, events)
    fake_context.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=2))

    await duel._finish_duel(fake_context, CHAT_ID, winner, loser, "Финал.\n")

    assert events == [
        "dick_steal_roll",
        "ordinary_result_applied",
        "item_steal_roll",
        "item_steal_choice",
        "round_flavor_choice",
        "ordinary_fact_choice",
        "berserk_roll",
        "post_message_roll",
    ]
    assert inventory_ids(db, WINNER_ID) == ["vevangel_wing"]
    assert inventory_ids(db, LOSER_ID) == []
    output = fake_context.bot.send_message.await_args.kwargs["text"]
    item_text = get_text("duel.finish.item_stolen", item_name="Крыло Вевангела")
    dick_text = get_text(
        "duel.finish.stolen",
        loser_title="loser",
        stats_text="stats",
        fact="fact",
    ).split("\n\nСегодня", 1)[0]
    assert item_text + dick_text in output
    assert output.index(item_text) < output.index(dick_text)
    assert "Карман порвался" not in output


@pytest.mark.asyncio
async def test_item_steal_miss_has_no_text_or_item_choice_and_drop_uses_same_inventory(
    monkeypatch,
    temp_database,
    fake_context,
):
    import database as db
    from handlers import duel

    winner, loser = duel_players()
    db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "vevangel_wing")
    events = []
    rolls = iter(
        (
            ("dick_steal_roll", 0.0),
            ("item_steal_roll", duel.DUEL_ITEM_STEAL_CHANCE),
            ("berserk_roll", duel.BERSERK_CHANCE),
            ("post_message_roll", duel.DUEL_POST_MESSAGE_CHANCE),
            ("item_drop_roll", duel.DUEL_ITEM_DROP_CHANCE),
        )
    )

    def random_roll():
        name, value = next(rolls)
        events.append(name)
        return value

    def choose(values):
        assert not (values and isinstance(values[0], dict))
        return values[0]

    monkeypatch.setattr(duel.random, "random", random_roll)
    monkeypatch.setattr(duel.random, "choice", choose)
    install_finish_persistence(monkeypatch, duel)
    fake_context.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=3))

    await duel._finish_duel(fake_context, CHAT_ID, winner, loser, "Финал.\n")

    assert events == [
        "dick_steal_roll",
        "item_steal_roll",
        "berserk_roll",
        "post_message_roll",
        "item_drop_roll",
    ]
    assert inventory_ids(db, WINNER_ID) == []
    assert inventory_ids(db, LOSER_ID) == ["vevangel_wing"]
    assert "Заодно спиздил:" not in fake_context.bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_disappeared_selected_instance_is_not_reselected_or_reported(
    monkeypatch,
    temp_database,
    fake_context,
):
    import database as db
    from handlers import duel

    winner, loser = duel_players()
    instance = db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "vevangel_wing")
    events = []
    rolls = iter((0.0, 0.0, duel.BERSERK_CHANCE, duel.DUEL_POST_MESSAGE_CHANCE))

    def choose(values):
        if values and isinstance(values[0], dict):
            events.append("item_steal_choice")
        return values[0]

    def disappear_before_transfer(chat_id, from_user_id, _to_user_id, instance_id):
        events.append("transfer_race_lost")
        assert instance_id == instance["id"]
        assert db.remove_duel_inventory_instance(chat_id, from_user_id, instance_id)
        return False

    monkeypatch.setattr(duel.random, "random", lambda: next(rolls))
    monkeypatch.setattr(duel.random, "choice", choose)
    monkeypatch.setattr(duel, "transfer_duel_inventory_item", disappear_before_transfer)
    install_finish_persistence(monkeypatch, duel)
    fake_context.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=4))

    await duel._finish_duel(fake_context, CHAT_ID, winner, loser, "Финал.\n")

    assert events == ["item_steal_choice", "transfer_race_lost"]
    assert inventory_ids(db, WINNER_ID) == []
    assert inventory_ids(db, LOSER_ID) == []
    assert "Заодно спиздил:" not in fake_context.bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_item_steal_then_pocket_drop_uses_fresh_remaining_instance_and_stays_last(
    monkeypatch,
    temp_database,
    fake_context,
):
    import database as db
    from handlers import duel

    winner, loser = duel_players()
    db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "vevangel_wing")
    db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "formangnome_whisker")
    events = []
    rolls = iter(
        (
            ("dick_steal_roll", 0.0),
            ("item_steal_roll", 0.0),
            ("berserk_roll", duel.BERSERK_CHANCE),
            ("post_message_roll", duel.DUEL_POST_MESSAGE_CHANCE),
            ("item_drop_roll", 0.0),
        )
    )

    def random_roll():
        name, value = next(rolls)
        events.append(name)
        return value

    def choose(values):
        if values and isinstance(values[0], dict):
            event = "item_steal_choice" if len(values) == 2 else "item_drop_choice"
            events.append((event, values[0]["item_id"]))
        elif values is duel.DWARFS_FACTS:
            events.append("ordinary_fact_choice")
        else:
            events.append("round_flavor_choice")
        return values[0]

    monkeypatch.setattr(duel.random, "random", random_roll)
    monkeypatch.setattr(duel.random, "choice", choose)
    install_finish_persistence(monkeypatch, duel, events)
    fake_context.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=5))

    await duel._finish_duel(fake_context, CHAT_ID, winner, loser, "Финал.\n")

    assert events == [
        "dick_steal_roll",
        "ordinary_result_applied",
        "item_steal_roll",
        ("item_steal_choice", "vevangel_wing"),
        "round_flavor_choice",
        "ordinary_fact_choice",
        "berserk_roll",
        "post_message_roll",
        "item_drop_roll",
        ("item_drop_choice", "formangnome_whisker"),
    ]
    assert inventory_ids(db, WINNER_ID) == ["vevangel_wing"]
    assert inventory_ids(db, LOSER_ID) == []
    output = fake_context.bot.send_message.await_args.kwargs["text"]
    assert "<b>Заодно спиздил:</b> Крыло Вевангела" in output
    assert output.endswith("\n\n<b>Карман порвался, выпало:</b> Ус Формангнома")


@pytest.mark.asyncio
async def test_item_steal_notification_escapes_item_name(monkeypatch, fake_context):
    from handlers import duel

    winner, loser = duel_players()
    rolls = iter((0.0, duel.BERSERK_CHANCE, duel.DUEL_POST_MESSAGE_CHANCE))
    monkeypatch.setattr(duel.random, "random", lambda: next(rolls))
    monkeypatch.setattr(duel.random, "choice", lambda values: values[0])
    monkeypatch.setattr(duel, "_maybe_steal_loser_inventory_item", lambda *_args: "<loot & thing>")
    monkeypatch.setattr(duel, "_maybe_drop_loser_inventory_item", lambda *_args: None)
    install_finish_persistence(monkeypatch, duel)
    fake_context.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=6))

    await duel._finish_duel(fake_context, CHAT_ID, winner, loser, "Финал.\n")

    output = fake_context.bot.send_message.await_args.kwargs["text"]
    assert "<b>Заодно спиздил:</b> &lt;loot &amp; thing&gt;" in output
    assert "<loot & thing>" not in output


def test_transferred_inventory_is_visible_with_permanent_base_items(temp_database):
    import database as db
    from handlers.duel_items import format_duel_display_inventory

    instance = db.add_duel_inventory_item(CHAT_ID, LOSER_ID, "vevangel_wing")
    assert db.transfer_duel_inventory_item(
        CHAT_ID,
        LOSER_ID,
        WINNER_ID,
        instance["id"],
    )

    assert format_duel_display_inventory(db.get_duel_inventory(CHAT_ID, WINNER_ID)) == (
        "Промасленная жилетка, Нож, Крыло Вевангела"
    )
    assert format_duel_display_inventory(db.get_duel_inventory(CHAT_ID, LOSER_ID)) == (
        "Промасленная жилетка, Нож"
    )
