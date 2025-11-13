import os
import requests
from flask import Flask, request

# Получаем токен из переменных окружения
TOKEN = os.getenv('BOT_TOKEN')
app = Flask(__name__)

@app.route("/")
def hello_world():
    return "<p>Hello, World!</p>"

# Эндпоинт для обработки вебхука от Telegram
@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    """Обработка входящих сообщений от Telegram"""
    update = request.get_json()
    
    # Логируем полученное сообщение (будет видно в логах Render)
    print("Получено сообщение:", update)
    
    # Обрабатываем сообщение
    if 'message' in update:
        chat_id = update['message']['chat']['id']
        text = update['message'].get('text', '')
        
        # Обработка команды /start
        if text == '/start':
            send_message(chat_id, "Привет! Я бот, работающий на Render! 🚀")
        else:
            send_message(chat_id, f"Вы написали: {text}")
    
    return 'ok', 200

# Функция для отправки сообщений
def send_message(chat_id, text):
    """Отправка сообщения через Telegram Bot API"""
    url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    data = {
        'chat_id': chat_id,
        'text': text
    }
    requests.post(url, data=data)

# Эндпоинт для установки вебхука
@app.route('/set_webhook')
def set_webhook():
    """Установка вебхука для Telegram"""
    webhook_url = f'https://my-telegram-bot-iept.onrender.com/{TOKEN}'
    result = requests.get(f'https://api.telegram.org/bot{TOKEN}/setWebhook', 
                         params={'url': webhook_url}).json()
    return f"Вебхук установлен: {result}"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
