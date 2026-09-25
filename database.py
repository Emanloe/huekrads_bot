import random
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo
import pytz
from config import DUEL_TIMEZONE, DICK_STEAL_CHANCE, DICK_STEAL_CHANCE_PER_WIN, DIG_FIND_CHANCE
from text_resources import get_text

DB_NAME = "bot_database.db"
BIRTHDAY_COOLDOWN = timedelta(days=365)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def moscow_month_key(when: datetime | None = None) -> str:
    """Calendar month of an instant in the game's Moscow timezone."""
    instant = when if when is not None else _utc_now()
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Month key requires a timezone-aware instant")
    return instant.astimezone(ZoneInfo(DUEL_TIMEZONE)).strftime("%Y-%m")


def moscow_date_key(when: datetime | None = None) -> str:
    """Calendar date of an instant in the game's Moscow timezone."""
    instant = when if when is not None else _utc_now()
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Dig date requires a timezone-aware instant")
    return instant.astimezone(ZoneInfo(DUEL_TIMEZONE)).date().isoformat()


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

        if "gnome_variant" not in cols:
            cursor.execute("ALTER TABLE duel_users ADD COLUMN gnome_variant TEXT")

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
                claimed_at TEXT,
                published_at REAL,
                origin_text TEXT,
                autoloot_claimed INTEGER NOT NULL DEFAULT 0,
                autoloot_announced_at TEXT
            )
        """)
        cursor.execute("PRAGMA table_info(duel_item_events)")
        item_event_columns = {column[1] for column in cursor.fetchall()}
        for column, column_type in (
            ("published_at", "REAL"),
            ("origin_text", "TEXT"),
            ("autoloot_claimed", "INTEGER NOT NULL DEFAULT 0"),
            ("autoloot_announced_at", "TEXT"),
        ):
            if column not in item_event_columns:
                cursor.execute(f"ALTER TABLE duel_item_events ADD COLUMN {column} {column_type}")
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_duel_item_events_active_chat
            ON duel_item_events (chat_id)
            WHERE claimed = 0
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_duel_item_events_autoloot
            ON duel_item_events (claimed, published_at)
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS huecrab_owners (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                obtained_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS huecrab_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                consumed INTEGER NOT NULL DEFAULT 0,
                attempted_by INTEGER,
                tamed INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_huecrab_events_active_chat
            ON huecrab_events (chat_id) WHERE consumed = 0
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS duel_dig_daily (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moscow_date TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id, moscow_date)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monthly_chat_stats (
                chat_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                dicks_stolen INTEGER NOT NULL DEFAULT 0,
                duels INTEGER NOT NULL DEFAULT 0,
                bosses_killed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, month)
            )
        """)

        # Durable ordinary duels shared by the Telegram adapter and future clients.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS duel_sessions (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                player1_user_id INTEGER NOT NULL,
                player2_user_id INTEGER NOT NULL,
                player1_snapshot_json TEXT NOT NULL,
                player2_snapshot_json TEXT NOT NULL,
                attacker_user_id INTEGER NOT NULL,
                defender_user_id INTEGER NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('publishing', 'active', 'finished', 'cancelled')),
                phase TEXT NOT NULL CHECK (phase IN ('attack', 'block')),
                attack_zone TEXT
                    CHECK (attack_zone IS NULL OR attack_zone IN ('head', 'body', 'dick')),
                round_no INTEGER NOT NULL DEFAULT 1 CHECK (round_no >= 1),
                turn_id INTEGER NOT NULL DEFAULT 1 CHECK (turn_id >= 1),
                deadline_at INTEGER,
                message_id INTEGER,
                original_message_id INTEGER,
                result_json TEXT,
                pocket_done_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                finished_at INTEGER,
                CHECK (player1_user_id <> player2_user_id),
                CHECK (
                    (attacker_user_id = player1_user_id AND defender_user_id = player2_user_id)
                    OR
                    (attacker_user_id = player2_user_id AND defender_user_id = player1_user_id)
                ),
                CHECK (
                    (phase = 'attack' AND attack_zone IS NULL)
                    OR
                    (phase = 'block' AND attack_zone IS NOT NULL)
                ),
                CHECK (
                    (status = 'active' AND deadline_at IS NOT NULL)
                    OR
                    (status <> 'active' AND deadline_at IS NULL)
                ),
                CHECK (
                    status <> 'finished'
                    OR (finished_at IS NOT NULL AND result_json IS NOT NULL)
                ),
                CHECK (pocket_done_at IS NULL OR status = 'finished')
            )
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_duel_sessions_one_active_chat
            ON duel_sessions (chat_id)
            WHERE status IN ('publishing', 'active')
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_duel_sessions_due
            ON duel_sessions (deadline_at, id)
            WHERE status = 'active'
        """)

        # Durable ordinary-duel publication intents.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS duel_outbox (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                duel_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN (
                    'attack_prompt', 'block_prompt', 'final_result', 'pocket_drop'
                )),
                turn_id INTEGER,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'leased', 'delivered', 'cancelled')),
                message_id INTEGER,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                lease_until INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                delivered_at INTEGER,
                CHECK (
                    (kind = 'pocket_drop' AND turn_id IS NULL)
                    OR (kind <> 'pocket_drop' AND turn_id IS NOT NULL AND turn_id >= 1)
                ),
                CHECK ((status = 'leased') = (lease_until IS NOT NULL)),
                CHECK ((status = 'delivered') = (delivered_at IS NOT NULL))
            )
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_duel_outbox_turn
            ON duel_outbox (chat_id, duel_id, kind, turn_id)
            WHERE kind IN ('attack_prompt', 'block_prompt')
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_duel_outbox_once
            ON duel_outbox (chat_id, duel_id, kind)
            WHERE kind IN ('final_result', 'pocket_drop')
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_duel_outbox_retry
            ON duel_outbox (chat_id, status, lease_until, id)
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS miniapp_launch_tokens (
                token_digest TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER,
                CHECK (expires_at > created_at)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_miniapp_launch_expiry
            ON miniapp_launch_tokens (expires_at)
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS miniapp_sessions (
                token_digest TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                CHECK (expires_at > created_at)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_miniapp_session_expiry
            ON miniapp_sessions (expires_at)
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

def _reset_user_if_new_day(cursor, row, *, persist: bool = True) -> dict | None:
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
        if persist:
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


def get_duel_user_by_id_in_transaction(cursor, chat_id: int, user_id: int, *, read_only: bool = False) -> dict | None:
    """Read a registered chat participant using the caller's transaction.

    Like the existing public getter, this applies the lazy daily reset.
    It never registers a missing participant.
    """
    cursor.execute("""
        SELECT user_id, chat_id, username, display_name, points, wins, losses,
               stolen_dicks_count, dick_stolen_count, dick_stolen_today,
               last_activity_date, last_stolen_by, daily_wins, dwarf_name
        FROM duel_users WHERE chat_id = ? AND user_id = ?
    """, (chat_id, user_id))
    return _reset_user_if_new_day(cursor, cursor.fetchone(), persist=not read_only)


def get_duel_user_by_id(chat_id: int, user_id: int, *, read_only: bool = False) -> dict | None:
    """Read an existing duel participant in one chat without registering them."""
    with get_db() as conn:
        return get_duel_user_by_id_in_transaction(conn.cursor(), chat_id, user_id,
                                                  read_only=read_only)


def get_duel_user_by_username(username: str, chat_id: int, *, read_only: bool = False) -> dict | None:
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
            return _reset_user_if_new_day(cursor, row, persist=not read_only)

        if read_only:
            return None

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


def _increment_monthly_chat_stats(
    cursor, chat_id: int, month: str, *, duels: int = 0,
    dicks_stolen: int = 0, bosses_killed: int = 0,
) -> None:
    cursor.execute(
        """
        INSERT INTO monthly_chat_stats
            (chat_id, month, dicks_stolen, duels, bosses_killed)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, month) DO UPDATE SET
            dicks_stolen = dicks_stolen + excluded.dicks_stolen,
            duels = duels + excluded.duels,
            bosses_killed = bosses_killed + excluded.bosses_killed
        """,
        (chat_id, month, dicks_stolen, duels, bosses_killed),
    )


def get_monthly_chat_stats(chat_id: int, month: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT dicks_stolen, duels, bosses_killed
            FROM monthly_chat_stats WHERE chat_id = ? AND month = ?
            """,
            (chat_id, month),
        ).fetchone()
    return dict(zip(("dicks_stolen", "duels", "bosses_killed"), row or (0, 0, 0)))


def increment_monthly_bosses_killed(chat_id: int, month: str | None = None) -> None:
    with get_db() as conn:
        _increment_monthly_chat_stats(
            conn.cursor(), chat_id, month or moscow_month_key(), bosses_killed=1,
        )


def apply_duel_result_plan(
    chat_id: int,
    result_plan: dict,
) -> tuple[int, int]:
    with get_db() as conn:
        return apply_duel_result_plan_in_transaction(conn.cursor(), chat_id, result_plan)


def apply_duel_result_plan_in_transaction(
    cursor, chat_id: int, result_plan: dict,
) -> tuple[int, int]:
    """Apply the existing result and monthly counters on the caller's connection."""
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

    _increment_monthly_chat_stats(
        cursor,
        chat_id,
        moscow_month_key(),
        duels=1,
        dicks_stolen=int(result_plan["is_dick_stolen"]),
    )

    return winner["points"], loser["points"]


def apply_duel_berserk(
    chat_id: int,
    berserker_user_id: int,
    victim_user_id: int,
    berserker_title: str,
) -> bool:
    with get_db() as conn:
        return apply_duel_berserk_in_transaction(
            conn.cursor(), chat_id, berserker_user_id, victim_user_id, berserker_title,
        )


def apply_duel_berserk_in_transaction(
    cursor, chat_id: int, berserker_user_id: int, victim_user_id: int,
    berserker_title: str,
) -> bool:
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
        return get_duel_inventory_in_transaction(conn.cursor(), chat_id, user_id)


def get_duel_inventory_in_transaction(cursor, chat_id: int, user_id: int) -> list[dict]:
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
        return transfer_duel_inventory_item_in_transaction(
            cursor, chat_id, from_user_id, to_user_id, inventory_instance_id,
        )


def transfer_duel_inventory_item_in_transaction(
    cursor, chat_id: int, from_user_id: int, to_user_id: int,
    inventory_instance_id: int,
) -> bool:
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


def create_duel_item_event_from_inventory(
    chat_id: int, user_id: int, instance_id: int,
) -> dict | None:
    """Move one exact inventory instance into the chat's active pickup slot."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        return create_duel_item_event_from_inventory_in_transaction(
            cursor, chat_id, user_id, instance_id,
        )


def create_duel_item_event_from_inventory_in_transaction(
    cursor, chat_id: int, user_id: int, instance_id: int,
) -> dict | None:
    cursor.execute(
        """
        SELECT item_id, created_at FROM duel_inventory
        WHERE id = ? AND chat_id = ? AND user_id = ?
        """,
        (instance_id, chat_id, user_id),
    )
    row = cursor.fetchone()
    if not row or row[0] in ("oiled_vest", "knife"):
        return None

    cursor.execute(
        "INSERT OR IGNORE INTO duel_item_events (chat_id, item_id) VALUES (?, ?)",
        (chat_id, row[0]),
    )
    if cursor.rowcount != 1:
        return None
    event_id = cursor.lastrowid
    cursor.execute(
        "DELETE FROM duel_inventory WHERE id = ? AND chat_id = ? AND user_id = ?",
        (instance_id, chat_id, user_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Selected duel inventory instance disappeared")
    return {
        "event_id": event_id,
        "chat_id": chat_id,
        "user_id": user_id,
        "instance_id": instance_id,
        "item_id": row[0],
        "created_at": row[1],
    }


def restore_unpublished_duel_drop(drop: dict, message_id: int | None = None) -> bool:
    """Atomically return a drop when publishing its pickup message failed."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        return restore_unpublished_duel_drop_in_transaction(cursor, drop, message_id)


def restore_unpublished_duel_drop_in_transaction(
    cursor, drop: dict, message_id: int | None = None,
) -> bool:
    cursor.execute(
        """
        SELECT 1 FROM duel_item_events
        WHERE event_id = ? AND chat_id = ? AND item_id = ?
          AND claimed = 0 AND (message_id IS NULL OR message_id = ?)
        """,
        (drop["event_id"], drop["chat_id"], drop["item_id"], message_id),
    )
    if not cursor.fetchone():
        return False
    cursor.execute(
        """
        INSERT INTO duel_inventory (id, chat_id, user_id, item_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            drop["instance_id"], drop["chat_id"], drop["user_id"],
            drop["item_id"], drop["created_at"],
        ),
    )
    cursor.execute(
        "DELETE FROM duel_item_events WHERE event_id = ?",
        (drop["event_id"],),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Unpublished duel drop disappeared during restoration")
    return True


def get_duel_dig_attempts(chat_id: int, user_id: int, date_key: str | None = None) -> int:
    date_key = date_key or moscow_date_key()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT attempts FROM duel_dig_daily
            WHERE chat_id = ? AND user_id = ? AND moscow_date = ?
            """,
            (chat_id, user_id, date_key),
        ).fetchone()
    return row[0] if row else 0


def try_duel_dig(chat_id: int, user_id: int, roll, item_selector) -> tuple[str, dict | None]:
    """Pay for one dig, and optionally reserve its exact loot, in one transaction."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        date_key = moscow_date_key()
        cursor.execute(
            """
            SELECT points, username, display_name, dwarf_name FROM duel_users
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        )
        user = cursor.fetchone()
        if user is None:
            return "not_registered", None

        cursor.execute(
            "SELECT 1 FROM duel_item_events WHERE chat_id = ? AND claimed = 0",
            (chat_id,),
        )
        if cursor.fetchone():
            return "active_event", None

        cursor.execute(
            """
            SELECT attempts FROM duel_dig_daily
            WHERE chat_id = ? AND user_id = ? AND moscow_date = ?
            """,
            (chat_id, user_id, date_key),
        )
        row = cursor.fetchone()
        attempts = row[0] if row else 0
        if attempts >= 5:
            return "daily_limit", None
        if user[0] < 10:
            return "insufficient_points", None

        found = roll() < DIG_FIND_CHANCE
        item_id = item_selector() if found else None
        event_id = None
        if found:
            if item_id in ("oiled_vest", "knife"):
                raise ValueError("Permanent base items cannot be dug up")
            cursor.execute(
                "INSERT OR IGNORE INTO duel_item_events (chat_id, item_id) VALUES (?, ?)",
                (chat_id, item_id),
            )
            if cursor.rowcount != 1:
                return "active_event", None
            event_id = cursor.lastrowid

        cursor.execute(
            """
            UPDATE duel_users SET points = points - 10
            WHERE chat_id = ? AND user_id = ? AND points >= 10
            """,
            (chat_id, user_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Dig points changed during locked transaction")
        cursor.execute(
            """
            INSERT INTO duel_dig_daily (chat_id, user_id, moscow_date, attempts)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id, moscow_date) DO UPDATE SET
                attempts = attempts + 1 WHERE attempts < 5
            """,
            (chat_id, user_id, date_key),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Dig daily limit changed during locked transaction")

        return ("found" if found else "miss"), {
            "chat_id": chat_id,
            "user_id": user_id,
            "date_key": date_key,
            "points": user[0] - 10,
            "remaining": 4 - attempts,
            "event_id": event_id,
            "item_id": item_id,
            "user": {
                "username": user[1],
                "display_name": user[2],
                "dwarf_name": user[3],
            },
        }


def cancel_unpublished_duel_dig(dig: dict, message_id: int | None = None) -> bool:
    """Refund one unpublished find without permitting a second refund."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """
            SELECT 1 FROM duel_item_events
            WHERE event_id = ? AND chat_id = ? AND item_id = ?
              AND claimed = 0 AND (message_id IS NULL OR message_id = ?)
            """,
            (dig["event_id"], dig["chat_id"], dig["item_id"], message_id),
        )
        if not cursor.fetchone():
            return False
        cursor.execute(
            "UPDATE duel_users SET points = points + 10 WHERE chat_id = ? AND user_id = ?",
            (dig["chat_id"], dig["user_id"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Dig user disappeared before refund")
        cursor.execute(
            """
            UPDATE duel_dig_daily SET attempts = attempts - 1
            WHERE chat_id = ? AND user_id = ? AND moscow_date = ? AND attempts > 0
            """,
            (dig["chat_id"], dig["user_id"], dig["date_key"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Dig attempt disappeared before refund")
        cursor.execute(
            "DELETE FROM duel_item_events WHERE event_id = ?",
            (dig["event_id"],),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Dig event disappeared before refund")
        return True


def set_duel_item_event_message(
    event_id: int, message_id: int, origin_text: str | None = None,
) -> bool:
    with get_db() as conn:
        return set_duel_item_event_message_in_transaction(
            conn.cursor(), event_id, message_id, origin_text,
        )


def set_duel_item_event_message_in_transaction(
    cursor, event_id: int, message_id: int, origin_text: str | None = None,
) -> bool:
    cursor.execute(
        """
        UPDATE duel_item_events
        SET message_id = ?, published_at = ?, origin_text = ?
        WHERE event_id = ? AND claimed = 0 AND message_id IS NULL
        """,
        (message_id, _utc_now().timestamp(), origin_text, event_id),
    )
    return cursor.rowcount == 1


def bind_duel_item_event_message_in_transaction(
    cursor, chat_id: int, event_id: int, message_id: int,
    published_at_ms: int, origin_text: str,
) -> bool:
    """Bind a persistent drop's pickup button within its chat and outbox ack."""
    cursor.execute(
        """
        UPDATE duel_item_events
        SET message_id = ?, published_at = ?, origin_text = ?
        WHERE chat_id = ? AND event_id = ? AND claimed = 0 AND message_id IS NULL
        """,
        (message_id, published_at_ms / 1000, origin_text, chat_id, event_id),
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
                   item_id, created_at, claimed_at, published_at,
                   origin_text, autoloot_announced_at
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
            "published_at": row[8],
            "origin_text": row[9],
            "autoloot_announced_at": row[10],
        }


def claim_duel_item_event(
    event_id: int,
    chat_id: int,
    user_id: int,
    item_selector,
    huegryz_decider=None,
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
            SELECT claimed, item_id, message_id
            FROM duel_item_events
            WHERE event_id = ? AND chat_id = ?
            """,
            (event_id, chat_id),
        )
        event = cursor.fetchone()
        if not event or event[0] or (event[1] is not None and event[2] is None):
            return "already_claimed", None

        cursor.execute(
            "SELECT 1 FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        if not cursor.fetchone():
            return "not_registered", None
        instance = _claim_item_in_transaction(
            cursor, event_id, chat_id, user_id, event[1], item_selector,
        )
        if instance is None:
            return "already_claimed", None
        if huegryz_decider is not None:
            instance["huegryz_outcome"] = None
            if huegryz_decider():
                cursor.execute(
                    """
                    UPDATE duel_users
                    SET dick_stolen_today = 1,
                        dick_stolen_count = dick_stolen_count + 1,
                        last_stolen_by = NULL
                    WHERE chat_id = ? AND user_id = ? AND dick_stolen_today = 0
                    """,
                    (chat_id, user_id),
                )
                instance["huegryz_outcome"] = (
                    "bitten" if cursor.rowcount == 1 else "already_dickless"
                )
        return "claimed", instance


def _claim_item_in_transaction(
    cursor, event_id: int, chat_id: int, user_id: int,
    fixed_item_id: str | None, item_selector,
) -> dict | None:
    """One item-event CAS and inventory transfer for manual and pet pickup."""
    cursor.execute(
        """
        UPDATE duel_item_events
        SET claimed = 1, claimed_by = ?, claimed_at = CURRENT_TIMESTAMP
        WHERE event_id = ? AND chat_id = ? AND claimed = 0
        """,
        (user_id, event_id, chat_id),
    )
    if cursor.rowcount != 1:
        return None
    item_id = fixed_item_id if fixed_item_id is not None else item_selector()
    cursor.execute(
        "INSERT INTO duel_inventory (chat_id, user_id, item_id) VALUES (?, ?, ?)",
        (chat_id, user_id, item_id),
    )
    instance = {
        "id": cursor.lastrowid,
        "chat_id": chat_id,
        "user_id": user_id,
        "item_id": item_id,
    }
    cursor.execute(
        "UPDATE duel_item_events SET item_id = ? WHERE event_id = ?",
        (item_id, event_id),
    )
    return instance


def list_due_huecrab_item_events(now: float, delay_seconds: int) -> list[int]:
    with get_db() as conn:
        return [row[0] for row in conn.execute(
            """SELECT event_id FROM duel_item_events
               WHERE claimed = 0 AND published_at IS NOT NULL AND published_at <= ?
               ORDER BY published_at, event_id""",
            (now - delay_seconds,),
        )]


def claim_due_item_for_huecrab(
    event_id: int, now: float, delay_seconds: int, item_selector, owner_selector,
) -> tuple[str, dict | None]:
    """Select a chat owner and claim under the same SQLite write lock as manual pickup."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        row = cursor.execute(
            """SELECT chat_id, item_id, message_id, origin_text
               FROM duel_item_events WHERE event_id = ? AND claimed = 0
               AND published_at IS NOT NULL AND published_at <= ?""",
            (event_id, now - delay_seconds),
        ).fetchone()
        if row is None or row[2] is None:
            return "unavailable", None
        chat_id, fixed_item_id, message_id, origin_text = row
        owners = cursor.execute(
            """SELECT u.user_id, u.username, u.display_name, u.dwarf_name
               FROM huecrab_owners AS h JOIN duel_users AS u
               ON u.chat_id = h.chat_id AND u.user_id = h.user_id
               WHERE h.chat_id = ? ORDER BY u.user_id""",
            (chat_id,),
        ).fetchall()
        if not owners:
            return "no_owners", None
        owner = owners[0] if len(owners) == 1 else owner_selector(owners)
        user_id, username, display_name, dwarf_name = owner
        instance = _claim_item_in_transaction(
            cursor, event_id, chat_id, user_id, fixed_item_id, item_selector,
        )
        if instance is None:
            return "unavailable", None
        cursor.execute(
            "UPDATE duel_item_events SET autoloot_claimed = 1 WHERE event_id = ?",
            (event_id,),
        )
        return "claimed", {
            **instance, "event_id": event_id, "message_id": message_id,
            "origin_text": origin_text,
            "owner": {
                "user_id": user_id, "username": username,
                "display_name": display_name, "dwarf_name": dwarf_name,
            },
        }


def list_unannounced_huecrab_claims() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT e.event_id, e.chat_id, e.message_id, e.item_id, e.origin_text,
                      u.user_id, u.username, u.display_name, u.dwarf_name
               FROM duel_item_events AS e JOIN duel_users AS u
               ON u.chat_id = e.chat_id AND u.user_id = e.claimed_by
               WHERE e.claimed = 1 AND e.autoloot_claimed = 1
               AND e.autoloot_announced_at IS NULL ORDER BY e.event_id"""
        ).fetchall()
    return [
        {
            "event_id": r[0], "chat_id": r[1], "message_id": r[2],
            "item_id": r[3], "origin_text": r[4],
            "owner": {"user_id": r[5], "username": r[6],
                      "display_name": r[7], "dwarf_name": r[8]},
        }
        for r in rows
    ]


def mark_huecrab_claim_announced(event_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            """UPDATE duel_item_events SET autoloot_announced_at = CURRENT_TIMESTAMP
               WHERE event_id = ? AND claimed = 1 AND autoloot_announced_at IS NULL""",
            (event_id,),
        )


def has_huecrab(chat_id: int, user_id: int) -> bool:
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM huecrab_owners WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone() is not None


def create_huecrab_event(chat_id: int) -> int | None:
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO huecrab_events (chat_id) VALUES (?)", (chat_id,),
        )
        return cursor.lastrowid if cursor.rowcount == 1 else None


def has_active_huecrab_event(chat_id: int) -> bool:
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM huecrab_events WHERE chat_id = ? AND consumed = 0",
            (chat_id,),
        ).fetchone() is not None


def bind_huecrab_event(event_id: int, message_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE huecrab_events SET message_id = ?
               WHERE event_id = ? AND consumed = 0 AND message_id IS NULL""",
            (message_id, event_id),
        )
        return cursor.rowcount == 1


def discard_unpublished_huecrab_event(event_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            """DELETE FROM huecrab_events
               WHERE event_id = ? AND consumed = 0 AND message_id IS NULL""",
            (event_id,),
        )
        return cursor.rowcount == 1


def tame_huecrab_event(
    event_id: int, chat_id: int, user_id: int, message_id: int, tame_decider,
) -> str:
    """The first registered petless attempt consumes one wild event."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        event = cursor.execute(
            """SELECT consumed FROM huecrab_events
               WHERE event_id = ? AND chat_id = ? AND message_id = ?""",
            (event_id, chat_id, message_id),
        ).fetchone()
        if event is None or event[0]:
            return "taken"
        if cursor.execute(
            "SELECT 1 FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone() is None:
            return "not_registered"
        if cursor.execute(
            "SELECT 1 FROM huecrab_owners WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone() is not None:
            return "already_owned"
        cursor.execute(
            """UPDATE huecrab_events SET consumed = 1, attempted_by = ?
               WHERE event_id = ? AND chat_id = ? AND consumed = 0""",
            (user_id, event_id, chat_id),
        )
        if cursor.rowcount != 1:
            return "taken"
        if not tame_decider():
            cursor.execute("UPDATE huecrab_events SET tamed = 0 WHERE event_id = ?", (event_id,))
            return "failed"
        cursor.execute(
            "INSERT INTO huecrab_owners (chat_id, user_id) VALUES (?, ?)",
            (chat_id, user_id),
        )
        cursor.execute("UPDATE huecrab_events SET tamed = 1 WHERE event_id = ?", (event_id,))
        return "tamed"


def get_duel_top(
    chat_id: int, sort_by: str = "wins", limit: int = 10,
    include_dwarf_name: bool = False,
) -> list:
    rows = get_duel_top_read_model(chat_id, sort_by=sort_by, limit=limit)
    return [
        (row["username"], row["display_name"], row["wins"], row["losses"],
         row["points"], row["dwarf_name"])
        if include_dwarf_name else
        (row["username"], row["display_name"], row["wins"], row["losses"],
         row["points"])
        for row in rows
    ]


def get_duel_top_read_model(
    chat_id: int, sort_by: str = "wins", limit: int = 10,
) -> list[dict]:
    """Canonical chat leaderboard rows for Telegram and Mini App presentation."""
    valid_cols = {"wins": "wins", "points": "points"}
    sort_column = valid_cols.get(sort_by, "wins")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT user_id, username, display_name, wins, losses, points,
                   dwarf_name, gnome_variant
            FROM duel_users
            WHERE chat_id = ?
            ORDER BY {sort_column} DESC, wins DESC
            LIMIT ?
        """, (chat_id, limit))
        rows = cursor.fetchall()

        return [
            {
                "user_id": user_id,
                "username": _clean_username(username),
                "display_name": _clean_username(display_name) or display_name,
                "wins": wins, "losses": losses, "points": points,
                "dwarf_name": dwarf_name, "gnome_variant": gnome_variant,
            }
            for user_id, username, display_name, wins, losses, points,
                dwarf_name, gnome_variant in rows
        ]


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
