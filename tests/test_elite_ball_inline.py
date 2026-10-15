"""Independent inline Elite Ball ownership, persistence, and one-shot use."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import InlineQueryResultArticle, InputMediaPhoto, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler

from database import get_db, init_db
from elite_ball_store import (
    activate_ball, consume_inline_action, create_inline_action,
)
from text_resources import get_text


pytestmark = pytest.mark.usefixtures("temp_database")


@pytest.fixture(autouse=True)
def use_primary_answer_branch(monkeypatch):
    from handlers import elite_ball

    monkeypatch.setattr(elite_ball.random, "random", lambda: 0.5)


def activations():
    with get_db() as conn:
        return list(conn.execute(
            "SELECT chat_id, user_id FROM elite_ball_activations ORDER BY id"
        ))


def callback(token, user_id=1):
    query = SimpleNamespace(
        data=f"ebi:{token}",
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        inline_message_id="inline-message-id",
        edit_message_caption=AsyncMock(),
        edit_message_media=AsyncMock(),
        edit_message_text=AsyncMock(),
        answer=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query, effective_chat=None), query


@pytest.mark.asyncio
async def test_inline_preview_and_callback_work_without_ball_charge(fake_context, monkeypatch):
    from handlers import elite_ball
    from handlers.inline_query import inline_query_dispatch

    choice = Mock(side_effect=AssertionError("preview used ball RNG"))
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    question = "Пить <чай> & спать?"
    update = SimpleNamespace(inline_query=SimpleNamespace(
        query=question, from_user=SimpleNamespace(id=1, is_bot=False),
        answer=AsyncMock(),
    ), effective_chat=None)

    await inline_query_dispatch(update, fake_context)

    weather, ball = update.inline_query.answer.await_args.args[0]
    assert weather.id != ball.id
    assert isinstance(ball, InlineQueryResultArticle)
    assert ball.title == get_text("elite_ball.button")
    assert ball.description == get_text("elite_ball.inline_description", question=question)
    assert ball.thumbnail_url is None  # The earlier Article result had no thumbnail.
    assert "&lt;чай&gt; &amp;" in ball.input_message_content.message_text
    assert ball.input_message_content.parse_mode == "HTML"
    button = ball.reply_markup.inline_keyboard[0][0]
    assert button.text == get_text("elite_ball.inline_button")
    assert len(button.callback_data.encode()) <= 64
    assert question not in button.callback_data
    assert activations() == []
    choice.assert_not_called()

    answer_choice = Mock(return_value="Да")
    monkeypatch.setattr(elite_ball.random, "choice", answer_choice)
    token = button.callback_data.removeprefix("ebi:")
    owner_update, owner_query = callback(token)
    await elite_ball.elite_ball_inline_callback(owner_update, fake_context)
    answer_choice.assert_called_once_with(["Да", "Нет", "Возможно"])
    owner_query.edit_message_media.assert_awaited_once()
    final_media = owner_query.edit_message_media.await_args.kwargs["media"]
    assert isinstance(final_media, InputMediaPhoto)
    assert final_media.media == elite_ball.ELITE_BALL_PHOTO_FILE_ID
    assert "Да" in final_media.caption
    assert "&lt;чай&gt;" in final_media.caption
    assert final_media.parse_mode == "HTML"
    assert owner_query.edit_message_media.await_args.kwargs["reply_markup"] is None
    owner_query.edit_message_caption.assert_not_awaited()
    owner_query.edit_message_text.assert_not_awaited()
    assert activations() == []


@pytest.mark.asyncio
async def test_owner_uses_inline_action_without_charge_or_destination_chat(fake_context, monkeypatch):
    from handlers import elite_ball

    token = create_inline_action(1, "Мой вопрос?")
    choice = Mock(return_value="Да")
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    update, query = callback(token)

    await elite_ball.elite_ball_inline_callback(update, fake_context)

    assert activations() == []
    choice.assert_called_once_with(["Да", "Нет", "Возможно"])
    kwargs = query.edit_message_media.await_args.kwargs
    assert isinstance(kwargs["media"], InputMediaPhoto)
    assert kwargs["media"].media == elite_ball.ELITE_BALL_PHOTO_FILE_ID
    assert "Мой вопрос?" in kwargs["media"].caption and "Да" in kwargs["media"].caption
    assert kwargs["reply_markup"] is None
    query.edit_message_caption.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()
    assert update.effective_chat is None


@pytest.mark.asyncio
async def test_other_user_cannot_use_card_or_rng(fake_context, monkeypatch):
    from handlers import elite_ball

    activate_ball(-100, 1)
    token = create_inline_action(1, "Вопрос")
    choice = Mock(side_effect=AssertionError("foreign callback used RNG"))
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    update, query = callback(token, user_id=2)

    await elite_ball.elite_ball_inline_callback(update, fake_context)

    assert activations() == [(-100, 1)]
    query.edit_message_media.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()
    query.answer.assert_awaited_once_with(
        get_text("elite_ball.inline_not_owner"), show_alert=True,
    )
    choice.assert_not_called()

    owner_update, owner_query = callback(token)
    monkeypatch.setattr(elite_ball.random, "choice", Mock(return_value="Да"))
    await elite_ball.elite_ball_inline_callback(owner_update, fake_context)
    owner_query.edit_message_media.assert_awaited_once()
    assert activations() == [(-100, 1)]


@pytest.mark.asyncio
async def test_inline_callback_never_reads_or_consumes_existing_chat_charge(fake_context, monkeypatch):
    from handlers import elite_ball

    activate_ball(-100, 1)
    token = create_inline_action(1, "Вопрос")
    choice = Mock(return_value="Нет")
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    update, query = callback(token)
    await elite_ball.elite_ball_inline_callback(update, fake_context)
    query.edit_message_media.assert_awaited_once()
    assert activations() == [(-100, 1)]
    choice.assert_called_once()


@pytest.mark.asyncio
async def test_chat_and_inline_modes_remain_independent_through_sent_preview(fake_context, monkeypatch):
    from handlers import elite_ball, weather
    from handlers.inline_query import inline_query_dispatch

    await elite_ball.ball_command(SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100),
        effective_user=SimpleNamespace(id=1, is_bot=False), message=None,
    ), fake_context)
    rolls = Mock(side_effect=["Да", "Нет"])
    monkeypatch.setattr(elite_ball.random, "choice", rolls)
    monkeypatch.setattr(weather, "_fetch_weather_html", Mock(return_value=None))
    monkeypatch.setattr(weather, "_bonus_gif", Mock(return_value=None))
    monkeypatch.setattr(weather, "_bonus_photo", Mock(return_value=None))
    inline = SimpleNamespace(query="хуй будешь?", from_user=SimpleNamespace(id=1, is_bot=False), answer=AsyncMock())
    await inline_query_dispatch(SimpleNamespace(inline_query=inline, effective_chat=None), fake_context)
    ball = inline.answer.await_args.args[0][1]
    token = ball.reply_markup.inline_keyboard[0][0].callback_data.removeprefix("ebi:")
    assert activations() == [(-100, 1)]
    rolls.assert_not_called()

    sent = Update.de_json({
        "update_id": 11,
        "message": {
            "message_id": 80, "date": 1,
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 1, "is_bot": False, "first_name": "Player"},
            "via_bot": {"id": 99, "is_bot": True, "first_name": "Inline Bot"},
            "text": "Элитный мячик знание\nВопрос: хуй будешь?",
        },
    }, None)
    await elite_ball.elite_ball_question(sent, fake_context)
    assert activations() == [(-100, 1)]
    rolls.assert_not_called()

    owner_update, query = callback(token)
    await elite_ball.elite_ball_inline_callback(owner_update, fake_context)
    assert "Да" in query.edit_message_media.await_args.kwargs["media"].caption
    assert query.edit_message_media.await_args.kwargs["reply_markup"] is None
    assert activations() == [(-100, 1)]
    assert rolls.call_count == 1

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False), via_bot=None,
        message_id=81, text="Обычный вопрос", reply_photo=AsyncMock(),
    )
    ordinary = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=-100))
    with pytest.raises(ApplicationHandlerStop):
        await elite_ball.elite_ball_question(ordinary, fake_context)
    assert activations() == []
    assert rolls.call_count == 2
    assert message.reply_photo.await_args.kwargs["caption"] == "Нет"


@pytest.mark.asyncio
async def test_repeat_callback_reuses_answer_without_second_charge_or_rng(fake_context, monkeypatch):
    from handlers import elite_ball

    activate_ball(-100, 1)
    activate_ball(-200, 1)
    token = create_inline_action(1, "Вопрос")
    choice = Mock(return_value="Да")
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    first, first_query = callback(token)
    second, second_query = callback(token)

    await elite_ball.elite_ball_inline_callback(first, fake_context)
    await elite_ball.elite_ball_inline_callback(second, fake_context)

    assert activations() == [(-100, 1), (-200, 1)]
    choice.assert_called_once()
    first_media = first_query.edit_message_media.await_args.kwargs["media"]
    second_media = second_query.edit_message_media.await_args.kwargs["media"]
    assert first_media.media == second_media.media == elite_ball.ELITE_BALL_PHOTO_FILE_ID
    assert first_media.caption == second_media.caption
    assert second_query.edit_message_media.await_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_retry_accepts_already_final_photo_without_reroll(fake_context, monkeypatch):
    from handlers import elite_ball

    token = create_inline_action(1, "Вопрос")
    choice = Mock(return_value="Да")
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    await elite_ball.elite_ball_inline_callback(callback(token)[0], fake_context)
    retry, query = callback(token)
    query.edit_message_media.side_effect = BadRequest("Message is not modified")

    await elite_ball.elite_ball_inline_callback(retry, fake_context)

    assert query.edit_message_media.await_args.kwargs["media"].caption.endswith("Ответ: Да")
    query.answer.assert_awaited_once_with()
    choice.assert_called_once()


@pytest.mark.asyncio
async def test_failed_edit_can_retry_saved_answer_after_restart(fake_context, monkeypatch):
    from handlers import elite_ball

    token = create_inline_action(1, "Вопрос")
    choice = Mock(return_value="Да")
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    update, query = callback(token)
    query.edit_message_media.side_effect = TelegramError("Telegram unavailable")

    await elite_ball.elite_ball_inline_callback(update, fake_context)
    assert activations() == []
    init_db()  # A new process can read the persisted action and answer.
    retry, retry_query = callback(token)
    await elite_ball.elite_ball_inline_callback(retry, fake_context)

    assert "Да" in retry_query.edit_message_media.await_args.kwargs["media"].caption
    choice.assert_called_once()


def test_concurrent_callbacks_consume_once_and_save_same_answer():
    token = create_inline_action(1, "Вопрос")
    gate = Barrier(2)
    guard = Lock()
    calls = []

    def choose():
        with guard:
            calls.append(1)
        return "Да"

    def run():
        gate.wait()
        return consume_inline_action(token, 1, choose)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        results = [future.result() for future in futures]

    assert {result.status for result in results} == {"used", "already_used"}
    assert all(result.answer == "Да" for result in results)
    assert len(calls) == 1
    assert activations() == []


def test_pending_token_is_digested_and_expiry_checked_without_rng():
    token = create_inline_action(1, "Вопрос")
    with get_db() as conn:
        digest, owner, question = conn.execute(
            "SELECT token_digest, owner_user_id, question FROM elite_ball_inline_actions"
        ).fetchone()
        assert digest == hashlib.sha256(token.encode("ascii")).hexdigest()
        assert token != digest and owner == 1 and question == "Вопрос"
        conn.execute(
            "UPDATE elite_ball_inline_actions SET created_at = 0, expires_at = 1"
        )
    choose = Mock(side_effect=AssertionError("expired action used RNG"))
    assert consume_inline_action(token, 1, choose).status == "unavailable"
    assert consume_inline_action("missing", 1, choose).status == "unavailable"
    choose.assert_not_called()


@pytest.mark.asyncio
async def test_expired_and_unknown_callback_are_safe_refusals(fake_context, monkeypatch):
    from handlers import elite_ball

    token = create_inline_action(1, "Вопрос")
    with get_db() as conn:
        conn.execute("UPDATE elite_ball_inline_actions SET created_at = 0, expires_at = 1")
    choice = Mock(side_effect=AssertionError("invalid action used RNG"))
    monkeypatch.setattr(elite_ball.random, "choice", choice)
    for action_token in (token, "missing"):
        update, query = callback(action_token)
        await elite_ball.elite_ball_inline_callback(update, fake_context)
        query.answer.assert_awaited_once_with(
            get_text("elite_ball.inline_unavailable"), show_alert=True,
        )
        query.edit_message_media.assert_not_awaited()
    choice.assert_not_called()


@pytest.mark.asyncio
async def test_empty_and_unowned_queries_keep_weather_and_ball_flow(fake_context, monkeypatch):
    from handlers import weather
    from handlers.inline_query import inline_query_dispatch

    monkeypatch.setattr(weather, "_fetch_weather_html", Mock(return_value=None))
    query = SimpleNamespace(query="Погода", from_user=SimpleNamespace(id=7, is_bot=False), answer=AsyncMock())
    await inline_query_dispatch(SimpleNamespace(inline_query=query), fake_context)
    assert len(query.answer.await_args.args[0]) == 2
    assert query.answer.await_args.args[0][0].title.startswith("Погода")
    assert query.answer.await_args.args[0][1].title == get_text("elite_ball.button")

    empty = SimpleNamespace(query=" ", from_user=query.from_user, answer=AsyncMock())
    await inline_query_dispatch(SimpleNamespace(inline_query=empty), fake_context)
    empty.answer.assert_awaited_once_with([], cache_time=1, is_personal=True)


def test_migration_is_additive_and_idempotent():
    activate_ball(-100, 1)
    with get_db() as conn:
        conn.execute("CREATE TABLE legacy_data (value TEXT)")
        conn.execute("INSERT INTO legacy_data VALUES ('keep')")
    init_db()
    init_db()
    with get_db() as conn:
        assert conn.execute("SELECT value FROM legacy_data").fetchone() == ("keep",)
    assert activations() == [(-100, 1)]


@pytest.mark.asyncio
async def test_inline_callback_handler_registered_once_and_narrow(monkeypatch):
    import bot

    handlers = []

    class FakeApplication:
        job_queue = None

        def add_handler(self, handler, group=0):
            handlers.append((group, handler))

        def add_error_handler(self, _handler):
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
    callbacks = [handler for _, handler in handlers
                 if isinstance(handler, CallbackQueryHandler)
                 and handler.callback is bot.elite_ball_inline_callback]
    assert len(callbacks) == 1
    assert callbacks[0].pattern.pattern == r"^ebi:[A-Za-z0-9_-]{24}$"
    assert not callbacks[0].pattern.match("elite_ball_ask")
    assert not callbacks[0].pattern.match("duel_strike_1")


def test_new_presentation_is_loaded_from_yaml():
    assert get_text("elite_ball.inline_button") == "Получить ответ"
    assert "{question}" not in get_text("elite_ball.inline_preview", question="Вопрос")
    assert "Да" in get_text("elite_ball.inline_final", question="Вопрос", answer="Да")
