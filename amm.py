import math
from decimal import Decimal

class PredictionMarketAMM:
    def __init__(self, yes_reserve=1000.0, no_reserve=1000.0):
        self.yes_reserve = Decimal(str(yes_reserve))
        self.no_reserve = Decimal(str(no_reserve))
        self.constant_product = self.yes_reserve * self.no_reserve
    
    def calculate_yes_price(self):
        """Текущая цена YES акций"""
        if self.yes_reserve + self.no_reserve == 0:
            return float(Decimal('0.5'))
        return float(self.no_reserve / (self.yes_reserve + self.no_reserve))
    
    def calculate_no_price(self):
        """Текущая цена NO акций"""
        if self.yes_reserve + self.no_reserve == 0:
            return float(Decimal('0.5'))
        return float(self.yes_reserve / (self.yes_reserve + self.no_reserve))
    
    def calculate_implied_probability(self):
        """Подразумеваемая вероятность из текущих цен"""
        yes_price = self.calculate_yes_price()
        return yes_price * 100
    
    def buy_shares(self, share_type, amount):
        """Покупка акций через AMM"""
        amount_dec = Decimal(str(amount))
        
        if share_type == 'yes':
            # Покупаем YES - увеличиваем резерв YES, уменьшаем резерв NO
            new_yes_reserve = self.yes_reserve + amount_dec
            new_no_reserve = self.constant_product / new_yes_reserve
            
            if new_no_reserve > self.no_reserve:
                return 0, self.calculate_yes_price()
                
            shares_received = self.no_reserve - new_no_reserve
            
            # Обновляем резервы
            self.yes_reserve = new_yes_reserve
            self.no_reserve = new_no_reserve
            self.constant_product = self.yes_reserve * self.no_reserve
            
            return float(shares_received), self.calculate_yes_price()
        
        elif share_type == 'no':
            # Покупаем NO - увеличиваем резерв NO, уменьшаем резерв YES
            new_no_reserve = self.no_reserve + amount_dec
            new_yes_reserve = self.constant_product / new_no_reserve
            
            if new_yes_reserve > self.yes_reserve:
                return 0, self.calculate_no_price()
                
            shares_received = self.yes_reserve - new_yes_reserve
            
            # Обновляем резервы
            self.no_reserve = new_no_reserve
            self.yes_reserve = new_yes_reserve
            self.constant_product = self.yes_reserve * self.no_reserve
            
            return float(shares_received), self.calculate_no_price()
        
        else:
            return 0, 0
    
    def calculate_potential_profit(self, share_type, amount):
        """Расчет потенциальной прибыли"""
        if share_type == 'yes':
            current_price = self.calculate_yes_price()
            return (1.0 - current_price) * amount
        else:
            current_price = self.calculate_no_price()
            return (1.0 - current_price) * amount
    
    def get_market_data(self):
        """Получение всех данных рынка"""
        return {
            'yes_price': self.calculate_yes_price(),
            'no_price': self.calculate_no_price(),
            'implied_probability': self.calculate_implied_probability(),
            'yes_reserve': float(self.yes_reserve),
            'no_reserve': float(self.no_reserve),
            'constant_product': float(self.constant_product)
        }
