import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from text_resources import get_text


CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "duel_post_messages.json"


def test_duel_post_message_catalog_is_raw_unique_utf8_json():
    from handlers import duel

    raw = CATALOG_PATH.read_text(encoding="utf-8")
    messages = json.loads(raw)

    assert isinstance(messages, list)
    assert len(messages) == 98
    assert messages
    assert all(isinstance(message, str) for message in messages)
    assert len(messages) == len(set(messages))
    assert "\\u" not in raw
    assert all("Dotagosubot" not in message for message in messages)
    assert all("все оскорбления:" not in message for message in messages)
    assert all(
        re.match(r"^\[\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}\]", message) is None
        for message in messages
    )
    assert duel.DUEL_POST_MESSAGES == tuple(messages)


def test_duel_post_message_loader_rejects_empty_and_invalid_catalogs(tmp_path):
    from handlers import duel

    empty = tmp_path / "empty.json"
    invalid = tmp_path / "invalid.json"
    non_strings = tmp_path / "non_strings.json"
    empty.write_text("[]", encoding="utf-8")
    invalid.write_text("not json", encoding="utf-8")
    non_strings.write_text('["valid", 1]', encoding="utf-8")

    assert duel._load_duel_post_messages(empty) == ()
    assert duel._load_duel_post_messages(invalid) == ()
    assert duel._load_duel_post_messages(non_strings) == ()


def gnomed_update(reply_to_message_id):
    target = (
        SimpleNamespace(message_id=reply_to_message_id)
        if reply_to_message_id is not None
        else None
    )
    return SimpleNamespace(
        message=SimpleNamespace(message_id=101, reply_to_message=target),
        effective_chat=SimpleNamespace(id=-700),
    )


@pytest.mark.asyncio
async def test_gnomed_sends_one_catalog_phrase_as_reply_to_original_message(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    phrase = duel.DUEL_POST_MESSAGES[0]
    choice = Mock(return_value=phrase)
    random_roll = Mock(side_effect=AssertionError("/gnomed consumed random.random"))
    monkeypatch.setattr(duel.random, "choice", choice)
    monkeypatch.setattr(duel.random, "random", random_roll)
    fake_context.bot.send_message = AsyncMock()

    await duel.gnomed_command(gnomed_update(100), fake_context)

    choice.assert_called_once_with(duel.DUEL_POST_MESSAGES)
    random_roll.assert_not_called()
    fake_context.bot.send_message.assert_awaited_once_with(
        chat_id=-700,
        text=phrase,
        reply_to_message_id=100,
    )
    sent = fake_context.bot.send_message.await_args.kwargs
    assert sent["reply_to_message_id"] != 101
    assert get_text("duel.finish.post_message.prefix") == (
        "На теле проигравшего обнаружили записку:"
    )
    assert get_text("duel.finish.post_message.prefix") not in sent["text"]


@pytest.mark.asyncio
async def test_gnomed_without_reply_uses_resource_hint_without_rng(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    choice = Mock(side_effect=AssertionError("no-reply /gnomed consumed choice"))
    monkeypatch.setattr(duel.random, "choice", choice)
    fake_context.bot.send_message = AsyncMock()

    await duel.gnomed_command(gnomed_update(None), fake_context)

    choice.assert_not_called()
    fake_context.bot.send_message.assert_awaited_once_with(
        chat_id=-700,
        text=get_text("duel.gnomed.reply_required"),
    )


@pytest.mark.asyncio
async def test_gnomed_empty_catalog_uses_resource_fallback_without_rng(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    choice = Mock(side_effect=AssertionError("empty /gnomed catalog consumed choice"))
    monkeypatch.setattr(duel, "DUEL_POST_MESSAGES", ())
    monkeypatch.setattr(duel.random, "choice", choice)
    fake_context.bot.send_message = AsyncMock()

    await duel.gnomed_command(gnomed_update(100), fake_context)

    choice.assert_not_called()
    fake_context.bot.send_message.assert_awaited_once_with(
        chat_id=-700,
        text=get_text("duel.gnomed.catalog_unavailable"),
    )


@pytest.mark.asyncio
async def test_gnomed_html_sensitive_phrase_is_sent_as_plain_text(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    catalog = ("<гном> & хуй",)
    choice = Mock(return_value=catalog[0])
    monkeypatch.setattr(duel, "DUEL_POST_MESSAGES", catalog)
    monkeypatch.setattr(duel.random, "choice", choice)
    fake_context.bot.send_message = AsyncMock()

    await duel.gnomed_command(gnomed_update(100), fake_context)

    choice.assert_called_once_with(catalog)
    fake_context.bot.send_message.assert_awaited_once_with(
        chat_id=-700,
        text="<гном> & хуй",
        reply_to_message_id=100,
    )
    assert "parse_mode" not in fake_context.bot.send_message.await_args.kwargs
