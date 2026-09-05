"""
Нашивки (достижения) и антинашивки (антидостижения).

В отличие от ranks.ACTIVITY_RANKS (одна "текущая" нашивка активности,
которая заменяется на более высокую по мере роста числа сообщений),
здесь — набор НЕЗАВИСИМЫХ значков, которые пользователь копит.

Главное правило (так решили в проекте): полученная нашивка или антинашивка
остаётся у пользователя НАВСЕГДА — даже если условие потом перестало
выполняться (например, репутация испортилась после "Чистой репутации",
или наоборот, человек исправился после "Хулигана"). Всё пишется в
storage.py (таблицы achievements / badges) и никогда не удаляется
автоматически.

Всё работает полностью автоматически: после каждого события (новое
сообщение, предупреждение, мут, репост чужой записи) вызывается
check_achievements(user_id) — он сам проверяет условия и сам выдаёт
новые нашивки/антинашивки, если условие выполнено.

Как подключено в bot.py:
    new_items = achievements.check_achievements(user_id)
    for kind, code, name in new_items:
        # объявить в чате: kind == "achievement" или "badge"
"""

import storage

# ---------- Хорошие нашивки (достижения) ----------
# (code, название, условие-функция от словаря статистики пользователя)
ACHIEVEMENTS = [
    ("first_message", "🔰 Первое слово", lambda s: s["total_messages"] >= 1),
    ("talkative_100", "💬 Разговорчивый", lambda s: s["total_messages"] >= 100),
    ("veteran_1000", "⭐ Ветеран чата", lambda s: s["total_messages"] >= 1000),
    ("legend_5000", "👑 Легенда чата", lambda s: s["total_messages"] >= 5000),
    ("clean_100", "😇 Чистая репутация", lambda s: s["total_messages"] >= 100 and s["total_warnings_ever"] == 0),
]

# ---------- Плохие нашивки (антидостижения) ----------
NEGATIVE_BADGES = [
    ("first_warning", "⚠️ Оступился", lambda s: s["total_warnings_ever"] >= 1),
    ("troublemaker", "😈 Непослушный", lambda s: s["total_warnings_ever"] >= 3),
    ("serial_offender", "🚨 Частый гость варна", lambda s: s["total_warnings_ever"] >= 10),
    ("muted_once", "🧊 Побывал в муте", lambda s: s["total_mutes_ever"] >= 1),
    ("repeat_offender", "👺 Хулиган чата", lambda s: s["total_mutes_ever"] >= 3),
    ("reposter", "🙈 Тащит чужой контент", lambda s: s["total_reposts_ever"] >= 1),
]


def _build_stats(user_id: int) -> dict:
    stats = storage.get_stats(user_id)
    stats["total_messages"] = storage.get_user_total_messages(user_id)
    return stats


def check_achievements(user_id: int):
    """Проверяет условия всех нашивок/антинашивок для пользователя и сразу
    же сохраняет новые (навсегда, через storage). Уже полученные повторно
    не проверяются и не могут исчезнуть.

    Возвращает список новых значков за этот вызов:
        [("achievement", code, name), ("badge", code, name), ...]
    Пустой список, если ничего нового не разблокировано.
    """
    stats = _build_stats(user_id)
    already_ach = set(storage.get_user_achievements(user_id))
    already_badges = set(storage.get_user_badges(user_id))

    new_items = []

    for code, name, condition in ACHIEVEMENTS:
        if code in already_ach:
            continue
        if condition(stats):
            storage.unlock_achievement(user_id, code)
            new_items.append(("achievement", code, name))

    for code, name, condition in NEGATIVE_BADGES:
        if code in already_badges:
            continue
        if condition(stats):
            storage.unlock_badge(user_id, code)
            new_items.append(("badge", code, name))

    return new_items


def format_profile_block(user_id: int) -> str:
    """Готовый текстовый блок для команды «мой профиль» —
    список нашивок и антинашивок пользователя (то, что накопилось навсегда)."""
    unlocked_ach = set(storage.get_user_achievements(user_id))
    unlocked_badges = set(storage.get_user_badges(user_id))

    ach_names = [name for code, name, _ in ACHIEVEMENTS if code in unlocked_ach]
    badge_names = [name for code, name, _ in NEGATIVE_BADGES if code in unlocked_badges]

    lines = [
        "🏆 Нашивки: " + (", ".join(ach_names) if ach_names else "пока нет"),
        "⚠️ Антинашивки: " + (", ".join(badge_names) if badge_names else "пока нет"),
    ]
    return "\n".join(lines)
