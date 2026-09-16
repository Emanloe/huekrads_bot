"""Small cached loader for UTF-8 YAML user-text resources."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any

import yaml


_TEXTS_DIR = Path(__file__).resolve().parent / "texts"


@lru_cache(maxsize=1)
def _load_resources() -> dict[str, dict[str, Any]]:
    """Load all text resources once, keyed by their YAML filename stem."""
    resources: dict[str, dict[str, Any]] = {}
    for path in sorted(_TEXTS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"Text resource {path.name} must contain a YAML mapping.")
        resources[path.stem] = data
    return resources


def _get_resource_value(key: str) -> Any:
    parts = key.split(".")
    if len(parts) < 2 or not all(parts):
        raise KeyError(f"Invalid text key: {key!r}")

    try:
        value: Any = _load_resources()[parts[0]]
        for part in parts[1:]:
            value = value[part]
    except KeyError as exc:
        raise KeyError(f"Unknown text key: {key}") from exc
    return value


def get_text(key: str, /, **placeholders: object) -> str:
    """Return a text value by a dotted key and apply named placeholders."""
    value = _get_resource_value(key)

    if not isinstance(value, str):
        raise TypeError(f"Text key {key!r} does not resolve to a string.")

    for _, field_name, _, _ in Formatter().parse(value):
        if field_name is not None and not field_name.isidentifier():
            raise ValueError(f"Text key {key!r} has a non-named placeholder: {field_name!r}")

    try:
        return value.format(**placeholders)
    except KeyError as exc:
        raise KeyError(f"Missing placeholder {exc.args[0]!r} for text key: {key}") from exc


def get_text_list(key: str) -> list[str]:
    """Return a cached ordered list of user-facing text strings."""
    value = _get_resource_value(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"Text key {key!r} does not resolve to a list of strings.")
    return value


def get_text_mapping(key: str) -> dict[Any, str]:
    """Return a cached mapping whose values are user-facing text strings."""
    value = _get_resource_value(key)
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        raise TypeError(f"Text key {key!r} does not resolve to a mapping of strings.")
    return value
