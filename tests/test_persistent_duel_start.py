"""Persistent ordinary-duel start, not yet connected to Telegram handlers."""

import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import database
from duel_session_repository import create_duel_session, get_current_duel_session, get_duel_session
from handlers import duel_service


CHAT_A = -8201
CHAT_B = -8202
NOW_MS = 1_800_000_000_000
SNAPSHOT_FIELDS = {
    "user_id", "username", "display_name", "dwarf_name", "points", "daily_wins",
}


@pytest.fixture
def start_db(temp_database, monkeypatch):
    monkeypatch.setattr(database, "_get_today_date_str", lambda: "2026-09-24")
    return temp_database


def register(chat_id, user_id, *, points=20, daily_wins=0, no_dick=False, dwarf_name=None):
    username = f"player{user_id}"
    database.get_or_create_duel_user(
        SimpleNamespace(id=user_id, username=username, first_name=username), chat_id,
    )
    with database.get_db() as conn:
        conn.execute(
            """
            UPDATE duel_users SET points = ?, daily_wins = ?,
                dick_stolen_today = ?, dwarf_name = ?
            WHERE chat_id = ? AND user_id = ?
            """,
            (points, daily_wins, int(no_dick), dwarf_name, chat_id, user_id),
        )


def active_count(path, chat_id=CHAT_A):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM duel_sessions WHERE chat_id = ? "
            "AND status IN ('publishing', 'active')",
            (chat_id,),
        ).fetchone()[0]


def reject_without_rng(monkeypatch, chat_id, initiator_id, opponent_id, reason):
    choice = Mock(side_effect=AssertionError("rejected start used RNG"))
    roll = Mock(side_effect=AssertionError("rejected start used random.random"))
    monkeypatch.setattr(random, "choice", choice)
    monkeypatch.setattr(random, "random", roll)
    result = duel_service.start_persistent_duel(
        chat_id, initiator_id, opponent_id, now_ms=NOW_MS,
    )
    assert result.success is False
    assert result.reason == reason
    assert result.session is None
    choice.assert_not_called()
    roll.assert_not_called()
    return result


def test_snapshot_exact_fields_and_json_round_trip(start_db):
    user = {
        "user_id": 1, "username": "telegram", "display_name": "Telegram",
        "dwarf_name": "Гномыч", "points": 14, "daily_wins": 3,
        "first_name": "not used", "dick_stolen_today": False,
        "wins": 20, "inventory": ["not stored"],
    }
    snapshot = duel_service.DuelParticipantSnapshot.from_duel_user(user)
    stored = snapshot.to_storage()
    assert set(stored) == SNAPSHOT_FIELDS
    assert "first_name" not in stored
    assert "dick_stolen_today" not in stored
    assert "inventory" not in stored
    session = create_duel_session(
        CHAT_A, 1, 2, stored, {**stored, "user_id": 2}, 1, 2, now_ms=NOW_MS,
    )
    restored = duel_service.DuelParticipantSnapshot.from_storage(
        get_duel_session(CHAT_A, session["id"])["player1_snapshot"],
    )
    assert restored == snapshot
    user["points"] = 0
    stored["dwarf_name"] = "changed"
    assert snapshot.points == 14
    assert snapshot.dwarf_name == "Гномыч"


@pytest.mark.parametrize(
    "bad_snapshot",
    [
        {"user_id": 1},
        {"user_id": 1, "username": None, "display_name": "A",
         "dwarf_name": None, "points": 1, "daily_wins": 0, "extra": 1},
        {"user_id": 1, "username": None, "display_name": "A",
         "dwarf_name": None, "points": True, "daily_wins": 0},
    ],
)
def test_snapshot_deserializer_rejects_noncanonical_values(bad_snapshot):
    with pytest.raises(ValueError):
        duel_service.DuelParticipantSnapshot.from_storage(bad_snapshot)


@pytest.mark.parametrize("initiator_first", [True, False])
def test_successful_start_preserves_existing_first_attacker_choice(
    start_db, monkeypatch, initiator_first,
):
    register(CHAT_A, 1, points=30, daily_wins=2, dwarf_name="Гномыч")
    register(CHAT_A, 2, points=40, daily_wins=1)
    choice = Mock(return_value=initiator_first)
    roll = Mock(side_effect=AssertionError("start used random.random"))
    monkeypatch.setattr(random, "choice", choice)
    monkeypatch.setattr(random, "random", roll)

    result = duel_service.start_persistent_duel(CHAT_A, 1, 2, now_ms=NOW_MS)

    assert result.success is True
    assert result.reason is None
    session = result.session
    assert session["chat_id"] == CHAT_A
    assert (session["player1_user_id"], session["player2_user_id"]) == (1, 2)
    assert (session["attacker_user_id"], session["defender_user_id"]) == (
        (1, 2) if initiator_first else (2, 1)
    )
    assert session["status"] == "publishing"
    assert session["phase"] == "attack"
    assert session["attack_zone"] is None
    assert session["deadline_at"] is None
    assert session["message_id"] is None
    assert session["round_no"] == session["turn_id"] == 1
    assert session["created_at"] == session["updated_at"] == NOW_MS
    assert session["player1_snapshot"] == {
        "user_id": 1, "username": "player1", "display_name": "player1",
        "dwarf_name": "Гномыч", "points": 30, "daily_wins": 2,
    }
    assert set(session["player2_snapshot"]) == SNAPSHOT_FIELDS
    assert get_current_duel_session(CHAT_A)["id"] == session["id"]
    choice.assert_called_once_with([True, False])
    roll.assert_not_called()


def test_stored_snapshots_do_not_follow_later_user_changes(start_db, monkeypatch):
    register(CHAT_A, 1, points=35, daily_wins=3, dwarf_name="Старое имя")
    register(CHAT_A, 2, points=16)
    monkeypatch.setattr(random, "choice", lambda values: True)
    session = duel_service.start_persistent_duel(CHAT_A, 1, 2, now_ms=NOW_MS).session
    original = get_duel_session(CHAT_A, session["id"])["player1_snapshot"]

    with database.get_db() as conn:
        conn.execute(
            """
            UPDATE duel_users SET points = 0, daily_wins = 99,
                dwarf_name = 'Новое имя', dick_stolen_today = 1
            WHERE chat_id = ? AND user_id = ?
            """,
            (CHAT_A, 1),
        )

    assert get_duel_session(CHAT_A, session["id"])["player1_snapshot"] == original
    assert original["points"] == 35
    assert original["daily_wins"] == 3
    assert original["dwarf_name"] == "Старое имя"
    assert "dick_stolen_today" not in original


def test_start_uses_existing_lazy_daily_reset_inside_transaction(start_db, monkeypatch):
    register(CHAT_A, 1, points=0, daily_wins=7, no_dick=True)
    register(CHAT_A, 2)
    with database.get_db() as conn:
        conn.execute(
            "UPDATE duel_users SET last_activity_date = '2026-09-23' "
            "WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, 1),
        )
    monkeypatch.setattr(random, "choice", lambda options: True)

    result = duel_service.start_persistent_duel(CHAT_A, 1, 2, now_ms=NOW_MS)

    assert result.success
    assert result.session["player1_snapshot"]["points"] == 20
    assert result.session["player1_snapshot"]["daily_wins"] == 0
    refreshed = database.get_duel_user_by_id(CHAT_A, 1)
    assert refreshed["points"] == 20
    assert refreshed["dick_stolen_today"] is False


def test_missing_initiator_does_not_register_or_roll(start_db, monkeypatch):
    register(CHAT_A, 2)
    reject_without_rng(monkeypatch, CHAT_A, 1, 2, "initiator_not_registered")
    assert active_count(start_db) == 0
    assert database.get_duel_user_by_id(CHAT_A, 1) is None


def test_missing_opponent_and_cross_chat_opponent_do_not_roll(start_db, monkeypatch):
    register(CHAT_A, 1)
    register(CHAT_B, 2)
    reject_without_rng(monkeypatch, CHAT_A, 1, 2, "opponent_not_registered")
    assert active_count(start_db) == 0
    assert database.get_duel_user_by_id(CHAT_A, 2) is None
    assert database.get_duel_user_by_id(CHAT_B, 2) is not None


def test_self_duel_does_not_roll(start_db, monkeypatch):
    register(CHAT_A, 1)
    reject_without_rng(monkeypatch, CHAT_A, 1, 1, "self_target")
    assert active_count(start_db) == 0


@pytest.mark.parametrize(
    "player,points,no_dick,reason",
    [
        ("initiator", 20, True, "initiator_no_dick"),
        ("initiator", 0, True, "initiator_no_dick"),
        ("opponent", 20, True, "opponent_no_dick"),
        ("opponent", 0, True, "opponent_no_dick"),
    ],
)
def test_existing_admission_rules_and_no_dick_priority(
    start_db, monkeypatch, player, points, no_dick, reason,
):
    register(
        CHAT_A, 1,
        points=points if player == "initiator" else 20,
        no_dick=no_dick if player == "initiator" else False,
    )
    register(
        CHAT_A, 2,
        points=points if player == "opponent" else 20,
        no_dick=no_dick if player == "opponent" else False,
    )
    reject_without_rng(monkeypatch, CHAT_A, 1, 2, reason)
    assert active_count(start_db) == 0


@pytest.mark.parametrize("zero_player", ("initiator", "opponent"))
def test_zero_point_player_starts_persistent_duel(start_db, monkeypatch, zero_player):
    register(CHAT_A, 1, points=0 if zero_player == "initiator" else 20)
    register(CHAT_A, 2, points=0 if zero_player == "opponent" else 20)
    choice = Mock(return_value=True)
    roll = Mock(side_effect=AssertionError("start used random.random"))
    monkeypatch.setattr(random, "choice", choice)
    monkeypatch.setattr(random, "random", roll)

    started = duel_service.start_persistent_duel(CHAT_A, 1, 2, now_ms=NOW_MS)

    assert started.success
    assert started.session["player1_snapshot"]["points"] == (0 if zero_player == "initiator" else 20)
    assert started.session["player2_snapshot"]["points"] == (0 if zero_player == "opponent" else 20)
    assert active_count(start_db) == 1
    choice.assert_called_once_with([True, False])
    roll.assert_not_called()


@pytest.mark.parametrize("existing_status", ["publishing", "active"])
def test_existing_session_rejects_start_before_rng(
    start_db, monkeypatch, existing_status,
):
    register(CHAT_A, 1)
    register(CHAT_A, 2)
    create_duel_session(
        CHAT_A, 1, 2, {"user_id": 1}, {"user_id": 2}, 1, 2,
        status=existing_status,
        deadline_at=NOW_MS + 10_000 if existing_status == "active" else None,
        now_ms=NOW_MS,
    )
    reject_without_rng(monkeypatch, CHAT_A, 1, 2, "active_duel")
    assert active_count(start_db) == 1


def test_same_user_ids_start_independently_in_different_chats(start_db, monkeypatch):
    for chat_id in (CHAT_A, CHAT_B):
        register(chat_id, 1, points=10 if chat_id == CHAT_A else 40)
        register(chat_id, 2)
    choice = Mock(return_value=True)
    monkeypatch.setattr(random, "choice", choice)

    first = duel_service.start_persistent_duel(CHAT_A, 1, 2, now_ms=NOW_MS)
    second = duel_service.start_persistent_duel(CHAT_B, 1, 2, now_ms=NOW_MS)

    assert first.success and second.success
    assert first.session["player1_snapshot"]["points"] == 10
    assert second.session["player1_snapshot"]["points"] == 40
    assert get_duel_session(CHAT_B, first.session["id"]) is None
    assert get_duel_session(CHAT_A, second.session["id"]) is None
    assert active_count(start_db, CHAT_A) == active_count(start_db, CHAT_B) == 1
    assert choice.call_count == 2


def test_concurrent_same_chat_start_uses_rng_once(start_db, monkeypatch):
    register(CHAT_A, 1)
    register(CHAT_A, 2)
    barrier = Barrier(2)
    lock = Lock()
    calls = []

    def choose(options):
        assert options == [True, False]
        with lock:
            calls.append(1)
        return True

    monkeypatch.setattr(random, "choice", choose)

    def attempt(_):
        barrier.wait()
        return duel_service.start_persistent_duel(CHAT_A, 1, 2, now_ms=NOW_MS)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))

    assert sorted(result.reason for result in results if not result.success) == [
        "active_duel",
    ]
    assert sum(result.success for result in results) == 1
    assert active_count(start_db) == 1
    assert calls == [1]
