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


def _reply_message(user_id):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id))


def _entity(entity_type, offset, length, *, user_id=None):
    return SimpleNamespace(
        type=entity_type,
        offset=offset,
        length=length,
        user=SimpleNamespace(id=user_id) if user_id is not None else None,
    )


@pytest.mark.asyncio
async def test_amigo_reply_to_rost_sends_configured_gif(fake_context):
    from handlers import triggers

    amigo = SimpleNamespace(id=6551933006, first_name="Amigo")
    message = Message(amigo, "любой текст")
    message.reply_to_message = _reply_message(328681951)

    await triggers.amigo_reply_to_rost_trigger(SimpleNamespace(message=message), fake_context)

    message.reply_animation.assert_awaited_once_with(
        animation=triggers.AMIGO_REPLY_GIF_ID,
        reply_to_message_id=message.message_id,
    )


@pytest.mark.asyncio
async def test_amigo_reply_to_rost_does_not_also_run_random_amigo_replies(
    temp_database, fake_context,
):
    from handlers import triggers

    amigo = SimpleNamespace(
        id=6551933006, first_name="Amigo", username="amigo", last_name=None, is_bot=False,
    )
    message = Message(amigo, "любой текст")
    message.reply_to_message = _reply_message(328681951)
    message.entities = [_entity("mention", 0, len("@Rost_is_luv"))]
    message.text = "@Rost_is_luv любой текст"
    update = SimpleNamespace(message=message)

    await triggers.amigo_reply_to_rost_trigger(update, fake_context)
    await triggers.respond_trigger(update, fake_context)

    message.reply_animation.assert_awaited_once_with(
        animation=triggers.AMIGO_REPLY_GIF_ID,
        reply_to_message_id=message.message_id,
    )
    message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_amigo_reply_to_another_user_does_not_send_gif(fake_context):
    from handlers import triggers

    message = Message(SimpleNamespace(id=6551933006, first_name="Amigo"), "текст")
    message.reply_to_message = _reply_message(7)

    await triggers.amigo_reply_to_rost_trigger(SimpleNamespace(message=message), fake_context)

    message.reply_animation.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_user_reply_to_rost_does_not_send_gif(fake_context):
    from handlers import triggers

    message = Message(SimpleNamespace(id=7, first_name="Другой"), "текст")
    message.reply_to_message = _reply_message(328681951)

    await triggers.amigo_reply_to_rost_trigger(SimpleNamespace(message=message), fake_context)

    message.reply_animation.assert_not_awaited()


@pytest.mark.asyncio
async def test_amigo_entity_mention_of_rost_sends_gif_case_insensitively(fake_context):
    from handlers import triggers

    message = Message(SimpleNamespace(id=6551933006, first_name="Amigo"), "@rOsT_Is_LuV")
    message.entities = [_entity("mention", 0, len("@rOsT_Is_LuV"))]

    await triggers.amigo_reply_to_rost_trigger(SimpleNamespace(message=message), fake_context)

    message.reply_animation.assert_awaited_once_with(
        animation=triggers.AMIGO_REPLY_GIF_ID,
        reply_to_message_id=message.message_id,
    )


@pytest.mark.asyncio
async def test_amigo_text_mention_and_caption_mention_of_rost_send_gif(fake_context):
    from handlers import triggers

    text_mention = Message(SimpleNamespace(id=6551933006, first_name="Amigo"), "ссылка")
    text_mention.entities = [_entity("text_mention", 0, len("ссылка"), user_id=328681951)]
    caption_mention = Message(SimpleNamespace(id=6551933006, first_name="Amigo"), None)
    caption_mention.caption = "@Rost_is_luv"
    caption_mention.caption_entities = [_entity("mention", 0, len("@Rost_is_luv"))]

    await triggers.amigo_reply_to_rost_trigger(SimpleNamespace(message=text_mention), fake_context)
    await triggers.amigo_reply_to_rost_trigger(SimpleNamespace(message=caption_mention), fake_context)

    text_mention.reply_animation.assert_awaited_once()
    caption_mention.reply_animation.assert_awaited_once()


@pytest.mark.asyncio
async def test_amigo_plain_text_or_similar_username_does_not_send_gif(fake_context):
    from handlers import triggers

    plain_text = Message(SimpleNamespace(id=6551933006, first_name="Amigo"), "@Rost_is_luv")
    similar = Message(SimpleNamespace(id=6551933006, first_name="Amigo"), "@Rost_is_luv123")
    similar.entities = [_entity("mention", 0, len("@Rost_is_luv123"))]

    await triggers.amigo_reply_to_rost_trigger(SimpleNamespace(message=plain_text), fake_context)
    await triggers.amigo_reply_to_rost_trigger(SimpleNamespace(message=similar), fake_context)

    plain_text.reply_animation.assert_not_awaited()
    similar.reply_animation.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_user_or_wrong_text_mention_does_not_send_gif(fake_context):
    from handlers import triggers

    other_user = Message(SimpleNamespace(id=7, first_name="Другой"), "@Rost_is_luv")
    other_user.entities = [_entity("mention", 0, len("@Rost_is_luv"))]
    wrong_target = Message(SimpleNamespace(id=6551933006, first_name="Amigo"), "ссылка")
    wrong_target.entities = [_entity("text_mention", 0, len("ссылка"), user_id=7)]

    await triggers.amigo_reply_to_rost_trigger(SimpleNamespace(message=other_user), fake_context)
    await triggers.amigo_reply_to_rost_trigger(SimpleNamespace(message=wrong_target), fake_context)

    other_user.reply_animation.assert_not_awaited()
    wrong_target.reply_animation.assert_not_awaited()


@pytest.mark.asyncio
async def test_amigo_reply_without_original_sender_does_not_send_gif(fake_context):
    from handlers import triggers

    message = Message(SimpleNamespace(id=6551933006, first_name="Amigo"), "текст")
    message.reply_to_message = SimpleNamespace(from_user=None)

    await triggers.amigo_reply_to_rost_trigger(SimpleNamespace(message=message), fake_context)

    message.reply_animation.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_amigo_rule_still_replies_to_other_text(temp_database, fake_context, monkeypatch):
    from handlers import triggers

    message = Message(SimpleNamespace(
        id=6551933006, first_name="Amigo", username="amigo", last_name=None, is_bot=False,
    ), "обычный текст")
    rolls = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(triggers.random, "random", lambda: next(rolls))

    await triggers.respond_trigger(SimpleNamespace(message=message), fake_context)

    message.reply_text.assert_awaited_once_with(
        get_text("triggers.responses.amigo"),
        reply_to_message_id=message.message_id,
    )
    message.reply_animation.assert_not_awaited()


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
