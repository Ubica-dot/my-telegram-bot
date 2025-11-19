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
    Flask,
    request,
    render_template_string,
    jsonify,
    Response,
    stream_with_context,
)

from database import db  # db.client = Supabase client

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
    # 1) Telegram initData
    init_str = request.args.get("init")
    payload = None
    if request.method in ("POST", "PUT", "PATCH"):
        payload = request.get_json(silent=True) or {}
    if not init_str:
        init_str = (payload or {}).get("init")
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

    # 2) chat_id + sig
    chat_id = request.args.get("chat_id", type=int)
    sig = request.args.get("sig", "")
    if payload:
        chat_id = chat_id if chat_id is not None else (int(payload.get("chat_id")) if payload.get("chat_id") else None)
        sig = sig or (payload.get("sig") or "")
    if not chat_id or not verify_sig(chat_id, sig):
        return None, "bad_sig"
    return chat_id, None


# ---------- Health / Legal ----------
@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/")
def index():
    return "OK"


LEGAL_HTML = """
<h1>Правила и политика конфиденциальности</h1>
<h2>Условия использования</h2>
<p>Это учебная платформа предсказательных рынков. Нет реальных денег. Котировки волатильны — вы можете потерять игровые кредиты.</p>
<h2>Политика конфиденциальности</h2>
<p>Храним минимальные данные Telegram (chat_id, логин), историю сделок и баланс. Данные не продаются и не передаются третьим лицам.</p>
<p>Вопросы — админу бота.</p>
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
                send_message(chat_id, "🚫 Доступ к приложению запрещён.")
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
                send_message(chat_id, "🚫 Доступ запрещён.")
        return "ok"

    return "ok"


# ---------- Mini App HTML ----------
MINI_APP_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>U — мини‑приложение</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 16px; background: #0b0d12; color: #e5e7eb; }
    a { color: #93c5fd; text-decoration: none; }
    .muted { color: #9aa3b2; }
    .row { display: flex; align-items: center; gap: 8px; }
    .section { margin-bottom: 20px; background: #11141b; border-radius: 12px; padding: 12px 12px 2px; border: 1px solid #1b2230; }
    .section h2 { margin: 0 0 8px; font-size: 16px; font-weight: 600; display:flex; align-items:center; justify-content:space-between; }
    .pill { display: inline-block; padding: 2px 8px; background:#192031; border:1px solid #263049; color:#cbd5e1; border-radius:999px; font-size:12px; }
    .event-card { background:#0f1320; border:1px solid #1b2232; border-radius:12px; padding:12px; margin-bottom:10px; transition: transform 120ms ease, box-shadow 120ms ease; }
    .event-card:active { transform: scale(0.98); box-shadow: 0 2px 10px rgba(0,0,0,.15); }
    .event-title { font-weight:600; margin:0 0 4px; }
    .event-description { color:#9aa3b2; font-size: 14px; margin-bottom:8px; }
    .event-meta { display:flex; gap:12px; font-size:12px; color:#9aa3b2; margin-bottom:8px; }
    .options { display:flex; flex-wrap:wrap; gap:8px; }
    .option { flex:1 1 120px; background:#121a2a; border:1px solid #1e2a44; border-radius:10px; padding:8px; }
    .price { font-weight:600; color:#e6edf7; }
    .event-chart { opacity: 0; transform: translateY(-4px); transition: opacity .25s ease, transform .25s ease; margin: 6px -4px 0; padding: 6px 4px 0; }
    .event-chart.visible { opacity: 1; transform: translateY(0); }
    .icon-btn { background: none; border: 0; font-size: 18px; cursor: pointer; color: #9aa3b2; }
    .icon-btn[aria-pressed="true"] { color: #ff6b6b; }
    .footer { text-align:center; margin-top: 24px; font-size: 12px; }
    .grid { display:grid; grid-template-columns: 1fr; gap:12px; }
    @media (min-width: 900px){ .grid { grid-template-columns: 1fr 1fr; } }
  </style>
</head>
<body>
  <div class="grid">
    <section class="section">
      <h2>Активные ставки <span><button id="onlyBookmarksBtn1" class="icon-btn" aria-pressed="false" title="Показать только закладки">🔖</button></span></h2>
      <div id="positions">Загрузка...</div>
    </section>

    <section class="section">
      <h2>Мероприятия <span><button id="onlyBookmarksBtn2" class="icon-btn" aria-pressed="false" title="Показать только закладки">🔖</button></span></h2>
      <div id="events"></div>
    </section>

    <section class="section">
      <h2>Прошедшие ставки (архив) <span><button id="onlyBookmarksBtn3" class="icon-btn" aria-pressed="false" title="Показать только закладки">🔖</button></span></h2>
      <div id="archive">Загрузка...</div>
    </section>

    <section class="section">
      <h2>Таблица лидеров <span class="muted" style="font-weight:400">
        <button class="pill" id="lbWeek">Неделя</button>
        <button class="pill" id="lbMonth">Месяц</button></span></h2>
      <div id="leaderboard">Загрузка…</div>
    </section>
  </div>

  <div class="footer">
    <a href="/legal" target="_blank">Правила и политика конфиденциальности</a>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
  <script>
    // Parse URL params
    const url = new URL(location.href);
    const chatId = Number(url.searchParams.get('chat_id'));
    const sig = url.searchParams.get('sig');

    // Simple helpers
    const fmtPct = (x) => (x * 100).toFixed(1) + '%';

    // Bookmarks
    const BM_SET_KEY = 'bookmarks';
    const ONLY_BM_KEY = 'onlyBookmarks';
    const bookmarks = new Set(JSON.parse(localStorage.getItem(BM_SET_KEY) || '[]'));
    function isBookmarked(event_uuid) { return bookmarks.has(event_uuid); }
    function toggleBookmark(event_uuid) {
      if (bookmarks.has(event_uuid)) bookmarks.delete(event_uuid);
      else bookmarks.add(event_uuid);
      localStorage.setItem(BM_SET_KEY, JSON.stringify([...bookmarks]));
      refreshLists();
    }
    function applyBookmarksFilter(items) {
      const active = localStorage.getItem(ONLY_BM_KEY) === 'true';
      if (!active) return items;
      return (items || []).filter(ev => isBookmarked(ev.event_uuid));
    }
    function initOnlyBookmarksButton(btnId){
      const btn = document.getElementById(btnId);
      if (!btn) return;
      const initState = localStorage.getItem(ONLY_BM_KEY) === 'true';
      btn.setAttribute('aria-pressed', initState ? 'true' : 'false');
      btn.addEventListener('click', () => {
        const active = btn.getAttribute('aria-pressed') === 'true';
        btn.setAttribute('aria-pressed', (!active).toString());
        localStorage.setItem(ONLY_BM_KEY, (!active).toString());
        refreshLists();
      });
    }
    initOnlyBookmarksButton('onlyBookmarksBtn1');
    initOnlyBookmarksButton('onlyBookmarksBtn2');
    initOnlyBookmarksButton('onlyBookmarksBtn3');

    // Event chart rendering
    function renderEventChart(container, payload) {
      const canvas = container.querySelector('canvas') || (function(){ container.innerHTML='<canvas height="140"></canvas>'; return container.querySelector('canvas'); })();
      const ctx = canvas.getContext('2d');
      const datasets = (payload.series || []).map((s, i) => ({
        label: s.label,
        data: s.data,
        borderColor: s.color,
        backgroundColor: s.color + '33',
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.25,
        fill: false
      }));
      new Chart(ctx, {
        type: 'line',
        data: { datasets },
        options: {
          parsing: false,
          animation: { duration: 500, easing: 'easeOutQuart' },
          interaction: { mode: 'nearest', intersect: false },
          scales: {
            x: { type: 'time', time: { tooltipFormat: 'dd.MM HH:mm' } },
            y: { min: 0, max: 1, ticks: { callback: v => Math.round(v * 100) + '%' } }
          },
          plugins: {
            legend: { position: 'bottom' },
            tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${(ctx.parsed.y * 100).toFixed(1)}%` } }
          }
        }
      });
    }

    async function toggleEventChart(cardEl, event_uuid) {
      cardEl.classList.add('pressed');
      setTimeout(() => cardEl.classList.remove('pressed'), 120);

      let chartEl = cardEl.querySelector('.event-chart');
      if (!chartEl) {
        chartEl = document.createElement('div');
        chartEl.className = 'event-chart';
        chartEl.innerHTML = '<canvas height="140"></canvas>';
        const desc = cardEl.querySelector('.event-description') || cardEl.firstElementChild;
        (desc ? desc : cardEl).insertAdjacentElement('afterend', chartEl);
      }
      if (!chartEl.dataset.loaded) {
        chartEl.dataset.loaded = '1';
        const params = new URLSearchParams({ event_uuid, chat_id: chatId, sig });
        const resp = await fetch('/api/event_prices?' + params.toString());
        const payload = await resp.json();
        renderEventChart(chartEl, payload);
        requestAnimationFrame(() => chartEl.classList.add('visible'));
      } else {
        chartEl.classList.toggle('visible');
      }
    }

    function eventCardHTML(ev){
      const evu = ev.event_uuid;
      const title = ev.name || '—';
      const desc = ev.description || '';
      const endShort = ev.end_short || '';
      const bookmarked = isBookmarked(evu) ? 'true' : 'false';
      const mk = ev.markets || {};
      const options = (ev.options || []).map((o, idx) => {
        const txt = (typeof o === 'object' && o && 'text' in o) ? o.text : String(o);
        const m = mk[idx] || {};
        const yp = (m.yes_price !== undefined) ? m.yes_price : 0.5;
        return `
          <div class="option">
            <div class="muted" style="font-size:12px">${txt}</div>
            <div class="price">${(yp*100).toFixed(1)}%</div>
          </div>`;
      }).join('');
      return `
        <div class="event-card" data-evu="${evu}" onclick="toggleEventChart(this, '${evu}')">
          <div class="row" style="justify-content:space-between; margin-bottom:6px">
            <div class="event-title">${title}</div>
            <button class="icon-btn" aria-pressed="${bookmarked}" title="Закладка" onclick="event.stopPropagation(); toggleBookmark('${evu}')">🔖</button>
          </div>
          <div class="event-description">${desc}</div>
          <div class="event-meta"><span class="muted">Дедлайн: ${endShort}</span></div>
          <div class="options">${options}</div>
          <!-- chart will appear here -->
        </div>`;
    }

    async function refreshLists(){
      // /api/me: позиции, архив
      const meParams = new URLSearchParams({ chat_id: chatId, sig });
      const meResp = await fetch('/api/me?' + meParams.toString());
      const me = await meResp.json();

      if (me.success){
        // positions - простым текстом
        const posEl = document.getElementById('positions');
        const pos = me.positions || [];
        if (!pos.length){ posEl.innerHTML = '<span class="muted">Нет активных позиций</span>'; }
        else {
          posEl.innerHTML = pos.map(p => `
            <div class="row" style="justify-content:space-between; border-bottom:1px dashed #1d2433; padding:6px 2px">
              <div>${p.event_name} · <span class="muted">${p.option_text}</span></div>
              <div class="muted">${p.share_type.toUpperCase()} · Qty ${p.quantity}</div>
            </div>`).join('');
        }

        // archive
        const arEl = document.getElementById('archive');
        const ar = me.archive || [];
        if (!ar.length){ arEl.innerHTML = '<span class="muted">Пока пусто</span>'; }
        else {
          arEl.innerHTML = ar.slice(0, 20).map(r => `
            <div class="row" style="justify-content:space-between; border-bottom:1px dashed #1d2433; padding:6px 2px">
              <div>${r.event_name} · <span class="muted">${r.option_text}</span></div>
              <div class="muted">${r.is_win ? '✅' : '❌'} ${r.payout.toFixed(2)}</div>
            </div>`).join('');
        }
      }

      // events server-rendered via Jinja context "events" (passed by Flask)
      // Но применим фильтр «только закладки»
      const container = document.getElementById('events');
      const serverEvents = {{ events|tojson }};
      const filtered = applyBookmarksFilter(serverEvents);
      if (!filtered.length){
        container.innerHTML = '<span class="muted">Ничего не найдено</span>';
      } else {
        container.innerHTML = filtered.map(eventCardHTML).join('');
      }
    }

    // Leaderboard
    async function loadLeaderboard(period='week'){
      const params = new URLSearchParams({ period, chat_id: chatId, sig });
      const resp = await fetch('/api/leaderboard?' + params.toString());
      const data = await resp.json();
      const el = document.getElementById('leaderboard');
      const rows = (data.rows || data.items || []);
      if (!rows.length){ el.innerHTML = '<span class="muted">Нет данных</span>'; return; }
      el.innerHTML = rows.slice(0, 20).map((r, i) => `
        <div class="row" style="justify-content:space-between; border-bottom:1px dashed #1d2433; padding:6px 2px">
          <div>#${i+1} ${r.login || r.chat_id}</div>
          <div class="muted">PnL: ${(r.pnl ?? 0).toFixed(2)}</div>
        </div>`).join('');
    }
    document.getElementById('lbWeek').addEventListener('click', () => loadLeaderboard('week'));
    document.getElementById('lbMonth').addEventListener('click', () => loadLeaderboard('month'));

    // Init
    refreshLists();
    loadLeaderboard('week');
  </script>
</body>
</html>
"""


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


# ---------- Rate limiting ----------
RL_USER_WINDOW = 10
RL_USER_LIMIT = 5
RL_IP_WINDOW = 60
RL_IP_LIMIT = 30

_rl_user = defaultdict(deque)
_rl_ip = defaultdict(deque)


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


# ---------- Mini‑app + API ----------
@app.get("/mini-app")
def mini_app():
    chat_id = request.args.get("chat_id", type=int)
    sig = request.args.get("sig", "")
    if not chat_id or not sig or not verify_sig(chat_id, sig):
        return Response("<h3>Доступ запрещён</h3><p>Откройте Mini App из бота после /start и одобрения.</p>", mimetype="text/html")

    user = db.get_user(chat_id)
    if not user or user.get("status") != "approved":
        return Response("<h3>Доступ только для одобренных пользователей</h3><p>Дождитесь одобрения администратора.</p>", mimetype="text/html")
    if user.get("status") == "banned":
        return Response("<h3>Доступ запрещён</h3>", mimetype="text/html")

    # Данные для шаблона (events)
    events = db.get_published_events()
    for e in events:
        end_iso = str(e.get("end_date", ""))
        e["end_short"] = _format_end_short(end_iso)
        # рынки + текущая цена
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
            markets[int(m["option_index"])] = {
                "yes_price": yp,
                "volume": volume,
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

    # Получить market_id
    market_id = db.get_market_id(event_uuid, option_index)
    if not market_id:
        return jsonify(success=False, error="market_not_found"), 404

    # Вызов RPC через database (fallback на прямой rpc, если метод отсутствует)
    try:
        result = db.trade_buy(chat_id=chat_id, market_id=market_id, side=side, amount=amount)
        err2 = None
    except AttributeError:
        # Прямой вызов RPC
        try:
            rr = (
                db.client.rpc(
                    "rpc_trade_buy",
                    {"p_chat_id": chat_id, "p_market_id": market_id, "p_side": side, "p_amount": amount},
                )
                .execute()
                .data
                or []
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
            err2 = None
        except Exception as e:
            print("[api_market_buy] rpc error:", e)
            result, err2 = None, "rpc_error"

    if err2:
        return jsonify(success=False, error=err2), 400

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


# ---------- Leaderboard (fixed) ----------
@app.get("/api/leaderboard")
def api_leaderboard():
    # Не меняем UX: допускаем вызов без initData, по chat_id+sig
    # Авторизацию мягко проверяем
    chat_id, err = auth_chat_id_from_request()
    if err:
        # Разрешим публичный просмотр (например, из мини‑приложения до фикса подписи),
        # но отметим в лог:
        print("[leaderboard] auth warning:", err)

    period = (request.args.get("period") or "week").lower()
    if period == "month":
        start, end = db.month_current_bounds()
    else:
        start, end = db.week_current_bounds()

    rows = db.leaderboard(start, end, limit=50)
    return jsonify({"period": period, "start": start, "end": end, "rows": rows})


# ---------- Market history (per market, keeps old endpoint) ----------
@app.get("/api/market/history")
def api_market_history():
    """
    История цены ДА для конкретного рынка (event_uuid + option_index),
    расчёт по market_orders с проигрыванием AMM (x*y=k) от стартовых резервов 1000/1000.
    Параметры: event_uuid, option_index, range: 1h|6h|1d|1w|1m|all
    """
    event_uuid = request.args.get("event_uuid", type=str)
    option_index = request.args.get("option_index", type=int)
    rng = (request.args.get("range") or "1d").lower()
    if not event_uuid or option_index is None:
        return jsonify(success=False, error="bad_params"), 400

    try:
        # 1) найти market_id и константу
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
        # стартовые резервы
        y = (k ** 0.5)
        n = (k ** 0.5)

        # 2) временной диапазон
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

        # 3) забрать приказы
        q = (
            db.client.table("market_orders")
            .select("order_type, amount, created_at")
            .eq("market_id", market_id)
            .order("created_at", desc=False)
        )
        if since:
            q = q.gte("created_at", since.isoformat())
        orders = q.execute().data or []

        # 4) проиграть историю
        points = []
        # стартовая точка
        if since:
            points.append({"ts": since.isoformat(), "yes_price": n / (y + n) if (y + n) > 0 else 0.5})
        else:
            points.append(
                {
                    "ts": (m.get("created_at") or datetime.now(timezone.utc).isoformat()),
                    "yes_price": n / (y + n) if (y + n) > 0 else 0.5,
                }
            )

        for o in orders:
            side = o["order_type"]
            amt = float(o["amount"])
            if side == "yes":
                n = n + amt
                y = k / n
            else:
                y = y + amt
                n = k / y
            price_yes = n / (y + n) if (y + n) > 0 else 0.5
            points.append({"ts": o["created_at"], "yes_price": price_yes})

        return jsonify(success=True, points=points)
    except Exception as e:
        print("[api_market_history] error:", e)
        return jsonify(success=False, error="server_error"), 500


# ---------- Event-level price history (one chart, many lines) ----------
@app.get("/api/event_prices")
def api_event_prices():
    chat_id, err = auth_chat_id_from_request()
    if err:
        # Разрешим без подписи, но лучше настроить chat_id+sig
        print("[event_prices] auth warning:", err)

    event_uuid = (request.args.get("event_uuid") or "").strip()
    if not event_uuid:
        return jsonify({"error": "no_event_uuid"}), 400

    # подписи опций
    ev = (
        db.client.table("events")
        .select("options")
        .eq("event_uuid", event_uuid)
        .single()
        .execute()
    ).data or {}

    opt_texts = []
    opts = ev.get("options") or []
    for o in opts:
        if isinstance(o, dict):
            opt_texts.append(o.get("text") or "—")
        else:
            opt_texts.append(str(o))

    # RPC по истории (см. созданную функцию rpc_event_price_history)
    try:
        rows = (
            db.client.rpc("rpc_event_price_history", {"p_event_uuid": event_uuid})
            .execute()
            .data
            or []
        )
    except Exception as e:
        print("[api_event_prices] rpc error:", e)
        rows = []

    # группируем по option_index
    by_idx = {}
    for r in rows:
        idx = int(r["option_index"])
        by_idx.setdefault(idx, []).append({"x": r["created_at"], "y": float(r["yes_price"] or 0)})

    palette = ["#ff6b6b", "#4dabf7", "#51cf66", "#ffd43b", "#845ef7", "#20c997", "#91a7ff", "#fcc419"]
    series = []
    for idx, data in by_idx.items():
        label = opt_texts[idx] if 0 <= idx < len(opt_texts) else f"Вариант {idx+1}"
        series.append({"option_index": idx, "label": label, "color": palette[idx % len(palette)], "data": data})

    return jsonify({"event_uuid": event_uuid, "series": series})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
