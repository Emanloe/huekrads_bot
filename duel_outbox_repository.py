"""Chat-scoped durable publication intents for dormant persistent duels."""

import json
import sqlite3

from database import get_db
from duel_session_repository import utc_unix_milliseconds


DUEL_OUTBOX_LEASE_MS = 60_000


def _publication_from_row(row, description=None) -> dict | None:
    if row is None:
        return None
    value = dict(row) if isinstance(row, sqlite3.Row) else dict(
        zip((column[0] for column in description), row)
    )
    value["payload"] = json.loads(value.pop("payload_json"))
    return value


def create_duel_publication_in_transaction(
    cursor: sqlite3.Cursor, chat_id: int, duel_id: int, kind: str,
    turn_id: int | None, payload: dict, now_ms: int,
) -> dict:
    """Insert one server-built intent on the gameplay transaction's connection."""
    cursor.execute(
        """
        INSERT INTO duel_outbox
            (chat_id, duel_id, kind, turn_id, payload_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chat_id, duel_id, kind, turn_id,
         json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
         now_ms, now_ms),
    )
    return get_duel_publication_in_transaction(cursor, chat_id, cursor.lastrowid)


def get_duel_publication_in_transaction(
    cursor: sqlite3.Cursor, chat_id: int, publication_id: int,
) -> dict | None:
    cursor.execute(
        "SELECT * FROM duel_outbox WHERE chat_id = ? AND id = ?",
        (chat_id, publication_id),
    )
    return _publication_from_row(cursor.fetchone(), cursor.description)


def get_duel_publication(chat_id: int, publication_id: int) -> dict | None:
    with get_db() as conn:
        return get_duel_publication_in_transaction(conn.cursor(), chat_id, publication_id)


def get_duel_publication_by_kind_in_transaction(
    cursor: sqlite3.Cursor, chat_id: int, duel_id: int, kind: str,
) -> dict | None:
    cursor.execute(
        "SELECT * FROM duel_outbox WHERE chat_id = ? AND duel_id = ? AND kind = ?",
        (chat_id, duel_id, kind),
    )
    return _publication_from_row(cursor.fetchone(), cursor.description)


def list_retryable_duel_publications(
    chat_id: int, now_ms: int, *, limit: int = 100,
) -> list[dict]:
    """Find pending and expired-lease intents after restart, within one chat."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM duel_outbox
            WHERE chat_id = ? AND (
                status = 'pending' OR (status = 'leased' AND lease_until <= ?)
            )
            ORDER BY id LIMIT ?
            """,
            (chat_id, now_ms, limit),
        )
        rows, description = cursor.fetchall(), cursor.description
        return [_publication_from_row(row, description) for row in rows]


def list_retryable_duel_publication_chat_ids(now_ms: int) -> list[int]:
    """Trusted recovery loop discovers chats, then processes each chat-scoped row."""
    with get_db() as conn:
        return [row[0] for row in conn.execute(
            """
            SELECT DISTINCT chat_id FROM duel_outbox
            WHERE status = 'pending' OR (status = 'leased' AND lease_until <= ?)
            ORDER BY chat_id
            """,
            (now_ms,),
        )]


def claim_duel_publication(
    chat_id: int, publication_id: int, *, now_ms: int | None = None,
    lease_ms: int = DUEL_OUTBOX_LEASE_MS,
) -> dict | None:
    """One worker leases an intent; an expired lease is recoverable."""
    timestamp = utc_unix_milliseconds() if now_ms is None else now_ms
    if lease_ms <= 0:
        raise ValueError("Publication lease must be positive")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """
            UPDATE duel_outbox
            SET status = 'leased', lease_until = ?, attempt_count = attempt_count + 1,
                updated_at = ?
            WHERE chat_id = ? AND id = ? AND (
                status = 'pending' OR (status = 'leased' AND lease_until <= ?)
            )
            """,
            (timestamp + lease_ms, timestamp, chat_id, publication_id, timestamp),
        )
        if cursor.rowcount != 1:
            return None
        return get_duel_publication_in_transaction(cursor, chat_id, publication_id)


def release_duel_publication(
    chat_id: int, publication_id: int, attempt_count: int,
    *, now_ms: int | None = None,
) -> bool:
    """A temporary send failure leaves the intent retryable, not compensated."""
    timestamp = utc_unix_milliseconds() if now_ms is None else now_ms
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """
            UPDATE duel_outbox SET status = 'pending', lease_until = NULL,
                updated_at = ?
            WHERE chat_id = ? AND id = ? AND status = 'leased'
              AND attempt_count = ?
            """,
            (timestamp, chat_id, publication_id, attempt_count),
        )
        return cursor.rowcount == 1


def mark_duel_publication_delivered_in_transaction(
    cursor: sqlite3.Cursor, chat_id: int, publication_id: int,
    attempt_count: int, message_id: int, delivered_at: int,
) -> bool:
    cursor.execute(
        """
        UPDATE duel_outbox SET status = 'delivered', lease_until = NULL,
            message_id = ?, delivered_at = ?, updated_at = ?
        WHERE chat_id = ? AND id = ? AND status = 'leased'
          AND attempt_count = ?
        """,
        (message_id, delivered_at, delivered_at, chat_id, publication_id, attempt_count),
    )
    return cursor.rowcount == 1


def cancel_duel_publication_in_transaction(
    cursor: sqlite3.Cursor, chat_id: int, publication_id: int, now_ms: int,
) -> bool:
    cursor.execute(
        """
        UPDATE duel_outbox SET status = 'cancelled', lease_until = NULL,
            delivered_at = NULL, updated_at = ?
        WHERE chat_id = ? AND id = ? AND status <> 'cancelled'
        """,
        (now_ms, chat_id, publication_id),
    )
    return cursor.rowcount == 1
