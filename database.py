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
                .update(
                    {
                        "status": "approved",
                        "approved_at": _now_utc().isoformat(),
                        "updated_at": _now_utc().isoformat(),
                    }
                )
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

            final_options = evt["options"]
            rows = []
            for idx, _ in enumerate(final_options):
                rows.append(
                    {
                        "event_uuid": event_uuid,
                        "option_index": idx,
                        "total_yes_reserve": 1000.0,
                        "total_no_reserve": 1000.0,
                        "constant_product": 1000000.0,
                    }
                )
            mr = self.client.table("prediction_markets").insert(rows).execute()
            if mr.data is None:
                return False, "markets_insert_failed"
            return True, None
        except Exception as e:
            print("[create_event_with_markets] error:", e)
            return False, "exception"

    # ---------------- GROUP RESOLVE (ADMIN) ----------------

    def _format_end_short(self, end_iso: str) -> str:
        try:
            dt = datetime.fromisoformat(end_iso.replace(" ", "T").split(".")[0])
            return dt.strftime("%d.%m.%y")
        except Exception:
            s = (end_iso or "")[:10]
            if len(s) == 10 and s[4] == "-" and s[7] == "-":
                y, m, d = s.split("-")
                return f"{d}.{m}.{y[2:]}"
            return (end_iso or "")[:10]

    def get_events_for_group_resolve(self) -> List[dict]:
        """
        Возвращает события, у которых достигнут end_date и есть нерешённые рынки.
        Формат: [
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

                options = e.get("options") or []
                rows = []
                for m in sorted(open_opts, key=lambda x: int(x["option_index"])):
                    idx = int(m["option_index"])
                    opt_text = "—"
                    if isinstance(options, list) and 0 <= idx < len(options):
                        o = options[idx]
                        opt_text = o.get("text") if isinstance(o, dict) else str(o)
                    rows.append({"option_index": idx, "option_text": opt_text})

                out.append(
                    {
                        "event_uuid": evu,
                        "name": e.get("name", "—"),
                        "end_short": self._format_end_short(str(e.get("end_date", ""))),
                        "open_count": len(rows),
                        "rows": rows,
                    }
                )
            return out
        except Exception as e:
            print("[get_events_for_group_resolve] error:", e)
            return []

    def resolve_event_by_winners(self, event_uuid: str, winners: Dict[str, str]) -> Tuple[bool, Optional[str]]:
        """
        winners: {"0":"yes","1":"no", ...} — для каждой опции обязательна пара yes/no.
        Закрывает все рынки события последовательно; каждая rpc_resolve_market_by_id атомарна в БД.
        """
        try:
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

            need = {int(m["option_index"]) for m in to_resolve}
            got = {int(k) for k in winners.keys() if str(k).isdigit()}
            if need != got:
                return False, "winners_incomplete"

            for m in sorted(to_resolve, key=lambda x: int(x["option_index"])):
                idx = int(m["option_index"])
                w = (winners.get(str(idx)) or winners.get(idx)) or ""
                w = str(w).lower()
                if w not in ("yes", "no"):
                    return False, f"bad_winner_for_{idx}"
                payload = {"p_market_id": int(m["id"]), "p_winner": w}
                rr = self.client.rpc("rpc_resolve_market_by_id", payload).execute()
                if not rr.data:
                    return False, "resolve_failed"
            return True, None
        except Exception as e:
            print("[resolve_event_by_winners] error:", e)
            return False, "rpc_error"

    # ---------------- POSITIONS / ARCHIVE ----------------

    def get_user_positions(self, chat_id: int) -> List[dict]:
        """Активные позиции (quantity>0) с текущими ценами из резервов."""
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
        Если в ledger есть market_id — обогащаем названием и опцией.
        Формат: {
          event_name, option_text, winner_side, payout, resolved_at, is_win
        }
        """
        out: List[dict] = []
        try:
            # Пробуем обогащать market_id (если колонка есть — мы её добавили в миграции)
            rows = (
                self.client.table("ledger")
                .select("chat_id, delta, reason, created_at, market_id")
                .eq("chat_id", chat_id)
                .order("created_at", desc=True)
                .limit(500)
                .execute()
                .data
                or []
            )
            payouts = [r for r in rows if str(r.get("reason", "")).startswith("payout_")]
            if not payouts:
                return []

            # Подтянем рынки и события
            mids = sorted({int(r["market_id"]) for r in payouts if r.get("market_id") is not None})
            pm_map: Dict[int, dict] = {}
            ev_map: Dict[str, dict] = {}
            if mids:
                pms = (
                    self.client.table("prediction_markets")
                    .select("id, event_uuid, option_index, winner_side, resolved_at")
                    .in_("id", mids)
                    .execute()
                    .data
                    or []
                )
                pm_map = {int(p["id"]): p for p in pms}
                evus = sorted({p["event_uuid"] for p in pms if p.get("event_uuid")})
                if evus:
                    evs = (
                        self.client.table("events")
                        .select("event_uuid, name, options")
                        .in_("event_uuid", evus)
                        .execute()
                        .data
                        or []
                    )
                    ev_map = {e["event_uuid"]: e for e in evs}

            for r in payouts:
                delta = _to_float(r.get("delta"))
                market_id = r.get("market_id")
                winner_side = "yes" if delta >= 0 else "no"  # fallback
                resolved_at = r.get("created_at")
                event_name = "—"
                option_text = "—"
                if market_id and int(market_id) in pm_map:
                    pm = pm_map[int(market_id)]
                    winner_side = pm.get("winner_side") or winner_side
                    resolved_at = pm.get("resolved_at") or resolved_at
                    ev = ev_map.get(pm.get("event_uuid") or "", {})
                    event_name = ev.get("name", "—")
                    try:
                        idx = int(pm.get("option_index") or 0)
                        opts = ev.get("options") or []
                        if isinstance(opts, list) and 0 <= idx < len(opts):
                            it = opts[idx]
                            option_text = it.get("text") if isinstance(it, dict) else str(it)
                    except Exception:
                        pass

                out.append(
                    {
                        "event_name": event_name,
                        "option_text": option_text,
                        "winner_side": winner_side,
                        "payout": round(delta, 4),
                        "resolved_at": resolved_at,
                        "is_win": delta > 0,
                    }
                )
            return out
        except Exception as e:
            print("[get_user_archive] error:", e)
            return []

    # ---------------- LEADERBOARD ----------------

    def week_current_bounds(self) -> Tuple[str, str]:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        return start.isoformat(), end.isoformat()

    def month_current_bounds(self) -> Tuple[str, str]:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start.isoformat(), end.isoformat()

    def leaderboard(self, start_iso: str, end_iso: str, limit: int = 50) -> List[dict]:
        # Сводим PnL по леджеру за период. При отсутствии buy_* в леджере — дополняем из market_orders.
        try:
            r = (
                self.client.table("ledger")
                .select("chat_id, delta, reason, created_at")
                .gte("created_at", start_iso)
                .lt("created_at", end_iso)
                .order("created_at", desc=False)
                .limit(20000)
                .execute()
            )
            rows = r.data or []
        except Exception as e:
            print("[leaderboard] ledger fetch error:", e)
            rows = []

        from collections import defaultdict

        agg = defaultdict(lambda: {"pnl": 0.0, "payouts": 0.0, "buys": 0.0, "trades": 0, "last_activity": None})
        for x in rows:
            chat_id = int(x["chat_id"])
            delta = float(x.get("delta") or 0)
            reason = str(x.get("reason") or "")
            ts = x.get("created_at")
            a = agg[chat_id]
            a["pnl"] += delta
            if reason.startswith("payout_"):
                a["payouts"] += delta
            if reason.startswith("buy_"):
                a["buys"] += delta  # отрицательное
                a["trades"] += 1
            a["last_activity"] = ts

        # Нет покупок в леджере? Подстрахуемся ордерами
        has_buys = any(a["buys"] != 0 for a in agg.values())
        if not has_buys:
            try:
                mo = (
                    self.client.table("market_orders")
                    .select("user_chat_id, amount, created_at")
                    .gte("created_at", start_iso)
                    .lt("created_at", end_iso)
                    .order("created_at", desc=False)
                    .limit(20000)
                    .execute()
                ).data or []
                for o in mo:
                    chat_id = int(o["user_chat_id"])
                    amount = float(o.get("amount") or 0)
                    a = agg[chat_id]
                    a["pnl"] -= amount
                    a["buys"] -= amount
                    a["trades"] += 1
                    a["last_activity"] = a["last_activity"] or o.get("created_at")
            except Exception as e:
                print("[leaderboard] orders supplement error:", e)

        chat_ids = list(agg.keys())
        users_map = {}
        if chat_ids:
            try:
                urows = (
                    self.client.table("users")
                    .select("chat_id, login, username")
                    .in_("chat_id", chat_ids)
                    .execute()
                ).data or []
                users_map = {int(u["chat_id"]): u for u in urows}
            except Exception:
                users_map = {}

        items = []
        for cid, a in agg.items():
            u = users_map.get(cid, {})
            items.append(
                {
                    "chat_id": cid,
                    "login": u.get("login") or str(cid),
                    "username": u.get("username"),
                    "pnl": round(a["pnl"], 4),
                    "payouts": round(a["payouts"], 4),
                    "buys_spent": round(-a["buys"], 4),  # «сколько потратил» (положительно)
                    "trades": int(a["trades"]),
                    "last_activity": a["last_activity"],
                }
            )

        items.sort(key=lambda x: (x["pnl"], x["payouts"]), reverse=True)
        return items[:limit]


# Singleton
db = Database()
