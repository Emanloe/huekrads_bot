"""Regression coverage for the per-chat next-battle registration queue."""

import asyncio
import sqlite3
import subprocess
import sys
from datetime import datetime as real_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from handlers import boss_registration, duel
from handlers.boss_read_model import get_boss_battle_read_model
from module_settings import set_module_enabled
from text_resources import get_text


def user(user_id=1001):
    return SimpleNamespace(id=user_id, username=f"user{user_id}",
                           first_name="User", last_name=None)


def command(chat_id, tg_user, chat_type="group"):
    return SimpleNamespace(message=SimpleNamespace(
        from_user=tg_user, chat=SimpleNamespace(id=chat_id, type=chat_type),
        chat_id=chat_id,
    ))


def callback(chat_id, tg_user, *, effective_chat_id=None, message_id=77):
    query = SimpleNamespace(
        data="boss_reg_next", from_user=tg_user, answer=AsyncMock(),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type="group"), message_id=message_id,
        ),
    )
    return SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(
        id=effective_chat_id if effective_chat_id is not None else chat_id,
    )), query


@pytest.fixture
def next_queue(temp_database, monkeypatch):
    monkeypatch.setattr(boss_registration, "_BOSS_REG_DB_PATH", temp_database)
    duel.ACTIVE_BOSS_BATTLES.clear()
    duel._BOSS_START_LOCKS.clear()
    yield temp_database
    duel.ACTIVE_BOSS_BATTLES.clear()
    duel._BOSS_START_LOCKS.clear()


def _track_handler_connections(monkeypatch, module, *, fail_sql=None):
    original_connect = sqlite3.connect
    connections = []

    class TrackingConnection(sqlite3.Connection):
        closed = False

        def execute(self, sql, parameters=(), /):
            normalized = " ".join(sql.split())
            if fail_sql and fail_sql in normalized:
                raise RuntimeError("forced SQLite failure")
            return super().execute(sql, parameters)

        def close(self):
            self.closed = True
            return super().close()

    def connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        conn = original_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(module, "sqlite3", SimpleNamespace(connect=connect))
    return connections


def test_boss_registration_closes_connections_after_success(next_queue, monkeypatch):
    connections = _track_handler_connections(monkeypatch, boss_registration)

    assert boss_registration._boss_register_user(-1, user(11)) is True
    assert boss_registration._boss_get_registered_users(-1)[0][0] == 11
    assert boss_registration._boss_registration_snapshot(-1, 11) == (1, True)
    assert boss_registration._boss_get_registered_chat_ids() == {-1}
    assert boss_registration._boss_consume_registrations(-1)[0][0] == 11

    assert len(connections) == 5
    assert all(conn.closed for conn in connections)


def test_boss_registration_rolls_back_and_closes_on_caller_error(
    next_queue, monkeypatch,
):
    connections = _track_handler_connections(monkeypatch, boss_registration)

    def fail_selector():
        raise RuntimeError("selection failed")

    with pytest.raises(RuntimeError, match="selection failed"):
        boss_registration._boss_prepare_next_boss(-1, fail_selector)

    assert connections[-1].closed is True
    with sqlite3.connect(next_queue) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM boss_next_battles WHERE chat_id = -1"
        ).fetchone()[0] == 0


def test_boss_registration_closes_connection_on_init_error(next_queue, monkeypatch):
    connections = _track_handler_connections(
        monkeypatch,
        boss_registration,
        fail_sql="CREATE TABLE IF NOT EXISTS boss_next_registrations",
    )
    with pytest.raises(RuntimeError, match="forced SQLite failure"):
        boss_registration._boss_get_registered_users(-1)
    assert connections[-1].closed is True


def test_boss_registration_closes_connection_on_snapshot_error(
    next_queue, monkeypatch,
):
    boss_registration._boss_get_registered_users(-1)
    connections = _track_handler_connections(
        monkeypatch,
        boss_registration,
        fail_sql="SELECT COUNT(DISTINCT user_id)",
    )
    with pytest.raises(RuntimeError, match="forced SQLite failure"):
        boss_registration._boss_registration_snapshot(-1, 11)
    assert connections[-1].closed is True


def _seed_legacy(next_queue, rows):
    with sqlite3.connect(next_queue) as conn:
        conn.execute("""CREATE TABLE boss_registrations (
            chat_id INTEGER, user_id INTEGER, username TEXT, first_name TEXT,
            last_name TEXT, reg_date TEXT,
            PRIMARY KEY (chat_id, user_id, reg_date))""")
        conn.executemany("INSERT INTO boss_registrations VALUES (?, ?, ?, ?, ?, ?)", rows)


def test_legacy_migration_date_uses_moscow_calendar_day(monkeypatch):
    observed = []
    class FrozenDateTime:
        @classmethod
        def now(cls, timezone):
            observed.append(timezone.key)
            return real_datetime(2026, 9, 27, 22, 30, tzinfo=ZoneInfo("UTC")).astimezone(timezone)
    monkeypatch.setattr(boss_registration, "datetime", FrozenDateTime)
    assert boss_registration._legacy_registration_today() == "2026-09-28"
    assert observed == ["Europe/Moscow"]


def test_legacy_migration_only_takes_current_moscow_date(next_queue, monkeypatch):
    monkeypatch.setattr(boss_registration, "_legacy_registration_today",
                        lambda: "2026-09-28")
    _seed_legacy(next_queue, [
        (-1, 11, "today", "Today", None, "2026-09-28"),
        (-1, 12, "stale", "Stale", None, "2026-09-27"),
        (-1, 13, "future", "Future", None, "2026-09-29"),
        (-2, 11, "other", "Other", None, "2026-09-28"),
        (-3, 14, "historical", "Historical", None, "2025-01-01"),
    ])
    assert boss_registration._boss_get_registered_users(-1) == [(11, "today", "Today", None)]
    assert boss_registration._boss_get_registered_users(-2) == [(11, "other", "Other", None)]
    assert boss_registration._boss_get_registered_users(-3) == []
    assert boss_registration._boss_get_registered_chat_ids() == {-1, -2}
    assert boss_registration._boss_consume_registrations(-1) == [(11, "today", "Today", None)]
    assert boss_registration._boss_get_registered_users(-2) == [(11, "other", "Other", None)]
    with sqlite3.connect(next_queue) as conn:
        assert conn.execute("SELECT COUNT(*) FROM boss_registrations").fetchone()[0] == 5


def test_legacy_migration_is_idempotent_and_keeps_existing_next_row(
    next_queue, monkeypatch,
):
    monkeypatch.setattr(boss_registration, "_legacy_registration_today",
                        lambda: "2026-09-28")
    _seed_legacy(next_queue, [
        (-1, 11, "legacy", "Legacy", None, "2026-09-28"),
        (-1, 12, "second", "Second", None, "2026-09-28"),
        (-2, 11, "another", "Another", None, "2026-09-28"),
    ])
    with sqlite3.connect(next_queue) as conn:
        conn.execute(boss_registration._NEXT_TABLE_SQL)
        conn.execute("""INSERT INTO boss_next_registrations
            VALUES (-1, 11, 'already', 'Already', NULL)""")
    expected = [(11, "already", "Already", None), (12, "second", "Second", None)]
    assert boss_registration._boss_get_registered_users(-1) == expected
    assert boss_registration._boss_get_registered_users(-1) == expected
    assert boss_registration._boss_get_registered_users(-2) == [(11, "another", "Another", None)]
    with sqlite3.connect(next_queue) as conn:
        assert conn.execute("SELECT COUNT(*) FROM boss_next_registrations").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM boss_registrations").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_four_users_share_result_button_without_disabling_it(next_queue, fake_context):
    queries = []
    for user_id in (1, 2, 3, 4):
        update, query = callback(-1, user(user_id))
        await duel.boss_callback(update, fake_context)
        queries.append(query)
    assert [row[0] for row in boss_registration._boss_get_registered_users(-1)] == [1, 2, 3, 4]
    for query in queries:
        query.answer.assert_awaited_once_with(get_text("boss.registration.callback_registered"))
    fake_context.bot.edit_message_text.assert_not_awaited()

    repeat, query = callback(-1, user(1))
    await duel.boss_callback(repeat, fake_context)
    query.answer.assert_awaited_once_with(
        get_text("boss.registration.callback_already_registered")
    )
    assert len(boss_registration._boss_get_registered_users(-1)) == 4


@pytest.mark.asyncio
async def test_four_concurrent_registrations_are_atomic_per_chat(next_queue, fake_context):
    updates = [callback(-1, user(user_id)) for user_id in (1, 2, 3, 4)]
    await asyncio.gather(*(duel.boss_callback(update, fake_context) for update, _ in updates))
    assert {row[0] for row in boss_registration._boss_get_registered_users(-1)} == {1, 2, 3, 4}
    assert all(query.answer.await_count == 1 for _, query in updates)

    # Separate SQLite connections also serialize simultaneous inserts.
    result = await asyncio.gather(*(
        asyncio.to_thread(boss_registration._boss_register_user, -2, user(user_id))
        for user_id in (1, 2, 3, 4)
    ))
    assert result == [True] * 4
    assert {row[0] for row in boss_registration._boss_get_registered_users(-2)} == {1, 2, 3, 4}


@pytest.mark.asyncio
@pytest.mark.parametrize("midnight_first", [True, False])
async def test_signup_survives_midnight_and_new_python_process_in_both_orders(
    next_queue, fake_context, monkeypatch, midnight_first,
):
    today = ["2026-10-03"]
    monkeypatch.setattr(boss_registration, "_legacy_registration_today", lambda: today[0])
    duel.ACTIVE_BOSS_BATTLES[-1] = {"battle_id": "old-runtime-battle"}
    update, query = callback(-1, user(41))
    await duel.boss_callback(update, fake_context)
    query.answer.assert_awaited_once_with(get_text("boss.registration.callback_registered"))

    def fresh_process_reads_queue():
        script = (
            "import sys; from pathlib import Path; "
            "from handlers import boss_registration as b; "
            "b._BOSS_REG_DB_PATH = Path(sys.argv[1]); "
            "assert [row[0] for row in b._boss_get_registered_users(-1)] == [41]"
        )
        subprocess.run([sys.executable, "-c", script, str(next_queue)],
                       check=True, capture_output=True, text=True)
        duel.ACTIVE_BOSS_BATTLES.clear()
        duel._BOSS_START_LOCKS.clear()

    if midnight_first:
        today[0] = "2026-10-04"
        assert boss_registration._boss_registration_snapshot(-1, 41) == (1, True)
        fresh_process_reads_queue()
    else:
        fresh_process_reads_queue()
        today[0] = "2026-10-04"
    assert boss_registration._boss_get_registered_users(-1)[0][0] == 41

    monkeypatch.setattr(duel, "_boss_make_participant", lambda person, _: {"id": person.id})
    assert await duel._start_boss_battle(fake_context, -1)
    battle = duel.ACTIVE_BOSS_BATTLES[-1]
    assert battle["battle_id"] != "old-runtime-battle"
    assert 41 in battle["participants"]
    assert boss_registration._boss_get_registered_users(-1) == []
    battle["phase_task"].cancel()


@pytest.mark.asyncio
async def test_finish_command_and_old_result_button_share_next_queue(
    next_queue, fake_context, monkeypatch,
):
    chat_id = -11
    send = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", send)
    monkeypatch.setattr(duel, "_persist_boss_finish_snapshot", Mock())
    monkeypatch.setattr(duel, "_boss_final_report", lambda *_: "existing result text")
    monkeypatch.setattr(duel, "_maybe_award_boss_item", lambda *_: None)
    battle = {"message_id": 42, "participants": {}, "phase_task": None}
    duel.ACTIVE_BOSS_BATTLES[chat_id] = battle
    await duel._boss_finish_defeat(fake_context, chat_id)
    report = fake_context.bot.edit_message_text.await_args.kwargs
    assert report["text"] == "existing result text"
    button = report["reply_markup"].inline_keyboard[0][0]
    assert button.text == get_text("boss.registration.next_button")
    assert button.callback_data == "boss_reg_next"

    first = user(1)
    original = duel._boss_register_user
    calls = []
    def shared_register(cid, person):
        calls.append((cid, person.id))
        return original(cid, person)
    monkeypatch.setattr(duel, "_boss_register_user", shared_register)
    await duel.boss_reg_command(command(chat_id, first), fake_context)
    assert get_text("boss.registration.next_registered") in send.await_args.args[2]
    old_button, query = callback(chat_id, first, effective_chat_id=-999)
    await duel.boss_callback(old_button, fake_context)
    query.answer.assert_awaited_once_with(get_text("boss.registration.callback_already_registered"))
    assert calls == [(chat_id, 1), (chat_id, 1)]
    assert boss_registration._boss_get_registered_users(chat_id)[0][0] == 1

    second_button, second_query = callback(chat_id, user(2))
    await duel.boss_callback(second_button, fake_context)
    second_query.answer.assert_awaited_once_with(get_text("boss.registration.callback_registered"))
    await duel.boss_callback(second_button, fake_context)
    assert second_query.answer.await_args.args == (get_text("boss.registration.callback_already_registered"),)
    assert len(boss_registration._boss_get_registered_users(chat_id)) == 2


@pytest.mark.asyncio
async def test_previous_battle_log_cleanup_does_not_remove_signup_button(
    next_queue, fake_context, monkeypatch,
):
    chat_id = -11
    monkeypatch.setattr(duel, "_persist_boss_finish_snapshot", Mock())
    monkeypatch.setattr(duel, "_boss_final_report", lambda *_: "final result")
    monkeypatch.setattr(duel, "_maybe_award_boss_item", lambda *_: None)
    duel.ACTIVE_BOSS_BATTLES[chat_id] = {
        "battle_id": "finished-battle", "message_id": 42,
        "battle_log_message_id": 55, "participants": {}, "phase_task": None,
    }
    await duel._boss_finish_defeat(fake_context, chat_id)
    assert chat_id not in duel.ACTIVE_BOSS_BATTLES
    assert fake_context.bot.edit_message_text.await_args.kwargs["reply_markup"] is not None
    fake_context.job.data = {
        "chat_id": chat_id, "message_ids": [55],
        "battle_id": "finished-battle", "round": 1,
    }
    await duel.delete_messages_job(fake_context)
    fake_context.bot.delete_message.assert_awaited_once_with(
        chat_id=chat_id, message_id=55,
    )

    update, query = callback(chat_id, user(11), message_id=42)
    await duel.boss_callback(update, fake_context)
    query.answer.assert_awaited_once_with(get_text("boss.registration.callback_registered"))
    assert boss_registration._boss_registration_snapshot(chat_id, 11) == (1, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_step", ["enable", "register"])
async def test_signup_database_error_answers_and_does_not_block_later_user(
    next_queue, fake_context, monkeypatch, caplog, failing_step,
):
    import logging

    caplog.set_level(logging.INFO)
    name = "set_boss_enabled" if failing_step == "enable" else "_boss_register_user"
    original = getattr(duel, name)
    def fail_once(*args):
        monkeypatch.setattr(duel, name, original)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(duel, name, fail_once)

    first, first_query = callback(-1, user(11))
    await duel.boss_callback(first, fake_context)
    first_query.answer.assert_awaited_once_with(
        get_text("boss.registration.callback_error"), show_alert=True,
    )
    assert boss_registration._boss_get_registered_users(-1) == []
    assert "BOSS_NEXT_BATTLE_SIGNUP_FAILED" in caplog.text

    second, second_query = callback(-1, user(12))
    await duel.boss_callback(second, fake_context)
    second_query.answer.assert_awaited_once_with(
        get_text("boss.registration.callback_registered")
    )
    assert [row[0] for row in boss_registration._boss_get_registered_users(-1)] == [12]
    assert "BOSS_NEXT_BATTLE_SIGNUP_OK" in caplog.text


@pytest.mark.asyncio
async def test_answer_failure_after_commit_is_logged_and_retry_reports_already(
    next_queue, fake_context, caplog,
):
    import logging

    caplog.set_level(logging.INFO)
    update, query = callback(-1, user(11))
    query.answer.side_effect = RuntimeError("Telegram unavailable")
    await duel.boss_callback(update, fake_context)
    assert boss_registration._boss_registration_snapshot(-1, 11) == (1, True)
    assert "stage=answer registration_committed=True" in caplog.text

    retry, retry_query = callback(-1, user(11))
    await duel.boss_callback(retry, fake_context)
    retry_query.answer.assert_awaited_once_with(
        get_text("boss.registration.callback_already_registered")
    )


@pytest.mark.asyncio
async def test_active_battle_registration_is_for_following_battle_and_per_chat(
    next_queue, fake_context, monkeypatch,
):
    monkeypatch.setattr(duel, "send_and_schedule", AsyncMock())
    monkeypatch.setattr(duel, "random", SimpleNamespace(choice=Mock(side_effect=AssertionError("RNG"))))
    active = {
        "participants": {}, "phase": "join", "lock": asyncio.Lock(),
        "boss": duel.BOSSES[0], "round": 0, "hits": 0,
    }
    duel.ACTIVE_BOSS_BATTLES[-1] = active
    await duel.boss_reg_command(command(-1, user(5)), fake_context)
    old_result, old_query = callback(-1, user(5))
    await duel.boss_callback(old_result, fake_context)
    old_query.answer.assert_awaited_once_with(
        get_text("boss.registration.callback_already_registered")
    )
    update, query = callback(-2, user(5))
    await duel.boss_callback(update, fake_context)
    assert active["participants"] == {}
    assert boss_registration._boss_registration_snapshot(-1, 5) == (1, True)
    assert boss_registration._boss_registration_snapshot(-2, 5) == (1, True)
    query.answer.assert_awaited_once_with(get_text("boss.registration.callback_registered"))
    model = await get_boss_battle_read_model(-1, 5)
    assert model["registration"]["viewer_registered"] is True
    assert model["registration"]["participants_count"] == 1


@pytest.mark.asyncio
async def test_auto_and_manual_starts_consume_same_pool_and_leave_later_registration(
    next_queue, fake_context, monkeypatch,
):
    monkeypatch.setattr(duel, "_boss_make_participant", lambda person, _: {"id": person.id})
    assert boss_registration._boss_register_user(-1, user(1))
    assert await duel._start_boss_battle(fake_context, -1, include_registrations=True)
    assert 1 in duel.ACTIVE_BOSS_BATTLES[-1]["participants"]
    assert boss_registration._boss_get_registered_users(-1) == []
    assert boss_registration._boss_register_user(-1, user(1))
    duel.ACTIVE_BOSS_BATTLES[-1]["phase_task"].cancel()
    duel.ACTIVE_BOSS_BATTLES.pop(-1)
    monkeypatch.setattr(duel, "ADMIN_IDS", {42})
    await duel.boss_command(command(-1, user(42)), fake_context)
    assert 1 in duel.ACTIVE_BOSS_BATTLES[-1]["participants"]
    assert boss_registration._boss_get_registered_users(-1) == []
    assert boss_registration._boss_register_user(-1, user(2))
    assert [row[0] for row in boss_registration._boss_get_registered_users(-1)] == [2]
    duel.ACTIVE_BOSS_BATTLES[-1]["phase_task"].cancel()


@pytest.mark.asyncio
async def test_boss_auto_off_preserves_queue_until_actual_start(
    next_queue, fake_context, monkeypatch,
):
    set_module_enabled(-1, "boss_auto", False, 42)
    monkeypatch.setattr(duel, "send_and_schedule", AsyncMock())
    await duel.boss_reg_command(command(-1, user(7)), fake_context)
    monkeypatch.setattr(duel, "get_all_chats", lambda: [-1])
    monkeypatch.setattr(duel, "is_boss_enabled", lambda _: True)
    await duel.boss_daily_job(fake_context)
    assert boss_registration._boss_registration_snapshot(-1, 7) == (1, True)
    fake_context.bot.send_message.assert_not_awaited()
    set_module_enabled(-1, "boss_auto", True, 42)
    monkeypatch.setattr(duel, "_boss_make_participant", lambda person, _: {"id": person.id})
    await duel.boss_daily_job(fake_context)
    assert 7 in duel.ACTIVE_BOSS_BATTLES[-1]["participants"]
    assert boss_registration._boss_get_registered_users(-1) == []
    duel.ACTIVE_BOSS_BATTLES[-1]["phase_task"].cancel()


@pytest.mark.asyncio
async def test_old_result_button_targets_new_pool_after_another_battle(
    next_queue, fake_context, monkeypatch,
):
    monkeypatch.setattr(duel, "_boss_make_participant", lambda person, _: {"id": person.id})
    assert await duel._start_boss_battle(fake_context, -1)
    duel.ACTIVE_BOSS_BATTLES[-1]["phase_task"].cancel()
    duel.ACTIVE_BOSS_BATTLES.pop(-1)
    assert await duel._start_boss_battle(fake_context, -1)
    duel.ACTIVE_BOSS_BATTLES[-1]["phase_task"].cancel()
    duel.ACTIVE_BOSS_BATTLES.pop(-1)
    update, query = callback(-1, user(9))
    await duel.boss_callback(update, fake_context)
    assert boss_registration._boss_registration_snapshot(-1, 9) == (1, True)
    assert await duel._start_boss_battle(fake_context, -1)
    assert 9 in duel.ACTIVE_BOSS_BATTLES[-1]["participants"]
    duel.ACTIVE_BOSS_BATTLES[-1]["phase_task"].cancel()


@pytest.mark.asyncio
async def test_registration_race_uses_single_consume_boundary(
    next_queue, fake_context, monkeypatch,
):
    gate = asyncio.Event()
    entered = asyncio.Event()
    async def delayed_send(**_):
        entered.set()
        await gate.wait()
        return SimpleNamespace(message_id=101)
    fake_context.bot.send_message.side_effect = delayed_send
    monkeypatch.setattr(duel, "_boss_make_participant", lambda person, _: {"id": person.id})
    assert boss_registration._boss_register_user(-1, user(1))
    start = asyncio.create_task(duel._start_boss_battle(fake_context, -1))
    await entered.wait()
    assert boss_registration._boss_register_user(-1, user(2))
    gate.set()
    assert await start
    assert set(duel.ACTIVE_BOSS_BATTLES[-1]["participants"]) == {1, 2}
    assert boss_registration._boss_get_registered_users(-1) == []
    assert boss_registration._boss_register_user(-1, user(3))
    assert [row[0] for row in boss_registration._boss_get_registered_users(-1)] == [3]
    duel.ACTIVE_BOSS_BATTLES[-1]["phase_task"].cancel()


@pytest.mark.asyncio
async def test_failed_start_and_disabled_manual_start_keep_or_consume_queue(
    next_queue, fake_context, monkeypatch,
):
    set_module_enabled(-1, "boss_auto", False, 42)
    assert boss_registration._boss_register_user(-1, user(8))
    fake_context.bot.send_message.side_effect = RuntimeError("Telegram unavailable")
    with pytest.raises(RuntimeError):
        await duel._start_boss_battle(fake_context, -1)
    assert boss_registration._boss_registration_snapshot(-1, 8) == (1, True)
    fake_context.bot.send_message.side_effect = None
    monkeypatch.setattr(duel, "ADMIN_IDS", {42})
    monkeypatch.setattr(duel, "_boss_make_participant", lambda person, _: {"id": person.id})
    await duel.boss_command(command(-1, user(42)), fake_context)
    assert 8 in duel.ACTIVE_BOSS_BATTLES[-1]["participants"]
    assert boss_registration._boss_get_registered_users(-1) == []
    duel.ACTIVE_BOSS_BATTLES[-1]["phase_task"].cancel()


@pytest.mark.asyncio
async def test_registration_keeps_group_eligibility_for_command_and_button(
    next_queue, fake_context, monkeypatch,
):
    send = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", send)
    await duel.boss_reg_command(command(11, user(1), "private"), fake_context)
    send.assert_awaited_once_with(
        command(11, user(1), "private"), fake_context,
        get_text("boss.registration.group_only"),
    )
    update, query = callback(11, user(1))
    query.message.chat.type = "private"
    await duel.boss_callback(update, fake_context)
    query.answer.assert_awaited_once_with(
        get_text("boss.registration.group_only"), show_alert=True,
    )
    assert boss_registration._boss_registration_snapshot(11, 1) == (0, False)

    missing_user, missing_query = callback(-1, user(2))
    missing_query.from_user = None
    await duel.boss_callback(missing_user, fake_context)
    missing_query.answer.assert_awaited_once_with(
        get_text("boss.registration.callback_error"), show_alert=True,
    )
    assert boss_registration._boss_registration_snapshot(-1, 2) == (0, False)


@pytest.mark.asyncio
async def test_victory_report_button_uses_same_result_text(
    next_queue, fake_context, monkeypatch,
):
    monkeypatch.setattr(duel, "_boss_final_report", lambda *_: "victory report")
    monkeypatch.setattr(duel, "_maybe_award_boss_item", lambda *_: None)
    await duel._boss_send_final_report(
        fake_context, -1, {"message_id": 25, "participants": {}}, victory=True,
    )
    sent = fake_context.bot.edit_message_text.await_args.kwargs
    assert sent["text"] == "victory report"
    assert sent["reply_markup"].inline_keyboard[0][0].callback_data == "boss_reg_next"


def test_miniapp_snapshot_reads_only_current_legacy_rows_without_migrating(
    next_queue, monkeypatch,
):
    monkeypatch.setattr(boss_registration, "_legacy_registration_today",
                        lambda: "2026-09-26")
    with sqlite3.connect(next_queue) as conn:
        conn.execute("""CREATE TABLE boss_registrations (
            chat_id INTEGER, user_id INTEGER, username TEXT, first_name TEXT,
            last_name TEXT, reg_date TEXT)""")
        conn.executemany("INSERT INTO boss_registrations VALUES (?, ?, ?, ?, ?, ?)", [
            (-1, 4, None, "User", None, "2026-09-25"),
            (-1, 4, None, "User", None, "2026-09-26"),
            (-1, 5, None, "Stale", None, "2026-09-25"),
            (-2, 4, None, "User", None, "2026-09-26"),
        ])
    before = next_queue.read_bytes()
    assert boss_registration._boss_registration_snapshot(-1, 4) == (1, True)
    assert boss_registration._boss_registration_snapshot(-1, 5) == (1, False)
    assert boss_registration._boss_registration_snapshot(-2, 5) == (1, False)
    assert next_queue.read_bytes() == before


@pytest.mark.asyncio
async def test_concurrent_starts_select_and_consume_only_once(
    next_queue, fake_context, monkeypatch,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    async def delayed_send(**_):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=101)
    fake_context.bot.send_message.side_effect = delayed_send
    choose = Mock(return_value=duel.BOSSES[0])
    monkeypatch.setattr(boss_registration, "choose_boss", lambda _: choose())
    monkeypatch.setattr(duel, "_boss_make_participant", lambda person, _: {"id": person.id})
    assert boss_registration._boss_register_user(-1, user(1))
    automatic = asyncio.create_task(duel._start_boss_battle(
        fake_context, -1, include_registrations=True,
    ))
    await entered.wait()
    manual = asyncio.create_task(duel._start_boss_battle(fake_context, -1))
    release.set()
    assert await automatic is True
    assert await manual is False
    assert fake_context.bot.send_message.await_count == 1
    choose.assert_called_once_with()
    assert 1 in duel.ACTIVE_BOSS_BATTLES[-1]["participants"]
    assert boss_registration._boss_get_registered_users(-1) == []
    duel.ACTIVE_BOSS_BATTLES[-1]["phase_task"].cancel()
