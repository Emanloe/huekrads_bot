"""Chat-scoped, restart-safe Mini App launch and bearer sessions."""

import hashlib
import re
import secrets
import time
from dataclasses import dataclass

from database import get_db


LAUNCH_TTL_SECONDS = 120
SESSION_TTL_SECONDS = 3600
_OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


@dataclass(frozen=True)
class MiniAppSession:
    chat_id: int
    user_id: int
    created_at: int
    expires_at: int


@dataclass(frozen=True)
class IssuedMiniAppSession:
    token: str
    session: MiniAppSession


def _digest(token: str) -> str | None:
    if not isinstance(token, str) or not _OPAQUE_TOKEN.fullmatch(token):
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_launch_token(chat_id: int, user_id: int, *, now: int | None = None) -> str:
    """Only Telegram group handlers should pass their trusted update IDs here."""
    if type(chat_id) is not int or type(user_id) is not int or chat_id >= 0 or user_id <= 0:
        raise ValueError("Group chat and Telegram user IDs are required")
    issued_at = int(time.time()) if now is None else now
    token = secrets.token_urlsafe(32)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO miniapp_launch_tokens
               (token_digest, chat_id, user_id, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (_digest(token), chat_id, user_id, issued_at, issued_at + LAUNCH_TTL_SECONDS),
        )
    return token


def exchange_launch_token(token: str, verified_user_id: int, *,
                          now: int | None = None) -> IssuedMiniAppSession | None:
    """Consume once and insert the session in the same SQLite write transaction."""
    digest = _digest(token)
    if digest is None or type(verified_user_id) is not int or verified_user_id <= 0:
        return None
    timestamp = int(time.time()) if now is None else now
    session_token = secrets.token_urlsafe(32)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        row = cursor.execute(
            """SELECT chat_id, user_id FROM miniapp_launch_tokens
               WHERE token_digest = ? AND consumed_at IS NULL AND expires_at > ?""",
            (digest, timestamp),
        ).fetchone()
        if row is None or row[1] != verified_user_id:
            return None
        cursor.execute(
            """UPDATE miniapp_launch_tokens SET consumed_at = ?
               WHERE token_digest = ? AND consumed_at IS NULL AND expires_at > ?""",
            (timestamp, digest, timestamp),
        )
        if cursor.rowcount != 1:
            return None
        session = MiniAppSession(row[0], row[1], timestamp, timestamp + SESSION_TTL_SECONDS)
        cursor.execute(
            """INSERT INTO miniapp_sessions
               (token_digest, chat_id, user_id, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (_digest(session_token), session.chat_id, session.user_id,
             session.created_at, session.expires_at),
        )
        return IssuedMiniAppSession(session_token, session)


def get_miniapp_session(token: str, *, now: int | None = None) -> MiniAppSession | None:
    digest = _digest(token)
    if digest is None:
        return None
    timestamp = int(time.time()) if now is None else now
    with get_db() as conn:
        row = conn.execute(
            """SELECT chat_id, user_id, created_at, expires_at
               FROM miniapp_sessions WHERE token_digest = ? AND expires_at > ?""",
            (digest, timestamp),
        ).fetchone()
        return MiniAppSession(*row) if row else None
