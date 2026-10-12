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
@pytest.mark.parametrize("registered", [False, True])
async def test_mwspukku_is_exact_and_has_no_profile_requirement(
    registered, temp_database, fake_context, tg_user,
):
    import database as db
    from handlers import commands
    from handlers.duel_messaging import delete_messages_job

    if registered:
        db.get_or_create_duel_user(tg_user, -44)
    message = SimpleNamespace(
        from_user=tg_user,
        chat_id=-44,
        message_id=777,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=778)),
    )
    update = SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=-44),
        effective_user=tg_user,
    )

    await commands.mwspukku_command(update, fake_context)

    message.reply_text.assert_awaited_once_with(
        "И вдобавок у вас украли команду", parse_mode="HTML", reply_markup=None,
    )
    assert fake_context.job_queue.calls == [(
        delete_messages_job, 60,
        {"data": {"chat_id": -44, "message_ids": [778, 777]}},
    )]
    with db.get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM duel_users WHERE chat_id = ? AND user_id = ?", (-44, tg_user.id),
        ).fetchone()[0] == int(registered)


@pytest.mark.asyncio
async def test_mwspukku_works_for_deleted_gnome(temp_database, fake_context, tg_user):
    import database as db
    from handlers import commands

    db.get_or_create_duel_user(tg_user, -44)
    assert db.delete_gnome(tg_user.id, tg_user.username)
    message = SimpleNamespace(
        from_user=tg_user,
        chat_id=-44,
        message_id=779,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=780)),
    )
    update = SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=-44),
        effective_user=tg_user,
    )

    await commands.mwspukku_command(update, fake_context)

    message.reply_text.assert_awaited_once_with(
        "И вдобавок у вас украли команду", parse_mode="HTML", reply_markup=None,
    )
    assert db.is_deleted_user(tg_user.id)


@pytest.mark.asyncio
async def test_set_bday_command_saves_own_date(monkeypatch, temp_database, fake_context, tg_user):
    from handlers import commands
    import database

    message = _message(tg_user)
    fake_context.args = ["26.01"]
    monkeypatch.setattr(commands, "schedule_auto_delete", lambda *_args: None)

    await commands.set_bday_command(
        SimpleNamespace(message=message, effective_user=tg_user, effective_chat=SimpleNamespace(id=-44)),
        fake_context,
    )

    message.reply_text.assert_awaited_once_with(
        "Твой день рождения сохранён (26.01).",
        parse_mode=None,
    )
    assert database.get_user_birthdate_from_db(tg_user.id, -44) == "26.01"


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
