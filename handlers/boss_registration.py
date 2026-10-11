"""SQLite queue for the next boss battle in each chat."""

import logging
import random
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DUEL_TIMEZONE
from database import is_deleted_user_in_transaction
from handlers.boss_catalog import choose_boss


_BOSS_REG_DB_PATH = Path(__file__).resolve().parent.parent / "bot_database.db"
_NEXT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS boss_next_registrations (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT NOT NULL,
    last_name TEXT,
    PRIMARY KEY (chat_id, user_id)
)
"""
_NEXT_BOSS_SQL = """
CREATE TABLE IF NOT EXISTS boss_next_battles (
    chat_id INTEGER PRIMARY KEY,
    boss_id TEXT NOT NULL,
    boss_name TEXT NOT NULL,
    boss_emoji TEXT NOT NULL,
    boss_description TEXT,
    max_hp INTEGER NOT NULL CHECK (max_hp > 0)
)
"""


def _boss_registration_is_open():
    """The next battle can be booked at any time, including during a battle."""
    return True


def _legacy_registration_today():
    """Use the same Moscow date that the old registration code stored and read."""
    try:
        timezone = ZoneInfo(DUEL_TIMEZONE)
    except Exception:
        logging.exception("Could not load DUEL_TIMEZONE=%r for boss migration", DUEL_TIMEZONE)
        timezone = ZoneInfo("UTC")
    return datetime.now(timezone).date().isoformat()


@contextmanager
def _boss_registration_connect():
    conn = sqlite3.connect(str(_BOSS_REG_DB_PATH), timeout=10)
    try:
        try:
            # Serialize migration with registration and consumption.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(_NEXT_TABLE_SQL)
            conn.execute(_NEXT_BOSS_SQL)
            conn.execute("""CREATE TABLE IF NOT EXISTS deleted_users (
                user_id INTEGER PRIMARY KEY, username TEXT, deleted_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS boss_registration_meta (
                key TEXT PRIMARY KEY
            )""")
            migrated = conn.execute(
                "SELECT 1 FROM boss_registration_meta WHERE key = 'next_queue_v1'"
            ).fetchone()
            if not migrated:
                legacy = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'boss_registrations'"
                ).fetchone()
                if legacy:
                    # Old code only read today's Moscow registrations. Skipped
                    # starts left older rows behind, but never read them again.
                    conn.execute("""
                        INSERT OR IGNORE INTO boss_next_registrations
                            (chat_id, user_id, username, first_name, last_name)
                        SELECT chat_id, user_id, username, first_name, last_name
                        FROM boss_registrations AS r WHERE reg_date = ?
                          AND NOT EXISTS (SELECT 1 FROM deleted_users AS d WHERE d.user_id = r.user_id)
                        ORDER BY rowid
                    """, (_legacy_registration_today(),))
                conn.execute(
                    "INSERT INTO boss_registration_meta (key) VALUES ('next_queue_v1')"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        with conn:
            yield conn
    finally:
        conn.close()


def _prepared_boss_from_row(row) -> dict:
    return {
        "id": row[0], "name": row[1], "emoji": row[2],
        "description": row[3], "max_hp": row[4],
    }


def _prepare_next_boss_in_transaction(conn, chat_id: int, selector) -> dict:
    row = conn.execute(
        """SELECT boss_id, boss_name, boss_emoji, boss_description, max_hp
           FROM boss_next_battles WHERE chat_id = ?""", (chat_id,),
    ).fetchone()
    if row is not None:
        return _prepared_boss_from_row(row)
    boss = selector()
    conn.execute(
        """INSERT INTO boss_next_battles
           (chat_id, boss_id, boss_name, boss_emoji, boss_description, max_hp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (chat_id, boss["id"], boss["name"], boss["emoji"],
         boss.get("description"), boss["max_hp"]),
    )
    return boss


def _boss_prepare_next_boss(chat_id: int, selector=None) -> dict:
    """Persist this chat's next boss once, at first signup or battle start."""
    if selector is None:
        selector = lambda: choose_boss(random)
    with _boss_registration_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _prepare_next_boss_in_transaction(conn, chat_id, selector)


def _boss_register_user(chat_id, tg_user):
    """Register once for this chat's next actual battle."""
    with _boss_registration_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if is_deleted_user_in_transaction(conn.cursor(), tg_user.id):
            return False
        _prepare_next_boss_in_transaction(conn, chat_id, lambda: choose_boss(random))
        cursor = conn.execute(
            """INSERT OR IGNORE INTO boss_next_registrations
               (chat_id, user_id, username, first_name, last_name)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, tg_user.id, tg_user.username, tg_user.first_name or "", tg_user.last_name),
        )
        return cursor.rowcount > 0


def _boss_get_registered_users(chat_id):
    with _boss_registration_connect() as conn:
        return conn.execute(
            """SELECT user_id, username, first_name, last_name
               FROM boss_next_registrations AS r WHERE chat_id = ?
                 AND NOT EXISTS (SELECT 1 FROM deleted_users AS d WHERE d.user_id = r.user_id)
               ORDER BY rowid""",
            (chat_id,),
        ).fetchall()


def _boss_consume_registrations(chat_id):
    """Atomically take the next-battle queue at the start boundary."""
    with _boss_registration_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT user_id, username, first_name, last_name
               FROM boss_next_registrations AS r WHERE chat_id = ?
                 AND NOT EXISTS (SELECT 1 FROM deleted_users AS d WHERE d.user_id = r.user_id)
               ORDER BY rowid""",
            (chat_id,),
        ).fetchall()
        conn.execute("DELETE FROM boss_next_registrations WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM boss_next_battles WHERE chat_id = ?", (chat_id,))
        return rows


def _boss_registration_snapshot(chat_id: int, viewer_user_id: int) -> tuple[int, bool]:
    """Read the next queue without creating tables or writing SQLite."""
    if not _BOSS_REG_DB_PATH.exists():
        return 0, False
    with closing(sqlite3.connect(
        f"{_BOSS_REG_DB_PATH.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=10,
    )) as conn:
        next_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'boss_next_registrations'"
        ).fetchone()
        if next_table:
            table = "boss_next_registrations"
            date_filter = ""
            params = (viewer_user_id, chat_id)
        elif conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'boss_registrations'"
        ).fetchone():
            table = "boss_registrations"
            date_filter = " AND reg_date = ?"
            params = (viewer_user_id, chat_id, _legacy_registration_today())
        else:
            return 0, False
        has_deleted_users = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'deleted_users'"
        ).fetchone()
        deleted_filter = (
            " AND NOT EXISTS (SELECT 1 FROM deleted_users AS d WHERE d.user_id = r.user_id)"
            if has_deleted_users else ""
        )
        row = conn.execute(
            f"""SELECT COUNT(DISTINCT user_id),
                       COALESCE(MAX(CASE WHEN user_id = ? THEN 1 ELSE 0 END), 0)
                FROM {table} AS r WHERE chat_id = ?{date_filter}{deleted_filter}""",
            params,
        ).fetchone()
    return int(row[0]), bool(row[1])


def _boss_get_registered_chat_ids():
    with _boss_registration_connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT chat_id FROM boss_next_registrations AS r
               WHERE NOT EXISTS (SELECT 1 FROM deleted_users AS d WHERE d.user_id = r.user_id)"""
        ).fetchall()
    return {row[0] for row in rows}
