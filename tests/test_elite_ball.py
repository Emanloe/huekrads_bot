"""One-shot elite ball behavior and handler routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, MessageHandler
from text_resources import get_text_list


CHAT_ID = -830
pytestmark = pytest.mark.usefixtures("temp_database")


def charges():
    from database import get_db

    with get_db() as conn:
        return set(conn.execute("SELECT chat_id, user_id FROM elite_ball_activations"))


ANSWERS = ("Да", "Нет", "Возможно", "Увлажните шар гнома усерднее")


def test_answer_resource_contains_the_four_existing_responses():
    assert get_text_list("elite_ball.answers") == list(ANSWERS)


def callback_update(user_id=1, chat_id=CHAT_ID):
    from handlers.elite_ball import ELITE_BALL_CALLBACK_DATA

    query = SimpleNamespace(
        data=ELITE_BALL_CALLBACK_DATA,
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        answer=AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=query, effective_chat=SimpleNamespace(id=chat_id),
    ), query


def text_update(user_id=1, chat_id=CHAT_ID, text="Вопрос", *, is_bot=False):
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, is_bot=is_bot),
        message_id=71,
        text=text,
        reply_text=AsyncMock(),
        reply_photo=AsyncMock(),
    )
    return SimpleNamespace(
        message=message, effective_chat=SimpleNamespace(id=chat_id),
    ), message


@pytest.mark.asyncio
async def test_start_menu_contains_elite_ball_button_exactly_once(fake_context, monkeypatch):
    from handlers import commands, elite_ball

    monkeypatch.setattr(commands, "save_or_update_user", Mock())
    reply = AsyncMock()
    monkeypatch.setattr(commands, "reply_or_send", reply)
    start = SimpleNamespace(message=SimpleNamespace(
        from_user=SimpleNamespace(id=1), chat_id=CHAT_ID,
    ))
    await commands.start_command(start, fake_context)

    reply.assert_awaited_once()
    markup = reply.await_args.kwargs["reply_markup"]
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [(button.text, button.callback_data) for button in buttons] == [
        ("Элитный мячик знание", elite_ball.ELITE_BALL_CALLBACK_DATA),
    ]


@pytest.mark.asyncio
async def test_click_waits_for_exact_user_and_chat_without_rng(fake_context, monkeypatch):
    from handlers import elite_ball

    choice = Mock(side_effect=AssertionError("click used RNG"))
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    update, query = callback_update()
    await elite_ball.elite_ball_callback(update, fake_context)
    query.answer.assert_awaited_once_with()
    fake_context.bot.send_message.assert_awaited_once_with(
        chat_id=CHAT_ID, text="Шар ожидает вопрос:",
    )
    assert charges() == {(CHAT_ID, 1)}
    choice.assert_not_called()

    other_update, other_message = text_update(user_id=2)
    await elite_ball.elite_ball_question(other_update, fake_context)
    other_message.reply_photo.assert_not_awaited()
    assert charges() == {(CHAT_ID, 1)}
    choice.assert_not_called()


@pytest.mark.asyncio
async def test_one_question_one_choice_reply_and_then_no_waiting(fake_context, monkeypatch):
    from handlers import elite_ball

    update, _ = callback_update()
    await elite_ball.elite_ball_callback(update, fake_context)
    choice = Mock(return_value="Да")
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    random_roll = Mock(return_value=0.5)
    monkeypatch.setattr(elite_ball.random, "random", random_roll)
    question, message = text_update(text="А завтра?")
    with pytest.raises(ApplicationHandlerStop):
        await elite_ball.elite_ball_question(question, fake_context)
    choice.assert_called_once_with(list(ANSWERS[:3]))
    random_roll.assert_called_once_with()
    message.reply_photo.assert_awaited_once_with(
        photo=elite_ball.ELITE_BALL_PHOTO_FILE_ID,
        caption="Да",
        reply_to_message_id=71,
    )
    message.reply_text.assert_not_awaited()
    assert charges() == set()

    next_update, next_message = text_update(text="Ещё вопрос")
    await elite_ball.elite_ball_question(next_update, fake_context)
    next_message.reply_photo.assert_not_awaited()
    choice.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ANSWERS[:3])
async def test_primary_answers_are_chosen_uniformly_after_the_rare_check(
    fake_context, monkeypatch, answer,
):
    from handlers import elite_ball

    await elite_ball.elite_ball_callback(callback_update()[0], fake_context)
    choice = Mock(return_value=answer)
    random_roll = Mock(return_value=0.05)
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    monkeypatch.setattr(elite_ball.random, "random", random_roll)
    update, message = text_update()
    with pytest.raises(ApplicationHandlerStop):
        await elite_ball.elite_ball_question(update, fake_context)
    choice.assert_called_once_with(list(ANSWERS[:3]))
    random_roll.assert_called_once_with()
    message.reply_photo.assert_awaited_once_with(
        photo="AgACAgIAAxkBAAPYarOx_Ot9KPfYe1lKYZsAAaKkq7kLAAIsIGsbFCShScvsbybBp2qPAQADAgADeAADPQQ",
        caption=answer,
        reply_to_message_id=71,
    )
    message.reply_text.assert_not_awaited()
    assert fake_context.job_queue.calls == []
    assert charges() == set()


@pytest.mark.parametrize("roll", [0.0, 0.049999999])
def test_rare_answer_occupies_exact_five_percent_threshold(monkeypatch, roll):
    from handlers import elite_ball

    random_roll = Mock(return_value=roll)
    choice = Mock(side_effect=AssertionError("rare answer selected by choice"))
    monkeypatch.setattr(elite_ball.random, "random", random_roll)
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    assert elite_ball.choose_ball_answer() == ANSWERS[3]
    random_roll.assert_called_once_with()
    choice.assert_not_called()


@pytest.mark.parametrize("answer", ANSWERS[:3])
def test_each_primary_answer_uses_an_equal_choice_pool(monkeypatch, answer):
    from handlers import elite_ball

    monkeypatch.setattr(elite_ball.random, "random", Mock(return_value=0.05))
    choice = Mock(return_value=answer)
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    assert elite_ball.choose_ball_answer() == answer
    choice.assert_called_once_with(list(ANSWERS[:3]))


@pytest.mark.asyncio
async def test_photo_send_failure_consumes_question_without_reroll(
    fake_context, monkeypatch, caplog,
):
    from handlers import elite_ball

    await elite_ball.elite_ball_callback(callback_update()[0], fake_context)
    choice = Mock(return_value="Да")
    random_roll = Mock(return_value=0.5)
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    monkeypatch.setattr(elite_ball.random, "random", random_roll)
    update, message = text_update()
    message.reply_photo.side_effect = RuntimeError("Telegram rejected photo")

    with pytest.raises(ApplicationHandlerStop):
        await elite_ball.elite_ball_question(update, fake_context)

    choice.assert_called_once_with(list(ANSWERS[:3]))
    random_roll.assert_called_once_with()
    message.reply_photo.assert_awaited_once_with(
        photo=elite_ball.ELITE_BALL_PHOTO_FILE_ID,
        caption="Да",
        reply_to_message_id=71,
    )
    message.reply_text.assert_not_awaited()
    assert charges() == set()
    assert "Could not send elite ball answer" in caplog.text
    await elite_ball.elite_ball_question(text_update()[0], fake_context)
    choice.assert_called_once()


@pytest.mark.asyncio
async def test_other_chat_and_other_users_have_independent_waiting(fake_context, monkeypatch):
    from handlers import elite_ball

    for user_id, chat_id in ((1, CHAT_ID), (1, -831), (2, CHAT_ID)):
        await elite_ball.elite_ball_callback(callback_update(user_id, chat_id)[0], fake_context)
    assert charges() == {
        (CHAT_ID, 1), (-831, 1), (CHAT_ID, 2),
    }
    monkeypatch.setattr(elite_ball.random, "choice", Mock(return_value="Нет"))
    monkeypatch.setattr(elite_ball.random, "random", Mock(return_value=0.5))
    with pytest.raises(ApplicationHandlerStop):
        await elite_ball.elite_ball_question(text_update(1, CHAT_ID)[0], fake_context)
    assert charges() == {(-831, 1), (CHAT_ID, 2)}


@pytest.mark.asyncio
@pytest.mark.parametrize("via_bot_id", (100, 200))
async def test_via_bot_message_from_any_inline_bot_does_not_consume_chat_charge(
    fake_context, monkeypatch, via_bot_id,
):
    from handlers import elite_ball

    await elite_ball.ball_command(SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_user=SimpleNamespace(id=1, is_bot=False),
        message=None,
    ), fake_context)
    choice = Mock(return_value="Да")
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    monkeypatch.setattr(elite_ball.random, "random", Mock(return_value=0.5))
    inline_message = Update.de_json({
        "update_id": 10,
        "message": {
            "message_id": 72, "date": 1,
            "chat": {"id": CHAT_ID, "type": "supergroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "Player"},
            "via_bot": {"id": via_bot_id, "is_bot": True, "first_name": "Inline Bot"},
            "text": "Элитный мячик знание\nВопрос: хуй будешь?",
        },
    }, None)
    assert inline_message.message.via_bot.id == via_bot_id

    await elite_ball.elite_ball_question(inline_message, fake_context)

    assert charges() == {(CHAT_ID, 1)}
    choice.assert_not_called()
    ordinary, message = text_update()
    with pytest.raises(ApplicationHandlerStop):
        await elite_ball.elite_ball_question(ordinary, fake_context)
    assert charges() == set()
    choice.assert_called_once_with(list(ANSWERS[:3]))
    message.reply_photo.assert_awaited_once()


def test_chat_charge_survives_storage_reinitialization():
    from database import init_db
    from elite_ball_store import activate_ball, consume_chat_ball

    activate_ball(CHAT_ID, 1)
    init_db()
    assert consume_chat_ball(CHAT_ID, 1)
    assert charges() == set()


@pytest.mark.asyncio
async def test_commands_nontext_bot_message_and_repeated_click_do_not_consume(
    fake_context, monkeypatch,
):
    from handlers import elite_ball

    update, _ = callback_update()
    await elite_ball.elite_ball_callback(update, fake_context)
    await elite_ball.elite_ball_callback(update, fake_context)
    assert charges() == {(CHAT_ID, 1)}
    choice = Mock(side_effect=AssertionError("ineligible message used RNG"))
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    for text, is_bot in (("/summary", False), (" /name Гном", False), (None, False), ("текст", True)):
        question, message = text_update(text=text, is_bot=is_bot)
        await elite_ball.elite_ball_question(question, fake_context)
        message.reply_photo.assert_not_awaited()
    assert charges() == {(CHAT_ID, 1)}
    choice.assert_not_called()

    answer_choice = Mock(return_value="Возможно")
    monkeypatch.setattr(elite_ball.random, "choice", answer_choice)
    monkeypatch.setattr(elite_ball.random, "random", Mock(return_value=0.5))
    question, message = text_update()
    with pytest.raises(ApplicationHandlerStop):
        await elite_ball.elite_ball_question(question, fake_context)
    message.reply_photo.assert_awaited_once_with(
        photo=elite_ball.ELITE_BALL_PHOTO_FILE_ID,
        caption="Возможно",
        reply_to_message_id=71,
    )
    assert charges() == set()
    await elite_ball.elite_ball_question(text_update()[0], fake_context)
    answer_choice.assert_called_once()


@pytest.mark.asyncio
async def test_unrelated_text_does_not_stop_existing_triggers(fake_context):
    from handlers import elite_ball

    trigger = AsyncMock()
    update, message = text_update(user_id=7)
    await elite_ball.elite_ball_question(update, fake_context)
    await trigger(update, fake_context)
    trigger.assert_awaited_once_with(update, fake_context)
    message.reply_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_and_question_listener_are_registered_once_in_safe_groups(monkeypatch):
    import bot

    handlers = []

    class FakeApplication:
        job_queue = None

        def add_handler(self, handler, group=0):
            handlers.append((group, handler))

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

    monkeypatch.setattr(bot, "run_ptb_and_http", AsyncMock())
    monkeypatch.setattr(bot, "init_db", lambda: None)
    monkeypatch.setattr(bot.Application, "builder", lambda: FakeBuilder())
    await bot.main()

    callbacks = [
        (group, handler) for group, handler in handlers
        if isinstance(handler, CallbackQueryHandler)
        and handler.callback is bot.elite_ball_callback
    ]
    listeners = [
        (group, handler) for group, handler in handlers
        if isinstance(handler, MessageHandler)
        and handler.callback is bot.elite_ball_question
    ]
    triggers = [group for group, handler in handlers if isinstance(handler, MessageHandler)
                and handler.callback is bot.respond_trigger]
    assert len(callbacks) == len(listeners) == 1
    assert callbacks[0][1].pattern.pattern == r"^elite_ball_ask$"
    assert listeners[0][0] == -1
    assert triggers == [0]
