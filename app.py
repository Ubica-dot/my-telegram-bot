import uuid
import os
import json
import requests
from flask import Flask, request, render_template_string, jsonify
from datetime import datetime
from database import db
from amm import PredictionMarketAMM

# Получаем токен из переменных окружения
TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = os.getenv('ADMIN_ID', '123456789')
app = Flask(__name__)

# HTML шаблон для админской панели (остается без изменений)
ADMIN_PANEL_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Админская панель</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .user-card { border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px; }
        .pending { background-color: #fff3cd; }
        .approved { background-color: #d4edda; }
        .rejected { background-color: #f8d7da; }
        button { padding: 5px 10px; margin: 0 5px; cursor: pointer; }
        .approve-btn { background-color: #28a745; color: white; border: none; }
        .reject-btn { background-color: #dc3545; color: white; border: none; }
        .balance-form { margin: 10px 0; padding: 10px; background: #f8f9fa; border-radius: 5px; }
        .balance-input { width: 100px; padding: 5px; margin: 0 5px; }
        .balance-btn { background-color: #007bff; color: white; border: none; padding: 5px 10px; }
    </style>
</head>
<body>
    <h1>Админская панель бота</h1>
    <h2>Заявки на регистрацию ({{ pending_count }})</h2>
    
    {% for user in pending_users %}
    <div class="user-card pending">
        <strong>ID:</strong> {{ user.chat_id }}<br>
        <strong>Логин:</strong> {{ user.login }}<br>
        <strong>Дата:</strong> {{ user.created_at[:16] }}<br>
        <strong>Username:</strong> @{{ user.username }}<br>
        <div>
            <button class="approve-btn" onclick="approveUser({{ user.chat_id }})">✅ Одобрить</button>
            <button class="reject-btn" onclick="rejectUser({{ user.chat_id }})">❌ Отклонить</button>
        </div>
    </div>
    {% endfor %}
    
    <h2>Одобренные пользователи ({{ approved_count }})</h2>
    {% for user in approved_users %}
    <div class="user-card approved">
        <strong>ID:</strong> {{ user.chat_id }}<br>
        <strong>Логин:</strong> {{ user.login }}<br>
        <strong>Username:</strong> @{{ user.username }}<br>
        <strong>Баланс:</strong> <span id="balance-{{ user.chat_id }}">{{ user.balance }}</span> кредитов<br>
        <strong>Дата одобрения:</strong> {{ user.approved_at[:16] if user.approved_at else 'Н/Д' }}<br>
        
        <div class="balance-form">
            <strong>Изменить баланс:</strong><br>
            <input type="number" id="new_balance_{{ user.chat_id }}" class="balance-input" value="{{ user.balance }}" min="0">
            <button class="balance-btn" onclick="updateBalance({{ user.chat_id }})">💳 Обновить</button>
        </div>
    </div>
    {% endfor %}

    <h2>Управление мероприятиями</h2>
    <a href="/admin/events"><button style="padding: 10px; margin: 5px;">🎇 Управление мероприятиями</button></a>

    <script>
    function approveUser(chatId) {
        fetch('/admin/approve/' + chatId)
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    alert('Пользователь одобрен!');
                    location.reload();
                } else {
                    alert('Ошибка: ' + data.error);
                }
            });
    }
    
    function rejectUser(chatId) {
        fetch('/admin/reject/' + chatId)
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    alert('Пользователь отклонен!');
                    location.reload();
                } else {
                    alert('Ошибка: ' + data.error);
                }
            });
    }
    
    function updateBalance(chatId) {
        const newBalance = document.getElementById('new_balance_' + chatId).value;
        if (!newBalance || newBalance < 0) {
            alert('Введите корректную сумму баланса!');
            return;
        }
        
        fetch('/admin/update_balance/' + chatId, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ balance: parseInt(newBalance) })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                document.getElementById('balance-' + chatId).textContent = newBalance;
                alert('Баланс обновлен!');
            } else {
                alert('Ошибка: ' + data.error);
            }
        });
    }
    </script>
</body>
</html>
"""

def send_message(chat_id, text, reply_markup=None):
    """Отправка сообщения через Telegram Bot API"""
    url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    data = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML'
    }
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    
    try:
        response = requests.post(url, data=data, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Ошибка отправки сообщения: {e}")
        return False

def notify_admin(message):
    """Уведомление администратора"""
    send_message(ADMIN_ID, message)

@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    """Обработка входящих сообщений от Telegram"""
    update = request.get_json()
    
    if 'message' in update:
        chat_id = update['message']['chat']['id']
        text = update['message'].get('text', '')
        username = update['message']['from'].get('username', 'нет')
        
        # Получаем пользователя из базы данных
        user = db.get_user(chat_id)
        is_approved = user and user['status'] == 'approved'
        
        if text == '/start':
            if is_approved:
                balance = user.get('balance', 1000)
                send_message(chat_id, f"✅ Вы уже зарегистрированы! Добро пожаловать в бот!\n\n💰 Ваш баланс: {balance} кредитов")
            else:
                if user:
                    send_message(chat_id, "⏳ Ваша заявка уже отправлена и ожидает рассмотрения администратором.")
                else:
                    welcome_text = """
👋 Добро пожаловать!

Для доступа к полному функционалу бота необходимо зарегистрироваться.

📝 Пожалуйста, отправьте ваш логин для регистрации.

После проверки администратором вы получите уведомление о доступе.
                    """
                    send_message(chat_id, welcome_text)

        elif text == '/events':
            if not is_approved:
                send_message(chat_id, "❌ У вас нет доступа к мероприятиям. Завершите регистрацию через /start")
                return 'ok', 200
            
            events = db.get_published_events()
            
            if not events:
                send_message(chat_id, "📅 На данный момент нет активных мероприятий.")
            else:
                message = "📅 Активные мероприятия:\n\n"
                for event in events:
                    message += f"🎪 {event['name']}\n"
                    message += f"📝 {event['description'][:50]}...\n"
                    message += f"⏰ До: {event['end_date'][:16]}\n"
                    message += f"👥 Участников: {event['participants']}\n\n"
                
                send_message(chat_id, message)

        elif text == '/balance':
            if not is_approved:
                send_message(chat_id, "❌ У вас нет доступа к этой команде. Завершите регистрацию через /start")
                return 'ok', 200
            
            balance = user.get('balance', 1000)
            send_message(chat_id, f"💰 Ваш текущий баланс: {balance} кредитов")

        elif text == '/app':
            web_app_url = f'https://{request.host}/mini-app'
            keyboard = {
                'inline_keyboard': [[{
                    'text': '📱 Открыть Mini App',
                    'web_app': {'url': web_app_url}
                }]]
            }
            send_message(chat_id, "Откройте мини-приложение для удобного просмотра мероприятий:", keyboard)

        elif text.startswith('/'):
            if not is_approved:
                send_message(chat_id, "❌ У вас нет доступа к этой команде. Сначала завершите регистрацию через /start")
        
        else:
            if not is_approved:
                if user:
                    send_message(chat_id, "⏳ Ваша заявка уже находится на рассмотрении. Ожидайте ответа администратора.")
                else:
                    # Создаем нового пользователя в базе данных
                    new_user = db.create_user(chat_id, text, username)
                    if new_user:
                        send_message(chat_id, f"✅ Ваш логин '{text}' отправлен на модерацию. Ожидайте подтверждения администратора.")
                        
                        # Уведомляем администратора
                        notify_admin(f"📝 Новая заявка на регистрацию!\n\nЛогин: {text}\nID: {chat_id}\nUsername: @{username}\n\nДля просмотра заявок перейдите в админ-панель: https://my-telegram-bot-iept.onrender.com/admin")
                    else:
                        send_message(chat_id, "❌ Произошла ошибка при отправке заявки. Попробуйте еще раз.")
            else:
                send_message(chat_id, "✅ Вы уже зарегистрированы! Используйте доступные команды.")
    
    return 'ok', 200

# Админская панель (остается без изменений)
@app.route('/admin')
def admin_panel():
    """Админская панель для управления заявками"""
    pending_users = db.get_pending_users()
    approved_users = db.get_approved_users()
    
    pending_count = len(pending_users)
    approved_count = len(approved_users)
    
    return render_template_string(
        ADMIN_PANEL_HTML,
        pending_users=pending_users,
        approved_users=approved_users,
        pending_count=pending_count,
        approved_count=approved_count
    )

@app.route('/admin/approve/<int:chat_id>')
def approve_user(chat_id):
    """Одобрение пользователя"""
    user = db.approve_user(chat_id)
    if user:
        # Уведомляем пользователя
        balance = user.get('balance', 1000)
        send_message(chat_id, f"🎉 Поздравляем! Ваша заявка одобрена администратором.\n\n💰 Ваш стартовый баланс: {balance} кредитов\n\nТеперь вам доступен полный функционал бота!")
        return {'success': True}
    else:
        return {'success': False, 'error': 'Пользователь не найден'}

@app.route('/admin/reject/<int:chat_id>')
def reject_user(chat_id):
    """Отклонение пользователя"""
    user = db.get_user(chat_id)
    if user:
        send_message(chat_id, "❌ К сожалению, ваша заявка была отклонена администратором. Вы можете подать заявку повторно через /start")
        return {'success': True}
    else:
        return {'success': False, 'error': 'Пользователь не найден'}

@app.route('/admin/update_balance/<int:chat_id>', methods=['POST'])
def update_user_balance(chat_id):
    """Обновление баланса пользователя"""
    data = request.get_json()
    new_balance = data.get('balance')
    
    if new_balance is None or new_balance < 0:
        return {'success': False, 'error': 'Некорректная сумма баланса'}
    
    user = db.update_user_balance(chat_id, new_balance)
    if user:
        # Уведомляем пользователя об изменении баланса
        send_message(chat_id, f"💰 Ваш баланс был изменен администратором.\n\nНовый баланс: {new_balance} кредитов")
        return {'success': True}
    else:
        return {'success': False, 'error': 'Пользователь не найден'}

# ==================== МЕРОПРИЯТИЯ ====================

@app.route('/admin/events')
def admin_events_panel():
    """Панель управления событиями"""
    return '''
    <h2>Управление мероприятиями</h2>
    <a href="/admin/create_event"><button style="padding: 10px; margin: 5px;">🎇 Создать новое мероприятие</button></a>
    <a href="/admin/view_events"><button style="padding: 10px; margin: 5px;">📋 Просмотр всех мероприятий</button></a>
    <br><a href="/admin">← Назад в админ-панель</a>
    '''

@app.route('/admin/create_event')
def create_event_form():
    """Форма создания нового мероприятия"""
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Создание мероприятия</title>
        <style>
            body { font-family: Arial; margin: 20px; }
            .form-group { margin: 15px 0; }
            label { display: block; margin: 5px 0; }
            input, textarea { width: 300px; padding: 8px; margin: 5px 0; }
            button { padding: 10px 15px; margin: 5px; cursor: pointer; }
            .option-group { border: 1px solid #ddd; padding: 10px; margin: 10px 0; }
        </style>
    </head>
    <body>
        <h2>🎇 Создание нового мероприятия</h2>
        <form action="/admin/publish_event" method="POST">
            <div class="form-group">
                <label><strong>1. Наименование мероприятия:</strong></label>
                <input type="text" name="event_name" placeholder="Введите название" required>
            </div>
            
            <div class="form-group">
                <label><strong>2. Правила и описание:</strong></label>
                <textarea name="event_rules" rows="5" placeholder="Подробное описание правил..." required></textarea>
            </div>
            
            <div class="form-group">
                <label><strong>3. Варианты выбора:</strong></label>
                <div id="options-container">
                    <div class="option-group">
                        <input type="text" name="option_1" placeholder="Вариант №1" required>
                    </div>
                </div>
                <button type="button" onclick="addOption()">➕ Добавить вариант</button>
            </div>
            
            <div class="form-group">
                <label><strong>4. Дата окончания мероприятия:</strong></label>
                <input type="datetime-local" name="end_date" required>
            </div>
            
            <button type="submit">📢 Опубликовать</button>
        </form>
        
        <script>
            let optionCount = 1;
            function addOption() {
                optionCount++;
                const container = document.getElementById('options-container');
                const newOption = document.createElement('div');
                newOption.className = 'option-group';
                newOption.innerHTML = `<input type="text" name="option_${optionCount}" placeholder="Вариант №${optionCount}" required>`;
                container.appendChild(newOption);
            }
        </script>
    </body>
    </html>
    '''

@app.route('/admin/publish_event', methods=['POST'])
def publish_event():
    """Обработка данных формы и публикация мероприятия в БД"""
    # Получаем данные из формы
    event_name = request.form['event_name']
    event_rules = request.form['event_rules']
    end_date = request.form['end_date']
    
    # Собираем все варианты
    options = []
    i = 1
    while f'option_{i}' in request.form:
        option_text = request.form[f'option_{i}']
        if option_text.strip():
            options.append({
                "text": option_text,
                "votes": 0
            })
        i += 1
    
    # Создаем объект мероприятия для базы данных
    event_uuid = str(uuid.uuid4())[:8]
    event_data = {
        'event_uuid': event_uuid,
        'name': event_name,
        'description': event_rules,
        'options': options,
        'end_date': end_date.replace('T', ' ') + ':00',
        'is_published': True,
        'creator_id': int(ADMIN_ID),
        'participants': 0
    }
    
    # Сохраняем в базу данных
    new_event = db.create_event(event_data)
    
    if new_event:
        # Создаем рынки предсказаний для каждого варианта
        for option_index in range(len(options)):
            db.create_prediction_market(event_uuid, option_index)
        
        # Отправляем уведомление одобренным пользователям
        approved_users = db.get_approved_users()
        for user in approved_users:
            message = f"🎉 Новое мероприятие!\n\n"
            message += f"📌 {event_name}\n"
            message += f"📝 {event_rules[:100]}...\n"
            message += f"⏰ До: {end_date[:16]}\n\n"
            message += f"Используйте /events для просмотра!"
            
            send_message(user['chat_id'], message)
        
        return f'''
        <h2>✅ Мероприятие опубликовано в базе данных!</h2>
        <p><strong>Название:</strong> {event_name}</p>
        <p><strong>Вариантов:</strong> {len(options)}</p>
        <p><strong>Окончание:</strong> {end_date}</p>
        <p><strong>Созданы рынки предсказаний для всех вариантов</strong></p>
        <p><strong>Уведомления отправлены пользователям</strong></p>
        <a href="/admin/events"><button>Вернуться к управлению мероприятиями</button></a>
        '''
    else:
        return "❌ Ошибка при создании мероприятия в базе данных"

@app.route('/admin/view_events')
def view_events():
    """Просмотр всех созданных мероприятий из БД"""
    events = db.get_all_events()
    
    html = '<h2>📋 Все мероприятия (из базы данных)</h2>'
    if not events:
        html += '<p>Мероприятий пока нет.</p>'
    else:
        for event in events:
            status = "✅ Опубликовано" if event.get('is_published') else "⏳ Черновик"
            options_text = ", ".join([opt["text"] for opt in event["options"]])
            
            html += f'''
            <div style="border:1px solid #ccc; padding:15px; margin:10px 0;">
                <h3>{event['name']} (ID: {event['event_uuid']})</h3>
                <p><strong>Статус:</strong> {status}</p>
                <p><strong>Описание:</strong> {event['description'][:100]}...</p>
                <p><strong>Варианты:</strong> {options_text}</p>
                <p><strong>Окончание:</strong> {event['end_date'][:16]}</p>
                <p><strong>Участников:</strong> {event['participants']}</p>
            </div>
            '''
    
    html += '<br><a href="/admin/events"><button>← Назад</button></a>'
    return html

# ==================== PREDICTION MARKET API ====================

@app.route('/api/market/<event_uuid>/<int:option_index>')
def get_market_data(event_uuid, option_index):
    """Получение данных рынка для конкретного варианта"""
    try:
        market = db.get_market_data(event_uuid, option_index)
        if not market:
            return jsonify({'error': 'Рынок не найден'}), 404
        
        amm = PredictionMarketAMM(market['total_yes_reserve'], market['total_no_reserve'])
        market_data = amm.get_market_data()
        market_data['market_id'] = market['id']
        
        return jsonify(market_data)
    except Exception as e:
        print(f"Market API Error: {e}")
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500

@app.route('/api/market/buy', methods=['POST'])
def buy_shares():
    """Покупка акций на рынке"""
    try:
        data = request.get_json()
        user_chat_id = data.get('user_chat_id')
        event_uuid = data.get('event_uuid')
        option_index = data.get('option_index')
        share_type = data.get('share_type')  # 'yes' или 'no'
        amount = data.get('amount')
        
        if not all([user_chat_id, event_uuid, option_index is not None, share_type, amount]):
            return jsonify({'error': 'Не все обязательные поля заполнены'}), 400
        
        # Проверяем баланс пользователя
        user = db.get_user(user_chat_id)
        if not user:
            return jsonify({'error': 'Пользователь не найден'}), 404
        
        if user['balance'] < amount:
            return jsonify({'error': 'Недостаточно средств'}), 400
        
        # Получаем данные рынка
        market = db.get_market_data(event_uuid, option_index)
        if not market:
            return jsonify({'error': 'Рынок не найден'}), 404
        
        # Выполняем покупку через AMM
        amm = PredictionMarketAMM(market['total_yes_reserve'], market['total_no_reserve'])
        shares_received, new_price = amm.buy_shares(share_type, amount)
        
        if shares_received <= 0:
            return jsonify({'error': 'Не удалось выполнить покупку'}), 400
        
        # Обновляем базу данных
        db.update_market_reserves(
            market['id'], 
            amm.yes_reserve, 
            amm.no_reserve, 
            amm.constant_product
        )
        db.update_user_balance(user_chat_id, user['balance'] - amount)
        db.add_user_shares(user_chat_id, market['id'], share_type, shares_received, new_price)
        db.add_market_order(user_chat_id, market['id'], share_type, amount, new_price, shares_received)
        
        return jsonify({
            'success': True,
            'shares_received': shares_received,
            'new_price': new_price,
            'new_balance': user['balance'] - amount,
            'market_data': amm.get_market_data()
        })
        
    except Exception as e:
        print(f"Buy API Error: {e}")
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500

@app.route('/api/user/<user_chat_id>/shares')
def get_user_shares(user_chat_id):
    """Получение акций пользователя"""
    try:
        shares = db.get_user_shares(user_chat_id)
        return jsonify({'shares': shares})
    except Exception as e:
        print(f"User Shares API Error: {e}")
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500

# ==================== MINI APP ====================

@app.route('/mini-app')
def mini_app():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Prediction Market Mini App</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <style>
            body {
                font-family: -apple-system, system-ui, sans-serif;
                margin: 0;
                padding: 20px;
                background: var(--tg-theme-bg-color, #ffffff);
                color: var(--tg-theme-text-color, #000000);
            }
            .container { max-width: 600px; margin: 0 auto; }
            .event-card {
                background: var(--tg-theme-secondary-bg-color, #f0f0f0);
                border-radius: 12px;
                padding: 16px;
                margin: 12px 0;
            }
            .button {
                background: var(--tg-theme-button-color, #2481cc);
                color: var(--tg-theme-button-text-color, #ffffff);
                border: none;
                padding: 12px 24px;
                border-radius: 8px;
                font-size: 16px;
                width: 100%;
                margin: 8px 0;
                cursor: pointer;
            }
            .balance-info {
                background: var(--tg-theme-secondary-bg-color, #f0f0f0);
                padding: 15px;
                border-radius: 12px;
                margin: 15px 0;
                text-align: center;
            }
            /* Стили для страницы рынка */
            #market-page { display: none; }
            .back-btn {
                background: var(--tg-theme-secondary-bg-color, #f0f0f0);
                color: var(--tg-theme-text-color, #000000);
                margin-bottom: 20px;
            }
            .market-header {
                background: var(--tg-theme-secondary-bg-color, #f0f0f0);
                padding: 20px;
                border-radius: 12px;
                margin-bottom: 20px;
            }
            .price-info {
                display: flex;
                justify-content: space-between;
                margin: 15px 0;
            }
            .price-card {
                background: var(--tg-theme-secondary-bg-color, #f0f0f0);
                padding: 15px;
                border-radius: 8px;
                text-align: center;
                flex: 1;
                margin: 0 5px;
            }
            .buy-section {
                margin: 20px 0;
            }
            .buy-buttons {
                display: flex;
                gap: 10px;
                margin: 10px 0;
            }
            .buy-btn {
                flex: 1;
                padding: 12px;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                font-size: 16px;
            }
            .buy-yes {
                background: #28a745;
                color: white;
            }
            .buy-no {
                background: #dc3545;
                color: white;
            }
            .modal {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0,0,0,0.5);
                z-index: 1000;
            }
            .modal-content {
                background: var(--tg-theme-bg-color, #ffffff);
                margin: 15% auto;
                padding: 20px;
                border-radius: 12px;
                width: 80%;
                max-width: 400px;
            }
            .amount-input {
                width: 100%;
                padding: 10px;
                margin: 10px 0;
                border: 1px solid #ddd;
                border-radius: 6px;
                font-size: 16px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <!-- Главная страница со списком мероприятий -->
            <div id="main-page">
                <h1 class="page-title">🎪 Prediction Markets</h1>
                
                <div class="balance-info">
                    <h3>💰 Ваш баланс</h3>
                    <div id="user-balance">Загрузка...</div>
                </div>
                
                <div id="events-list">
                    <p>Загрузка мероприятий...</p>
                </div>
            </div>

            <!-- Страница рынка -->
            <div id="market-page">
                <button class="button back-btn" onclick="goBack()">← Назад к мероприятиям</button>
                
                <div class="market-header">
                    <h2 id="market-title">Название рынка</h2>
                    <p id="market-description">Описание...</p>
                    <div class="price-info">
                        <div class="price-card">
                            <h3>Вероятность</h3>
                            <div id="market-probability">0%</div>
                        </div>
                    </div>
                </div>

                <div class="price-info">
                    <div class="price-card">
                        <h3>Цена ДА</h3>
                        <div id="yes-price">$0.00</div>
                        <div id="yes-profit">Прибыль: $0.00</div>
                    </div>
                    <div class="price-card">
                        <h3>Цена НЕТ</h3>
                        <div id="no-price">$0.00</div>
                        <div id="no-profit">Прибыль: $0.00</div>
                    </div>
                </div>

                <div class="buy-section">
                    <h3>Купить акции</h3>
                    <div class="buy-buttons">
                        <button class="buy-btn buy-yes" onclick="showBuyModal('yes')">Купить ДА</button>
                        <button class="buy-btn buy-no" onclick="showBuyModal('no')">Купить НЕТ</button>
                    </div>
                </div>

                <div id="user-shares-section">
                    <h3>Ваши акции</h3>
                    <div id="user-shares">Загрузка...</div>
                </div>
            </div>
        </div>

        <!-- Модальное окно покупки -->
        <div id="buy-modal" class="modal">
            <div class="modal-content">
                <h3 id="modal-title">Покупка акций</h3>
                <p>Текущая цена: <span id="modal-price">$0.00</span></p>
                <p>Подразумеваемая вероятность: <span id="modal-probability">0%</span></p>
                
                <label>Сумма покупки ($):</label>
                <input type="number" id="buy-amount" class="amount-input" min="0.01" step="0.01" value="1.00">
                
                <div id="potential-profit">
                    <p>Потенциальная прибыль: $<span id="profit-amount">0.00</span></p>
                    <p>Получите акций: <span id="shares-amount">0</span></p>
                </div>
                
                <div class="buy-buttons">
                    <button class="buy-btn buy-yes" onclick="executeBuy()">Купить</button>
                    <button class="button back-btn" onclick="closeModal()">Отмена</button>
                </div>
            </div>
        </div>
        
        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();
            tg.ready();

            let currentMarket = null;
            let currentShareType = null;
            let currentEventUuid = null;
            let currentOptionIndex = null;

            // Загрузка баланса пользователя
            async function loadBalance() {
                try {
                    const response = await fetch('/api/user/balance');
                    const data = await response.json();
                    document.getElementById('user-balance').innerHTML = `<h2>${data.balance} кредитов</h2>`;
                } catch (error) {
                    console.error('Error loading balance:', error);
                    document.getElementById('user-balance').innerHTML = '<p>Ошибка загрузки</p>';
                }
            }
            
            // Загрузка мероприятий
            async function loadEvents() {
                try {
                    const response = await fetch('/api/events');
                    const events = await response.json();
                    
                    const eventsList = document.getElementById('events-list');
                    
                    if (events.length === 0) {
                        eventsList.innerHTML = '<p>Нет активных мероприятий</p>';
                        return;
                    }
                    
                    eventsList.innerHTML = events.map(event => `
                        <div class="event-card">
                            <h3>${event.name}</h3>
                            <p>${event.description.substring(0, 100)}...</p>
                            <p><small>Участников: ${event.participants}</small></p>
                            <p><small>До: ${new Date(event.end_date).toLocaleString('ru-RU')}</small></p>
                            <button class="button" onclick="showEventOptions('${event.event_uuid}', '${event.name}', '${event.description}')">
                                Торговать
                            </button>
                        </div>
                    `).join('');
                } catch (error) {
                    console.error('Error loading events:', error);
                    document.getElementById('events-list').innerHTML = '<p>Ошибка загрузки</p>';
                }
            }
            
            // Показать варианты мероприятия
            async function showEventOptions(eventUuid, eventName, eventDescription) {
                try {
                    const response = await fetch(`/api/event/${eventUuid}`);
                    const event = await response.json();
                    
                    if (event.error) {
                        tg.showPopup({
                            title: 'Ошибка',
                            message: event.error,
                            buttons: [{ type: 'ok' }]
                        });
                        return;
                    }
                    
                    // Создаем список вариантов для торговли
                    const optionsHTML = event.options.map((option, index) => `
                        <div class="event-card">
                            <h4>${option.text}</h4>
                            <button class="button" onclick="showMarket('${eventUuid}', ${index}, '${eventName}', '${option.text}')">
                                Открыть рынок
                            </button>
                        </div>
                    `).join('');
                    
                    document.getElementById('main-page').style.display = 'none';
                    document.getElementById('market-page').style.display = 'block';
                    
                    document.getElementById('market-title').textContent = eventName;
                    document.getElementById('market-description').textContent = eventDescription;
                    document.getElementById('user-shares-section').style.display = 'none';
                    
                    document.getElementById('market-page').innerHTML += optionsHTML;
                    
                } catch (error) {
                    console.error('Error loading event options:', error);
                    tg.showPopup({
                        title: 'Ошибка',
                        message: 'Не удалось загрузить варианты мероприятия',
                        buttons: [{ type: 'ok' }]
                    });
                }
            }
            
            // Показать рынок для конкретного варианта
            async function showMarket(eventUuid, optionIndex, eventName, optionText) {
                try {
                    currentEventUuid = eventUuid;
                    currentOptionIndex = optionIndex;
                    
                    const response = await fetch(`/api/market/${eventUuid}/${optionIndex}`);
                    const marketData = await response.json();
                    
                    if (marketData.error) {
                        tg.showPopup({
                            title: 'Ошибка',
                            message: marketData.error,
                            buttons: [{ type: 'ok' }]
                        });
                        return;
                    }
                    
                    currentMarket = marketData;
                    
                    // Обновляем интерфейс рынка
                    document.getElementById('market-title').textContent = `${eventName} - ${optionText}`;
                    document.getElementById('market-probability').textContent = `${marketData.implied_probability.toFixed(2)}%`;
                    document.getElementById('yes-price').textContent = `$${marketData.yes_price.toFixed(4)}`;
                    document.getElementById('no-price').textContent = `$${marketData.no_price.toFixed(4)}`;
                    
                    // Расчет потенциальной прибыли
                    const yesProfit = (1 - marketData.yes_price) * 1;
                    const noProfit = (1 - marketData.no_price) * 1;
                    
                    document.getElementById('yes-profit').textContent = `Прибыль: $${yesProfit.toFixed(4)}`;
                    document.getElementById('no-profit').textContent = `Прибыль: $${noProfit.toFixed(4)}`;
                    
                    // Загружаем акции пользователя
                    await loadUserShares();
                    document.getElementById('user-shares-section').style.display = 'block';
                    
                } catch (error) {
                    console.error('Error loading market:', error);
                    tg.showPopup({
                        title: 'Ошибка',
                        message: 'Не удалось загрузить данные рынка',
                        buttons: [{ type: 'ok' }]
                    });
                }
            }
            
            // Загрузка акций пользователя
            async function loadUserShares() {
                try {
                    // Здесь нужно получить user_chat_id из Telegram Web App
                    const userChatId = tg.initDataUnsafe.user?.id || 123456789; // Заглушка
                    
                    const response = await fetch(`/api/user/${userChatId}/shares`);
                    const data = await response.json();
                    
                    if (data.shares && data.shares.length > 0) {
                        const sharesHTML = data.shares.map(share => `
                            <div class="event-card">
                                <p>Тип: ${share.share_type === 'yes' ? 'ДА' : 'НЕТ'}</p>
                                <p>Количество: ${share.quantity}</p>
                                <p>Средняя цена: $${share.average_price}</p>
                            </div>
                        `).join('');
                        document.getElementById('user-shares').innerHTML = sharesHTML;
                    } else {
                        document.getElementById('user-shares').innerHTML = '<p>У вас пока нет акций</p>';
                    }
                } catch (error) {
                    console.error('Error loading user shares:', error);
                    document.getElementById('user-shares').innerHTML = '<p>Ошибка загрузки</p>';
                }
            }
            
            // Показать модальное окно покупки
            function showBuyModal(shareType) {
                currentShareType = shareType;
                
                const modal = document.getElementById('buy-modal');
                const title = document.getElementById('modal-title');
                const price = document.getElementById('modal-price');
                const probability = document.getElementById('modal-probability');
                
                if (shareType === 'yes') {
                    title.textContent = 'Покупка акций ДА';
                    price.textContent = `$${currentMarket.yes_price.toFixed(4)}`;
                    probability.textContent = `${currentMarket.implied_probability.toFixed(2)}%`;
                } else {
                    title.textContent = 'Покупка акций НЕТ';
                    price.textContent = `$${currentMarket.no_price.toFixed(4)}`;
                    probability.textContent = `${(100 - currentMarket.implied_probability).toFixed(2)}%`;
                }
                
                modal.style.display = 'block';
                updateProfitCalculation();
            }
            
            // Обновление расчета прибыли
            function updateProfitCalculation() {
                const amount = parseFloat(document.getElementById('buy-amount').value) || 0;
                const profitElement = document.getElementById('profit-amount');
                const sharesElement = document.getElementById('shares-amount');
                
                if (currentShareType === 'yes') {
                    const profit = (1 - currentMarket.yes_price) * amount;
                    const shares = amount / currentMarket.yes_price;
                    profitElement.textContent = profit.toFixed(4);
                    sharesElement.textContent = shares.toFixed(4);
                } else {
                    const profit = (1 - currentMarket.no_price) * amount;
                    const shares = amount / currentMarket.no_price;
                    profitElement.textContent = profit.toFixed(4);
                    sharesElement.textContent = shares.toFixed(4);
                }
            }
            
            // Закрыть модальное окно
            function closeModal() {
                document.getElementById('buy-modal').style.display = 'none';
            }
            
            // Выполнить покупку
            async function executeBuy() {
                try {
                    const amount = parseFloat(document.getElementById('buy-amount').value) || 0;
                    const userChatId = tg.initDataUnsafe.user?.id || 123456789; // Заглушка
                    
                    if (amount <= 0) {
                        tg.showPopup({
                            title: 'Ошибка',
                            message: 'Введите корректную сумму',
                            buttons: [{ type: 'ok' }]
                        });
                        return;
                    }
                    
                    const response = await fetch('/api/market/buy', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({
                            user_chat_id: userChatId,
                            event_uuid: currentEventUuid,
                            option_index: currentOptionIndex,
                            share_type: currentShareType,
                            amount: amount
                        })
                    });
                    
                    const result = await response.json();
                    
                    if (result.success) {
                        tg.showPopup({
                            title: 'Успех!',
                            message: `Вы купили ${result.shares_received.toFixed(4)} акций`,
                            buttons: [{ type: 'ok' }]
                        });
                        
                        closeModal();
                        // Обновляем данные рынка
                        await showMarket(currentEventUuid, currentOptionIndex, 
                                       document.getElementById('market-title').textContent, '');
                        await loadBalance();
                        await loadUserShares();
                    } else {
                        tg.showPopup({
                            title: 'Ошибка',
                            message: result.error,
                            buttons: [{ type: 'ok' }]
                        });
                    }
                    
                } catch (error) {
                    console.error('Error executing buy:', error);
                    tg.showPopup({
                        title: 'Ошибка',
                        message: 'Не удалось выполнить покупку',
                        buttons: [{ type: 'ok' }]
                    });
                }
            }
            
            function goBack() {
                document.getElementById('main-page').style.display = 'block';
                document.getElementById('market-page').style.display = 'none';
                currentMarket = null;
                currentShareType = null;
            }
            
            // Слушатель изменения суммы покупки
            document.getElementById('buy-amount').addEventListener('input', updateProfitCalculation);
            
            // Загружаем баланс и мероприятия при старте
            loadBalance();
            loadEvents();
        </script>
    </body>
    </html>
    '''

# API для Mini App
@app.route('/api/events')
def api_events():
    """API для получения мероприятий (для Mini App)"""
    try:
        events = db.get_published_events()
        return jsonify(events)
    except Exception as e:
        print(f"API Error: {e}")
        return jsonify([])

@app.route('/api/event/<event_uuid>')
def api_event(event_uuid):
    """API для получения конкретного мероприятия"""
    try:
        event = db.get_event_by_uuid(event_uuid)
        if event:
            return jsonify(event)
        else:
            return jsonify({'error': 'Мероприятие не найдено'}), 404
    except Exception as e:
        print(f"API Event Error: {e}")
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500

@app.route('/api/user/balance')
def api_user_balance():
    """API для получения баланса пользователя (для Mini App)"""
    try:
        # В реальном приложении здесь нужно определить пользователя
        # Для демонстрации возвращаем баланс первого одобренного пользователя
        approved_users = db.get_approved_users()
        if approved_users:
            balance = approved_users[0].get('balance', 1000)
            return jsonify({'balance': balance})
        return jsonify({'balance': 1000})
    except Exception as e:
        print(f"API Balance Error: {e}")
        return jsonify({'balance': 1000})

@app.route('/')
def hello_world():
    return "<p>Prediction Market Bot работает! Админ-панель: <a href='/admin'>/admin</a></p>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
