from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class FakeJobQueue:
    def __init__(self):
        self.calls = []

    def run_once(self, callback, when, **kwargs):
        self.calls.append((callback, when, kwargs))
        return SimpleNamespace(schedule_removal=lambda: None)

    def get_jobs_by_name(self, _name):
        return []


class FakeBot:
    def __init__(self):
        self.send_message = AsyncMock(return_value=SimpleNamespace(message_id=101))
        self.edit_message_text = AsyncMock()
        self.delete_message = AsyncMock()
        self.set_message_reaction = AsyncMock()
        self.answer_inline_query = AsyncMock()
        self.edit_message_media = AsyncMock()
        self.get_chat = AsyncMock(return_value=SimpleNamespace(birthdate=None))


@pytest.fixture
def temp_database(tmp_path, monkeypatch):
    """Redirect the shared database module before each DB-dependent test."""
    import database

    db_path = tmp_path / "bot_database.db"
    monkeypatch.setattr(database, "DB_NAME", str(db_path))
    database.init_db()
    return db_path


@pytest.fixture
def fake_context():
    return SimpleNamespace(
        bot=FakeBot(),
        job_queue=FakeJobQueue(),
        user_data={},
        bot_data={},
        args=[],
        job=SimpleNamespace(data={}),
    )


@pytest.fixture
def tg_user():
    return SimpleNamespace(
        id=1001,
        username="tester",
        first_name="Tester",
        last_name="User",
        is_bot=False,
    )


@pytest.fixture
def open_duel_selection():
    """Create buttons through /duel, including their server-side ownership."""
    next_message_id = 700

    async def open_selection(context, actor, chat_id):
        from handlers import duel

        nonlocal next_message_id
        next_message_id += 2
        sent = SimpleNamespace(message_id=next_message_id, delete=AsyncMock(),
                               is_accessible=True)
        message = SimpleNamespace(
            from_user=actor, chat=SimpleNamespace(id=chat_id), chat_id=chat_id,
            message_id=next_message_id - 1, text="/duel",
            reply_text=AsyncMock(return_value=sent),
        )
        update = SimpleNamespace(message=message, effective_chat=message.chat)
        await duel.duel_command(update, context)
        sent.reply_markup = message.reply_text.await_args.kwargs["reply_markup"]
        return sent

    return open_selection
