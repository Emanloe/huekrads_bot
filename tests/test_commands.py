from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from text_resources import get_text


def _message(user, chat_id=-44):
    return SimpleNamespace(
        from_user=user,
        chat_id=chat_id,
        reply_text=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_top_command_preserves_html_template_and_trailing_newlines(monkeypatch, fake_context, tg_user):
    from handlers import commands

    message = _message(tg_user)
    monkeypatch.setattr(commands, "schedule_auto_delete", lambda *_args: None)
    monkeypatch.setattr(commands, "get_top_beauties", lambda *_args, **_kwargs: [("@alice", 2), (None, 1)])

    await commands.top_command(SimpleNamespace(message=message), fake_context)

    message.reply_text.assert_awaited_once_with(
        "🏆 <b>Топ пидоров чата:</b>\n\n1. <b>alice</b> — 2 раз(а)\n2. <b>Аноним</b> — 1 раз(а)\n",
        parse_mode="HTML",
    )


@pytest.mark.asyncio
async def test_donate_command_replies_with_yaml_text(fake_context, tg_user):
    from handlers import commands

    message = _message(tg_user)

    await commands.donate_command(SimpleNamespace(message=message), fake_context)

    message.reply_text.assert_awaited_once_with(
        "Поддержать хуекрадство рублём:\nhttps://t.me/tribute/app?startapp=dQyP",
        parse_mode=None,
    )


@pytest.mark.asyncio
async def test_set_bday_command_preserves_dynamic_reply(monkeypatch, fake_context, tg_user):
    from handlers import commands

    message = _message(tg_user)
    fake_context.args = ["@alice", "26.01"]
    monkeypatch.setattr(commands, "schedule_auto_delete", lambda *_args: None)
    monkeypatch.setattr(commands, "is_admin", lambda _user_id: True)
    monkeypatch.setattr(commands, "save_custom_birthdate", lambda *_args: True)

    await commands.set_bday_command(SimpleNamespace(message=message), fake_context)

    message.reply_text.assert_awaited_once_with(
        "День рождения для alice успешно сохранён (26.01).",
        parse_mode=None,
    )


@pytest.mark.asyncio
async def test_toggle_forward_command_preserves_enabled_status(monkeypatch, fake_context, tg_user):
    from handlers import commands

    message = _message(tg_user)
    monkeypatch.setattr(commands, "schedule_auto_delete", lambda *_args: None)
    monkeypatch.setattr(commands, "is_admin", lambda _user_id: True)
    monkeypatch.setattr(commands, "is_forward_reply_enabled", lambda _chat_id: False)
    monkeypatch.setattr(commands, "set_forward_reply_enabled", lambda *_args: None)

    await commands.toggle_forward_reply_command(SimpleNamespace(message=message), fake_context)

    message.reply_text.assert_awaited_once_with(
        "Ответ 'Форвардни себе за щеку' включен.",
        parse_mode=None,
    )


def test_command_text_resources_preserve_unicode_placeholders_and_multiline():
    assert get_text("commands.admin.only") == "⛔ Эта команда доступна только администраторам."
    assert get_text("commands.force_pidor.winner", username="alice", count=2) == "Пидор дня — alice! Он был пидором уже 2 раз(а)."
    assert get_text("commands.top.header") == "🏆 <b>Топ пидоров чата:</b>\n\n"
    assert get_text("commands.top.item", index=1, username="Аноним", count=1) == "1. <b>Аноним</b> — 1 раз(а)\n"
