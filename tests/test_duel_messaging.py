import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest


def test_duel_reexports_messaging_contract():
    from handlers import duel, duel_messaging

    assert duel.AUTO_DELETE_DELAY is duel_messaging.AUTO_DELETE_DELAY
    assert duel.delete_messages_job is duel_messaging.delete_messages_job
    assert duel.schedule_auto_delete is duel_messaging.schedule_auto_delete
    assert duel.send_and_schedule is duel_messaging.send_and_schedule


def test_schedule_auto_delete_uses_current_delay_and_skips_missing_queue(
    fake_context,
):
    from handlers import duel

    message_ids = [101, 102]
    duel.schedule_auto_delete(fake_context, -55, message_ids)

    assert duel.AUTO_DELETE_DELAY == 60
    assert fake_context.job_queue.calls == [
        (
            duel.delete_messages_job,
            60,
            {"data": {"chat_id": -55, "message_ids": message_ids}},
        )
    ]

    fake_context.job_queue = None
    duel.schedule_auto_delete(fake_context, -55, [103])


def test_cleanup_job_copies_message_ids_instead_of_sharing_mutable_list(fake_context):
    from handlers.duel_messaging import schedule_auto_delete

    message_ids = [1]
    schedule_auto_delete(fake_context, -61, message_ids)
    message_ids.append(2)
    assert fake_context.job_queue.calls[0][2]["data"] == {
        "chat_id": -61, "message_ids": [1],
    }


async def test_delete_messages_job_continues_after_delete_error(caplog):
    from handlers import duel

    caplog.set_level(logging.INFO)

    deleted_ids = []

    async def delete_message(*, chat_id, message_id):
        deleted_ids.append((chat_id, message_id))
        if message_id == 2:
            raise RuntimeError("Telegram delete failed")

    context = SimpleNamespace(
        job=SimpleNamespace(data={"chat_id": -56, "message_ids": [1, 2, 3]}),
        bot=SimpleNamespace(delete_message=AsyncMock(side_effect=delete_message)),
    )

    await duel.delete_messages_job(context)

    assert deleted_ids == [(-56, 1), (-56, 2), (-56, 3)]
    assert "TEMP_MESSAGE_DELETE_OK chat_id=-56" in caplog.text
    assert "TEMP_MESSAGE_DELETE_FAILED chat_id=-56" in caplog.text


async def test_delete_messages_job_logs_success_without_failure(caplog):
    from handlers.duel_messaging import delete_messages_job

    caplog.set_level(logging.INFO)
    context = SimpleNamespace(
        job=SimpleNamespace(data={"chat_id": -57, "message_ids": [10]}),
        bot=SimpleNamespace(delete_message=AsyncMock(return_value=True)),
    )

    await delete_messages_job(context)

    context.bot.delete_message.assert_awaited_once_with(chat_id=-57, message_id=10)
    assert "TEMP_MESSAGE_DELETE_OK chat_id=-57" in caplog.text
    assert "TEMP_MESSAGE_DELETE_FAILED chat_id=-57" not in caplog.text


async def test_delete_messages_job_treats_missing_message_as_idempotent(caplog):
    from handlers.duel_messaging import delete_messages_job

    caplog.set_level(logging.INFO)
    context = SimpleNamespace(
        job=SimpleNamespace(data={"chat_id": -58, "message_ids": [11]}),
        bot=SimpleNamespace(
            delete_message=AsyncMock(
                side_effect=BadRequest("Message to delete not found"),
            ),
        ),
    )

    await delete_messages_job(context)

    assert "TEMP_MESSAGE_DELETE_ALREADY_ABSENT chat_id=-58" in caplog.text
    assert "TEMP_MESSAGE_DELETE_FAILED chat_id=-58" not in caplog.text
    record = next(
        item for item in caplog.records
        if "TEMP_MESSAGE_DELETE_ALREADY_ABSENT" in item.getMessage()
    )
    assert record.levelno == logging.INFO
    assert record.exc_info is None


async def test_delete_messages_job_does_not_suppress_other_bad_requests(caplog):
    from handlers.duel_messaging import delete_messages_job

    caplog.set_level(logging.INFO)
    context = SimpleNamespace(
        job=SimpleNamespace(data={"chat_id": -59, "message_ids": [12, 13]}),
        bot=SimpleNamespace(
            delete_message=AsyncMock(
                side_effect=[BadRequest("Message can't be deleted"), True],
            ),
        ),
    )

    await delete_messages_job(context)

    assert context.bot.delete_message.await_count == 2
    assert "TEMP_MESSAGE_DELETE_FAILED chat_id=-59" in caplog.text
    assert "TEMP_MESSAGE_DELETE_OK chat_id=-59" in caplog.text
    failure = next(
        item for item in caplog.records
        if "TEMP_MESSAGE_DELETE_FAILED" in item.getMessage()
    )
    assert failure.levelno == logging.ERROR
    assert failure.exc_info is not None


async def test_battle_delete_jobs_keep_identity_and_log_failures(fake_context, caplog):
    from handlers.duel_messaging import delete_messages_job, schedule_auto_delete

    caplog.set_level(logging.INFO)

    schedule_auto_delete(fake_context, -90, [11, 12], battle_id="battle-a", round_num=2)
    callback, delay, kwargs = fake_context.job_queue.calls[0]
    assert callback is delete_messages_job
    assert delay == 60
    assert kwargs["data"] == {
        "chat_id": -90, "message_ids": [11, 12],
        "battle_id": "battle-a", "round": 2,
    }
    assert "BATTLE_MESSAGE_DELETE_SCHEDULED chat_id=-90 battle_id=battle-a round=2 message_id=11" in caplog.text
    fake_context.bot.delete_message.side_effect = [RuntimeError("gone"), True]
    fake_context.job.data = kwargs["data"]
    await callback(fake_context)
    assert fake_context.bot.delete_message.await_count == 2
    assert "BATTLE_MESSAGE_DELETE_FAILED chat_id=-90 battle_id=battle-a round=2 message_id=11" in caplog.text
    assert "BATTLE_MESSAGE_DELETE_OK chat_id=-90 battle_id=battle-a round=2 message_id=12" in caplog.text


async def test_send_and_schedule_replies_and_schedules_both_messages(fake_context):
    from handlers import duel

    reply_markup = object()
    message = SimpleNamespace(
        message_id=70,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=71)),
    )
    update = SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=-57),
    )

    await duel.send_and_schedule(
        update,
        fake_context,
        "response text",
        reply_markup=reply_markup,
        parse_mode="MarkdownV2",
    )

    message.reply_text.assert_awaited_once_with(
        "response text",
        parse_mode="MarkdownV2",
        reply_markup=reply_markup,
    )
    assert fake_context.job_queue.calls == [
        (
            duel.delete_messages_job,
            duel.AUTO_DELETE_DELAY,
            {"data": {"chat_id": -57, "message_ids": [71, 70]}},
        )
    ]


async def test_send_and_schedule_falls_back_to_bot_send_message(fake_context):
    from handlers import duel

    reply_markup = object()
    message = SimpleNamespace(
        message_id=80,
        reply_text=AsyncMock(side_effect=RuntimeError("reply failed")),
    )
    update = SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=-58),
    )

    await duel.send_and_schedule(
        update,
        fake_context,
        "fallback text",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )

    fake_context.bot.send_message.assert_awaited_once_with(
        chat_id=-58,
        text="fallback text",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )
    assert fake_context.job_queue.calls == [
        (
            duel.delete_messages_job,
            duel.AUTO_DELETE_DELAY,
            {"data": {"chat_id": -58, "message_ids": [101, 80]}},
        )
    ]
