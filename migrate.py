import json
from database import db
from datetime import datetime

def migrate_from_json():
    try:
        # Загружаем данные из JSON
        with open('users.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Мигрируем пользователей
        print("Мигрируем пользователей...")
        for user in data.get('pending', []):
            existing = db.get_user(user['chat_id'])
            if not existing:
                db.create_user(user['chat_id'], user['login'], user['username'])
        
        for user in data.get('approved', []):
            existing = db.get_user(user['chat_id'])
            if not existing:
                db.create_user(user['chat_id'], user['login'], user['username'])
                db.approve_user(user['chat_id'])
        
        # Мигрируем мероприятия
        print("Мигрируем мероприятия...")
        for event in data.get('events', []):
            # Преобразуем варианты в правильный формат
            options = []
            for opt in event.get('options', []):
                if isinstance(opt, dict):
                    options.append(opt)
                else:
                    options.append({
                        "text": opt,
                        "votes": 0,
                        "voters": []
                    })
            
            event_data = {
                'event_uuid': event['id'],
                'name': event['name'],
                'description': event['description'],
                'options': options,
                'end_date': event['end_date'],
                'is_published': event.get('is_published', True),
                'creator_id': event.get('creator_id', 0),
                'participants': event.get('participants', 0)
            }
            
            existing = db.get_event_by_uuid(event['id'])
            if not existing:
                db.create_event(event_data)
        
        print("Миграция завершена успешно!")
        
    except Exception as e:
        print(f"Ошибка миграции: {e}")

if __name__ == "__main__":
    migrate_from_json()
