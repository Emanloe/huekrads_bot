import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


CHAT_ID = -810
START = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def user(user_id=1, username="alice"):
    return SimpleNamespace(
        id=user_id, username=username, first_name=username.title(), is_bot=False,
    )


def birthday_row(path, chat_id=CHAT_ID, user_id=1):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT birthdate, birthday_changed_at FROM users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()


def test_existing_birthdays_migrate_once_and_blank_birthdays_stay_available(tmp_path, monkeypatch):
    import database as db

    path = tmp_path / "production.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE users (
            user_id INTEGER, chat_id INTEGER, username TEXT, first_name TEXT,
            beauty_count INTEGER DEFAULT 0, is_bot INTEGER DEFAULT 0,
            birthdate TEXT, PRIMARY KEY (user_id, chat_id))""")
        conn.execute(
            "INSERT INTO users (user_id, chat_id, username, first_name, birthdate) "
            "VALUES (1, ?, 'alice', 'Alice', '15.04')", (CHAT_ID,),
        )
        conn.execute(
            "INSERT INTO users (user_id, chat_id, username, first_name) "
            "VALUES (2, ?, 'bob', 'Bob')", (CHAT_ID,),
        )

    clock = [START]
    monkeypatch.setattr(db, "DB_NAME", str(path))
    monkeypatch.setattr(db, "_utc_now", lambda: clock[0])
    db.init_db()
    assert birthday_row(path) == ("15.04", START.isoformat(timespec="microseconds"))
    assert birthday_row(path, user_id=2) == (None, None)
    assert db.set_user_birthdate_with_cooldown(CHAT_ID, 1, "16.04") == (
        "cooldown", START + timedelta(days=365)
    )
    assert db.set_user_birthdate_with_cooldown(CHAT_ID, 2, "16.04") == ("success", None)

    before = birthday_row(path)
    clock[0] += timedelta(days=1)
    db.init_db()
    assert birthday_row(path) == before
    assert birthday_row(path, user_id=2)[1] == START.isoformat(timespec="microseconds")


def test_migration_schema_and_backfill_roll_back_together(tmp_path, monkeypatch):
    import database as db

    path = tmp_path / "rollback.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE users (
            user_id INTEGER, chat_id INTEGER, username TEXT, first_name TEXT,
            beauty_count INTEGER DEFAULT 0, is_bot INTEGER DEFAULT 0,
            birthdate TEXT, PRIMARY KEY (user_id, chat_id))""")
        conn.execute(
            "INSERT INTO users (user_id, chat_id, birthdate) VALUES (1, ?, '15.04')",
            (CHAT_ID,),
        )
    monkeypatch.setattr(db, "DB_NAME", str(path))

    def fail_clock():
        raise RuntimeError("interrupted migration")

    monkeypatch.setattr(db, "_utc_now", fail_clock)
    with pytest.raises(RuntimeError, match="interrupted migration"):
        db.init_db()
    with sqlite3.connect(path) as conn:
        assert "birthday_changed_at" not in {
            row[1] for row in conn.execute("PRAGMA table_info(users)")
        }

    monkeypatch.setattr(db, "_utc_now", lambda: START)
    db.init_db()
    assert birthday_row(path) == ("15.04", START.isoformat(timespec="microseconds"))


def test_rolling_365_days_has_inclusive_boundary_and_no_rejected_write(
    temp_database, monkeypatch,
):
    import database as db

    clock = [START]
    monkeypatch.setattr(db, "_utc_now", lambda: clock[0])
    db.save_or_update_user(user(), CHAT_ID)
    assert db.set_user_birthdate_with_cooldown(CHAT_ID, 1, "29.02") == ("success", None)
    first_row = birthday_row(temp_database)
    assert first_row == ("29.02", START.isoformat(timespec="microseconds"))

    assert db.set_user_birthdate_with_cooldown(CHAT_ID, 1, "01.03") == (
        "cooldown", START + timedelta(days=365)
    )
    assert birthday_row(temp_database) == first_row
    clock[0] = START + timedelta(days=365, seconds=-1)
    assert db.set_user_birthdate_with_cooldown(CHAT_ID, 1, "01.03")[0] == "cooldown"
    assert birthday_row(temp_database) == first_row

    clock[0] = START + timedelta(days=365)
    assert db.set_user_birthdate_with_cooldown(CHAT_ID, 1, "01.03") == ("success", None)
    assert birthday_row(temp_database) == (
        "01.03", clock[0].isoformat(timespec="microseconds")
    )
    assert db.set_user_birthdate_with_cooldown(CHAT_ID, 1, "02.03") == (
        "cooldown", clock[0] + timedelta(days=365)
    )


@pytest.mark.parametrize("date_text", ["31.04", "29.02.2025", "00.01", "15.13", "2026-09-23", "1.01", "01.01 extra"])
def test_invalid_dates_do_not_write(temp_database, monkeypatch, date_text):
    import database as db

    clock = [START]
    monkeypatch.setattr(db, "_utc_now", lambda: clock[0])
    db.save_or_update_user(user(), CHAT_ID)
    before = birthday_row(temp_database)
    assert db.set_user_birthdate_with_cooldown(CHAT_ID, 1, date_text) == ("invalid", None)
    assert birthday_row(temp_database) == before


def test_cooldown_is_per_chat_and_per_user_and_concurrent_updates_serialize(
    temp_database, monkeypatch,
):
    import database as db

    clock = [START]
    monkeypatch.setattr(db, "_utc_now", lambda: clock[0])
    for chat_id, person in ((CHAT_ID, user()), (CHAT_ID - 1, user()), (CHAT_ID, user(2, "bob"))):
        db.save_or_update_user(person, chat_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda bday: db.set_user_birthdate_with_cooldown(CHAT_ID, 1, bday),
            ("01.02", "02.03"),
        ))
    assert sorted(status for status, _ in results) == ["cooldown", "success"]
    stored_date, stored_time = birthday_row(temp_database)
    assert stored_date in {"01.02", "02.03"}
    assert stored_time == START.isoformat(timespec="microseconds")
    clock[0] = START + timedelta(days=365)
    with ThreadPoolExecutor(max_workers=2) as pool:
        later_results = list(pool.map(
            lambda bday: db.set_user_birthdate_with_cooldown(CHAT_ID, 1, bday),
            ("06.07", "07.08"),
        ))
    assert sorted(status for status, _ in later_results) == ["cooldown", "success"]
    assert birthday_row(temp_database)[1] == clock[0].isoformat(timespec="microseconds")
    assert db.set_user_birthdate_with_cooldown(CHAT_ID - 1, 1, "03.04") == ("success", None)
    assert db.set_user_birthdate_with_cooldown(CHAT_ID, 2, "04.05") == ("success", None)
    assert db.set_user_birthdate_with_cooldown(CHAT_ID, 3, "05.06") == ("not_found", None)
    assert birthday_row(temp_database)[0] in {"06.07", "07.08"}


def command_update(person, chat_id=CHAT_ID, reply_user=None):
    message = SimpleNamespace(
        from_user=person, chat_id=chat_id, message_id=1,
        reply_to_message=SimpleNamespace(from_user=reply_user) if reply_user else None,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        message=message, effective_user=person, effective_chat=SimpleNamespace(id=chat_id),
    )


@pytest.mark.asyncio
async def test_setbday_is_self_only_validated_and_shows_moscow_next_change(
    temp_database, monkeypatch, fake_context,
):
    import database as db
    from handlers import commands

    clock = [START]
    monkeypatch.setattr(db, "_utc_now", lambda: clock[0])
    monkeypatch.setattr(commands, "schedule_auto_delete", lambda *_args: None)
    alice, bob = user(), user(2, "bob")
    db.save_or_update_user(alice, CHAT_ID)
    db.save_or_update_user(bob, CHAT_ID)
    update = command_update(bob, reply_user=alice)

    fake_context.args = ["26.01"]
    await commands.set_bday_command(update, fake_context)
    assert birthday_row(temp_database, user_id=1) == (None, None)
    assert birthday_row(temp_database, user_id=2)[0] == "26.01"

    fake_context.args = ["@alice", "27.01"]
    await commands.set_bday_command(update, fake_context)
    assert "Использование: /setbday" in update.message.reply_text.await_args.args[0]
    assert birthday_row(temp_database, user_id=1) == (None, None)
    first_bob_row = birthday_row(temp_database, user_id=2)

    fake_context.args = ["31.04"]
    await commands.set_bday_command(update, fake_context)
    assert birthday_row(temp_database, user_id=2) == first_bob_row

    fake_context.args = ["27.01"]
    await commands.set_bday_command(update, fake_context)
    assert birthday_row(temp_database, user_id=2) == first_bob_row
    assert "23.09.2027 15:00:00 МСК" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_legacy_birthday_trigger_cannot_change_another_user_or_bypass_cooldown(
    temp_database, monkeypatch, fake_context,
):
    import database as db
    from handlers import triggers

    monkeypatch.setattr(db, "_utc_now", lambda: START)
    alice, bob = user(), user(2, "bob")
    db.save_or_update_user(alice, CHAT_ID)
    db.save_or_update_user(bob, CHAT_ID)

    async def send_trigger(text):
        message = SimpleNamespace(
            from_user=bob, chat_id=CHAT_ID, text=text, message_id=8,
            reply_text=AsyncMock(),
        )
        await triggers.respond_trigger(SimpleNamespace(message=message), fake_context)
        return message.reply_text.await_args.args[0]

    assert "только свой" in await send_trigger("@alice 01.02")
    assert birthday_row(temp_database, user_id=1) == (None, None)
    assert "Запомнил!" in await send_trigger("@bob 01.02")
    assert "23.09.2027 15:00:00 МСК" in await send_trigger("@bob 02.03")
    assert birthday_row(temp_database, user_id=2)[0] == "01.02"
