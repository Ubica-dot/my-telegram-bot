from decimal import Decimal

class PredictionMarketAMM:
    def __init__(self, yes_reserve: float = 1000.0, no_reserve: float = 1000.0):
        self.yes_reserve = Decimal(str(yes_reserve))
        self.no_reserve = Decimal(str(no_reserve))
        self.constant_product = self.yes_reserve * self.no_reserve

    def _total(self) -> Decimal:
        return self.yes_reserve + self.no_reserve

    def calculate_yes_price(self) -> float:
        total = self._total()
        if total == 0:
            return 0.5
        return float(self.no_reserve / total)

    def calculate_no_price(self) -> float:
        total = self._total()
        if total == 0:
            return 0.5
        return float(self.yes_reserve / total)

    def buy_shares(self, share_type: str, amount: float):
        amt = Decimal(str(amount))
        if share_type == "yes":
            new_yes = self.yes_reserve + amt
            new_no = self.constant_product / new_yes
            if new_no > self.no_reserve:
                return 0.0, self.calculate_yes_price()
            shares = self.no_reserve - new_no
            self.yes_reserve, self.no_reserve = new_yes, new_no
            self.constant_product = self.yes_reserve * self.no_reserve
            return float(shares), self.calculate_yes_price()

        if share_type == "no":
            new_no = self.no_reserve + amt
            new_yes = self.constant_product / new_no
            if new_yes > self.yes_reserve:
                return 0.0, self.calculate_no_price()
            shares = self.yes_reserve - new_yes
            self.no_reserve, self.yes_reserve = new_no, new_yes
            self.constant_product = self.yes_reserve * self.no_reserve
            return float(shares), self.calculate_no_price()

        return 0.0, 0.0
