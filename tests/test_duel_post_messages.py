import json
import re
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "duel_post_messages.json"


def test_duel_post_message_catalog_is_raw_unique_utf8_json():
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
