import os
import threading
import time
import requests
from aiohttp import web
from groq import Groq

# Настройки подключения
KEEP_ALIVE_URL = "https://vk-mod-bot-e8ee.onrender.com"
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CONFIRMATION_CODE = os.getenv("VK_CALLBACK_CONFIRMATION", "b84ecffe")

# Проверка наличия API ключа
if not GROQ_API_KEY:
    print("Ошибка: GROQ_API_KEY не найден в переменных окружения!")
    exit(1)

client = Groq(api_key=GROQ_API_KEY)

# Keep-Alive для Render
def keep_alive():
    while True:
        try:
            requests.get(KEEP_ALIVE_URL, timeout=5)
            print("Keep-alive ping sent")
        except Exception as e:
            print(f"Keep-alive error: {e}")
        time.sleep(300)

threading.Thread(target=keep_alive, daemon=True).start()

# Обработчик подтверждения
async def handle_post(request):
    try:
        data = await request.json()
        req_type = data.get('type')
        
        if req_type == 'confirmation':
            return web.Response(text=CONFIRMATION_CODE)
        
        # Обработка сообщений
        if req_type == 'message_new':
            obj = data.get('object', {})
            message = obj.get('message', {})
            text = message.get('text', '')
            peer_id = message.get('peer_id')
            
            # Простая обработка сообщений через ИИ
            try:
                response = client.chat.completions.create(
                    model="llama3-1-8b-instruct",
                    messages=[
                        {"role": "system", "content": "Ты — дружелюбный бот-помощник."},
                        {"role": "user", "content": text}
                    ],
                    temperature=0.7,
                    max_tokens=512
                )
                ai_response = response.choices[0].message.content
                
                # Отправляем ответ в ВК
                # Здесь нужно добавить код для отправки сообщения через VK API
                # Но для минимального запуска пока пропустим эту часть
                
            except Exception as e:
                print(f"Ошибка ИИ: {e}")
                ai_response = "Сейчас я немного задумался, попробуйте позже!"
            
            # TODO: Добавить отправку ответа через VK API
            
        return web.Response(text='ok')

    except Exception as e:
        print(f"Ошибка обработки: {e}")
        return web.Response(status=500)

# Запуск сервера
app = web.Application()
app.router.add_post('/', handle_post)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8080))
    print(f"Сервер запущен на порту {port}")
    web.run_app(app, port=port)
