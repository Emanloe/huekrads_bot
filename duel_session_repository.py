"""Chat-scoped storage for future persistent ordinary-duel sessions.

The Telegram duel runtime does not use this repository yet. It stores session
snapshots as JSON without interpreting game rules or performing RNG.
"""

import json
import sqlite3
from datetime import datetime, timezone

from database import get_db


def utc_unix_milliseconds(when: datetime | None = None) -> int:
    """Return UTC Unix milliseconds; callers may provide an aware test clock."""
    instant = when if when is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Duel session timestamps require a timezone-aware datetime")
    return int(instant.timestamp() * 1000)


def _json_object(value: dict) -> str:
    if not isinstance(value, dict):
        raise TypeError("Duel session JSON values must be dictionaries")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _session_from_row(row, description=None) -> dict | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        session = dict(row)
    else:
        session = dict(zip((column[0] for column in description), row))
    session["player1_snapshot"] = json.loads(session.pop("player1_snapshot_json"))
    session["player2_snapshot"] = json.loads(session.pop("player2_snapshot_json"))
    result_json = session.pop("result_json")
    session["result"] = json.loads(result_json) if result_json is not None else None
    return session


def create_duel_session(
    chat_id: int,
    player1_user_id: int,
    player2_user_id: int,
    player1_snapshot: dict,
    player2_snapshot: dict,
    attacker_user_id: int,
    defender_user_id: int,
    *,
    status: str = "publishing",
    phase: str = "attack",
    attack_zone: str | None = None,
    round_no: int = 1,
    turn_id: int = 1,
    deadline_at: int | None = None,
    message_id: int | None = None,
    original_message_id: int | None = None,
    result: dict | None = None,
    pocket_done_at: int | None = None,
    finished_at: int | None = None,
    now_ms: int | None = None,
    cursor: sqlite3.Cursor | None = None,
) -> dict:
    """Insert one session; SQLite constraints reject conflicting active slots.

    This is storage only: it does not register players, check admission, or
    decide the first attacker. An IntegrityError is left for the caller to
    classify without hiding a failed CHECK behind an active-slot conflict.

    When cursor is supplied, its caller owns the transaction. This lets the
    future start operation validate admission and insert under one write lock.
    """
    created_at = utc_unix_milliseconds() if now_ms is None else now_ms
    snapshot1_json = _json_object(player1_snapshot)
    snapshot2_json = _json_object(player2_snapshot)
    result_json = _json_object(result) if result is not None else None

    def insert_in_transaction(db_cursor: sqlite3.Cursor) -> dict:
        db_cursor.execute(
            """
            INSERT INTO duel_sessions (
                chat_id, player1_user_id, player2_user_id,
                player1_snapshot_json, player2_snapshot_json,
                attacker_user_id, defender_user_id, status, phase, attack_zone,
                round_no, turn_id, deadline_at, message_id, original_message_id,
                result_json, pocket_done_at, created_at, updated_at, finished_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                chat_id, player1_user_id, player2_user_id,
                snapshot1_json, snapshot2_json, attacker_user_id, defender_user_id,
                status, phase, attack_zone, round_no, turn_id, deadline_at,
                message_id, original_message_id, result_json, pocket_done_at,
                created_at, created_at, finished_at,
            ),
        )
        db_cursor.execute(
            "SELECT * FROM duel_sessions WHERE chat_id = ? AND id = ?",
            (chat_id, db_cursor.lastrowid),
        )
        return _session_from_row(db_cursor.fetchone(), db_cursor.description)

    if cursor is not None:
        return insert_in_transaction(cursor)
    with get_db() as conn:
        db_cursor = conn.cursor()
        db_cursor.execute("BEGIN IMMEDIATE")
        return insert_in_transaction(db_cursor)


def has_current_duel_session(chat_id: int, *, cursor: sqlite3.Cursor) -> bool:
    """Check this chat's active slot inside the caller's transaction."""
    return cursor.execute(
        """
        SELECT 1 FROM duel_sessions
        WHERE chat_id = ? AND status IN ('publishing', 'active')
        """,
        (chat_id,),
    ).fetchone() is not None


def get_duel_session(chat_id: int, duel_id: int) -> dict | None:
    """Never return a session without matching its chat security boundary."""
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM duel_sessions WHERE chat_id = ? AND id = ?",
            (chat_id, duel_id),
        ).fetchone()
        return _session_from_row(row)


def get_duel_session_in_transaction(
    chat_id: int, duel_id: int, *, cursor: sqlite3.Cursor,
) -> dict | None:
    """Read one chat-scoped session on the caller's locked connection."""
    cursor.execute(
        "SELECT * FROM duel_sessions WHERE chat_id = ? AND id = ?",
        (chat_id, duel_id),
    )
    return _session_from_row(cursor.fetchone(), cursor.description)


def activate_duel_session_in_transaction(
    chat_id: int,
    duel_id: int,
    turn_id: int,
    message_id: int,
    deadline_at: int,
    published_at: int,
    *,
    cursor: sqlite3.Cursor,
) -> bool:
    """Bind a published prompt and arm its deadline without changing turn_id."""
    cursor.execute(
        """
        UPDATE duel_sessions
        SET status = 'active', message_id = ?, deadline_at = ?, updated_at = ?
        WHERE chat_id = ? AND id = ? AND status = 'publishing' AND turn_id = ?
          AND (phase = 'attack' OR result_json IS NULL)
        """,
        (message_id, deadline_at, published_at, chat_id, duel_id, turn_id),
    )
    return cursor.rowcount == 1


def save_duel_attack_in_transaction(
    chat_id: int,
    duel_id: int,
    turn_id: int,
    attacker_user_id: int,
    zone: str,
    updated_at: int,
    *,
    cursor: sqlite3.Cursor,
) -> bool:
    """CAS an accepted attack into an unpublished block prompt."""
    cursor.execute(
        """
        UPDATE duel_sessions
        SET status = 'publishing', phase = 'block', attack_zone = ?,
            turn_id = turn_id + 1, deadline_at = NULL, result_json = NULL,
            updated_at = ?
        WHERE chat_id = ? AND id = ? AND status = 'active'
          AND phase = 'attack' AND turn_id = ? AND attacker_user_id = ?
        """,
        (zone, updated_at, chat_id, duel_id, turn_id, attacker_user_id),
    )
    return cursor.rowcount == 1


def save_duel_block_resolution_in_transaction(
    chat_id: int,
    duel_id: int,
    turn_id: int,
    defender_user_id: int,
    resolution: dict,
    updated_at: int,
    *,
    terminal: bool,
    cursor: sqlite3.Cursor,
) -> bool:
    """Checkpoint one resolved block; terminal DB effects are deliberately absent."""
    resolution_json = _json_object(resolution)
    if terminal:
        cursor.execute(
            """
            UPDATE duel_sessions
            SET status = 'publishing', turn_id = turn_id + 1,
                deadline_at = NULL, result_json = ?, updated_at = ?
            WHERE chat_id = ? AND id = ? AND status = 'active'
              AND phase = 'block' AND turn_id = ? AND defender_user_id = ?
            """,
            (resolution_json, updated_at, chat_id, duel_id, turn_id, defender_user_id),
        )
    else:
        cursor.execute(
            """
            UPDATE duel_sessions
            SET status = 'publishing', phase = 'attack', attack_zone = NULL,
                attacker_user_id = defender_user_id,
                defender_user_id = attacker_user_id,
                round_no = round_no + 1, turn_id = turn_id + 1,
                deadline_at = NULL, result_json = ?, updated_at = ?
            WHERE chat_id = ? AND id = ? AND status = 'active'
              AND phase = 'block' AND turn_id = ? AND defender_user_id = ?
            """,
            (resolution_json, updated_at, chat_id, duel_id, turn_id, defender_user_id),
        )
    return cursor.rowcount == 1


def finish_duel_session_in_transaction(
    chat_id: int, duel_id: int, turn_id: int, result: dict, finished_at: int,
    *, cursor: sqlite3.Cursor,
) -> bool:
    """Commit a server-computed terminal result under the caller's write lock."""
    cursor.execute(
        """
        UPDATE duel_sessions
        SET status = 'finished', result_json = ?, deadline_at = NULL,
            updated_at = ?, finished_at = ?
        WHERE chat_id = ? AND id = ? AND status = 'publishing'
          AND phase = 'block' AND turn_id = ? AND finished_at IS NULL
        """,
        (_json_object(result), finished_at, finished_at, chat_id, duel_id, turn_id),
    )
    return cursor.rowcount == 1


def get_current_duel_session(chat_id: int) -> dict | None:
    """Return this chat's sole publishing or active ordinary duel, if any."""
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT * FROM duel_sessions
            WHERE chat_id = ? AND status IN ('publishing', 'active')
            """,
            (chat_id,),
        ).fetchone()
        return _session_from_row(row)


def bind_duel_session_message(
    chat_id: int,
    duel_id: int,
    message_id: int,
    *,
    expected_turn_id: int,
    expected_status: str = "publishing",
    now_ms: int | None = None,
) -> bool:
    """CAS-bind a Telegram message for one expected turn and status."""
    updated_at = utc_unix_milliseconds() if now_ms is None else now_ms
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE duel_sessions SET message_id = ?, updated_at = ?
            WHERE chat_id = ? AND id = ? AND status = ? AND turn_id = ?
            """,
            (message_id, updated_at, chat_id, duel_id, expected_status, expected_turn_id),
        )
        return cursor.rowcount == 1


def list_due_duel_sessions(chat_id: int, now_ms: int, *, limit: int = 100) -> list[dict]:
    """Find only this chat's active sessions whose UTC deadline has passed."""
    if limit < 1:
        raise ValueError("limit must be positive")
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM duel_sessions
            WHERE chat_id = ? AND status = 'active' AND deadline_at <= ?
            ORDER BY deadline_at, id LIMIT ?
            """,
            (chat_id, now_ms, limit),
        ).fetchall()
        return [_session_from_row(row) for row in rows]
