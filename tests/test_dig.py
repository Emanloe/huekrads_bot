"""Paid digging shares the existing atomic duel-item pickup slot."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


CHAT_ID = -840
TODAY = "2026-09-30"
TOMORROW = "2026-10-01"


def user(user_id, username=None):
    name = username or f"user{user_id}"
    return SimpleNamespace(
        id=user_id, username=name, first_name=name, last_name=None, is_bot=False,
    )


def register(db, user_id=1, chat_id=CHAT_ID, points=50, username=None):
    tg_user = user(user_id, username)
    db.get_or_create_duel_user(tg_user, chat_id)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE duel_users SET points = ? WHERE chat_id = ? AND user_id = ?",
            (points, chat_id, user_id),
        )
    return tg_user


def points(db, user_id=1, chat_id=CHAT_ID):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT points FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
    return row[0] if row else None


def update(tg_user, chat_id=CHAT_ID):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id), effective_user=tg_user,
        message=SimpleNamespace(from_user=tg_user, chat_id=chat_id),
    )


@pytest.fixture
def frozen_dig_date(temp_database, monkeypatch):
    import database as db

    clock = [datetime(2026, 9, 30, 20, 59, 59, tzinfo=timezone.utc)]
    monkeypatch.setattr(db, "_utc_now", lambda: clock[0])
    return clock


def test_moscow_day_boundary_and_idempotent_existing_db_migration(tmp_path, monkeypatch):
    import database as db

    before = datetime(2026, 9, 30, 20, 59, 59, tzinfo=timezone.utc)
    after = datetime(2026, 9, 30, 21, 0, 0, tzinfo=timezone.utc)
    assert db.moscow_date_key(before) == TODAY
    assert db.moscow_date_key(after) == TOMORROW

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE preserved (value TEXT)")
        conn.execute("INSERT INTO preserved VALUES ('keep')")
    monkeypatch.setattr(db, "DB_NAME", str(path))
    db.init_db()
    register(db)
    status, _ = db.try_duel_dig(CHAT_ID, 1, lambda: 1.0, lambda: None)
    assert status == "miss"
    db.init_db()
    assert db.get_duel_dig_attempts(CHAT_ID, 1, db.moscow_date_key()) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM preserved").fetchone()[0] == "keep"


@pytest.mark.asyncio
async def test_unregistered_low_points_and_active_slot_reject_before_rng(
    frozen_dig_date, fake_context, monkeypatch,
):
    import database as db
    from handlers import dig

    roll = Mock(side_effect=AssertionError("rejected dig used RNG"))
    choice = Mock(side_effect=AssertionError("rejected dig chose item"))
    monkeypatch.setattr(dig.random, "random", roll)
    monkeypatch.setattr(dig.random, "choice", choice)

    await dig.dig_command(update(user(1)), fake_context)
    assert points(db) is None
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 0
    assert fake_context.bot.send_message.await_args.kwargs["text"] == "Сначала стань гномом."

    register(db, points=9)
    await dig.dig_command(update(user(1)), fake_context)
    assert points(db) == 9
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 0
    assert "10 очков" in fake_context.bot.send_message.await_args.kwargs["text"]

    old_event = db.create_duel_item_event(CHAT_ID)
    await dig.dig_command(update(user(1)), fake_context)
    assert points(db) == 9
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 0
    assert db.get_duel_item_event(old_event)["claimed"] is False
    assert "подберите" in fake_context.bot.send_message.await_args.kwargs["text"].lower()
    roll.assert_not_called()
    choice.assert_not_called()


@pytest.mark.asyncio
async def test_exactly_ten_points_miss_costs_one_roll_and_one_attempt(
    frozen_dig_date, fake_context, monkeypatch,
):
    import database as db
    from handlers import dig

    register(db, points=10)
    roll = Mock(return_value=0.20)
    choice = Mock(side_effect=AssertionError("miss chose item"))
    monkeypatch.setattr(dig.random, "random", roll)
    monkeypatch.setattr(dig.random, "choice", choice)
    await dig.dig_command(update(user(1)), fake_context)

    roll.assert_called_once_with()
    choice.assert_not_called()
    assert points(db) == 0
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 1
    assert "Нихуя не откопал" in fake_context.bot.send_message.await_args.kwargs["text"]
    assert "Попыток осталось сегодня: 4" in fake_context.bot.send_message.await_args.kwargs["text"]
    assert "Очков осталось: 0" in fake_context.bot.send_message.await_args.kwargs["text"]
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_item_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_reply_and_arguments_cannot_change_dig_target(
    frozen_dig_date, fake_context, monkeypatch,
):
    import database as db
    from handlers import dig

    digger = register(db, 1, points=20)
    register(db, 2, points=20)
    monkeypatch.setattr(dig.random, "random", Mock(return_value=1.0))
    message = update(digger)
    message.message.reply_to_message = SimpleNamespace(from_user=user(2))
    fake_context.args = ["2", "@user2"]
    await dig.dig_command(message, fake_context)
    assert points(db, 1) == 10
    assert points(db, 2) == 20
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 1
    assert db.get_duel_dig_attempts(CHAT_ID, 2, TODAY) == 0


@pytest.mark.asyncio
async def test_hit_uses_common_catalog_and_publishes_exact_item_without_auto_award(
    frozen_dig_date, fake_context, monkeypatch,
):
    import database as db
    from handlers import dig
    from handlers.duel_items import BASE_DUEL_ITEM_IDS, DUEL_ITEMS

    register(db, points=50, username="<Eman&>")
    db.set_duel_dwarf_name_once(CHAT_ID, 1, "<Гном>")
    selected = DUEL_ITEMS[-1]
    roll = Mock(return_value=0.0)
    choice = Mock(return_value=selected)
    monkeypatch.setattr(dig.random, "random", roll)
    monkeypatch.setattr(dig.random, "choice", choice)
    await dig.dig_command(update(user(1)), fake_context)

    roll.assert_called_once_with()
    choice.assert_called_once_with(DUEL_ITEMS)
    assert selected["id"] not in BASE_DUEL_ITEM_IDS
    assert points(db) == 40
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 1
    assert db.get_duel_inventory(CHAT_ID, 1) == []
    message = fake_context.bot.send_message.await_args.kwargs
    assert message["parse_mode"] == "HTML"
    assert "&lt;Гном&gt; (&lt;Eman&amp;&gt;)" in message["text"]
    assert "Попыток осталось сегодня: 4" in message["text"]
    assert "Очков осталось: 40" in message["text"]
    button = message["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Подобрать"
    event_id = int(button.callback_data.removeprefix("duel_item_claim_"))
    event = db.get_duel_item_event(event_id)
    assert event["item_id"] == selected["id"]
    assert event["message_id"] == 101
    assert event["claimed"] is False
    assert db.get_monthly_chat_stats(CHAT_ID, "2026-09") == {
        "dicks_stolen": 0, "duels": 0, "bosses_killed": 0,
    }
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM monthly_chat_stats").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("claimant_id", [1, 2])
async def test_existing_callback_awards_found_item_to_first_registered_claimant(
    frozen_dig_date, fake_context, monkeypatch, claimant_id,
):
    import database as db
    from handlers import dig, duel_items
    from tests.test_duel_items import item_event_update

    digger = register(db, 1)
    other = register(db, 2)
    monkeypatch.setattr(dig.random, "random", Mock(return_value=0.0))
    monkeypatch.setattr(dig.random, "choice", Mock(return_value=duel_items.DUEL_ITEMS[0]))
    await dig.dig_command(update(digger), fake_context)
    callback_data = fake_context.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    event_id = int(callback_data.removeprefix(duel_items.DUEL_ITEM_EVENT_CALLBACK_PREFIX))
    monkeypatch.setattr(duel_items.random, "choice", Mock(side_effect=AssertionError("fixed loot rerolled")))
    outsider_update, outsider_query = item_event_update(CHAT_ID, user(3), event_id)
    await duel_items.duel_item_event_callback(outsider_update, fake_context)
    outsider_query.answer.assert_awaited_once_with("Сначала стань гномом.", show_alert=True)
    claimant = digger if claimant_id == 1 else other
    callback, query = item_event_update(CHAT_ID, claimant, event_id)
    await duel_items.duel_item_event_callback(callback, fake_context)
    query.answer.assert_awaited_once_with()
    assert [item["item_id"] for item in db.get_duel_inventory(CHAT_ID, claimant_id)] == [
        duel_items.DUEL_ITEMS[0]["id"]
    ]
    assert db.get_duel_item_event(event_id)["claimed"] is True
    loser = other if claimant_id == 1 else digger
    losing_update, losing_query = item_event_update(CHAT_ID, loser, event_id)
    await duel_items.duel_item_event_callback(losing_update, fake_context)
    losing_query.answer.assert_awaited_once_with("Уже утащили.", show_alert=True)
    assert db.create_duel_item_event(CHAT_ID) is not None


def test_daily_limit_and_next_moscow_day_are_scoped_per_chat(frozen_dig_date):
    import database as db

    register(db, 1, CHAT_ID, points=100)
    register(db, 1, CHAT_ID - 1, points=100)
    roll = Mock(return_value=1.0)
    choice = Mock(side_effect=AssertionError("miss chose item"))
    for _ in range(5):
        assert db.try_duel_dig(CHAT_ID, 1, roll, choice)[0] == "miss"
    assert points(db) == 50
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 5
    assert db.try_duel_dig(CHAT_ID, 1, roll, choice)[0] == "daily_limit"
    assert roll.call_count == 5
    assert points(db) == 50
    assert db.try_duel_dig(CHAT_ID - 1, 1, roll, choice)[0] == "miss"
    assert db.get_duel_dig_attempts(CHAT_ID - 1, 1, TODAY) == 1

    frozen_dig_date[0] = datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc)
    assert db.try_duel_dig(CHAT_ID, 1, roll, choice)[0] == "miss"
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 5
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TOMORROW) == 1
    assert points(db) == 40


def test_concurrent_digs_cannot_exceed_limit_or_overdraw_points(frozen_dig_date):
    import database as db

    register(db, points=100)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(
            lambda _: db.try_duel_dig(CHAT_ID, 1, lambda: 1.0, lambda: None)[0],
            range(12),
        ))
    assert results.count("miss") == 5
    assert results.count("daily_limit") == 7
    assert points(db) == 50
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 5

    register(db, 2, points=20)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: db.try_duel_dig(CHAT_ID, 2, lambda: 1.0, lambda: None)[0],
            range(8),
        ))
    assert results.count("miss") == 2
    assert results.count("insufficient_points") == 6
    assert points(db, 2) == 0


def test_concurrent_hits_create_one_active_event_and_charge_only_one(frozen_dig_date):
    import database as db

    register(db, 1, points=20)
    register(db, 2, points=20)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda user_id: db.try_duel_dig(
                CHAT_ID, user_id, lambda: 0.0, lambda: "vevangel_wing",
            )[0],
            (1, 2),
        ))
    assert sorted(results) == ["active_event", "found"]
    assert points(db, 1) + points(db, 2) == 30
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) + db.get_duel_dig_attempts(CHAT_ID, 2, TODAY) == 1
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_item_events WHERE claimed = 0").fetchone()[0] == 1


def test_hit_insert_conflict_after_rng_does_not_charge_or_reroll(frozen_dig_date, temp_database):
    import database as db

    register(db)
    with sqlite3.connect(temp_database) as conn:
        conn.execute("""
            CREATE TRIGGER occupy_slot BEFORE INSERT ON duel_item_events
            BEGIN
                INSERT INTO duel_item_events (chat_id, item_id)
                VALUES (NEW.chat_id, 'rat_knuckle');
                SELECT RAISE(IGNORE);
            END
        """)
    roll = Mock(return_value=0.0)
    choice = Mock(return_value="vevangel_wing")
    assert db.try_duel_dig(CHAT_ID, 1, roll, choice)[0] == "active_event"
    roll.assert_called_once_with()
    choice.assert_called_once_with()
    assert points(db) == 50
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 0
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT item_id FROM duel_item_events WHERE claimed = 0").fetchone()[0] == "rat_knuckle"


def test_db_failure_after_event_insert_rolls_back_charge_and_event(frozen_dig_date, temp_database):
    import database as db

    register(db)
    with sqlite3.connect(temp_database) as conn:
        conn.execute("""
            CREATE TRIGGER reject_dig_charge BEFORE UPDATE OF points ON duel_users
            BEGIN SELECT RAISE(ABORT, 'charge failed'); END
        """)
    with pytest.raises(sqlite3.IntegrityError):
        db.try_duel_dig(CHAT_ID, 1, lambda: 0.0, lambda: "vevangel_wing")
    assert points(db) == 50
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 0
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_item_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_telegram_send_failure_refunds_points_attempt_and_active_slot(
    frozen_dig_date, fake_context, monkeypatch,
):
    import database as db
    from handlers import dig, duel_items

    register(db)
    monkeypatch.setattr(dig.random, "random", Mock(return_value=0.0))
    monkeypatch.setattr(dig.random, "choice", Mock(return_value=duel_items.DUEL_ITEMS[0]))
    fake_context.bot.send_message.side_effect = RuntimeError("Telegram unavailable")
    await dig.dig_command(update(user(1)), fake_context)
    assert points(db) == 50
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 0
    assert db.create_duel_item_event(CHAT_ID) is not None


def test_refund_transaction_rolls_back_if_attempt_cannot_be_decremented(
    frozen_dig_date, temp_database,
):
    import database as db

    register(db)
    status, found = db.try_duel_dig(CHAT_ID, 1, lambda: 0.0, lambda: "vevangel_wing")
    assert status == "found"
    with sqlite3.connect(temp_database) as conn:
        conn.execute("""
            CREATE TRIGGER reject_refund BEFORE UPDATE OF attempts ON duel_dig_daily
            BEGIN SELECT RAISE(ABORT, 'refund failed'); END
        """)
    with pytest.raises(sqlite3.IntegrityError):
        db.cancel_unpublished_duel_dig(found)
    assert points(db) == 40
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 1
    assert db.get_duel_item_event(found["event_id"])["claimed"] is False


@pytest.mark.asyncio
async def test_binding_failure_after_send_refunds_once_and_removes_button(
    frozen_dig_date, fake_context, monkeypatch,
):
    import database as db
    from handlers import dig, duel_items

    register(db)
    monkeypatch.setattr(dig.random, "random", Mock(return_value=0.0))
    monkeypatch.setattr(dig.random, "choice", Mock(return_value=duel_items.DUEL_ITEMS[0]))

    def bind_then_fail(event_id, message_id):
        assert db.set_duel_item_event_message(event_id, message_id)
        raise RuntimeError("late acknowledgement failure")

    monkeypatch.setattr(dig, "set_duel_item_event_message", bind_then_fail)
    await dig.dig_command(update(user(1)), fake_context)
    fake_context.bot.delete_message.assert_awaited_once()
    assert points(db) == 50
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 0
    event_id = int(fake_context.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.removeprefix("duel_item_claim_"))
    assert db.get_duel_item_event(event_id) is None
    assert db.cancel_unpublished_duel_dig({
        "event_id": event_id, "chat_id": CHAT_ID, "user_id": 1,
        "date_key": TODAY, "item_id": duel_items.DUEL_ITEMS[0]["id"],
    }) is False
    assert points(db) == 50


@pytest.mark.asyncio
async def test_scheduled_and_pocket_drop_see_dig_event_as_active(
    frozen_dig_date, fake_context, monkeypatch,
):
    import database as db
    from handlers import dig, duel, duel_items

    register(db, 1)
    register(db, 2)
    loser_item = db.add_duel_inventory_item(CHAT_ID, 2, "formangnome_whisker")
    monkeypatch.setattr(dig.random, "random", Mock(return_value=0.0))
    monkeypatch.setattr(dig.random, "choice", Mock(return_value=duel_items.DUEL_ITEMS[0]))
    await dig.dig_command(update(user(1)), fake_context)
    event_id = int(fake_context.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.removeprefix("duel_item_claim_"))
    monkeypatch.setattr(duel_items.random, "random", Mock(return_value=0.0))
    await duel_items._spawn_duel_item_event(fake_context, CHAT_ID)
    assert fake_context.bot.send_message.await_count == 1
    monkeypatch.setattr(duel.random, "random", Mock(return_value=0.0))
    monkeypatch.setattr(duel.random, "choice", Mock(side_effect=lambda values: values[0]))
    assert duel._maybe_drop_loser_inventory_item(CHAT_ID, 2) is None
    assert db.get_duel_inventory(CHAT_ID, 2)[0]["id"] == loser_item["id"]
    assert db.get_duel_item_event(event_id)["claimed"] is False


def test_base_item_id_is_rejected_without_charge(frozen_dig_date):
    import database as db

    register(db)
    with pytest.raises(ValueError, match="Permanent base items"):
        db.try_duel_dig(CHAT_ID, 1, lambda: 0.0, lambda: "knife")
    assert points(db) == 50
    assert db.get_duel_dig_attempts(CHAT_ID, 1, TODAY) == 0


def test_dig_uses_existing_equal_weight_catalog_and_chance():
    from config import DIG_FIND_CHANCE
    from handlers.duel_items import BASE_DUEL_ITEM_IDS, DUEL_ITEMS

    assert DIG_FIND_CHANCE == 0.20
    assert DUEL_ITEMS
    assert all(item["id"] not in BASE_DUEL_ITEM_IDS for item in DUEL_ITEMS)
