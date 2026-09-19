import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "duel_items.json"


def make_user(user_id, username, first_name=None):
    return SimpleNamespace(
        id=user_id,
        username=username,
        first_name=first_name or username,
        last_name=None,
        is_bot=False,
    )


def test_duel_item_catalog_has_exact_stable_contract():
    from config import (
        BOSS_ITEM_DROP_CHANCE,
        DUEL_ITEM_DROP_CHANCE,
        DUEL_ITEM_EVENT_CHANCE,
        DUEL_ITEM_EVENT_CHECK_MINUTES,
    )
    from handlers.duel_items import (
        BASE_DUEL_ITEM_IDS,
        BASE_DUEL_ITEMS,
        DUEL_ITEMS,
        DUEL_ITEM_NAMES,
        get_duel_item_name,
    )
    from handlers import duel
    from text_resources import get_text_list

    with open(CATALOG_PATH, encoding="utf-8") as catalog_file:
        items = json.load(catalog_file)
    assert items == list(DUEL_ITEMS)
    assert duel.DUEL_ITEMS is DUEL_ITEMS
    assert len(items) == 40
    assert len(BASE_DUEL_ITEMS) == 2
    assert BASE_DUEL_ITEM_IDS == ("oiled_vest", "knife")
    assert [item["name"] for item in BASE_DUEL_ITEMS] == [
        "Промасленная жилетка",
        "Нож",
    ]
    assert set(BASE_DUEL_ITEM_IDS).isdisjoint(item["id"] for item in DUEL_ITEMS)
    assert {"vevangel_wing", "formangnome_whisker"} <= {
        item["id"] for item in DUEL_ITEMS
    }
    assert all(set(item) == {"id", "name"} for item in items)
    assert len({item["id"] for item in items}) == 40
    assert len({item["name"] for item in items}) == 40
    assert all(re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", item["id"]) for item in items)
    assert all(item["name"].strip() for item in items)
    assert DUEL_ITEM_NAMES["vevangel_wing"] == "Крыло Вевангела"
    assert DUEL_ITEM_NAMES["formangnome_whisker"] == "Ус Формангнома"
    assert get_duel_item_name("rusty_dwarf_fork") == "Ржавая гномья вилка"
    assert get_duel_item_name("legacy_missing_id") == "Неизвестная находка"
    assert len(get_text_list("duel.item_event.intros")) == 8
    assert DUEL_ITEM_DROP_CHANCE == 0.50
    assert BOSS_ITEM_DROP_CHANCE == 0.50
    assert DUEL_ITEM_EVENT_CHANCE == 0.10
    assert DUEL_ITEM_EVENT_CHECK_MINUTES == 60


def test_inventory_migration_is_idempotent_on_existing_database(tmp_path, monkeypatch):
    import database as db

    database_path = tmp_path / "existing.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE duel_users (
                user_id INTEGER,
                chat_id INTEGER DEFAULT 0,
                username TEXT,
                display_name TEXT NOT NULL,
                points INTEGER DEFAULT 20,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                stolen_dicks_count INTEGER DEFAULT 0,
                dick_stolen_count INTEGER DEFAULT 0,
                dick_stolen_today INTEGER DEFAULT 0,
                last_activity_date TEXT,
                last_stolen_by TEXT DEFAULT NULL,
                daily_wins INTEGER DEFAULT 0,
                bosses_defeated INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO duel_users (user_id, chat_id, username, display_name, points)
            VALUES (1, -1, 'migration_user', 'migration_user', 73)
            """
        )

    monkeypatch.setattr(db, "DB_NAME", str(database_path))
    db.init_db()
    db.init_db()

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        preserved = connection.execute(
            "SELECT username FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (-1, 1),
        ).fetchone()
        points = connection.execute(
            "SELECT points FROM duel_users WHERE chat_id = -1 AND user_id = 1"
        ).fetchone()

    assert {"duel_inventory", "duel_item_events"} <= tables
    assert "idx_duel_inventory_owner" in indexes
    assert "idx_duel_item_events_active_chat" in indexes
    assert preserved == ("migration_user",)
    assert points == (73,)


def test_inventory_instances_duplicates_deletion_and_owner_isolation(temp_database):
    import database as db

    first = db.add_duel_inventory_item(-10, 1, "vevangel_wing")
    second = db.add_duel_inventory_item(-10, 1, "vevangel_wing")
    db.add_duel_inventory_item(-10, 1, "formangnome_whisker")
    db.add_duel_inventory_item(-11, 1, "rat_knuckle")
    db.add_duel_inventory_item(-10, 2, "holy_screwdriver")

    inventory = db.get_duel_inventory(-10, 1)
    assert [item["item_id"] for item in inventory] == [
        "vevangel_wing",
        "vevangel_wing",
        "formangnome_whisker",
    ]
    assert first["id"] != second["id"]
    assert db.remove_duel_inventory_instance(-10, 1, first["id"]) is True
    assert db.remove_duel_inventory_instance(-10, 1, first["id"]) is False
    assert [item["item_id"] for item in db.get_duel_inventory(-10, 1)] == [
        "vevangel_wing",
        "formangnome_whisker",
    ]
    assert [item["item_id"] for item in db.get_duel_inventory(-11, 1)] == [
        "rat_knuckle"
    ]
    assert [item["item_id"] for item in db.get_duel_inventory(-10, 2)] == [
        "holy_screwdriver"
    ]


def test_chat_event_claim_is_atomic_persistent_and_awards_one_instance(temp_database):
    import database as db

    chat_id = -20
    db.get_or_create_duel_user(make_user(1, "first"), chat_id)
    db.get_or_create_duel_user(make_user(2, "second"), chat_id)
    event_id = db.create_duel_item_event(chat_id)
    assert event_id is not None
    assert db.create_duel_item_event(chat_id) is None

    first_selector = Mock(return_value="vevangel_wing")
    second_selector = Mock(return_value="formangnome_whisker")
    assert db.claim_duel_item_event(event_id, chat_id, 1, first_selector)[0] == "claimed"
    assert db.claim_duel_item_event(event_id, chat_id, 2, second_selector) == (
        "already_claimed",
        None,
    )
    first_selector.assert_called_once_with()
    second_selector.assert_not_called()
    assert [item["item_id"] for item in db.get_duel_inventory(chat_id, 1)] == [
        "vevangel_wing"
    ]
    assert db.get_duel_inventory(chat_id, 2) == []
    assert db.get_duel_item_event(event_id) == db.get_duel_item_event(event_id)
    assert db.get_duel_item_event(event_id)["claimed"] is True
    assert db.get_duel_item_event(event_id)["claimed_by"] == 1


def test_concurrent_chat_event_claim_has_one_winner_and_one_item(temp_database):
    import database as db

    chat_id = -21
    for user_id in (1, 2):
        db.get_or_create_duel_user(make_user(user_id, f"user{user_id}"), chat_id)
    event_id = db.create_duel_item_event(chat_id)

    def claim(user_id):
        return db.claim_duel_item_event(
            event_id,
            chat_id,
            user_id,
            lambda: "rat_knuckle",
        )[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(claim, (1, 2)))

    assert sorted(statuses) == ["already_claimed", "claimed"]
    all_items = db.get_duel_inventory(chat_id, 1) + db.get_duel_inventory(chat_id, 2)
    assert [item["item_id"] for item in all_items] == ["rat_knuckle"]


@pytest.mark.asyncio
async def test_duel_stats_inventory_grouping_html_safety_unknown_and_read_only(
    monkeypatch,
    temp_database,
    fake_context,
):
    import database as db
    from handlers import duel
    from handlers import duel_items

    chat_id = -30
    user = make_user(3, None, "<unsafe & dwarf>")
    db.get_or_create_duel_user(user, chat_id)
    db.add_duel_inventory_item(chat_id, user.id, "vevangel_wing")
    db.add_duel_inventory_item(chat_id, user.id, "vevangel_wing")
    db.add_duel_inventory_item(chat_id, user.id, "formangnome_whisker")
    db.add_duel_inventory_item(chat_id, user.id, "legacy_unknown")
    before = db.get_duel_inventory(chat_id, user.id)

    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    update = SimpleNamespace(
        message=SimpleNamespace(from_user=user, chat=SimpleNamespace(id=chat_id), chat_id=chat_id)
    )
    await duel.duel_stats_command(update, fake_context)

    output = sent.await_args.args[2]
    assert "&lt;unsafe &amp; dwarf&gt;" in output
    assert "<b>Инвентарь:</b>" in output
    assert "Крыло Вевангела ×2" in output
    assert "Ус Формангнома" in output
    assert "Неизвестная находка" in output
    assert "legacy_unknown" not in output
    assert db.get_duel_inventory(chat_id, user.id) == before

    assert duel_items.format_duel_inventory([]) == "Промасленная жилетка, Нож"
    assert duel_items.format_duel_inventory(
        [
            {"item_id": "oiled_vest"},
            {"item_id": "knife"},
            {"item_id": "vevangel_wing"},
            {"item_id": "vevangel_wing"},
        ]
    ) == "Промасленная жилетка, Нож, Крыло Вевангела ×2"
    monkeypatch.setitem(duel_items.DUEL_ITEM_NAMES, "unsafe_item", "<loot & thing>")
    assert duel_items.format_duel_inventory([{"item_id": "unsafe_item"}]) == (
        "Промасленная жилетка, Нож, &lt;loot &amp; thing&gt;"
    )


@pytest.mark.asyncio
async def test_old_and_new_dwarfs_see_virtual_base_items_without_database_rows(
    monkeypatch,
    temp_database,
    fake_context,
):
    import database as db
    from handlers import duel

    chat_id = -31
    users = [make_user(31, "old_dwarf"), make_user(32, "new_dwarf")]
    db.get_or_create_duel_user(users[0], chat_id)
    assert db.get_duel_inventory(chat_id, users[0].id) == []

    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    for user in users:
        update = SimpleNamespace(
            message=SimpleNamespace(
                from_user=user,
                chat=SimpleNamespace(id=chat_id),
                chat_id=chat_id,
            )
        )
        await duel.duel_stats_command(update, fake_context)
        assert sent.await_args.args[2].endswith(
            "<b>Инвентарь:</b> Промасленная жилетка, Нож"
        )
        assert db.get_duel_inventory(chat_id, user.id) == []


def item_event_update(chat_id, user, event_id):
    query = SimpleNamespace(
        data=f"duel_item_claim_{event_id}",
        from_user=user,
        answer=AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=chat_id),
    ), query


@pytest.mark.asyncio
async def test_item_event_job_uses_eligible_chat_button_and_persistent_active_limit(
    monkeypatch,
    temp_database,
    fake_context,
):
    import database as db
    from handlers import duel_items

    await duel_items.duel_item_event_job(fake_context)
    fake_context.bot.send_message.assert_not_awaited()

    chat_id = -40
    db.get_or_create_duel_user(make_user(4, "eligible"), chat_id)
    monkeypatch.setattr(duel_items.random, "random", Mock(return_value=0.0))
    monkeypatch.setattr(duel_items.random, "choice", lambda values: values[0])
    await duel_items.duel_item_event_job(fake_context)
    await duel_items.duel_item_event_job(fake_context)

    fake_context.bot.send_message.assert_awaited_once()
    kwargs = fake_context.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == chat_id
    assert kwargs["parse_mode"] == "HTML"
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Подобрать"
    assert re.fullmatch(r"duel_item_claim_\d+", button.callback_data)


@pytest.mark.asyncio
async def test_item_event_callback_rejects_invalid_then_claims_once_and_edits_safely(
    monkeypatch,
    temp_database,
    fake_context,
):
    import database as db
    from handlers import duel_items

    chat_id = -41
    event_id = db.create_duel_item_event(chat_id)
    db.set_duel_item_event_message(event_id, 777)
    choice = Mock(return_value=duel_items.DUEL_ITEMS[0])
    monkeypatch.setattr(duel_items.random, "choice", choice)

    outsider = make_user(10, "outsider")
    update, query = item_event_update(chat_id, outsider, event_id)
    await duel_items.duel_item_event_callback(update, fake_context)
    query.answer.assert_awaited_once_with("Сначала стань гномом.", show_alert=True)
    choice.assert_not_called()

    claimant = make_user(11, None, "<claimant & one>")
    db.get_or_create_duel_user(claimant, chat_id)
    update, query = item_event_update(chat_id, claimant, event_id)
    await duel_items.duel_item_event_callback(update, fake_context)
    query.answer.assert_awaited_once_with()
    choice.assert_called_once_with(duel_items.DUEL_ITEMS)
    assert len(db.get_duel_inventory(chat_id, claimant.id)) == 1
    fake_context.bot.edit_message_text.assert_awaited_once()
    edit = fake_context.bot.edit_message_text.await_args.kwargs
    assert edit["message_id"] == 777
    assert edit["reply_markup"] is None
    assert "&lt;claimant &amp; one&gt;" in edit["text"]
    assert "Ржавая гномья вилка" in edit["text"]

    second = make_user(12, "second")
    db.get_or_create_duel_user(second, chat_id)
    update, query = item_event_update(chat_id, second, event_id)
    await duel_items.duel_item_event_callback(update, fake_context)
    query.answer.assert_awaited_once_with("Уже утащили.", show_alert=True)
    assert db.get_duel_inventory(chat_id, second.id) == []
    choice.assert_called_once()

    update, query = item_event_update(chat_id, claimant, event_id)
    await duel_items.duel_item_event_callback(update, fake_context)
    query.answer.assert_awaited_once_with("Уже утащили.", show_alert=True)
    assert len(db.get_duel_inventory(chat_id, claimant.id)) == 1
