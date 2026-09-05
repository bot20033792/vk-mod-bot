"""
Ранги модераторов — 5 уровней, от младшего к старшему.
Уровень 0 (не в словаре MODERATORS) — обычный участник, без прав.
"""

RANK_TITLES = {
    1: "Модератор",
    2: "Старший модератор",
    3: "Младший администратор",
    4: "Старший администратор",
    5: "Владелец",
}


def title(level: int) -> str:
    return RANK_TITLES.get(level, f"Уровень {level}")


def can_manage(actor_level: int, target_level: int) -> bool:
    """Может ли actor_level назначить/изменить ранг у кого-то с текущим
    или новым уровнем target_level. Правило простое: можно управлять только
    теми, чей уровень строго ниже своего (нельзя понижать/повышать равных
    или старших себе)."""
    return actor_level > target_level


# Нашивки активности участников — считаются по количеству сообщений в чате,
# не связаны с рангами модераторов выше. Список должен идти по возрастанию
# порога; можно менять пороги или названия как угодно.
ACTIVITY_RANKS = [
    (0, "🔰 Новичок"),
    (50, "💬 Активный участник"),
    (300, "🎖️ Рядовой чата"),
    (1000, "⭐ Старожил"),
    (3000, "🏅 Ветеран"),
    (5000, "👑 Легенда чата"),
]


def get_activity_rank(message_count: int) -> str:
    """Возвращает название нашивки по количеству сообщений (текущий ранг)."""
    current = ACTIVITY_RANKS[0][1]
    for threshold, name in ACTIVITY_RANKS:
        if message_count >= threshold:
            current = name
        else:
            break
    return current


def get_activity_rank_at(message_count: int):
    """То же самое, но возвращает (порог, название) — удобно для сравнения
    рангов до/после, чтобы понять, произошло ли повышение."""
    current = ACTIVITY_RANKS[0]
    for threshold, name in ACTIVITY_RANKS:
        if message_count >= threshold:
            current = (threshold, name)
        else:
            break
    return current
