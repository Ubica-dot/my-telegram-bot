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

WEBAPP_SIGNING_SECRET = os.getenv("WEBAPP_SIGNING_SECRET")  # ОБЯЗАТЕЛЬНО задайте в Render


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
    # Без секрета Mini App запрещён
    if not WEBAPP_SIGNING_SECRET:
        return ""
    msg = str(chat_id).encode()
    return hmac.new(WEBAPP_SIGNING_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:32]


def verify_sig(chat_id: int, sig: str) -> bool:
    if not WEBAPP_SIGNING_SECRET:
        return False
    return hmac.compare_digest(make_sig(chat_id), sig or "")


# ---------- Health ----------
@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/")
def index():
    return "OK"


# ---------- Telegram webhook: только /start ----------
@app.post("/webhook")
def telegram_webhook():
    # Проверка секрета Telegram
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

    # Разрешаем только /start, остальное игнорируем
    if text == "/start":
        if not user:
            # Нет в базе — просим ввести логин одним сообщением
            send_message(
                chat_id,
                "Добро пожаловать! Напишите ваш желаемый логин одним сообщением. После модерации получите доступ к приложению."
            )
        else:
            if status == "approved":
                # Одобрен — сразу даём кнопку Mini App
                sig = make_sig(chat_id)
                if not sig:
                    send_message(chat_id, "Сервис временно недоступен. Повторите позже.")
                    return "ok"
                web_app_url = f"https://{request.host}/mini-app?chat_id={chat_id}&sig={sig}&v={int(time.time())}"
                kb = { "inline_keyboard": [ [{"text": "Открыть Mini App", "web_app": {"url": web_app_url}}] ] }
                send_message(chat_id, "Приложение готово. Открывайте:", kb)
            elif status == "pending":
                send_message(chat_id, "⏳ Ваша заявка на регистрацию ожидает проверки администратором.")
            elif status == "banned":
                send_message(chat_id, "🚫 Доступ к приложению запрещён.")
            elif status == "rejected":
                send_message(chat_id, "❌ Заявка отклонена. Отправьте новый логин одним сообщением для повторной подачи.")
            else:
                send_message(chat_id, "Напишите ваш логин одним сообщением для регистрации.")
        return "ok"

    # Любой текст НЕ команда: если нет пользователя — трактуем как логин, если в pending — просто уведомляем
    if not text.startswith("/"):
        if not user:
            new_user = db.create_user(chat_id, text, username)
            if new_user:
                send_message(chat_id, f"✅ Логин '{text}' отправлен на модерацию. Ожидайте подтверждения.")
                notify_admin(f"Новая заявка:\nЛогин: {text}\nID: {chat_id}\nUsername: @{username}\nАдминка: {BASE_URL}/admin")
            else:
                send_message(chat_id, "❌ Ошибка при создании заявки. Попробуйте ещё раз.")
        else:
            if status == "pending":
                send_message(chat_id, "⏳ Заявка уже на рассмотрении. Ожидайте ответа администратора.")
            elif status == "approved":
                pass
            elif status == "banned":
                send_message(chat_id, "🚫 Доступ запрещён.")
        return "ok"

    # Любые другие /команды игнорируем
    return "ok"


# ---------- Mini App HTML ----------
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
    .btn  { padding: 10px 14px; border: 0; border-radius: 10px; cursor: pointer; font-size: 15px; font-weight: 700;
            transition: background-color .18s ease, color .18s ease; min-width: 92px; min-height: 42px; }
    /* Мягкие фоны (без alpha). На hover — насыщенный фон, текст — бледный в тон исходному фону */
    .yes  { background: #CDEAD2; color: #2e7d32; }
    .no   { background: #F3C7C7; color: #c62828; }
    .yes:hover { background: #2e7d32; color: #CDEAD2; }
    .no:hover  { background: #c62828; color: #F3C7C7; }
    .actions { display:flex; gap: 8px; align-items: stretch; height: 100%; }
    .actions .btn { height: 100%; display:flex; align-items:center; justify-content:center; }
    .muted { color: #666; font-size: 14px; }
    .meta-left  { grid-column: 1; font-size: 12px; color: #666; margin-top: 6px; }
    .meta-right { grid-column: 3; justify-self: end; font-size: 12px; color: #666; margin-top: 6px; }
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
    /* active bets */
    .bet { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    .bet-info { flex: 1 1 auto; }
    .bet-win { flex: 0 0 auto; text-align:right; }
    .win-val { font-size:22px; font-weight:900; color:#0b8457; line-height:1.1; }
    .win-sub { font-size:12px; color:#666; }
    .unit { font-size:12px; font-weight:700; color:#444; margin-left:2px; }
    /* leaderboard */
    .lb-head { text-align:center; font-weight:700; margin:6px 0 10px; }
    .seg { display:inline-flex; background:#f0f0f0; border-radius:999px; padding:4px; gap:4px; }
    .seg-btn { border:0; background:transparent; padding:6px 12px; border-radius:999px; font-weight:700; cursor:pointer; color:#444; transition:all .15s ease; }
    .seg-btn.active { background:#fff; box-shadow:0 1px 4px rgba(0,0,0,.08); color:#111; }
    .lb-table { width:100%; }
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
            <p>{{ e.description }}</p>

            {% for idx, opt in enumerate(e.options) %}
              {% set md = e.markets.get(idx, {'yes_price': 0.5, 'volume': 0, 'end_short': e.end_short}) %}
              {% set yes_pct = ('%.0f' % (md.yes_price * 100)) %}
              {% set no_pct  = ('%.0f' % ((1 - md.yes_price) * 100)) %}
              <div class="opt" data-option="{{ idx }}">
                <div class="opt-title">{{ opt.text }}</div>
                <div class="prob" title="Вероятность ДА">{{ yes_pct }}%</div>
                <div class="actions">
                  <button class="btn yes buy-btn"
                          data-event="{{ e.event_uuid }}"
                          data-index="{{ idx }}"
                          data-side="yes"
                          data-prob="{{ yes_pct }}"
                          data-text="{{ opt.text|e }}">ДА</button>

                  <button class="btn no buy-btn"
                          data-event="{{ e.event_uuid }}"
                          data-index="{{ idx }}"
                          data-side="no"
                          data-prob="{{ no_pct }}"
                          data-text="{{ opt.text|e }}">НЕТ</button>
                </div>
                <div class="meta-left">
                  {% set vol = (md.volume or 0) | int %}
                  {% if vol >= 1000 %}
                    {{ (vol // 1000) | int }} тыс. кредитов
                  {% else %}
                    {{ vol }} кредитов
                  {% endif %}
                </div>
                <div class="meta-right">До {{ md.end_short }}</div>
              </div>
            {% endfor %}
          </div>
        {% endfor %}
      </div>
    </div>
  </div>

  <!-- Таблица лидеров -->
  <div id="wrap-leaders" class="section">
    <div id="head-leaders" class="section-head" onclick="toggleLeaders()">
      <span class="section-title">Таблица лидеров</span>
      <span id="caret-leaders" class="caret">▸</span>
    </div>
    <div id="section-leaders" class="section-body" style="display:none;">
      <div style="display:flex; align-items:center; justify-content:space-between; margin:6px 0 10px;">
        <div id="lb-range" class="lb-head" style="margin:0; flex:1; text-align:center;">—</div>
      </div>
      <div style="display:flex; justify-content:center; margin-bottom:8px;">
        <div class="seg" id="seg">
          <button class="seg-btn active" data-period="week">Неделя</button>
          <button class="seg-btn" data-period="month">Месяц</button>
        </div>
      </div>
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

    // Секции
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
        if (!toggleLeaders._loaded) { fetchLeaderboard(currentPeriod); toggleLeaders._loaded = true; }
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

    // Баланс и активные позиции
    async function fetchMe() {
      const cid = getChatId();
      const activeDiv = document.getElementById("active");
      if (!cid) {
        document.getElementById("balance").textContent = "Баланс: —";
        activeDiv.textContent = "Не удалось получить chat_id. Откройте Mini App из чата командой /start.";
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

    // hover: подмена текста на вероятность
    document.addEventListener('mouseenter', (ev) => {
      const btn = ev.target.closest('.buy-btn');
      if (!btn) return;
      btn.dataset.label = btn.textContent.trim();
      const prob = btn.dataset.prob ? `${btn.dataset.prob}%` : btn.textContent.trim();
      btn.textContent = prob;
    }, true);

    document.addEventListener('mouseleave', (ev) => {
      const btn = ev.target.closest('.buy-btn');
      if (!btn) return;
      const label = btn.dataset.label || (btn.dataset.side === 'yes' ? 'ДА' : 'НЕТ');
      btn.textContent = label;
    }, true);

    // ---- лидеры неделя/месяц ----
    let currentPeriod = 'week';
    function bindSeg() {
      const seg = document.getElementById('seg');
      if (!seg) return;
      seg.addEventListener('click', (e) => {
        const b = e.target.closest('.seg-btn');
        if (!b) return;
        const period = b.dataset.period || 'week';
        if (period === currentPeriod) return;
        currentPeriod = period;
        for (const x of seg.querySelectorAll('.seg-btn')) x.classList.toggle('active', x === b);
        fetchLeaderboard(period);
      });
    }

    async function fetchLeaderboard(period='week') {
      const cid = getChatId();
      const lb = document.getElementById("lb-container");
      try {
        const url = "/api/leaderboard?period=" + encodeURIComponent(period) + (cid ? `&viewer=${cid}` : "");
        const r = await fetch(url);
        const data = await r.json();
        if (!r.ok || !data.success) {
          lb.textContent = "Ошибка загрузки рейтинга.";
          return;
        }
        document.getElementById("lb-range").textContent = data.week.label;

        const items = data.items || [];
        if (items.length === 0) {
          lb.textContent = "Пока нет данных за текущий период.";
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

    // Покупка
    let buyCtx = null;
    function openBuy(event_uuid, option_index, side, optText) {
      const cid = getChatId();
      if (!cid) { alert("Не удалось получить chat_id. Откройте Mini App из чата командой /start."); return; }
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

        const card = document.querySelector(`[data-event='${buyCtx.event_uuid}'] [data-option='${buyCtx.option_index}']`);
        if (card) {
          const probEl = card.querySelector(".prob");
          if (probEl) probEl.textContent = `${Math.round(data.market.yes_price * 100)}%`;
        }

        await fetchMe();
        if (document.getElementById("section-leaders").style.display !== "none") fetchLeaderboard(currentPeriod);

        closeBuy();
        if (tg && tg.showPopup) tg.showPopup({ title: "Успешно", message: `Куплено ${data.trade.got_shares.toFixed(4) + " акций" (${buyCtx.side.toUpperCase()})` });
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
      bindSeg();
      // Лидеров грузим при первом раскрытии
    })();
  </script>
</body>
</html>
"""


def _format_end_short(end_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(end_iso.replace(" ", "T").split(".")[0])
        return dt.strftime("%d.%m.%y")
    except Exception:
        s = end_iso[:10]
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            y, m, d = s.split("-")
            return f"{d}.{m}.{y[2:]}"
        return end_iso[:10]


@app.get("/mini-app")
def mini_app():
    # Жёсткий доступ: нужен chat_id + sig + статус approved (и не banned)
    chat_id = request.args.get("chat_id", type=int)
    sig = request.args.get("sig", "")
    if not chat_id or not sig or not verify_sig(chat_id, sig):
        return Response("<h3>Доступ запрещён</h3><p>Откройте Mini App из бота после /start и одобрения.</p>", mimetype="text/html")

    user = db.get_user(chat_id)
    if not user or user.get("status") != "approved":
        return Response("<h3>Доступ только для одобренных пользователей</h3><p>Дождитесь одобрения администратора.</p>", mimetype="text/html")
    if user.get("status") == "banned":
        return Response("<h3>Доступ запрещён</h3>", mimetype="text/html")

    # Данные для шаблона
    events = db.get_published_events()
    for e in events:
        end_iso = str(e.get("end_date", ""))
        e["end_short"] = _format_end_short(end_iso)

        mk = db.get_markets_for_event(e["event_uuid"])
        markets = {}
        for m in mk:
            yes = float(m["total_yes_reserve"])
            no = float(m["total_no_reserve"])
            total = yes + no
            yp = (no / total) if total > 0 else 0.5
            volume = max(0.0, total - 2000.0)  # «заведённые» кредиты в пул сверх стартовых
            markets[m["option_index"]] = {"yes_price": yp, "volume": volume, "end_short": e["end_short"]}
        e["markets"] = markets

    return render_template_string(MINI_APP_HTML, events=events, enumerate=enumerate)


# ---------- API (все требуют подпись и status=approved) ----------
@app.get("/api/me")
def api_me():
    chat_id = request.args.get("chat_id", type=int)
    sig = request.args.get("sig", "")
    if not chat_id or not verify_sig(chat_id, sig):
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
        sig = str(payload.get("sig") or "")
        if not verify_sig(chat_id, sig):
            return jsonify(success=False, error="bad_sig"), 403
        event_uuid = str(payload.get("event_uuid"))
        option_index = int(payload.get("option_index"))
        side = str(payload.get("side"))
        amount = float(payload.get("amount"))
    except Exception:
        return jsonify(success=False, error="bad_payload"), 400

    result, err = db.trade_buy(
        chat_id=chat_id, event_uuid=event_uuid, option_index=option_index, side=side, amount=amount
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
    chat_id = request.args.get("chat_id", type=int)
    sig = request.args.get("sig", "")
    if not chat_id or not verify_sig(chat_id, sig):
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
    if period == "month":
        bounds = db.month_current_bounds()
        items = db.get_leaderboard_month(bounds["start"], limit=50)
    else:
        bounds = db.week_current_bounds()
        items = db.get_leaderboard_week(bounds["start"], limit=50)
    return jsonify(success=True, week=bounds, items=items)


# ---------- Admin panel ----------
ADMIN_HTML = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"><title>Админ</title>
<style>
body{font-family:system-ui, -apple-system, Segoe UI, Roboto, sans-serif;margin:16px;}
h1{margin:0 0 12px;}
.section{margin:18px 0;}
.item{border:1px solid #ddd;border-radius:10px;margin:6px 0;}
.item-head{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;cursor:pointer;}
.item-body{display:none;border-top:1px dashed #ddd;padding:10px 12px;}
.badge{padding:2px 8px;border-radius:999px;font-size:12px;background:#eee;}
.row{margin:6px 0;}
.btn{padding:6px 10px;border:0;border-radius:8px;cursor:pointer;}
.approve{background:#2e7d32;color:#fff;}
.reject{background:#999;color:#fff;}
.ban{background:#c62828;color:#fff;}
.unban{background:#6a5acd;color:#fff;}
.save{background:#1976d2;color:#fff;}
small{color:#666;}
.list{display:flex;flex-direction:column;gap:6px;}
</style>
<script>
function toggleBody(id){
  const e=document.getElementById(id);
  e.style.display = e.style.display==='none' || !e.style.display ? 'block':'none';
}
async function adminPost(url, data){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data||{})});
  if(!r.ok){alert('Ошибка'); return null;}
  return await r.json().catch(()=>null);
}
</script>
</head>
<body>
<h1>Админская панель</h1>

<div class="section">
  <h2>Заявки ({{ pending|length }})</h2>
  <div class="list">
  {% for u in pending %}
    <div class="item">
      <div class="item-head" onclick="toggleBody('p{{u.chat_id}}')">
        <div>#{{ loop.index }} • <b>{{ u.login }}</b> <small>(@{{u.username}})</small></div>
        <div><span class="badge">ID {{ u.chat_id }}</span></div>
      </div>
      <div class="item-body" id="p{{u.chat_id}}">
        <div class="row"><small>Заявка от: {{ (u.created_at or '')[:16] }}</small></div>
        <div class="row">
          <button class="btn approve" onclick="adminPost('/admin/approve/{{u.chat_id}}').then(()=>location.reload())">Одобрить</button>
          <button class="btn reject" onclick="adminPost('/admin/reject/{{u.chat_id}}').then(()=>location.reload())">Отклонить</button>
        </div>
      </div>
    </div>
  {% endfor %}
  </div>
</div>

<div class="section">
  <h2>Одобренные ({{ approved|length }})</h2>
  <div class="list">
  {% for u in approved %}
    <div class="item">
      <div class="item-head" onclick="toggleBody('a{{u.chat_id}}')">
        <div>#{{ loop.index }} • <b>{{ u.login }}</b> <small>(@{{u.username}})</small></div>
        <div><span class="badge">Баланс: {{u.balance}}</span></div>
      </div>
      <div class="item-body" id="a{{u.chat_id}}">
        <div class="row">Изменить баланс:
          <input type="number" id="b{{u.chat_id}}" value="{{u.balance}}" style="width:120px;">
          <button class="btn save" onclick="
            adminPost('/admin/update_balance/{{u.chat_id}}',{balance: parseInt(document.getElementById('b{{u.chat_id}}').value||0)})
            .then(()=>location.reload())
          ">Сохранить</button>
        </div>
        <div class="row">
          <button class="btn ban" onclick="adminPost('/admin/ban/{{u.chat_id}}').then(()=>location.reload())">Бан</button>
        </div>
      </div>
    </div>
  {% endfor %}
  </div>
</div>

<div class="section">
  <h2>Забаненные ({{ banned|length }})</h2>
  <div class="list">
  {% for u in banned %}
    <div class="item">
      <div class="item-head" onclick="toggleBody('b{{u.chat_id}}')">
        <div>#{{ loop.index }} • <b>{{ u.login }}</b> <small>(@{{u.username}})</small></div>
        <div><span class="badge">ID {{ u.chat_id }}</span></div>
      </div>
      <div class="item-body" id="b{{u.chat_id}}">
        <div class="row"><small>Статус: banned</small></div>
        <div class="row">
          <button class="btn unban" onclick="adminPost('/admin/unban/{{u.chat_id}}').then(()=>location.reload())">Разбанить (одобрить)</button>
        </div>
      </div>
    </div>
  {% endfor %}
  </div>
</div>

</body>
</html>
"""

@app.get("/admin")
@requires_auth
def admin_panel():
    pending = db.get_pending_users()
    approved = db.get_approved_users()
    banned = db.get_banned_users()
    return render_template_string(ADMIN_HTML, pending=pending, approved=approved, banned=banned)


# Действия админа
@app.post("/admin/approve/<int:chat_id>")
@requires_auth
def admin_approve(chat_id: int):
    user = db.approve_user(chat_id)
    if user:
        # Отправим пользователю кнопку Mini App
        sig = make_sig(chat_id)
        if sig:
            web_app_url = f"https://{request.host}/mini-app?chat_id={chat_id}&sig={sig}&v={int(time.time())}"
            kb = { "inline_keyboard": [ [{"text": "Открыть Mini App", "web_app": {"url": web_app_url}}] ] }
            send_message(chat_id, "✅ Ваша регистрация подтверждена! Добро пожаловать.", kb)
        return jsonify(success=True)
    return jsonify(success=False), 404


@app.post("/admin/reject/<int:chat_id>")
@requires_auth
def admin_reject(chat_id: int):
    # По требованию — НЕ уведомляем пользователя об отклонении
    ok = db.reject_user(chat_id)
    return jsonify(success=bool(ok))


@app.post("/admin/update_balance/<int:chat_id>")
@requires_auth
def admin_update_balance(chat_id: int):
    payload = request.get_json(silent=True) or {}
    new_balance = payload.get("balance")
    try:
        new_balance = int(new_balance)
        if new_balance < 0:
            raise ValueError
    except Exception:
        return jsonify(success=False, error="bad_balance"), 400
    user = db.update_user_balance(chat_id, new_balance)
    return jsonify(success=bool(user))


@app.post("/admin/ban/<int:chat_id>")
@requires_auth
def admin_ban(chat_id: int):
    user = db.ban_user(chat_id)
    return jsonify(success=bool(user))


@app.post("/admin/unban/<int:chat_id>")
@requires_auth
def admin_unban(chat_id: int):
    user = db.unban_user(chat_id)
    return jsonify(success=bool(user))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


