import os
import json
import hmac
import hashlib
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl
from functools import wraps
from collections import deque, defaultdict

import requests
from flask import (
    Flask, request, render_template_string, jsonify, Response,
    stream_with_context, redirect, url_for
)

from database import db  # Supabase client под капотом

app = Flask(__name__)

# --- ENV ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
BASE_URL = os.getenv("WEBHOOK_URL")
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "change-me")
ADMIN_BASIC_USER = os.getenv("ADMIN_BASIC_USER", "admin")
ADMIN_BASIC_PASS = os.getenv("ADMIN_BASIC_PASS", "admin")
WEBAPP_SIGNING_SECRET = os.getenv("WEBAPP_SIGNING_SECRET")  # обязателен

# ---------- Admin auth ----------
def _check_auth(u, p):
    return u == ADMIN_BASIC_USER and p == ADMIN_BASIC_PASS

def _auth_required():
    return Response("Auth required", 401, {"WWW-Authenticate": 'Basic realm="Admin"'})

def requires_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return _auth_required()
        return fn(*args, **kwargs)
    return wrapper

# ---------- Utils ----------
def send_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, data=data, timeout=10)
        return r.ok
    except Exception as e:
        print(f"[send_message] error: {e}")
        return False

def notify_admin(text: str):
    if ADMIN_ID:
        send_message(ADMIN_ID, text)

def ensure_webhook():
    if not (BASE_URL and TOKEN):
        print("[setWebhook] skipped: BASE_URL or TOKEN missing")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/setWebhook",
            json={"url": f"{BASE_URL}/webhook", "secret_token": TELEGRAM_SECRET_TOKEN},
            timeout=10,
        )
        print(f"[setWebhook] {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[setWebhook] error: {e}")

@app.before_request
def _init_once():
    if not getattr(app, "_init_done", False):
        ensure_webhook()
        app._init_done = True

def make_sig(chat_id: int) -> str:
    if not WEBAPP_SIGNING_SECRET:
        return ""
    msg = str(chat_id).encode()
    return hmac.new(WEBAPP_SIGNING_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:32]

def verify_sig(chat_id: int, sig: str) -> bool:
    if not WEBAPP_SIGNING_SECRET:
        return False
    return hmac.compare_digest(make_sig(chat_id), sig or "")

# ---------- WebApp initData verification ----------
def verify_telegram_init_data(init_data: str, max_age: int = 86400):
    if not init_data:
        return None, "no_init"
    if not TOKEN:
        return None, "no_token"
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        recv_hash = (pairs.pop("hash", "") or "").lower()
        if not recv_hash:
            return None, "no_hash"
        try:
            auth_date = int(pairs.get("auth_date", "0") or "0")
        except Exception:
            return None, "bad_auth_date"
        if auth_date <= 0 or (int(time.time()) - auth_date) > max_age:
            return None, "stale"

        data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs.keys()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, recv_hash):
            return None, "bad_hash"

        user = {}
        if "user" in pairs and pairs["user"]:
            try:
                user = json.loads(pairs["user"])
            except Exception:
                user = {}

        payload = {
            "auth_date": auth_date,
            "query_id": pairs.get("query_id"),
            "user": user,
            "user_id": int(user["id"]) if isinstance(user, dict) and "id" in user else None,
            "raw": pairs,
        }
        return payload, None
    except Exception as e:
        print("[verify_init] exception:", e)
        return None, "exception"

def auth_chat_id_from_request():
    init_str = request.args.get("init")
    payload = None
    if request.method in ("POST", "PUT", "PATCH"):
        payload = request.get_json(silent=True) or {}
        if not init_str:
            init_str = payload.get("init")

    if init_str:
        info, err = verify_telegram_init_data(init_str)
        if err:
            return None, f"bad_init:{err}"
        user_id = info.get("user_id")
        if not user_id:
            return None, "bad_init:no_user"
        chat_id_param = request.args.get("chat_id", type=int)
        if chat_id_param is None and payload:
            try:
                chat_id_param = int(payload.get("chat_id")) if payload.get("chat_id") is not None else None
            except Exception:
                chat_id_param = None
        if chat_id_param is not None and chat_id_param != user_id:
            return None, "chat_id_mismatch"
        return user_id, None

    chat_id = request.args.get("chat_id", type=int)
    sig = request.args.get("sig", "")
    if payload:
        chat_id = chat_id if chat_id is not None else (int(payload.get("chat_id")) if payload.get("chat_id") else None)
        sig = sig or (payload.get("sig") or "")
    if not chat_id or not verify_sig(chat_id, sig):
        return None, "bad_sig"
    return chat_id, None

# ---------- helper для /mini-app: короткая дата (фикс 500) ----------
def _format_end_short(end_iso: str) -> str:
    try:
        dt = datetime.fromisoformat((end_iso or "").replace(" ", "T").split(".")[0])
        return dt.strftime("%d.%m.%y")
    except Exception:
        s = (end_iso or "")[:10]
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            y, m, d = s.split("-")
            return f"{d}.{m}.{y[2:]}"
        return s

# ---------- Health / Legal ----------
@app.get("/health")
def health():
    return jsonify(ok=True)

@app.get("/")
def index():
    return "OK"

LEGAL_HTML = """
Правила и политика конфиденциальности
# Условия использования

Это учебная платформа предсказательных рынков. Нет реальных денег. Котировки волатильны — вы можете потерять игровые кредиты.
## Политика конфиденциальности

Храним минимальные данные Telegram (chat_id, логин), историю сделок и баланс. Данные не продаются и не передаются третьим лицам.

Вопросы — админу бота.
"""

@app.get("/legal")
def legal():
    return Response(LEGAL_HTML, mimetype="text/html")

# ---------- Telegram webhook ----------
@app.post("/webhook")
def telegram_webhook():
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if TELEGRAM_SECRET_TOKEN and secret != TELEGRAM_SECRET_TOKEN:
        return "forbidden", 403

    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message")
    if not message:
        return "ok"

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    username = message.get("from", {}).get("username", "") or "нет"

    user = db.get_user(chat_id)
    status = (user or {}).get("status")

    if text == "/start":
        if not user:
            send_message(
                chat_id,
                "Добро пожаловать! Напишите ваш желаемый логин одним сообщением.\nПосле модерации получите доступ к приложению.",
            )
        else:
            if status == "approved":
                sig = make_sig(chat_id)
                if not sig:
                    send_message(chat_id, "Сервис временно недоступен. Повторите позже.")
                    return "ok"
                web_app_url = f"https://{request.host}/mini-app?chat_id={chat_id}&sig={sig}&v={int(time.time())}"
                kb = {"inline_keyboard": [[{"text": "Открыть Mini App", "web_app": {"url": web_app_url}}]]}
                send_message(chat_id, "Приложение готово.\nОткрывайте:", kb)
            elif status == "pending":
                send_message(chat_id, "⏳ Ваша заявка на регистрацию ожидает проверки администратором.")
            elif status == "banned":
                send_message(chat_id, "⛔ Доступ к приложению запрещён.")
            elif status == "rejected":
                send_message(chat_id, "❌ Заявка отклонена.\nОтправьте новый логин одним сообщением для повторной подачи.")
            else:
                send_message(chat_id, "Напишите ваш логин одним сообщением для регистрации.")
        return "ok"

    # логин без команды
    if not text.startswith("/"):
        if not user:
            new_user = db.create_user(chat_id, text, username)
            if new_user:
                send_message(chat_id, f"✅ Логин '{text}' отправлен на модерацию.\nОжидайте подтверждения.")
                notify_admin(
                    f"Новая заявка:\nЛогин: {text}\nID: {chat_id}\nUsername: @{username}\nАдминка: {BASE_URL}/admin"
                )
            else:
                send_message(chat_id, "❌ Ошибка при создании заявки. Попробуйте ещё раз.")
        else:
            if status == "pending":
                send_message(chat_id, "⏳ Заявка уже на рассмотрении.\nОжидайте ответа администратора.")
            elif status == "banned":
                send_message(chat_id, "⛔ Доступ запрещён.")
        return "ok"

    return "ok"

# ---------- Mini App HTML (твоя вёрстка сохранена) ----------
MINI_APP_HTML = """
U — мини‑приложение

U

—

Баланс: —

<h2>Активные ставки ▾</h2>
<label><input type="checkbox"/> только закладки</label>
<div id="active">Загрузка...</div>

<h2>Мероприятия ▾</h2>
<label><input type="checkbox"/> только закладки</label>
<div id="events"> </div>

<h2>Прошедшие ставки (архив) ▾</h2>
<label><input type="checkbox"/> только закладки</label>
<div id="archive">Загрузка...</div>

<h2>Таблица лидеров ▸</h2>
<button data-period="week">Неделя</button> <button data-period="month">Месяц</button>
<div id="leaders">—</div>

<p><a href="/legal" target="_blank" rel="noopener">Правила и политика конфиденциальности</a></p>

<!-- Покупка -->
<h3>Покупка</h3>
<p>Укажите сумму, не выше вашего баланса.</p>
<label>Сумма (кредиты):</label>
<input type="number" step="0.01"/>
<button>Купить</button> <button>Отмена</button>
"""

# ---------- Admin: мероприятия (список + график + резолв) ----------
ADMIN_EVENTS_HTML = """
Admin · Мероприятия  <a href="/admin">← Админ</a>

# Мероприятия

<form method="get" action="/admin/events" style="margin:8px 0">
  <input type="text" name="q" value="{{q or ''}}" placeholder="Искать"/>
  <button type="submit">Искать</button>
</form>

<h2>Активные</h2>
{% for ev in active %}
  <details style="margin:8px 0">
    <summary>
      <b>UUID</b> {{ev.event_uuid}} |
      <b>Название</b> {{ev.name}} |
      <b>Дедлайн</b> {{ev.end_date}} |
      <b>Опубл.</b> {{'да' if ev.is_published else 'нет'}} |
      <b>Рынков</b> {{ev.markets_count}}
    </summary>
    <div style="margin:6px 0">{{ev.description}}</div>
    <button onclick="resolveEvent('{{ev.event_uuid}}')">Закрыть событие</button>
    <div id="markets_{{ev.event_uuid}}">Загрузка вариантов…</div>
    <div style="margin-top:8px">
      {% for t,r in [('1Ч','1h'),('6Ч','6h'),('1Д','1d'),('1Н','1w'),('1М','1m'),('ВСЕ','all')] %}
        <button class="range-btn" data-range="{{r}}" data-event="{{ev.event_uuid}}">{{t}}</button>
      {% endfor %}
    </div>
  </details>
{% endfor %}
{% if not active %}<div>Нет активных</div>{% endif %}

<h2>Прошедшие</h2>
{% for ev in past %}
  <details style="margin:8px 0">
    <summary>
      <b>UUID</b> {{ev.event_uuid}} |
      <b>Название</b> {{ev.name}} |
      <b>Дедлайн</b> {{ev.end_date}} |
      <b>Опубл.</b> {{'да' if ev.is_published else 'нет'}} |
      <b>Рынков</b> {{ev.markets_count}}
    </summary>
    <div style="margin:6px 0">{{ev.description}}</div>
    <button onclick="resolveEvent('{{ev.event_uuid}}')">Закрыть событие</button>
    <div id="markets_{{ev.event_uuid}}">Загрузка вариантов…</div>
    <div style="margin-top:8px">
      {% for t,r in [('1Ч','1h'),('6Ч','6h'),('1Д','1d'),('1Н','1w'),('1М','1m'),('ВСЕ','all')] %}
        <button class="range-btn" data-range="{{r}}" data-event="{{ev.event_uuid}}">{{t}}</button>
      {% endfor %}
    </div>
  </details>
{% endfor %}
{% if not past %}<div>Нет прошедших</div>{% endif %}

<script>
async function resolveEvent(event_uuid) {
  const mwrap = document.getElementById('markets_'+event_uuid);
  mwrap.textContent = 'Загрузка…';
  const r = await fetch('/api/admin/event_markets?event_uuid='+encodeURIComponent(event_uuid));
  const data = await r.json();
  const opts = data.options || [];
  const markets = data.markets || [];
  // простая форма выбора победителей
  const form = document.createElement('form');
  form.innerHTML = '<div><b>Варианты</b></div>';
  markets.forEach(m => {
    const idx = m.option_index;
    const label = (opts[idx] || ('Вариант '+idx));
    const cur = m.winner_side || '';
    form.innerHTML += `
      <div style="margin:4px 0">
        <b>${label}</b>:
        <label><input type="radio" name="w_${idx}" value="yes" ${cur==='yes'?'checked':''}/> ДА</label>
        <label><input type="radio" name="w_${idx}" value="no"  ${cur==='no'?'checked':''}/> НЕТ</label>
        <small>${m.resolved ? '(уже закрыт)' : ''}</small>
      </div>`;
  });
  form.innerHTML += '<button type="submit">Резолв</button>';
  mwrap.innerHTML = '';
  mwrap.appendChild(form);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const winners = {};
    markets.forEach(m => {
      const v = form.querySelector(`input[name="w_${m.option_index}"]:checked`);
      if (v) winners[m.option_index] = v.value;
    });
    const resp = await fetch('/admin/events/resolve', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({event_uuid, winners, require_all:false})
    });
    const jr = await resp.json();
    alert(jr.success ? ('Закрыто рынков: '+jr.closed) : ('Ошибка: '+jr.error));
    location.reload();
  });
}
</script>
"""

# --- ДОБАВЛЕННАЯ мини‑форма создания события (рендерится ПЕРЕД ADMIN_EVENTS_HTML, сам шаблон не меняем) ---
ADMIN_EVENTS_CREATE_FORM = """
<h2>Создать событие</h2>
<form method="post" action="/admin/events/create">
  <div>
    <label>Название</label><br/>
    <input type="text" name="name" required style="width: 420px"/>
  </div>
  <div>
    <label>Описание</label><br/>
    <textarea name="description" required rows="3" style="width: 420px"></textarea>
  </div>
  <div>
    <label>Варианты (по одному на строку). Если включить «Двойной исход», список будет проигнорирован.</label><br/>
    <textarea name="options" rows="3" style="width: 420px" placeholder="Вариант 1&#10;Вариант 2"></textarea>
  </div>
  <div>
    <label>Дедлайн</label><br/>
    <input type="datetime-local" name="end_date" required/>
  </div>
  <div>
    <label>Теги (через запятую)</label><br/>
    <input type="text" name="tags" placeholder="спорт, политика"/>
  </div>
  <div style="margin-top:6px">
    <label><input type="checkbox" name="publish" value="1"/> Опубликовать сразу</label><br/>
    <label><input type="checkbox" name="double_outcome" value="1"/> Двойной исход (ДА/НЕТ)</label>
  </div>
  <div style="margin-top:8px">
    <button type="submit">Создать</button>
  </div>
</form>
<hr/>
"""

# ---------- Admin: маршруты мероприятий ----------
@app.get("/admin")
@requires_auth
def admin_home():
    return render_template_string(
        """Admin
# Админ

- <a href="/admin/events">Мероприятия</a><br/>
- <a href="/admin/users">Пользователи</a><br/>
- <a href="/admin/reset">Сброс данных (стенд)</a>
"""
    )

@app.get("/admin/events")
@requires_auth
def admin_events():
    q = (request.args.get("q") or "").strip().lower()
    try:
        evs = (
            db.client.table("events")
            .select("event_uuid,name,description,options,end_date,is_published,created_at,tags")
            .order("created_at", desc=True)
            .execute()
            .data or []
        )
    except Exception as e:
        print("[/admin/events] fetch error:", e)
        evs = []

    now = datetime.now(timezone.utc)

    def match(e):
        if not q:
            return True
        return any([
            q in str(e.get("event_uuid","")).lower(),
            q in str(e.get("name","")).lower(),
            q in ",".join([str(t).lower() for t in (e.get("tags") or [])])
        ])

    filt = [e for e in evs if match(e)]
    uuids = [e["event_uuid"] for e in filt]
    pm_map = {}
    if uuids:
        try:
            pms = (
                db.client.table("prediction_markets")
                .select("event_uuid,id")
                .in_("event_uuid", uuids)
                .execute()
                .data or []
            )
            from collections import Counter
            pm_map = dict(Counter([p["event_uuid"] for p in pms]))
        except Exception:
            pm_map = {}

    def enrich(e):
        e2 = dict(e)
        e2["markets_count"] = pm_map.get(e["event_uuid"], 0)
        return e2

    active = [enrich(e) for e in filt if e.get("end_date") and datetime.fromisoformat(str(e["end_date"]).replace(" ","T")) > now]
    past   = [enrich(e) for e in filt if e.get("end_date") and datetime.fromisoformat(str(e["end_date"]).replace(" ","T")) <= now]

    # ВАЖНО: не меняем твой шаблон — просто добавляем форму сверху
    return render_template_string(ADMIN_EVENTS_CREATE_FORM + ADMIN_EVENTS_HTML, active=active, past=past, q=q)

@app.get("/api/admin/event_markets")
@requires_auth
def api_admin_event_markets():
    evu = (request.args.get("event_uuid") or "").strip()
    if not evu:
        return jsonify({"error": "no_event_uuid"}), 400
    try:
        ev = (
            db.client.table("events")
            .select("options")
            .eq("event_uuid", evu)
            .single()
            .execute()
            .data or {}
        )
        opts = []
        for o in (ev.get("options") or []):
            opts.append(o.get("text") if isinstance(o, dict) else str(o))

        pms = (
            db.client.table("prediction_markets")
            .select("id, option_index, total_yes_reserve, total_no_reserve, resolved, winner_side")
            .eq("event_uuid", evu)
            .order("option_index", desc=False)
            .execute()
            .data or []
        )
        return jsonify({"options": opts, "markets": pms})
    except Exception as e:
        print("[/api/admin/event_markets] error:", e)
        return jsonify({"options": [], "markets": []}), 200

@app.post("/admin/events/resolve")
@requires_auth
def admin_events_resolve():
    payload = request.get_json(silent=True) or {}
    evu = (payload.get("event_uuid") or "").strip()
    winners = payload.get("winners") or {}  # { "0":"yes", "1":"no", ... }
    require_all = bool(payload.get("require_all"))
    if not evu or not isinstance(winners, dict):
        return jsonify(success=False, error="bad_payload"), 400
    try:
        ev = (
            db.client.table("events")
            .select("end_date")
            .eq("event_uuid", evu)
            .single()
            .execute()
            .data
        )
        if not ev:
            return jsonify(success=False, error="event_not_found"), 404
        end_dt = datetime.fromisoformat(str(ev["end_date"]).replace(" ","T"))
        now = datetime.now(timezone.utc)

        pms = (
            db.client.table("prediction_markets")
            .select("id, option_index, resolved")
            .eq("event_uuid", evu)
            .order("option_index", desc=False)
            .execute()
            .data or []
        )
        unresolved = [m for m in pms if not m.get("resolved")]
        if require_all:
            need = {int(m["option_index"]) for m in unresolved}
            got = {int(k) for k in winners.keys() if str(k).isdigit()}
            if need != got:
                return jsonify(success=False, error="winners_incomplete"), 400

        closed = 0
        payout_total = 0.0
        for m in unresolved:
            idx = int(m["option_index"])
            w = (winners.get(str(idx)) or winners.get(idx) or "").lower()
            if w not in ("yes","no"):
                if require_all:
                    return jsonify(success=False, error=f"winner_missing_for_option_{idx}"), 400
                continue
            try:
                if now < end_dt:
                    rr = (
                        db.client.rpc("rpc_resolve_market_force", {"p_market_id": int(m["id"]), "p_winner": w})
                        .execute().data or []
                    )
                else:
                    rr = (
                        db.client.rpc("rpc_resolve_market_by_id", {"p_market_id": int(m["id"]), "p_winner": w})
                        .execute().data or []
                    )
                if rr:
                    closed += 1
                    payout_total += float(rr[0].get("total_payout") or 0)
            except Exception as e:
                print("[resolve_one] error:", e)
                continue

        return jsonify(success=True, closed=closed, payout_total=round(payout_total,4))
    except Exception as e:
        print("[/admin/events/resolve] error:", e)
        return jsonify(success=False, error="server_error"), 500

# ---------- ДОБАВЛЕНО: создание события (POST) ----------
@app.post("/admin/events/create")
@requires_auth
def admin_events_create():
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    options_raw = (request.form.get("options") or "").strip()
    end_date = (request.form.get("end_date") or "").strip()  # 'YYYY-MM-DDTHH:MM'
    tags_raw = (request.form.get("tags") or "").strip()
    publish = bool(request.form.get("publish"))
    double_outcome = bool(request.form.get("double_outcome"))

    if not name or not description or not end_date:
        return redirect(url_for("admin_events"))

    options = []
    if not double_outcome:
        lines = [l.strip() for l in options_raw.splitlines() if l.strip()]
        if len(lines) < 2:
            return redirect(url_for("admin_events"))
        options = [{"text": l} for l in lines]

    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
    try:
        creator_id = int(ADMIN_ID) if ADMIN_ID else None
    except Exception:
        creator_id = None

    ok, err = db.create_event_with_markets(
        name=name,
        description=description,
        options=options,
        end_date=end_date,
        tags=tags,
        publish=publish,
        creator_id=creator_id,
        double_outcome=double_outcome,
    )
    if not ok:
        print("[/admin/events/create] error:", err)

    return redirect(url_for("admin_events"))

# ---------- Admin: сброс данных (стенд) ----------
ADMIN_RESET_HTML = """
Admin · Сброс данных  <a href="/admin">← Админ</a>
# Сброс данных (стенд)

Удалит события/рынки/ордера/позиции/леджер и сбросит балансы к 1000. Введите «RESET» для подтверждения.

<form method="post" action="/admin/reset">
  <div><label><input type="checkbox" name="wipe_events"/> Удалить события и рынки</label></div>
  <div><label><input type="checkbox" name="wipe_trades"/> Удалить ордера/позиции/леджер</label></div>
  <div><label><input type="checkbox" name="reset_balances"/> Сбросить балансы пользователей к 1000</label></div>
  <div style="margin:6px 0">
    <input type="text" name="confirm" placeholder="RESET"/>
  </div>
  <button type="submit">Сбросить</button>
</form>

{% if msg %}
  <div style="margin-top:8px">{{msg}}</div>
{% endif %}
"""

@app.get("/admin/reset")
@requires_auth
def admin_reset_get():
    return render_template_string(ADMIN_RESET_HTML, msg=request.args.get("msg",""))

@app.post("/admin/reset")
@requires_auth
def admin_reset_post():
    confirm = (request.form.get("confirm") or "").strip().upper()
    if confirm != "RESET":
        return redirect(url_for("admin_reset_get", msg="Нужно ввести RESET"))
    wipe_events = bool(request.form.get("wipe_events"))
    wipe_trades = bool(request.form.get("wipe_trades"))
    reset_balances = bool(request.form.get("reset_balances"))
    try:
        if wipe_trades:
            db.client.table("ledger").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
            db.client.table("market_orders").delete().gte("id", 0).execute()
            db.client.table("user_shares").delete().gte("id", 0).execute()
        if wipe_events:
            db.client.table("prediction_markets").delete().gte("id", 0).execute()
            db.client.table("events").delete().gte("id", 0).execute()
        if reset_balances:
            db.client.table("users").update({"balance": 1000.0}).neq("chat_id", None).execute()
        return redirect(url_for("admin_reset_get", msg="Готово"))
    except Exception as e:
        print("[/admin/reset] error:", e)
        return redirect(url_for("admin_reset_get", msg="Ошибка сброса"))

# ---------- Mini‑app + API ----------
RL_USER_WINDOW = 10
RL_USER_LIMIT  = 5
RL_IP_WINDOW   = 60
RL_IP_LIMIT    = 30
_rl_user = defaultdict(deque)
_rl_ip   = defaultdict(deque)

def _now_ts() -> float:
    return time.time()

def _client_ip() -> str:
    xfwd = request.headers.get("X-Forwarded-For", "") or ""
    if xfwd:
        return xfwd.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"

def _check_rate(chat_id: int) -> bool:
    t = _now_ts()
    dq = _rl_user[chat_id]
    while dq and t - dq[0] > RL_USER_WINDOW:
        dq.popleft()
    if len(dq) >= RL_USER_LIMIT:
        return False
    dq.append(t)

    ip = _client_ip()
    di = _rl_ip[ip]
    while di and t - di[0] > RL_IP_WINDOW:
        di.popleft()
    if len(di) >= RL_IP_LIMIT:
        dq.pop()
        return False
    di.append(t)
    return True

@app.get("/mini-app")
def mini_app():
    chat_id = request.args.get("chat_id", type=int)
    sig = request.args.get("sig", "")
    if not chat_id or not sig or not verify_sig(chat_id, sig):
        return Response("""
### Доступ запрещён

Откройте Mini App из бота после /start и одобрения.

""", mimetype="text/html")

    user = db.get_user(chat_id)
    if not user or user.get("status") != "approved":
        return Response("""

### Доступ только для одобренных пользователей

Дождитесь одобрения администратора.

""", mimetype="text/html")
    if user.get("status") == "banned":
        return Response("### Доступ запрещён", mimetype="text/html")

    events = db.get_published_events()
    for e in events:
        end_iso = str(e.get("end_date", ""))
        e["end_short"] = _format_end_short(end_iso)

        mk = db.get_markets_for_event(e["event_uuid"])
        markets = {}
        event_total_volume = 0.0
        for m in mk:
            yes = float(m["total_yes_reserve"])
            no = float(m["total_no_reserve"])
            total = yes + no
            yp = (no / total) if total > 0 else 0.5
            volume = max(0.0, total - 2000.0)
            event_total_volume += volume
            markets[m["option_index"]] = {
                "yes_price": yp,
                "volume": volume,
                "end_short": e["end_short"],
                "resolved": bool(m.get("resolved")) if isinstance(m, dict) else False,
                "winner_side": m.get("winner_side") if isinstance(m, dict) else None,
            }
        e["markets"] = markets
        e["tags"] = e.get("tags") or []
        try:
            dt = datetime.fromisoformat(end_iso.replace(" ", "T"))
            e["end_ts"] = int(dt.replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            e["end_ts"] = 0
        e["total_volume"] = event_total_volume

    return render_template_string(MINI_APP_HTML, events=events, enumerate=enumerate)

@app.get("/api/me")
def api_me():
    chat_id, err = auth_chat_id_from_request()
    if err:
        return jsonify(success=False, error=err), 403
    u = db.get_user(chat_id)
    if not u:
        return jsonify(success=False, error="user_not_found"), 404
    if u.get("status") != "approved":
        return jsonify(success=False, error="not_approved"), 403

    positions = db.get_user_positions(chat_id)
    archive = db.get_user_archive(chat_id)

    return jsonify(
        success=True,
        user={"chat_id": chat_id, "balance": float(u.get("balance", 0)), "login": u.get("login")},
        positions=positions,
        archive=archive,
    )

@app.post("/api/market/buy")
def api_market_buy():
    chat_id, err = auth_chat_id_from_request()
    if err:
        return jsonify(success=False, error=err), 403
    if not _check_rate(chat_id):
        return jsonify(success=False, error="rate_limited"), 429

    payload = request.get_json(silent=True) or {}
    try:
        event_uuid = str(payload.get("event_uuid"))
        option_index = int(payload.get("option_index"))
        side = str(payload.get("side")).lower()
        amount = float(payload.get("amount"))
        if side not in ("yes", "no"):
            raise ValueError
        if not (0 < amount <= 1_000_000):
            raise ValueError
    except Exception:
        return jsonify(success=False, error="bad_payload"), 400

    # Получить market_id и вызвать RPC
    market_id = db.get_market_id(event_uuid, option_index)
    if not market_id:
        return jsonify(success=False, error="market_not_found"), 404

    try:
        rr = (
            db.client.rpc(
                "rpc_trade_buy",
                {"p_chat_id": chat_id, "p_market_id": market_id, "p_side": side, "p_amount": amount},
            )
            .execute()
            .data or []
        )
        if not rr:
            return jsonify(success=False, error="rpc_failed"), 400
        row = rr[0]
        result = {
            "got_shares": float(row["got_shares"]),
            "trade_price": float(row["trade_price"]),
            "new_balance": float(row["new_balance"]),
            "yes_price": float(row["yes_price"]),
            "no_price": float(row["no_price"]),
            "yes_reserve": float(row["yes_reserve"]),
            "no_reserve": float(row["no_reserve"]),
        }
    except Exception as e:
        print("[api_market_buy] rpc error:", e)
        return jsonify(success=False, error="rpc_error"), 500

    return jsonify(
        success=True,
        trade={
            "got_shares": result["got_shares"],
            "trade_price": result["trade_price"],
            "new_balance": result["new_balance"],
        },
        market={
            "yes_price": result["yes_price"],
            "no_price": result["no_price"],
            "yes_reserve": result["yes_reserve"],
            "no_reserve": result["no_reserve"],
        },
    )

@app.get("/api/userpic")
def api_userpic():
    chat_id, err = auth_chat_id_from_request()
    if err:
        return "bad_auth", 403
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/getUserProfilePhotos"
        r = requests.get(url, params={"user_id": chat_id, "limit": 1}, timeout=10)
        data = r.json()
        photos = (data or {}).get("result", {}).get("photos", [])
        if not photos:
            return Response(status=204)
        sizes = photos[0] or []
        if not sizes:
            return Response(status=204)
        file_id = sizes[-1]["file_id"]
        r2 = requests.get(f"https://api.telegram.org/bot{TOKEN}/getFile", params={"file_id": file_id}, timeout=10)
        fp = r2.json().get("result", {}).get("file_path")
        if not fp:
            return Response(status=204)
        furl = f"https://api.telegram.org/file/bot{TOKEN}/{fp}"
        fr = requests.get(furl, timeout=10, stream=True)
        headers = {"Content-Type": fr.headers.get("Content-Type", "image/jpeg"), "Cache-Control": "public, max-age=3600"}
        return Response(stream_with_context(fr.iter_content(chunk_size=4096)), headers=headers, status=200)
    except Exception as e:
        print(f"[userpic] error: {e}")
        return Response(status=204)

@app.get("/api/leaderboard")
def api_leaderboard():
    period = (request.args.get("period") or "week").lower()

    def _parse_iso(s):
        try:
            return datetime.fromisoformat(str(s).replace(" ", "T"))
        except Exception:
            return None

    def _label_ru(start_iso, end_iso, prefix):
        ds = _parse_iso(start_iso)
        de = _parse_iso(end_iso)
        if ds and de:
            return f"{prefix}: {ds.strftime('%d.%m')} – {de.strftime('%d.%m')}"
        return prefix

    if period == "month":
        start, end = db.month_current_bounds()
        label = _label_ru(start, end, "За месяц")
    else:
        start, end = db.week_current_bounds()
        label = _label_ru(start, end, "За неделю")

    # Возвращаем формат как у тебя (success + week + items)
    items = []
    try:
        if hasattr(db, "leaderboard"):
            items_raw = db.leaderboard(start, end, limit=50)
            # совместим с фронтом: login, earned
            for it in items_raw:
                items.append({"login": it.get("login"), "earned": max(float(it.get("payouts",0) or 0), 0.0)})
    except Exception:
        pass

    return jsonify(success=True, week={"start": start, "end": end, "label": label}, items=items)

# ---------- Market history (для графиков) ----------
@app.get("/api/market/history")
def api_market_history():
    event_uuid = request.args.get("event_uuid", type=str)
    option_index = request.args.get("option_index", type=int)
    rng = (request.args.get("range") or "1d").lower()
    if not event_uuid or option_index is None:
        return jsonify(success=False, error="bad_params"), 400
    try:
        m = (
            db.client.table("prediction_markets")
            .select("id, constant_product, created_at")
            .eq("event_uuid", event_uuid)
            .eq("option_index", option_index)
            .single()
            .execute()
        ).data
        if not m:
            return jsonify(success=False, error="market_not_found"), 404

        market_id = int(m["id"])
        k = float(m.get("constant_product") or 1_000_000.0)
        y = (k ** 0.5)
        n = (k ** 0.5)
        now = datetime.now(timezone.utc)
        dt_map = {
            "1h": now - timedelta(hours=1),
            "6h": now - timedelta(hours=6),
            "1d": now - timedelta(days=1),
            "1w": now - timedelta(weeks=1),
            "1m": now - timedelta(days=30),
            "all": None,
        }
        since = dt_map.get(rng, now - timedelta(days=1))
        q = (
            db.client.table("market_orders")
            .select("order_type, amount, created_at")
            .eq("market_id", market_id)
            .order("created_at", desc=False)
        )
        if since:
            q = q.gte("created_at", since.isoformat())
        orders = q.execute().data or []

        points = []
        if since:
            points.append({"ts": since.isoformat(), "yes_price": n/(y+n) if (y+n)>0 else 0.5})
        else:
            points.append({"ts": (m.get("created_at") or datetime.now(timezone.utc).isoformat()), "yes_price": n/(y+n) if (y+n)>0 else 0.5})

        for o in orders:
            side = o["order_type"]
            amt = float(o["amount"])
            if side in ("yes","buy_yes"):
                n = n + amt
                y = k / n
            else:
                y = y + amt
                n = k / y
            price_yes = n/(y+n) if (y+n)>0 else 0.5
            points.append({"ts": o["created_at"], "yes_price": price_yes})

        return jsonify(success=True, points=points)
    except Exception as e:
        print("[api_market_history] error:", e)
        return jsonify(success=False, error="server_error"), 500

# ---------- Admin: Пользователи (новая страница) ----------
ADMIN_USERS_HTML = """
Admin · Пользователи  <a href="/admin">← Админ</a>

<h1>Пользователи</h1>

<form method="get" action="/admin/users" style="margin:8px 0">
  <input type="hidden" name="status" value="{{status}}"/>
  <input type="text" name="q" value="{{q or ''}}" placeholder="chat_id или логин"/>
  <select name="sort">
    <option value="">По умолчанию</option>
    <option value="created_at" {% if sort=='created_at' %}selected{% endif %}>По дате регистрации</option>
    <option value="balance" {% if sort=='balance' %}selected{% endif %}>По балансу</option>
  </select>
  <button type="submit">Искать</button>
</form>

<div style="margin:8px 0">
  Статус:
  {% for s in ['pending','approved','banned'] %}
    {% if s==status %}<b>{{s}}</b>{% else %}<a href="/admin/users?status={{s}}">{{s}}</a>{% endif %}
    &nbsp;
  {% endfor %}
</div>

<table border="1" cellspacing="0" cellpadding="6">
  <tr>
    <th>chat_id</th>
    <th>логин</th>
    <th>username</th>
    <th>статус</th>
    <th>баланс</th>
    <th>действия</th>
  </tr>
  {% for u in users %}
    <tr>
      <td>{{u.chat_id}}</td>
      <td>{{u.login}}</td>
      <td>{{u.username or '—'}}</td>
      <td>{{u.status}}</td>
      <td>{{'%.2f'|format(u.balance or 0)}}</td>
      <td>
        <form method="post" action="/admin/users/action" style="display:inline">
          <input type="hidden" name="chat_id" value="{{u.chat_id}}"/>
          {% if status=='pending' %}
            <button name="action" value="approve">Одобрить</button>
            <button name="action" value="reject">Отклонить</button>
          {% elif status=='approved' %}
            <button name="action" value="ban">Забанить</button>
          {% elif status=='banned' %}
            <button name="action" value="unban">Разбанить</button>
          {% endif %}
        </form>
        &nbsp;
        <details>
          <summary>Подробнее / баланс</summary>
          <div style="margin:6px 0">
            <form method="post" action="/admin/users/balance">
              <input type="hidden" name="chat_id" value="{{u.chat_id}}"/>
              <input type="number" name="balance" step="0.01" placeholder="Новый баланс" />
              <button type="submit">Сохранить</button>
            </form>
          </div>
          <div>
            <b>Недавние операции</b>
            <ul>
              {% for l in (ledger_map.get(u.chat_id) or []) %}
                <li>{{l.created_at}}: {{l.reason}} ({{l.delta}})</li>
              {% endfor %}
              {% if not ledger_map.get(u.chat_id) %}<li>Нет записей</li>{% endif %}
            </ul>
          </div>
        </details>
      </td>
    </tr>
  {% endfor %}
  {% if not users %}
    <tr><td colspan="6">Нет пользователей</td></tr>
  {% endif %}
</table>
"""

@app.get("/admin/users")
@requires_auth
def admin_users():
    status = (request.args.get("status") or "pending").lower()
    if status not in ("pending","approved","banned"):
        status = "pending"
    q = (request.args.get("q") or "").strip()
    sort = (request.args.get("sort") or "").strip()

    users = db.search_users(status=status, q=q, sort=sort)
    ledger_map = {}
    for u in users:
        try:
            ledger_map[u["chat_id"]] = db.get_ledger_for_user(u["chat_id"], limit=10)
        except Exception:
            ledger_map[u["chat_id"]] = []

    return render_template_string(ADMIN_USERS_HTML, status=status, q=q, sort=sort, users=users, ledger_map=ledger_map)

@app.post("/admin/users/action")
@requires_auth
def admin_users_action():
    chat_id = request.form.get("chat_id", type=int)
    action = (request.form.get("action") or "").lower()
    if not chat_id or action not in ("approve","reject","ban","unban"):
        return redirect(url_for("admin_users"))
    try:
        if action == "approve":
            db.approve_user(chat_id)
        elif action == "reject":
            db.reject_user(chat_id)
        elif action == "ban":
            db.ban_user(chat_id)
        elif action == "unban":
            db.unban_user(chat_id)
    except Exception as e:
        print("[/admin/users/action] error:", e)
    return redirect(url_for("admin_users", status=request.args.get("status","pending")))

@app.post("/admin/users/balance")
@requires_auth
def admin_users_balance():
    chat_id = request.form.get("chat_id", type=int)
    new_balance = request.form.get("balance", type=float)
    if not chat_id or new_balance is None:
        return redirect(url_for("admin_users", status=request.args.get("status","approved")))
    try:
        db.admin_set_balance_via_ledger(chat_id, new_balance)
    except Exception as e:
        print("[/admin/users/balance] error:", e)
    return redirect(url_for("admin_users", status=request.args.get("status","approved")))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
