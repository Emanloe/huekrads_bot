import random
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from html import escape
import pytz
from config import DUEL_TIMEZONE, DICK_STEAL_CHANCE, DICK_STEAL_CHANCE_PER_WIN
from text_resources import get_text

DB_NAME = "bot_database.db"
BIRTHDAY_COOLDOWN = timedelta(days=365)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_NAME)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _get_today_date_str() -> str:
    tz = pytz.timezone(DUEL_TIMEZONE)
    return datetime.now(tz).strftime("%Y-%m-%d")


def _clean_username(username: str | None) -> str | None:
    """Удаляет @ из юзернейма и прибирает пробелы."""
    if not username:
        return None
    cleaned = username.strip().lstrip("@")
    return cleaned if cleaned else None


def format_user_title_plain(user_data: dict, *, include_dwarf_name: bool = True) -> str:
    """Participant title for plain text, including Telegram buttons."""
    username = _clean_username(user_data.get('username'))
    title = username or user_data.get('display_name') or get_text("common.user.default_title")
    dwarf_name = user_data.get('dwarf_name') if include_dwarf_name else None
    return f"{dwarf_name} ({title})" if dwarf_name else title


def format_user_title(user_data: dict) -> str:
    """Participant title safe to insert into Telegram HTML messages."""
    return escape(format_user_title_plain(user_data))


def get_dick_steal_percent(daily_wins: int) -> int:
    """Шанс кражи хуя в процентах: база +1% за каждую победу за сегодня."""
    wins = max(0, int(daily_wins or 0))
    base_percent = int(round(DICK_STEAL_CHANCE * 100))
    step_percent = int(round(DICK_STEAL_CHANCE_PER_WIN * 100))
    return min(100, max(0, base_percent + wins * step_percent))


def get_dick_steal_chance(daily_wins: int) -> float:
    return get_dick_steal_percent(daily_wins) / 100.0


def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER,
                chat_id INTEGER,
                username TEXT,
                first_name TEXT,
                beauty_count INTEGER DEFAULT 0,
                is_bot INTEGER DEFAULT 0,
                birthdate TEXT,
                birthday_changed_at TEXT,
                PRIMARY KEY (user_id, chat_id)
            )
        """)

        cursor.execute("PRAGMA table_info(users)")
        user_columns = {column[1] for column in cursor.fetchall()}
        if "birthday_changed_at" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN birthday_changed_at TEXT")
            cursor.execute(
                """UPDATE users SET birthday_changed_at = ?
                   WHERE birthdate IS NOT NULL AND TRIM(birthdate) <> ''""",
                (_utc_now().isoformat(timespec="microseconds"),),
            )

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pizda_candidates (
                chat_id INTEGER,
                message_id INTEGER,
                created_at INTEGER,
                used INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, message_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                forward_reply_enabled INTEGER DEFAULT 1,
                auto_delete_enabled INTEGER DEFAULT 1,
                boss_enabled INTEGER DEFAULT 1
            )
        """)

        # Миграция существующей БД: добавляем настройку боссов,
        # если таблица была создана в старой версии бота.
        cursor.execute("PRAGMA table_info(chat_settings)")
        chat_settings_cols = [col[1] for col in cursor.fetchall()]
        if "boss_enabled" not in chat_settings_cols:
            cursor.execute(
                "ALTER TABLE chat_settings ADD COLUMN boss_enabled INTEGER DEFAULT 1"
            )

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='duel_users'")
        table_exists = cursor.fetchone()

        if table_exists:
            cursor.execute("PRAGMA table_info(duel_users)")
            cols = [col[1] for col in cursor.fetchall()]
            
            if "chat_id" not in cols:
                cursor.execute("ALTER TABLE duel_users RENAME TO duel_users_old")
                cursor.execute("""
                    CREATE TABLE duel_users (
                        user_id INTEGER,
                        chat_id INTEGER DEFAULT 0,
                        username TEXT,
                        display_name TEXT NOT NULL,
                        points INTEGER DEFAULT 20,
                        wins INTEGER DEFAULT 0,
                        losses INTEGER DEFAULT 0,
                        stolen_dicks_count INTEGER DEFAULT 0,
                        dick_stolen_count INTEGER DEFAULT 0,
                        dick_stolen_today INTEGER DEFAULT 0,
                        last_activity_date TEXT,
                        last_stolen_by TEXT DEFAULT NULL,
                        daily_wins INTEGER DEFAULT 0,
                        PRIMARY KEY (user_id, chat_id)
                    )
                """)
                cursor.execute("""
                    INSERT OR IGNORE INTO duel_users (
                        user_id, chat_id, username, display_name, points, wins, losses,
                        stolen_dicks_count, dick_stolen_count, dick_stolen_today,
                        last_activity_date, last_stolen_by
                    )
                    SELECT 
                        user_id, 0, username, display_name, points, wins, losses,
                        stolen_dicks_count, dick_stolen_count, dick_stolen_today,
                        last_activity_date, last_stolen_by
                    FROM duel_users_old
                """)
                cursor.execute("DROP TABLE duel_users_old")
        else:
            cursor.execute("""
                CREATE TABLE duel_users (
                    user_id INTEGER,
                    chat_id INTEGER DEFAULT 0,
                    username TEXT,
                    display_name TEXT NOT NULL,
                    points INTEGER DEFAULT 20,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    stolen_dicks_count INTEGER DEFAULT 0,
                    dick_stolen_count INTEGER DEFAULT 0,
                    dick_stolen_today INTEGER DEFAULT 0,
                    last_activity_date TEXT,
                    last_stolen_by TEXT DEFAULT NULL,
                    daily_wins INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, chat_id)
                )
            """)

        cursor.execute("PRAGMA table_info(duel_users)")
        cols = [col[1] for col in cursor.fetchall()]
        if "daily_wins" not in cols:
            cursor.execute(
                "ALTER TABLE duel_users ADD COLUMN daily_wins INTEGER DEFAULT 0"
            )
        
        cursor.execute("PRAGMA table_info(duel_users)")
        cols = [col[1] for col in cursor.fetchall()]
        if "bosses_defeated" not in cols:
            cursor.execute(
                "ALTER TABLE duel_users ADD COLUMN bosses_defeated INTEGER DEFAULT 0"
            )

        if "dwarf_name" not in cols:
            cursor.execute("ALTER TABLE duel_users ADD COLUMN dwarf_name TEXT")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS duel_inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_duel_inventory_owner
            ON duel_inventory (chat_id, user_id, id)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS duel_item_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                claimed INTEGER NOT NULL DEFAULT 0,
                claimed_by INTEGER,
                item_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claimed_at TEXT
            )
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_duel_item_events_active_chat
            ON duel_item_events (chat_id)
            WHERE claimed = 0
        """)

        # Fix broken initial data where points=0 and losses=20 from prior seed bug
        cursor.execute("""
            UPDATE duel_users 
            SET points = 20, losses = 0 
            WHERE points = 0 AND losses = 20 AND wins = 0
        """)

        # Seed duel_users correctly from users table
        today_str = _get_today_date_str()
        cursor.execute("""
            INSERT OR IGNORE INTO duel_users (
                user_id, chat_id, username, display_name, points, wins, losses,
                stolen_dicks_count, dick_stolen_count, dick_stolen_today,
                last_activity_date, last_stolen_by
            )
            SELECT 
                user_id, 
                chat_id, 
                REPLACE(username, '@', ''), 
                COALESCE(REPLACE(username, '@', ''), first_name, 'Гном'), 
                20, 0, 0, 0, 0, 0, ?, NULL
            FROM users 
            WHERE is_bot = 0
        """, (today_str,))


# ==========================================
# ⚙️ НАСТРОЙКИ ЧАТОВ
# ==========================================

def is_forward_reply_enabled(chat_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT forward_reply_enabled FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return bool(row[0]) if row and row[0] is not None else True


def set_forward_reply_enabled(chat_id: int, enabled: bool):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO chat_settings (chat_id, forward_reply_enabled)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET forward_reply_enabled = excluded.forward_reply_enabled
        """, (chat_id, 1 if enabled else 0))


def is_auto_delete_enabled(chat_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT auto_delete_enabled FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return bool(row[0]) if row and row[0] is not None else True


def set_auto_delete_enabled(chat_id: int, enabled: bool):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO chat_settings (chat_id, auto_delete_enabled)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET auto_delete_enabled = excluded.auto_delete_enabled
        """, (chat_id, 1 if enabled else 0))


def is_boss_enabled(chat_id: int) -> bool:
    """Возвращает, разрешена ли ежедневная битва с боссом в чате."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT boss_enabled FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = cursor.fetchone()

        # Для старых/ещё не зарегистрированных чатов значение по умолчанию —
        # босс включён.
        return bool(row[0]) if row and row[0] is not None else True


def set_boss_enabled(chat_id: int, enabled: bool):
    """Включает или выключает ежедневный запуск босса в чате."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO chat_settings (chat_id, boss_enabled)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                boss_enabled = excluded.boss_enabled
        """, (chat_id, 1 if enabled else 0))


# ==========================================
# 🗡️ ЛОГИКА ДУЭЛЕЙ
# ==========================================

def _reset_user_if_new_day(cursor, row) -> dict | None:
    if not row:
        return None

    today_str = _get_today_date_str()
    (
        user_id, chat_id, username, display_name, points, wins, losses,
        stolen_dicks_count, dick_stolen_count, dick_stolen_today,
        last_activity_date, last_stolen_by, daily_wins, dwarf_name
    ) = row

    if last_activity_date != today_str:
        points = 20
        dick_stolen_today = 0
        last_stolen_by = None
        daily_wins = 0
        last_activity_date = today_str
        cursor.execute("""
            UPDATE duel_users
            SET points = 20, dick_stolen_today = 0, last_stolen_by = NULL,
                daily_wins = 0, last_activity_date = ?
            WHERE user_id = ? AND chat_id = ?
        """, (today_str, user_id, chat_id))

    return {
        "user_id": user_id,
        "chat_id": chat_id,
        "username": _clean_username(username),
        "display_name": _clean_username(display_name) or display_name,
        "points": points,
        "wins": wins,
        "losses": losses,
        "stolen_dicks_count": stolen_dicks_count,
        "dick_stolen_count": dick_stolen_count,
        "dick_stolen_today": bool(dick_stolen_today),
        "last_activity_date": last_activity_date,
        "last_stolen_by": last_stolen_by,
        "daily_wins": daily_wins or 0,
        "dwarf_name": dwarf_name,
    }


def get_or_create_duel_user(tg_user, chat_id: int) -> dict:
    username = _clean_username(tg_user.username)
    display_name = username or tg_user.first_name or "Гном"
    today_str = _get_today_date_str()

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT user_id, chat_id, username, display_name, points, wins, losses,
                   stolen_dicks_count, dick_stolen_count, dick_stolen_today,
                   last_activity_date, last_stolen_by, daily_wins, dwarf_name
            FROM duel_users WHERE user_id = ? AND chat_id = ?
        """, (tg_user.id, chat_id))
        row = cursor.fetchone()

        if not row:
            cursor.execute("""
                INSERT INTO duel_users (
                    user_id, chat_id, username, display_name, points, wins, losses,
                    stolen_dicks_count, dick_stolen_count, dick_stolen_today,
                    last_activity_date, last_stolen_by
                )
                VALUES (?, ?, ?, ?, 20, 0, 0, 0, 0, 0, ?, NULL)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET
                    username = excluded.username,
                    display_name = excluded.display_name
            """, (tg_user.id, chat_id, username, display_name, today_str))

            cursor.execute("""
                SELECT user_id, chat_id, username, display_name, points, wins, losses,
                       stolen_dicks_count, dick_stolen_count, dick_stolen_today,
                       last_activity_date, last_stolen_by, daily_wins, dwarf_name
                FROM duel_users WHERE user_id = ? AND chat_id = ?
            """, (tg_user.id, chat_id))
            row = cursor.fetchone()
        else:
            if row[2] != username or row[3] != display_name:
                cursor.execute("""
                    UPDATE duel_users SET username = ?, display_name = ? WHERE user_id = ? AND chat_id = ?
                """, (username, display_name, tg_user.id, chat_id))

        return _reset_user_if_new_day(cursor, row)


def get_duel_user_by_username(username: str, chat_id: int) -> dict | None:
    clean_search = _clean_username(username)
    if not clean_search:
        return None

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT user_id, chat_id, username, display_name, points, wins, losses,
                   stolen_dicks_count, dick_stolen_count, dick_stolen_today,
                   last_activity_date, last_stolen_by, daily_wins, dwarf_name
            FROM duel_users 
            WHERE chat_id = ? AND (LOWER(username) = LOWER(?) OR LOWER(display_name) = LOWER(?))
        """, (chat_id, clean_search, clean_search))
        row = cursor.fetchone()

        if row:
            return _reset_user_if_new_day(cursor, row)

        cursor.execute("""
            SELECT user_id, username, first_name 
            FROM users 
            WHERE chat_id = ? AND (LOWER(username) = LOWER(?) OR LOWER(first_name) = LOWER(?))
        """, (chat_id, clean_search, clean_search))
        user_row = cursor.fetchone()

        if not user_row:
            return None

        u_id, u_name, f_name = user_row
        today_str = _get_today_date_str()
        usr_name = _clean_username(u_name)
        disp_name = usr_name or f_name or "Гном"

        cursor.execute("""
            INSERT INTO duel_users (
                user_id, chat_id, username, display_name, points, wins, losses,
                stolen_dicks_count, dick_stolen_count, dick_stolen_today,
                last_activity_date, last_stolen_by
            )
            VALUES (?, ?, ?, ?, 20, 0, 0, 0, 0, 0, ?, NULL)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name
        """, (u_id, chat_id, usr_name, disp_name, today_str))

        cursor.execute("""
            SELECT user_id, chat_id, username, display_name, points, wins, losses,
                   stolen_dicks_count, dick_stolen_count, dick_stolen_today,
                   last_activity_date, last_stolen_by, daily_wins, dwarf_name
            FROM duel_users WHERE user_id = ? AND chat_id = ?
        """, (u_id, chat_id))
        new_row = cursor.fetchone()

        return _reset_user_if_new_day(cursor, new_row)


def get_duel_dwarf_name(chat_id: int, user_id: int) -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT dwarf_name FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        return row[0] if row else None


def is_duel_user_registered(chat_id: int, user_id: int) -> bool:
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone() is not None


def set_duel_dwarf_name_once(chat_id: int, user_id: int, dwarf_name: str) -> tuple[str, str | None]:
    """Atomically set a registered dwarf's name; return status and stored name."""
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE duel_users SET dwarf_name = ?
               WHERE chat_id = ? AND user_id = ? AND dwarf_name IS NULL""",
            (dwarf_name, chat_id, user_id),
        )
        if cursor.rowcount == 1:
            return "set", dwarf_name
        row = conn.execute(
            "SELECT dwarf_name FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        return ("already_named", row[0]) if row else ("not_registered", None)


def delete_duel_user_by_username(username: str, chat_id: int) -> bool:
    clean_search = _clean_username(username)
    if not clean_search:
        return False
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM duel_users 
            WHERE chat_id = ? AND (LOWER(username) = LOWER(?) OR LOWER(display_name) = LOWER(?))
        """, (chat_id, clean_search, clean_search))
        return cursor.rowcount > 0


def apply_duel_result_plan(
    chat_id: int,
    result_plan: dict,
) -> tuple[int, int]:
    with get_db() as conn:
        cursor = conn.cursor()
        winner = result_plan["winner"]
        loser = result_plan["loser"]

        if result_plan["is_dick_stolen"]:
            cursor.execute("""
                UPDATE duel_users
                SET points = ?, wins = wins + ?, daily_wins = daily_wins + ?,
                    stolen_dicks_count = stolen_dicks_count + ?
                WHERE user_id = ? AND chat_id = ?
            """, (
                winner["points"],
                winner["wins_increment"],
                winner["daily_wins_increment"],
                winner["stolen_dicks_count_increment"],
                winner["user_id"],
                chat_id,
            ))

            cursor.execute("""
                UPDATE duel_users
                SET points = ?, losses = losses + ?,
                    dick_stolen_count = dick_stolen_count + ?,
                    dick_stolen_today = ?, last_stolen_by = ?
                WHERE user_id = ? AND chat_id = ?
            """, (
                loser["points"],
                loser["losses_increment"],
                loser["dick_stolen_count_increment"],
                loser["dick_stolen_today"],
                loser["last_stolen_by"],
                loser["user_id"],
                chat_id,
            ))
        else:
            cursor.execute("""
                UPDATE duel_users
                SET points = ?, wins = wins + ?, daily_wins = daily_wins + ?
                WHERE user_id = ? AND chat_id = ?
            """, (
                winner["points"],
                winner["wins_increment"],
                winner["daily_wins_increment"],
                winner["user_id"],
                chat_id,
            ))

            cursor.execute("""
                UPDATE duel_users
                SET points = ?, losses = losses + ?
                WHERE user_id = ? AND chat_id = ?
            """, (
                loser["points"],
                loser["losses_increment"],
                loser["user_id"],
                chat_id,
            ))

        return winner["points"], loser["points"]


def apply_duel_berserk(
    chat_id: int,
    berserker_user_id: int,
    victim_user_id: int,
    berserker_title: str,
) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE duel_users
            SET dick_stolen_count = dick_stolen_count + 1,
                dick_stolen_today = 1, last_stolen_by = ?
            WHERE user_id = ? AND chat_id = ? AND dick_stolen_today = 0
            """,
            (berserker_title, victim_user_id, chat_id),
        )
        if cursor.rowcount == 0:
            return False

        cursor.execute(
            """
            UPDATE duel_users
            SET stolen_dicks_count = stolen_dicks_count + 1
            WHERE user_id = ? AND chat_id = ?
            """,
            (berserker_user_id, chat_id),
        )
        return True


# ==========================================
# 🎒 ГНОМИЙ ИНВЕНТАРЬ И ITEM EVENTS
# ==========================================

def add_duel_inventory_item(chat_id: int, user_id: int, item_id: str) -> dict:
    """Добавляет один отдельный экземпляр предмета."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO duel_inventory (chat_id, user_id, item_id)
            VALUES (?, ?, ?)
            """,
            (chat_id, user_id, item_id),
        )
        return {
            "id": cursor.lastrowid,
            "chat_id": chat_id,
            "user_id": user_id,
            "item_id": item_id,
        }


def get_duel_inventory(chat_id: int, user_id: int) -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, chat_id, user_id, item_id, created_at
            FROM duel_inventory
            WHERE chat_id = ? AND user_id = ?
            ORDER BY id
            """,
            (chat_id, user_id),
        )
        return [
            {
                "id": row[0],
                "chat_id": row[1],
                "user_id": row[2],
                "item_id": row[3],
                "created_at": row[4],
            }
            for row in cursor.fetchall()
        ]


def remove_duel_inventory_instance(
    chat_id: int,
    user_id: int,
    instance_id: int,
) -> bool:
    """Удаляет только указанный instance, не все дубликаты item_id."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM duel_inventory
            WHERE id = ? AND chat_id = ? AND user_id = ?
            """,
            (instance_id, chat_id, user_id),
        )
        return cursor.rowcount == 1


def transfer_duel_inventory_item(
    chat_id: int,
    from_user_id: int,
    to_user_id: int,
    inventory_instance_id: int,
) -> bool:
    """Атомарно передаёт один конкретный inventory instance."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """
            SELECT item_id
            FROM duel_inventory
            WHERE id = ? AND chat_id = ? AND user_id = ?
            """,
            (inventory_instance_id, chat_id, from_user_id),
        )
        row = cursor.fetchone()
        if not row:
            return False

        item_id = row[0]
        cursor.execute(
            """
            DELETE FROM duel_inventory
            WHERE id = ? AND chat_id = ? AND user_id = ?
            """,
            (inventory_instance_id, chat_id, from_user_id),
        )
        if cursor.rowcount != 1:
            return False

        cursor.execute(
            """
            INSERT INTO duel_inventory (chat_id, user_id, item_id)
            VALUES (?, ?, ?)
            """,
            (chat_id, to_user_id, item_id),
        )
        return True


def get_duel_item_event_chat_ids() -> list[int]:
    """Возвращает групповые чаты с хотя бы одним duel-user."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT chat_id FROM duel_users WHERE chat_id < 0 ORDER BY chat_id"
        )
        return [row[0] for row in cursor.fetchall()]


def create_duel_item_event(chat_id: int) -> int | None:
    """Создаёт event, если в чате нет другого unclaimed event."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO duel_item_events (chat_id) VALUES (?)",
            (chat_id,),
        )
        return cursor.lastrowid if cursor.rowcount == 1 else None


def set_duel_item_event_message(event_id: int, message_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE duel_item_events
            SET message_id = ?
            WHERE event_id = ? AND claimed = 0
            """,
            (message_id, event_id),
        )
        return cursor.rowcount == 1


def discard_unpublished_duel_item_event(event_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM duel_item_events
            WHERE event_id = ? AND claimed = 0 AND message_id IS NULL
            """,
            (event_id,),
        )
        return cursor.rowcount == 1


def get_duel_item_event(event_id: int) -> dict | None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT event_id, chat_id, message_id, claimed, claimed_by,
                   item_id, created_at, claimed_at
            FROM duel_item_events
            WHERE event_id = ?
            """,
            (event_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "event_id": row[0],
            "chat_id": row[1],
            "message_id": row[2],
            "claimed": bool(row[3]),
            "claimed_by": row[4],
            "item_id": row[5],
            "created_at": row[6],
            "claimed_at": row[7],
        }


def claim_duel_item_event(
    event_id: int,
    chat_id: int,
    user_id: int,
    item_selector,
) -> tuple[str, dict | None]:
    """
    Атомарно бронирует event и выдаёт один item instance.

    item_selector вызывается только после успешной атомарной брони;
    проигравший race не тратит item-choice RNG.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """
            SELECT claimed
            FROM duel_item_events
            WHERE event_id = ? AND chat_id = ?
            """,
            (event_id, chat_id),
        )
        event = cursor.fetchone()
        if not event or event[0]:
            return "already_claimed", None

        cursor.execute(
            "SELECT 1 FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        if not cursor.fetchone():
            return "not_registered", None

        cursor.execute(
            """
            UPDATE duel_item_events
            SET claimed = 1, claimed_by = ?, claimed_at = CURRENT_TIMESTAMP
            WHERE event_id = ? AND chat_id = ? AND claimed = 0
            """,
            (user_id, event_id, chat_id),
        )
        if cursor.rowcount != 1:
            return "already_claimed", None

        item_id = item_selector()
        cursor.execute(
            """
            INSERT INTO duel_inventory (chat_id, user_id, item_id)
            VALUES (?, ?, ?)
            """,
            (chat_id, user_id, item_id),
        )
        instance_id = cursor.lastrowid
        cursor.execute(
            "UPDATE duel_item_events SET item_id = ? WHERE event_id = ?",
            (item_id, event_id),
        )
        return "claimed", {
            "id": instance_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "item_id": item_id,
        }


def get_duel_top(
    chat_id: int, sort_by: str = "wins", limit: int = 10,
    include_dwarf_name: bool = False,
) -> list:
    valid_cols = {"wins": "wins", "points": "points"}
    sort_column = valid_cols.get(sort_by, "wins")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT username, display_name, wins, losses, points, dwarf_name
            FROM duel_users
            WHERE chat_id = ?
            ORDER BY {sort_column} DESC, wins DESC
            LIMIT ?
        """, (chat_id, limit))
        rows = cursor.fetchall()
        
        cleaned_rows = []
        for u, d, w, l, p, dwarf_name in rows:
            clean_u = _clean_username(u)
            clean_d = _clean_username(d) or d
            row = (clean_u, clean_d, w, l, p)
            cleaned_rows.append(row + (dwarf_name,) if include_dwarf_name else row)
            
        return cleaned_rows


# ==========================================
# 📌 ОСНОВНЫЕ ФУНКЦИИ БОТА
# ==========================================

def save_or_update_user(user, chat_id: int):
    if user.is_bot:
        return
    clean_username = _clean_username(user.username)
    display_name = clean_username or user.first_name or "Гном"
    today_str = _get_today_date_str()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (user_id, chat_id, username, first_name, beauty_count, is_bot)
            VALUES (?, ?, ?, ?, 0, 0)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (user.id, chat_id, clean_username, user.first_name))

        cursor.execute("""
            INSERT INTO duel_users (
                user_id, chat_id, username, display_name, points, wins, losses,
                stolen_dicks_count, dick_stolen_count, dick_stolen_today,
                last_activity_date, last_stolen_by
            )
            VALUES (?, ?, ?, ?, 20, 0, 0, 0, 0, 0, ?, NULL)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name
        """, (user.id, chat_id, clean_username, display_name, today_str))


def valid_birthdate(bday_str: str) -> bool:
    """Accept a real DD.MM or DD.MM.YYYY date; 29.02 is valid without a year."""
    if not isinstance(bday_str, str):
        return False
    match = re.fullmatch(r"([0-9]{2})\.([0-9]{2})(?:\.([0-9]{4}))?", bday_str)
    if not match:
        return False
    day, month, year = match.groups()
    try:
        date(int(year) if year else 2000, int(month), int(day))
    except ValueError:
        return False
    return True


def set_user_birthdate_with_cooldown(
    chat_id: int, user_id: int, bday_str: str,
) -> tuple[str, datetime | None]:
    """Serialize the cooldown check and birthday update for one chat user."""
    if not valid_birthdate(bday_str):
        return "invalid", None

    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT birthday_changed_at FROM users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        if row is None:
            return "not_found", None

        now = _utc_now()
        if row[0]:
            changed_at = datetime.fromisoformat(row[0])
            if changed_at.tzinfo is None:
                changed_at = changed_at.replace(tzinfo=timezone.utc)
            next_change = changed_at.astimezone(timezone.utc) + BIRTHDAY_COOLDOWN
            if now < next_change:
                return "cooldown", next_change

        conn.execute(
            """UPDATE users SET birthdate = ?, birthday_changed_at = ?
               WHERE chat_id = ? AND user_id = ?""",
            (bday_str, now.isoformat(timespec="microseconds"), chat_id, user_id),
        )
        return "success", None


def save_custom_birthdate(chat_id: int, username: str, bday_str: str) -> bool:
    """Compatibility wrapper for the historical username-based DB API."""
    clean_username = _clean_username(username)
    if not clean_username:
        return False
    with get_db() as conn:
        row = conn.execute(
            """SELECT user_id FROM users WHERE chat_id = ?
               AND (LOWER(username) = LOWER(?) OR LOWER(first_name) = LOWER(?))
               LIMIT 1""",
            (chat_id, clean_username, clean_username),
        ).fetchone()
    return bool(row and set_user_birthdate_with_cooldown(chat_id, row[0], bday_str)[0] == "success")


def get_user_birthdate_from_db(user_id: int, chat_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT birthdate FROM users WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        row = cursor.fetchone()
        return row[0] if row and row[0] else None


def pick_beauty_of_the_day(chat_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, first_name, beauty_count FROM users WHERE chat_id = ? AND is_bot = 0", (chat_id,))
        users = cursor.fetchall()
        if not users:
            return None

        winner = random.choice(users)
        user_id, username, first_name, count = winner
        new_count = count + 1
        cursor.execute("UPDATE users SET beauty_count = ? WHERE user_id = ? AND chat_id = ?", (new_count, user_id, chat_id))

        raw_username = _clean_username(username)
        formatted_winner = raw_username if raw_username else (first_name or "Гном")
        return formatted_winner, new_count


def get_top_beauties(chat_id: int, limit: int = 3):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT username, first_name, beauty_count 
            FROM users 
            WHERE chat_id = ? AND is_bot = 0 AND beauty_count > 0
            ORDER BY beauty_count DESC 
            LIMIT ?
        """, (chat_id, limit))
        top_users = cursor.fetchall()
        return [(_clean_username(u) or f, c) for u, f, c in top_users]


def get_all_chats():
    """Возвращает ID групп/супергрупп, в которых бот ранее видел пользователей."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT chat_id FROM users WHERE chat_id < 0"
        )
        rows = cursor.fetchall()
        return [row[0] for row in rows]


def save_pizda_candidate(chat_id: int, message_id: int, created_at: int, used: int = 0) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO pizda_candidates (chat_id, message_id, created_at, used)
            VALUES (?, ?, ?, ?)
        """, (chat_id, message_id, created_at, used))
        return cursor.rowcount > 0


def mark_pizda_candidate_used(chat_id: int, message_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pizda_candidates SET used = 1 WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )


def get_pizda_candidate_chats(before_ts: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT chat_id
            FROM pizda_candidates
            WHERE used = 0 AND created_at < ?
        """, (before_ts,))
        return cursor.fetchall()


def get_all_pizda_candidates():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, message_id, created_at, used FROM pizda_candidates")
        rows = cursor.fetchall()
        return [
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "created_at": created_at,
                "used": used,
            }
            for chat_id, message_id, created_at, used in rows
        ]


def pick_pizda_candidates(chat_id: int, before_ts: int, limit: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT message_id
            FROM pizda_candidates
            WHERE chat_id = ? AND used = 0 AND created_at < ?
            ORDER BY RANDOM()
            LIMIT ?
        """, (chat_id, before_ts, limit))
        rows = cursor.fetchall()
        return [message_id for (message_id,) in rows]


def get_bot_meta(key: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM bot_meta WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else None


def set_bot_meta(key: str, value):
    with get_db() as conn:
        cursor = conn.cursor()
        if value is None:
            cursor.execute("DELETE FROM bot_meta WHERE key = ?", (key,))
        else:
            cursor.execute("""
                INSERT INTO bot_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, value))
            
# ==========================================
# 👹 БИТВЫ С БОССАМИ
# ==========================================

def get_bosses_defeated(user_id: int, chat_id: int) -> int:
    """Возвращает количество побежденных боссов игроком в конкретном чате."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(bosses_defeated, 0)
            FROM duel_users
            WHERE user_id = ? AND chat_id = ?
            """,
            (user_id, chat_id),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0


def reward_boss_victory(user_id: int, chat_id: int) -> bool:
    """
    Награда за победу над боссом.

    Игрок:
    - получает ровно 100 очков;
    - увеличивает счётчик побежденных боссов;
    - возвращается из состояния "без хуя";
    - снимается отметка о сегодняшней краже.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE duel_users
            SET points = 100,
                bosses_defeated = COALESCE(bosses_defeated, 0) + 1,
                dick_stolen_today = 0,
                last_stolen_by = NULL
            WHERE user_id = ? AND chat_id = ?
            """,
            (user_id, chat_id),
        )

        return cursor.rowcount > 0
