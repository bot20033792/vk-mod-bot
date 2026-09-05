import os
import threading
import time
import requests
from aiohttp import web
import vk_api

# ---------------------------------------------------------
# 1. БЛОК ПОДДЕРЖКИ ОНЛАЙНА (Keep-Alive)
# ---------------------------------------------------------
# Этот поток каждые 5 минут стучится в сам себя, чтобы Render не усыплял сервис.
def keep_alive():
    while True:
        try:
            # Вставь сюда свой URL на Render
            url = "https://vk-mod-bot-e8ee.onrender.com"
            requests.get(url, timeout=5)
            print("Keep-alive ping sent")
        except Exception as e:
            print(f"Keep-alive error: {e}")
        time.sleep(300)

# Запускаем поток сразу при старте скрипта
threading.Thread(target=keep_alive, daemon=True).start()

# ---------------------------------------------------------
# 2. НАСТРОЙКИ И ИНИЦИАЛИЗАЦИЯ
# ---------------------------------------------------------
# Получаем токены из переменных окружения (Render Dashboard)
VK_TOKEN = os.getenv("VK_GROUP_TOKEN")
CONFIRMATION_CODE = os.getenv("VK_CALLBACK_CONFIRMATION", "b84ecffe")

# Инициализация API ВКонтакте
vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()

# ---------------------------------------------------------
# 3. ОБРАБОТЧИК ЗАПРОСОВ (MAIN LOGIC)
# ---------------------------------------------------------
async def handle_post(request):
    try:
        # Парсим входящий JSON от ВКонтакте
        data = await request.json()
        
        # САМОЕ ВАЖНОЕ: Проверка типа запроса
        req_type = data.get('type')
        
        # Если это запрос на подтверждение сервера — отдаем ТОЛЬКО код
        if req_type == 'confirmation':
            return web.Response(text=CONFIRMATION_CODE)
        
        # Если это событие (например, новое сообщение) — обрабатываем
        if req_type == 'message_new':
            obj = data.get('object', {})
            message = obj.get('message', {})
            text = message.get('text', '')
            peer_id = message.get('peer_id')
            
            # --- ТВОЯ ЛОГИКА ЗДЕСЬ ---
            # Пример: отвечаем на любое сообщение
            if text:
                vk.messages.send(
                    peer_id=peer_id,
                    message=f"Бот получил: {text}",
                    random_id=0
                )
            # -------------------------
            
        # Если тип запроса не известен или обработан — возвращаем 'ok'
        return web.Response(text='ok')

    except Exception as e:
        print(f"Ошибка обработки запроса: {e}")
        return web.Response(status=500, text="Internal Server Error")

# ---------------------------------------------------------
# 4. ЗАПУСК СЕРВЕРА
# ---------------------------------------------------------
app = web.Application()
app.router.add_post('/', handle_post)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8080))
    print(f"Сервер запущен на порту {port}")
    web.run_app(app, port=port)
