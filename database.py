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

# Глобальный экземпляр базы данных
db = Database()
