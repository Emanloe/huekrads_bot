import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


CHAT_ID = -700


def user(user_id=1, username="Emanloe"):
    return SimpleNamespace(
        id=user_id, username=username, first_name=username,
        last_name=None, is_bot=False,
    )


def name_update(tg_user, text, chat_id=CHAT_ID, reply_user=None):
    message = SimpleNamespace(
        text=text, from_user=tg_user, chat_id=chat_id,
        chat=SimpleNamespace(id=chat_id),
        reply_to_message=SimpleNamespace(from_user=reply_user) if reply_user else None,
    )
    return SimpleNamespace(message=message, effective_user=tg_user, effective_chat=message.chat)


def test_existing_database_migrates_without_losing_rows(tmp_path, monkeypatch):
    import database as db

    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE duel_users (
            user_id INTEGER, chat_id INTEGER, username TEXT, display_name TEXT NOT NULL,
            points INTEGER DEFAULT 20, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
            stolen_dicks_count INTEGER DEFAULT 0, dick_stolen_count INTEGER DEFAULT 0,
            dick_stolen_today INTEGER DEFAULT 0, last_activity_date TEXT,
            last_stolen_by TEXT, daily_wins INTEGER DEFAULT 0,
            bosses_defeated INTEGER DEFAULT 0, PRIMARY KEY(user_id, chat_id))""")
        conn.execute(
            "INSERT INTO duel_users (user_id, chat_id, username, display_name, points, wins) "
            "VALUES (1, -700, 'Emanloe', 'Emanloe', 73, 9)"
        )
    monkeypatch.setattr(db, "DB_NAME", str(path))
    db.init_db()
    db.init_db()
    with sqlite3.connect(path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(duel_users)")]
        row = conn.execute(
            "SELECT points, wins, dwarf_name FROM duel_users WHERE chat_id = -700 AND user_id = 1"
        ).fetchone()
    assert columns.count("dwarf_name") == 1
    assert row == (73, 9, None)
    assert db.format_user_title(db.get_or_create_duel_user(user(), CHAT_ID)) == "Emanloe"


@pytest.mark.asyncio
async def test_name_command_first_set_multiple_words_whitespace_and_reply_ownership(
    temp_database, monkeypatch, fake_context,
):
    import database as db
    from handlers import duel_name

    first, other = user(), user(2, "Other")
    db.get_or_create_duel_user(first, CHAT_ID)
    db.get_or_create_duel_user(other, CHAT_ID)
    sent = AsyncMock()
    monkeypatch.setattr(duel_name, "send_and_schedule", sent)

    await duel_name.name_command(
        name_update(first, "/name   Вася    Железная   Жопа  ", reply_user=other), fake_context
    )
    assert db.get_duel_dwarf_name(CHAT_ID, first.id) == "Вася Железная Жопа"
    assert db.get_duel_dwarf_name(CHAT_ID, other.id) is None
    assert "Вася Железная Жопа" in sent.await_args.args[2]

    await duel_name.name_command(name_update(first, "/name Второй"), fake_context)
    assert db.get_duel_dwarf_name(CHAT_ID, first.id) == "Вася Железная Жопа"
    assert "Вася Железная Жопа" in sent.await_args.args[2]


@pytest.mark.asyncio
async def test_name_requires_registration_and_is_scoped_to_chat(
    temp_database, monkeypatch, fake_context,
):
    import database as db
    from handlers import duel_name

    person = user()
    sent = AsyncMock()
    monkeypatch.setattr(duel_name, "send_and_schedule", sent)
    await duel_name.name_command(name_update(person, "/name Гномыч"), fake_context)
    assert "Сначала стань гномом" in sent.await_args.args[2]
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_users").fetchone()[0] == 0

    for chat_id in (CHAT_ID, CHAT_ID - 1):
        db.get_or_create_duel_user(person, chat_id)
    await duel_name.name_command(name_update(person, "/name Первый"), fake_context)
    await duel_name.name_command(name_update(person, "/name Второй", CHAT_ID - 1), fake_context)
    assert db.get_duel_dwarf_name(CHAT_ID, person.id) == "Первый"
    assert db.get_duel_dwarf_name(CHAT_ID - 1, person.id) == "Второй"


@pytest.mark.parametrize("raw,error", [
    ("", "missing"), ("   ", "missing"),
    ("a" * 41, "too_long"), ("a\nb", "invalid"),
    ("a\tb", "invalid"), ("a\x00b", "invalid"),
])
def test_name_validation(raw, error):
    from handlers.duel_name import normalize_dwarf_name

    assert normalize_dwarf_name(raw) == (None, error)
    assert normalize_dwarf_name("  Гном   Гномыч  ") == ("Гном Гномыч", None)
    assert normalize_dwarf_name("я" * 40) == ("я" * 40, None)


@pytest.mark.asyncio
async def test_invalid_commands_leave_registered_name_unset(
    temp_database, monkeypatch, fake_context,
):
    import database as db
    from handlers import duel_name

    person = user()
    db.get_or_create_duel_user(person, CHAT_ID)
    sent = AsyncMock()
    monkeypatch.setattr(duel_name, "send_and_schedule", sent)
    for text in ("/name", "/name " + "я" * 41, "/name a\nb"):
        await duel_name.name_command(name_update(person, text), fake_context)
        assert db.get_duel_dwarf_name(CHAT_ID, person.id) is None
    assert sent.await_count == 3


@pytest.mark.asyncio
async def test_name_response_escapes_user_html(temp_database, monkeypatch, fake_context):
    import database as db
    from handlers import duel_name

    person = user()
    db.get_or_create_duel_user(person, CHAT_ID)
    sent = AsyncMock()
    monkeypatch.setattr(duel_name, "send_and_schedule", sent)
    await duel_name.name_command(name_update(person, "/name <b>ХУЙ</b> & Гном"), fake_context)
    assert db.get_duel_dwarf_name(CHAT_ID, person.id) == "<b>ХУЙ</b> & Гном"
    assert "&lt;b&gt;ХУЙ&lt;/b&gt; &amp; Гном" in sent.await_args.args[2]

    await duel_name.name_command(name_update(person, "/name Новый"), fake_context)
    assert "&lt;b&gt;ХУЙ&lt;/b&gt; &amp; Гном" in sent.await_args.args[2]


def test_atomic_set_once_and_distinct_users(temp_database):
    import database as db

    db.get_or_create_duel_user(user(), CHAT_ID)
    db.get_or_create_duel_user(user(2, "Other"), CHAT_ID)

    def set_name(name):
        return db.set_duel_dwarf_name_once(CHAT_ID, 1, name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(set_name, ("Первый", "Второй")))
    assert sorted(status for status, _ in outcomes) == ["already_named", "set"]
    stored = db.get_duel_dwarf_name(CHAT_ID, 1)
    assert stored in {"Первый", "Второй"}
    assert all(value == stored for _, value in outcomes)
    assert db.set_duel_dwarf_name_once(CHAT_ID, 1, "Третий") == ("already_named", stored)
    assert db.set_duel_dwarf_name_once(CHAT_ID, 3, "Чужой") == ("not_registered", None)
    assert db.get_duel_dwarf_name(CHAT_ID, 2) is None


def test_formatter_plain_and_html_safety(temp_database):
    import database as db

    person = user(username=None)
    person.first_name = "<Eman & Loe>"
    db.get_or_create_duel_user(person, CHAT_ID)
    assert db.format_user_title(db.get_or_create_duel_user(person, CHAT_ID)) == "&lt;Eman &amp; Loe&gt;"
    assert db.set_duel_dwarf_name_once(CHAT_ID, 1, "<b>ХУЙ</b> & Гном") == (
        "set", "<b>ХУЙ</b> & Гном"
    )
    participant = db.get_or_create_duel_user(person, CHAT_ID)
    assert db.format_user_title_plain(participant) == "<b>ХУЙ</b> & Гном (<Eman & Loe>)"
    assert db.format_user_title_plain(participant, include_dwarf_name=False) == "<Eman & Loe>"
    assert db.format_user_title(participant) == (
        "&lt;b&gt;ХУЙ&lt;/b&gt; &amp; Гном (&lt;Eman &amp; Loe&gt;)"
    )


@pytest.mark.asyncio
async def test_runtime_stats_selection_duel_start_and_leaderboard(
    temp_database, monkeypatch, fake_context,
):
    import database as db
    from handlers import duel

    first, second = user(), user(2, "Other")
    db.get_or_create_duel_user(first, CHAT_ID)
    db.get_or_create_duel_user(second, CHAT_ID)
    db.set_duel_dwarf_name_once(CHAT_ID, 1, "Гномыч")
    db.set_duel_dwarf_name_once(CHAT_ID, 2, "<b>Второй</b>")
    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    update = name_update(first, "/duel_stats")
    await duel.duel_stats_command(update, fake_context)
    assert "Гномыч (Emanloe)" in sent.await_args.args[2]
    assert "Emanloe (Гномыч)" not in sent.await_args.args[2]
    await duel.duel_top_command(update, fake_context)
    assert "&lt;b&gt;Второй&lt;/b&gt; (Other)" in sent.await_args.args[2]
    await duel.duel_command(update, fake_context)
    buttons = sent.await_args.kwargs["reply_markup"].inline_keyboard
    assert any("<b>Второй</b> (Other)" in button.text for row in buttons for button in row)

    def close_task(coroutine):
        coroutine.close()
        return SimpleNamespace(cancel=lambda: None, done=lambda: False)

    monkeypatch.setattr(duel.asyncio, "create_task", close_task)
    await duel._start_interactive_fight(
        fake_context, CHAT_ID, first, second,
        db.get_or_create_duel_user(first, CHAT_ID),
        db.get_or_create_duel_user(second, CHAT_ID),
    )
    text = fake_context.bot.send_message.await_args.kwargs["text"]
    assert "Гномыч (Emanloe)" in text
    assert "&lt;b&gt;Второй&lt;/b&gt; (Other)" in text
    duel.ACTIVE_DUELS.pop(CHAT_ID)


@pytest.mark.asyncio
async def test_runtime_item_boss_and_hyperborean_paths(temp_database, monkeypatch, fake_context):
    import database as db
    from handlers import duel, duel_items, duel_formatting, boss_presentation
    from handlers import hyperborean_event as hyper
    from handlers import boss_registration

    person = user()
    db.get_or_create_duel_user(person, CHAT_ID)
    db.set_duel_dwarf_name_once(CHAT_ID, 1, "<b>Гномыч</b>")
    title = "&lt;b&gt;Гномыч&lt;/b&gt; (Emanloe)"

    event_id = db.create_duel_item_event(CHAT_ID)
    db.set_duel_item_event_message(event_id, 77)
    monkeypatch.setattr(duel_items.random, "choice", lambda items: items[0])
    query = SimpleNamespace(data=f"duel_item_claim_{event_id}", from_user=person, answer=AsyncMock())
    await duel_items.duel_item_event_callback(
        SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT_ID)), fake_context
    )
    assert title in fake_context.bot.edit_message_text.await_args.kwargs["text"]

    monkeypatch.setattr(boss_registration, "_BOSS_REG_DB_PATH", temp_database)
    assert boss_registration._boss_register_user(CHAT_ID, person)
    registered = boss_registration._boss_get_registered_users(CHAT_ID)
    participant = duel._boss_make_participant(
        duel._boss_tg_user_from_registration(registered[0]), CHAT_ID
    )
    battle = {"participants": {1: participant}, "phase": "attack", "boss": {"name": "Босс"},
              "hits": 0, "round": 1, "message_id": 77}
    assert title in duel_formatting._boss_players_status_text(battle)
    assert title in boss_presentation._boss_final_report(battle, victory=True)
    monkeypatch.setattr(duel.random, "random", lambda: 0.0)
    monkeypatch.setattr(duel.random, "choice", lambda options: options[0])
    await duel._boss_send_final_report(fake_context, CHAT_ID, battle, victory=True)
    report = fake_context.bot.edit_message_text.await_args.kwargs["text"]
    assert title in report
    assert f"Добычу забирает <b>{title}</b>" in report

    monkeypatch.setattr(hyper, "_HYPERBOREAN_DB_PATH", temp_database)
    monkeypatch.setattr(hyper.random, "choice", lambda options: options[0])
    _, selected, _ = hyper._claim_hyperboreic_huy_for_other(CHAT_ID, person)
    assert db.format_user_title(selected) == title
    hyper.ACTIVE_HYPERBOREAN_EVENTS[CHAT_ID] = {"message_id": 88, "event_type": "arthur"}
    fake_context.bot.edit_message_reply_markup = AsyncMock()
    query = SimpleNamespace(data="hyperboreic_huy_other", from_user=person, answer=AsyncMock())
    await hyper.hyperboreic_huy_callback(
        SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT_ID)),
        fake_context,
    )
    assert title in fake_context.bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_runtime_duel_result_shows_both_named_players(temp_database, monkeypatch, fake_context):
    import database as db
    from handlers import duel

    first, second = user(), user(2, "Other")
    winner = db.get_or_create_duel_user(first, CHAT_ID)
    loser = db.get_or_create_duel_user(second, CHAT_ID)
    db.set_duel_dwarf_name_once(CHAT_ID, 1, "Гномыч")
    db.set_duel_dwarf_name_once(CHAT_ID, 2, "<i>Лузер</i>")
    winner = db.get_or_create_duel_user(first, CHAT_ID)
    loser = db.get_or_create_duel_user(second, CHAT_ID)
    monkeypatch.setattr(duel.random, "random", lambda: 1.0)
    monkeypatch.setattr(duel, "DUEL_POST_MESSAGES", ())

    await duel._finish_duel(fake_context, CHAT_ID, winner, loser, "")
    result = fake_context.bot.send_message.await_args.kwargs["text"]
    assert "Гномыч (Emanloe)" in result
    assert "&lt;i&gt;Лузер&lt;/i&gt; (Other)" in result
