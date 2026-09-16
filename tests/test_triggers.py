from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from text_resources import get_text


class Message:
    def __init__(self, user, text, chat_id=-44, message_id=9):
        self.from_user = user
        self.text = text
        self.chat_id = chat_id
        self.message_id = message_id
        self.date = 1
        self.forward_origin = None
        self.reply_text = AsyncMock()
        self.reply_animation = AsyncMock()
        self.reply_sticker = AsyncMock()


@pytest.mark.asyncio
async def test_trigger_records_user_and_immediately_answers_first_yes(temp_database, monkeypatch, fake_context, tg_user):
    from handlers import triggers
    import database as db

    message = Message(tg_user, "да")
    update = SimpleNamespace(message=message)
    rolls = iter([0.99])
    monkeypatch.setattr(triggers.random, "random", lambda: next(rolls))
    await triggers.respond_trigger(update, fake_context)
    message.reply_text.assert_awaited_once_with(
        "Пизда",
        reply_to_message_id=message.message_id,
    )
    assert db.get_duel_user_by_username("tester", -44)
    assert db.get_all_pizda_candidates()[0]["used"] == 1
    with pytest.raises(StopIteration):
        next(rolls)


@pytest.mark.asyncio
async def test_trigger_birthday_assignment_returns_early(temp_database, fake_context, tg_user):
    from handlers import triggers
    import database as db

    db.save_or_update_user(tg_user, -44)
    message = Message(tg_user, "@tester 26.01.1994")
    await triggers.respond_trigger(SimpleNamespace(message=message), fake_context)
    assert db.get_user_birthdate_from_db(tg_user.id, -44) == "26.01.1994"
    message.reply_text.assert_awaited_once_with(
        "Запомнил! День рождения для @tester: 26.01.1994",
        reply_to_message_id=message.message_id,
    )


def test_trigger_text_resources_preserve_placeholders_and_emoji():
    assert get_text(
        "triggers.birthday.registration.missing_user",
        target_username="@tester",
    ) == "Пользователь @tester ещё не писал в этом чате, не могу сохранить дату."
    assert get_text("triggers.birthday.greeting", user_name="Tester") == "🎉 С днём рождения, Tester! 🥳🎂"
    assert get_text("triggers.responses.forwarded") == "Форварднул тебе за щеку, проверяй"
    assert get_text("triggers.responses.no") == "Пидора ответ"
