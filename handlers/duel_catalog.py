"""Shared presentation catalogs for ordinary-duel terminal results."""

import json
import logging
from pathlib import Path


_DWARFS_FACTS_PATH = Path(__file__).resolve().parent.parent / "data" / "dwarfs_facts.json"
with open(_DWARFS_FACTS_PATH, encoding="utf-8") as _facts_file:
    DWARFS_FACTS = tuple(json.load(_facts_file)["facts"])

_DUEL_POST_MESSAGES_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "duel_post_messages.json"
)


def _load_duel_post_messages(path=_DUEL_POST_MESSAGES_PATH):
    try:
        with open(path, encoding="utf-8") as messages_file:
            messages = json.load(messages_file)
    except (OSError, json.JSONDecodeError):
        logging.exception("Не удалось загрузить каталог post-duel сообщений")
        return ()

    if not isinstance(messages, list) or not messages or not all(
        isinstance(message, str) for message in messages
    ):
        logging.error("Каталог post-duel сообщений пуст или некорректен")
        return ()
    return tuple(messages)


DUEL_POST_MESSAGES = _load_duel_post_messages()
