"""
ИИ-модерация сообщений — единственный уровень проверки.

Каждое сообщение (кроме сообщений модераторов) отправляется в ИИ через Groq
вместе с текстом правил чата (config.json -> rules_text). Модель отвечает
строго в JSON: нарушение или нет, причина, рекомендуемое действие. Бот
применяет это действие.

Используется Groq (console.groq.com) — щедрый бесплатный тариф.
Модели по умолчанию: openai/gpt-oss-20b (быстрая) или openai/gpt-oss-120b (умнее).

Требуется: pip install requests
И ключ API в переменной окружения GROQ_API_KEY (или config.json -> ai.api_key)
"""

import json
import requests

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = """Ты — модератор чата. Тебе присылают одно сообщение из беседы
и список правил этого чата. Твоя задача — решить, нарушает ли сообщение эти
правила, учитывая контекст, скрытый смысл, завуалированные оскорбления,
попытки обойти фильтр слов (замена букв, пробелы, транслит) и т.п.

Отвечай СТРОГО в формате JSON, без каких-либо пояснений вокруг:
{
  "violation": true/false,
  "reason": "краткая причина на русском, если violation=true, иначе пустая строка",
  "action": "warn" | "delete_and_warn" | "mute"
}

Бан/исключение из чата запрещён как действие — самое строгое, что ты можешь
предложить, это "mute" (временное ограничение). Если сомневаешься — ставь
violation=false (не наказывай без явных оснований).
"""


def _call_groq(text: str, rules_description: str, api_key: str, model: str) -> dict:
    """Один запрос к Groq на конкретной модели. Бросает исключение при ошибке —
    вызывающий код сам решает, пробовать ли резервную модель."""
    user_prompt = f"""Правила чата:
{rules_description}

Сообщение пользователя для проверки:
\"\"\"{text}\"\"\"
"""
    response = requests.post(
        GROQ_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 200,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        },
        timeout=15,
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    parsed = json.loads(raw)
    action = parsed.get("action", "warn")
    if action not in ("warn", "delete_and_warn", "mute"):
        # Подстраховка: бота нельзя использовать для бана/кика, даже если
        # модель вдруг предложит его вопреки инструкции в промпте.
        action = "mute"
    return {
        "violation": bool(parsed.get("violation", False)),
        "reason": parsed.get("reason", "") or "нарушение по оценке ИИ",
        "action": action,
    }


def check_with_ai(text: str, rules_description: str, api_key: str, model: str, fallback_model: str = None) -> dict:
    """
    Возвращает dict: {"violation": bool, "reason": str, "action": str}

    Сначала пробует основную модель (model). Если Groq вернул ошибку 429
    (лимит запросов/токенов исчерпан) и задан fallback_model — сразу пробует
    ту же проверку на резервной модели.
    При любой другой ошибке или если резерв тоже не сработал — считает, что
    нарушения нет (fail-safe, не банит зря).
    """
    try:
        return _call_groq(text, rules_description, api_key, model)
    except requests.exceptions.HTTPError as e:
        is_rate_limit = e.response is not None and e.response.status_code == 429
        if is_rate_limit and fallback_model and fallback_model != model:
            print(f"[ai_moderation] Лимит модели {model} исчерпан, пробуем {fallback_model}")
            try:
                return _call_groq(text, rules_description, api_key, fallback_model)
            except Exception as e2:
                print(f"[ai_moderation] Резервная модель тоже недоступна, пропускаем проверку: {e2}")
                return {"violation": False, "reason": "", "action": None}
        print(f"[ai_moderation] Ошибка обращения к ИИ, пропускаем проверку: {e}")
        return {"violation": False, "reason": "", "action": None}
    except Exception as e:
        print(f"[ai_moderation] Ошибка обращения к ИИ, пропускаем проверку: {e}")
        return {"violation": False, "reason": "", "action": None}


def build_rules_description(rules_text: str) -> str:
    """Правила для промпта — берутся из config.json -> rules_text (то же самое,
    что видят участники по команде «правила»). Если не заданы — общее описание."""
    rules_text = (rules_text or "").strip()
    if rules_text:
        return rules_text
    return (
        "Запрещена скрытая реклама, спам, токсичное поведение, оскорбления "
        "участников, а также мат и запрещённый контент."
    )
