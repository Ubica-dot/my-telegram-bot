import os
import json
import hmac
import hashlib
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from functools import wraps
from collections import deque, defaultdict

import requests
from flask import Flask, request, render_template_string, jsonify, Response, stream_with_context

from database import db

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
  <title>U — мини‑приложение</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    :root{ --spot-x:50%; --spot-y:50%; }
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 12px; }
    .row { display:flex; align-items:center; gap:12px; }
    .muted { color:#666; }
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
    .meta-left  { grid-column: 1; font-size: 12px; color: #666; margin-top: 6px; }
    .meta-right { grid-column: 3; justify-self: end; font-size: 12px; color: #666; margin-top: 6px; }
    .section { margin: 18px 0; }
    .section-head { display:flex; align-items:center; justify-content:space-between; padding:10px 12px;
                    background:#f5f5f5; border-radius:10px; cursor:pointer; user-select:none; position: relative; overflow:hidden; }
    .section-title { font-weight:700; }
    .caret { transition: transform .15s ease; }
    .collapsed .caret { transform: rotate(-90deg); }
    .section-body { padding:10px 0 0 0; }
    .prob { color:#000; font-weight:800; font-size:18px; display:flex; align-items:center; justify-content:flex-end; padding: 0 4px; }
    .hover-spot::after{
      content:""; position:absolute; inset:0; pointer-events:none;
      background: radial-gradient(200px circle at var(--spot-x) var(--spot-y), rgba(0,0,0,.06), transparent 60%);
      opacity:0; transition: opacity .15s ease;
    }
    .hover-spot:hover::after{ opacity:1; }

    /* top header */
    .header { display:flex; align-items:center; gap:12px; }
    .avatar { width: 56px; height: 56px; border-radius: 50%; border: 2px solid #eee; box-shadow: 0 2px 8px rgba(0,0,0,.06); object-fit:cover; }
    .avatarPH { width:56px;height:56px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:20px;color:#fff;
                background: radial-gradient(circle at 30% 30%, #6a5acd, #00bcd4); text-transform: uppercase; }
    .usr { display:flex; flex-direction:column; gap:2px; }
    .uname { font-weight:900; font-size:18px; }
    .ubal { font-weight:900; font-size:14px; color:#0b8457; }

    input[type=text] { width:100%; padding:8px; border:1px solid #ddd; border-radius:8px; }

    /* leaders slider */
    .seg { display:inline-flex; background:#f0f0f0; border-radius:999px; padding:4px; gap:4px; }
    .seg-btn { border:0; background:transparent; padding:6px 12px; border-radius:999px; font-weight:700; cursor:pointer; color:#444; transition:all .15s ease; }
    .seg-btn.active { background:#fff; box-shadow:0 1px 4px rgba(0,0,0,.08); color:#111; }

    /* footer link */
    .footer { margin: 40px 6px 10px; text-align:center; }
    .footer a { color:#9aa; text-decoration:none; font-size:12px; opacity:.6; }
    .footer a:hover { opacity:.9; }
  </style>
</head>
<body>

  <!-- Header with avatar, username, balance -->
  <div class="header">
    <div id="avaPH" class="avatarPH">U</div>
    <img id="avaIMG" class="avatar" alt="avatar" style="display:none">
    <div class="usr">
      <div id="uname" class="uname">—</div>
      <div id="ubal" class="ubal">Баланс: —</div>
    </div>
  </div>

  <!-- Активные ставки -->
  <div id="wrap-active" class="section">
    <div id="head-active" class="section-head hover-spot" onclick="toggleSection('active')">
      <span class="section-title">Активные ставки</span>
      <span id="caret-active" class="caret">▾</span>
    </div>
    <div id="section-active" class="section-body">
      <div style="margin:6px 0 8px"><input id="qActive" type="text" placeholder="Поиск по активным ставкам"></div>
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
      <div style="margin:6px 0 8px"><input id="qArch" type="text" placeholder="Поиск по архиву"></div>
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
      <div style="margin:6px 0 8px"><input id="qEvents" type="text" placeholder="Поиск по событиям и тегам"></div>
      <div id="events-list"></div>
    </div>
  </div>

  <!-- Таблица лидеров -->
  <div id="wrap-leaders" class="section">
    <div id="head-leaders" class="section-head hover-spot" onclick="toggleLeaders()">
      <span class="section-title">Таблица лидеров</span>
      <span id="caret-leaders" class="caret">▸</span>
    </div>
    <div id="section-leaders" class="section-body" style="display:none;">
      <div style="display:flex; justify-content:center; margin:8px 0;">
        <div class="seg" id="seg">
          <button class="seg-btn active" data-period="week">Неделя</button>
          <button class="seg-btn" data-period="month">Месяц</button>
        </div>
      </div>
      <div id="lb-range" class="muted" style="margin:6px 0 10px;text-align:center;">—</div>
      <div id="lb-container" class="muted">Загрузка…</div>
    </div>
  </div>

  <!-- Footer -->
  <div class="footer">
    <a href="/legal" target="_blank" rel="noopener">Правила и политика конфиденциальности</a>
  </div>

  <!-- Покупка -->
  <div id="modalBg" class="modal-bg" style="position: fixed; inset: 0; background: rgba(0,0,0,.5); display:none; align-items:center; justify-content:center;">
    <div class="modal" style="background:#fff; border-radius:12px; padding:16px; width:90%; max-width:400px;">
      <h3 id="mTitle" style="margin:0 0 8px 0;">Покупка</h3>
      <div class="muted" id="mSub">Укажите сумму, не выше вашего баланса.</div>
      <div class="row" style="margin-top:10px;">
        <label>Сумма (кредиты):&nbsp;</label>
        <input type="number" id="mAmount" min="1" step="1" value="100"/>
      </div>
      <div class="row" style="margin-top:12px; gap:8px;">
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

    // radial hover
    document.addEventListener('pointermove', (e) => {
      const head = e.target.closest('.hover-spot');
      if (!head) return;
      const rect = head.getBoundingClientRect();
      head.style.setProperty('--spot-x', (e.clientX - rect.left) + 'px');
      head.style.setProperty('--spot-y', (e.clientY - rect.top) + 'px');
    });

    // sections
    function toggleSection(name){
      const b = document.getElementById("section-"+name);
      const c = document.getElementById("caret-"+name);
      const h = document.getElementById("head-"+name);
      const key = "collapse_"+name;
      const shown = b.style.display !== "none";
      if (shown){ b.style.display="none"; c.textContent="▸"; h.classList.add("collapsed"); try{localStorage.setItem(key,"1");}catch(e){} }
      else { b.style.display="block"; c.textContent="▾"; h.classList.remove("collapsed"); try{localStorage.setItem(key,"0");}catch(e){} }
    }
    function toggleLeaders(){
      const b = document.getElementById("section-leaders");
      const c = document.getElementById("caret-leaders");
      const h = document.getElementById("head-leaders");
      const shown = b.style.display !== "none";
      if (shown){ b.style.display="none"; c.textContent="▸"; h.classList.add("collapsed"); }
      else { b.style.display="block"; c.textContent="▾"; h.classList.remove("collapsed"); if (!toggleLeaders._loaded){ fetchLeaderboard('week'); toggleLeaders._loaded=true; } }
    }

    // Header avatar/name/balance
    function fillHeader(login, balance){
      const ph = document.getElementById('avaPH');
      const img= document.getElementById('avaIMG');
      const uname = document.getElementById('uname');
      const ubal = document.getElementById('ubal');

      // username
      const tgUser = tg && tg.initDataUnsafe ? tg.initDataUnsafe.user : null;
      const nick = (login || (tgUser && (tgUser.username || tgUser.first_name)) || "Профиль");
      uname.textContent = nick;

      // balance to .00
      const balText = (typeof balance === 'number' && !isNaN(balance)) ? balance.toFixed(2) : "—";
      ubal.textContent = "Баланс: " + balText + " кредитов";

      // avatar
      function tryImg(src){
        if (!src) return false;
        img.onload = ()=>{ img.style.display='block'; ph.style.display='none'; }
        img.onerror= ()=>{ img.style.display='none'; ph.style.display='flex'; }
        img.src = src; return true;
      }
      // initials
      let initials = "";
      if (tgUser){
        if (tgUser.first_name) initials += tgUser.first_name[0];
        if (tgUser.last_name)  initials += tgUser.last_name[0];
        if (!initials && tgUser.username) initials = tgUser.username.slice(0,2);
      } else if (login) {
        initials = String(login).slice(0,2);
      }
      ph.textContent = (initials || "U").toUpperCase();

      if (tgUser && tgUser.photo_url) { if (tryImg(tgUser.photo_url)) return; }
      const cid = getChatId();
      if (cid){
        let url = `/api/userpic?chat_id=${cid}`;
        if (SIG) url += `&sig=${SIG}`;
        if (INIT) url += `&init=${encodeURIComponent(INIT)}`;
        tryImg(url);
      }
    }

    // Filters
    function attachFilters(){
      const qA = document.getElementById('qActive');
      const qR = document.getElementById('qArch');
      const qE = document.getElementById('qEvents');
      if (qA){
        qA.addEventListener('input', ()=>{
          const needle = (qA.value||"").toLowerCase();
          const cards = document.getElementById('active').querySelectorAll('.card');
          cards.forEach(c=>{
            const show = !needle || c.innerText.toLowerCase().includes(needle);
            c.style.display = show ? '' : 'none';
          });
        });
      }
      if (qR){
        qR.addEventListener('input', ()=>{
          const needle = (qR.value||"").toLowerCase();
          const rows = document.getElementById('arch').children;
          [...rows].forEach(c=>{
            const show = !needle || c.innerText.toLowerCase().includes(needle);
            c.style.display = show ? '' : 'none';
          });
        });
      }
      if (qE){
        qE.addEventListener('input', ()=>{
          const needle = (qE.value||"").toLowerCase();
          const cards = document.getElementById('events-list').children;
          [...cards].forEach(c=>{
            const tags = (c.dataset.tags || '').toLowerCase();
            const show = !needle || c.innerText.toLowerCase().includes(needle) || tags.includes(needle);
            c.style.display = show ? '' : 'none';
          });
        });
      }
    }

    // Render helpers
    function renderEvents(events){
      const list = document.getElementById('events-list');
      list.innerHTML = "";
      events.forEach(e=>{
        const card = document.createElement('div');
        card.className = 'card';
        card.dataset.event = e.event_uuid;
        card.dataset.tags = (e.tags||[]).join(',');
        card.dataset.end = e.end_ts || 0;
        card.dataset.vol = e.total_volume || 0;
        const tagsLine = (e.tags && e.tags.length) ? `<div class="muted">Теги: ${e.tags.join(', ')}</div>` : '';
        let optsHtml = '';
        (e.options||[]).forEach((opt, idx)=>{
          const md = (e.markets && e.markets[idx]) || {yes_price:0.5, volume:0, end_short:e.end_short, resolved:false, winner_side:null};
          const yes_pct = Math.round((md.yes_price||0.5)*100);
          const no_pct  = 100-yes_pct;
          optsHtml += `
            <div class="opt" data-option="${idx}">
              <div class="opt-title">${opt.text}</div>
              <div class="prob" title="Вероятность ДА">${md.resolved ? ((md.winner_side==='yes')?'YES ✓':'NO ✓') : (yes_pct+'%')}</div>
              <div class="actions">
                ${md.resolved ? `<button class="btn yes" disabled>Закрыт</button>` : `
                <button class="btn yes buy-btn" data-event="${e.event_uuid}" data-index="${idx}" data-side="yes" data-text="${opt.text}">ДА</button>
                <button class="btn no  buy-btn" data-event="${e.event_uuid}" data-index="${idx}" data-side="no"  data-text="${opt.text}">НЕТ</button>`}
              </div>
              <div class="meta-left">
                ${ (md.volume||0) >= 1000 ? Math.floor((md.volume||0)/1000)+' тыс. кредитов' : (md.volume||0)+' кредитов' }
              </div>
              <div class="meta-right">До ${md.end_short}</div>
            </div>`;
        });
        card.innerHTML = `<div><b>${e.name}</b></div><p class="muted">${e.description}</p>${tagsLine}${optsHtml}`;
        list.appendChild(card);
      });
    }

    // Fetch profile (positions + archive)
    async function fetchMe(){
      const cid = getChatId();
      const activeDiv = document.getElementById("active");
      const archDiv = document.getElementById("arch");
      if (!cid){
        document.getElementById("ubal").textContent = "Баланс: —";
        document.getElementById("uname").textContent = "Профиль";
        activeDiv.textContent = "Откройте Mini App из бота (/start).";
        archDiv.textContent = "—";
        return;
      }
      try{
        let url = `/api/me?chat_id=${cid}`;
        if (SIG)  url += `&sig=${SIG}`;
        if (INIT) url += `&init=${encodeURIComponent(INIT)}`;
        const r = await fetch(url);
        const data = await r.json();
        if (!r.ok || !data.success){ activeDiv.textContent = "Ошибка загрузки."; archDiv.textContent="Ошибка."; return; }

        fillHeader(data.user.login || "", Number(data.user.balance));
        // Active
        activeDiv.innerHTML = "";
        const pos = data.positions||[];
        if (!pos.length){ activeDiv.innerHTML = `<div class="muted">У вас пока нет активных ставок.</div>`; }
        else{
          pos.forEach(p=>{
            const qty = +p.quantity, avg=+p.average_price;
            const el = document.createElement('div');
            el.className = 'card';
            el.innerHTML = `
              <div class="row" style="justify-content:space-between;">
                <div>
                  <div><b>${p.event_name}</b></div>
                  <div class="muted">${p.option_text}</div>
                  <div class="muted">Сторона: ${p.share_type.toUpperCase()}</div>
                  <div>Кол-во: ${qty.toFixed(4)} | Ср. цена: ${avg.toFixed(4)}</div>
                  <div class="muted">Тек. цена ДА/НЕТ: ${(+p.current_yes_price).toFixed(3)} / ${(+p.current_no_price).toFixed(3)}</div>
                </div>
                <div style="text-align:right;">
                  <div style="font-size:22px;font-weight:900;color:#0b8457">${qty.toFixed(2)} <span class="muted" style="font-size:12px">кред.</span></div>
                  <div class="muted" style="font-size:12px;">возможная выплата</div>
                </div>
              </div>`;
            activeDiv.appendChild(el);
          });
        }

        // Archive
        archDiv.innerHTML = "";
        const arr = data.archive||[];
        if (!arr.length){ archDiv.innerHTML = `<div class="muted">Архив пуст.</div>`; }
        else{
          arr.forEach(it=>{
            const win = !!it.is_win;
            const sign = win ? "+" : "−";
            const amount = Number(it.payout||0).toFixed(2);
            const when = (it.resolved_at||"").slice(0,16);
            const el = document.createElement('div');
            el.className = 'card';
            el.style.background = win ? "#eaf7ef" : "#fdecec";
            el.innerHTML = `
              <div class="row" style="justify-content:space-between;">
                <div>
                  <div><b>${it.event_name || '—'}</b></div>
                  <div class="muted">${(it.option_text||'—')} • исход: ${(it.winner_side||'—').toUpperCase()}</div>
                  <div class="muted">${when}</div>
                </div>
                <div style="text-align:right;font-weight:900;${win?'color:#0b8457':'color:#bd1a1a'}">${sign}${amount}</div>
              </div>`;
            archDiv.appendChild(el);
          });
        }

      }catch(e){
        document.getElementById("active").textContent = "Сетевая ошибка.";
        document.getElementById("arch").textContent = "Сетевая ошибка.";
      }
    }

    // Fetch events (server renders list on first load; we re-render client side from injected data)
    // Сервер подставляет events в шаблон — используем их для первичного рендера
    const __events = {{ events|tojson|safe }};
    renderEvents(__events);

    // Filters after initial render
    attachFilters();

    // Leaders
    document.getElementById('seg').addEventListener('click', (e)=>{
      const b = e.target.closest('.seg-btn'); if (!b) return;
      document.querySelectorAll('#seg .seg-btn').forEach(x=>x.classList.toggle('active', x===b));
      fetchLeaderboard(b.dataset.period||'week');
    });

    async function fetchLeaderboard(period='week'){
      const lb = document.getElementById("lb-container");
      const rng= document.getElementById("lb-range");
      try{
        let url = "/api/leaderboard?period=" + encodeURIComponent(period);
        const r = await fetch(url);
        const data = await r.json();
        if (!r.ok || !data.success){ lb.textContent = "Ошибка загрузки рейтинга."; return; }
        rng.textContent = data.week.label;
        const items = data.items||[];
        lb.innerHTML = items.length ? "" : "Пока нет данных.";
        items.slice(0,50).forEach((it,i)=>{
          const row = document.createElement('div');
          row.style.display='grid'; row.style.gridTemplateColumns='32px 40px 1fr auto'; row.style.alignItems='center'; row.style.gap='8px'; row.style.padding='6px 0'; row.style.borderBottom='1px solid #eee';
          row.innerHTML = `
            <div style="text-align:center;font-weight:800;color:#444;">${i+1}</div>
            <div style="position:relative;width:32px;height:32px;">
              <div style="position:absolute;inset:0;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#fff;background:linear-gradient(135deg,#6a5acd,#00bcd4)">${(it.login||'U').slice(0,2).toUpperCase()}</div>
            </div>
            <div style="font-weight:600;color:#111;">${it.login||'—'}</div>
            <div style="font-weight:900;font-size:16px;color:#0b8457;">${Number(it.earned||0).toFixed(2)}</div>`;
          lb.appendChild(row);
        });
      }catch(e){
        lb.textContent = "Сетевая ошибка.";
      }
    }

    // Buying
    let buyCtx=null;
    document.addEventListener('click',(ev)=>{
      const btn = ev.target.closest('.buy-btn'); if(!btn) return;
      buyCtx = { event_uuid:btn.dataset.event, option_index:parseInt(btn.dataset.index,10), side:btn.dataset.side, chat_id:getChatId(), text:btn.dataset.text };
      document.getElementById('mTitle').textContent = `Покупка: ${buyCtx.side.toUpperCase()} · ${buyCtx.text}`;
      document.getElementById('mAmount').value="100";
      document.getElementById('modalBg').style.display='flex';
      if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("light");
    });
    function closeBuy(){ document.getElementById('modalBg').style.display='none'; buyCtx=null; }
    async function confirmBuy(){
      if (!buyCtx) return;
      const amount = parseFloat(document.getElementById("mAmount").value||"0");
      if (!(amount>0)){ alert("Введите положительную сумму"); return; }
      try{
        const body = { chat_id:+buyCtx.chat_id, event_uuid:buyCtx.event_uuid, option_index:buyCtx.option_index, side:buyCtx.side, amount };
        if (SIG) body.sig = SIG; if (INIT) body.init = INIT;
        const r = await fetch("/api/market/buy",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
        const data = await r.json();
        if (!r.ok || !data.success){ alert("Ошибка: "+(data.error||r.statusText)); return; }
        await fetchMe();
        closeBuy();
      }catch(e){ alert("Сетевая ошибка"); }
    }
    window.confirmBuy = confirmBuy; window.closeBuy = closeBuy;

    (async function init(){
      if (tg) tg.ready();
      await fetchMe();
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
        s = (end_iso or "")[:10]
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            y, m, d = s.split("-")
            return f"{d}.{m}.{y[2:]}"
        return (end_iso or "")[:10]


# ---------- Rate limiting ----------
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
    # вернём login, чтобы в шапке отрисовать ник
    return jsonify(success=True, user={"chat_id": chat_id, "balance": float(u.get("balance", 0)), "login": u.get("login")}, positions=positions, archive=archive)


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
    period = (request.args.get("period") or "week").lower()
    if period == "month":
        bounds = db.month_current_bounds()
        items = db.get_leaderboard_month(bounds["start"], limit=50)
        return jsonify(success=True, week=bounds, items=items)
    else:
        bounds = db.week_current_bounds()
        items = db.get_leaderboard_week(bounds["start"], limit=50)
        return jsonify(success=True, week=bounds, items=items)
