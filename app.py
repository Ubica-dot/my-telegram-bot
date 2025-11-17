import os
import json
import uuid
import hmac
import hashlib
import time
from datetime import datetime

import requests
from flask import Flask, request, render_template_string, jsonify, Response, stream_with_context
from functools import wraps

from database import db

app = Flask(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
BASE_URL = os.getenv("WEBHOOK_URL")
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "change-me")

ADMIN_BASIC_USER = os.getenv("ADMIN_BASIC_USER", "admin")
ADMIN_BASIC_PASS = os.getenv("ADMIN_BASIC_PASS", "admin")

WEBAPP_SIGNING_SECRET = os.getenv("WEBAPP_SIGNING_SECRET")  # подпись chat_id для mini-app


# ------------- Admin auth -------------
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


# ------------- Utils -------------
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
        return True
    return hmac.compare_digest(make_sig(chat_id), sig or "")


# ------------- Health -------------
@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/")
def index():
    return "OK"


# ------------- Telegram webhook -------------
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
    text = message.get("text", "") or ""
    username = message.get("from", {}).get("username", "нет")

    user = db.get_user(chat_id)
    is_approved = bool(user and user.get("status") == "approved")

    if text == "/start":
        if is_approved:
            balance = user.get("balance", 1000)
            send_message(chat_id, f"✅ Вы уже зарегистрированы!\n\nВаш баланс: {balance} кредитов")
        else:
            if user:
                send_message(chat_id, "⏳ Ваша заявка уже отправлена и ожидает рассмотрения.")
            else:
                send_message(
                    chat_id,
                    "Добро пожаловать! Отправьте свой логин одним сообщением для регистрации. После модерации придёт уведомление.",
                )

    elif text == "/events":
        if not is_approved:
            send_message(chat_id, "❌ Нет доступа. Завершите регистрацию через /start")
        else:
            events = db.get_published_events()
            if not events:
                send_message(chat_id, "На данный момент нет активных мероприятий.")
            else:
                lines = ["Активные мероприятия:"]
                for e in events:
                    lines.append(
                        f"• {e['name']}\n{e['description'][:80]}...\n⏰ До: {e['end_date'][:16]}\nУчастников: {e.get('participants', 0)}\n"
                    )
                send_message(chat_id, "\n\n".join(lines))

    elif text == "/balance":
        if not is_approved:
            send_message(chat_id, "❌ Нет доступа. Завершите регистрацию через /start")
        else:
            balance = user.get("balance", 1000)
            send_message(chat_id, f"Ваш текущий баланс: {balance} кредитов")

    elif text == "/app":
        sig = make_sig(chat_id)
        ts = int(time.time())
        web_app_url = f"https://{request.host}/mini-app?chat_id={chat_id}&v={ts}"
        if sig:
            web_app_url += f"&sig={sig}"
        kb = {"inline_keyboard": [[{"text": "Открыть Mini App", "web_app": {"url": web_app_url}}]]}
        send_message(chat_id, "Откройте мини‑приложение для просмотра и покупки:", kb)

    elif text.startswith("/"):
        if not is_approved:
            send_message(chat_id, "❌ Нет доступа. Сначала завершите регистрацию через /start")
        else:
            send_message(chat_id, "Команда не распознана.")

    else:
        if is_approved:
            send_message(chat_id, "✅ Вы уже зарегистрированы! Используйте доступные команды.")
        else:
            if user:
                send_message(chat_id, "⏳ Ваша заявка уже на рассмотрении. Ожидайте ответа администратора.")
            else:
                new_user = db.create_user(chat_id, text, username)
                if new_user:
                    send_message(chat_id, f"✅ Логин '{text}' отправлен на модерацию. Ожидайте подтверждения.")
                    notify_admin(f"Новая заявка:\nЛогин: {text}\nID: {chat_id}\nUsername: @{username}\nАдмин‑панель: {BASE_URL}/admin")
                else:
                    send_message(chat_id, "❌ Ошибка при отправке заявки. Попробуйте ещё раз.")

    return "ok"


# ------------- Mini App (аватар/инициалы, баланс, вероятности, ставки, лидеры) -------------
MINI_APP_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Мероприятия</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 12px; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 12px; margin-bottom: 14px; }
    .opt  { padding: 10px; border: 1px dashed #ccc; border-radius: 10px; margin: 6px 0;
            display: grid; grid-template-columns: 1fr auto auto; gap: 10px; align-items: stretch; }
    .opt-title { display:flex; align-items:center; font-weight: 700; }
    .btn  { padding: 8px 12px; border: 0; border-radius: 10px; cursor: pointer; font-size: 14px; }
    /* Палитра кнопок: мягкие фоны (не через alpha), текст — насыщенный цвет */
    .yes  { background: #CDEAD2; color: #2e7d32; border: 2px solid #2e7d32; }
    .no   { background: #F3C7C7; color: #c62828; border: 2px solid #c62828; }
    .yes:hover { background: #BFE1C6; }
    .no:hover  { background: #ECB3B3; }
    .actions { display:flex; gap: 8px; align-items: stretch; height: 100%; }
    .actions .btn { height: 100%; display:flex; align-items:center; justify-content:center; }
    .muted { color: #666; font-size: 14px; }
    .section { margin: 20px 0; }
    .section-head { display:flex; align-items:center; justify-content:space-between; padding:10px 12px; background:#f5f5f5; border-radius:10px; cursor:pointer; user-select:none; }
    .section-title { font-weight:600; }
    .caret { transition: transform .15s ease; }
    .collapsed .caret { transform: rotate(-90deg); }
    .section-body { padding:10px 0 0 0; }
    .prob { color:#000; font-weight:800; font-size:18px; display:flex; align-items:center; justify-content:flex-end; padding: 0 4px; }
    /* top bar */
    .topbar { display:flex; justify-content:center; align-items:center; flex-direction:column; }
    .avatar-wrap { position: relative; width: 86px; height: 86px; }
    .avatar { width: 86px; height: 86px; border-radius: 50%; border: 2px solid #eee; box-shadow: 0 2px 8px rgba(0,0,0,.06); }
    .avatar.img { position:absolute; inset:0; object-fit: cover; display:none; }
    .avatar.ph  { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-weight:800; font-size:28px; color:#fff;
                  background: radial-gradient(circle at 30% 30%, #6a5acd, #00bcd4); letter-spacing: 1px; text-transform: uppercase; }
    .balance { text-align:center; font-weight:900; font-size:22px; margin: 10px 0 18px; }
    /* modal */
    .modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,.5); display:none; align-items:center; justify-content:center; }
    .modal { background:#fff; border-radius:12px; padding:16px; width:90%; max-width:400px; }
    .modal h3 { margin:0 0 8px 0; }
    .modal .row { justify-content: flex-start; }
    input[type=number] { padding:8px; width: 140px; }
    /* active bets (casino-like payout on right) */
    .bet { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    .bet-info { flex: 1 1 auto; }
    .bet-win { flex: 0 0 auto; text-align:right; }
    .win-val { font-size:22px; font-weight:900; color:#0b8457; line-height:1.1; }
    .win-sub { font-size:12px; color:#666; }
    .unit { font-size:12px; font-weight:700; color:#444; margin-left:2px; }
    /* leaderboard */
    .lb-head { text-align:center; font-weight:700; margin:6px 0 10px; }
    .lb-table { width:100%; border-collapse: collapse; }
    .lb-row { display:grid; grid-template-columns: 32px 40px 1fr auto; align-items:center; padding:8px 6px; border-bottom:1px solid #eee; gap:8px; }
    .lb-rank { text-align:center; font-weight:800; color:#444; }
    .lb-ava { width:32px; height:32px; border-radius:50%; object-fit:cover; }
    .lb-login { font-weight:600; color:#111; }
    .lb-val { font-weight:900; font-size:16px; color:#0b8457; }
  </style>
</head>
<body>

  <!-- Аватар/инициалы и баланс -->
  <div class="topbar">
    <div class="avatar-wrap">
      <div id="avatarPH" class="avatar ph">U</div>
      <img id="avatar" class="avatar img" alt="avatar">
    </div>
  </div>
  <div id="balance" class="balance">Баланс: —</div>

  <!-- Активные ставки -->
  <div id="wrap-active" class="section" style="margin-top:22px;">
    <div id="head-active" class="section-head" onclick="toggleSection('active')">
      <span class="section-title">Активные ставки</span>
      <span id="caret-active" class="caret">▾</span>
    </div>
    <div id="section-active" class="section-body">
      <div id="active" class="muted">Загрузка...</div>
    </div>
  </div>

  <!-- Мероприятия -->
  <div id="wrap-events" class="section">
    <div id="head-events" class="section-head" onclick="toggleSection('events')">
      <span class="section-title">Мероприятия</span>
      <span id="caret-events" class="caret">▾</span>
    </div>
    <div id="section-events" class="section-body">
      <div id="events-list">
        {% for e in events %}
          <div class="card" data-event="{{ e.event_uuid }}">
            <div><b>{{ e.name }}</b></div>
            <div><small>До: {{ e.end_date[:16] }}</small></div>
            <p>{{ e.description }}</p>

            {% for idx, opt in enumerate(e.options) %}
              {% set md = e.markets.get(idx, {'yes_price': 0.5}) %}
              <div class="opt" data-option="{{ idx }}">
                <div class="opt-title">{{ opt.text }}</div>
                <div class="prob" title="Вероятность ДА">{{ ('%.0f' % (md.yes_price * 100)) }}%</div>
                <div class="actions">
                  <button class="btn yes buy-btn"
                          data-event="{{ e.event_uuid }}"
                          data-index="{{ idx }}"
                          data-side="yes"
                          data-text="{{ opt.text|e }}">ДА</button>

                  <button class="btn no buy-btn"
                          data-event="{{ e.event_uuid }}"
                          data-index="{{ idx }}"
                          data-side="no"
                          data-text="{{ opt.text|e }}">НЕТ</button>
                </div>
              </div>
            {% endfor %}
          </div>
        {% endfor %}
      </div>
    </div>
  </div>

  <!-- Таблица лидеров (свернута по умолчанию) -->
  <div id="wrap-leaders" class="section">
    <div id="head-leaders" class="section-head" onclick="toggleLeaders()">
      <span class="section-title">Таблица лидеров</span>
      <span id="caret-leaders" class="caret">▸</span>
    </div>
    <div id="section-leaders" class="section-body" style="display:none;">
      <div id="lb-range" class="lb-head">—</div>
      <div id="lb-container" class="lb-table">Загрузка…</div>
    </div>
  </div>

  <!-- Modal -->
  <div id="modalBg" class="modal-bg">
    <div class="modal">
      <h3 id="mTitle">Покупка</h3>
      <div class="muted" id="mSub">Укажите сумму, не выше вашего баланса.</div>
      <div class="row" style="margin-top:10px;">
        <label>Сумма (кредиты):&nbsp;</label>
        <input type="number" id="mAmount" min="1" step="1" value="100"/>
      </div>
      <div class="muted" id="mHint" style="margin-top:8px;"></div>
      <div class="row" style="margin-top:12px;">
        <button class="btn yes" onclick="confirmBuy()">Купить</button>
        <button class="btn no"  onclick="closeBuy()">Отмена</button>
      </div>
    </div>
  </div>

  <script>
    const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    if (tg) tg.ready();

    const qs = new URLSearchParams(location.search);
    const CHAT_ID = qs.get("chat_id");
    const SIG = qs.get("sig") || "";

    function getChatId() {
      if (CHAT_ID) return CHAT_ID;
      if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id) return tg.initDataUnsafe.user.id;
      return null;
    }

    function getInitials() {
      try {
        const u = tg && tg.initDataUnsafe ? tg.initDataUnsafe.user : null;
        let s = "";
        if (u) {
          if (u.first_name) s += u.first_name[0];
          if (u.last_name)  s += u.last_name[0];
          if (!s && u.username) s = u.username.slice(0, 2);
        }
        if (!s) s = "U";
        return s.toUpperCase();
      } catch(e) { return "U"; }
    }

    function setAvatar() {
      const ph = document.getElementById('avatarPH');
      const img = document.getElementById('avatar');
      ph.textContent = getInitials();
      const tryImg = (src) => {
        if (!src) return false;
        img.onload = () => { img.style.display = 'block'; ph.style.display = 'none'; };
        img.onerror = () => { img.style.display = 'none'; ph.style.display = 'flex'; };
        img.src = src;
        return true;
      };
      if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.photo_url) {
        if (tryImg(tg.initDataUnsafe.user.photo_url)) return;
      }
      const cid = getChatId();
      if (cid) {
        const url = `/api/userpic?chat_id=${cid}` + (SIG ? `&sig=${SIG}` : "");
        tryImg(url);
      }
    }

    // --- Секции ---
    function toggleSection(name) {
      const body = document.getElementById("section-" + name);
      const caret = document.getElementById("caret-" + name);
      const head = document.getElementById("head-" + name);
      const key = "collapse_" + name;

      const nowShown = body.style.display !== "none";
      if (nowShown) {
        body.style.display = "none";
        caret.textContent = "▸";
        head.classList.add("collapsed");
        try { localStorage.setItem(key, "1"); } catch(e){}
      } else {
        body.style.display = "block";
        caret.textContent = "▾";
        head.classList.remove("collapsed");
        try { localStorage.setItem(key, "0"); } catch(e){}
      }
    }

    function toggleLeaders() {
      const body = document.getElementById("section-leaders");
      const caret = document.getElementById("caret-leaders");
      const head  = document.getElementById("head-leaders");
      const shown = body.style.display !== "none";
      if (shown) {
        body.style.display = "none"; caret.textContent = "▸"; head.classList.add("collapsed");
      } else {
        body.style.display = "block"; caret.textContent = "▾"; head.classList.remove("collapsed");
        if (!toggleLeaders._loaded) { fetchLeaderboard(); toggleLeaders._loaded = true; }
      }
    }

    function applySavedCollapses() {
      ["active","events"].forEach(name => {
        const key = "collapse_" + name;
        let collapsed = "0";
        try { collapsed = localStorage.getItem(key) || "0"; } catch(e){}
        if (collapsed === "1") {
          const body = document.getElementById("section-" + name);
          const caret = document.getElementById("caret-" + name);
          const head = document.getElementById("head-" + name);
          body.style.display = "none";
          caret.textContent = "▸";
          head.classList.add("collapsed");
        }
      });
    }

    // --- Баланс и активные позиции ---
    async function fetchMe() {
      const cid = getChatId();
      const activeDiv = document.getElementById("active");
      if (!cid) {
        document.getElementById("balance").textContent = "Баланс: —";
        activeDiv.textContent = "Не удалось получить chat_id. Откройте Mini App из чата командой /app.";
        return;
      }
      try {
        const url = `/api/me?chat_id=${cid}` + (SIG ? `&sig=${SIG}` : "");
        const r = await fetch(url);
        const data = await r.json();
        if (!r.ok || !data.success) {
          document.getElementById("balance").textContent = "Баланс: —";
          activeDiv.textContent = "Ошибка загрузки профиля.";
          return;
        }
        renderBalance(data);
        renderActive(data);
      } catch (e) {
        document.getElementById("balance").textContent = "Баланс: —";
        activeDiv.textContent = "Сетевая ошибка.";
      }
    }

    function renderBalance(data) {
      const bal = data.user && typeof data.user.balance !== 'undefined' ? data.user.balance : "—";
      document.getElementById("balance").textContent = `Баланс: ${bal} кредитов`;
    }

    function renderActive(data) {
      const div = document.getElementById("active");
      div.innerHTML = "";

      if (!data.positions || data.positions.length === 0) {
        const m = document.createElement("div");
        m.className = "muted";
        m.textContent = "У вас пока нет активных ставок.";
        div.appendChild(m);
        return;
      }

      data.positions.forEach(pos => {
        const qty = +pos.quantity;
        const avg = +pos.average_price;
        const payout = qty;
        const el = document.createElement("div");
        el.className = "card";
        el.innerHTML = `
          <div class="bet">
            <div class="bet-info">
              <div><b>${pos.event_name}</b></div>
              <div class="muted">${pos.option_text}</div>
              <div class="muted">Сторона: ${pos.share_type.toUpperCase()}</div>
              <div>Кол-во: ${qty.toFixed(4)} | Ср. цена: ${avg.toFixed(4)}</div>
              <div class="muted">Тек. цена ДА/НЕТ: ${(+pos.current_yes_price).toFixed(3)} / ${(+pos.current_no_price).toFixed(3)}</div>
            </div>
            <div class="bet-win">
              <div class="win-val">${payout.toFixed(2)} <span class="unit">кред.</span></div>
              <div class="win-sub">возможная выплата</div>
            </div>
          </div>
        `;
        div.appendChild(el);
      });
    }

    // --- Лидеры ---
    async function fetchLeaderboard() {
      const cid = getChatId();
      const lb = document.getElementById("lb-container");
      try {
        const url = "/api/leaderboard" + (cid ? `?viewer=${cid}` : "");
        const r = await fetch(url);
        const data = await r.json();
        if (!r.ok || !data.success) {
          lb.textContent = "Ошибка загрузки рейтинга.";
          return;
        }
        document.getElementById("lb-range").textContent = data.week.label;

        const items = data.items || [];
        if (items.length === 0) {
          lb.textContent = "Пока нет данных за текущую неделю.";
          return;
        }
        lb.innerHTML = "";
        items.forEach((it, i) => {
          const row = document.createElement("div");
          row.className = "lb-row";
          const sig = (SIG ? `&sig=${SIG}` : "");
          const avaUrl = `/api/userpic?chat_id=${it.chat_id}${sig}`;
          const initials = (it.login || "U").slice(0,2).toUpperCase();

          row.innerHTML = `
            <div class="lb-rank">${i+1}</div>
            <div style="position:relative; width:32px; height:32px;">
              <div style="position:absolute; inset:0; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:12px; font-weight:800; color:#fff; background:linear-gradient(135deg,#6a5acd,#00bcd4)" data-ph>${initials}</div>
              <img src="${avaUrl}" class="lb-ava" style="display:none" onload="this.style.display='block'; this.previousElementSibling.style.display='none';" onerror="this.style.display='none'; this.previousElementSibling.style.display='flex';">
            </div>
            <div class="lb-login">${it.login || '—'}</div>
            <div class="lb-val">${(+it.earned).toFixed(2)}</div>
          `;
          lb.appendChild(row);
        });
      } catch(e) {
        lb.textContent = "Сетевая ошибка.";
      }
    }

    // --- Покупка ---
    let buyCtx = null;

    function openBuy(event_uuid, option_index, side, optText) {
      const cid = getChatId();
      if (!cid) { alert("Не удалось получить chat_id. Откройте Mini App из чата командой /app."); return; }
      buyCtx = { event_uuid, option_index, side, chat_id: cid, optText };
      document.getElementById("mTitle").textContent = `Покупка: ${side.toUpperCase()} · ${optText}`;
      document.getElementById("mHint").textContent = "Сумма будет списана с вашего баланса.";
      document.getElementById("mAmount").value = "100";
      document.getElementById("modalBg").style.display = "flex";
      if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("light");
    }

    function closeBuy() { document.getElementById("modalBg").style.display = "none"; buyCtx = null; }

    async function confirmBuy() {
      if (!buyCtx) return;
      const amount = parseFloat(document.getElementById("mAmount").value || "0");
      if (!(amount > 0)) { alert("Введите положительную сумму"); return; }
      try {
        const body = { chat_id:+buyCtx.chat_id, event_uuid:buyCtx.event_uuid, option_index:buyCtx.option_index, side:buyCtx.side, amount };
        if (SIG) body.sig = SIG;

        const r = await fetch("/api/market/buy", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify(body) });
        const data = await r.json();
        if (!r.ok || !data.success) { alert("Ошибка: " + (data.error || r.statusText)); return; }

        // Обновим вероятность
        const card = document.querySelector(`[data-event='${buyCtx.event_uuid}'] [data-option='${buyCtx.option_index}']`);
        if (card) {
          const probEl = card.querySelector(".prob");
          if (probEl) probEl.textContent = `${Math.round(data.market.yes_price * 100)}%`;
        }

        await fetchMe();         // баланс + активные ставки
        if (document.getElementById("section-leaders").style.display !== "none") fetchLeaderboard();

        closeBuy();
        if (tg && tg.showPopup) tg.showPopup({ title: "Успешно", message: `Куплено ${data.trade.got_shares.toFixed(4)} акций (${buyCtx.side.toUpperCase()})` });
        else alert("Успех: куплено " + data.trade.got_shares.toFixed(4) + " акций");
      } catch (e) { console.error(e); alert("Сетевая ошибка"); }
    }

    // Делегирование кликов по кнопкам покупки
    document.addEventListener('click', (ev) => {
      const btn = ev.target.closest('.buy-btn');
      if (!btn) return;
      const event_uuid = btn.dataset.event;
      const option_index = parseInt(btn.dataset.index, 10);
      const side = btn.dataset.side;
      const optText = btn.dataset.text || '';
      openBuy(event_uuid, option_index, side, optText);
    });

    window.openBuy = openBuy; window.confirmBuy = confirmBuy; window.closeBuy = closeBuy;

    // Инициализация
    (function init(){
      if (tg) tg.ready();
      applySavedCollapses();
      setAvatar();
      fetchMe();
      // Лидеров по умолчанию не загружаем — загрузятся при первом раскрытии
    })();
  </script>
</body>
</html>
"""

@app.get("/mini-app")
def mini_app():
    events = db.get_published_events()
    enriched = []
    for e in events:
        mk = db.get_markets_for_event(e["event_uuid"])
        markets = {}
        for m in mk:
            yes = float(m["total_yes_reserve"])
            no = float(m["total_no_reserve"])
            total = yes + no
            if total == 0:
                yp, np = 0.5, 0.5
            else:
                yp, np = no / total, yes / total
            markets[m["option_index"]] = {"yes_price": yp, "no_price": np}
        e["markets"] = markets
        enriched.append(e)
    return render_template_string(MINI_APP_HTML, events=enriched, enumerate=enumerate)


# ------------- API: профиль, покупка, аватар, лидеры -------------
@app.get("/api/me")
def api_me():
    try:
        chat_id = int(request.args.get("chat_id", "0"))
    except Exception:
        return jsonify(success=False, error="bad_chat_id"), 400
    sig = request.args.get("sig", "")
    if not verify_sig(chat_id, sig):
        return jsonify(success=False, error="bad_sig"), 403
    u = db.get_user(chat_id)
    if not u:
        return jsonify(success=False, error="user_not_found"), 404
    if u.get("status") != "approved":
        return jsonify(success=False, error="not_approved"), 403
    positions = db.get_user_positions(chat_id)
    return jsonify(success=True, user={"chat_id": chat_id, "balance": u.get("balance", 0)}, positions=positions)


@app.post("/api/market/buy")
def api_market_buy():
    payload = request.get_json(silent=True) or {}
    try:
        chat_id = int(payload.get("chat_id"))
        event_uuid = str(payload.get("event_uuid"))
        option_index = int(payload.get("option_index"))
        side = str(payload.get("side"))
        amount = float(payload.get("amount"))
    except Exception:
        return jsonify(success=False, error="bad_payload"), 400
    sig = payload.get("sig", "")
    if not verify_sig(chat_id, sig):
        return jsonify(success=False, error="bad_sig"), 403
    result, err = db.trade_buy(
        chat_id=chat_id,
        event_uuid=event_uuid,
        option_index=option_index,
        side=side,
        amount=amount,
    )
    if err:
        return jsonify(success=False, error=err), 400
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
    try:
        chat_id = int(request.args.get("chat_id", "0"))
    except Exception:
        return "bad_chat_id", 400
    sig = request.args.get("sig", "")
    if not verify_sig(chat_id, sig):
        return "bad_sig", 403
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
        r2 = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getFile", params={"file_id": file_id}, timeout=10
        )
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
    week = db.week_current_bounds()  # {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "label": "DD.MM.YY-DD.MM.YY"}
    items = db.get_leaderboard(week["start"], limit=50)
    return jsonify(success=True, week=week, items=items)


# ------------- Admin panel (как было) -------------
ADMIN_PANEL_HTML = """
<!doctype html>
<html lang="ru">
<head><meta charset="utf-8"><title>Админ</title></head>
<body>
  <h1>Админская панель бота</h1>

  <h2>Заявки на регистрацию ({{ pending_count }})</h2>
  {% for user in pending_users %}
    <div style="margin:8px 0;padding:6px;border:1px solid #ccc;">
      <div><b>ID:</b> {{ user.chat_id }}</div>
      <div><b>Логин:</b> {{ user.login }}</div>
      <div><b>Username:</b> @{{ user.username }}</div>
      <div><b>Дата:</b> {{ (user.created_at or '')[:16] }}</div>
      <div>
        <form method="post" action="/admin/approve/{{ user.chat_id }}" style="display:inline;">
          <button type="submit">✅ Одобрить</button>
        </form>
        <form method="post" action="/admin/reject/{{ user.chat_id }}" style="display:inline;">
          <button type="submit">❌ Отклонить</button>
        </form>
      </div>
    </div>
  {% endfor %}

  <h2>Одобренные пользователи ({{ approved_count }})</h2>
  {% for user in approved_users %}
    <div style="margin:8px 0;padding:6px;border:1px solid #ccc;">
      <div><b>ID:</b> {{ user.chat_id }}</div>
      <div><b>Логин:</b> {{ user.login }}</div>
      <div><b>Username:</b> @{{ user.username }}</div>
      <div><b>Баланс:</b> {{ user.balance }} кредитов</div>
      <div><b>Одобрен:</b> {{ (user.approved_at or '')[:16] }}</div>
      <form method="post" action="/admin/update_balance/{{ user.chat_id }}">
        <input type="number" name="balance" min="0" step="1" value="{{ user.balance }}" />
        <button type="submit">Обновить</button>
      </form>
    </div>
  {% endfor %}

  <h2>Управление мероприятиями</h2>
  <p><a href="/admin/create_event">Создать новое мероприятие</a></p>

  <h2>Торговля</h2>
  <p><a href="/admin/orders">Последние сделки</a> · <a href="/admin/positions">Позиции пользователей</a></p>
</body>
</html>
"""

@app.get("/admin")
@requires_auth
def admin_panel():
    pending = db.get_pending_users()
    approved = db.get_approved_users()
    return render_template_string(
        ADMIN_PANEL_HTML,
        pending_users=pending,
        approved_users=approved,
        pending_count=len(pending),
        approved_count=len(approved),
    )


@app.post("/admin/approve/<int:chat_id>")
@requires_auth
def approve_user(chat_id: int):
    user = db.approve_user(chat_id)
    if user:
        balance = user.get("balance", 1000)
        send_message(chat_id, f"Поздравляем! Ваша заявка одобрена.\nВаш стартовый баланс: {balance} кредитов")
        return jsonify(success=True)
    return jsonify(success=False, error="Пользователь не найден"), 404


@app.post("/admin/reject/<int:chat_id>")
@requires_auth
def reject_user(chat_id: int):
    u = db.get_user(chat_id)
    if u:
        send_message(chat_id, "❌ К сожалению, ваша заявка отклонена. Подайте заново через /start")
        return jsonify(success=True)
    return jsonify(success=False, error="Пользователь не найден"), 404


@app.post("/admin/update_balance/<int:chat_id>")
@requires_auth
def update_user_balance(chat_id: int):
    if request.is_json:
        new_balance = request.json.get("balance")
    else:
        new_balance = request.form.get("balance", type=int)

    if new_balance is None or int(new_balance) < 0:
        return jsonify(success=False, error="Некорректная сумма"), 400

    user = db.update_user_balance(chat_id, int(new_balance))
    if user:
        return jsonify(success=True)
    return jsonify(success=False, error="Пользователь не найден"), 404


@app.get("/admin/create_event")
@requires_auth
def create_event_form():
    return """
    <h3>Создание мероприятия</h3>
    <form method="post" action="/admin/publish_event">
      <div>Название: <input name="event_name" required></div>
      <div>Правила и описание:<br><textarea name="event_rules" rows="6" cols="60" required></textarea></div>
      <div>Варианты:<br>
        <input name="option_1" placeholder="Вариант 1" required><br>
        <input name="option_2" placeholder="Вариант 2" required><br>
        <button type="button" onclick="addOpt()">➕ Добавить вариант</button>
      </div>
      <div>Дата окончания: <input type="datetime-local" name="end_date" required></div>
      <div><button type="submit">Опубликовать</button></div>
    </form>
    <script>
      function addOpt(){
        const n = document.querySelectorAll('input[name^=option_]').length + 1;
        const inp = document.createElement('input');
        inp.name = 'option_' + n;
        inp.placeholder = 'Вариант ' + n;
        document.forms[0].insertBefore(inp, document.forms[0].children[6]);
        document.forms[0].insertBefore(document.createElement('br'), document.forms[0].children[6]);
      }
    </script>
    """


@app.post("/admin/publish_event")
@requires_auth
def publish_event():
    event_name = request.form["event_name"]
    event_rules = request.form["event_rules"]
    end_date = request.form["end_date"]

    options = []
    i = 1
    while True:
        key = f"option_{i}"
        if key not in request.form:
            break
        text = request.form[key].strip()
        if text:
            options.append({"text": text, "votes": 0})
        i += 1

    event_uuid = str(uuid.uuid4())[:8]
    event = {
        "event_uuid": event_uuid,
        "name": event_name,
        "description": event_rules,
        "options": options,
        "end_date": end_date.replace("T", " ") + ":00",
        "is_published": True,
        "creator_id": int(ADMIN_ID) if ADMIN_ID else None,
        "participants": 0,
        "created_at": datetime.utcnow().isoformat(),
    }

    new_event = db.create_event(event)
    if new_event:
        for idx in range(len(options)):
            db.create_prediction_market(event_uuid, idx)

        for u in db.get_approved_users():
            msg = (
                f"Новое мероприятие!\n\n{event_name}\n{event_rules[:100]}...\n"
                f"⏰ До: {end_date[:16]}\n\nИспользуйте /app → Mini App."
            )
            send_message(u["chat_id"], msg)

        return f"<h3>✅ Опубликовано</h3><p>Название: {event_name}</p><p>Вариантов: {len(options)}</p><p>Окончание: {end_date}</p>"

    return Response("Ошибка при создании события", 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
