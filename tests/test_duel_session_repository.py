"""Dormant persistent ordinary-duel schema and chat-scoped repository."""

import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import database
from duel_session_repository import (
    bind_duel_session_message,
    create_duel_session,
    get_current_duel_session,
    get_duel_session,
    list_due_duel_sessions,
    utc_unix_milliseconds,
)


CHAT_A = -7101
CHAT_B = -7102
NOW_MS = 1_800_000_000_000


def create(chat_id=CHAT_A, **overrides):
    fields = {
        "player1_user_id": 11,
        "player2_user_id": 22,
        "player1_snapshot": {"user_id": 11, "display_name": "Гном"},
        "player2_snapshot": {"user_id": 22, "display_name": "Другой"},
        "attacker_user_id": 11,
        "defender_user_id": 22,
        "now_ms": NOW_MS,
    }
    fields.update(overrides)
    return create_duel_session(chat_id, **fields)


def test_schema_is_additive_and_init_is_idempotent(temp_database):
    session = create()
    with sqlite3.connect(temp_database) as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(duel_sessions)")}
        assert {
            "idx_duel_sessions_one_active_chat", "idx_duel_sessions_due",
        } <= indexes
        conn.execute(
            "INSERT INTO duel_users (user_id, chat_id, display_name, points) "
            "VALUES (?, ?, ?, ?)",
            (99, CHAT_A, "existing", 37),
        )

    database.init_db()
    database.init_db()

    assert get_duel_session(CHAT_A, session["id"]) == session
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute(
            "SELECT display_name, points FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, 99),
        ).fetchone() == ("existing", 37)


def test_valid_publishing_attack_session_and_json_round_trip(temp_database):
    session = create(original_message_id=901)

    assert isinstance(session, dict)
    assert session["chat_id"] == CHAT_A
    assert session["status"] == "publishing"
    assert session["phase"] == "attack"
    assert session["attack_zone"] is None
    assert session["round_no"] == session["turn_id"] == 1
    assert session["deadline_at"] is None
    assert session["created_at"] == session["updated_at"] == NOW_MS
    assert session["original_message_id"] == 901
    assert session["player1_snapshot"] == {"user_id": 11, "display_name": "Гном"}
    assert get_duel_session(CHAT_A, session["id"]) == session
    with sqlite3.connect(temp_database) as conn:
        stored = conn.execute(
            "SELECT player1_snapshot_json FROM duel_sessions WHERE chat_id = ? AND id = ?",
            (CHAT_A, session["id"]),
        ).fetchone()[0]
    assert '"Гном"' in stored
    assert "\\u0413" not in stored


def test_valid_active_session_requires_and_finds_deadline(temp_database):
    session = create(status="active", deadline_at=NOW_MS + 10_000)
    assert get_current_duel_session(CHAT_A)["id"] == session["id"]
    assert list_due_duel_sessions(CHAT_A, NOW_MS + 9_999) == []
    assert [item["id"] for item in list_due_duel_sessions(CHAT_A, NOW_MS + 10_000)] == [
        session["id"],
    ]


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"status": "active"},
        {"status": "publishing", "deadline_at": NOW_MS + 10_000},
        {"phase": "attack", "attack_zone": "head"},
        {"phase": "block"},
        {"phase": "block", "attack_zone": "other"},
        {"player2_user_id": 11},
        {"attacker_user_id": 33},
        {"defender_user_id": 33},
        {"status": "finished", "finished_at": NOW_MS + 1},
        {"status": "finished", "result": {}},
        {"pocket_done_at": NOW_MS + 1},
        {"round_no": 0},
        {"turn_id": 0},
        {"status": "unknown"},
        {"phase": "unknown"},
    ],
)
def test_schema_rejects_invalid_state(temp_database, invalid_fields):
    with pytest.raises(sqlite3.IntegrityError):
        create(**invalid_fields)
    assert get_current_duel_session(CHAT_A) is None


def test_valid_block_and_finished_session(temp_database):
    block = create(
        status="finished", phase="block", attack_zone="dick",
        result={"winner_user_id": 11}, finished_at=NOW_MS + 1,
        pocket_done_at=NOW_MS + 2,
    )
    assert block["result"] == {"winner_user_id": 11}
    assert block["attack_zone"] == "dick"
    assert block["pocket_done_at"] == NOW_MS + 2


def test_one_publishing_or_active_session_per_chat(temp_database):
    first = create()
    with pytest.raises(sqlite3.IntegrityError):
        create(status="active", deadline_at=NOW_MS + 10_000)
    assert get_current_duel_session(CHAT_A)["id"] == first["id"]


@pytest.mark.parametrize("terminal_status", ["finished", "cancelled"])
def test_terminal_session_releases_active_slot(temp_database, terminal_status):
    first = create(
        status="active", deadline_at=NOW_MS + 10_000,
    ) if terminal_status == "finished" else create()
    with sqlite3.connect(temp_database) as conn:
        if terminal_status == "finished":
            conn.execute(
                "UPDATE duel_sessions SET status = 'finished', result_json = '{}', "
                "finished_at = ?, deadline_at = NULL WHERE chat_id = ? AND id = ?",
                (NOW_MS + 1, CHAT_A, first["id"]),
            )
        else:
            conn.execute(
                "UPDATE duel_sessions SET status = 'cancelled' "
                "WHERE chat_id = ? AND id = ?",
                (CHAT_A, first["id"]),
            )
    second = create()
    assert second["id"] != first["id"]
    assert get_current_duel_session(CHAT_A)["id"] == second["id"]


def test_same_users_and_ids_are_independent_in_different_chats(temp_database):
    first = create(CHAT_A, status="active", deadline_at=NOW_MS + 1)
    second = create(CHAT_B, status="active", deadline_at=NOW_MS + 2)

    assert first["player1_user_id"] == second["player1_user_id"] == 11
    assert first["player2_user_id"] == second["player2_user_id"] == 22
    assert get_duel_session(CHAT_B, first["id"]) is None
    assert get_duel_session(CHAT_A, second["id"]) is None
    assert get_current_duel_session(CHAT_A)["id"] == first["id"]
    assert get_current_duel_session(CHAT_B)["id"] == second["id"]
    assert [item["id"] for item in list_due_duel_sessions(CHAT_A, NOW_MS + 2)] == [
        first["id"],
    ]
    assert [item["id"] for item in list_due_duel_sessions(CHAT_B, NOW_MS + 2)] == [
        second["id"],
    ]


def test_due_query_excludes_publishing_finished_cancelled_and_future(temp_database):
    old = create(status="finished", result={}, finished_at=NOW_MS)
    future = create(status="active", deadline_at=NOW_MS + 1)
    create(CHAT_B, status="active", deadline_at=NOW_MS)

    assert list_due_duel_sessions(CHAT_A, NOW_MS) == []
    assert [item["id"] for item in list_due_duel_sessions(CHAT_A, NOW_MS + 1)] == [
        future["id"],
    ]
    assert get_duel_session(CHAT_A, old["id"])["status"] == "finished"
    with pytest.raises(ValueError):
        list_due_duel_sessions(CHAT_A, NOW_MS + 1, limit=0)


def test_message_binding_is_chat_and_status_scoped(temp_database):
    session = create()
    assert not bind_duel_session_message(
        CHAT_B, session["id"], 123, expected_turn_id=1, now_ms=NOW_MS + 1,
    )
    assert not bind_duel_session_message(
        CHAT_A, session["id"], 123, expected_turn_id=1,
        expected_status="active", now_ms=NOW_MS + 1,
    )
    assert not bind_duel_session_message(
        CHAT_A, session["id"], 123, expected_turn_id=2, now_ms=NOW_MS + 1,
    )
    assert get_duel_session(CHAT_A, session["id"])["message_id"] is None
    assert bind_duel_session_message(
        CHAT_A, session["id"], 123, expected_turn_id=1, now_ms=NOW_MS + 1,
    )
    bound = get_duel_session(CHAT_A, session["id"])
    assert bound["message_id"] == 123
    assert bound["updated_at"] == NOW_MS + 1


def test_concurrent_same_chat_inserts_have_one_winner(temp_database):
    barrier = Barrier(2)

    def attempt(status):
        barrier.wait()
        try:
            return create(
                status=status,
                deadline_at=NOW_MS + 10_000 if status == "active" else None,
            )
        except sqlite3.IntegrityError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("publishing", "active")))

    assert sum(session is not None for session in results) == 1
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM duel_sessions WHERE chat_id = ? "
            "AND status IN ('publishing', 'active')",
            (CHAT_A,),
        ).fetchone()[0] == 1


def test_json_rejects_telegram_objects_without_inserting(temp_database):
    with pytest.raises(TypeError):
        create(player1_snapshot={"telegram_user": SimpleNamespace(id=11)})
    assert get_current_duel_session(CHAT_A) is None


def test_utc_milliseconds_accepts_aware_clock_and_rejects_naive():
    moscow = timezone(timedelta(hours=3))
    assert utc_unix_milliseconds(datetime(2026, 10, 1, 3, tzinfo=moscow)) == (
        utc_unix_milliseconds(datetime(2026, 10, 1, tzinfo=timezone.utc))
    )
    with pytest.raises(ValueError):
        utc_unix_milliseconds(datetime(2026, 10, 1))


def test_repository_never_calls_rng(temp_database, monkeypatch):
    choice = Mock(side_effect=AssertionError("repository used random.choice"))
    roll = Mock(side_effect=AssertionError("repository used random.random"))
    monkeypatch.setattr(random, "choice", choice)
    monkeypatch.setattr(random, "random", roll)

    session = create(status="active", deadline_at=NOW_MS)
    assert get_duel_session(CHAT_A, session["id"])["id"] == session["id"]
    assert get_current_duel_session(CHAT_A)["id"] == session["id"]
    assert list_due_duel_sessions(CHAT_A, NOW_MS)[0]["id"] == session["id"]
    assert bind_duel_session_message(
        CHAT_A, session["id"], 123, expected_turn_id=1,
        expected_status="active", now_ms=NOW_MS + 1,
    )
    choice.assert_not_called()
    roll.assert_not_called()
