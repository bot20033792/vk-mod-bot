"""
Хранилище на SQLite: предупреждения пользователей и лог действий бота.
Файл базы: moderation.db (создаётся автоматически рядом с bot.py).
"""

import sqlite3
import time

DB_PATH = "moderation.db"


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
    conn.commit()
    conn.close()


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
