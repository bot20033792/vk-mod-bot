"""
Хранилище на SQLite: предупреждения, лог действий бота, временные муты,
статистика сообщений (для «мой профиль» и «статистика»).
Файл базы: moderation.db (создаётся автоматически рядом с bot.py).
"""

import sqlite3
import time

DB_PATH = "moderation.db"

# Не храним историю сообщений дольше этого срока (статистика нужна максимум
# за последние сутки, но с запасом храним неделю на случай разбирательств).
MESSAGE_LOG_RETENTION_SECONDS = 7 * 24 * 60 * 60


def _connect():
    """Подключение к БД с настройками для многопоточной работы."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    """Создаёт все таблицы, если их ещё нет. Вызывается один раз при старте."""
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS warnings (
                    user_id INTEGER PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS actions_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    user_id INTEGER NOT NULL,
                    peer_id INTEGER NOT NULL,
                    message_id INTEGER,
                    action TEXT NOT NULL,
                    reason TEXT,
                    message_text TEXT,
                    reverted INTEGER NOT NULL DEFAULT 0,
                    reverted_by INTEGER
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mutes (
                    user_id INTEGER PRIMARY KEY,
                    until_ts REAL NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS message_counts (
                    user_id INTEGER PRIMARY KEY,
                    total_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS message_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    ts REAL NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_message_log_ts ON message_log (ts)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS achievements (
                    user_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    unlocked_at REAL NOT NULL,
                    PRIMARY KEY (user_id, code)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS badges (
                    user_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    unlocked_at REAL NOT NULL,
                    PRIMARY KEY (user_id, code)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id INTEGER PRIMARY KEY,
                    total_warnings_ever INTEGER NOT NULL DEFAULT 0,
                    total_mutes_ever INTEGER NOT NULL DEFAULT 0,
                    total_reposts_ever INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.commit()
    except sqlite3.Error as e:
        print(f"[storage] Ошибка при init_db: {e}")
        raise


# ---------- Счётчики для нашивок/антинашивок (навсегда, без сброса) ----------

_STAT_COLUMNS = ("total_warnings_ever", "total_mutes_ever", "total_reposts_ever")


def increment_stat(user_id: int, field: str, amount: int = 1):
    if field not in _STAT_COLUMNS:
        raise ValueError(f"Неизвестный счётчик: {field}")
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (user_id,))
            cur.execute(f"UPDATE user_stats SET {field} = {field} + ? WHERE user_id = ?",
                        (amount, user_id))
            conn.commit()
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в increment_stat: {e}")


def get_stats(user_id: int) -> dict:
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT total_warnings_ever, total_mutes_ever, total_reposts_ever "
                "FROM user_stats WHERE user_id = ?", (user_id,)
            )
            row = cur.fetchone()
        if row is None:
            return {col: 0 for col in _STAT_COLUMNS}
        return dict(zip(_STAT_COLUMNS, row))
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в get_stats: {e}")
        return {col: 0 for col in _STAT_COLUMNS}


# ---------- Нашивки (достижения) — навсегда ----------

def unlock_achievement(user_id: int, code: str) -> bool:
    """True, если нашивка выдана впервые (иначе она уже была раньше)."""
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO achievements (user_id, code, unlocked_at) VALUES (?, ?, ?)",
                (user_id, code, time.time())
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в unlock_achievement: {e}")
        return False


def get_user_achievements(user_id: int):
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT code FROM achievements WHERE user_id = ?", (user_id,))
            return [r[0] for r in cur.fetchall()]
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в get_user_achievements: {e}")
        return []


# ---------- Антинашивки (плохие нашивки) — навсегда ----------

def unlock_badge(user_id: int, code: str) -> bool:
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO badges (user_id, code, unlocked_at) VALUES (?, ?, ?)",
                (user_id, code, time.time())
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в unlock_badge: {e}")
        return False


def get_user_badges(user_id: int):
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT code FROM badges WHERE user_id = ?", (user_id,))
            return [r[0] for r in cur.fetchall()]
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в get_user_badges: {e}")
        return []


# ---------- Предупреждения ----------

def add_warning(user_id: int) -> int:
    """Увеличивает счётчик предупреждений пользователя и возвращает новое значение."""
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO warnings (user_id, count) VALUES (?, 1) "
                "ON CONFLICT(user_id) DO UPDATE SET count = count + 1",
                (user_id,)
            )
            cur.execute("SELECT count FROM warnings WHERE user_id = ?", (user_id,))
            count = cur.fetchone()[0]
            conn.commit()
            return count
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в add_warning: {e}")
        return 0


def reset_warnings(user_id: int):
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE warnings SET count = 0 WHERE user_id = ?", (user_id,))
            conn.commit()
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в reset_warnings: {e}")


# ---------- Лог действий ----------

def log_action(user_id: int, peer_id: int, message_id, action: str, reason: str, text: str):
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO actions_log (ts, user_id, peer_id, message_id, action, reason, message_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), user_id, peer_id, message_id, action, reason, text)
            )
            conn.commit()
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в log_action: {e}")


def get_recent_actions(limit: int = 20):
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, ts, user_id, action, reason, message_text, reverted "
                "FROM actions_log ORDER BY id DESC LIMIT ?", (limit,)
            )
            return cur.fetchall()
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в get_recent_actions: {e}")
        return []


def mark_reverted(action_id: int, moderator_id: int) -> bool:
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM actions_log WHERE id = ?", (action_id,))
            if cur.fetchone() is None:
                return False
            cur.execute(
                "UPDATE actions_log SET reverted = 1, reverted_by = ? WHERE id = ?",
                (moderator_id, action_id)
            )
            conn.commit()
            return True
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в mark_reverted: {e}")
        return False


# ---------- Муты ----------

def set_mute(user_id: int, until_ts: float):
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO mutes (user_id, until_ts) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET until_ts = ?",
                (user_id, until_ts, until_ts)
            )
            conn.commit()
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в set_mute: {e}")


def get_mute_until(user_id: int):
    """Возвращает timestamp окончания мута, если пользователь ещё замьючен,
    иначе None (в том числе если мут уже истёк)."""
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT until_ts FROM mutes WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
        if row is None:
            return None
        until_ts = row[0]
        if until_ts <= time.time():
            return None
        return until_ts
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в get_mute_until: {e}")
        return None


# ---------- Статистика сообщений ----------

def bump_message_count(user_id: int, ts: float) -> int:
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO message_counts (user_id, total_count) VALUES (?, 1) "
                "ON CONFLICT(user_id) DO UPDATE SET total_count = total_count + 1",
                (user_id,)
            )
            cur.execute("INSERT INTO message_log (user_id, ts) VALUES (?, ?)", (user_id, ts))
            cur.execute("DELETE FROM message_log WHERE ts < ?",
                        (ts - MESSAGE_LOG_RETENTION_SECONDS,))
            cur.execute("SELECT total_count FROM message_counts WHERE user_id = ?", (user_id,))
            new_total = cur.fetchone()[0]
            conn.commit()
            return new_total
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в bump_message_count: {e}")
        return 0


def get_user_total_messages(user_id: int) -> int:
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT total_count FROM message_counts WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            return row[0] if row else 0
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в get_user_total_messages: {e}")
        return 0


def get_activity_last_hours(hours: int = 24, limit: int = 15):
    """Список (user_id, количество сообщений) за последние N часов, по убыванию."""
    try:
        since = time.time() - hours * 60 * 60
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT user_id, COUNT(*) as cnt FROM message_log WHERE ts >= ? "
                "GROUP BY user_id ORDER BY cnt DESC LIMIT ?",
                (since, limit)
            )
            return cur.fetchall()
    except sqlite3.Error as e:
        print(f"[storage] Ошибка в get_activity_last_hours: {e}")
        return []
