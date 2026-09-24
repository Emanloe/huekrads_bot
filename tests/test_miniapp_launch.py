"""The Mini App entry takes chat and user IDs only from a Telegram update."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

import database
from handlers.miniapp import duel_app_command
from miniapp_sessions import exchange_launch_token


def update(chat_id, chat_type, user_id=101):
    actor = SimpleNamespace(id=user_id, username=f"player{user_id}", first_name="Player",
                            last_name=None, is_bot=False)
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id,
                               type=chat_type), effective_user=actor)


@pytest.mark.asyncio
async def test_group_command_issues_bound_link_without_game_rng(temp_database, monkeypatch):
    request = update(-9901, "supergroup")
    database.get_or_create_duel_user(request.effective_user, -9901)
    context = SimpleNamespace(bot=SimpleNamespace(username="ExampleBot"))
    from handlers import duel_service
    monkeypatch.setattr(duel_service, "random", SimpleNamespace(
        random=lambda: (_ for _ in ()).throw(AssertionError("game RNG")),
        choice=lambda _: (_ for _ in ()).throw(AssertionError("game RNG")),
    ))
    await duel_app_command(request, context)
    button = request.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    parsed = urlparse(button.url)
    assert parsed.scheme == "https" and parsed.netloc == "t.me"
    launch = parse_qs(parsed.query)["startapp"][0]
    assert len(launch) <= 64
    assert exchange_launch_token(launch, 202) is None
    issued = exchange_launch_token(launch, 101)
    assert (issued.session.chat_id, issued.session.user_id) == (-9901, 101)
    assert exchange_launch_token(launch, 101) is None


@pytest.mark.asyncio
async def test_private_and_unregistered_user_get_no_token(temp_database):
    private = update(101, "private")
    missing = update(-9901, "group")
    context = SimpleNamespace(bot=SimpleNamespace(username="ExampleBot"))
    await duel_app_command(private, context)
    await duel_app_command(missing, context)
    assert "группе" in private.message.reply_text.await_args.args[0]
    assert "зарегистрируйтесь" in missing.message.reply_text.await_args.args[0]
    with database.get_db() as conn:
        assert conn.execute("SELECT count(*) FROM miniapp_launch_tokens").fetchone()[0] == 0
