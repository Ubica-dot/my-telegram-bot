import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from decimal import Decimal, getcontext

from supabase import create_client, Client

getcontext().prec = 28


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DB:
    def __init__(self):
        url = os.getenv("SUPABASE_URL")
        # Лучше service_role, но поддержим и обычный ключ
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL/SUPABASE_KEY not set")
        self.client: Client = create_client(url, key)

    # ---------- Users ----------
    def get_user(self, chat_id: int) -> Optional[Dict[str, Any]]:
        res = self.client.table("users").select("*").eq("chat_id", chat_id).limit(1).execute()
        return res.data[0] if res.data else None

    def create_user(self, chat_id: int, login: str, username: str) -> Optional[Dict[str, Any]]:
        row = {
            "chat_id": chat_id,
            "login": login,
            "username": username,
            "status": "pending",
            "balance": 1000,  # убедитесь, что колонка добавлена миграцией
            "created_at": _now(),
        }
        res = self.client.table("users").insert(row).execute()
        return res.data[0] if res.data else None

    def get_pending_users(self) -> List[Dict[str, Any]]:
        res = self.client.table("users").select("*").eq("status", "pending").order("created_at", desc=True).execute()
        return res.data or []

    def get_approved_users(self) -> List[Dict[str, Any]]:
        res = self.client.table("users").select("*").eq("status", "approved").order("approved_at", desc=True).execute()
        return res.data or []

    def approve_user(self, chat_id: int) -> Optional[Dict[str, Any]]:
        res = (
            self.client.table("users")
            .update({"status": "approved", "approved_at": _now()})
            .eq("chat_id", chat_id)
            .execute()
        )
        return res.data[0] if res.data else None

    def update_user_balance(self, chat_id: int, new_balance: int) -> Optional[Dict[str, Any]]:
        res = self.client.table("users").update({"balance": new_balance}).eq("chat_id", chat_id).execute()
        return res.data[0] if res.data else None

    # ---------- Events ----------
    def create_event(self, event_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        res = self.client.table("events").insert(event_data).execute()
        return res.data[0] if res.data else None

    def get_published_events(self) -> List[Dict[str, Any]]:
        res = (
            self.client.table("events")
            .select("*")
            .eq("is_published", True)
            .order("end_date", desc=False)
            .execute()
        )
        return res.data or []

    # ---------- Markets (prediction_markets) ----------
    def create_prediction_market(self, event_uuid: str, option_index: int) -> Optional[Dict[str, Any]]:
        row = {
            "event_uuid": event_uuid,
            "option_index": option_index,
            "total_yes_reserve": 1000.0,
            "total_no_reserve": 1000.0,
            "constant_product": 1000.0 * 1000.0,
            "created_at": _now(),
        }
        res = self.client.table("prediction_markets").insert(row).execute()
        return res.data[0] if res.data else None

    def get_market(self, event_uuid: str, option_index: int):
        res = (
            self.client.table("prediction_markets")
            .select("*")
            .eq("event_uuid", event_uuid)
            .eq("option_index", option_index)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    def get_markets_for_event(self, event_uuid: str):
        return (
            self.client.table("prediction_markets")
            .select("*")
            .eq("event_uuid", event_uuid)
            .order("option_index")
            .execute()
        ).data or []

    def _prices_from_row(self, row):
        y = Decimal(str(row["total_yes_reserve"]))
        n = Decimal(str(row["total_no_reserve"]))
        total = y + n
        if total == 0:
            return 0.5, 0.5
        yes_p = float(n / total)   # цена YES = доля противоположного резерва
        no_p = float(y / total)
        return yes_p, no_p

    def trade_buy(self, *, chat_id: int, event_uuid: str, option_index: int, side: str, amount: float):
        # 1) Пользователь и баланс
        u = self.get_user(chat_id)
        if not u:
            return None, "user_not_found"
        if u.get("status") != "approved":
            return None, "not_approved"
        if float(u.get("balance", 0)) < float(amount):
            return None, "insufficient_funds"

        # 2) Рынок
        row = self.get_market(event_uuid, option_index)
        if not row:
            row = self.create_prediction_market(event_uuid, option_index)

        y = Decimal(str(row["total_yes_reserve"]))
        n = Decimal(str(row["total_no_reserve"]))
        k = y * n
        amt = Decimal(str(amount))

        if side not in ("yes", "no"):
            return None, "bad_side"

        if side == "yes":
            new_y = y + amt
            new_n = k / new_y
            if new_n > n:
                return None, "calc_error"
            shares = n - new_n
            new_yes, new_no = new_y, new_n
        else:
            new_n = n + amt
            new_y = k / new_n
            if new_y > y:
                return None, "calc_error"
            shares = y - new_y
            new_yes, new_no = new_y, new_n

        if shares <= 0:
            return None, "zero_shares"

        trade_price = float(Decimal(str(amount)) / shares)

        # 3) Обновляем рынок
        upd = (
            self.client.table("prediction_markets")
            .update({
                "total_yes_reserve": float(new_yes),
                "total_no_reserve": float(new_no),
                "constant_product": float(new_yes * new_no),
            })
            .eq("id", row["id"])
            .execute()
        )
        market = upd.data[0] if upd.data else row

        # 4) Списываем баланс
        new_balance = int(u.get("balance", 0)) - int(float(amount))
        self.client.table("users").update({"balance": new_balance}).eq("chat_id", chat_id).execute()

        # 5) user_shares upsert
        existing = (
            self.client.table("user_shares")
            .select("*")
            .eq("user_chat_id", chat_id)
            .eq("market_id", row["id"])
            .eq("share_type", side)
            .limit(1)
            .execute()
        )
        if existing.data:
            us = existing.data[0]
            old_q = Decimal(str(us["quantity"]))
            old_avg = Decimal(str(us["average_price"]))
            new_q = old_q + shares
            new_avg = (old_q * old_avg + Decimal(str(amount))) / new_q
            self.client.table("user_shares").update({
                "quantity": float(new_q),
                "average_price": float(new_avg),
            }).eq("id", us["id"]).execute()
        else:
            self.client.table("user_shares").upsert({
                "user_chat_id": chat_id,
                "market_id": row["id"],
                "share_type": side,
                "quantity": float(shares),
                "average_price": float(Decimal(str(amount)) / shares),
            }).execute()

        # 6) Ордер
        yes_p, no_p = self._prices_from_row(market)
        self.client.table("market_orders").insert({
            "user_chat_id": chat_id,
            "market_id": row["id"],
            "order_type": f"buy_{side}",
            "amount": float(amount),
            "price": float(trade_price),
            "shares": float(shares),
            "status": "completed",
        }).execute()

        return {
            "market_id": row["id"],
            "event_uuid": event_uuid,
            "option_index": option_index,
            "side": side,
            "spent": float(amount),
            "got_shares": float(shares),
            "trade_price": float(trade_price),
            "new_balance": new_balance,
            "yes_price": yes_p,
            "no_price": no_p,
            "yes_reserve": float(market["total_yes_reserve"]),
            "no_reserve": float(market["total_no_reserve"]),
        }, None


db = DB()
