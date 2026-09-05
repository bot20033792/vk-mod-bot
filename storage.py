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
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = _connect()
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

    # ---- Нашивки и антинашивки (achievements.py) — хранятся навсегда ----
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
    # Счётчики "за всё время" — не сбрасываются при муте/погашении предупреждений,
    # нужны только для проверки условий нашивок/антинашивок.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            total_warnings_ever INTEGER NOT NULL DEFAULT 0,
            total_mutes_ever INTEGER NOT NULL DEFAULT 0,
            total_reposts_ever INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


# ---------- Счётчики для нашивок/антинашивок (навсегда, без сброса) ----------

_STAT_COLUMNS = ("total_warnings_ever", "total_mutes_ever", "total_reposts_ever")


def increment_stat(user_id: int, field: str, amount: int = 1):
    if field not in _STAT_COLUMNS:
        raise ValueError(f"Неизвестный счётчик: {field}")
    conn = _connect()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (user_id,))
    cur.execute(f"UPDATE user_stats SET {field} = {field} + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


def get_stats(user_id: int) -> dict:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT total_warnings_ever, total_mutes_ever, total_reposts_ever "
        "FROM user_stats WHERE user_id = ?", (user_id,)
    )
    row = cur.fetchone()
    conn.close()
    if row is None:
        return {col: 0 for col in _STAT_COLUMNS}
    return dict(zip(_STAT_COLUMNS, row))


# ---------- Нашивки (достижения) — навсегда ----------

def unlock_achievement(user_id: int, code: str) -> bool:
    """True, если нашивка выдана впервые (иначе она уже была раньше)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO achievements (user_id, code, unlocked_at) VALUES (?, ?, ?)",
        (user_id, code, time.time())
    )
    conn.commit()
    added = cur.rowcount > 0
    conn.close()
    return added


def get_user_achievements(user_id: int):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT code FROM achievements WHERE user_id = ?", (user_id,))
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


# ---------- Антинашивки (плохие нашивки) — навсегда ----------

def unlock_badge(user_id: int, code: str) -> bool:
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO badges (user_id, code, unlocked_at) VALUES (?, ?, ?)",
        (user_id, code, time.time())
    )
    conn.commit()
    added = cur.rowcount > 0
    conn.close()
    return added


def get_user_badges(user_id: int):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT code FROM badges WHERE user_id = ?", (user_id,))
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


# ---------- Предупреждения ----------

def add_warning(user_id: int) -> int:
    """Увеличивает счётчик предупреждений пользователя и возвращает новое значение."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO warnings (user_id, count) VALUES (?, 1) "
                "ON CONFLICT(user_id) DO UPDATE SET count = count + 1", (user_id,))
    conn.commit()
    cur.execute("SELECT count FROM warnings WHERE user_id = ?", (user_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count


def reset_warnings(user_id: int):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("UPDATE warnings SET count = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ---------- Лог действий ----------

def log_action(user_id: int, peer_id: int, message_id, action: str, reason: str, text: str):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO actions_log (ts, user_id, peer_id, message_id, action, reason, message_text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (time.time(), user_id, peer_id, message_id, action, reason, text)
    )
    conn.commit()
    conn.close()


def get_recent_actions(limit: int = 20):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, ts, user_id, action, reason, message_text, reverted "
        "FROM actions_log ORDER BY id DESC LIMIT ?", (limit,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def mark_reverted(action_id: int, moderator_id: int) -> bool:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT id FROM actions_log WHERE id = ?", (action_id,))
    if cur.fetchone() is None:
        conn.close()
        return False
    cur.execute(
        "UPDATE actions_log SET reverted = 1, reverted_by = ? WHERE id = ?",
        (moderator_id, action_id)
    )
    conn.commit()
    conn.close()
    return True


# ---------- Муты ----------

def set_mute(user_id: int, until_ts: float):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO mutes (user_id, until_ts) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET until_ts = ?",
        (user_id, until_ts, until_ts)
    )
    conn.commit()
    conn.close()


def get_mute_until(user_id: int):
    """Возвращает timestamp окончания мута, если пользователь ещё замьючен,
    иначе None (в том числе если мут уже истёк)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT until_ts FROM mutes WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    until_ts = row[0]
    if until_ts <= time.time():
        return None
    return until_ts


# ---------- Статистика сообщений ----------

def bump_message_count(user_id: int, ts: float) -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO message_counts (user_id, total_count) VALUES (?, 1) "
                "ON CONFLICT(user_id) DO UPDATE SET total_count = total_count + 1", (user_id,))
    cur.execute("INSERT INTO message_log (user_id, ts) VALUES (?, ?)", (user_id, ts))
    # Периодическая чистка старых записей лога сообщений, чтобы база не росла бесконечно
    cur.execute("DELETE FROM message_log WHERE ts < ?", (ts - MESSAGE_LOG_RETENTION_SECONDS,))
    conn.commit()
    cur.execute("SELECT total_count FROM message_counts WHERE user_id = ?", (user_id,))
    new_total = cur.fetchone()[0]
    conn.close()
    return new_total


def get_user_total_messages(user_id: int) -> int:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT total_count FROM message_counts WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def get_activity_last_hours(hours: int = 24, limit: int = 15):
    """Список (user_id, количество сообщений) за последние N часов, по убыванию."""
    since = time.time() - hours * 60 * 60
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, COUNT(*) as cnt FROM message_log WHERE ts >= ? "
        "GROUP BY user_id ORDER BY cnt DESC LIMIT ?",
        (since, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return rows
