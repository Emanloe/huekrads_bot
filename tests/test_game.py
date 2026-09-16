from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_run_game_sends_existing_two_message_sequence(monkeypatch, fake_context):
    from handlers import game

    monkeypatch.setattr(game, "pick_beauty_of_the_day", lambda chat_id: ("alice", 2))
    monkeypatch.setattr(game.asyncio, "sleep", AsyncMock())
    await game.run_pidor_game_in_chat(fake_context, -1)
    assert fake_context.bot.send_message.await_count == 2
    assert fake_context.bot.send_message.await_args_list[0].kwargs == {
        "chat_id": -1,
        "text": "Выбираем пидора дня...",
    }
    assert fake_context.bot.send_message.await_args_list[1].kwargs == {
        "chat_id": -1,
        "text": "Пидор дня — alice. Он был пидором 2 раза.",
    }


def test_game_text_resources_preserve_plural_forms_and_placeholders():
    from handlers import game

    assert [game.get_plural_raz(count) for count in (1, 2, 5, 11)] == [
        "раз",
        "раза",
        "раз",
        "раз",
    ]


@pytest.mark.asyncio
async def test_run_game_no_participants_preserves_message(monkeypatch, fake_context):
    from handlers import game

    monkeypatch.setattr(game, "pick_beauty_of_the_day", lambda _chat_id: None)

    await game.run_pidor_game_in_chat(fake_context, -1)

    fake_context.bot.send_message.assert_awaited_once_with(
        chat_id=-1,
        text="В этом чате пока нет зарегистрированных участников!",
    )


@pytest.mark.asyncio
async def test_daily_game_continues_after_chat_error(monkeypatch, fake_context):
    from handlers import game

    calls = []
    monkeypatch.setattr(game, "get_all_chats", lambda: [-1, -2])

    async def run(_context, chat_id):
        calls.append(chat_id)
        if chat_id == -1:
            raise RuntimeError("expected test failure")

    monkeypatch.setattr(game, "run_pidor_game_in_chat", run)
    await game.daily_beauty_job(fake_context)
    assert calls == [-1, -2]
