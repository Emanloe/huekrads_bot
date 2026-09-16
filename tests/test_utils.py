from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
