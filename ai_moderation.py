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
  "action": "warn" | "delete_and_warn" | "mute" | "ban"
}

Если сомневаешься — ставь violation=false (не наказывай без явных оснований).
"""


def check_with_ai(text: str, rules_description: str, api_key: str, model: str) -> dict:
    """
    Возвращает dict: {"violation": bool, "reason": str, "action": str}
    При любой ошибке API — считает, что нарушения нет (fail-safe, не банит зря).
    """
    user_prompt = f"""Правила чата:
{rules_description}

Сообщение пользователя для проверки:
\"\"\"{text}\"\"\"
"""

    try:
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
        return {
            "violation": bool(parsed.get("violation", False)),
            "reason": parsed.get("reason", "") or "нарушение по оценке ИИ",
            "action": parsed.get("action", "warn"),
        }
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
