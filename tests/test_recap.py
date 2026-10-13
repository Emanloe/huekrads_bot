from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database as db
import handlers.recap as recap
from handlers.recap import (
    TELEGRAM_SAFE_TEXT_LIMIT,
    build_recap_text,
    game_day_bounds_ms,
    recap_command,
)


CHAT = -700
OTHER_CHAT = -701


def user(user_id, username=None):
    return SimpleNamespace(
        id=user_id, username=username or f"user{user_id}",
        first_name=f"User {user_id}", last_name=None, is_bot=False,
    )


def register(user_id, chat_id=CHAT):
    return db.get_or_create_duel_user(user(user_id), chat_id)


def event(kind, data, source=None, occurred_at=1):
    return {
        "event_type": kind, "data": data, "source_id": source,
        "occurred_at": occurred_at,
    }


def test_game_day_uses_moscow_midnight():
    start, end = game_day_bounds_ms(datetime(2026, 1, 2, 22, 30, tzinfo=timezone.utc))
    assert datetime.fromtimestamp(start / 1000, timezone.utc) == datetime(
        2026, 1, 2, 21, 0, tzinfo=timezone.utc,
    )
    assert end - start == 86_400_000


def test_event_query_is_scoped_to_day_chat_and_sender(temp_database):
    start, end = game_day_bounds_ms(datetime(2026, 5, 3, 12, tzinfo=timezone.utc))
    db.record_game_event(CHAT, 1, "huecrab_tamed", {}, source_id="a", occurred_at_ms=start)
    db.record_game_event(CHAT, 1, "huecrab_tamed", {}, source_id="b", occurred_at_ms=end - 1)
    db.record_game_event(CHAT, 1, "huecrab_tamed", {}, source_id="old", occurred_at_ms=start - 1)
    db.record_game_event(OTHER_CHAT, 1, "huecrab_tamed", {}, source_id="other-chat", occurred_at_ms=start)
    db.record_game_event(CHAT, 2, "huecrab_tamed", {}, source_id="other-user", occurred_at_ms=start)

    rows = db.list_game_events(CHAT, 1, start, end)
    assert [row["source_id"] for row in rows] == ["a", "b"]


def test_event_source_makes_reprocessing_idempotent(temp_database):
    assert db.record_game_event(CHAT, 1, "item_received", {"item_id": "x"}, source_id="item:9")
    assert not db.record_game_event(CHAT, 1, "item_received", {"item_id": "x"}, source_id="item:9")
    assert len(db.list_game_events(CHAT, 1, 0, 10**15)) == 1


def test_dig_claim_logs_receipt_and_missed_find_atomically(temp_database):
    register(1)
    register(2)
    status, dug = db.try_duel_dig(CHAT, 1, lambda: 0, lambda: "odd_relic")
    assert status == "found"
    assert db.set_duel_item_event_message(dug["event_id"], 50, "found")

    status, item = db.claim_duel_item_event(
        dug["event_id"], CHAT, 2, lambda: "unused",
    )
    assert status == "claimed" and item["item_id"] == "odd_relic"
    finder = db.list_game_events(CHAT, 1, 0, 10**15)
    claimant = db.list_game_events(CHAT, 2, 0, 10**15)
    assert [row["event_type"] for row in finder] == ["item_found", "item_missed"]
    assert claimant[0]["event_type"] == "item_received"
    assert claimant[0]["data"]["method"] == "claim"


def test_failed_unpublished_dig_removes_provisional_history(temp_database):
    register(1)
    status, dug = db.try_duel_dig(CHAT, 1, lambda: 0, lambda: "odd_relic")
    assert status == "found"
    assert db.cancel_unpublished_duel_dig(dug)
    assert db.list_game_events(CHAT, 1, 0, 10**15) == []


def test_two_identical_lures_are_two_lost_instances(temp_database):
    register(1)
    with db.get_db() as conn:
        conn.executemany(
            "INSERT INTO duel_inventory (chat_id, user_id, item_id) VALUES (?, ?, ?)",
            [(CHAT, 1, "same_relic"), (CHAT, 1, "same_relic")],
        )
    for _ in range(2):
        status, item = db.consume_random_unequipped_duel_inventory_item(
            CHAT, 1, lambda rows: rows[0],
        )
        assert status == "consumed" and item["item_id"] == "same_relic"
    rows = db.list_game_events(CHAT, 1, 0, 10**15)
    assert len(rows) == 2
    assert len({row["source_id"] for row in rows}) == 2


def test_huecrab_history_only_records_successful_taming(temp_database):
    register(1)
    failed = db.create_huecrab_event(CHAT)
    assert db.bind_huecrab_event(failed, 10)
    assert db.tame_huecrab_event(failed, CHAT, 1, 10, lambda: False) == "failed"
    assert db.list_game_events(CHAT, 1, 0, 10**15) == []

    successful = db.create_huecrab_event(CHAT)
    assert db.bind_huecrab_event(successful, 11)
    assert db.tame_huecrab_event(successful, CHAT, 1, 11, lambda: True) == "tamed"
    assert [row["event_type"] for row in db.list_game_events(CHAT, 1, 0, 10**15)] == [
        "huecrab_tamed",
    ]


def test_native_pickup_and_pet_timestamps_are_available_as_legacy_fallbacks(temp_database):
    register(1)
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO duel_item_events
               (chat_id, claimed, claimed_by, item_id, claimed_at, autoloot_claimed)
               VALUES (?, 1, ?, ?, CURRENT_TIMESTAMP, 1)""",
            (CHAT, 1, "old_item"),
        )
        event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO huecrab_owners (chat_id, user_id) VALUES (?, ?)",
            (CHAT, 1),
        )
    start, end = game_day_bounds_ms()
    assert db.list_item_receipts_for_recap(CHAT, 1, start, end) == [{
        "item_event_id": event_id, "item_id": "old_item",
        "method": "huecrab",
        "occurred_at": pytest.approx(int(datetime.now(timezone.utc).timestamp()) * 1000, abs=2000),
    }]
    assert db.get_huecrab_tame_for_recap(CHAT, 1, start, end) is not None


def test_recap_aggregates_duel_attacks_without_individual_results(temp_database):
    events = [
        event("duel_finished", {
            "initiator_user_id": 1, "initiator_name": "Me",
            "opponent_user_id": 2, "opponent_name": "<b>Evil</b>",
            "winner_user_id": 2, "winner_name": "<b>Evil</b>",
            "loser_user_id": 1, "loser_name": "Me",
            "winner_points_delta": 10, "loser_points_delta": -10,
            "is_dick_stolen": False, "rounds": 2, "stolen_item": None,
        }),
        event("duel_finished", {
            "initiator_user_id": 2, "initiator_name": "Enemy",
            "opponent_user_id": 1, "opponent_name": "Me",
            "winner_user_id": 1, "winner_name": "Me",
            "loser_user_id": 2, "loser_name": "Enemy",
            "winner_points_delta": 8, "loser_points_delta": -8,
            "is_dick_stolen": True, "rounds": 1, "stolen_item": None,
        }),
    ]
    text = build_recap_text(events, 1)
    assert "побед 1, поражений 1" in text
    assert "украл хуй у Enemy" in text
    assert "Ты нападал на &lt;b&gt;Evil&lt;/b&gt; — 1 раз" in text
    assert "На тебя нападал &lt;b&gt;Evil&lt;/b&gt; — 1 раз" in text
    assert text.count("Твои победы: 1 · Победы соперника: 1") == 1
    assert "&lt;b&gt;Evil&lt;/b&gt;" in text
    assert "победа:" not in text
    assert "поражение:" not in text
    assert "очки" not in text
    assert "раунд" not in text
    assert "+8" not in text and "-10" not in text


def test_recap_combines_repeated_attacks_by_the_same_player(temp_database):
    duel = {
        "initiator_user_id": 2, "initiator_name": "Гобелен",
        "opponent_user_id": 1, "opponent_name": "Мой гном",
        "winner_user_id": 1, "winner_name": "Мой гном",
        "loser_user_id": 2, "loser_name": "Гобелен",
        "winner_points_delta": 10, "loser_points_delta": -10,
        "is_dick_stolen": False, "rounds": 3, "stolen_item": None,
    }
    text = build_recap_text([event("duel_finished", duel), event("duel_finished", duel)], 1)
    assert "На тебя нападал Гобелен — 2 раза" in text
    assert text.count("На тебя нападал Гобелен") == 1
    assert text.count("Твои победы: 2 · Победы соперника: 0") == 1


def test_recap_counts_all_initiator_and_winner_combinations_for_one_opponent(temp_database):
    def duel(initiator_id, winner_id):
        return event("duel_finished", {
            "initiator_user_id": initiator_id,
            "initiator_name": "Мой гном" if initiator_id == 1 else "Соперник",
            "opponent_user_id": 2 if initiator_id == 1 else 1,
            "opponent_name": "Соперник" if initiator_id == 1 else "Мой гном",
            "winner_user_id": winner_id,
            "winner_name": "Мой гном" if winner_id == 1 else "Соперник",
            "loser_user_id": 2 if winner_id == 1 else 1,
            "loser_name": "Соперник" if winner_id == 1 else "Мой гном",
            "is_dick_stolen": False, "stolen_item": None,
        })

    text = build_recap_text([
        duel(1, 1), duel(1, 2), duel(2, 2), duel(2, 1),
    ], 1)
    assert "Ты нападал на Соперник — 2 раза" in text
    assert "На тебя нападал Соперник — 2 раза" in text
    assert text.count("Твои победы: 2 · Победы соперника: 2") == 1
    assert "Дуэли: побед 2, поражений 2" in text


def test_recap_keeps_same_named_opponents_separate_by_user_id(temp_database):
    first = event("duel_finished", {
        "initiator_user_id": 2, "initiator_name": "Одинаковый",
        "opponent_user_id": 1, "opponent_name": "Мой гном",
        "winner_user_id": 1, "winner_name": "Мой гном",
        "loser_user_id": 2, "loser_name": "Одинаковый",
        "is_dick_stolen": False, "stolen_item": None,
    })
    second = event("duel_finished", {
        "initiator_user_id": 3, "initiator_name": "Одинаковый",
        "opponent_user_id": 1, "opponent_name": "Мой гном",
        "winner_user_id": 3, "winner_name": "Одинаковый",
        "loser_user_id": 1, "loser_name": "Мой гном",
        "is_dick_stolen": False, "stolen_item": None,
    })
    text = build_recap_text([first, second], 1)
    assert text.count("На тебя нападал Одинаковый — 1 раз") == 2
    assert "Твои победы: 1 · Победы соперника: 0" in text
    assert "Твои победы: 0 · Победы соперника: 1" in text


@pytest.mark.asyncio
async def test_recap_does_not_double_count_journalled_native_duel(
    temp_database, fake_context, monkeypatch,
):
    register(1)
    duel_data = {
        "duel_id": 7, "initiator_user_id": 1, "initiator_name": "Мой гном",
        "opponent_user_id": 2, "opponent_name": "Соперник",
        "winner_user_id": 1, "winner_name": "Мой гном",
        "loser_user_id": 2, "loser_name": "Соперник",
        "is_dick_stolen": False, "stolen_item": None,
    }
    journalled = event("duel_finished", duel_data, source="duel:7")
    native = {
        "duel_id": 7, "finished_at": 1, "rounds": 1,
        "player1": {"user_id": 1, "username": "me", "display_name": "Мой гном", "dwarf_name": None},
        "player2": {"user_id": 2, "username": "opponent", "display_name": "Соперник", "dwarf_name": None},
        "result": {"kind": "finalized", "winner_user_id": 1, "loser_user_id": 2},
    }
    monkeypatch.setattr(recap, "list_game_events", lambda *_: [journalled])
    monkeypatch.setattr(recap, "list_finished_duels_for_recap", lambda *_: [native])
    monkeypatch.setattr(recap, "list_item_receipts_for_recap", lambda *_: [])
    monkeypatch.setattr(recap, "get_huecrab_tame_for_recap", lambda *_: None)
    message = SimpleNamespace(
        message_id=10,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=11)),
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT), effective_user=user(1), message=message,
    )
    await recap_command(update, fake_context)
    text = message.reply_text.await_args.args[0]
    assert "Ты нападал на Соперник — 1 раз" in text
    assert text.count("Твои победы: 1 · Победы соперника: 0") == 1
    assert "Дуэли: побед 1, поражений 0" in text


def test_stolen_inventory_item_is_rendered_as_a_loss(temp_database):
    text = build_recap_text([event("duel_finished", {
        "initiator_user_id": 1, "initiator_name": "Me",
        "opponent_user_id": 2, "opponent_name": "Enemy",
        "winner_user_id": 2, "winner_name": "Enemy",
        "loser_user_id": 1, "loser_name": "Me",
        "winner_points_delta": 10, "loser_points_delta": -10,
        "is_dick_stolen": True, "rounds": 1,
        "stolen_item": {"item_name": "Амулет"},
    })], 1)
    assert "Амулет ×1 (украден соперником)" in text


def test_recap_keeps_find_receipt_missed_loss_and_duplicate_counts_separate(temp_database):
    events = [
        event("item_found", {"item_id": "knife", "item_event_id": 1}),
        event("item_received", {"item_id": "knife", "item_event_id": 1, "method": "claim"}),
        event("item_received", {"item_id": "knife", "item_event_id": 2, "method": "claim"}),
        event("item_found", {"item_id": "oiled_vest", "item_event_id": 3}),
        event("item_missed", {
            "item_id": "oiled_vest", "item_event_id": 3,
            "claimed_by_name": "Другой",
        }),
        event("item_lost", {"item_id": "knife", "reason": "lure"}),
    ]
    text = build_recap_text(events, 1)
    assert "Получено в инвентарь" in text and "×2" in text
    assert "Упущенная находка" in text and "Другой" in text
    assert "выброшен через /lure" in text
    assert "найдено 2, получено 2, потеряно 1" in text


def test_huecrab_events_have_their_own_section(temp_database):
    text = build_recap_text([
        event("huecrab_tamed", {"huecrab_event_id": 5}),
        event("item_received", {
            "item_id": "knife", "item_event_id": 6, "method": "huecrab",
        }),
    ], 1)
    assert "🦀 <b>Хуекрабы</b>" in text
    assert "Приручено хуекрабов: 1" in text
    assert "Хуекраб подобрал" in text


def test_empty_day_message_is_exact(temp_database):
    assert build_recap_text([], 1) == (
        "Сегодня твой гном пока ничем не отличился. Подозрительно тихий день."
    )


def test_very_long_recap_stays_within_telegram_limit(temp_database):
    events = [
        event("item_received", {
            "item_id": f"unknown-{index}-" + "я" * 200,
            "item_event_id": index, "method": "claim",
        }, occurred_at=index)
        for index in range(200)
    ]
    text = build_recap_text(events, 1)
    assert len(text) <= TELEGRAM_SAFE_TEXT_LIMIT
    assert "📊 <b>Итог дня</b>" in text


@pytest.mark.asyncio
async def test_command_only_reads_sender_and_schedules_both_messages(temp_database, fake_context):
    register(1)
    register(2)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    db.record_game_event(CHAT, 1, "huecrab_tamed", {}, source_id="mine", occurred_at_ms=now)
    db.record_game_event(CHAT, 2, "item_lost", {
        "item_id": "secret", "reason": "lure",
    }, source_id="other", occurred_at_ms=now)

    message = SimpleNamespace(
        message_id=55,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=77)),
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT),
        effective_user=user(1),
        message=message,
    )
    await recap_command(update, fake_context)

    text = message.reply_text.await_args.args[0]
    assert "Приручено хуекрабов" in text
    assert "secret" not in text
    callback, delay, kwargs = fake_context.job_queue.calls[-1]
    assert delay == 60
    assert kwargs["data"] == {"chat_id": CHAT, "message_ids": [77, 55]}


@pytest.mark.asyncio
async def test_command_rejects_unregistered_gnome(temp_database, fake_context):
    message = SimpleNamespace(
        message_id=10,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=11)),
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT), effective_user=user(9), message=message,
    )
    await recap_command(update, fake_context)
    assert "Сначала зарегистрируй" in message.reply_text.await_args.args[0]
