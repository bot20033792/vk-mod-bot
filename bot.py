"""
VK-бот: автомодератор на чистом ИИ (без словарей и ручных правил).

Как работает модерация:
- Каждое сообщение (кроме сообщений модераторов) отправляется в ИИ (Groq)
  вместе с текстом правил чата (config.json -> rules_text).
- ИИ решает: есть нарушение или нет, и если да — какое действие применить
  (warn / delete_and_warn / mute / ban).
- Каждое действие бота пишется в лог (storage.actions_log), модераторы могут
  посмотреть его через /log и отменить через /revert.
- Если ИИ недоступен или вернул ошибку — считается, что нарушения нет
  (fail-safe, бот не наказывает "на всякий случай").

Команды модераторов (только для ID из config.json -> moderators):
    /log             — последние 20 действий бота
    /revert <id>     — отменить действие бота
    /setrules <текст> — задать/сменить текст правил чата (виден всем и
                        используется как инструкция для ИИ)

Команда для всех:
    правила          — показать текущие правила чата

Про бесплатный хостинг на Render (Web Service):
- Бесплатный тариф Render ждёт, что приложение слушает HTTP-порт — сам бот
  порт не открывает (только VK Long Poll), поэтому ниже поднят простейший
  веб-сервер "для вида" (health-check) в отдельном потоке.
- Бесплатный тариф "засыпает" примерно через 15 минут без HTTP-запросов —
  чтобы бот не засыпал, настрой внешний пинг-сервис (например, UptimeRobot),
  который заходит на адрес сервиса каждые 5-10 минут.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

import storage
import ai_moderation


class _HealthCheckHandler(BaseHTTPRequestHandler):
    """Простейший обработчик: отвечает 200 OK на любой запрос.
    Нужен только для того, чтобы Render считал Web Service живым."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Бот работает".encode("utf-8"))

    def log_message(self, format, *args):
        pass  # не засоряем логи Render запросами health-check


def start_health_check_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Health-check сервер запущен на порту {port} (для Render Web Service)")


with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

# Секреты (токены) берутся из переменных окружения Render — так они не хранятся
# в самом коде на GitHub. Если переменной окружения нет (например, локальный
# тест), используется значение из config.json как запасной вариант.
VK_GROUP_TOKEN = os.environ.get("VK_GROUP_TOKEN") or CONFIG.get("vk_group_token")
GROUP_ID = int(os.environ.get("VK_GROUP_ID") or CONFIG.get("group_id", 0))
AI_API_KEY = os.environ.get("GROQ_API_KEY") or os.environ.get("AI_API_KEY") or CONFIG.get("ai", {}).get("api_key")

MODERATORS = set(CONFIG.get("moderators", []))
WARNINGS_BEFORE_BAN = CONFIG.get("warnings_before_ban", 3)
RULES_TEXT = CONFIG.get("rules_text", "").strip()
AI_CONFIG = CONFIG.get("ai", {"enabled": False})
AI_RULES_DESCRIPTION = ai_moderation.build_rules_description(RULES_TEXT)

if not VK_GROUP_TOKEN:
    raise RuntimeError("Не найден токен VK. Задай переменную окружения VK_GROUP_TOKEN.")
if not GROUP_ID:
    raise RuntimeError("Не найден group_id VK. Задай переменную окружения VK_GROUP_ID.")

vk_session = vk_api.VkApi(token=VK_GROUP_TOKEN)
vk = vk_session.get_api()
longpoll = VkBotLongPoll(vk_session, GROUP_ID)


def send_message(peer_id: int, text: str):
    vk.messages.send(
        peer_id=peer_id,
        message=text,
        random_id=int(time.time() * 1000) % (10**9)
    )


def delete_message(peer_id: int, message_id: int):
    try:
        vk.messages.delete(peer_id=peer_id, message_ids=message_id, delete_for_all=1)
    except vk_api.exceptions.ApiError as e:
        print(f"Не удалось удалить сообщение: {e}")


def kick_user(peer_id: int, user_id: int):
    chat_id = peer_id - 2000000000
    try:
        vk.messages.removeChatUser(chat_id=chat_id, member_id=user_id)
    except vk_api.exceptions.ApiError as e:
        print(f"Не удалось кикнуть пользователя: {e}")


def save_config():
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)


# ---------- Модерация ----------

def apply_punishment(action: str, peer_id: int, user_id: int, message_id: int, reason: str, text: str):
    action_taken = action

    if action in ("delete_and_warn", "warn"):
        if action == "delete_and_warn":
            delete_message(peer_id, message_id)
        warn_count = storage.add_warning(user_id)
        send_message(
            peer_id,
            f"[id{user_id}|Пользователь], предупреждение ({warn_count}/{WARNINGS_BEFORE_BAN}). "
            f"Причина: {reason}"
        )
        if warn_count >= WARNINGS_BEFORE_BAN:
            kick_user(peer_id, user_id)
            storage.reset_warnings(user_id)
            action_taken = "ban_after_warnings"
            send_message(peer_id, f"[id{user_id}|Пользователь] удалён из беседы за повторные нарушения.")

    elif action == "mute":
        delete_message(peer_id, message_id)
        send_message(
            peer_id,
            f"[id{user_id}|Пользователь] получает предупреждение с ограничением. Причина: {reason}"
        )

    elif action == "ban":
        kick_user(peer_id, user_id)
        send_message(peer_id, f"[id{user_id}|Пользователь] забанен. Причина: {reason}")

    storage.log_action(user_id, peer_id, message_id, action_taken, reason, text)


def handle_moderator_command(peer_id: int, user_id: int, text: str):
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()

    if cmd == "/log":
        rows = storage.get_recent_actions(20)
        if not rows:
            send_message(peer_id, "Лог пуст.")
            return
        lines = ["Последние действия бота:"]
        for row in rows:
            _id, ts, uid, action, reason, msg_text, reverted = row
            status = "ОТМЕНЕНО" if reverted else "активно"
            lines.append(f"#{_id} | id{uid} | {action} | {reason} | [{status}]")
        send_message(peer_id, "\n".join(lines))

    elif cmd == "/revert":
        if len(parts) < 2 or not parts[1].strip().isdigit():
            send_message(peer_id, "Использование: /revert <id_действия>. ID смотри в /log")
            return
        action_id = int(parts[1].strip())
        ok = storage.mark_reverted(action_id, user_id)
        send_message(peer_id, f"Действие #{action_id} отменено." if ok else "Действие с таким ID не найдено.")

    elif cmd == "/setrules":
        if len(parts) < 2 or not parts[1].strip():
            send_message(
                peer_id,
                "Использование: /setrules <текст правил>\n"
                "Каждый пункт — с новой строки (Shift+Enter). Текущие правила — командой «правила»."
            )
            return
        global RULES_TEXT, AI_RULES_DESCRIPTION
        RULES_TEXT = parts[1].strip()
        CONFIG["rules_text"] = RULES_TEXT
        AI_RULES_DESCRIPTION = ai_moderation.build_rules_description(RULES_TEXT)
        save_config()
        send_message(peer_id, "Правила чата обновлены. ИИ-модерация уже использует новый текст.")


def main():
    start_health_check_server()
    storage.init_db()
    print("Бот запущен, слушаю события...")

    for event in longpoll.listen():
        if event.type != VkBotEventType.MESSAGE_NEW:
            continue

        message = event.obj.message
        peer_id = message["peer_id"]
        user_id = message["from_id"]
        message_id = message["id"]
        text = message.get("text", "")

        if not text:
            continue

        # Команды модераторов
        if text.startswith("/") and user_id in MODERATORS:
            handle_moderator_command(peer_id, user_id, text)
            continue

        # Публичная команда — показать правила
        if text.strip().lower() in ("правила", "правила чата", "покажи правила"):
            if RULES_TEXT:
                send_message(peer_id, "📋 Правила чата:\n\n" + RULES_TEXT)
            else:
                send_message(peer_id, "Правила чата пока не заданы.")
            continue

        # Модераторов бот не наказывает
        if user_id in MODERATORS:
            continue

        if not AI_CONFIG.get("enabled"):
            continue

        ai_result = ai_moderation.check_with_ai(
            text, AI_RULES_DESCRIPTION, AI_API_KEY, AI_CONFIG["model"]
        )
        if ai_result["violation"]:
            apply_punishment(
                ai_result["action"] or "warn",
                peer_id, user_id, message_id,
                ai_result["reason"], text
            )


if __name__ == "__main__":
    main()
