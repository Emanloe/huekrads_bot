"""Production Huecrab rules on the miniapp branch's shared item claim."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import database as db
from config import (
    HUECRAB_AUTOLOOT_DELAY_SECONDS, HUECRAB_EVENT_CHANCE,
    HUECRAB_EVENT_DAILY_LIMIT, HUECRAB_TAME_CHANCE,
)
from handlers import duel, duel_items, huecrab


def user(user_id, name=None):
    return SimpleNamespace(
        id=user_id, username=name or f"user{user_id}",
        first_name=name or f"User {user_id}", last_name=None, is_bot=False,
    )


def register(chat_id, user_id):
    db.get_or_create_duel_user(user(user_id), chat_id)


def give_pet(chat_id, user_id):
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO huecrab_owners (chat_id, user_id) VALUES (?, ?)",
            (chat_id, user_id),
        )


def published_item(chat_id, *, item_id="vevangel_wing", origin="Находка"):
    event_id = db.create_duel_item_event(chat_id)
    assert db.set_duel_item_event_message(event_id, event_id + 500, origin)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE duel_item_events SET item_id = ?, published_at = 1000 WHERE event_id = ?",
            (item_id, event_id),
        )
    return event_id


def test_settings_schema_migration_and_chat_scoped_pet(temp_database):
    assert (HUECRAB_EVENT_CHANCE, HUECRAB_TAME_CHANCE) == (0.05, 0.5)
    assert (HUECRAB_EVENT_DAILY_LIMIT, HUECRAB_AUTOLOOT_DELAY_SECONDS) == (5, 20)
    register(-101, 1)
    register(-102, 1)
    give_pet(-101, 1)
    assert db.has_huecrab(-101, 1) and not db.has_huecrab(-102, 1)
    with pytest.raises(sqlite3.IntegrityError):
        give_pet(-101, 1)
    db.init_db()
    assert db.has_huecrab(-101, 1)
    with sqlite3.connect(temp_database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(duel_item_events)")}
    assert {"published_at", "origin_text", "autoloot_claimed", "autoloot_announced_at"} <= columns


def test_old_item_event_schema_migrates_without_guessing_send_time(tmp_path, monkeypatch):
    old_path = tmp_path / "old.db"
    with sqlite3.connect(old_path) as conn:
        conn.execute("""CREATE TABLE duel_item_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            message_id INTEGER, claimed INTEGER NOT NULL DEFAULT 0,
            claimed_by INTEGER, item_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, claimed_at TEXT
        )""")
        conn.execute("INSERT INTO duel_item_events (chat_id, message_id) VALUES (-1, 88)")
    monkeypatch.setattr(db, "DB_NAME", str(old_path))
    db.init_db()
    event = db.get_duel_item_event(1)
    assert event["message_id"] == 88 and event["published_at"] is None
    db.init_db()
    assert db.get_duel_item_event(1) == event


def test_existing_main_huecrab_tables_survive_miniapp_init(tmp_path, monkeypatch):
    main_path = tmp_path / "main.db"
    with sqlite3.connect(main_path) as conn:
        conn.execute("""CREATE TABLE duel_item_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            message_id INTEGER, claimed INTEGER NOT NULL DEFAULT 0,
            claimed_by INTEGER, item_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, claimed_at TEXT,
            published_at REAL, origin_text TEXT,
            autoloot_claimed INTEGER NOT NULL DEFAULT 0, autoloot_announced_at TEXT
        )""")
        conn.execute("""CREATE TABLE huecrab_owners (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            obtained_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, user_id)
        )""")
        conn.execute("INSERT INTO huecrab_owners (chat_id, user_id) VALUES (-1, 7)")
        conn.execute("""INSERT INTO duel_item_events
            (chat_id, message_id, item_id, published_at, origin_text)
            VALUES (-1, 42, 'vevangel_wing', 1000, 'Старый предмет')""")
    monkeypatch.setattr(db, "DB_NAME", str(main_path))
    db.init_db()
    assert db.has_huecrab(-1, 7)
    event = db.get_duel_item_event(1)
    assert event["published_at"] == 1000 and event["origin_text"] == "Старый предмет"
    db.init_db()
    assert db.has_huecrab(-1, 7)


def test_pet_is_absent_from_transferable_pool(temp_database):
    register(-103, 1)
    give_pet(-103, 1)
    assert db.get_duel_inventory(-103, 1) == []
    assert duel._maybe_drop_loser_inventory_item(-103, 1) is None
    assert duel._maybe_steal_loser_inventory_item(-103, 2, 1) is None
    assert db.create_duel_item_event_from_inventory(-103, 1, 1) is None
    assert not db.transfer_duel_inventory_item(-103, 1, 2, 1)


@pytest.mark.asyncio
async def test_pet_appears_in_telegram_loot(temp_database, fake_context, monkeypatch):
    register(-104, 1)
    give_pet(-104, 1)
    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    update = SimpleNamespace(message=SimpleNamespace(
        from_user=user(1), chat=SimpleNamespace(id=-104), chat_id=-104,
    ))
    await duel.duel_stats_command(update, fake_context)
    assert "Питомец: Хуекраб" in sent.await_args.args[2]
    assert db.get_duel_inventory(-104, 1) == []


@pytest.mark.asyncio
async def test_spawn_chance_daily_limit_and_independent_chats(
    temp_database, fake_context, monkeypatch,
):
    huecrab.HUECRAB_DAILY_SPAWNS.clear()
    roll = Mock(return_value=0.0)
    monkeypatch.setattr(huecrab.random, "random", roll)
    await huecrab.spawn_huecrab_event(fake_context, -105)
    await huecrab.spawn_huecrab_event(fake_context, -105)
    assert roll.call_count == 1  # active event suppresses a new spawn decision
    for count in range(1, 5):
        with db.get_db() as conn:
            conn.execute("UPDATE huecrab_events SET consumed = 1 WHERE chat_id = -105")
        await huecrab.spawn_huecrab_event(fake_context, -105)
        assert huecrab.HUECRAB_DAILY_SPAWNS[-105] == (date.today(), count + 1)
    with db.get_db() as conn:
        conn.execute("UPDATE huecrab_events SET consumed = 1 WHERE chat_id = -105")
    await huecrab.spawn_huecrab_event(fake_context, -105)
    await huecrab.spawn_huecrab_event(fake_context, -106)
    assert fake_context.bot.send_message.await_count == 6
    button = fake_context.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Помахать дубиной"
    huecrab.HUECRAB_DAILY_SPAWNS.clear()


def test_tame_valid_invalid_failure_and_chat_isolation(temp_database):
    register(-107, 1)
    register(-107, 2)
    register(-108, 1)
    event_id = db.create_huecrab_event(-107)
    assert db.bind_huecrab_event(event_id, 99)
    decision = Mock(return_value=True)
    assert db.tame_huecrab_event(event_id, -108, 1, 99, decision) == "taken"
    assert db.tame_huecrab_event(event_id, -107, 3, 99, decision) == "not_registered"
    assert db.tame_huecrab_event(event_id, -107, 1, 99, decision) == "tamed"
    assert db.tame_huecrab_event(event_id, -107, 2, 99, decision) == "taken"
    decision.assert_called_once()
    assert db.has_huecrab(-107, 1) and not db.has_huecrab(-108, 1)
    next_id = db.create_huecrab_event(-107)
    assert db.bind_huecrab_event(next_id, 100)
    assert db.tame_huecrab_event(next_id, -107, 1, 100, decision) == "already_owned"
    decision.assert_called_once()
    assert db.tame_huecrab_event(next_id, -107, 2, 100, lambda: False) == "failed"
    assert not db.has_huecrab(-107, 2)


def test_concurrent_tame_has_one_roll_and_one_owner(temp_database):
    register(-109, 1)
    register(-109, 2)
    event_id = db.create_huecrab_event(-109)
    db.bind_huecrab_event(event_id, 99)
    decision = Mock(return_value=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda uid: db.tame_huecrab_event(event_id, -109, uid, 99, decision), (1, 2),
        ))
    assert sorted(results) == ["taken", "tamed"]
    decision.assert_called_once()
    assert sum(db.has_huecrab(-109, uid) for uid in (1, 2)) == 1


@pytest.mark.asyncio
async def test_tame_callback_uses_update_chat_and_terminal_edit(
    temp_database, fake_context, monkeypatch,
):
    register(-110, 1)
    event_id = db.create_huecrab_event(-110)
    db.bind_huecrab_event(event_id, 99)
    roll = Mock(return_value=0.0)
    monkeypatch.setattr(huecrab.random, "random", roll)
    query = SimpleNamespace(
        data=f"huecrab_tame_{event_id}", from_user=user(1),
        message=SimpleNamespace(message_id=99), answer=AsyncMock(),
    )
    await huecrab.huecrab_tame_callback(
        SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=-111)),
        fake_context,
    )
    roll.assert_not_called()
    await huecrab.huecrab_tame_callback(
        SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=-110)),
        fake_context,
    )
    roll.assert_called_once()
    assert db.has_huecrab(-110, 1)
    edit = fake_context.bot.edit_message_text.await_args.kwargs
    assert edit["reply_markup"] is None and "Теперь у вас есть хуекраб" in edit["text"]


def test_due_item_owner_selection_and_no_huegryz(temp_database):
    for chat, uid in ((-112, 1), (-112, 2), (-113, 3)):
        register(chat, uid)
        give_pet(chat, uid)
    event_id = published_item(-112)
    owner_selector = Mock(side_effect=lambda owners: owners[1])
    item_selector = Mock(side_effect=AssertionError("fixed item rerolled"))
    assert db.list_due_huecrab_item_events(1019.9, 20) == []
    assert db.claim_due_item_for_huecrab(
        event_id, 1019.9, 20, item_selector, owner_selector,
    )[0] == "unavailable"
    assert db.claim_due_item_for_huecrab(
        event_id, 1020, 20, item_selector, owner_selector,
    )[0] == "claimed"
    owner_selector.assert_called_once()
    item_selector.assert_not_called()
    assert len(db.get_duel_inventory(-112, 2)) == 1
    assert db.get_duel_inventory(-113, 3) == []
    assert db.claim_due_item_for_huecrab(
        event_id, 1020, 20, item_selector, owner_selector,
    )[0] == "unavailable"
    owner_selector.assert_called_once()


def test_no_owner_one_owner_and_manual_first_rng_rules(temp_database):
    register(-114, 1)
    event_id = published_item(-114)
    select = Mock(side_effect=AssertionError("owner selection must not roll"))
    assert db.claim_due_item_for_huecrab(event_id, 1020, 20, lambda: None, select)[0] == "no_owners"
    give_pet(-114, 1)
    assert db.claim_due_item_for_huecrab(event_id, 1020, 20, lambda: None, select)[0] == "claimed"
    select.assert_not_called()
    second_id = published_item(-114)
    assert db.claim_duel_item_event(second_id, -114, 1, lambda: "x", lambda: False)[0] == "claimed"
    assert db.claim_due_item_for_huecrab(second_id, 1020, 20, lambda: None, select)[0] == "unavailable"
    select.assert_not_called()


def test_manual_vs_auto_claim_is_atomic(temp_database):
    register(-115, 1)
    register(-115, 2)
    give_pet(-115, 2)
    event_id = published_item(-115)
    huegryz = Mock(return_value=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(db.claim_duel_item_event, event_id, -115, 1, lambda: "x", huegryz),
            pool.submit(db.claim_due_item_for_huecrab, event_id, 1020, 20, lambda: "x", lambda owners: owners[0]),
        ]
        statuses = [future.result()[0] for future in futures]
    assert statuses.count("claimed") == 1
    assert len(db.get_duel_inventory(-115, 1) + db.get_duel_inventory(-115, 2)) == 1
    assert huegryz.call_count == int(db.get_duel_item_event(event_id)["claimed_by"] == 1)


@pytest.mark.asyncio
async def test_manual_button_after_auto_claim_is_already_claimed(temp_database, fake_context):
    register(-116, 1)
    register(-116, 2)
    give_pet(-116, 1)
    event_id = published_item(-116)
    assert db.claim_due_item_for_huecrab(
        event_id, 1020, 20, lambda: "x", lambda owners: owners[0],
    )[0] == "claimed"
    query = SimpleNamespace(
        data=f"duel_item_claim_{event_id}", from_user=user(2), answer=AsyncMock(),
    )
    await duel_items.duel_item_event_callback(
        SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=-116)),
        fake_context,
    )
    query.answer.assert_awaited_once_with("Уже утащили.", show_alert=True)
    assert db.get_duel_inventory(-116, 2) == []


def test_item_choice_failure_rolls_back_auto_claim(temp_database):
    register(-117, 1)
    give_pet(-117, 1)
    event_id = db.create_duel_item_event(-117)
    db.set_duel_item_event_message(event_id, 77, "Находка")
    with db.get_db() as conn:
        conn.execute("UPDATE duel_item_events SET published_at = 1000 WHERE event_id = ?", (event_id,))
    with pytest.raises(RuntimeError, match="choice failed"):
        db.claim_due_item_for_huecrab(
            event_id, 1020, 20,
            lambda: (_ for _ in ()).throw(RuntimeError("choice failed")),
            lambda owners: owners[0],
        )
    assert not db.get_duel_item_event(event_id)["claimed"]
    assert db.get_duel_inventory(-117, 1) == []


@pytest.mark.asyncio
async def test_scheduled_and_dig_items_start_clock_only_after_send(
    temp_database, fake_context, monkeypatch,
):
    from handlers import dig

    for chat in (-118, -119):
        register(chat, 1)
        give_pet(chat, 1)
    monkeypatch.setattr(duel_items.random, "random", lambda: 0.0)
    monkeypatch.setattr(duel_items.random, "choice", lambda values: values[0])
    await duel_items._spawn_duel_item_event(fake_context, -118)
    found, dig_data = db.try_duel_dig(
        -119, 1, lambda: 0.0, lambda: "formangnome_whisker",
    )
    assert found == "found"
    await dig._publish_dig_find(fake_context, dig_data)
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT event_id, chat_id, origin_text, published_at FROM duel_item_events"
            " WHERE chat_id IN (-118, -119) ORDER BY chat_id"
        ).fetchall()
        assert len(rows) == 2 and all(row[2] and row[3] for row in rows)
        conn.execute("UPDATE duel_item_events SET published_at = 1000 WHERE chat_id IN (-118, -119)")
    monkeypatch.setattr(huecrab.random, "random", Mock(side_effect=AssertionError("Huegryz rolled")))
    monkeypatch.setattr(huecrab, "time", lambda: 1020)
    await huecrab.huecrab_autoloot_job(fake_context)
    for event_id, chat_id, _, _ in rows:
        assert db.get_duel_item_event(event_id)["claimed_by"] == 1
        assert len(db.get_duel_inventory(chat_id, 1)) == 1
    assert fake_context.bot.edit_message_text.await_count == 2
    assert all(call.kwargs["reply_markup"] is None for call in
               fake_context.bot.edit_message_text.await_args_list)


@pytest.mark.asyncio
async def test_autoloot_edit_retry_after_restart_does_not_reclaim(
    temp_database, fake_context, monkeypatch,
):
    register(-120, 1)
    give_pet(-120, 1)
    event_id = published_item(-120, origin="<b>Находка</b>")
    monkeypatch.setattr(huecrab, "time", lambda: 1020)
    fake_context.bot.edit_message_text.side_effect = RuntimeError("temporary outage")
    await huecrab.huecrab_autoloot_job(fake_context)
    assert len(db.get_duel_inventory(-120, 1)) == 1
    assert db.get_duel_item_event(event_id)["autoloot_announced_at"] is None
    db.init_db()  # new process reopens the same SQLite file
    fake_context.bot.edit_message_text.side_effect = None
    await huecrab.huecrab_autoloot_job(fake_context)
    assert db.get_duel_item_event(event_id)["autoloot_announced_at"]
    assert len(db.get_duel_inventory(-120, 1)) == 1
    fake_context.bot.edit_message_text.reset_mock()
    await huecrab.huecrab_autoloot_job(fake_context)
    fake_context.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_succeeded_before_ack_retry_is_idempotent(
    temp_database, fake_context, monkeypatch,
):
    register(-121, 1)
    give_pet(-121, 1)
    event_id = published_item(-121)
    monkeypatch.setattr(huecrab, "time", lambda: 1020)
    original_ack = huecrab.mark_huecrab_claim_announced
    monkeypatch.setattr(
        huecrab, "mark_huecrab_claim_announced", Mock(side_effect=RuntimeError("ack failed")),
    )
    await huecrab.huecrab_autoloot_job(fake_context)
    assert db.get_duel_item_event(event_id)["autoloot_announced_at"] is None
    monkeypatch.setattr(huecrab, "mark_huecrab_claim_announced", original_ack)
    fake_context.bot.edit_message_text.side_effect = RuntimeError("Message is not modified")
    await huecrab.huecrab_autoloot_job(fake_context)
    assert db.get_duel_item_event(event_id)["autoloot_announced_at"]
    assert len(db.get_duel_inventory(-121, 1)) == 1


@pytest.mark.asyncio
async def test_persistent_pocket_publication_then_autoloot_preserves_origin_owner_and_rng(
    temp_database, fake_context, monkeypatch,
):
    from handlers import duel_service
    from handlers.persistent_duel_publisher import publish_persistent_duel_outbox
    from tests.test_persistent_duel_outbox import (
        CHAT, NOW, finish_and_get_final, install_rng, mark_final_published,
    )

    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    item = db.add_duel_inventory_item(CHAT, 2, "po_lochki")
    give_pet(CHAT, 3)
    mark_final_published(CHAT, final_pub)
    trace = install_rng(monkeypatch, [0.0])
    dropped = duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    event_id = dropped.drop["event_id"]
    assert trace.trace == [("random", 0.0), ("choice", (item["item_id"],))]
    assert db.get_duel_item_event(event_id)["published_at"] is None
    assert db.list_due_huecrab_item_events((NOW + 200_000) / 1000, 20) == []

    sent = await publish_persistent_duel_outbox(
        CHAT, dropped.publication["id"], fake_context.bot,
        claim_time_ms=NOW + 5, published_at_ms=NOW + 6,
    )
    assert sent.reason == "delivered"
    event = db.get_duel_item_event(event_id)
    assert event["published_at"] == (NOW + 6) / 1000
    assert "Карман порвался" in event["origin_text"]
    assert "player2" in event["origin_text"]
    assert duel_items.get_duel_item_name(item["item_id"]) in event["origin_text"]
    assert db.list_due_huecrab_item_events((NOW + 20_005) / 1000, 20) == []
    monkeypatch.setattr(huecrab.random, "random", Mock(side_effect=AssertionError("Huegryz rolled")))
    monkeypatch.setattr(huecrab, "time", lambda: (NOW + 20_006) / 1000)
    await huecrab.huecrab_autoloot_job(fake_context)
    edit = fake_context.bot.edit_message_text.await_args.kwargs
    assert "Карман порвался" in edit["text"]
    assert "player2" in edit["text"]  # original owner from immutable snapshot
    assert "player3" in edit["text"]  # pet owner
    assert duel_items.get_duel_item_name(item["item_id"]) in edit["text"]
    assert edit["message_id"] == event["message_id"]
    assert edit["reply_markup"] is None
    assert db.get_duel_item_event(event_id)["claimed_by"] == 3
    assert db.get_duel_inventory(CHAT, 3)[0]["item_id"] == item["item_id"]
    assert trace.trace == [("random", 0.0), ("choice", (item["item_id"],))]


@pytest.mark.asyncio
async def test_persistent_pocket_send_failure_never_arms_autoloot_and_compensates(
    temp_database, fake_context, monkeypatch,
):
    from handlers import duel_service
    from handlers.persistent_duel_publisher import publish_persistent_duel_outbox
    from tests.test_persistent_duel_outbox import (
        CHAT, NOW, finish_and_get_final, install_rng, mark_final_published,
    )

    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    item = db.add_duel_inventory_item(CHAT, 2, "po_lochki")
    give_pet(CHAT, 3)
    mark_final_published(CHAT, final_pub)
    trace = install_rng(monkeypatch, [0.0])
    dropped = duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    before = list(trace.trace)
    fake_context.bot.send_message.side_effect = RuntimeError("Telegram unavailable")
    assert (await publish_persistent_duel_outbox(
        CHAT, dropped.publication["id"], fake_context.bot, claim_time_ms=NOW + 5,
    )).reason == "send_failed"
    assert db.get_duel_item_event(dropped.drop["event_id"])["published_at"] is None
    assert db.list_due_huecrab_item_events((NOW + 200_000) / 1000, 20) == []
    compensated = duel_service.compensate_persistent_duel_drop(CHAT, finished.session["id"])
    assert compensated.reason == "compensated"
    assert db.get_duel_item_event(dropped.drop["event_id"]) is None
    assert db.get_duel_inventory(CHAT, 2)[0]["id"] == item["id"]
    assert trace.trace == before


@pytest.mark.asyncio
async def test_pocket_message_checkpoint_retry_arms_clock_once_without_resend(
    temp_database, fake_context, monkeypatch,
):
    from handlers import duel_service, persistent_duel_publisher as publisher
    from tests.test_persistent_duel_outbox import (
        CHAT, NOW, finish_and_get_final, install_rng, mark_final_published,
    )
    from duel_outbox_repository import get_duel_publication

    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    db.add_duel_inventory_item(CHAT, 2, "po_lochki")
    give_pet(CHAT, 3)
    mark_final_published(CHAT, final_pub)
    trace = install_rng(monkeypatch, [0.0])
    dropped = duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    event_id = dropped.drop["event_id"]
    original_ack = publisher.acknowledge_persistent_duel_publication
    monkeypatch.setattr(
        publisher, "acknowledge_persistent_duel_publication",
        Mock(side_effect=RuntimeError("SQLite unavailable")),
    )
    first = await publisher.publish_persistent_duel_outbox(
        CHAT, dropped.publication["id"], fake_context.bot, claim_time_ms=NOW + 5,
    )
    assert first.reason == "ack_failed"
    assert fake_context.bot.send_message.await_count == 1
    assert get_duel_publication(CHAT, dropped.publication["id"])["message_id"] == 101
    assert db.get_duel_item_event(event_id)["published_at"] is None
    assert db.list_due_huecrab_item_events((NOW + 200_000) / 1000, 20) == []
    monkeypatch.setattr(publisher, "acknowledge_persistent_duel_publication", original_ack)
    retry = await publisher.publish_persistent_duel_outbox(
        CHAT, dropped.publication["id"], fake_context.bot,
        claim_time_ms=NOW + 60_006, published_at_ms=NOW + 60_007,
    )
    assert retry.reason == "delivered"
    assert fake_context.bot.send_message.await_count == 1
    event = db.get_duel_item_event(event_id)
    assert event["published_at"] == (NOW + 60_007) / 1000
    assert "Карман порвался" in event["origin_text"]
    assert (await publisher.publish_persistent_duel_outbox(
        CHAT, dropped.publication["id"], fake_context.bot,
    )).reason == "not_retryable"
    assert db.get_duel_item_event(event_id)["published_at"] == event["published_at"]
    assert trace.trace == [("random", 0.0), ("choice", ("po_lochki",))]


def test_pocket_item_bind_and_outbox_delivery_roll_back_together(
    temp_database, monkeypatch,
):
    from duel_outbox_repository import claim_duel_publication, get_duel_publication
    from handlers import duel_service
    from tests.test_persistent_duel_outbox import (
        CHAT, NOW, finish_and_get_final, install_rng, mark_final_published,
    )

    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    db.add_duel_inventory_item(CHAT, 2, "po_lochki")
    mark_final_published(CHAT, final_pub)
    install_rng(monkeypatch, [0.0])
    dropped = duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    event_id = dropped.drop["event_id"]
    lease = claim_duel_publication(CHAT, dropped.publication["id"], now_ms=NOW + 5)
    original_mark = duel_service.mark_duel_publication_delivered_in_transaction
    monkeypatch.setattr(
        duel_service, "mark_duel_publication_delivered_in_transaction", Mock(return_value=False),
    )
    with pytest.raises(RuntimeError, match="could not be acknowledged"):
        duel_service.acknowledge_persistent_duel_publication(
            CHAT, dropped.publication["id"], lease["attempt_count"], 101, NOW + 6,
        )
    event = db.get_duel_item_event(event_id)
    assert event["message_id"] is None and event["published_at"] is None
    assert get_duel_publication(CHAT, dropped.publication["id"])["status"] == "leased"
    monkeypatch.setattr(duel_service, "mark_duel_publication_delivered_in_transaction", original_mark)
    ack = duel_service.acknowledge_persistent_duel_publication(
        CHAT, dropped.publication["id"], lease["attempt_count"], 101, NOW + 7,
    )
    assert ack.reason == "delivered"
    assert db.get_duel_item_event(event_id)["published_at"] == (NOW + 7) / 1000
