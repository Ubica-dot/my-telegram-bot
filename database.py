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
                    # триграммный индекс ускорит ilike
                    query = query.ilike("login", f"%{qq}%")
            if sort == "created_at":
                query = query.order("created_at", desc=False)
            elif sort == "balance":
                query = query.order("balance", desc=True)
            else:
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

    # Совместимость
    def update_user_balance(self, chat_id: int, new_balance: float) -> Optional[dict]:
        return self.admin_set_balance_via_ledger(chat_id, new_balance)

    # ---------------- EVENTS / MARKETS ----------------
    def get_published_events(self) -> List[dict]:
        try:
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

    # Создание события с тегами и (опционально) двойным исходом
    def create_event_with_markets(
        self,
        name: str,
        description: str,
        options: List[dict],
        end_date: str,
        tags: List[str],
        publish: bool,
        creator_id: Optional[int] = None,
        double_outcome: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        try:
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
                "options": options if not double_outcome else [{"text": "ДА"}, {"text": "НЕТ"}],
                "end_date": end_iso,
                "is_published": bool(publish),
                "creator_id": creator_id or None,
            }
            try:
                evt_with_tags = dict(evt)
                evt_with_tags["tags"] = tags or []
                er = self.client.table("events").insert(evt_with_tags).execute()
            except Exception:
                er = self.client.table("events").insert(evt).execute()

            if not er.data:
                return False, "event_insert_failed"

            # Автосоздание рынков по количеству опций
            final_options = evt["options"]
            rows = []
            for idx, _ in enumerate(final_options):
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

    # ---------------- GROUP RESOLVE (ADMIN) ----------------
    def get_events_for_group_resolve(self) -> List[dict]:
        """
        Возвращает события, у которых достигнут end_date и есть нерешённые рынки.
        Формат:
          [
            { event_uuid, name, end_short, open_count, rows: [{option_index, option_text}] }
          ]
        """
        try:
            now = _now_utc().isoformat()
            evs = (
                self.client.table("events")
                .select("event_uuid, name, options, end_date, is_published")
                .lte("end_date", now)
                .eq("is_published", True)
                .order("end_date", desc=False)
                .execute()
                .data
                or []
            )
            out: List[dict] = []
            for e in evs:
                evu = e["event_uuid"]
                mk = (
                    self.client.table("prediction_markets")
                    .select("id, option_index, resolved")
                    .eq("event_uuid", evu)
                    .execute()
                    .data
                    or []
                )
                open_opts = [m for m in mk if not bool(m.get("resolved"))]
                if not open_opts:
                    continue
                # option_text
                options = e.get("options") or []
                rows = []
                for m in sorted(open_opts, key=lambda x: int(x["option_index"])):
                    idx = int(m["option_index"])
                    opt_text = "—"
                    if isinstance(options, list) and 0 <= idx < len(options):
                        o = options[idx]
                        opt_text = o.get("text") if isinstance(o, dict) else str(o)
                    rows.append({"option_index": idx, "option_text": opt_text})
                out.append({
                    "event_uuid": evu,
                    "name": e.get("name", "—"),
                    "end_short": self._format_end_short(str(e.get("end_date", ""))),
                    "open_count": len(rows),
                    "rows": rows,
                })
            return out
        except Exception as e:
            print("[get_events_for_group_resolve] error:", e)
            return []

    def resolve_event_by_winners(self, event_uuid: str, winners: Dict[str, str]) -> Tuple[bool, Optional[str]]:
        """
        winners: {"0":"yes","1":"no", ...} — для каждой опции обязательна пара yes/no.
        Закрывает все рынки события атомарно на стороне приложения (последовательно),
        БД обеспечивает корректность (каждый рынок резолвится своей транзакцией).
        """
        try:
            # Проверка дедлайна
            ev = (
                self.client.table("events")
                .select("end_date, options")
                .eq("event_uuid", event_uuid)
                .single()
                .execute()
            ).data
            if not ev:
                return False, "event_not_found"

            end_date = ev.get("end_date")
            try:
                edt = datetime.fromisoformat(str(end_date).replace(" ", "T"))
                if edt.tzinfo is None:
                    edt = edt.replace(tzinfo=timezone.utc)
            except Exception:
                return False, "bad_end_date"
            if _now_utc() < edt:
                return False, "too_early"

            # Нерешённые рынки
            mk = (
                self.client.table("prediction_markets")
                .select("id, option_index, resolved")
                .eq("event_uuid", event_uuid)
                .execute()
                .data
                or []
            )
            to_resolve = [m for m in mk if not bool(m.get("resolved"))]
            if not to_resolve:
                return True, None

            # Должны быть указаны исходы по всем опциям
            need = {int(m["option_index"]) for m in to_resolve}
            got = {int(k) for k in winners.keys() if str(k).isdigit()}
            if need != got:
                return False, "winners_incomplete"

            # Резолвим рынки последовательно
            for m in sorted(to_resolve, key=lambda x: int(x["option_index"])):
                idx = int(m["option_index"])
                w = (winners.get(str(idx)) or winners.get(idx)) or ""
                w = w.lower()
                if w not in ("yes", "no"):
                    return False, f"bad_winner_for_{idx}"
                payload = {"p_market_id": int(m["id"]), "p_winner": w}
                # RPC — транзакционно на каждой функции
                rr = self.client.rpc("rpc_resolve_market_by_id", payload).execute()
                if not rr.data:
                    return False, "resolve_failed"
            return True, None
        except Exception as e:
            print("[resolve_event_by_winners] error:", e)
            return False, "rpc_error"

    # ---------------- POSITIONS / ARCHIVE ----------------
    def get_user_positions(self, chat_id: int) -> List[dict]:
        """
        Активные позиции (ненулевые) с актуальными ценами.
        """
        try:
            shares = (
                self.client.table("user_shares")
                .select("user_chat_id, market_id, share_type, quantity, average_price")
                .eq("user_chat_id", chat_id)
                .execute()
                .data
                or []
            )
            shares = [s for s in shares if _to_float(s.get("quantity")) > 0]
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

    def get_user_archive(self, chat_id: int) -> List[dict]:
        """
        Возвращает прошедшие ставки (выплаты), основано на ledger (reason payout_yes/no).
        Если в ledger есть market_id, обогащаем названием события и опцией.
        Формат элементов:
          {
            "event_name": str, "option_text": str, "winner_side": "yes"/"no",
            "payout": float, "resolved_at": iso, "is_win": bool
          }
        """
        out: List[dict] = []
        try:
            # Пытаемся получить market_id (если колонка есть)
            cols = (
                self.client.rpc("introspect_columns", {"p_table": "ledger"})
                .execute()
                .data
            )
        except Exception:
            cols = None

        has_market_id = False
        try:
            # introspect_columns — не стандартная функция. Если её нет — просто попробуем запросом.
            test = (
                self.client.table("ledger")
                .select("chat_id, delta, reason, market_id, created_at")
                .eq("chat_id", chat_id)
                .limit(1)
                .execute()
            )
            # если дошли сюда — колонка есть
            has_market_id = True
        except Exception:
            has_market_id = False

        try:
            if has_market_id:
                # Берём все выплаты с market_id
                pay = (
                    self.client.table("ledger")
                    .select("delta, reason, market_id, created_at")
                    .eq("chat_id", chat_id)
                    .in_("reason", ["payout_yes", "payout_no"])
                    .order("created_at", desc=True)
                    .limit(200)
                    .execute()
                    .data
                    or []
                )
                if not pay:
                    return []
                mids = sorted({int(p["market_id"]) for p in pay if p.get("market_id") is not None})
                if mids:
                    mk = (
                        self.client.table("prediction_markets")
                        .select("id, event_uuid, option_index, winner_side, resolved_at")
                        .in_("id", mids)
                        .execute()
                        .data
                        or []
                    )
                else:
                    mk = []
                mk_map = {int(m["id"]): m for m in mk}
                evu_set = sorted({m["event_uuid"] for m in mk if m.get("event_uuid")})
                ev_map = {}
                if evu_set:
                    evs = (
                        self.client.table("events")
                        .select("event_uuid, name, options")
                        .in_("event_uuid", evu_set)
                        .execute()
                        .data
                        or []
                    )
                    ev_map = {e["event_uuid"]: e for e in evs}

                for p in pay:
                    mid = int(p["market_id"])
                    m = mk_map.get(mid)
                    if not m:
                        # fallback
                        out.append({
                            "event_name": "—",
                            "option_text": "—",
                            "winner_side": "yes" if p["reason"] == "payout_yes" else "no",
                            "payout": _to_float(p["delta"]),
                            "resolved_at": p.get("created_at"),
                            "is_win": True if p["delta"] and _to_float(p["delta"]) > 0 else False,
                        })
                        continue
                    ev = ev_map.get(m["event_uuid"], {})
                    options = ev.get("options") or []
                    opt_text = "—"
                    try:
                        idx = int(m.get("option_index", 0))
                        if isinstance(options, list) and 0 <= idx < len(options):
                            item = options[idx]
                            opt_text = item.get("text") if isinstance(item, dict) else str(item)
                    except Exception:
                        pass
                    out.append({
                        "event_name": ev.get("name", "—"),
                        "option_text": opt_text,
                        "winner_side": (m.get("winner_side") or ("yes" if p["reason"] == "payout_yes" else "no")),
                        "payout": _to_float(p["delta"]),
                        "resolved_at": m.get("resolved_at") or p.get("created_at"),
                        "is_win": True if p["delta"] and _to_float(p["delta"]) > 0 else False,
                    })
                return out
            else:
                # Без market_id — покажем хотя бы факт выплат
                pay = (
                    self.client.table("ledger")
                    .select("delta, reason, created_at")
                    .eq("chat_id", chat_id)
                    .in_("reason", ["payout_yes", "payout_no"])
                    .order("created_at", desc=True)
                    .limit(200)
                    .execute()
                    .data
                    or []
                )
                for p in pay:
                    out.append({
                        "event_name": "—",
                        "option_text": "—",
                        "winner_side": "yes" if p["reason"] == "payout_yes" else "no",
                        "payout": _to_float(p["delta"]),
                        "resolved_at": p.get("created_at"),
                        "is_win": True if p["delta"] and _to_float(p["delta"]) > 0 else False,
                    })
                return out
        except Exception as e:
            print("[get_user_archive] error:", e)
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
            try:
                r = self.client.table("prediction_markets").select("id", count="exact").eq("resolved", False).execute()
                stats["open_markets"] = int(r.count or (len(r.data) if r.data else 0))
            except Exception:
                r = self.client.table("prediction_markets").select("id", count="exact").execute()
                stats["open_markets"] = int(r.count or (len(r.data) if r.data else 0))
        except Exception:
            pass

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

    # ---------------- Helpers ----------------
    @staticmethod
    def _format_end_short(end_iso: str) -> str:
        try:
            dt = datetime.fromisoformat(end_iso.replace(" ", "T").split(".")[0])
            return dt.strftime("%d.%m.%y")
        except Exception:
            s = (end_iso or "")[:10]
            if len(s) == 10 and s[4] == "-" and s[7] == "-":
                y, m, d = s.split("-")
                return f"{d}.{m}.{y[2:]}"
            return (end_iso or "")[:10]


# Экспорт единственного экземпляра
db = Database()
