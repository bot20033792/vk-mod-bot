import os
import threading
import time
import requests

# --- НАЧАЛО БЛОКА ДЛЯ ПОДДЕРЖКИ ОНЛАЙНА ---
def keep_alive():
    while True:
        try:
            url = "https://vk-mod-bot-e8ee.onrender.com"
            requests.get(url, timeout=5)
            print("Keep-alive ping sent")
        except Exception as e:
            print(f"Keep-alive error: {e}")
        time.sleep(300)  # Пингуем каждые 5 минут

threading.Thread(target=keep_alive, daemon=True).start()
# --- КОНЕЦ БЛОКА ---



"""
VK-бот: автомодератор на чистом ИИ + ранги модераторов + профиль/статистика.

Архитектура — Callback API (а не Long Poll):
- VK сам отправляет HTTP POST на наш сервер при каждом новом сообщении,
  вместо того чтобы бот сам постоянно спрашивал VK "есть новое?".
- Это нужно из-за бесплатного тарифа Render: там в любом случае нужен
  открытый HTTP-порт, а Long Poll-цикл никак не совместим с тем, что
  Render "усыпляет" процесс — усыплённый процесс не может сам стучаться
  наружу. С Callback API входящий запрос от VK — это как раз то, что
  будит спящий сервис.
- Настройка на стороне VK: Группа -> Управление -> Работа с API ->
  Callback API -> Адрес: https://<твой-сервис>.onrender.com/callback
  Код подтверждения и секретный ключ — в config.json (или переменных
  окружения VK_CALLBACK_CONFIRMATION / VK_CALLBACK_SECRET).

Как работает модерация:
- Каждое сообщение (кроме сообщений модераторов и замьюченных участников)
  отправляется в ИИ (Groq) вместе с текстом правил чата (config.json ->
  rules_text).
- ИИ решает: есть нарушение или нет, и если да — какое действие применить
  (warn / delete_and_warn / mute).
- Бот НИКОГО не банит и не кикает — максимум наказание это временный мут.
- Повторные предупреждения тоже превращаются в мут, а не в исключение.
- Репост наказывается отдельно и без ИИ — сразу удаление + предупреждение.
- Каждое действие бота пишется в лог, модераторы могут посмотреть и отменить.
- При каждом наказании бот шлёт личное уведомление всем модераторам.

Нашивки и антинашивки выдаются автоматически после каждого события.
Ранги модераторов: уровни 1-5.
"""

import json
import os
import random
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import vk_api

import storage
import ai_moderation
import ranks
import achievements

with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

VK_GROUP_TOKEN = os.environ.get("VK_GROUP_TOKEN") or CONFIG.get("vk_group_token")
GROUP_ID = int(os.environ.get("VK_GROUP_ID") or CONFIG.get("group_id", 0))
AI_API_KEY = os.environ.get("GROQ_API_KEY") or os.environ.get("AI_API_KEY") or CONFIG.get("ai", {}).get("api_key")

CONFIRMATION_CODE = os.environ.get("VK_CALLBACK_CONFIRMATION") or CONFIG.get("callback_confirmation_code")
CALLBACK_SECRET = os.environ.get("VK_CALLBACK_SECRET") or CONFIG.get("callback_secret")

MODERATORS = {m["id"]: m["level"] for m in CONFIG.get("moderators", []) if m.get("id")}
WARNINGS_BEFORE_MUTE = CONFIG.get("warnings_before_mute", 3)
MUTE_DURATION_MINUTES = CONFIG.get("mute_duration_minutes", 20)
RULES_TEXT = CONFIG.get("rules_text", "").strip()
AI_CONFIG = CONFIG.get("ai", {"enabled": False})
AI_RULES_DESCRIPTION = ai_moderation.build_rules_description(RULES_TEXT)

if not VK_GROUP_TOKEN:
    raise RuntimeError("Не найден токен VK. Задай переменную окружения VK_GROUP_TOKEN.")
if not GROUP_ID:
    raise RuntimeError("Не найден group_id VK. Задай переменную окружения VK_GROUP_ID.")
if not CONFIRMATION_CODE:
    raise RuntimeError(
        "Не найден код подтверждения Callback API. Задай VK_CALLBACK_CONFIRMATION "
        "или config.json -> callback_confirmation_code."
    )

vk_session = vk_api.VkApi(token=VK_GROUP_TOKEN)
vk = vk_session.get_api()


def save_config():
    CONFIG["moderators"] = [{"id": uid, "level": lvl} for uid, lvl in MODERATORS.items()]
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)


def resolve_moderators():
    global MODERATORS
    changed = False
    for m in CONFIG.get("moderators", []):
        if m.get("id") or not m.get("screen_name"):
            continue
        try:
            resolved = vk.utils.resolveScreenName(screen_name=m["screen_name"])
            if resolved and resolved.get("type") == "user":
                m["id"] = resolved["object_id"]
                changed = True
                print(f"[moderators] {m['screen_name']} -> id{m['id']}")
            else:
                print(f"[moderators] Не удалось определить ID для {m['screen_name']}")
        except vk_api.exceptions.ApiError as e:
            print(f"[moderators] Ошибка resolveScreenName для {m['screen_name']}: {e}")
    if changed:
        MODERATORS = {m["id"]: m["level"] for m in CONFIG.get("moderators", []) if m.get("id")}
        save_config()


resolve_moderators()


def notify_staff(user_id: int, message_text: str, reason: str, action_taken: str):
    text = (
        f"🚨 Нарушение в чате\n"
        f"Участник: [id{user_id}|id{user_id}] (vk.com/id{user_id})\n"
        f"Действие бота: {action_taken}\n"
        f"Причина: {reason}\n"
        f"Сообщение: {(message_text or '')[:300]}"
    )
    for staff_id in MODERATORS:
        try:
            vk.messages.send(user_id=staff_id, message=text, random_id=random.randint(1, 2**31 - 1))
        except vk_api.exceptions.ApiError as e:
            print(f"[notify_staff] Не удалось написать модератору id{staff_id}: {e}")


def announce_new_achievements(peer_id: int, user_id: int, new_items):
    for kind, code, name in new_items:
        if kind == "achievement":
            send_message(peer_id, f"🏆 [id{user_id}|Участник] получает нашивку: {name}!")
        else:
            send_message(peer_id, f"⚠️ [id{user_id}|Участнику] выдана антинашивка: {name}.")


def is_repost(message: dict) -> bool:
    for att in message.get("attachments", []):
        if att.get("type") == "wall":
            return True
    return False


def send_message(peer_id: int, text: str):
    vk.messages.send(
        peer_id=peer_id,
        message=text,
        random_id=random.randint(1, 2**31 - 1)
    )


def delete_message(peer_id: int, message_id: int):
    try:
        vk.messages.delete(peer_id=peer_id, message_ids=message_id, delete_for_all=1)
    except vk_api.exceptions.ApiError as e:
        print(f"Не удалось удалить сообщение: {e}")


# ---------- Модерация ----------

def apply_mute(peer_id: int, user_id: int, reason: str):
    until_ts = time.time() + MUTE_DURATION_MINUTES * 60
    storage.set_mute(user_id, until_ts)
    storage.reset_warnings(user_id)
    storage.increment_stat(user_id, "total_mutes_ever")
    send_message(
        peer_id,
        f"[id{user_id}|Пользователь] получает мут на {MUTE_DURATION_MINUTES} мин. "
        f"Причина: {reason}. В это время его сообщения будут удаляться."
    )


def apply_punishment(action: str, peer_id: int, user_id: int, message_id: int, reason: str, text: str):
    action_taken = action

    if action in ("delete_and_warn", "warn"):
        if action == "delete_and_warn":
            delete_message(peer_id, message_id)
        warn_count = storage.add_warning(user_id)
        storage.increment_stat(user_id, "total_warnings_ever")
        send_message(
            peer_id,
            f"[id{user_id}|Пользователь], предупреждение ({warn_count}/{WARNINGS_BEFORE_MUTE}). "
            f"Причина: {reason}"
        )
        if warn_count >= WARNINGS_BEFORE_MUTE:
            apply_mute(peer_id, user_id, "накопились предупреждения")
            action_taken = "mute_after_warnings"

    elif action == "mute":
        delete_message(peer_id, message_id)
        apply_mute(peer_id, user_id, reason)

    storage.log_action(user_id, peer_id, message_id, action_taken, reason, text)
    notify_staff(user_id, text, reason, action_taken)

    new_items = achievements.check_achievements(user_id)
    announce_new_achievements(peer_id, user_id, new_items)


# ---------- Команды модераторов ----------

def handle_moderator_command(peer_id: int, user_id: int, text: str, reply_to_user_id):
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

    elif cmd == "/setrank":
        if len(parts) < 2:
            send_message(
                peer_id,
                "Использование:\n"
                "/setrank <уровень> — ответом на сообщение участника\n"
                "/setrank <id> <уровень> — по числовому ID\n"
                "Уровни: 1-Модератор, 2-Ст.модератор, 3-Мл.админ, 4-Ст.админ, 5-Владелец"
            )
            return

        arg = parts[1].strip()
        if arg.isdigit() and reply_to_user_id is not None:
            target_id = reply_to_user_id
            level_str = arg
        else:
            arg_parts = arg.split(maxsplit=1)
            if len(arg_parts) < 2 or not arg_parts[0].isdigit():
                send_message(peer_id, "Не понял команду. Либо ответь на сообщение участника и напиши "
                                       "/setrank <уровень>, либо укажи /setrank <id> <уровень>.")
                return
            target_id = int(arg_parts[0])
            level_str = arg_parts[1].strip()

        if not level_str.isdigit() or not (1 <= int(level_str) <= 5):
            send_message(peer_id, "Уровень должен быть числом от 1 до 5.")
            return
        new_level = int(level_str)

        actor_level = MODERATORS.get(user_id, 0)
        current_target_level = MODERATORS.get(target_id, 0)

        if not ranks.can_manage(actor_level, new_level) or not ranks.can_manage(actor_level, current_target_level):
            send_message(peer_id, "Недостаточно прав, чтобы назначить этот ранг.")
            return

        MODERATORS[target_id] = new_level
        save_config()
        send_message(peer_id, f"[id{target_id}|Участник] теперь имеет ранг «{ranks.title(new_level)}».")


# ---------- Публичные команды ----------

def handle_public_command(peer_id: int, user_id: int, lowered_text: str) -> bool:
    if lowered_text in ("правила", "правила чата", "покажи правила"):
        if RULES_TEXT:
            send_message(peer_id, "📋 Правила чата:\n\n" + RULES_TEXT)
        else:
            send_message(peer_id, "Правила чата пока не заданы.")
        return True

    if lowered_text in ("мой профиль", "профиль"):
        total = storage.get_user_total_messages(user_id)
        activity_rank = ranks.get_activity_rank(total)
        lines = [
            f"👤 Профиль [id{user_id}|участника]:",
            f"Сообщений всего: {total}",
            f"Нашивка: {activity_rank}",
        ]
        level = MODERATORS.get(user_id)
        if level:
            lines.append(f"Ранг модератора: {ranks.title(level)}")
        mute_until = storage.get_mute_until(user_id)
        if mute_until:
            minutes_left = max(0, int((mute_until - time.time()) / 60) + 1)
            lines.append(f"⛔ В муте ещё ~{minutes_left} мин.")
        lines.append(achievements.format_profile_block(user_id))
        send_message(peer_id, "\n".join(lines))
        return True

    if lowered_text in ("статистика", "статистика чата"):
        top = storage.get_activity_last_hours(24, 15)
        if not top:
            send_message(peer_id, "За последние 24 часа сообщений не было.")
            return True
        lines = ["📊 Активность за последние 24 часа:"]
        for i, (uid, count) in enumerate(top, start=1):
            lines.append(f"{i}. [id{uid}|участник] — {count} сообщ.")
        send_message(peer_id, "\n".join(lines))
        return True

    return False


# ---------- Обработка одного сообщения ----------

def process_message(message: dict):
    peer_id = message["peer_id"]
    user_id = message["from_id"]
    message_id = message["id"]
    text = message.get("text", "")

    reply = message.get("reply_message")
    reply_to_user_id = reply["from_id"] if reply else None

    if not text:
        return

    lowered = text.strip().lower()

    if text.startswith("/") and user_id in MODERATORS:
        handle_moderator_command(peer_id, user_id, text, reply_to_user_id)
        return

    if handle_public_command(peer_id, user_id, lowered):
        return

    prev_total = storage.get_user_total_messages(user_id)
    new_total = storage.bump_message_count(user_id, time.time())
    prev_threshold, _ = ranks.get_activity_rank_at(prev_total)
    new_threshold, new_rank_name = ranks.get_activity_rank_at(new_total)
    if new_threshold > prev_threshold:
        send_message(
            peer_id,
            f"🎉 [id{user_id}|Участник] получает новую нашивку: {new_rank_name} "
            f"({new_total} сообщ.)!"
        )

    announce_new_achievements(peer_id, user_id, achievements.check_achievements(user_id))

    if user_id in MODERATORS:
        return

    mute_until = storage.get_mute_until(user_id)
    if mute_until:
        delete_message(peer_id, message_id)
        return

    if is_repost(message):
        storage.increment_stat(user_id, "total_reposts_ever")
        apply_punishment(
            "delete_and_warn", peer_id, user_id, message_id,
            "публикация записи/поста из чужого паблика или канала (п. 3.1 правил чата)",
            text
        )
        return

    if not AI_CONFIG.get("enabled"):
        return

    ai_result = ai_moderation.check_with_ai(
        text, AI_RULES_DESCRIPTION, AI_API_KEY,
        AI_CONFIG.get("model", "openai/gpt-oss-20b"),
        AI_CONFIG.get("fallback_model")
    )
    if ai_result["violation"]:
        apply_punishment(
            ai_result["action"] or "warn",
            peer_id, user_id, message_id,
            ai_result["reason"], text
        )


def process_message_safe(message: dict):
    try:
        process_message(message)
    except Exception as e:
        print(f"[process_message] Необработанная ошибка: {e}")
        traceback.print_exc()


# ---------- HTTP-сервер ----------

class VkCallbackHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Бот работает".encode("utf-8"))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                event = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                print("[callback] Не удалось разобрать JSON от VK, тело запроса:", raw[:500])
                self.send_response(400)
                self.end_headers()
                return

            event_type = event.get("type")
            print(f"[callback] Получено событие от VK: {event_type}")

            if event_type == "confirmation":
                self.send_response(200)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(CONFIRMATION_CODE.encode("utf-8"))
                print(f"[callback] Отправлен код подтверждения: {CONFIRMATION_CODE}")
                return

            if CALLBACK_SECRET and event.get("secret") != CALLBACK_SECRET:
                print("[callback] Событие с неверным secret — игнорируем")
                self._respond_ok()
                return

            self._respond_ok()

            if event_type == "message_new":
                message = event.get("object", {}).get("message", {})
                if not message:
                    print(f"[callback] message_new: пустой объект message, raw: {raw[:500]}")
                    return
                print(f"[callback] message_new: from_id={message.get('from_id')} "
                      f"text={message.get('text')!r}")
                threading.Thread(target=process_message_safe, args=(message,), daemon=True).start()
            else:
                print(f"[callback] Необрабатываемый тип события: {event_type}")
        except Exception:
            print("[callback] Необработанная ошибка в do_POST:")
            traceback.print_exc()
            try:
                self._respond_ok()
            except Exception:
                pass

    def _respond_ok(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        if self.command == "GET":
            return
        print("[http] " + (format % args))


def main():
    storage.init_db()
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), VkCallbackHandler)
    print(f"Бот запущен (Callback API), слушаю порт {port}...")
    print(f"GROUP_ID={GROUP_ID}, модераторов={len(MODERATORS)}, ИИ включён={AI_CONFIG.get('enabled', False)}")
    print(f"Адрес для настройки в VK: https://<твой-сервис>.onrender.com/ (любой путь)")
    server.serve_forever()


if __name__ == "__main__":
    main()
