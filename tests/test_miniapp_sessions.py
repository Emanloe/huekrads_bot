"""One-use group launch credentials and durable chat-scoped sessions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from database import get_db
from miniapp_sessions import (
    LAUNCH_TTL_SECONDS, SESSION_TTL_SECONDS, create_launch_token,
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
