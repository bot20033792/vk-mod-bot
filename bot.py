"""
VK-бот: автомодератор на чистом ИИ + ранги модераторов + профиль/статистика.

Архитектура — Callback API (а не Long Poll):
- VK сам отправляет HTTP POST на наш сервер при каждом новом сообщении,
  вместо того чтобы бот сам постоянно спрашивал VK "есть новое?".
- Это нужно из-за бесплатного тарифа Render: там в любом случае нужен
  открытый HTTP-порт, а Long Poll-цикл никак не совместим с тем, что
  Render "усыпляет" процесс — усыплённый процесс не может сам стучаться
  наружу. С Callback API входящий запрос от VK — это как раз то, что
  будит спящий сервис (см. подробности в конце файла).
- Настройка на стороне VK: Группа -> Управление -> Работа с API ->
  Callback API -> Адрес: https://<твой-сервис>.onrender.com/callback
  (путь может быть любым — сервер отвечает на POST на любой адрес).
  Код подтверждения и секретный ключ — в config.json (или переменных
  окружения VK_CALLBACK_CONFIRMATION / VK_CALLBACK_SECRET).

Как работает модерация:
- Каждое сообщение (кроме сообщений модераторов и замьюченных участников)
  отправляется в ИИ (Groq) вместе с текстом правил чата (config.json ->
  rules_text).
- ИИ решает: есть нарушение или нет, и если да — какое действие применить
  (warn / delete_and_warn / mute).
- Бот НИКОГО не банит и не кикает — максимум наказание это временный мут
  (симулируется удалением сообщений замьюченного, т.к. VK Bot API не умеет
  native-мут для бесед). Длительность мута фиксированная, задаётся в
  config.json -> mute_duration_minutes (для теста — 20 минут).
- Повторные предупреждения (warnings_before_mute штук) тоже превращаются
  в мут, а не в исключение из чата.
- Репост (пересланная запись со стены чужого паблика/канала) наказывается
  отдельно и без ИИ — сразу удаление + предупреждение со ссылкой на п. 3.1.
- Каждое действие бота пишется в лог (storage.actions_log), модераторы могут
  посмотреть его через /log и отменить через /revert.
- При каждом наказании бот шлёт личное уведомление всем модераторам/
  администраторам/владельцу (notify_staff).
- Если ИИ недоступен или вернул ошибку — считается, что нарушения нет
  (fail-safe, бот не наказывает "на всякий случай").

Нашивки и антинашивки (achievements.py) выдаются полностью автоматически
после каждого события и хранятся навсегда (см. сам файл achievements.py).
Отдельно есть нашивки активности (ranks.ACTIVITY_RANKS) по числу сообщений.

Ранги модераторов (config.json -> moderators, уровни 1-5):
    1 — Модератор
    2 — Старший модератор
    3 — Младший администратор
    4 — Старший администратор
    5 — Владелец
Управлять рангом (своим и чужим) может только тот, чей уровень строго выше.
Модератора можно указать по числовому ID ({"id": ..., "level": ...}) или по
короткому имени страницы ({"screen_name": "...", "level": ...}) — во втором
случае бот сам определит числовой ID при старте и допишет его в config.json.

Команды модераторов (доступны только участникам из moderators):
    /log                    — последние 20 действий бота
    /revert <id>            — отменить действие бота
    /setrules <текст>       — задать/сменить текст правил чата (виден всем и
                              используется как инструкция для ИИ)
    /setrank <id> <уровень> — назначить ранг по числовому ID
    /setrank <уровень>      — назначить ранг участнику, чьё сообщение
                              зацитировано (ответом на его сообщение)

Команды для всех:
    правила       — показать текущие правила чата
    мой профиль   — своя статистика + нашивки/антинашивки
    статистика    — топ активности в чате за последние 24 часа

Про бесплатный хостинг на Render (Web Service) и "засыпание":
- Бесплатный тариф Render усыпляет процесс примерно через 15 минут без
  входящих HTTP-запросов. Переход на Callback API снижает риск (VK сам
  стучится к нам при каждом сообщении и разбудит спящий сервис), но первое
  сообщение после сна всё равно может прийти с задержкой в 30-60 секунд,
  пока Render поднимает контейнер.
- Чтобы сервис вообще не засыпал — дополнительно подключи бесплатный
  пинг-сервис (UptimeRobot / cron-job.org), который заходит на GET-адрес
  сервиса каждые 5-10 минут. Это делает работу бота стабильной 24/7, а не
  "лучше, чем было".
"""

import json
import os
import random
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

import vk_api

import storage
import ai_moderation
import ranks
import achievements

with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

# Секреты (токены) берутся из переменных окружения Render — так они не хранятся
# в самом коде на GitHub. Если переменной окружения нет (например, локальный
# тест), используется значение из config.json как запасной вариант.
VK_GROUP_TOKEN = os.environ.get("VK_GROUP_TOKEN") or CONFIG.get("vk_group_token")
GROUP_ID = int(os.environ.get("VK_GROUP_ID") or CONFIG.get("group_id", 0))
AI_API_KEY = os.environ.get("GROQ_API_KEY") or os.environ.get("AI_API_KEY") or CONFIG.get("ai", {}).get("api_key")

# Данные Callback API (см. вкладку "Callback API" в настройках группы VK).
# CONFIRMATION_CODE — строка, которую VK просит вернуть при подтверждении
# адреса сервера ("Строка, которую должен вернуть сервер" на скрине).
# CALLBACK_SECRET — необязательный секретный ключ; если задан в VK, VK кладёт
# его в каждое событие (event["secret"]), и мы можем проверить, что запрос
# действительно пришёл от VK, а не от кого-то постороннего.
CONFIRMATION_CODE = os.environ.get("VK_CALLBACK_CONFIRMATION") or CONFIG.get("callback_confirmation_code")
CALLBACK_SECRET = os.environ.get("VK_CALLBACK_SECRET") or CONFIG.get("callback_secret")

# moderators: список объектов {"id":.., "level":..} (или {"screen_name":.., "level":..},
# если числовой ID ещё не известен — см. resolve_moderators() ниже) -> словарь id -> level
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
        "или config.json -> callback_confirmation_code (смотри вкладку Callback API в группе VK)."
    )

vk_session = vk_api.VkApi(token=VK_GROUP_TOKEN)
vk = vk_session.get_api()


def save_config():
    CONFIG["moderators"] = [{"id": uid, "level": lvl} for uid, lvl in MODERATORS.items()]
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)


def resolve_moderators():
    """Даёт возможность указать модератора в config.json по короткому имени
    страницы (screen_name, например "lomtev_shadow12"), если числовой ID
    ещё не известен. При старте бот сам определяет ID через VK API
    (utils.resolveScreenName) и дозаписывает его в config.json — дальше
    используется уже сохранённый числовой ID."""
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
                print(f"[moderators] Не удалось определить ID для {m['screen_name']}, "
                      f"проверь имя страницы")
        except vk_api.exceptions.ApiError as e:
            print(f"[moderators] Ошибка resolveScreenName для {m['screen_name']}: {e}")
    if changed:
        MODERATORS = {m["id"]: m["level"] for m in CONFIG.get("moderators", []) if m.get("id")}
        save_config()


resolve_moderators()


def notify_staff(user_id: int, message_text: str, reason: str, action_taken: str):
    """Личное уведомление каждому модератору/администратору/владельцу
    (все, кто есть в MODERATORS) о нарушении — сразу, автоматически."""
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
    """Объявляет в чате о новых нашивках/антинашивках сразу после того,
    как achievements.check_achievements() их выдал."""
    for kind, code, name in new_items:
        if kind == "achievement":
            send_message(peer_id, f"🏆 [id{user_id}|Участник] получает нашивку: {name}!")
        else:
            send_message(peer_id, f"⚠️ [id{user_id}|Участнику] выдана антинашивка: {name}.")


def is_repost(message: dict) -> bool:
    """True, если сообщение — пересланная запись (репост) со стены другого
    паблика/канала, а не собственный текст автора."""
    for att in message.get("attachments", []):
        if att.get("type") == "wall":
            return True
    return False


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


# ---------- Модерация (только предупреждение / мут, никогда бан) ----------

def apply_mute(peer_id: int, user_id: int, reason: str):
    """Мут симулируется: VK Bot API не даёт запретить писать конкретному
    человеку в беседе, поэтому все его сообщения будут молча удаляться,
    пока не истечёт until_ts."""
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

    # Уведомляем модераторов/администратора/владельца в личку — автоматически
    notify_staff(user_id, text, reason, action_taken)

    # Проверяем и сразу выдаём (навсегда) новые нашивки/антинашивки
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
        # Вариант "/setrank 3" ответом на чьё-то сообщение
        if arg.isdigit() and reply_to_user_id is not None:
            target_id = reply_to_user_id
            level_str = arg
        else:
            # Вариант "/setrank <id> <уровень>"
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


# ---------- Публичные команды (доступны всем) ----------

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


# ---------- Обработка одного сообщения (вызывается из Callback API) ----------

def process_message(message: dict):
    """Та же логика, что раньше крутилась в цикле Long Poll — теперь
    вызывается один раз на каждое HTTP-событие message_new от VK."""
    peer_id = message["peer_id"]
    user_id = message["from_id"]
    message_id = message["id"]
    text = message.get("text", "")

    reply = message.get("reply_message")
    reply_to_user_id = reply["from_id"] if reply else None

    if not text:
        return

    lowered = text.strip().lower()

    # Команды модераторов
    if text.startswith("/") and user_id in MODERATORS:
        handle_moderator_command(peer_id, user_id, text, reply_to_user_id)
        return

    # Публичные команды (правила / профиль / статистика)
    if handle_public_command(peer_id, user_id, lowered):
        return

    # Учитываем активность для статистики и нашивок (включая модераторов)
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

    # Нашивки/антинашивки за общее число сообщений (например "Ветеран чата",
    # "Чистая репутация") проверяем после каждого сообщения — независимо
    # от нашивок активности выше.
    announce_new_achievements(peer_id, user_id, achievements.check_achievements(user_id))

    # Модераторов бот не наказывает
    if user_id in MODERATORS:
        return

    # Если пользователь замьючен — просто молча удаляем сообщение,
    # без повторного обращения к ИИ и без спама предупреждениями
    mute_until = storage.get_mute_until(user_id)
    if mute_until:
        delete_message(peer_id, message_id)
        return

    # Репост (пересланная запись) из чужого паблика/канала — запрещено
    # правилами (п. 3.1), проверяется без ИИ: удаляем, предупреждаем,
    # указываем конкретный пункт правил.
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
        AI_CONFIG["model"], AI_CONFIG.get("fallback_model")
    )
    if ai_result["violation"]:
        apply_punishment(
            ai_result["action"] or "warn",
            peer_id, user_id, message_id,
            ai_result["reason"], text
        )


def process_message_safe(message: dict):
    """Обёртка для запуска process_message в отдельном потоке — исключение
    внутри не должно "тихо" убивать поток без следа в логах."""
    try:
        process_message(message)
    except Exception as e:
        print(f"[process_message] Необработанная ошибка: {e}")


# ---------- HTTP-сервер: Callback API + health-check ----------

class VkCallbackHandler(BaseHTTPRequestHandler):
    """Принимает события Callback API от VK (POST) и заодно отвечает на
    обычный GET — это нужно и для проверки в браузере, и для пинг-сервисов
    (UptimeRobot и т.п.), которые не дают Render усыпить процесс."""

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

            # Подтверждение адреса сервера — обязательный первый шаг настройки
            # Callback API в группе VK. Отвечаем строкой без кавычек, как требует VK.
            if event_type == "confirmation":
                self.send_response(200)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(CONFIRMATION_CODE.encode("utf-8"))
                return

            # Если задан секретный ключ в config.json — проверяем, что событие
            # действительно от VK (защита от посторонних запросов на этот адрес).
            if CALLBACK_SECRET and event.get("secret") != CALLBACK_SECRET:
                print("[callback] Событие с неверным secret — игнорируем")
                self._respond_ok()
                return

            # Отвечаем VK "ok" сразу же, не дожидаясь обработки (проверка ИИ может
            # занять пару секунд) — иначе VK решит, что доставка не удалась, и
            # будет повторно слать то же событие.
            self._respond_ok()

            if event_type == "message_new":
                message = event.get("object", {}).get("message", {})
                print(f"[callback] message_new: from_id={message.get('from_id')} "
                      f"text={message.get('text')!r}")
                threading.Thread(target=process_message_safe, args=(message,), daemon=True).start()
            # Остальные типы событий (message_edit, group_join и т.п.) пока не обрабатываем.
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
        # Не засоряем логи запросами health-check пинг-сервиса (GET), но
        # НЕ глушим ничего для POST — там могут быть реальные ошибки.
        if self.command == "GET":
            return
        print("[http] " + (format % args))


def main():
    storage.init_db()
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), VkCallbackHandler)
    print(f"Бот запущен (Callback API), слушаю порт {port}...")
    print("Адрес для настройки в VK (Callback API): https://<твой-сервис>.onrender.com/ (любой путь)")
    server.serve_forever()


if __name__ == "__main__":
    main()
