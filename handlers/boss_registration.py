"""SQLite-backed registration for the daily boss battle."""

import logging
import sqlite3
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DUEL_TIMEZONE


BOSS_REG_CUTOFF_HOUR = 13
BOSS_REG_CUTOFF_MINUTE = 37
_BOSS_REG_DB_PATH = Path(__file__).resolve().parent.parent / "bot_database.db"
_BOSS_REG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS boss_registrations (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT NOT NULL,
    last_name TEXT,
    reg_date TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id, reg_date)
)
"""


def _boss_reg_timezone():
    try:
        return ZoneInfo(DUEL_TIMEZONE)
    except Exception:
        logging.exception("Не удалось загрузить DUEL_TIMEZONE=%r для регистрации босса", DUEL_TIMEZONE)
        return ZoneInfo("UTC")


def _boss_today():
    return datetime.now(_boss_reg_timezone()).date().isoformat()


def _boss_registration_is_open():
    now = datetime.now(_boss_reg_timezone())
    cutoff = dt_time(BOSS_REG_CUTOFF_HOUR, BOSS_REG_CUTOFF_MINUTE)
    return now.time() < cutoff


def _boss_registration_connect():
    conn = sqlite3.connect(str(_BOSS_REG_DB_PATH), timeout=10)
    conn.execute(_BOSS_REG_TABLE_SQL)
    conn.commit()
    return conn


def _boss_register_user(chat_id, tg_user):
    reg_date = _boss_today()
    with _boss_registration_connect() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO boss_registrations
            (chat_id, user_id, username, first_name, last_name, reg_date)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, tg_user.id, tg_user.username, tg_user.first_name or "", tg_user.last_name, reg_date),
        )
        return cursor.rowcount > 0


def _boss_get_registered_users(chat_id):
    reg_date = _boss_today()
    with _boss_registration_connect() as conn:
        return conn.execute(
            """
            SELECT user_id, username, first_name, last_name
            FROM boss_registrations
            WHERE chat_id = ? AND reg_date = ?
            ORDER BY rowid
            """,
            (chat_id, reg_date),
        ).fetchall()


def _boss_registration_snapshot(chat_id: int, viewer_user_id: int) -> tuple[int, bool]:
    """Read today's registration without creating a table or writing SQLite."""
    if not _BOSS_REG_DB_PATH.exists():
        return 0, False
    try:
        with sqlite3.connect(f"{_BOSS_REG_DB_PATH.resolve().as_uri()}?mode=ro",
                             uri=True, timeout=10) as conn:
            row = conn.execute(
                """SELECT COUNT(*), COALESCE(MAX(CASE WHEN user_id = ? THEN 1 ELSE 0 END), 0)
                   FROM boss_registrations WHERE chat_id = ? AND reg_date = ?""",
                (viewer_user_id, chat_id, _boss_today()),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table: boss_registrations" in str(exc):
            return 0, False
        raise
    return int(row[0]), bool(row[1])


def _boss_clear_registrations(chat_id, reg_date=None):
    reg_date = reg_date or _boss_today()
    with _boss_registration_connect() as conn:
        conn.execute(
            "DELETE FROM boss_registrations WHERE chat_id = ? AND reg_date = ?",
            (chat_id, reg_date),
        )
        conn.commit()


def _boss_get_registered_chat_ids(reg_date=None):
    reg_date = reg_date or _boss_today()
    with _boss_registration_connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM boss_registrations WHERE reg_date = ?",
            (reg_date,),
        ).fetchall()
    return {row[0] for row in rows}
