import os
from supabase import create_client, Client
from datetime import datetime
import json

class Database:
    def __init__(self):
        supabase_url = os.getenv('SUPABASE_URL')
        supabase_key = os.getenv('SUPABASE_KEY')
        
        if not supabase_url or not supabase_key:
            raise ValueError("Supabase URL and KEY must be set in environment variables")
        
        self.supabase: Client = create_client(supabase_url, supabase_key)
    
    # === USER METHODS ===
    def get_user(self, chat_id):
        try:
            response = self.supabase.table('users').select('*').eq('chat_id', chat_id).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Error getting user: {e}")
            return None
    
    def create_user(self, chat_id, login, username):
        try:
            user_data = {
                'chat_id': chat_id,
                'login': login,
                'username': username,
                'status': 'pending'
            }
            response = self.supabase.table('users').insert(user_data).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Error creating user: {e}")
            return None
    
    def approve_user(self, chat_id):
        try:
            response = self.supabase.table('users').update({
                'status': 'approved',
                'approved_at': datetime.now().isoformat()
            }).eq('chat_id', chat_id).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Error approving user: {e}")
            return None
    
    def get_pending_users(self):
        try:
            response = self.supabase.table('users').select('*').eq('status', 'pending').execute()
            return response.data
        except Exception as e:
            print(f"Error getting pending users: {e}")
            return []
    
    def get_approved_users(self):
        try:
            response = self.supabase.table('users').select('*').eq('status', 'approved').execute()
            return response.data
        except Exception as e:
            print(f"Error getting approved users: {e}")
            return []
    
    # === EVENT METHODS ===
    def create_event(self, event_data):
        try:
            response = self.supabase.table('events').insert(event_data).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Error creating event: {e}")
            return None
    
    def get_published_events(self):
        try:
            response = self.supabase.table('events').select('*').eq('is_published', True).execute()
            return response.data
        except Exception as e:
            print(f"Error getting published events: {e}")
            return []
    
    def get_all_events(self):
        try:
            response = self.supabase.table('events').select('*').execute()
            return response.data
        except Exception as e:
            print(f"Error getting all events: {e}")
            return []
    
    def get_event_by_uuid(self, event_uuid):
        try:
            response = self.supabase.table('events').select('*').eq('event_uuid', event_uuid).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Error getting event: {e}")
            return None
    
    # === VOTE METHODS ===
    def add_vote(self, event_uuid, voter_chat_id, option_index):
        try:
            # Проверяем, голосовал ли уже пользователь
            existing_vote = self.supabase.table('votes').select('*').eq('event_uuid', event_uuid).eq('voter_chat_id', voter_chat_id).execute()
            
            if existing_vote.data:
                return None  # Уже голосовал
            
            vote_data = {
                'event_uuid': event_uuid,
                'voter_chat_id': voter_chat_id,
                'option_index': option_index
            }
            response = self.supabase.table('votes').insert(vote_data).execute()
            
            # Обновляем счетчик участников в мероприятии
            if response.data:
                event = self.get_event_by_uuid(event_uuid)
                if event:
                    participants = len(self.supabase.table('votes').select('voter_chat_id').eq('event_uuid', event_uuid).execute().data)
                    self.supabase.table('events').update({'participants': participants}).eq('event_uuid', event_uuid).execute()
            
            return response.data[0] if response.data else None
        except Exception as e:
            print(f"Error adding vote: {e}")
            return None
    
    def get_event_votes(self, event_uuid):
        try:
            response = self.supabase.table('votes').select('*').eq('event_uuid', event_uuid).execute()
            return response.data
        except Exception as e:
            print(f"Error getting event votes: {e}")
            return []
                
    # === USERS BALANCE ===
# В класс Database добавьте:

def update_user_balance(self, chat_id, new_balance):
    """Обновление баланса пользователя"""
    try:
        response = self.supabase.table('users').update({
            'balance': new_balance,
            'updated_at': datetime.now().isoformat()
        }).eq('chat_id', chat_id).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error updating user balance: {e}")
        return None

def get_user_balance(self, chat_id):
    """Получение баланса пользователя"""
    user = self.get_user(chat_id)
    return user.get('balance', 1000) if user else 1000

# В класс Database добавьте следующие методы:

def create_prediction_market(self, event_uuid, option_index):
    """Создание рынка предсказаний для варианта мероприятия"""
    try:
        market_data = {
            'event_uuid': event_uuid,
            'option_index': option_index,
            'total_yes_reserve': 1000.0,
            'total_no_reserve': 1000.0,
            'constant_product': 1000000.0
        }
        response = self.supabase.table('prediction_markets').insert(market_data).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error creating prediction market: {e}")
        return None

def get_market_data(self, event_uuid, option_index):
    """Получение данных рынка для варианта мероприятия"""
    try:
        response = self.supabase.table('prediction_markets').select('*').eq('event_uuid', event_uuid).eq('option_index', option_index).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error getting market data: {e}")
        return None

def update_market_reserves(self, market_id, yes_reserve, no_reserve, constant_product):
    """Обновление резервов рынка"""
    try:
        response = self.supabase.table('prediction_markets').update({
            'total_yes_reserve': yes_reserve,
            'total_no_reserve': no_reserve,
            'constant_product': constant_product,
            'updated_at': datetime.now().isoformat()
        }).eq('id', market_id).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error updating market reserves: {e}")
        return None

def add_user_shares(self, user_chat_id, market_id, share_type, quantity, average_price):
    """Добавление акций пользователя"""
    try:
        share_data = {
            'user_chat_id': user_chat_id,
            'market_id': market_id,
            'share_type': share_type,
            'quantity': quantity,
            'average_price': average_price
        }
        response = self.supabase.table('user_shares').insert(share_data).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error adding user shares: {e}")
        return None

def get_user_shares(self, user_chat_id, market_id=None):
    """Получение акций пользователя"""
    try:
        query = self.supabase.table('user_shares').select('*').eq('user_chat_id', user_chat_id)
        if market_id:
            query = query.eq('market_id', market_id)
        response = query.execute()
        return response.data
    except Exception as e:
        print(f"Error getting user shares: {e}")
        return []

def add_market_order(self, user_chat_id, market_id, order_type, amount, price, shares):
    """Добавление ордера на покупку"""
    try:
        order_data = {
            'user_chat_id': user_chat_id,
            'market_id': market_id,
            'order_type': order_type,
            'amount': amount,
            'price': price,
            'shares': shares
        }
        response = self.supabase.table('market_orders').insert(order_data).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error adding market order: {e}")
        return None

# Глобальный экземпляр базы данных
db = Database()
