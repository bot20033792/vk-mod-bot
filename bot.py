import os
import threading
import time
import requests
import json
import random
from aiohttp import web
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

# --- БЛОК ДЛЯ ПОДДЕРЖКИ ОНЛАЙНА ---
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

# Настройки
VK_GROUP_TOKEN = os.getenv("VK_GROUP_TOKEN")
GROUP_ID = int(os.getenv("GROUP_ID", 0))
CONFIRMATION_CODE = os.getenv("VK_CALLBACK_CONFIRMATION", "b84ecffe")

# Инициализация VK API
vk_session = vk_api.VkApi(token=VK_GROUP_TOKEN)
vk = vk_session.get_api()

async def handle_post(request):
    try:
        data = await request.json()
        
        # Обработка подтверждения
        if data.get('type') == 'confirmation':
            return web.Response(text=CONFIRMATION_CODE)
        
        # Обработка сообщений
        if data.get('type') == 'message_new':
            message_event = data['object']['message']
            # Здесь твоя логика обработки сообщений
            
        return web.Response(text='ok')
    
    except Exception as e:
        print(f"Ошибка обработки запроса: {e}")
        return web.Response(status=500)

async def health_check(request):
    return web.Response(text="Бот работает")

app = web.Application()
app.router.add_post('/', handle_post)
app.router.add_get('/', health_check)

if __name__ == '__main__':
    print("Бот запущен")
    web.run_app(app, port=os.getenv('PORT', 8080))
