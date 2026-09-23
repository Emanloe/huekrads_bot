"""Inline choice routing and the chat-scoped Elite Ball activation path."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Update
from telegram.ext import CommandHandler, InlineQueryHandler


CHAT_ID = -9902


def _inline_update(query):
    return SimpleNamespace(inline_query=SimpleNamespace(query=query, answer=AsyncMock()))


def _telegram_user(user_id):
    return {"id": user_id, "is_bot": False, "first_name": f"user{user_id}"}


def _real_command_update(user_id=1, chat_id=CHAT_ID):
    return Update.de_json({
        "update_id": 10,
        "message": {
            "message_id": 25, "date": 1,
            "chat": {"id": chat_id, "type": "supergroup"},
            "from": _telegram_user(user_id), "text": "/ball",
            "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
        },
    }, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("query,forecast", [
    ("Сделаю?", None),
    ("Москва", ("<b>Прогноз</b>", "Москва")),
])
async def test_one_inline_answer_contains_weather_and_elite_ball(
    query, forecast, fake_context, monkeypatch,
):
    from handlers import weather
    from handlers.inline_query import inline_query_dispatch
    from handlers.elite_ball import ELITE_BALL_INLINE_RESULT_ID

    monkeypatch.setattr(weather, "_fetch_weather_html", Mock(return_value=forecast))
    monkeypatch.setattr(weather, "_bonus_gif", Mock(return_value=None))
    monkeypatch.setattr(weather, "_bonus_photo", Mock(return_value=None))
    update = _inline_update(query)

    await inline_query_dispatch(update, fake_context)

    update.inline_query.answer.assert_awaited_once()
    results = update.inline_query.answer.await_args.args[0]
    assert len(results) == 2
    weather_result, ball_result = results
    assert weather_result.id != ball_result.id
    assert ball_result.id == ELITE_BALL_INLINE_RESULT_ID
    assert ball_result.title == "Элитный мячик знание"
    assert ball_result.reply_markup is None
    assert "/ball" in ball_result.input_message_content.message_text
    if forecast is None:
        assert weather_result.title == "Погода: Сделаю?"
        assert "не найден" in weather_result.input_message_content.message_text
    else:
        assert weather_result.title == "Погода в Москва"
        assert weather_result.input_message_content.message_text == forecast[0]


@pytest.mark.asyncio
async def test_weather_failure_cannot_hide_ball_and_empty_query_stays_empty(
    fake_context, monkeypatch,
):
    from handlers import weather
    from handlers.inline_query import inline_query_dispatch

    monkeypatch.setattr(weather, "_fetch_weather_html", Mock(side_effect=RuntimeError("API down")))
    broken = _inline_update("не город")
    await inline_query_dispatch(broken, fake_context)
    results = broken.inline_query.answer.await_args.args[0]
    assert len(results) == 2
    assert results[1].title == "Элитный мячик знание"

    empty = _inline_update(" ")
    await inline_query_dispatch(empty, fake_context)
    empty.inline_query.answer.assert_awaited_once_with([], cache_time=1, is_personal=True)


@pytest.mark.asyncio
async def test_chosen_weather_result_keeps_weather_attachment_path_not_ball(fake_context, monkeypatch):
    from handlers import elite_ball, weather

    weather._store_attach(fake_context, "weather-result", "photo", "photo-id", "forecast")
    replace = AsyncMock()
    monkeypatch.setattr(weather, "_replace_now_or_retry", replace)
    chosen_weather = SimpleNamespace(chosen_inline_result=SimpleNamespace(
        result_id="weather-result", inline_message_id="inline-message",
    ))
    await weather.weather_chosen_inline_result(chosen_weather, fake_context)
    replace.assert_awaited_once_with(
        fake_context, "inline-message", ("photo", "photo-id", "forecast"),
        "weather-result",
    )

    replace.reset_mock()
    chosen_ball = SimpleNamespace(chosen_inline_result=SimpleNamespace(
        result_id=elite_ball.ELITE_BALL_INLINE_RESULT_ID, inline_message_id=None,
    ))
    await weather.weather_chosen_inline_result(chosen_ball, fake_context)
    replace.assert_not_awaited()
    assert fake_context.bot_data.get("elite_ball_waiting") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    "Да", "Нет", "Возможно", "Увлажните шар гнома усерднее",
])
async def test_real_telegram_inline_updates_have_no_chat_and_command_activates_in_chat(
    fake_context, monkeypatch, answer,
):
    from handlers import elite_ball, weather
    from telegram.ext import ApplicationHandlerStop

    inline = Update.de_json({
        "update_id": 1,
        "inline_query": {
            "id": "q1", "from": _telegram_user(1),
            "query": "Сделаю?", "offset": "",
        },
    }, None)
    chosen = Update.de_json({
        "update_id": 2,
        "chosen_inline_result": {
            "result_id": elite_ball.ELITE_BALL_INLINE_RESULT_ID,
            "from": _telegram_user(1), "query": "Сделаю?",
        },
    }, None)
    assert inline.effective_chat is None
    assert chosen.effective_chat is None
    await weather.weather_chosen_inline_result(chosen, fake_context)
    await elite_ball.ball_command(chosen, fake_context)
    assert fake_context.bot_data.get("elite_ball_waiting") is None

    command = _real_command_update()
    assert command.effective_chat.id == CHAT_ID
    await elite_ball.ball_command(command, fake_context)
    await elite_ball.ball_command(command, fake_context)
    assert fake_context.bot_data["elite_ball_waiting"] == {(CHAT_ID, 1)}
    assert fake_context.bot.send_message.await_args.kwargs == {
        "chat_id": CHAT_ID, "text": "Шар ожидает вопрос:",
    }

    choice = Mock(return_value=answer)
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    monkeypatch.setattr(elite_ball.random, "random", Mock(
        side_effect=AssertionError("question used random.random"),
    ))

    def question_update(user_id, chat_id, text="Вопрос"):
        message = SimpleNamespace(
            message_id=30, from_user=SimpleNamespace(id=user_id, is_bot=False),
            text=text, reply_text=AsyncMock(),
        )
        return SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=chat_id))

    for not_mine in (
        question_update(1, CHAT_ID - 1), question_update(2, CHAT_ID),
        question_update(1, CHAT_ID, "/summary"), question_update(1, CHAT_ID, None),
    ):
        await elite_ball.elite_ball_question(not_mine, fake_context)
    assert fake_context.bot_data["elite_ball_waiting"] == {(CHAT_ID, 1)}
    choice.assert_not_called()

    mine = question_update(1, CHAT_ID)
    with pytest.raises(ApplicationHandlerStop):
        await elite_ball.elite_ball_question(mine, fake_context)
    choice.assert_called_once_with([
        "Да", "Нет", "Возможно", "Увлажните шар гнома усерднее",
    ])
    mine.message.reply_text.assert_awaited_once_with(
        answer, reply_to_message_id=30,
    )
    assert fake_context.bot_data["elite_ball_waiting"] == set()


@pytest.mark.asyncio
async def test_one_inline_handler_and_one_ball_command_are_wired(monkeypatch):
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

    monkeypatch.setattr(bot.nest_asyncio, "apply", lambda: None)
    monkeypatch.setattr(bot, "init_db", lambda: None)
    monkeypatch.setattr(bot.Application, "builder", lambda: FakeBuilder())
    await bot.main()

    inline_handlers = [handler for _, handler in handlers if isinstance(handler, InlineQueryHandler)]
    assert len(inline_handlers) == 1
    assert inline_handlers[0].callback is bot.inline_query_dispatch
    commands = [handler for _, handler in handlers if isinstance(handler, CommandHandler)
                and handler.commands == frozenset({"ball"})]
    assert len(commands) == 1
    assert commands[0].callback is bot.ball_command
