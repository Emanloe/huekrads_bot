import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def user(user_id, name=None):
    return SimpleNamespace(
        id=user_id, username=name or f"user{user_id}",
        first_name=name or f"User {user_id}", last_name=None, is_bot=False,
    )


def register(db, chat_id, user_id):
    db.get_or_create_duel_user(user(user_id), chat_id)


def published_event(db, chat_id, item_id="vevangel_wing", origin="Карман порвался"):
    event_id = db.create_duel_item_event(chat_id)
    assert db.set_duel_item_event_message(event_id, event_id + 500, origin)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE duel_item_events SET item_id = ?, published_at = ? WHERE event_id = ?",
            (item_id, 1000.0, event_id),
        )
    return event_id


def give_pet(db, chat_id, user_id):
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO huecrab_owners(chat_id, user_id) VALUES (?, ?)",
            (chat_id, user_id),
        )


def test_schema_migration_pet_isolation_and_nontransferable(temp_database):
    import database as db
    from handlers import duel
    register(db, -1, 1)
    register(db, -2, 1)
    give_pet(db, -1, 1)
    assert db.has_huecrab(-1, 1)
    assert not db.has_huecrab(-2, 1)
    assert db.get_duel_inventory(-1, 1) == []
    assert db.create_duel_item_event_from_inventory(-1, 1, 1) is None
    assert db.transfer_duel_inventory_item(-1, 1, 2, 1) is False
    assert duel._maybe_drop_loser_inventory_item(-1, 1) is None
    assert duel._maybe_steal_loser_inventory_item(-1, 2, 1) is None
    db.init_db()
    assert db.has_huecrab(-1, 1)
    with sqlite3.connect(temp_database) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(duel_item_events)")}
    assert {"published_at", "origin_text", "autoloot_claimed", "autoloot_announced_at"} <= columns


def test_existing_item_event_table_migrates_without_losing_events(tmp_path, monkeypatch):
    import database as db
    old_path = tmp_path / "old.db"
    with sqlite3.connect(old_path) as conn:
        conn.execute("""CREATE TABLE duel_item_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            message_id INTEGER, claimed INTEGER NOT NULL DEFAULT 0,
            claimed_by INTEGER, item_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, claimed_at TEXT
        )""")
        conn.execute("INSERT INTO duel_item_events (chat_id, message_id) VALUES (-99, 123)")
    monkeypatch.setattr(db, "DB_NAME", str(old_path))
    db.init_db()
    event = db.get_duel_item_event(1)
    assert event["chat_id"] == -99 and event["message_id"] == 123
    assert event["published_at"] is None  # no guessed publication timestamp
    db.init_db()
    assert db.get_duel_item_event(1) == event


@pytest.mark.asyncio
async def test_pet_appears_in_stats_not_collectible(temp_database, fake_context, monkeypatch):
    import database as db
    from handlers import duel
    register(db, -3, 1)
    give_pet(db, -3, 1)
    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    update = SimpleNamespace(message=SimpleNamespace(
        from_user=user(1), chat=SimpleNamespace(id=-3), chat_id=-3,
    ))
    await duel.duel_stats_command(update, fake_context)
    assert "Питомец: Хуекраб" in sent.await_args.args[2]
    assert db.get_duel_inventory(-3, 1) == []


@pytest.mark.asyncio
async def test_spawn_chance_daily_cap_and_chat_independence(temp_database, fake_context, monkeypatch):
    import database as db
    from handlers import huecrab
    huecrab.HUECRAB_DAILY_SPAWNS.clear()
    roll = Mock(return_value=0.0)
    monkeypatch.setattr(huecrab.random, "random", roll)
    await huecrab.spawn_huecrab_event(fake_context, -4)
    assert fake_context.bot.send_message.await_count == 1
    assert "Помахать дубиной" == fake_context.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text
    await huecrab.spawn_huecrab_event(fake_context, -4)
    assert roll.call_count == 1  # active event blocks another roll
    for count in range(1, 5):
        with db.get_db() as conn:
            conn.execute("UPDATE huecrab_events SET consumed = 1 WHERE chat_id = -4")
        await huecrab.spawn_huecrab_event(fake_context, -4)
        assert huecrab.HUECRAB_DAILY_SPAWNS[-4] == (date.today(), count + 1)
    with db.get_db() as conn:
        conn.execute("UPDATE huecrab_events SET consumed = 1 WHERE chat_id = -4")
    await huecrab.spawn_huecrab_event(fake_context, -4)
    assert fake_context.bot.send_message.await_count == 5
    await huecrab.spawn_huecrab_event(fake_context, -5)
    assert fake_context.bot.send_message.await_count == 6
    huecrab.HUECRAB_DAILY_SPAWNS.clear()


def test_tame_first_valid_attempt_one_roll_and_persistent_ownership(temp_database):
    import database as db
    from config import HUECRAB_EVENT_CHANCE, HUECRAB_TAME_CHANCE, HUECRAB_EVENT_DAILY_LIMIT
    assert (HUECRAB_EVENT_CHANCE, HUECRAB_TAME_CHANCE, HUECRAB_EVENT_DAILY_LIMIT) == (0.05, 0.5, 5)
    register(db, -6, 1)
    register(db, -6, 2)
    register(db, -7, 1)
    event_id = db.create_huecrab_event(-6)
    assert db.bind_huecrab_event(event_id, 99)
    roll = Mock(return_value=True)
    assert db.tame_huecrab_event(event_id, -7, 1, 99, roll) == "taken"
    assert db.tame_huecrab_event(event_id, -6, 3, 99, roll) == "not_registered"
    assert db.tame_huecrab_event(event_id, -6, 1, 99, roll) == "tamed"
    assert db.tame_huecrab_event(event_id, -6, 2, 99, roll) == "taken"
    roll.assert_called_once()
    assert db.has_huecrab(-6, 1) and not db.has_huecrab(-7, 1)
    db.init_db()
    assert db.has_huecrab(-6, 1)
    next_id = db.create_huecrab_event(-6)
    assert db.bind_huecrab_event(next_id, 100)
    assert db.tame_huecrab_event(next_id, -6, 1, 100, roll) == "already_owned"
    roll.assert_called_once()
    assert db.tame_huecrab_event(next_id, -6, 2, 100, lambda: False) == "failed"
    assert not db.has_huecrab(-6, 2)


def test_tame_race_consumes_event_once(temp_database):
    import database as db
    register(db, -8, 1)
    register(db, -8, 2)
    event_id = db.create_huecrab_event(-8)
    db.bind_huecrab_event(event_id, 91)
    roll = Mock(return_value=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda uid: db.tame_huecrab_event(event_id, -8, uid, 91, roll), (1, 2),
        ))
    assert sorted(results) == ["taken", "tamed"]
    roll.assert_called_once()
    assert sum(db.has_huecrab(-8, uid) for uid in (1, 2)) == 1


@pytest.mark.asyncio
async def test_tame_callback_uses_update_chat_and_edits_terminal_message(
    temp_database, fake_context, monkeypatch,
):
    import database as db
    from handlers import huecrab
    register(db, -9, 1)
    event_id = db.create_huecrab_event(-9)
    db.bind_huecrab_event(event_id, 99)
    roll = Mock(return_value=0.0)
    monkeypatch.setattr(huecrab.random, "random", roll)
    query = SimpleNamespace(
        data=f"huecrab_tame_{event_id}", from_user=user(1),
        message=SimpleNamespace(message_id=99), answer=AsyncMock(),
    )
    wrong_chat = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=-10))
    await huecrab.huecrab_tame_callback(wrong_chat, fake_context)
    query.answer.assert_awaited_once_with("Хуекраба уже спугнули.", show_alert=True)
    roll.assert_not_called()
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=-9))
    query.answer.reset_mock()
    await huecrab.huecrab_tame_callback(update, fake_context)
    query.answer.assert_awaited_once_with()
    roll.assert_called_once()
    assert db.has_huecrab(-9, 1)
    edit = fake_context.bot.edit_message_text.await_args.kwargs
    assert edit["chat_id"] == -9 and edit["message_id"] == 99
    assert edit["reply_markup"] is None and "Теперь у вас есть хуекраб" in edit["text"]


def test_autoloot_due_selection_chat_isolation_and_no_huegryz(temp_database):
    import database as db
    from config import HUECRAB_AUTOLOOT_DELAY_SECONDS
    assert HUECRAB_AUTOLOOT_DELAY_SECONDS == 20
    for chat, uid in ((-10, 1), (-10, 2), (-11, 3)):
        register(db, chat, uid)
        give_pet(db, chat, uid)
    event_id = published_event(db, -10)
    choose_owner = Mock(side_effect=lambda owners: owners[1])
    choose_item = Mock(side_effect=AssertionError("fixed item needs no choice"))
    assert db.list_due_huecrab_item_events(1019.9, 20) == []
    assert db.claim_due_item_for_huecrab(event_id, 1019.9, 20, choose_item, choose_owner)[0] == "unavailable"
    assert choose_owner.call_count == 0
    status, claim = db.claim_due_item_for_huecrab(event_id, 1020, 20, choose_item, choose_owner)
    assert status == "claimed" and claim["user_id"] == 2
    choose_owner.assert_called_once()
    choose_item.assert_not_called()
    assert db.get_duel_inventory(-10, 2)[0]["item_id"] == "vevangel_wing"
    assert db.get_duel_inventory(-11, 3) == []
    assert db.claim_due_item_for_huecrab(event_id, 1021, 20, choose_item, choose_owner)[0] == "unavailable"
    choose_owner.assert_called_once()
    assert db.get_duel_item_event(event_id)["claimed_by"] == 2


def test_autoloot_no_owner_or_manual_win_uses_zero_selection_rng(temp_database):
    import database as db
    register(db, -12, 1)
    event_id = published_event(db, -12)
    select = Mock(side_effect=AssertionError("must not select"))
    assert db.claim_due_item_for_huecrab(event_id, 1020, 20, lambda: "x", select)[0] == "no_owners"
    assert db.claim_duel_item_event(event_id, -12, 1, lambda: "x", lambda: False)[0] == "claimed"
    assert db.claim_due_item_for_huecrab(event_id, 1020, 20, lambda: "x", select)[0] == "unavailable"
    select.assert_not_called()


def test_single_owner_autoloot_uses_item_rng_but_no_owner_selection(temp_database):
    import database as db
    register(db, -17, 1)
    give_pet(db, -17, 1)
    event_id = db.create_duel_item_event(-17)
    db.set_duel_item_event_message(event_id, 700, "Находка")
    with db.get_db() as conn:
        conn.execute("UPDATE duel_item_events SET published_at = 1000 WHERE event_id = ?", (event_id,))
    item_selector = Mock(return_value="vevangel_wing")
    owner_selector = Mock(side_effect=AssertionError("one owner needs no RNG"))
    assert db.claim_due_item_for_huecrab(
        event_id, 1020, 20, item_selector, owner_selector,
    )[0] == "claimed"
    item_selector.assert_called_once()
    owner_selector.assert_not_called()
    assert db.get_duel_inventory(-17, 1)[0]["item_id"] == "vevangel_wing"


def test_autoloot_transaction_rolls_back_if_item_choice_fails(temp_database):
    import database as db
    register(db, -18, 1)
    give_pet(db, -18, 1)
    event_id = db.create_duel_item_event(-18)
    db.set_duel_item_event_message(event_id, 701, "Находка")
    with db.get_db() as conn:
        conn.execute("UPDATE duel_item_events SET published_at = 1000 WHERE event_id = ?", (event_id,))
    with pytest.raises(RuntimeError, match="choice failed"):
        db.claim_due_item_for_huecrab(
            event_id, 1020, 20,
            lambda: (_ for _ in ()).throw(RuntimeError("choice failed")),
            lambda owners: owners[0],
        )
    assert not db.get_duel_item_event(event_id)["claimed"]
    assert db.get_duel_inventory(-18, 1) == []


@pytest.mark.asyncio
async def test_manual_callback_after_autoloot_uses_already_claimed_response(
    temp_database, fake_context,
):
    import database as db
    from handlers import duel_items
    register(db, -19, 1)
    register(db, -19, 2)
    give_pet(db, -19, 1)
    event_id = published_event(db, -19)
    assert db.claim_due_item_for_huecrab(
        event_id, 1020, 20, lambda: "unused", lambda owners: owners[0],
    )[0] == "claimed"
    query = SimpleNamespace(
        data=f"duel_item_claim_{event_id}", from_user=user(2), answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=-19))
    await duel_items.duel_item_event_callback(update, fake_context)
    query.answer.assert_awaited_once_with("Уже утащили.", show_alert=True)
    assert db.get_duel_inventory(-19, 2) == []


def test_manual_auto_race_single_claim_and_no_auto_huegryz(temp_database):
    import database as db
    register(db, -13, 1)
    register(db, -13, 2)
    give_pet(db, -13, 2)
    event_id = published_event(db, -13)
    huegryz = Mock(return_value=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(db.claim_duel_item_event, event_id, -13, 1, lambda: "x", huegryz),
            pool.submit(db.claim_due_item_for_huecrab, event_id, 1020, 20, lambda: "x", random.choice),
        ]
        statuses = [future.result()[0] for future in futures]
    assert statuses.count("claimed") == 1
    assert len(db.get_duel_inventory(-13, 1) + db.get_duel_inventory(-13, 2)) == 1
    assert huegryz.call_count == int(db.get_duel_item_event(event_id)["claimed_by"] == 1)


@pytest.mark.asyncio
async def test_autoloot_worker_restart_and_pocket_origin_preserved(temp_database, fake_context, monkeypatch):
    import database as db
    from handlers import huecrab
    register(db, -14, 1)
    give_pet(db, -14, 1)
    event_id = published_event(db, -14, origin="<b>Карман порвался, выпало:</b> Крыло Вевангела")
    monkeypatch.setattr(huecrab, "time", lambda: 1020)
    await huecrab.huecrab_autoloot_job(fake_context)
    event = db.get_duel_item_event(event_id)
    assert event["claimed"] and event["autoloot_announced_at"]
    edit = fake_context.bot.edit_message_text.await_args.kwargs
    assert "Карман порвался" in edit["text"]
    assert "Хуекраб" in edit["text"] and "Крыло Вевангела" in edit["text"]
    assert edit["message_id"] == event_id + 500 and edit["reply_markup"] is None
    fake_context.bot.edit_message_text.reset_mock()
    db.init_db()  # simulate process restart and repeat worker scan
    await huecrab.huecrab_autoloot_job(fake_context)
    fake_context.bot.edit_message_text.assert_not_awaited()
    assert len(db.get_duel_inventory(-14, 1)) == 1


@pytest.mark.asyncio
async def test_announcement_retry_after_claim_commit_no_second_item(temp_database, fake_context, monkeypatch):
    import database as db
    from handlers import huecrab
    register(db, -15, 1)
    give_pet(db, -15, 1)
    event_id = published_event(db, -15)
    monkeypatch.setattr(huecrab, "time", lambda: 1020)
    fake_context.bot.edit_message_text.side_effect = RuntimeError("network")
    await huecrab.huecrab_autoloot_job(fake_context)
    assert db.get_duel_item_event(event_id)["autoloot_announced_at"] is None
    assert len(db.get_duel_inventory(-15, 1)) == 1
    fake_context.bot.edit_message_text.side_effect = None
    await huecrab.huecrab_autoloot_job(fake_context)
    assert db.get_duel_item_event(event_id)["autoloot_announced_at"]
    assert len(db.get_duel_inventory(-15, 1)) == 1


@pytest.mark.asyncio
async def test_restart_after_successful_edit_before_ack_is_idempotent(
    temp_database, fake_context, monkeypatch,
):
    import database as db
    from handlers import huecrab
    register(db, -20, 1)
    give_pet(db, -20, 1)
    event_id = published_event(db, -20)
    monkeypatch.setattr(huecrab, "time", lambda: 1020)
    original_ack = huecrab.mark_huecrab_claim_announced
    monkeypatch.setattr(huecrab, "mark_huecrab_claim_announced", Mock(side_effect=RuntimeError("crash")))
    await huecrab.huecrab_autoloot_job(fake_context)
    assert db.get_duel_item_event(event_id)["autoloot_announced_at"] is None
    fake_context.bot.edit_message_text.side_effect = RuntimeError("Message is not modified")
    monkeypatch.setattr(huecrab, "mark_huecrab_claim_announced", original_ack)
    db.init_db()
    await huecrab.huecrab_autoloot_job(fake_context)
    assert db.get_duel_item_event(event_id)["autoloot_announced_at"]
    assert len(db.get_duel_inventory(-20, 1)) == 1


def test_publication_timestamp_is_set_once_at_binding(temp_database):
    import database as db
    event_id = db.create_duel_item_event(-16)
    assert db.get_duel_item_event(event_id)["published_at"] is None
    assert db.set_duel_item_event_message(event_id, 111, "published")
    assert db.get_duel_item_event(event_id)["published_at"] is not None
    assert not db.set_duel_item_event_message(event_id, 222, "duplicate")


@pytest.mark.asyncio
async def test_all_three_sources_publish_then_autoloot_with_no_huegryz_rng(
    temp_database, fake_context, monkeypatch,
):
    import database as db
    from handlers import dig, duel, duel_items, huecrab

    for chat_id in (-21, -22, -23):
        register(db, chat_id, 1)
        give_pet(db, chat_id, 1)

    monkeypatch.setattr(duel_items.random, "random", lambda: 0.0)
    monkeypatch.setattr(duel_items.random, "choice", lambda values: values[0])
    await duel_items._spawn_duel_item_event(fake_context, -21)
    status, dig_found = db.try_duel_dig(
        -22, 1, lambda: 0.0, lambda: "formangnome_whisker",
    )
    assert status == "found"
    await dig._publish_dig_find(fake_context, dig_found)

    instance = db.add_duel_inventory_item(-23, 1, "vevangel_wing")
    drop = db.create_duel_item_event_from_inventory(-23, 1, instance["id"])
    await duel._publish_duel_drop(fake_context, drop)

    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT event_id, chat_id, origin_text, published_at FROM duel_item_events"
            " WHERE chat_id IN (-21, -22, -23) ORDER BY chat_id"
        ).fetchall()
        assert len(rows) == 3
        assert all(row[2] and row[3] for row in rows)
        conn.execute(
            "UPDATE duel_item_events SET published_at = 1000 WHERE chat_id IN (-21, -22, -23)"
        )

    def forbidden_huegryz_roll():
        raise AssertionError("auto-loot must not roll Huegryz")

    monkeypatch.setattr(huecrab.random, "random", forbidden_huegryz_roll)
    monkeypatch.setattr(huecrab, "time", lambda: 1020)
    await huecrab.huecrab_autoloot_job(fake_context)
    for event_id, chat_id, _, _ in rows:
        assert db.get_duel_item_event(event_id)["claimed_by"] == 1
        assert len(db.get_duel_inventory(chat_id, 1)) == 1
    assert any("Карман порвался" in call.kwargs["text"] for call in fake_context.bot.edit_message_text.await_args_list)
    assert all(call.kwargs["reply_markup"] is None for call in fake_context.bot.edit_message_text.await_args_list)
