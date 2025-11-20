import os
import uuid
from datetime import datetime, timedelta, timezone

from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

class Database:
    def __init__(self):
        assert SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY, "Supabase env not set"
        self.client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

    # --- users ---
    def get_user(self, chat_id: int):
        try:
            r = self.client.table("users").select("*").eq("chat_id", chat_id).single().execute()
            return r.data
        except Exception:
            return None

    def create_user(self, chat_id: int, login: str, username: str = None):
        try:
            data = {
                "chat_id": chat_id,
                "login": login.strip()[:100],
                "username": username,
                "status": "pending",
                "balance": 1000.0,
            }
            r = self.client.table("users").insert(data).execute()
            return bool(r.data)
        except Exception as e:
            print("[db.create_user] error:", e)
            return False

    def approve_user(self, chat_id: int):
        self.client.table("users").update({"status":"approved","approved_at":datetime.now(timezone.utc).isoformat()}).eq("chat_id", chat_id).execute()

    def reject_user(self, chat_id: int):
        self.client.table("users").update({"status":"rejected"}).eq("chat_id", chat_id).execute()

    def ban_user(self, chat_id: int):
        self.client.table("users").update({"status":"banned"}).eq("chat_id", chat_id).execute()

    def unban_user(self, chat_id: int):
        self.client.table("users").update({"status":"approved"}).eq("chat_id", chat_id).execute()

    def admin_set_balance_via_ledger(self, chat_id: int, new_balance: float):
        u = self.get_user(chat_id)
        if not u:
            return
        cur = float(u.get("balance") or 0)
        delta = float(new_balance) - cur
        if abs(delta) > 0:
            # положим запись в ledger
            self.client.table("ledger").insert({
                "chat_id": chat_id,
                "delta": delta,
                "reason": "admin_set_balance",
            }).execute()
            self.client.table("users").update({"balance": float(new_balance)}).eq("chat_id", chat_id).execute()

    def search_users(self, status="pending", q="", sort=""):
        q = (q or "").strip()
        st = (status or "pending").lower()
        query = self.client.table("users").select("chat_id,login,username,status,balance,created_at").eq("status", st)
        if q:
            if q.isdigit():
                query = query.eq("chat_id", int(q))
            else:
                query = query.ilike("login", f"%{q}%")
        if sort == "balance":
            query = query.order("balance", desc=True)
        elif sort == "created_at":
            query = query.order("created_at", desc=True)
        else:
            query = query.order("created_at", desc=True)
        try:
            return query.limit(200).execute().data or []
        except Exception as e:
            print("[db.search_users] error:", e)
            return []

    def get_ledger_for_user(self, chat_id: int, limit: int = 10):
        try:
            r = (
                self.client.table("ledger")
                .select("delta,reason,created_at,market_id,order_id")
                .eq("chat_id", chat_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return r.data or []
        except Exception:
            return []

    # --- events & markets ---
    def get_published_events(self):
        try:
            r = (
                self.client.table("events")
                .select("event_uuid,name,description,options,end_date,is_published,created_at,tags")
                .eq("is_published", True)
                .order("end_date", desc=False)
                .execute()
            )
            return r.data or []
        except Exception as e:
            print("[db.get_published_events] error:", e)
            return []

    def get_markets_for_event(self, event_uuid: str):
        try:
            r = (
                self.client.table("prediction_markets")
                .select("id,event_uuid,option_index,total_yes_reserve,total_no_reserve,resolved,winner_side")
                .eq("event_uuid", event_uuid)
                .order("option_index", desc=False)
                .execute()
            )
            return r.data or []
        except Exception as e:
            print("[db.get_markets_for_event] error:", e)
            return []

    def get_market_id(self, event_uuid: str, option_index: int):
        try:
            r = (
                self.client.table("prediction_markets")
                .select("id")
                .eq("event_uuid", event_uuid)
                .eq("option_index", option_index)
                .single()
                .execute()
            )
            return int(r.data["id"]) if r.data else None
        except Exception:
            return None

    def create_event_with_markets(self, name: str, description: str, options, end_date: str,
                                  tags, publish: bool, creator_id: int | None, double_outcome: bool):
        try:
            event_uuid = str(uuid.uuid4())
            if double_outcome:
                options = [{"text": "ДА"}, {"text": "НЕТ"}]

            # Вставка события
            ev_payload = {
                "event_uuid": event_uuid,
                "name": name,
                "description": description,
                "options": options or [],
                "end_date": end_date,
                "is_published": bool(publish),
                "tags": tags or [],
                "creator_id": creator_id,
            }
            self.client.table("events").insert(ev_payload).execute()

            # Создание рынков под каждый вариант
            markets = []
            for idx, _ in enumerate(options or []):
                markets.append({
                    "event_uuid": event_uuid,
                    "option_index": idx,
                    # резервы по умолчанию как в схеме
                    "total_yes_reserve": 1000.0,
                    "total_no_reserve": 1000.0,
                    "constant_product": 1_000_000.0,
                })
            if markets:
                self.client.table("prediction_markets").insert(markets).execute()

            return True, None
        except Exception as e:
            print("[db.create_event_with_markets] error:", e)
            return False, str(e)

    # --- positions / archive for /api/me ---
    def get_user_positions(self, chat_id: int):
        try:
            shares = (
                self.client.table("user_shares")
                .select("market_id,share_type,quantity,average_price,created_at")
                .eq("user_chat_id", chat_id)
                .gt("quantity", 0)
                .order("created_at", desc=True)
                .execute()
            ).data or []

            if not shares:
                return []

            market_ids = sorted({int(s["market_id"]) for s in shares})
            markets = {}
            if market_ids:
                r = (
                    self.client.table("prediction_markets")
                    .select("id,event_uuid,option_index,resolved,winner_side")
                    .in_("id", market_ids)
                    .execute()
                )
                for m in (r.data or []):
                    markets[int(m["id"])] = m

            positions = []
            for s in shares:
                mid = int(s["market_id"])
                m = markets.get(mid, {})
                positions.append({
                    "event_uuid": m.get("event_uuid"),
                    "option_index": m.get("option_index"),
                    "share_type": s.get("share_type"),
                    "quantity": float(s.get("quantity", 0)),
                    "avg_price": float(s.get("average_price", 0)),
                    "resolved": bool(m.get("resolved")),
                    "winner_side": m.get("winner_side"),
                })
            return positions
        except Exception as e:
            print("[db.get_user_positions] error:", e)
            return []

    def get_user_archive(self, chat_id: int, limit: int = 50):
        try:
            r = (
                self.client.table("market_orders")
                .select("market_id,order_type,amount,price,shares,created_at")
                .eq("user_chat_id", chat_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return r.data or []
        except Exception:
            return []

    # --- leaderboard helpers ---
    @staticmethod
    def week_current_bounds():
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=now.weekday())  # Monday
        start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end = start + timedelta(days=7)
        return start.isoformat(), end.isoformat()

    @staticmethod
    def month_current_bounds():
        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        if now.month == 12:
            end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
        return start.isoformat(), end.isoformat()

    def leaderboard(self, start_iso: str, end_iso: str, limit: int = 50):
        try:
            # суммируем только положительные начисления (payout_*), группируем в коде
            r = (
                self.client.table("ledger")
                .select("chat_id,delta,reason,created_at")
                .gte("created_at", start_iso)
                .lt("created_at", end_iso)
                .gt("delta", 0)
                .execute()
            )
            payouts = {}
            for row in (r.data or []):
                cid = int(row["chat_id"])
                payouts[cid] = payouts.get(cid, 0.0) + float(row.get("delta") or 0)
            if not payouts:
                return []

            ids = list(payouts.keys())
            users_map = {}
            r2 = self.client.table("users").select("chat_id,login").in_("chat_id", ids).execute()
            for u in (r2.data or []):
                users_map[int(u["chat_id"])] = u.get("login")

            items = [{"chat_id": cid, "login": users_map.get(cid) or str(cid), "payouts": v} for cid, v in payouts.items()]
            items.sort(key=lambda x: x["payouts"], reverse=True)
            return items[:limit]
        except Exception as e:
            print("[db.leaderboard] error:", e)
            return []

db = Database()
