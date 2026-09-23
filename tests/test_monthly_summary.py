"""Regression coverage for persistent per-chat Moscow-month game summaries."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


SEPTEMBER = "2026-09"
OCTOBER = "2026-10"


def user(user_id):
    return SimpleNamespace(id=user_id, username=f"user{user_id}", first_name=f"User {user_id}")


def finish_plan(db, chat_id, winner_id, loser_id, *, stolen):
    from handlers.duel_state import _build_duel_result_plan

    winner = db.get_or_create_duel_user(user(winner_id), chat_id)
    loser = db.get_or_create_duel_user(user(loser_id), chat_id)
    plan = _build_duel_result_plan(winner, loser, stolen, f"user{winner_id}", 100)
    db.apply_duel_result_plan(chat_id, plan)


def test_month_keys_use_moscow_midnight_not_utc_midnight():
    from database import moscow_month_key
    from handlers.monthly_summary import previous_moscow_month_key

    before = datetime(2026, 9, 30, 20, 59, 59, tzinfo=timezone.utc)
    after = datetime(2026, 9, 30, 21, 0, 0, tzinfo=timezone.utc)
    assert moscow_month_key(before) == SEPTEMBER
    assert moscow_month_key(after) == OCTOBER
    assert previous_moscow_month_key(datetime(2026, 10, 1, 7, tzinfo=timezone.utc)) == SEPTEMBER


def test_new_table_zero_defaults_and_repeated_init_preserves_history(temp_database):
    import database as db

    assert db.get_monthly_chat_stats(-1, SEPTEMBER) == {
        "dicks_stolen": 0, "duels": 0, "bosses_killed": 0,
    }
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM monthly_chat_stats").fetchone()[0] == 0
    db.increment_monthly_bosses_killed(-1, SEPTEMBER)
    db.init_db()
    db.init_db()
    assert db.get_monthly_chat_stats(-1, SEPTEMBER)["bosses_killed"] == 1
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM monthly_chat_stats").fetchone()[0] == 1


def test_existing_database_migrates_without_losing_existing_table(tmp_path, monkeypatch):
    import database as db

    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE preserved (value TEXT)")
        conn.execute("INSERT INTO preserved VALUES ('keep')")
    monkeypatch.setattr(db, "DB_NAME", str(path))
    db.init_db()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM preserved").fetchone()[0] == "keep"
        assert conn.execute("SELECT COUNT(*) FROM monthly_chat_stats").fetchone()[0] == 0


def test_completed_duels_aggregate_theft_and_non_theft_by_chat_and_month(
    temp_database, monkeypatch,
):
    import database as db

    clock = [datetime(2026, 9, 30, 20, 59, 59, tzinfo=timezone.utc)]
    monkeypatch.setattr(db, "_utc_now", lambda: clock[0])
    finish_plan(db, -1, 1, 2, stolen=True)
    finish_plan(db, -1, 3, 2, stolen=False)
    finish_plan(db, -2, 1, 2, stolen=False)
    assert db.get_monthly_chat_stats(-1, SEPTEMBER) == {
        "dicks_stolen": 1, "duels": 2, "bosses_killed": 0,
    }
    assert db.get_monthly_chat_stats(-2, SEPTEMBER) == {
        "dicks_stolen": 0, "duels": 1, "bosses_killed": 0,
    }

    clock[0] = datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc)
    finish_plan(db, -1, 1, 3, stolen=False)
    assert db.get_monthly_chat_stats(-1, OCTOBER)["duels"] == 1
    assert db.get_monthly_chat_stats(-1, SEPTEMBER)["duels"] == 2


def test_concurrent_upserts_do_not_lose_increments(temp_database):
    import database as db

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: db.increment_monthly_bosses_killed(-1, SEPTEMBER), range(40)))
    assert db.get_monthly_chat_stats(-1, SEPTEMBER)["bosses_killed"] == 40


def test_failed_monthly_write_rolls_back_duel_result(temp_database, monkeypatch):
    import database as db
    from handlers.duel_state import _build_duel_result_plan

    monkeypatch.setattr(db, "_utc_now", lambda: datetime(2026, 9, 30, 20, tzinfo=timezone.utc))
    winner = db.get_or_create_duel_user(user(1), -1)
    loser = db.get_or_create_duel_user(user(2), -1)
    with sqlite3.connect(temp_database) as conn:
        conn.execute("""
            CREATE TRIGGER reject_monthly BEFORE INSERT ON monthly_chat_stats
            BEGIN SELECT RAISE(ABORT, 'monthly write failed'); END
        """)
    with pytest.raises(sqlite3.IntegrityError):
        db.apply_duel_result_plan(
            -1, _build_duel_result_plan(winner, loser, True, "user1", 100),
        )
    assert db.get_duel_user_by_username("user1", -1)["wins"] == 0
    assert db.get_duel_user_by_username("user2", -1)["losses"] == 0
    assert db.get_monthly_chat_stats(-1, SEPTEMBER)["duels"] == 0


def test_berserk_does_not_count_as_ordinary_theft(temp_database):
    import database as db

    db.get_or_create_duel_user(user(1), -1)
    db.get_or_create_duel_user(user(2), -1)
    assert db.apply_duel_berserk(-1, 1, 2, "user1") is True
    assert db.get_monthly_chat_stats(-1, db.moscow_month_key()) == {
        "dicks_stolen": 0, "duels": 0, "bosses_killed": 0,
    }


@pytest.mark.asyncio
async def test_boss_victory_counts_once_and_defeat_does_not(
    temp_database, fake_context, monkeypatch,
):
    import database as db
    from handlers import duel
    from tests.test_boss_flow import make_battle, make_participant

    monkeypatch.setattr(db, "_utc_now", lambda: datetime(2026, 9, 30, 20, tzinfo=timezone.utc))
    monkeypatch.setattr(duel, "reward_boss_victory", Mock(return_value=True))
    monkeypatch.setattr(duel, "_boss_send_final_report", AsyncMock())
    duel.ACTIVE_BOSS_BATTLES[-1] = make_battle([make_participant(1), make_participant(2)])
    await duel._boss_finish_victory(fake_context, -1)
    await duel._boss_finish_victory(fake_context, -1)
    duel.ACTIVE_BOSS_BATTLES[-1] = make_battle([make_participant(1)])
    await duel._boss_finish_defeat(fake_context, -1)
    assert db.get_monthly_chat_stats(-1, SEPTEMBER)["bosses_killed"] == 1


@pytest.mark.asyncio
async def test_summary_command_reads_current_chat_month_and_shows_zero(
    temp_database, fake_context, monkeypatch,
):
    import database as db
    from handlers import monthly_summary

    monkeypatch.setattr(db, "_utc_now", lambda: datetime(2026, 10, 1, 0, tzinfo=timezone.utc))
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=-10))
    await monthly_summary.summary_command(update, fake_context)
    assert fake_context.bot.send_message.await_args.kwargs == {
        "chat_id": -10,
        "text": "Это был тяжелый месяц\nУкрадено хуев: 0\n"
                "Проведено жестоких боев: 0\nУбито боссов: 0",
    }
    db.increment_monthly_bosses_killed(-10, OCTOBER)
    await monthly_summary.summary_command(update, fake_context)
    assert fake_context.bot.send_message.await_args.kwargs["text"].endswith("Убито боссов: 1")


@pytest.mark.asyncio
async def test_auto_summary_uses_previous_month_all_registered_chats_and_isolates_failure(
    temp_database, fake_context, monkeypatch,
):
    import database as db
    from handlers import monthly_summary

    for chat_id in (-10, -20, -30):
        db.get_or_create_duel_user(user(1), chat_id)
    db.increment_monthly_bosses_killed(-10, SEPTEMBER)
    db.increment_monthly_bosses_killed(-10, OCTOBER)
    db.increment_monthly_bosses_killed(-30, SEPTEMBER)
    previous = monthly_summary.previous_moscow_month_key(
        datetime(2026, 10, 1, 7, tzinfo=timezone.utc)
    )
    monkeypatch.setattr(
        monthly_summary, "previous_moscow_month_key",
        lambda: previous,
    )

    async def send_message(*, chat_id, text):
        if chat_id == -20:
            raise RuntimeError("Telegram failed for one chat")
        return SimpleNamespace(message_id=chat_id)

    fake_context.bot.send_message.side_effect = send_message
    await monthly_summary.monthly_summary_job(fake_context)
    calls = fake_context.bot.send_message.await_args_list
    assert [call.kwargs["chat_id"] for call in calls] == [-30, -20, -10]
    assert calls[0].kwargs["text"].endswith("Убито боссов: 1")
    assert calls[1].kwargs["text"].endswith("Убито боссов: 0")
    assert calls[2].kwargs["text"].endswith("Убито боссов: 1")
    assert db.get_monthly_chat_stats(-10, SEPTEMBER)["bosses_killed"] == 1
    assert db.get_monthly_chat_stats(-10, OCTOBER)["bosses_killed"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("already_registered", [False, True])
async def test_monthly_job_wiring_is_once_on_first_day_ten_moscow(monkeypatch, already_registered):
    import bot

    jobs = []

    class FakeJobQueue:
        def run_daily(self, *_args, **_kwargs):
            pass

        def run_monthly(self, callback, **kwargs):
            jobs.append((callback, kwargs))

        def run_repeating(self, *_args, **_kwargs):
            pass

        def get_jobs_by_name(self, name):
            return [object()] if name == "monthly_summary_job" and already_registered else []

    class FakeApplication:
        job_queue = FakeJobQueue()

        def add_handler(self, _handler, group=0):
            pass

        def add_error_handler(self, _handler):
            pass

        async def run_polling(self, **_kwargs):
            pass

    class FakeBuilder:
        def token(self, _token):
            return self

        def post_init(self, _callback):
            return self

        def build(self):
            return FakeApplication()

    monkeypatch.setattr(bot.nest_asyncio, "apply", lambda: None)
    monkeypatch.setattr(bot, "init_db", lambda: None)
    monkeypatch.setattr(bot.Application, "builder", lambda: FakeBuilder())
    monkeypatch.setattr(bot, "schedule_past_pizda_job", lambda _queue: None)
    await bot.main()

    assert len(jobs) == (0 if already_registered else 1)
    if jobs:
        callback, kwargs = jobs[0]
        assert callback is bot.monthly_summary_job
        assert kwargs["name"] == "monthly_summary_job"
        assert kwargs["day"] == 1
        assert (kwargs["when"].hour, kwargs["when"].minute) == (10, 0)
        assert kwargs["when"].tzinfo.key == "Europe/Moscow"
