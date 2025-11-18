import os
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from supabase import create_client, Client


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_float(x) -> float:
    try:
        if x is None:
            return 0.0
        return float(x)
    except Exception:
        return 0.0


class Database:
    def __init__(self):
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL or SUPABASE_*KEY is not configured")
        self.client: Client = create_client(url, key)

    # ---------------- USERS ----------------
    def get_user(self, chat_id: int) -> Optional[dict]:
        try:
            r = (
                self.client.table("users")
                .select("*")
                .eq("chat_id", chat_id)
                .single()
                .execute()
            )
            return r.data
        except Exception:
            return None

    def create_user(self, chat_id: int, login: str, username: str = "") -> Optional[dict]:
        try:
            ex = self.get_user(chat_id)
            if ex:
                return ex
            ins = {
                "chat_id": chat_id,
                "login": login,
                "username": username or None,
                "status": "pending",
            }
            r = self.client.table("users").insert(ins).execute()
            return r.data[0] if r.data else None
        except Exception as e:
            print("[create_user] error:", e)
            return None

    def approve_user(self, chat_id: int) -> Optional[dict]:
        try:
            r = (
                self.client.table("users")
                .update({"status": "approved", "approved_at": _now_utc().isoformat(), "updated_at": _now_utc().isoformat()})
                .eq("chat_id", chat_id)
                .execute()
            )
            return r.data[0] if r.data else None
        except Exception:
            return None

    def reject_user(self, chat_id: int) -> bool:
        try:
            r = (
                self.client.table("users")
                .update({"status": "rejected", "updated_at": _now_utc().isoformat()})
                .eq("chat_id", chat_id)
                .execute()
            )
            return bool(r.data)
        except Exception:
            return False

    def ban_user(self, chat_id: int) -> bool:
        try:
            r = (
                self.client.table("users")
                .update({"status": "banned", "updated_at": _now_utc().isoformat()})
                .eq("chat_id", chat_id)
                .execute()
            )
            return bool(r.data)
        except Exception:
            return False

    def unban_user(self, chat_id: int) -> bool:
        try:
            r = (
                self.client.table("users")
                .update({"status": "approved", "updated_at": _now_utc().isoformat()})
                .eq("chat_id", chat_id)
                .execute()
            )
            return bool(r.data)
        except Exception:
            return False

    def get_user_balance(self, chat_id: int) -> float:
        try:
            r = (
                self.client.table("users")
                .select("balance")
                .eq("chat_id", chat_id)
                .single()
                .execute()
            )
            return _to_float(r.data["balance"]) if r.data else 0.0
        except Exception:
            return 0.0

    def get_pending_users(self) -> List[dict]:
        try:
            r = (
                self.client.table("users")
                .select("chat_id, login, username, status, created_at, balance")
                .eq("status", "pending")
                .order("created_at", desc=False)
                .execute()
            )
            return r.data or []
        except Exception:
            return []

    def get_approved_users(self) -> List[dict]:
        try:
            r = (
                self.client.table("users")
                .select("chat_id, login, username, status, approved_at, balance")
                .eq("status", "approved")
                .order("approved_at", desc=True)
                .execute()
            )
            return r.data or []
        except Exception:
            return []

    def get_banned_users(self) -> List[dict]:
        try:
            r = (
                self.client.table("users")
                .select("chat_id, login, username, status, updated_at")
                .eq("status", "banned")
                .order("updated_at", desc=True)
                .execute()
            )
            return r.data or []
        except Exception:
            return []

    # Поиск/фильтр/сортировка пользователей для админки
    def search_users(self, status: str, q: str = "", sort: str = "") -> List[dict]:
        try:
            sel = "chat_id, login, username, status, created_at, approved_at, balance"
            query = self.client.table("users").select(sel).eq("status", status)
            if q:
                qq = q.strip()
                if qq.isdigit():
                    query = query.eq("chat_id", int(qq))
                else:
                    query = query.ilike("login", f"%{qq}%")
            if sort == "created_at":
                query = query.order("created_at", desc=False)
            elif sort == "balance":
                query = query.order("balance", desc=True)
            else:
                # по умолчанию: для approved — по дате одобрения, иначе — по дате создания
                if status == "approved":
                    query = query.order("approved_at", desc=True)
                else:
                    query = query.order("created_at", desc=False)
            r = query.limit(500).execute()
            return r.data or []
        except Exception as e:
            print("[search_users] error:", e)
            return []

    # История из ledger
    def get_ledger_for_user(self, chat_id: int, limit: int = 20) -> List[dict]:
        try:
            r = (
                self.client.table("ledger")
                .select("delta, reason, order_id, created_at")
                .eq("chat_id", chat_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return r.data or []
        except Exception:
            return []

    # Бухгалтерская корректировка + кэш
    def admin_set_balance_via_ledger(self, chat_id: int, new_balance: float) -> Optional[dict]:
        try:
            cur = self.get_user_balance(chat_id)
            delta = float(new_balance) - float(cur)
            if abs(delta) > 0:
                self.client.table("ledger").insert(
                    {
                        "chat_id": chat_id,
                        "delta": round(delta, 4),
                        "reason": "admin_adjustment",
                        "order_id": None,
                    }
                ).execute()
                self.client.table("users").update({"balance": round(float(new_balance), 4)}).eq("chat_id", chat_id).execute()
            return self.get_user(chat_id)
        except Exception as e:
            print("[admin_set_balance_via_ledger] error:", e)
            return None

    # Совместимость со старым вызовом
    def update_user_balance(self, chat_id: int, new_balance: float) -> Optional[dict]:
        return self.admin_set_balance_via_ledger(chat_id, new_balance)

    # ---------------- EVENTS / MARKETS ----------------
    def get_published_events(self) -> List[dict]:
        try:
            # пробуем включить tags, если колонка есть
            r = (
                self.client.table("events")
                .select("event_uuid, name, description, options, end_date, is_published, created_at, tags")
                .eq("is_published", True)
                .order("created_at", desc=True)
                .execute()
            )
            return r.data or []
        except Exception:
            try:
                r = (
                    self.client.table("events")
                    .select("event_uuid, name, description, options, end_date, is_published, created_at")
                    .eq("is_published", True)
                    .order("created_at", desc=True)
                    .execute()
                )
                return r.data or []
            except Exception:
                return []

    def get_markets_for_event(self, event_uuid: str) -> List[dict]:
        try:
            r = (
                self.client.table("prediction_markets")
                .select("id, option_index, total_yes_reserve, total_no_reserve, resolved, winner_side")
                .eq("event_uuid", event_uuid)
                .order("option_index", desc=False)
                .execute()
            )
            return r.data or []
        except Exception:
            try:
                r = (
                    self.client.table("prediction_markets")
                    .select("id, option_index, total_yes_reserve, total_no_reserve")
                    .eq("event_uuid", event_uuid)
                    .order("option_index", desc=False)
                    .execute()
                )
                return r.data or []
            except Exception:
                return []

    def get_market_id(self, event_uuid: str, option_index: int) -> Optional[int]:
        try:
            r = (
                self.client.table("prediction_markets")
                .select("id")
                .eq("event_uuid", event_uuid)
                .eq("option_index", option_index)
                .single()
                .execute()
            )
            if r.data and "id" in r.data:
                return int(r.data["id"])
            return None
        except Exception:
            return None

    # Создание события с тегами и автосозданием рынков
    def create_event_with_markets(
        self,
        name: str,
        description: str,
        options: List[dict],
        end_date: str,
        tags: List[str],
        publish: bool,
        creator_id: Optional[int] = None,
    ) -> Tuple[bool, Optional[str]]:
        try:
            # end_date строку приводим к ISO (timestamptz)
            try:
                dt = datetime.fromisoformat(end_date.replace(" ", "T"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                end_iso = dt.isoformat()
            except Exception:
                return False, "bad_end_date"

            event_uuid = uuid4().hex[:8]
            evt = {
                "event_uuid": event_uuid,
                "name": name,
                "description": description,
                "options": options,         # [{text:...}]
                "end_date": end_iso,
                "is_published": bool(publish),
                "creator_id": creator_id or None,
            }
            # если есть колонка tags — добавим
            try:
                evt_with_tags = dict(evt)
                evt_with_tags["tags"] = tags or []
                er = self.client.table("events").insert(evt_with_tags).execute()
            except Exception:
                er = self.client.table("events").insert(evt).execute()

            if not er.data:
                return False, "event_insert_failed"

            # Автосоздание рынков по количеству опций
            rows = []
            for idx, _ in enumerate(options):
                rows.append({
                    "event_uuid": event_uuid,
                    "option_index": idx,
                    "total_yes_reserve": 1000.0,
                    "total_no_reserve": 1000.0,
                    "constant_product": 1000000.0,
                })
            mr = self.client.table("prediction_markets").insert(rows).execute()
            if mr.data is None:
                return False, "markets_insert_failed"

            return True, None
        except Exception as e:
            print("[create_event_with_markets] error:", e)
            return False, "exception"

    # ---------------- POSITIONS ----------------
    def get_user_positions(self, chat_id: int) -> List[dict]:
        try:
            shares = (
                self.client.table("user_shares")
                .select("user_chat_id, market_id, share_type, quantity, average_price")
                .eq("user_chat_id", chat_id)
                .execute()
                .data
                or []
            )
            if not shares:
                return []

            market_ids = sorted({int(s["market_id"]) for s in shares if s.get("market_id") is not None})
            if not market_ids:
                return []

            mk = (
                self.client.table("prediction_markets")
                .select("id, event_uuid, option_index, total_yes_reserve, total_no_reserve")
                .in_("id", market_ids)
                .execute()
                .data
                or []
            )
            market_by_id = {int(m["id"]): m for m in mk}

            event_uuids = sorted({m["event_uuid"] for m in mk if m.get("event_uuid")})
            ev_map: Dict[str, dict] = {}
            if event_uuids:
                evs = (
                    self.client.table("events")
                    .select("event_uuid, name, options")
                    .in_("event_uuid", event_uuids)
                    .execute()
                    .data
                    or []
                )
                ev_map = {e["event_uuid"]: e for e in evs}

            out: List[dict] = []
            for s in shares:
                mid = int(s["market_id"])
                mkt = market_by_id.get(mid)
                if not mkt:
                    continue
                yes_r = _to_float(mkt.get("total_yes_reserve"))
                no_r = _to_float(mkt.get("total_no_reserve"))
                tot = yes_r + no_r
                yes_price = (no_r / tot) if tot > 0 else 0.5
                no_price = 1.0 - yes_price

                ev = ev_map.get(mkt["event_uuid"], {})
                name = ev.get("name", "—")
                options = ev.get("options") or []
                opt_text = "—"
                try:
                    idx = int(mkt.get("option_index", 0))
                    if isinstance(options, list) and 0 <= idx < len(options):
                        item = options[idx]
                        opt_text = item.get("text") if isinstance(item, dict) else str(item)
                except Exception:
                    pass

                out.append(
                    {
                        "event_name": name,
                        "option_text": opt_text,
                        "share_type": s["share_type"],
                        "quantity": _to_float(s["quantity"]),
                        "average_price": _to_float(s["average_price"]),
                        "current_yes_price": yes_price,
                        "current_no_price": no_price,
                    }
                )
            return out
        except Exception as e:
            print("[get_user_positions] error:", e)
            return []

    # ---------------- LEADERBOARD ----------------
    def _week_start_utc(self, dt: Optional[datetime] = None) -> datetime:
        dt = dt or _now_utc()
        monday = dt - timedelta(days=(dt.weekday()))
        return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)

    def _month_start_utc(self, dt: Optional[datetime] = None) -> datetime:
        dt = dt or _now_utc()
        return datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)

    def week_current_bounds(self) -> Dict[str, Any]:
        start = self._week_start_utc()
        end = start + timedelta(days=7)
        label = f"{start.strftime('%d.%m.%y')} — {end.strftime('%d.%m.%y')}"
        return {"start": start.date().isoformat(), "end": end.date().isoformat(), "label": label}

    def month_current_bounds(self) -> Dict[str, Any]:
        start = self._month_start_utc()
        if start.month == 12:
            end = datetime(start.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(start.year, start.month + 1, 1, tzinfo=timezone.utc)
        label = f"{start.strftime('%d.%m.%y')} — {end.strftime('%d.%m.%y')}"
        return {"start": start.date().isoformat(), "end": end.date().isoformat(), "label": label}

    def _current_equity_map(self, chat_ids: List[int]) -> Dict[int, float]:
        if not chat_ids:
            return {}
        try:
            users = (
                self.client.table("users")
                .select("chat_id, balance, status")
                .in_("chat_id", chat_ids)
                .execute()
                .data
                or []
            )
        except Exception:
            users = []
        bal_map = {int(u["chat_id"]): _to_float(u.get("balance")) for u in users if u.get("status") == "approved"}

        try:
            shares = (
                self.client.table("user_shares")
                .select("user_chat_id, market_id, share_type, quantity")
                .in_("user_chat_id", chat_ids)
                .execute()
                .data
                or []
            )
        except Exception:
            shares = []

        if not shares:
            return bal_map

        market_ids = sorted({int(s["market_id"]) for s in shares if s.get("market_id") is not None})
        mk_map: Dict[int, dict] = {}
        if market_ids:
            try:
                mk = (
                    self.client.table("prediction_markets")
                    .select("id, total_yes_reserve, total_no_reserve")
                    .in_("id", market_ids)
                    .execute()
                    .data
                    or []
                )
                mk_map = {int(m["id"]): m for m in mk}
            except Exception:
                mk_map = {}

        for s in shares:
            uid = int(s["user_chat_id"])
            mid = int(s["market_id"])
            m = mk_map.get(mid)
            if not m:
                continue
            yes_r = _to_float(m.get("total_yes_reserve"))
            no_r = _to_float(m.get("total_no_reserve"))
            tot = yes_r + no_r
            yes_price = (no_r / tot) if tot > 0 else 0.5
            no_price = 1.0 - yes_price
            price = yes_price if s["share_type"] == "yes" else no_price
            val = _to_float(s["quantity"]) * price
            bal_map[uid] = bal_map.get(uid, 0.0) + val

        return bal_map

    def get_leaderboard_week(self, start_iso: str, limit: int = 50) -> List[dict]:
        try:
            users = (
                self.client.table("users")
                .select("chat_id, login")
                .eq("status", "approved")
                .execute()
                .data
                or []
            )
            chat_ids = [int(u["chat_id"]) for u in users]
            if not chat_ids:
                return []

            bl = (
                self.client.table("weekly_baselines")
                .select("user_chat_id, equity")
                .eq("week_start", start_iso)
                .in_("user_chat_id", chat_ids)
                .execute()
                .data
                or []
            )
            base_map = {int(b["user_chat_id"]): _to_float(b["equity"]) for b in bl}

            eq_map = self._current_equity_map(chat_ids)

            items: List[dict] = []
            for u in users:
                cid = int(u["chat_id"])
                cur = eq_map.get(cid, 0.0)
                base = base_map.get(cid, cur)
                earned = cur - base
                items.append({"chat_id": cid, "login": u.get("login"), "earned": float(round(earned, 2))})

            items.sort(key=lambda x: x["earned"], reverse=True)
            return items[:limit]
        except Exception as e:
            print("[get_leaderboard_week] error:", e)
            return []

    def get_leaderboard_month(self, start_iso: str, limit: int = 50) -> List[dict]:
        try:
            users = (
                self.client.table("users")
                .select("chat_id, login")
                .eq("status", "approved")
                .execute()
                .data
                or []
            )
            chat_ids = [int(u["chat_id"]) for u in users]
            if not chat_ids:
                return []

            bl = (
                self.client.table("monthly_baselines")
                .select("user_chat_id, equity")
                .eq("month_start", start_iso)
                .in_("user_chat_id", chat_ids)
                .execute()
                .data
                or []
            )
            base_map = {int(b["user_chat_id"]): _to_float(b["equity"]) for b in bl}

            eq_map = self._current_equity_map(chat_ids)

            items: List[dict] = []
            for u in users:
                cid = int(u["chat_id"])
                cur = eq_map.get(cid, 0.0)
                base = base_map.get(cid, cur)
                earned = cur - base
                items.append({"chat_id": cid, "login": u.get("login"), "earned": float(round(earned, 2))})

            items.sort(key=lambda x: x["earned"], reverse=True)
            return items[:limit]
        except Exception as e:
            print("[get_leaderboard_month] error:", e)
            return []

    # ---------------- ADMIN STATS ----------------
    def get_admin_stats(self) -> Dict[str, Any]:
        stats = {
            "open_markets": 0,
            "turnover_day": 0.0,
            "turnover_week": 0.0,
            "top_events": [],
        }
        try:
            # open markets
            try:
                r = self.client.table("prediction_markets").select("id", count="exact").eq("resolved", False).execute()
                stats["open_markets"] = int(r.count or (len(r.data) if r.data else 0))
            except Exception:
                r = self.client.table("prediction_markets").select("id", count="exact").execute()
                stats["open_markets"] = int(r.count or (len(r.data) if r.data else 0))
        except Exception:
            pass

        # turnover day/week
        now = _now_utc()
        day_dt = (now - timedelta(days=1)).isoformat()
        week_dt = (now - timedelta(days=7)).isoformat()
        try:
            d = (
                self.client.table("market_orders")
                .select("amount, created_at")
                .gte("created_at", day_dt)
                .execute()
                .data
                or []
            )
            stats["turnover_day"] = float(round(sum(_to_float(x.get("amount")) for x in d), 2))
        except Exception:
            pass
        try:
            w = (
                self.client.table("market_orders")
                .select("amount, market_id, created_at")
                .gte("created_at", week_dt)
                .execute()
                .data
                or []
            )
            stats["turnover_week"] = float(round(sum(_to_float(x.get("amount")) for x in w), 2))

            # top events по сумме amount за неделю
            if w:
                by_market: Dict[int, float] = {}
                for x in w:
                    mid = int(x["market_id"])
                    by_market[mid] = by_market.get(mid, 0.0) + _to_float(x["amount"])
                mids = list(by_market.keys())
                mk = (
                    self.client.table("prediction_markets")
                    .select("id, event_uuid")
                    .in_("id", mids)
                    .execute()
                    .data
                    or []
                )
                event_by_mid = {int(m["id"]): m["event_uuid"] for m in mk}
                by_event: Dict[str, float] = {}
                for mid, vol in by_market.items():
                    evu = event_by_mid.get(mid)
                    if evu:
                        by_event[evu] = by_event.get(evu, 0.0) + vol
                if by_event:
                    evs = (
                        self.client.table("events")
                        .select("event_uuid, name")
                        .in_("event_uuid", list(by_event.keys()))
                        .execute()
                        .data
                        or []
                    )
                    name_by_evu = {e["event_uuid"]: e["name"] for e in evs}
                    items = [{"name": name_by_evu.get(evu, evu), "volume": v} for evu, v in by_event.items()]
                    items.sort(key=lambda x: x["volume"], reverse=True)
                    stats["top_events"] = items[:10]
        except Exception as e:
            print("[get_admin_stats] error:", e)

        return stats

    # ---------------- TRADING (RPC) ----------------
    def trade_buy(
        self, chat_id: int, event_uuid: str, option_index: int, side: str, amount: float
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        try:
            market_id = self.get_market_id(event_uuid, option_index)
            if market_id is None:
                return {}, "market_not_found"

            payload = {
                "p_chat_id": chat_id,
                "p_market_id": market_id,
                "p_side": side,
                "p_amount": float(amount),
            }
            r = self.client.rpc("rpc_trade_buy", payload).execute()
            if not r.data:
                return {}, "trade_failed"

            row = r.data[0]
            result = {
                "got_shares": _to_float(row.get("got_shares")),
                "trade_price": _to_float(row.get("trade_price")),
                "new_balance": _to_float(row.get("new_balance")),
                "yes_price": _to_float(row.get("yes_price")),
                "no_price": _to_float(row.get("no_price")),
                "yes_reserve": _to_float(row.get("yes_reserve")),
                "no_reserve": _to_float(row.get("no_reserve")),
            }
            return result, None
        except Exception as e:
            print("[trade_buy rpc] error:", e)
            return {}, "rpc_error"

    # ---------------- RESOLVE / PAYOUTS ----------------
    def market_can_resolve(self, market_id: int) -> Tuple[bool, Optional[str]]:
        """
        Проверяем, наступило ли end_date у события, к которому принадлежит рынок.
        """
        try:
            mk = (
                self.client.table("prediction_markets")
                .select("event_uuid")
                .eq("id", market_id)
                .single()
                .execute()
            ).data
            if not mk:
                return False, "market_not_found"

            evu = mk["event_uuid"]
            ev = (
                self.client.table("events")
                .select("end_date")
                .eq("event_uuid", evu)
                .single()
                .execute()
            ).data
            if not ev:
                return False, "event_not_found"

            end_date = ev.get("end_date")
            if not end_date:
                return False, "no_end_date"

            try:
                edt = datetime.fromisoformat(str(end_date).replace(" ", "T"))
                if edt.tzinfo is None:
                    edt = edt.replace(tzinfo=timezone.utc)
            except Exception:
                return False, "bad_end_date"

            return (_now_utc() >= edt, None)
        except Exception as e:
            print("[market_can_resolve] error:", e)
            return False, "error"

    def resolve_market_by_id(self, market_id: int, winner_side: str) -> Tuple[Optional[dict], Optional[str]]:
        """
        Вызывает rpc_resolve_market_by_id(market_id, winner_side).
        Возвращает сводку (total_winners, total_payout, winner_side) или текст ошибки.
        """
        try:
            payload = {"p_market_id": market_id, "p_winner": winner_side}
            r = self.client.rpc("rpc_resolve_market_by_id", payload).execute()
            if not r.data:
                return None, "resolve_failed"
            row = r.data[0]
            return {
                "winner_side": row.get("winner_side", winner_side),
                "total_winners": int(row.get("total_winners", 0)),
                "total_payout": _to_float(row.get("total_payout")),
            }, None
        except Exception as e:
            msg = str(e)
            print("[resolve_market_by_id rpc] error:", msg)
            return None, msg or "rpc_error"


# Экспорт единственного экземпляра
db = Database()
