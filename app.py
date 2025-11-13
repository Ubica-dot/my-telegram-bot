import uuid
import os
import json
import requests
from flask import Flask, request, render_template_string
from datetime import datetime

# Получаем токен из переменных окружения
TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = os.getenv('ADMIN_ID', '123456789')  # Ваш ID в Telegram
app = Flask(__name__)

# Файл для хранения данных пользователей
USERS_FILE = 'users.json'

# HTML шаблон для админской панели
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
    </style>
</head>
<body>
    <h1>Админская панель бота</h1>
    <h2>Заявки на регистрацию ({{ pending_count }})</h2>
    
    {% for user in pending_users %}
    <div class="user-card pending">
        <strong>ID:</strong> {{ user.chat_id }}<br>
        <strong>Логин:</strong> {{ user.login }}<br>
        <strong>Дата:</strong> {{ user.timestamp }}<br>
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
        <strong>Дата одобрения:</strong> {{ user.approved_timestamp }}<br>
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
    </script>
</body>
</html>
"""

def load_users():
    """Загрузка данных пользователей из файла"""
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # Убедимся, что все необходимые ключи существуют
            if 'pending' not in data:
                data['pending'] = []
            if 'approved' not in data:
                data['approved'] = []
            if 'events' not in data:
                data['events'] = []
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        # Если файла нет, вернем структуру по умолчанию
        return {'pending': [], 'approved': [], 'events': []}

def save_users(users_data):
    """Сохранение данных пользователей в файл"""
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users_data, f, ensure_ascii=False, indent=2)

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
    """Уведомление администратора о новой заявке"""
    send_message(ADMIN_ID, message)

def notify_users_about_new_event(event):
    """Уведомление всех одобренных пользователей о новом мероприятии"""
    users_data = load_users()
    
    for user in users_data['approved']:
        message = f"🎉 Новое мероприятие!\n\n"
        message += f"📌 {event['name']}\n"
        message += f"📝 {event['description'][:100]}...\n"
        message += f"⏰ До: {event['end_date']}\n\n"
        message += f"Используйте /events для просмотра и участия!"
        
        send_message(user['chat_id'], message)

@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    """Обработка входящих сообщений от Telegram"""
    update = request.get_json()
    
    if 'message' in update:
        chat_id = update['message']['chat']['id']
        text = update['message'].get('text', '')
        username = update['message']['from'].get('username', 'нет')
        
        users_data = load_users()
        
        # Проверяем, одобрен ли уже пользователь
        is_approved = any(user['chat_id'] == chat_id for user in users_data['approved'])
        
        if text == '/start':
            if is_approved:
                send_message(chat_id, "✅ Вы уже зарегистрированы! Добро пожаловать в бот!")
            else:
                # Проверяем, есть ли уже заявка от этого пользователя
                has_pending = any(user['chat_id'] == chat_id for user in users_data['pending'])
                
                if has_pending:
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
            users_data = load_users()
            
            # Проверяем, одобрен ли пользователь
            is_approved = any(user['chat_id'] == chat_id for user in users_data['approved'])
            if not is_approved:
                send_message(chat_id, "❌ У вас нет доступа к мероприятиям. Завершите регистрацию через /start")
                return 'ok', 200
            
            published_events = [event for event in users_data.get('events', []) 
                               if event.get('is_published', False)]
            
            if not published_events:
                send_message(chat_id, "📅 На данный момент нет активных мероприятий.")
            else:
                message = "📅 Активные мероприятия:\n\n"
                for event in published_events:
                    message += f"🎪 {event['name']}\n"
                    message += f"📝 {event['description'][:50]}...\n"
                    message += f"⏰ До: {event['end_date']}\n"
                    message += f"👥 Участников: {event['participants']}\n\n"
                
                send_message(chat_id, message)

        elif text.startswith('/'):
            # Игнорируем другие команды для непринятых пользователей
            if not is_approved:
                send_message(chat_id, "❌ У вас нет доступа к этой команде. Сначала завершите регистрацию через /start")
        
        else:
            # Обработка текстовых сообщений (логин)
            if not is_approved:
                # Проверяем, есть ли уже заявка
                has_pending = any(user['chat_id'] == chat_id for user in users_data['pending'])
                
                if has_pending:
                    send_message(chat_id, "⏳ Ваша заявка уже находится на рассмотрении. Ожидайте ответа администратора.")
                else:
                    # Сохраняем новую заявку
                    new_user = {
                        'chat_id': chat_id,
                        'login': text,
                        'username': username,
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'status': 'pending'
                    }
                    
                    users_data['pending'].append(new_user)
                    save_users(users_data)
                    
                    # Уведомляем пользователя
                    send_message(chat_id, f"✅ Ваш логин '{text}' отправлен на модерацию. Ожидайте подтверждения администратора.")
                    
                    # Уведомляем администратора
                    notify_admin(f"📝 Новая заявка на регистрацию!\n\nЛогин: {text}\nID: {chat_id}\nUsername: @{username}\n\nДля просмотра заявок перейдите в админ-панель: https://my-telegram-bot-iept.onrender.com/admin")
            else:
                send_message(chat_id, "✅ Вы уже зарегистрированы! Используйте доступные команды.")
    
    return 'ok', 200

# Админская панель
@app.route('/admin')
def admin_panel():
    """Админская панель для управления заявками"""
    users_data = load_users()
    
    pending_count = len(users_data['pending'])
    approved_count = len(users_data['approved'])
    
    return render_template_string(
        ADMIN_PANEL_HTML,
        pending_users=users_data['pending'],
        approved_users=users_data['approved'],
        pending_count=pending_count,
        approved_count=approved_count
    )

@app.route('/admin/approve/<int:chat_id>')
def approve_user(chat_id):
    """Одобрение пользователя"""
    users_data = load_users()
    
    # Находим пользователя в pending
    user_index = None
    user_data = None
    
    for i, user in enumerate(users_data['pending']):
        if user['chat_id'] == chat_id:
            user_index = i
            user_data = user
            break
    
    if user_index is not None:
        # Перемещаем в approved
        user_data['approved_timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        users_data['approved'].append(user_data)
        users_data['pending'].pop(user_index)
        save_users(users_data)
        
        # Уведомляем пользователя
        send_message(chat_id, "🎉 Поздравляем! Ваша заявка одобрена администратором. Теперь вам доступен полный функционал бота!")
        
        return {'success': True}
    else:
        return {'success': False, 'error': 'Пользователь не найден'}

@app.route('/admin/reject/<int:chat_id>')
def reject_user(chat_id):
    """Отклонение пользователя"""
    users_data = load_users()
    
    # Находим пользователя в pending
    user_index = None
    
    for i, user in enumerate(users_data['pending']):
        if user['chat_id'] == chat_id:
            user_index = i
            break
    
    if user_index is not None:
        # Удаляем из pending
        users_data['pending'].pop(user_index)
        save_users(users_data)
        
        # Уведомляем пользователя
        send_message(chat_id, "❌ К сожалению, ваша заявка была отклонена администратором. Вы можете подать заявку повторно через /start")
        
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
                <label><strong>1. Наименование мероприятия (максимум 2 строки):</strong></label>
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
    """Обработка данных формы и публикация мероприятия"""
    users_data = load_users()
    
    # Получаем данные из формы
    event_name = request.form['event_name']
    event_rules = request.form['event_rules']
    end_date = request.form['end_date']
    
    # Собираем все варианты с структурой для голосования
    options = []
    i = 1
    while f'option_{i}' in request.form:
        option_text = request.form[f'option_{i}']
        if option_text.strip():
            options.append({
                "text": option_text,
                "votes": 0,
                "voters": []
            })
        i += 1
    
    # Создаем объект мероприятия
    new_event = {
        'id': str(uuid.uuid4())[:8],
        'name': event_name,
        'description': event_rules,
        'options': options,
        'end_date': end_date,
        'is_published': True,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'creator_id': ADMIN_ID,
        'participants': 0
    }
    
    # Добавляем мероприятие в данные
    if 'events' not in users_data:
        users_data['events'] = []
    users_data['events'].append(new_event)
    save_users(users_data)
    
    # Отправляем уведомление одобренным пользователям
    notify_users_about_new_event(new_event)
    
    return f'''
    <h2>✅ Мероприятие опубликовано!</h2>
    <p><strong>Название:</strong> {event_name}</p>
    <p><strong>Вариантов:</strong> {len(options)}</p>
    <p><strong>Окончание:</strong> {end_date}</p>
    <p><strong>Уведомления отправлены пользователям</strong></p>
    <a href="/admin/events"><button>Вернуться к управлению мероприятиями</button></a>
    '''

@app.route('/admin/view_events')
def view_events():
    """Просмотр всех созданных мероприятий"""
    users_data = load_users()
    events = users_data.get('events', [])
    
    html = '<h2>📋 Все мероприятия</h2>'
    if not events:
        html += '<p>Мероприятий пока нет.</p>'
    else:
        for event in events:
            status = "✅ Опубликовано" if event.get('is_published', False) else "⏳ Черновик"
            html += f'''
            <div style="border:1px solid #ccc; padding:15px; margin:10px 0;">
                <h3>{event['name']} (ID: {event['id']})</h3>
                <p><strong>Статус:</strong> {status}</p>
                <p><strong>Описание:</strong> {event['description'][:100]}...</p>
                <p><strong>Варианты:</strong> {", ".join(opt["text"] for opt in event["options"])}</p>
                <p><strong>Окончание:</strong> {event['end_date']}</p>
            </div>
            '''
    
    html += '<br><a href="/admin/events"><button>← Назад</button></a>'
    return html

@app.route('/')
def hello_world():
    return "<p>Бот работает! Админ-панель: <a href='/admin'>/admin</a></p>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
