import os
from datetime import datetime, timezone, timedelta, date
from typing import Optional, List, Dict, Any
from decimal import Decimal, getcontext

from supabase import create_client, Client

getcontext().prec = 28


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _monday_of_week_utc(dt: datetime) -> date:
    # Неделя: понедельник 00:00:00Z — воскресенье 23:59:59
    d = dt.date()
    delta = timedelta(days=d.isoweekday() - 1)  # Mon=1..Sun=7
    return d - delta


def _format_range_label(start: date) -> str:
    end = start + timedelta(days=6)
    def fmt(d: date) -> str:
        return d.strftime("%d.%m.%y")
    return f"{fmt(start)}-{fmt(end)}"


class DB:
    def __init__(self):
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL/SUPABASE_KEY not set")
        self.client: Client = create_client(url, key)

    # ---------- Users ----------
    def get_user(self, chat_id: int) -> Optional[Dict[str, Any]]:
        res = self.client.table("users").select("*").eq("chat_id", chat_id).limit(1).execute()
        return res.data[0] if res.data else None

    def get_approved_users(self) -> List[Dict[str, Any]]:
        res = self.client.table("users").select("chat_id,login,balance,status").eq("status", "approved").execute()
        return res.data or []

    def create_user(self, chat_id: int, login: str, username: str) -> Optional[Dict[str, Any]]:
        row = {
            "chat_id": chat_id,
            "login": login,
            "username": username,
            "status": "pending",
            "balance": 1000,
            "created_at": _now(),
        }
        res = self.client.table("users").insert(row).execute()
        return res.data[0] if res.data else None

    def get_pending_users(self) -> List[Dict[str, Any]]:
        res = self.client.table("users").select("*").eq("status", "pending").order("created_at", desc=True).execute()
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
            self.client.table("events").select("*").eq("is_published", True).order("end_date", desc=False).execute()
        )
        return res.data or []

    # ---------- Markets ----------
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

    # ---------- Trade ----------
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
            .update(
                {
                    "total_yes_reserve": float(new_yes),
                    "total_no_reserve": float(new_no),
                    "constant_product": float(new_yes * new_no),
                }
            )
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
            self.client.table("user_shares").insert({
                "user_chat_id": chat_id,
                "market_id": row["id"],
                "share_type": side,
                "quantity": float(shares),
                "average_price": float(Decimal(str(amount)) / shares),
                "created_at": _now(),
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
            "created_at": _now(),
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

    # ---------- Positions / Equity ----------
    def get_user_positions(self, chat_id: int) -> List[Dict[str, Any]]:
        shares = (
            self.client.table("user_shares")
            .select("*")
            .eq("user_chat_id", chat_id)
            .order("created_at", desc=True)
            .execute()
        ).data or []
        if not shares:
            return []

        market_ids = sorted({s["market_id"] for s in shares if s.get("market_id") is not None})
        markets = {}
        if market_ids:
            mdata = (
                self.client.table("prediction_markets")
                .select("*")
                .in_("id", market_ids)
                .execute()
            ).data or []
            for m in mdata:
                markets[m["id"]] = m

        event_uuids = sorted({markets[s["market_id"]]["event_uuid"] for s in shares if s["market_id"] in markets})
        events = {}
        if event_uuids:
            edata = (
                self.client.table("events")
                .select("*")
                .in_("event_uuid", event_uuids)
                .execute()
            ).data or []
            for e in edata:
                events[e["event_uuid"]] = e

        out = []
        for s in shares:
            mid = s["market_id"]
            m = markets.get(mid)
            if not m:
                continue
            ev = events.get(m["event_uuid"], {})
            yes_p, no_p = self._prices_from_row(m)
            opts = ev.get("options") or []
            opt_idx = m.get("option_index", 0)
            opt_text = ""
            try:
                opt_text = (opts[opt_idx] or {}).get("text", "")
            except Exception:
                opt_text = ""
            out.append({
                "user_chat_id": chat_id,
                "market_id": mid,
                "event_uuid": m["event_uuid"],
                "event_name": ev.get("name", ""),
                "option_index": opt_idx,
                "option_text": opt_text,
                "share_type": s["share_type"],
                "quantity": float(s["quantity"]),
                "average_price": float(s["average_price"]),
                "current_yes_price": yes_p,
                "current_no_price": no_p,
            })
        return out

    def _current_equities_all(self) -> Dict[int, float]:
        # equity = balance + sum(q * EV), где EV для YES = P(YES), для NO = 1 - P(YES)
        users = self.get_approved_users()
        balances = {u["chat_id"]: float(u.get("balance", 0)) for u in users}

        shares = (
            self.client.table("user_shares")
            .select("user_chat_id,market_id,share_type,quantity")
            .execute()
        ).data or []
        if not shares:
            return balances

        market_ids = sorted({s["market_id"] for s in shares if s.get("market_id")})
        markets = {}
        if market_ids:
            mdata = (
                self.client.table("prediction_markets")
                .select("*")
                .in_("id", market_ids)
                .execute()
            ).data or []
            for m in mdata:
                markets[m["id"]] = m

        ev_sum: Dict[int, Decimal] = {cid: Decimal("0") for cid in balances.keys()}
        for s in shares:
            cid = s["user_chat_id"]
            mid = s["market_id"]
            side = s["share_type"]
            q = Decimal(str(s["quantity"]))
            m = markets.get(mid)
            if not m:
                continue
            yes_p, _no_p = self._prices_from_row(m)
            ev_per_share = Decimal(str(yes_p if side == "yes" else (1 - yes_p)))
            ev_sum[cid] = ev_sum.get(cid, Decimal("0")) + q * ev_per_share

        equities = {}
        for cid, bal in balances.items():
            equities[cid] = float(Decimal(str(bal)) + ev_sum.get(cid, Decimal("0")))
        return equities

    # ---------- Weekly baselines / Leaderboard ----------
    def week_current_bounds(self) -> Dict[str, str]:
        now = datetime.now(timezone.utc)
        start = _monday_of_week_utc(now)
        end = start + timedelta(days=6)
        return {"start": start.isoformat(), "end": end.isoformat(), "label": _format_range_label(start)}

    def _get_existing_baselines(self, week_start_iso: str) -> Dict[int, float]:
        rows = (
            self.client.table("weekly_baselines")
            .select("user_chat_id,equity")
            .eq("week_start", week_start_iso)
            .execute()
        ).data or []
        return {r["user_chat_id"]: float(r["equity"]) for r in rows}

    def _insert_missing_baselines(self, week_start_iso: str, equities: Dict[int, float], existing: Dict[int, float]):
        to_add = []
        for cid, eq in equities.items():
            if cid not in existing:
                to_add.append({
                    "user_chat_id": cid,
                    "week_start": week_start_iso,
                    "equity": float(eq),
                    "created_at": _now(),
                })
        if to_add:
            self.client.table("weekly_baselines").insert(to_add).execute()

    def get_leaderboard(self, week_start_iso: str, limit: int = 50) -> List[Dict[str, Any]]:
        equities = self._current_equities_all()
        existing = self._get_existing_baselines(week_start_iso)
        self._insert_missing_baselines(week_start_iso, equities, existing)
        baselines = self._get_existing_baselines(week_start_iso)

        users = self.get_approved_users()
        u_login = {u["chat_id"]: u.get("login", "") for u in users}

        items = []
        for cid, cur_eq in equities.items():
            base = baselines.get(cid, cur_eq)
            earned = cur_eq - base
            items.append({"chat_id": cid, "login": u_login.get(cid, ""), "earned": float(earned)})

        items.sort(key=lambda x: x["earned"], reverse=True)
        return items[:limit]


db = DB()
