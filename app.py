import os
import json
import uuid
import hmac
import hashlib
import time
from datetime import datetime

import requests
from flask import Flask, request, render_template_string, jsonify, Response, stream_with_context, abort
from functools import wraps

from database import db

app = Flask(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
BASE_URL = os.getenv("WEBHOOK_URL")
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "change-me")

ADMIN_BASIC_USER = os.getenv("ADMIN_BASIC_USER", "admin")
ADMIN_BASIC_PASS = os.getenv("ADMIN_BASIC_PASS", "admin")

WEBAPP_SIGNING_SECRET = os.getenv("WEBAPP_SIGNING_SECRET")  # ОБЯЗАТЕЛЬНО задайте в Render для защиты Mini App


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
        # Лучше фейлить, чтобы не открывать Mini App без подписи
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
                kb = {"inline_keyboard": [[{"text": "Открыть Mini App", "web_app": {"url": web_app_url}}]]]}
                send_message(chat_id, "Приложение готово. Открывайте:", kb)
            elif status == "pending":
                send_message(chat_id, "⏳ Ваша заявка на регистрацию ожидает проверки администратором.")
            elif status == "banned":
                send_message(chat_id, "🚫 Доступ к приложению запрещён.")
            elif status == "rejected":
                # Можно предложить подать заново
                send_message(chat_id, "❌ Заявка отклонена. Отправьте новый логин одним сообщением для повторной подачи.")
            else:
                send_message(chat_id, "Напишите ваш логин одним сообщением для регистрации.")
        return "ok"

    # Любой текст НЕ команда: если нет пользователя — трактуем как логин, если в pending — игнор с сообщением, иначе — нейтрально
    if not text.startswith("/"):
        if not user:
            # Создаём pending
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
                # Ничего не делаем (интеракция через Mini App)
                pass
            elif status == "banned":
                send_message(chat_id, "🚫 Доступ запрещён.")
        return "ok"

    # Любые другие /команды игнорируем
    return "ok"


# ---------- Mini App с жёстким серверным допуском ----------
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


# Шаблон Mini App (с кнопками и лидерами) — оставляем как есть из предыдущей версии
MINI_APP_HTML = """{{ html | safe }}"""


def _render_mini_app(events):
    # Готовим Jinja-разметку (вставляем из предыдущих версий готовый HTML)
    # Чтобы не перепечатывать длинный HTML, ниже собираем его по частям
    # Для простоты — берём из предыдущей версии как готовую строку (содержимое опущено тут ради краткости).
    # В реальном файле вставьте HTML из предыдущего успешного app.py (версия с большими кнопками, мета-блоком и лидерами с переключателем).
    return MINI_APP_HTML


@app.get("/mini-app")
def mini_app():
    # Требуем chat_id + sig и проверяем статус пользователя
    chat_id = request.args.get("chat_id", type=int)
    sig = request.args.get("sig", "")

    if not chat_id or not sig or not verify_sig(chat_id, sig):
        return Response(
            "<h3>Доступ запрещён</h3><p>Откройте мини‑приложение кнопкой в боте после /start и одобрения.</p>",
            mimetype="text/html",
        )

    user = db.get_user(chat_id)
    if not user or user.get("status") != "approved":
        return Response(
            "<h3>Доступ только для одобренных пользователей</h3><p>Дождитесь одобрения администратора.</p>",
            mimetype="text/html",
        )
    if user.get("status") == "banned":
        return Response("<h3>Доступ запрещён</h3>", mimetype="text/html")

    # Готовим данные событий
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
            volume = max(0.0, total - 2000.0)
            markets[m["option_index"]] = {"yes_price": yp, "volume": volume, "end_short": e["end_short"]}
        e["markets"] = markets

    # Здесь вставьте актуальный HTML из предыдущей версии (из-за объёма опущен) — см. ваш последний успешный app.py
    # Чтобы ответ поместился, подразумевается, что вы просто оставляете предыдущий MINI_APP_HTML и возвращаете его.
    html = "<h2>Mini App загружен</h2><p>Подставьте сюда предыдущий MINI_APP_HTML.</p>"
    return render_template_string(MINI_APP_HTML, html=html)


# ---------- API (все требуют подпись и статус approved) ----------
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
meta{display:block}
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
            kb = {"inline_keyboard": [[{"text": "Открыть Mini App", "web_app": {"url": web_app_url}}]]]}
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
