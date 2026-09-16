from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_yes_no_and_export_helpers():
    from handlers import past_pizda as p

    assert p.match_yes_no("да") == "да"
    assert p.match_yes_no("нет") == "нет"
    assert p.match_yes_no("давг") is None
    assert p.flatten_export_text(["a", {"text": "b"}]) == "ab"
    assert p.export_chat_id_to_bot(123, "public_supergroup") == -100123
    assert p.export_chat_id_to_bot(123, "private") == 123


def test_next_run_obeys_current_window_and_gap(monkeypatch):
    from handlers import past_pizda as p

    monkeypatch.setattr(p.random, "randint", lambda start, end: start)
    now = p.MOSCOW_TZ.localize(datetime(2025, 1, 10, 9, 0))
    first = p.compute_next_run(now, None)
    assert first.hour == p.WINDOW_START_HOUR
    last = p.MOSCOW_TZ.localize(datetime(2025, 1, 1, 12, 0))
    next_run = p.compute_next_run(now, last)
    assert next_run.date() == now.date()
    assert next_run.hour == p.WINDOW_START_HOUR


def test_candidate_roundtrip_uses_temporary_database(temp_database):
    from handlers import past_pizda as p
    import database as db

    assert p.remember_pizda_candidate(-8, 20, 100)
    assert db.pick_pizda_candidates(-8, 101, 5) == [20]


@pytest.mark.asyncio
async def test_past_pizda_reply_uses_exact_text_resource(monkeypatch):
    from handlers import past_pizda as p

    sent = AsyncMock()
    context = SimpleNamespace(bot=SimpleNamespace(send_message=sent))
    monkeypatch.setattr(p, "_start_of_today_ts", lambda: 100)
    monkeypatch.setattr(p.random, "randint", lambda _start, _end: 1)
    monkeypatch.setattr(p, "pick_pizda_candidates", lambda *_args: [42])
    monkeypatch.setattr(p, "mark_pizda_candidate_used", lambda *_args: None)
    monkeypatch.setattr(p.asyncio, "sleep", AsyncMock())

    await p.run_past_pizda_in_chat(context, -8)

    sent.assert_awaited_once_with(
        chat_id=-8,
        text="Пизда",
        reply_to_message_id=42,
    )
