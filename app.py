import os
import json
import hmac
import hashlib
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import requests
from flask import Flask, request, render_template_string, jsonify, Response, stream_with_context
from functools import wraps
from collections import deque, defaultdict

from database import db

app = Flask(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
BASE_URL = os.getenv("WEBHOOK_URL")
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "change-me")

ADMIN_BASIC_USER = os.getenv("ADMIN_BASIC_USER", "admin")
ADMIN_BASIC_PASS = os.getenv("ADMIN_BASIC_PASS", "admin")

WEBAPP_SIGNING_SECRET = os.getenv("WEBAPP_SIGNING_SECRET")  # обязательный секрет


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


# ---------- Health / Legal ----------
@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/")
def index():
    return "OK"


LEGAL_HTML = """
<!doctype html><meta charset="utf-8">
<title>Правила и политика конфиденциальности</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:780px;margin:24px auto;padding:0 16px;line-height:1.55;color:#222}
  h1{margin:0 0 12px}
  h2{margin:18px 0 8px}
  p{margin:8px 0}
</style>
<h1>Условия использования</h1>
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
                "Добро пожаловать! Напишите ваш желаемый логин одним сообщением. После модерации получите доступ к приложению."
            )
        else:
            if status == "approved":
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
    :root{
      --spot-x: 50%;
      --spot-y: 50%;
    }
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 12px; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 12px; margin-bottom: 14px; background:#fff }
    .opt  { padding: 10px; border: 1px dashed #ccc; border-radius: 10px; margin: 6px 0;
            display: grid; grid-template-columns: 1fr auto auto; gap: 10px; align-items: stretch; }
    .opt-title { display:flex; align-items:center; font-weight: 700; }
    .btn  { padding: 10px 14px; border: 0; border-radius: 10px; cursor: pointer; font-size: 15px; font-weight: 700;
            transition: background-color .18s ease, color .18s ease; min-width: 92px; min-height: 42px; }
    .yes  { background: #CDEAD2; color: #2e7d32; }
    .no   { background: #F3C7C7; color: #c62828; }
    .yes:hover { background: #2e7d32; color: #CDEAD2; }
    .no:hover  { background: #c62828; color: #F3C7C7; }
    .actions { display:flex; gap: 8px; align-items: stretch; height: 100%; }
    .actions .btn { height: 100%; display:flex; align-items:center; justify-content:center; }
    .muted { color: #666; font-size: 14px; }
    .meta-left  { grid-column: 1; font-size: 12px; color: #666; margin-top: 6px; }
    .meta-right { grid-column: 3; justify-self: end; font-size: 12px; color: #666; margin-top: 6px; }
    .section { margin: 18px 0; }
    .section-head { display:flex; align-items:center; justify-content:space-between; padding:10px 12px;
                    background:#f5f5f5; border-radius:10px; cursor:pointer; user-select:none; position: relative; overflow:hidden; }
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
    .balance { text-align:center; font-weight:900; font-size:22px; margin: 10px 0 10px; }
    /* slider (декоративный) */
    .slider { margin: 6px auto 10px; width: 220px; height: 34px; background:#eee; border-radius:999px; position:relative; box-shadow: inset 0 1px 3px rgba(0,0,0,.08); }
    .slider .handle { position:absolute; top:3px; left:3px; width: 214px; height: 28px; background: linear-gradient(180deg,#fff,#f6f6f6); border-radius:999px;
                      display:flex; align-items:center; justify-content:center; font-weight:700; color:#333; }
    .toolbar { display:flex; gap:8px; align-items:center; margin: 6px 0 6px; }
    .toolbar input { flex: 1 1 auto; padding:8px; border-radius:8px; border:1px solid #ddd; }
    /* footer link */
    .footer-link { position: fixed; bottom: 8px; left: 50%; transform: translateX(-50%); color: #9aa; text-decoration: none;
                   font-size: 12px; opacity: .6; }
    .footer-link:hover { opacity: .9; }
    /* hover radial gradient on section-head */
    .hover-spot::after{
      content:""; position:absolute; inset:0; pointer-events:none;
      background: radial-gradient(200px circle at var(--spot-x) var(--spot-y), rgba(0,0,0,.06), transparent 60%);
      opacity:0; transition: opacity .15s ease;
    }
    .hover-spot:hover::after{ opacity:1; }
    /* архив */
    .arch-row { border:1px solid #eee; border-radius:10px; padding:10px; margin:6px 0; display:flex; justify-content:space-between; gap:8px; }
    .arch-win { background:#eaf7ef; }
    .arch-lose{ background:#fdecec; }
  </style>
</head>
<body>

  <div class="topbar">
    <div class="avatar-wrap">
      <div id="avatarPH" class="avatar ph">U</div>
      <img id="avatar" class="avatar img" alt="avatar">
    </div>
  </div>
  <div id="balance" class="balance">Баланс: —</div>

  <!-- Поиск (клиентский) -->
  <div class="toolbar">
    <input id="q" placeholder="Поиск по событиям и тегам…">
  </div>

  <!-- Активные ставки -->
  <div id="wrap-active" class="section">
    <div id="head-active" class="section-head hover-spot" onclick="toggleSection('active')">
      <span class="section-title">Активные ставки</span>
      <span id="caret-active" class="caret">▾</span>
    </div>
    <div id="section-active" class="section-body">
      <div id="active" class="muted">Загрузка...</div>
    </div>
  </div>

  <!-- Архив ставок -->
  <div id="wrap-arch" class="section">
    <div id="head-arch" class="section-head hover-spot" onclick="toggleSection('arch')">
      <span class="section-title">Прошедшие ставки (архив)</span>
      <span id="caret-arch" class="caret">▾</span>
    </div>
    <div id="section-arch" class="section-body">
      <div id="arch" class="muted">Загрузка...</div>
    </div>
  </div>

  <!-- Мероприятия -->
  <div id="wrap-events" class="section">
    <div id="head-events" class="section-head hover-spot" onclick="toggleSection('events')">
      <span class="section-title">Мероприятия</span>
      <span id="caret-events" class="caret">▾</span>
    </div>
    <div id="section-events" class="section-body">
      <div id="events-list">
        {% for e in events %}
          <div class="card" data-event="{{ e.event_uuid }}" data-tags="{{ (e.tags or [])|join(',') }}" data-end="{{ e.end_ts }}" data-vol="{{ e.total_volume|int }}">
            <div><b>{{ e.name }}</b></div>
            <p class="muted">{{ e.description }}</p>
            {% if e.tags %}<div class="muted">Теги: {{ e.tags|join(', ') }}</div>{% endif %}

            {% for idx, opt in enumerate(e.options) %}
              {% set md = e.markets.get(idx, {'yes_price': 0.5, 'volume': 0, 'end_short': e.end_short, 'resolved': false, 'winner_side': None}) %}
              {% set yes_pct = ('%.0f' % (md.yes_price * 100)) %}
              {% set no_pct  = ('%.0f' % ((1 - md.yes_price) * 100)) %}
              <div class="opt" data-option="{{ idx }}">
                <div class="opt-title">{{ opt.text }}</div>
                <div class="prob" title="Вероятность ДА">
                  {% if md.resolved %}
                    {{ 'YES' if md.winner_side=='yes' else 'NO' }} ✓
                  {% else %}
                    {{ yes_pct }}%
                  {% endif %}
                </div>
                <div class="actions">
                  {% if not md.resolved %}
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
                  {% else %}
                  <button class="btn yes" disabled>Закрыт</button>
                  {% endif %}
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
    <div id="head-leaders" class="section-head hover-spot" onclick="toggleLeaders()">
      <span class="section-title">Таблица лидеров</span>
      <span id="caret-leaders" class="caret">▸</span>
    </div>
    <div id="section-leaders" class="section-body" style="display:none;">
      <!-- Декоративный слайдер вместо переключателя периода -->
      <div class="slider" title="Неделя">
        <div class="handle">Неделя</div>
      </div>
      <div id="lb-range" class="muted" style="margin:6px 0 10px;text-align:center;">—</div>
      <div id="lb-container" class="muted">Загрузка…</div>
    </div>
  </div>

  <a class="footer-link" href="/legal" target="_blank" rel="noopener" style="text-decoration:none;">Правила и политика конфиденциальности</a>

  <!-- Покупка -->
  <div id="modalBg" class="modal-bg" style="position: fixed; inset: 0; background: rgba(0,0,0,.5); display:none; align-items:center; justify-content:center;">
    <div class="modal" style="background:#fff; border-radius:12px; padding:16px; width:90%; max-width:400px;">
      <h3 id="mTitle" style="margin:0 0 8px 0;">Покупка</h3>
      <div class="muted" id="mSub">Укажите сумму, не выше вашего баланса.</div>
      <div class="row" style="margin-top:10px;">
        <label>Сумма (кредиты):&nbsp;</label>
        <input type="number" id="mAmount" min="1" step="1" value="100"/>
      </div>
      <div class="row" style="margin-top:12px; display:flex; gap:8px;">
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
    const INIT = tg && tg.initData ? tg.initData : "";

    function getChatId() {
      if (CHAT_ID) return CHAT_ID;
      if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id) return tg.initDataUnsafe.user.id;
      return null;
    }

    // hover radial-spot tracking
    document.addEventListener('pointermove', (e) => {
      const head = e.target.closest('.hover-spot');
      if (!head) return;
      const rect = head.getBoundingClientRect();
      head.style.setProperty('--spot-x', (e.clientX - rect.left) + 'px');
      head.style.setProperty('--spot-y', (e.clientY - rect.top) + 'px');
    });

    // Секции
    function toggleSection(name) {
      const body = document.getElementById("section-" + name);
      const caret = document.getElementById("caret-" + name);
      const head = document.getElementById("head-" + name);
      const key = "collapse_" + name;
      const nowShown = body.style.display !== "none";
      if (nowShown) {
        body.style.display = "none"; caret.textContent = "▸"; head.classList.add("collapsed");
        try { localStorage.setItem(key, "1"); } catch(e){}
      } else {
        body.style.display = "block"; caret.textContent = "▾"; head.classList.remove("collapsed");
        try { localStorage.setItem(key, "0"); } catch(e){}
      }
    }
    function toggleLeaders() {
      const body = document.getElementById("section-leaders");
      const caret = document.getElementById("caret-leaders");
      const head  = document.getElementById("head-leaders");
      const shown = body.style.display !== "none";
      if (shown) { body.style.display = "none"; caret.textContent = "▸"; head.classList.add("collapsed"); }
      else { body.style.display = "block"; caret.textContent = "▾"; head.classList.remove("collapsed"); if (!toggleLeaders._loaded) { fetchLeaderboard('week'); toggleLeaders._loaded = true; } }
    }

    // Поиск по клиенту
    const q = document.getElementById('q');
    const list = document.getElementById('events-list');
    q.addEventListener('input', () => applyFilter());
    function applyFilter() {
      const needle = (q.value || '').trim().toLowerCase();
      const cards = Array.from(list.children);
      cards.forEach(c => {
        const text = c.innerText.toLowerCase();
        const tags = (c.dataset.tags || '').toLowerCase();
        const show = !needle || text.includes(needle) || tags.includes(needle);
        c.style.display = show ? '' : 'none';
      });
    }

    // Профиль / позиции / архив
    async function fetchMe() {
      const cid = getChatId();
      const activeDiv = document.getElementById("active");
      const archDiv = document.getElementById("arch");
      if (!cid) {
        document.getElementById("balance").textContent = "Баланс: —";
        activeDiv.textContent = "Не удалось получить chat_id. Откройте Mini App из чата командой /start.";
        archDiv.textContent = "—";
        return;
      }
      try {
        let url = `/api/me?chat_id=${cid}`;
        if (SIG)  url += `&sig=${SIG}`;
        if (INIT) url += `&init=${encodeURIComponent(INIT)}`;
        const r = await fetch(url);
        const data = await r.json();
        if (!r.ok || !data.success) {
          document.getElementById("balance").textContent = "Баланс: —";
          activeDiv.textContent = "Ошибка загрузки профиля.";
          archDiv.textContent = "Ошибка загрузки архива.";
          return;
        }
        renderBalance(data);
        renderActive(data);
        renderArchive(data);
      } catch (e) {
        document.getElementById("balance").textContent = "Баланс: —";
        activeDiv.textContent = "Сетевая ошибка.";
        archDiv.textContent = "Сетевая ошибка.";
      }
    }
    function renderBalance(data) {
      const bal = data.user && typeof data.user.balance !== 'undefined' ? Number(data.user.balance) : NaN;
      const s = isNaN(bal) ? "—" : bal.toFixed(2);
      document.getElementById("balance").textContent = `Баланс: ${s} кредитов`;
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
          <div class="bet" style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
            <div class="bet-info">
              <div><b>${pos.event_name}</b></div>
              <div class="muted">${pos.option_text}</div>
              <div class="muted">Сторона: ${pos.share_type.toUpperCase()}</div>
              <div>Кол-во: ${qty.toFixed(4)} | Ср. цена: ${avg.toFixed(4)}</div>
              <div class="muted">Тек. цена ДА/НЕТ: ${(+pos.current_yes_price).toFixed(3)} / ${(+pos.current_no_price).toFixed(3)}</div>
            </div>
            <div class="bet-win" style="text-align:right;">
              <div style="font-size:22px;font-weight:900;color:#0b8457;line-height:1.1;">${payout.toFixed(2)} <span style="font-size:12px;font-weight:700;color:#444;">кред.</span></div>
              <div class="muted" style="font-size:12px;">возможная выплата</div>
            </div>
          </div>`;
        div.appendChild(el);
      });
    }
    function renderArchive(data) {
      const div = document.getElementById("arch");
      div.innerHTML = "";
      const items = data.archive || [];
      if (!items.length) {
        div.textContent = "Архив пуст.";
        return;
      }
      items.forEach(it => {
        const win = !!it.is_win;
        const cls = win ? "arch-row arch-win" : "arch-row arch-lose";
        const sign = win ? "+" : "−";
        const amount = Number(it.payout || 0).toFixed(2);
        const when = (it.resolved_at || "").slice(0,16);
        const el = document.createElement("div");
        el.className = cls;
        el.innerHTML = `
          <div>
            <div><b>${it.event_name}</b></div>
            <div class="muted">${it.option_text} • исход: ${it.winner_side.toUpperCase()}</div>
            <div class="muted">${when}</div>
          </div>
          <div style="text-align:right; font-weight:900; ${win?'color:#0b8457':'color:#bd1a1a'}">${sign}${amount}</div>
        `;
        div.appendChild(el);
      });
    }

    // Лидеры
    async function fetchLeaderboard(period='week') {
      const lb = document.getElementById("lb-container");
      try {
        let url = "/api/leaderboard?period=" + encodeURIComponent(period);
        const cid = getChatId();
        if (cid)  url += `&viewer=${cid}`;
        if (INIT) url += `&init=${encodeURIComponent(INIT)}`;
        const r = await fetch(url);
        const data = await r.json();
        if (!r.ok || !data.success) { lb.textContent = "Ошибка загрузки рейтинга."; return; }
        document.getElementById("lb-range").textContent = data.week.label;
        const items = data.items || [];
        lb.innerHTML = items.length ? "" : "Пока нет данных.";
        items.slice(0, 50).forEach((it, i) => {
          const row = document.createElement("div");
          row.className = "lb-row";
          const avaUrl = `/api/userpic?chat_id=${it.chat_id}`;
          row.innerHTML = `
            <div style="text-align:center;font-weight:800;color:#444;">${i+1}</div>
            <div style="position:relative; width:32px; height:32px;">
              <div style="position:absolute; inset:0; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:12px; font-weight:800; color:#fff; background:linear-gradient(135deg,#6a5acd,#00bcd4)" data-ph>${(it.login||'U').slice(0,2).toUpperCase()}</div>
              <img src="${avaUrl}" style="width:32px;height:32px;border-radius:50%;object-fit:cover;display:none" onload="this.style.display='block'; this.previousElementSibling.style.display='none';" onerror="this.style.display='none'; this.previousElementSibling.style.display='flex';">
            </div>
            <div style="font-weight:600;color:#111;">${it.login || '—'}</div>
            <div style="font-weight:900;font-size:16px;color:#0b8457;">${(+it.earned).toFixed(2)}</div>`;
          lb.appendChild(row);
        });
      } catch(e) { lb.textContent = "Сетевая ошибка."; }
    }

    // Покупка
    let buyCtx = null;
    function openBuy(event_uuid, option_index, side, optText) {
      const cid = getChatId();
      if (!cid) { alert("Не удалось получить chat_id. Откройте Mini App из чата командой /start."); return; }
      buyCtx = { event_uuid, option_index, side, chat_id: cid, optText };
      document.getElementById("mTitle").textContent = `Покупка: ${side.toUpperCase()} · ${optText}`;
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
        if (SIG)  body.sig = SIG;
        if (INIT) body.init = INIT;
        const r = await fetch("/api/market/buy", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify(body) });
        const data = await r.json();
        if (!r.ok || !data.success) { alert("Ошибка: " + (data.error || r.statusText)); return; }
        const card = document.querySelector(`[data-event='${buyCtx.event_uuid}'] [data-option='${buyCtx.option_index}']`);
        if (card) { const probEl = card.querySelector(".prob"); if (probEl) probEl.textContent = `${Math.round(data.market.yes_price * 100)}%`; }
        await fetchMe();
        if (document.getElementById("section-leaders").style.display !== "none") fetchLeaderboard('week');
        closeBuy();
        if (tg && tg.showPopup) tg.showPopup({ title: "Успешно", message: `Куплено ${data.trade.got_shares.toFixed(4)} акций (${buyCtx.side.toUpperCase()})` });
        else alert("Успех: куплено " + data.trade.got_shares.toFixed(4) + " акций");
      } catch (e) { console.error(e); alert("Сетевая ошибка"); }
    }

    document.addEventListener('click', (ev) => {
      const btn = ev.target.closest('.buy-btn');
      if (!btn) return;
      const event_uuid = btn.dataset.event;
      const option_index = parseInt(btn.dataset.index, 10);
      const side = btn.dataset.side;
      const optText = btn.dataset.text || '';
      openBuy(event_uuid, option_index, side, optText);
    });

    (function init(){
      if (tg) tg.ready();
      fetchMe();
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


# ---------- Rate limiting ----------
RL_USER_WINDOW = 10   # сек
RL_USER_LIMIT  = 5    # запросов за окно
RL_IP_WINDOW   = 60   # сек
RL_IP_LIMIT    = 30   # запросов за окно
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


# ---------- Mini App endpoints ----------
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
    archive = db.get_user_archive(chat_id)  # новый
    return jsonify(success=True, user={"chat_id": chat_id, "balance": u.get("balance", 0)}, positions=positions, archive=archive)


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
        if side not in ("yes","no"):
            raise ValueError
        if not (0 < amount <= 1_000_000):
            raise ValueError
    except Exception:
        return jsonify(success=False, error="bad_payload"), 400

    result, err2 = db.trade_buy(
        chat_id=chat_id, event_uuid=event_uuid, option_index=option_index, side=side, amount=amount
    )
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


@app.get("/api/leaderboard")
def api_leaderboard():
    period = "week"
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
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:16px;max-width:1200px}
h1{margin:0 0 12px}
.section{margin:18px 0}
.item{border:1px solid #ddd;border-radius:10px;margin:6px 0;background:#fff}
.item-head{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;cursor:pointer;position:relative;overflow:hidden;background:#f7f7f7;border-radius:10px}
.item-body{display:none;border-top:1px dashed #ddd;padding:10px 12px}
.badge{padding:2px 8px;border-radius:999px;font-size:12px;background:#eef}
.row{margin:6px 0}
.btn{padding:6px 10px;border:0;border-radius:8px;cursor:pointer}
.approve{background:#2e7d32;color:#fff}
.reject{background:#999;color:#fff}
.ban{background:#c62828;color:#fff}
.unban{background:#6a5acd;color:#fff}
.save{background:#1976d2;color:#fff}
small{color:#666}
.list{display:flex;flex-direction:column;gap:6px}
input[type=text],input[type=number],input[type=datetime-local],textarea,select{width:100%;padding:8px;border:1px solid #ddd;border-radius:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
table{width:100%;border-collapse:collapse;background:#fff}
td,th{padding:6px;border-bottom:1px solid #eee;text-align:left;font-size:14px}
th{font-weight:700;color:#333}
.muted{color:#666}
.pill{padding:2px 8px;border-radius:999px;background:#f0f0f0}
</style>
<script>
function toggleBody(id){const e=document.getElementById(id); e.style.display = e.style.display==='none'||!e.style.display? 'block':'none';}
async function adminPost(url, data){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data||{})});
  const txt = await r.text();
  let j=null; try{ j=JSON.parse(txt);}catch(e){ alert('Ошибка: '+txt); return null;}
  if(!r.ok || !j.success){ alert('Ошибка: ' + (j.error || txt)); return null; }
  return j;
}
async function resolveMarket(mid, winner){ const ok = await adminPost('/admin/resolve_market/' + mid, {winner}); if(ok){ alert('Рынок закрыт: ' + winner.toUpperCase()); location.reload(); } }
async function resolveEvent(evu){
  const form = document.querySelector('[data-resolve-ev="'+evu+'"]');
  const radios = form.querySelectorAll('input[type=radio]:checked');
  const map = {}; radios.forEach(r => { map[r.name.replace('opt_','')] = r.value; });
  const need = parseInt(form.dataset.count,10);
  if (Object.keys(map).length !== need){ alert('Отметьте исход по каждой опции'); return; }
  const ok = await adminPost('/admin/resolve_event/'+evu, {winners: map});
  if (ok) { alert('Событие закрыто'); location.reload(); }
}
function onDoubleOutcomeToggle(cb){
  const ta = document.getElementById('optArea');
  ta.disabled = cb.checked;
  if (cb.checked){ ta.value = "ДА\\nНЕТ"; }
}
</script>
</head>
<body>
<h1>Админская панель</h1>

<!-- Создать событие -->
<div class="section item">
  <div class="item-head" onclick="toggleBody('createEvent')"><b>Создать событие</b><span class="pill">новое</span></div>
  <div class="item-body" id="createEvent" style="display:none">
    <form method="post" action="/admin/events/create" enctype="application/x-www-form-urlencoded" class="grid">
      <div><label>Название</label><input type="text" name="name" required></div>
      <div><label>Дата окончания</label><input type="datetime-local" name="end_date" required></div>
      <div style="grid-column:1/3"><label>Описание</label><textarea name="description" rows="3" required></textarea></div>
      <div style="grid-column:1/3"><label>Опции (по одной в строке)</label><textarea id="optArea" name="options" rows="4" placeholder="Вариант 1&#10;Вариант 2" required></textarea></div>
      <div><label>Теги (через запятую)</label><input type="text" name="tags" placeholder="спорт, выборы"></div>
      <div><label><input type="checkbox" name="double_outcome" value="1" onchange="onDoubleOutcomeToggle(this)"> Двойной исход (ДА/НЕТ)</label></div>
      <div><label>Публиковать сразу</label><input type="checkbox" name="is_published" value="1" checked></div>
      <div style="grid-column:1/3"><button class="btn save" type="submit">Создать</button></div>
    </form>
  </div>
</div>

<!-- Фильтр пользователей -->
<div class="section item">
  <div class="item-head" onclick="toggleBody('filterUsers')"><b>Фильтр пользователей</b></div>
  <div class="item-body" id="filterUsers" style="display:block">
    <form method="get" action="/admin" class="grid">
      <div><label>Поиск (логин/ID)</label><input type="text" name="q" value="{{ q or '' }}"></div>
      <div>
        <label>Сортировка</label>
        <select name="sort">
          <option value="">—</option>
          <option value="created_at" {% if sort=='created_at' %}selected{% endif %}>Дата заявки</option>
          <option value="balance" {% if sort=='balance' %}selected{% endif %}>Баланс</option>
        </select>
      </div>
      <div style="grid-column:1/3"><button class="btn save" type="submit">Применить</button></div>
    </form>
  </div>
</div>

<!-- Заявки -->
<div class="section">
  <h2>Заявки ({{ pending|length }})</h2>
  <div class="list">
  {% for u in pending %}
    <div class="item">
      <div class="item-head" onclick="toggleBody('p{{u.chat_id}}')">
        <div>#{{ loop.index }} • <b>{{ u.login }}</b> <small>(ID {{u.chat_id}})</small></div>
        <div><span class="badge">Заявка от: {{ (u.created_at or '')[:16] }}</span></div>
      </div>
      <div class="item-body" id="p{{u.chat_id}}">
        <div class="row">
          <button class="btn approve" onclick="adminPost('/admin/approve/{{u.chat_id}}').then(()=>location.reload())">Одобрить</button>
          <button class="btn reject"  onclick="adminPost('/admin/reject/{{u.chat_id}}').then(()=>location.reload())">Отклонить</button>
        </div>
      </div>
    </div>
  {% endfor %}
  </div>
</div>

<!-- Одобренные -->
<div class="section">
  <h2>Одобренные ({{ approved|length }})</h2>
  <div class="list">
  {% for u in approved %}
    <div class="item">
      <div class="item-head" onclick="toggleBody('a{{u.chat_id}}')">
        <div>#{{ loop.index }} • <b>{{ u.login }}</b> <small>(ID {{u.chat_id}})</small></div>
        <div><span class="badge">Баланс: {{u.balance}}</span></div>
      </div>
      <div class="item-body" id="a{{u.chat_id}}">
        <div class="row">Изменить баланс:
          <input type="number" id="b{{u.chat_id}}" value="{{u.balance}}" style="width:160px;">
          <button class="btn save" onclick="
            adminPost('/admin/update_balance/{{u.chat_id}}',{balance: parseFloat(document.getElementById('b{{u.chat_id}}').value||0)})
            .then(()=>location.reload())
          ">Сохранить</button>
        </div>
        <div class="row">
          <button class="btn ban" onclick="adminPost('/admin/ban/{{u.chat_id}}').then(()=>location.reload())">Бан</button>
        </div>
        <!-- История баланса -->
        <div class="row">
          <div class="muted">История изменений (последние записи):</div>
          <table>
            <thead><tr><th>Когда</th><th>Δ</th><th>Причина</th><th>Order</th></tr></thead>
            <tbody>
              {% for l in u.ledger %}
                <tr><td>{{ (l.created_at or '')[:16] }}</td><td>{{ l.delta }}</td><td>{{ l.reason }}</td><td>{{ l.order_id or '—' }}</td></tr>
              {% endfor %}
              {% if not u.ledger %}<tr><td colspan="4" class="muted">Нет записей</td></tr>{% endif %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  {% endfor %}
  </div>
</div>

<!-- Закрытие событий (групповое) -->
<div class="section">
  <h2>Закрытие событий</h2>
  <div class="list">
  {% for ev in resolvable %}
    <div class="item">
      <div class="item-head" onclick="toggleBody('ev{{ev.event_uuid}}')">
        <div><b>{{ ev.name }}</b> <small class="muted">до {{ ev.end_short }}</small></div>
        <div><span class="badge">Нерешённых опций: {{ ev.open_count }}</span></div>
      </div>
      <div class="item-body" id="ev{{ev.event_uuid}}">
        <form data-resolve-ev="{{ ev.event_uuid }}" data-count="{{ ev.open_count }}">
          <table>
            <thead><tr><th>Опция</th><th>Выбор</th></tr></thead>
            <tbody>
              {% for row in ev.rows %}
                <tr>
                  <td>{{ row.option_text }}</td>
                  <td>
                    <label><input type="radio" name="opt_{{row.option_index}}" value="yes"> YES</label>
                    <label style="margin-left:12px"><input type="radio" name="opt_{{row.option_index}}" value="no"> NO</label>
                  </td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
          <div class="row"><button class="btn save" type="button" onclick="resolveEvent('{{ ev.event_uuid }}')">Закрыть событие</button></div>
        </form>
      </div>
    </div>
  {% endfor %}
  {% if not resolvable %}
    <div class="muted">Нет событий, доступных к закрытию сейчас.</div>
  {% endif %}
  </div>
</div>

</body>
</html>
"""

@app.get("/admin")
@requires_auth
def admin_panel():
    q = (request.args.get("q") or "").strip()
    sort = (request.args.get("sort") or "").strip()

    pending = db.search_users(status="pending", q=q, sort=sort)
    approved = db.search_users(status="approved", q=q, sort=sort)
    banned = db.search_users(status="banned", q=q, sort=sort)

    for u in approved:
        u["ledger"] = db.get_ledger_for_user(u["chat_id"], limit=10) or []

    # События, у которых прошёл дедлайн и есть нерешённые опции
    resolvable = db.get_events_for_group_resolve()

    return render_template_string(ADMIN_HTML,
                                  pending=pending, approved=approved, banned=banned,
                                  q=q, sort=sort, resolvable=resolvable)


# ---- Admin actions ----
@app.post("/admin/approve/<int:chat_id>")
@requires_auth
def admin_approve(chat_id: int):
    user = db.approve_user(chat_id)
    if user:
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
    ok = db.reject_user(chat_id)
    return jsonify(success=bool(ok))


@app.post("/admin/update_balance/<int:chat_id>")
@requires_auth
def admin_update_balance(chat_id: int):
    payload = request.get_json(silent=True) or request.form or {}
    new_balance = payload.get("balance")
    try:
        new_balance = float(new_balance)
        if new_balance < 0:
            raise ValueError
    except Exception:
        return jsonify(success=False, error="bad_balance"), 400
    user = db.admin_set_balance_via_ledger(chat_id, new_balance)
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


@app.post("/admin/resolve_market/<int:market_id>")
@requires_auth
def admin_resolve_market(market_id: int):
    # Остался для совместимости (двойной исход)
    payload = request.get_json(silent=True) or {}
    winner = (payload.get("winner") or "").lower()
    if winner not in ("yes", "no"):
        return jsonify(success=False, error="bad_winner"), 400
    ok_time, err_time = db.market_can_resolve(market_id)
    if not ok_time:
        return jsonify(success=False, error=err_time or "too_early"), 400
    res, err = db.resolve_market_by_id(market_id, winner)
    if err:
        return jsonify(success=False, error=err), 400
    return jsonify(success=True, summary=res)


@app.post("/admin/resolve_event/<event_uuid>")
@requires_auth
def admin_resolve_event(event_uuid: str):
    payload = request.get_json(silent=True) or {}
    winners = payload.get("winners") or {}
    if not isinstance(winners, dict) or not winners:
        return jsonify(success=False, error="bad_winners"), 400
    ok, err = db.resolve_event_by_winners(event_uuid, winners)
    if not ok:
        return jsonify(success=False, error=err or "resolve_failed"), 400
    return jsonify(success=True)


@app.post("/admin/events/create")
@requires_auth
def admin_create_event():
    f = request.form
    name = (f.get("name") or "").strip()
    description = (f.get("description") or "").strip()
    options_raw = (f.get("options") or "").strip()
    end_date = (f.get("end_date") or "").strip()
    tags_raw = (f.get("tags") or "").strip()
    is_published = bool(f.get("is_published"))
    double_outcome = bool(f.get("double_outcome"))

    if not (name and description and end_date):
        return Response("Bad request", status=400)

    if double_outcome:
        options = [{"text": "ДА"}, {"text": "НЕТ"}]
    else:
        options = []
        for line in options_raw.splitlines():
            t = line.strip()
            if t:
                options.append({"text": t})
        if not options:
            return Response("No options", status=400)

    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []

    ok, err = db.create_event_with_markets(
        name=name,
        description=description,
        options=options,
        end_date=end_date,
        tags=tags,
        publish=is_published,
        creator_id=int(ADMIN_ID) if ADMIN_ID and str(ADMIN_ID).isdigit() else None,
        double_outcome=double_outcome
    )
    if not ok:
        return Response("Create error: " + (err or ""), status=400)
    return Response("<script>location.href='/admin';</script>", mimetype="text/html")


# ---------- Buy API uses rate limiting above ----------
# (уже реализовано в api_market_buy)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
