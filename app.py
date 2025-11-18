import os
import json
import hmac
import hashlib
import time
from datetime import datetime

import requests
from flask import (
    Flask,
    request,
    render_template_string,
    jsonify,
    Response,
    stream_with_context,
)
from functools import wraps

from database import db


app = Flask(__name__)

# --- ENV ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
BASE_URL = os.getenv("WEBHOOK_URL")
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "change-me")

ADMIN_BASIC_USER = os.getenv("ADMIN_BASIC_USER", "admin")
ADMIN_BASIC_PASS = os.getenv("ADMIN_BASIC_PASS", "admin")

# Для подписи Mini App (HMAC(chat_id, secret))
WEBAPP_SIGNING_SECRET = os.getenv("WEBAPP_SIGNING_SECRET")  # обязательно задайте


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
            json={
                "url": f"{BASE_URL}/webhook",
                "secret_token": TELEGRAM_SECRET_TOKEN,
                # при необходимости раскомментируйте:
                # "drop_pending_updates": True,
            },
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
                "Добро пожаловать! Напишите ваш желаемый логин одним сообщением.\n"
                "После модерации получите доступ к приложению.",
            )
        else:
            if status == "approved":
                # Одобрен — сразу даём кнопку Mini App
                sig = make_sig(chat_id)
                if not sig:
                    send_message(chat_id, "Сервис временно недоступен. Повторите позже.")
                    return "ok"
                web_app_url = (
                    f"https://{request.host}/mini-app?"
                    f"chat_id={chat_id}&sig={sig}&v={int(time.time())}"
                )
                kb = {
                    "inline_keyboard": [
                        [{"text": "Открыть Mini App", "web_app": {"url": web_app_url}}]
                    ]
                }
                send_message(chat_id, "Приложение готово.\nОткрывайте:", kb)
            elif status == "pending":
                send_message(
                    chat_id, "⏳ Ваша заявка на регистрацию ожидает проверки администратором."
                )
            elif status == "banned":
                send_message(chat_id, "🚫 Доступ к приложению запрещён.")
            elif status == "rejected":
                send_message(
                    chat_id,
                    "❌ Заявка отклонена.\nОтправьте новый логин одним сообщением для повторной подачи.",
                )
            else:
                send_message(chat_id, "Напишите ваш логин одним сообщением для регистрации.")
        return "ok"

    # Любой текст НЕ команда:
    # если нет пользователя — трактуем как логин, если в pending — просто уведомляем
    if not text.startswith("/"):
        if not user:
            new_user = db.create_user(chat_id, text, username)
            if new_user:
                send_message(
                    chat_id,
                    f"✅ Логин '{text}' отправлен на модерацию.\nОжидайте подтверждения.",
                )
                notify_admin(
                    f"Новая заявка:\nЛогин: {text}\nID: {chat_id}\nUsername: @{username}\n"
                    f"Админка: {BASE_URL}/admin"
                )
            else:
                send_message(chat_id, "❌ Ошибка при создании заявки. Попробуйте ещё раз.")
        else:
            if status == "pending":
                send_message(chat_id, "⏳ Заявка уже на рассмотрении.\nОжидайте ответа администратора.")
            elif status == "approved":
                pass
            elif status == "banned":
                send_message(chat_id, "🚫 Доступ запрещён.")
        return "ok"

    # Любые другие /команды игнорируем
    return "ok"


# ---------- Mini App HTML + JS ----------
MINI_APP_HTML = """
<!doctype html>
<meta charset="utf-8">
<title>Мероприятия</title>
<style>
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 16px; }
  .event { border: 1px solid #eee; border-radius: 10px; padding: 12px; margin: 12px 0; }
  .opt { display: flex; align-items: center; gap: 10px; margin: 8px 0; }
  .pill { padding: 2px 8px; border-radius: 999px; background:#f2f2f2; font-size: 12px; border: none; cursor: pointer; }
  button.buy { padding: 8px 12px; border: 0; border-radius: 8px; cursor: pointer; }
  button.yes { background:#e6f7ee; color:#0a7f42; }
  button.no  { background:#fdeaea; color:#bd1a1a; }
  #buyModal { position: fixed; inset: 0; background: #0006; display:none; align-items:center; justify-content:center; }
  #buyCard { background:#fff; padding:16px; border-radius:12px; width: 320px; }
  .muted { color:#666; font-size: 13px; }
</style>

<h2>U</h2>

<div class="muted">Баланс: <b id="balance">—</b></div>

<h3>Активные ставки ▾</h3>
<div id="positions" class="muted">Загрузка...</div>

<h3>Мероприятия ▾</h3>
<div id="events">
  {% for e in events %}
  <div class="event" data-ev="{{ e.event_uuid }}">
    <div><b>{{ e.name }}</b></div>
    <div class="muted">{{ e.description }}</div>

    {% for idx, opt in enumerate(e.options) %}
      {% set md = e.markets.get(idx, {'yes_price': 0.5, 'volume': 0, 'end_short': e.end_short}) %}
      {% set yes_pct = ('%.0f' % (md.yes_price * 100)) %}
      {% set no_pct = ('%.0f' % ((1 - md.yes_price) * 100)) %}
      <div class="opt">
        <div style="flex:1">
          <div><b>{{ opt.text }}</b></div>
          <div class="muted">
            Вероятность ДА: {{ yes_pct }}% · Объем:
            {% set vol = (md.volume or 0) | int %}
            {% if vol >= 1000 %} {{ (vol // 1000) | int }} тыс. кредитов {% else %} {{ vol }} кредитов {% endif %}
            · До {{ md.end_short }}
          </div>
        </div>
        <div style="display:flex; gap:8px">
          <button class="buy yes" data-ev="{{ e.event_uuid }}" data-opt="{{ idx }}" data-side="yes">ДА</button>
          <button class="buy no"  data-ev="{{ e.event_uuid }}" data-opt="{{ idx }}" data-side="no">НЕТ</button>
        </div>
      </div>
    {% endfor %}
  </div>
  {% endfor %}
</div>

<h3>Таблица лидеров ▸</h3>
<div class="muted" style="margin: 6px 0 10px">
  <button id="lbWeek" class="pill">Неделя</button>
  <button id="lbMonth" class="pill">Месяц</button>
</div>
<div id="leaderboard" class="muted">Загрузка…</div>

<!-- Модалка покупки -->
<div id="buyModal">
  <div id="buyCard">
    <h3>Покупка</h3>
    <div class="muted">Укажите сумму, не выше вашего баланса.</div>
    <input id="amount" type="number" min="1" step="1" placeholder="Сумма (кредиты)" style="width:100%;margin:8px 0;padding:8px">
    <div style="display:flex; gap:8px; justify-content:flex-end">
      <button id="confirmBuy">Купить</button>
      <button id="cancelBuy">Отмена</button>
    </div>
  </div>
</div>

<script>
  const qs = new URLSearchParams(location.search);
  const chat_id = Number(qs.get('chat_id'));
  const sig = qs.get('sig') || '';

  const elBal = document.getElementById('balance');
  const elPos = document.getElementById('positions');
  const elLB  = document.getElementById('leaderboard');
  const modal = document.getElementById('buyModal');
  const amount = document.getElementById('amount');

  let pendingBuy = null;

  async function loadMe() {
    try {
      const r = await fetch(`/api/me?chat_id=${chat_id}&sig=${sig}`);
      const j = await r.json();
      if (!j.success) throw new Error(j.error || 'api_me_failed');
      elBal.textContent = j.user.balance;
      if (!j.positions.length) {
        elPos.textContent = 'Нет активных позиций';
      } else {
        elPos.innerHTML = j.positions.map(p => (
          `${p.event_name} · ${p.option_text} · ${p.share_type.toUpperCase()} · кол-во ${p.quantity} · ср. цена ${p.average_price}`
        )).join('<br>');
      }
    } catch (e) {
      elPos.textContent = 'Ошибка загрузки. Обновите окно.';
      console.error(e);
    }
  }

  async function loadLB(period = 'week') {
    try {
      const r = await fetch(`/api/leaderboard?period=${period}`);
      const j = await r.json();
      if (!j.success) throw new Error('lb_failed');
      elLB.innerHTML = j.items.slice(0, 20).map((it, i) =>
        `${i+1}. ${it.login || it.chat_id} — ${Number(it.earned).toFixed(2)}`
      ).join('<br>');
    } catch (e) {
      elLB.textContent = 'Ошибка загрузки таблицы лидеров';
      console.error(e);
    }
  }

  function openBuy(btn) {
    pendingBuy = {
      event_uuid: btn.dataset.ev,
      option_index: Number(btn.dataset.opt),
      side: btn.dataset.side,
    };
    amount.value = '';
    modal.style.display = 'flex';
    amount.focus();
  }

  async function confirmBuy() {
    const amt = Number(amount.value);
    if (!pendingBuy || !amt || amt <= 0) return;
    try {
      const r = await fetch('/api/market/buy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id, sig, amount: amt, ...pendingBuy })
      });
      const data = await r.json();
      if (!data.success) {
        alert("Ошибка: " + (data.error || "buy_failed"));
      } else {
        // ВАЖНО: исправленная строка без лишней фигурной скобки
        alert("Успех: куплено " + data.trade.got_shares.toFixed(4) + " акций");
        elBal.textContent = data.trade.new_balance;
      }
      modal.style.display = 'none';
      pendingBuy = null;
    } catch (e) {
      alert('Сеть/сервер: ' + (e?.message || e));
    }
  }

  // Вешаем обработчики
  document.querySelectorAll('button.buy').forEach(btn => {
    btn.addEventListener('click', () => openBuy(btn));
  });

  document.getElementById('cancelBuy').onclick = () => { pendingBuy = null; modal.style.display = 'none'; };
  document.getElementById('confirmBuy').onclick = confirmBuy;

  document.getElementById('lbWeek').onclick = () => loadLB('week');
  document.getElementById('lbMonth').onclick = () => loadLB('month');

  // init
  loadMe();
  loadLB('week');
</script>
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


@app.get("/mini-app")
def mini_app():
    # Жёсткий доступ: нужен chat_id + sig + статус approved (и не banned)
    chat_id = request.args.get("chat_id", type=int)
    sig = request.args.get("sig", "")
    if not chat_id or not sig or not verify_sig(chat_id, sig):
        return Response(
            "<h3>Доступ запрещён</h3><p>Откройте Mini App из бота после /start и одобрения.</p>",
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
            yp = (no / total) if total > 0 else 0.5  # цена "ДА" ~= доля NO в пулах
            volume = max(0.0, total - 2000.0)  # «заведённые» кредиты в пул сверх стартовых
            markets[m["option_index"]] = {
                "yes_price": yp,
                "volume": volume,
                "end_short": e["end_short"],
            }
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
        r2 = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=10,
        )
        fp = r2.json().get("result", {}).get("file_path")
        if not fp:
            return Response(status=204)
        furl = f"https://api.telegram.org/file/bot{TOKEN}/{fp}"
        fr = requests.get(furl, timeout=10, stream=True)
        headers = {
            "Content-Type": fr.headers.get("Content-Type", "image/jpeg"),
            "Cache-Control": "public, max-age=3600",
        }
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
# Админская панель

## Заявки ({{ pending|length }})
{% for u in pending %}
**#{{ loop.index }} • {{ u.login }} (@{{u.username}})**
ID {{ u.chat_id }}  
Заявка от: {{ (u.created_at or '')[:16] }}

<!--citation:1--> • <!--citation:2-->
---
{% endfor %}

## Одобренные ({{ approved|length }})
{% for u in approved %}
**#{{ loop.index }} • {{ u.login }} (@{{u.username}})**  
Баланс: {{u.balance}}

Изменить баланс:
<form action="/admin/update_balance/?chat_id={{u.chat_id}}" method="post">
  <input name="balance" type="number" value="{{u.balance}}">
  <button type="submit">Сохранить</button>
</form>

<!--citation:3-->
---
{% endfor %}

## Забаненные ({{ banned|length }})
{% for u in banned %}
**#{{ loop.index }} • {{ u.login }} (@{{u.username}})**  
ID {{ u.chat_id }}  
Статус: banned

[Разбанить (одобрить)](/admin/unban/?chat_id={{u.chat_id}})
---
{% endfor %}
"""


@app.get("/admin")
@requires_auth
def admin_panel():
    pending = db.get_pending_users()
    approved = db.get_approved_users()
    banned = db.get_banned_users()
    return render_template_string(ADMIN_HTML, pending=pending, approved=approved, banned=banned)


# Примитивные админ-действия через query (?chat_id=)
@app.post("/admin/approve/")
@requires_auth
def admin_approve():
    chat_id = request.args.get("chat_id", type=int)
    if not chat_id:
        return jsonify(success=False, error="chat_id_required"), 400
    user = db.approve_user(chat_id)
    if user:
        # Отправим пользователю кнопку Mini App
        sig = make_sig(chat_id)
        if sig:
            web_app_url = f"https://{request.host}/mini-app?chat_id={chat_id}&sig={sig}&v={int(time.time())}"
            kb = {
                "inline_keyboard": [
                    [{"text": "Открыть Mini App", "web_app": {"url": web_app_url}}]
                ]
            }
            send_message(chat_id, "✅ Ваша регистрация подтверждена! Добро пожаловать.", kb)
        return jsonify(success=True)
    return jsonify(success=False), 404


@app.post("/admin/reject/")
@requires_auth
def admin_reject():
    chat_id = request.args.get("chat_id", type=int)
    if not chat_id:
        return jsonify(success=False, error="chat_id_required"), 400
    ok = db.reject_user(chat_id)
    return jsonify(success=bool(ok))


@app.post("/admin/update_balance/")
@requires_auth
def admin_update_balance():
    chat_id = request.args.get("chat_id", type=int)
    if not chat_id:
        return jsonify(success=False, error="chat_id_required"), 400
    new_balance = request.values.get("balance", type=int)
    if new_balance is None or new_balance < 0:
        return jsonify(success=False, error="bad_balance"), 400
    user = db.update_user_balance(chat_id, new_balance)
    return jsonify(success=bool(user))


@app.post("/admin/ban/")
@requires_auth
def admin_ban():
    chat_id = request.args.get("chat_id", type=int)
    if not chat_id:
        return jsonify(success=False, error="chat_id_required"), 400
    user = db.ban_user(chat_id)
    return jsonify(success=bool(user))


@app.post("/admin/unban/")
@requires_auth
def admin_unban():
    chat_id = request.args.get("chat_id", type=int)
    if not chat_id:
        return jsonify(success=False, error="chat_id_required"), 400
    user = db.unban_user(chat_id)
    return jsonify(success=bool(user))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
