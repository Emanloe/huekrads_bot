"""One-use group launch credentials and durable chat-scoped sessions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

import database
from database import get_db
from miniapp_sessions import (
    LAUNCH_TTL_SECONDS, SESSION_TTL_SECONDS, bind_launch_message_id, create_launch_token,
    exchange_launch_token, get_miniapp_session,
)


CHAT_A = -8801
CHAT_B = -8802
NOW = 1_800_000_000


def test_launch_is_user_and_chat_bound_one_time_and_digest_only(temp_database):
    launch = create_launch_token(CHAT_A, 101, now=NOW)
    assert exchange_launch_token(launch, 202, now=NOW) is None
    issued = exchange_launch_token(launch, 101, now=NOW)
    assert issued.session.chat_id == CHAT_A and issued.session.user_id == 101
    assert issued.launch_message_id is None
    assert issued.session.expires_at == NOW + SESSION_TTL_SECONDS
    assert exchange_launch_token(launch, 101, now=NOW) is None
    assert get_miniapp_session(issued.token, now=NOW) == issued.session
    with get_db() as conn:
        launch_row = conn.execute("SELECT token_digest, consumed_at FROM miniapp_launch_tokens").fetchone()
        session_row = conn.execute("SELECT token_digest FROM miniapp_sessions").fetchone()
    assert launch_row[0] != launch and session_row[0] != issued.token
    assert len(launch_row[0]) == len(session_row[0]) == 64
    assert launch_row[1] == NOW


def test_expiry_unknown_tampered_and_reopen_are_rejected(temp_database):
    expired_launch = create_launch_token(CHAT_A, 101, now=NOW)
    assert exchange_launch_token(expired_launch, 101, now=NOW + LAUNCH_TTL_SECONDS) is None
    fresh_launch = create_launch_token(CHAT_A, 101, now=NOW)
    issued = exchange_launch_token(fresh_launch, 101, now=NOW)
    assert get_miniapp_session(issued.token, now=NOW + 1) == issued.session
    assert get_miniapp_session(issued.token, now=NOW + SESSION_TTL_SECONDS) is None
    assert get_miniapp_session("unknown" * 7, now=NOW) is None
    assert get_miniapp_session(issued.token[:-1] + ("A" if issued.token[-1] != "A" else "B"),
                               now=NOW) is None


def test_same_user_has_independent_chat_worlds(temp_database):
    token_a = create_launch_token(CHAT_A, 101, now=NOW)
    token_b = create_launch_token(CHAT_B, 101, now=NOW)
    session_a = exchange_launch_token(token_a, 101, now=NOW).session
    session_b = exchange_launch_token(token_b, 101, now=NOW).session
    assert (session_a.chat_id, session_b.chat_id) == (CHAT_A, CHAT_B)
    assert session_a.user_id == session_b.user_id == 101


def test_concurrent_double_consumption_creates_one_session(temp_database):
    launch = create_launch_token(CHAT_A, 101, now=NOW)
    barrier = Barrier(2)

    def consume(_):
        barrier.wait()
        return exchange_launch_token(launch, 101, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, range(2)))
    assert sum(item is not None for item in results) == 1
    with get_db() as conn:
        assert conn.execute("SELECT count(*) FROM miniapp_sessions").fetchone()[0] == 1


def test_binding_is_per_token_and_requires_original_chat_user_and_unconsumed_token(temp_database):
    first = create_launch_token(CHAT_A, 101, now=NOW)
    second = create_launch_token(CHAT_A, 101, now=NOW)
    assert not bind_launch_message_id(second, CHAT_B, 101, 202, now=NOW)
    assert not bind_launch_message_id(second, CHAT_A, 202, 202, now=NOW)
    assert not bind_launch_message_id(second, CHAT_A, 101, 0, now=NOW)
    assert bind_launch_message_id(first, CHAT_A, 101, 101, now=NOW)
    assert bind_launch_message_id(second, CHAT_A, 101, 202, now=NOW)
    assert not bind_launch_message_id(second, CHAT_A, 101, 999, now=NOW)
    issued = exchange_launch_token(second, 101, now=NOW)
    assert issued.launch_message_id == 202
    assert not bind_launch_message_id(second, CHAT_A, 101, 999, now=NOW)
    assert exchange_launch_token(first, 101, now=NOW).launch_message_id == 101


def test_launch_message_migration_preserves_existing_tokens_and_is_idempotent(
    tmp_path, monkeypatch,
):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE miniapp_launch_tokens (
                token_digest TEXT PRIMARY KEY, chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL, consumed_at INTEGER,
                CHECK (expires_at > created_at)
            )""",
        )
        conn.execute(
            "INSERT INTO miniapp_launch_tokens VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-digest", CHAT_A, 101, NOW, NOW + 120, None),
        )
    monkeypatch.setattr(database, "DB_NAME", str(path))
    database.init_db()
    database.init_db()
    with get_db() as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(miniapp_launch_tokens)")]
        row = conn.execute(
            "SELECT token_digest, chat_id, user_id, launch_message_id "
            "FROM miniapp_launch_tokens",
        ).fetchone()
    assert columns.count("launch_message_id") == 1
    assert row == ("legacy-digest", CHAT_A, 101, None)
