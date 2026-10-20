"""Confirmed event losses share the existing personal chronicle journal."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database as db
from handlers import hyperborean_event as event
from handlers.recap import build_recap_model, build_recap_text, game_day_bounds_ms, load_recap_events
from tests.test_hyperborean_event import _get_dick_state, _set_dick_state
from tests.test_recap import CHAT, OTHER_CHAT, user


@pytest.fixture
def event_db(temp_database, monkeypatch):
    monkeypatch.setattr(event, "_HYPERBOREAN_DB_PATH", temp_database)
    monkeypatch.setattr(event, "ACTIVE_HYPERBOREAN_EVENTS", {})
    return temp_database


def register(db_path, user_id=1, *, chat_id=CHAT, dickless=0):
    actor = user(user_id)
    db.get_or_create_duel_user(actor, chat_id)
    _set_dick_state(db_path, chat_id, user_id, points=73, dick_stolen_today=dickless)
    return actor


def journal(user_id=1, chat_id=CHAT):
    return db.list_game_events(chat_id, user_id, 0, 10**15)


@pytest.mark.parametrize("reason, wording", [
    ("hyperboreic", "• Лишился хуя из-за гиперборейского хуя."),
    ("arthur", "• Лишился хуя из-за хуя Короля Артура."),
])
async def test_callback_journals_actual_loss_once(event_db, fake_context, reason, wording):
    actor = register(event_db)
    saved = {"message_id": 77, "event_type": reason}
    event.ACTIVE_HYPERBOREAN_EVENTS[CHAT] = saved
    query = SimpleNamespace(data="hyperboreic_huy_self", from_user=actor,
                            message=SimpleNamespace(message_id=77), answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT))
    fake_context.bot.edit_message_reply_markup = AsyncMock()

    await event.hyperboreic_huy_callback(update, fake_context)
    await event.hyperboreic_huy_callback(update, fake_context)
    # Even another worker with a stale in-memory event cannot apply it again.
    event.ACTIVE_HYPERBOREAN_EVENTS[CHAT] = saved
    await event.hyperboreic_huy_callback(update, fake_context)

    assert _get_dick_state(event_db, CHAT, 1) == (0, 1)
    rows = load_recap_events(CHAT, 1)
    assert len(rows) == 1
    assert rows[0]["source_id"] == f"hyperborean:{CHAT}:77"
    assert rows[0]["occurred_at"] > 0
    assert rows[0]["data"] == {
        "reason": reason, "action": "self", "result": "exploded", "dick_lost": True,
    }
    assert build_recap_model(rows, 1)["thefts"] == [wording]
    text = build_recap_text(rows, 1)
    assert "🍆 <b>Происшествия с хуём</b>" in text
    assert text.count(wording) == 1
    fake_context.bot.send_message.assert_awaited_once()


@pytest.mark.parametrize("reason", ["hyperboreic", "arthur"])
@pytest.mark.parametrize("dickless", [0, 1])
def test_other_action_never_journals_a_false_loss(event_db, monkeypatch, reason, dickless):
    actor = register(event_db, dickless=dickless)
    monkeypatch.setattr(event.random, "choice", lambda rows: rows[0])
    result, selected, _ = event._claim_hyperboreic_huy_for_other(CHAT, actor, reason, "other:77")
    assert result == ("exploded" if dickless else "unchanged")
    assert selected["user_id"] == 1
    assert _get_dick_state(event_db, CHAT, 1) == (73, dickless)
    rows = journal()
    assert len(rows) == 1 and rows[0]["data"]["dick_lost"] is False
    assert build_recap_model(rows, 1)["empty"] is True
    # A retry may neither reroll a target nor switch to the self action.
    monkeypatch.setattr(event.random, "choice", lambda _: pytest.fail("Repeated target roll"))
    assert event._claim_hyperboreic_huy_for_other(CHAT, actor, reason, "other:77")[0] == "taken"
    assert event._claim_hyperboreic_huy(CHAT, actor, reason, "other:77") == "taken"
    assert len(journal()) == 1
    assert _get_dick_state(event_db, CHAT, 1) == (73, dickless)


@pytest.mark.parametrize("reason", ["hyperboreic", "arthur"])
def test_restore_then_new_loss_are_distinct_and_replay_cannot_toggle_state(event_db, reason):
    actor = register(event_db)
    assert event._claim_hyperboreic_huy(CHAT, actor, reason, "self:1") == "exploded"
    assert event._claim_hyperboreic_huy(CHAT, actor, reason, "self:2") == "restored"
    assert _get_dick_state(event_db, CHAT, 1) == (0, 0)
    assert event._claim_hyperboreic_huy(CHAT, actor, reason, "self:1") == "taken"
    assert _get_dick_state(event_db, CHAT, 1) == (0, 0)
    assert event._claim_hyperboreic_huy(CHAT, actor, reason, "self:3") == "exploded"
    assert event._claim_hyperboreic_huy(CHAT, actor, reason, "self:2") == "taken"
    assert _get_dick_state(event_db, CHAT, 1) == (0, 1)
    rows = load_recap_events(CHAT, 1)
    assert [row["data"]["dick_lost"] for row in rows] == [True, False, True]
    assert len(build_recap_model(rows, 1)["thefts"]) == 2
    assert build_recap_text(rows, 1).count("• Лишился хуя из-за") == 2


@pytest.mark.parametrize("reason", ["hyperboreic", "arthur"])
def test_already_dickless_self_action_restores_without_reporting_loss(event_db, reason):
    actor = register(event_db, dickless=1)
    assert event._claim_hyperboreic_huy(CHAT, actor, reason, "restore:1") == "restored"
    assert _get_dick_state(event_db, CHAT, 1) == (73, 0)
    rows = journal()
    assert rows[0]["data"]["dick_lost"] is False
    assert build_recap_model(rows, 1)["empty"] is True


@pytest.mark.parametrize("after_insert", [False, True])
@pytest.mark.parametrize("dickless", [0, 1])
def test_journal_failure_rolls_back_state_and_fact(event_db, monkeypatch, after_insert, dickless):
    actor = register(event_db, dickless=dickless)
    original = event.record_game_event_in_transaction

    def fail(*args, **kwargs):
        if after_insert:
            original(*args, **kwargs)
        raise RuntimeError("Journal failure")

    monkeypatch.setattr(event, "record_game_event_in_transaction", fail)
    assert event._claim_hyperboreic_huy(CHAT, actor, "arthur", "rollback:1") == "error"
    assert _get_dick_state(event_db, CHAT, 1) == (73, dickless)
    assert journal() == []
    monkeypatch.setattr(event, "record_game_event_in_transaction", original)
    assert event._claim_hyperboreic_huy(CHAT, actor, "arthur", "rollback:1") == (
        "restored" if dickless else "exploded"
    )
    assert len(journal()) == 1


def test_concurrent_claims_of_one_message_only_change_one_player(event_db):
    actors = [register(event_db, player) for player in (1, 2)]
    barrier = Barrier(2)

    def claim(actor):
        barrier.wait(timeout=10)
        return event._claim_hyperboreic_huy(CHAT, actor, "arthur", "race:1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, actors))
    assert sorted(results) == ["exploded", "taken"]
    assert sorted(_get_dick_state(event_db, CHAT, actor.id) for actor in actors) == [(0, 1), (73, 0)]
    assert len(journal(1) + journal(2)) == 1


def test_concurrent_distinct_events_use_actual_locked_state(event_db):
    actor = register(event_db)
    barrier = Barrier(2)

    def claim(source):
        barrier.wait(timeout=10)
        return event._claim_hyperboreic_huy(CHAT, actor, "hyperboreic", source)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["race:1", "race:2"]))
    assert sorted(results) == ["exploded", "restored"]
    assert _get_dick_state(event_db, CHAT, 1) == (0, 0)
    assert len(journal()) == 2
    assert len(build_recap_model(journal(), 1)["thefts"]) == 1


def test_event_history_uses_moscow_day_chat_and_player(event_db, monkeypatch):
    actor = register(event_db)
    other = register(event_db, 2)
    register(event_db, chat_id=OTHER_CHAT)
    when = datetime(2026, 10, 20, 22, 30, tzinfo=timezone.utc)
    start, end = game_day_bounds_ms(when)
    for index, (at, chat_id, target) in enumerate([
        (start - 1, CHAT, actor), (start, CHAT, actor),
        (end - 1, CHAT, actor), (end, CHAT, actor),
        (start, OTHER_CHAT, actor), (start, CHAT, other),
    ]):
        _set_dick_state(event_db, chat_id, target.id, points=73, dick_stolen_today=0)
        monkeypatch.setattr(db, "_utc_now_ms", lambda: at)
        assert event._claim_hyperboreic_huy(chat_id, target, "arthur", f"bounds:{index}") == "exploded"
    rows = load_recap_events(CHAT, 1, when)
    assert [row["source_id"] for row in rows] == ["bounds:1", "bounds:2"]
    assert len(build_recap_model(rows, 1)["thefts"]) == 2


async def test_old_message_cannot_consume_a_new_event(event_db, fake_context):
    actor = register(event_db)
    saved = {"message_id": 78, "event_type": "arthur"}
    event.ACTIVE_HYPERBOREAN_EVENTS[CHAT] = saved
    query = SimpleNamespace(data="hyperboreic_huy_self", from_user=actor,
                            message=SimpleNamespace(message_id=77), answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT))
    await event.hyperboreic_huy_callback(update, fake_context)
    assert event.ACTIVE_HYPERBOREAN_EVENTS[CHAT] == saved
    assert _get_dick_state(event_db, CHAT, 1) == (73, 0)
    assert journal() == []


def test_legacy_records_and_different_causes_remain_separate(event_db):
    # Older records have no reason/dick_lost/source fields. They retain their
    # established meaning, while incomplete resolution records imply no loss.
    rows = [
        {"event_type": "dick_stolen"},
        {"event_type": "hyperborean_resolved", "data": {}},
        {"event_type": "hyperborean_resolved", "data": {"reason": "arthur"}},
        {"event_type": "hyperborean_resolved", "data": {
            "reason": "unknown", "dick_lost": True,
        }},
    ]
    actor = register(event_db)
    event._claim_hyperboreic_huy(CHAT, actor, "hyperboreic", "cause:1")
    event._claim_hyperboreic_huy(CHAT, actor, "arthur", "cause:2")  # Restore.
    event._claim_hyperboreic_huy(CHAT, actor, "arthur", "cause:3")
    model = build_recap_model(rows + journal(), 1)
    assert model["thefts"] == [
        "• Хуегрыз откусил тебе хуй при подборе добычи",
        "• Лишился хуя из-за гиперборейского хуя.",
        "• Лишился хуя из-за хуя Короля Артура.",
    ]


@pytest.mark.parametrize("winner", [1, 2])
def test_ordinary_duel_theft_keeps_legacy_wording(event_db, winner):
    rows = [{"event_type": "duel_finished", "data": {
        "initiator_user_id": 1, "initiator_name": "Мой гном",
        "opponent_user_id": 2, "opponent_name": "Когли Изобретательный",
        "winner_user_id": winner,
        "winner_name": "Мой гном" if winner == 1 else "Когли Изобретательный",
        "loser_user_id": 2 if winner == 1 else 1,
        "loser_name": "Когли Изобретательный" if winner == 1 else "Мой гном",
        "is_dick_stolen": True,
    }}]
    assert build_recap_model(rows, 1)["thefts"] == [
        f"• {'украл хуй у' if winner == 1 else 'лишился хуя из-за'} Когли Изобретательный",
    ]
