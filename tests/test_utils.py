import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from text_resources import get_text, get_text_mapping


def test_media_text_resources_preserve_labels_and_html_placeholder():
    assert get_text_mapping("media.types") == {
        "file": "File",
        "photo": "Photo",
        "gif": "GIF",
        "video": "Video",
        "document": "Document",
        "sticker": "Sticker",
    }
    assert get_text(
        "media.templates.file_id",
        media_type="Photo",
        file_id="photo-id",
    ) == "Photo file_id:\n<code>photo-id</code>"


@pytest.mark.asyncio
async def test_file_id_handler_uses_media_template(monkeypatch):
    from handlers import utils

    reply_text = AsyncMock()
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        photo=[SimpleNamespace(file_id="photo-id")],
        animation=None,
        video=None,
        document=None,
        sticker=None,
        reply_text=reply_text,
    )
    monkeypatch.setattr(utils, "is_admin", lambda _user_id: True)

    await utils.get_file_id_handler(SimpleNamespace(message=message), None)

    reply_text.assert_awaited_once_with(
        "Photo file_id:\n<code>photo-id</code>",
        parse_mode="HTML",
    )


@pytest.mark.asyncio
async def test_command_delete_job_treats_missing_message_as_idempotent(
    monkeypatch, caplog,
):
    from handlers import utils

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(utils, "is_auto_delete_enabled", lambda _chat_id: True)
    context = SimpleNamespace(
        job=SimpleNamespace(data={"chat_id": -60, "message_id": 20}),
        bot=SimpleNamespace(
            set_message_reaction=AsyncMock(),
            delete_message=AsyncMock(
                side_effect=BadRequest("Message to delete not found"),
            ),
        ),
    )

    await utils.delete_messages_job(context)

    assert "COMMAND_MESSAGE_DELETE_ALREADY_ABSENT chat_id=-60 message_id=20" in caplog.text
    assert "COMMAND_MESSAGE_DELETE_FAILED" not in caplog.text
    record = next(
        item for item in caplog.records
        if "COMMAND_MESSAGE_DELETE_ALREADY_ABSENT" in item.getMessage()
    )
    assert record.levelno == logging.INFO
    assert record.exc_info is None


@pytest.mark.asyncio
async def test_command_delete_job_logs_unexpected_delete_error(
    monkeypatch, caplog,
):
    from handlers import utils

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(utils, "is_auto_delete_enabled", lambda _chat_id: True)
    context = SimpleNamespace(
        job=SimpleNamespace(data={"chat_id": -61, "message_id": 21}),
        bot=SimpleNamespace(
            set_message_reaction=AsyncMock(),
            delete_message=AsyncMock(side_effect=RuntimeError("network failed")),
        ),
    )

    await utils.delete_messages_job(context)

    assert "COMMAND_MESSAGE_DELETE_FAILED chat_id=-61 message_id=21" in caplog.text
    failure = next(
        item for item in caplog.records
        if "COMMAND_MESSAGE_DELETE_FAILED" in item.getMessage()
    )
    assert failure.levelno == logging.ERROR
    assert failure.exc_info is not None
