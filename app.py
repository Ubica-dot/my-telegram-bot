import os
import json
import uuid
from datetime import datetime

import requests
from flask import Flask, request, render_template_string, jsonify, Response
from functools import wraps

from database import db

app = Flask(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
BASE_URL = os.getenv("WEBHOOK_URL")
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "change-me")

ADMIN_BASIC_USER = os.getenv("ADMIN_BASIC_USER", "admin")
ADMIN_BASIC_PASS = os.getenv("ADMIN_BASIC_PASS", "admin")


# ---------------- Admin auth ----------------
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


# ---------------- Telegram utils ----------------
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


# ---------------- Webhook setup ----------------
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


@app.before_first_request
def _before_first_request():
    ensure_webhook()


# ---------------- Health ----------------
@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/")
def index():
    return "OK"


# ---------------- Telegram Webhook ----------------
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
        web_app_url = f"https://{request.host}/mini-app"
        kb = {
            "inline_keyboard": [
                [
                    {"text": "Открыть Mini App", "web_app": {"url": web_app_url}}
                ]
            ]
        }
        send_message(chat_id, "Откройте мини‑приложение для удобного просмотра мероприятий:", kb)

    elif text.startswith("/"):
        if not is_approved:
            send_message(chat_id, "❌ Нет доступа. Сначала завершите регистрацию через /start")
        else:
            send_message(chat_id, "Команда не распознана.")

    else:
        # Текст без / — трактуем как логин для регистрации
        if is_approved:
            send_message(chat_id, "✅ Вы уже зарегистрированы! Используйте доступные команды.")
        else:
            if user:
                send_message(chat_id, "⏳ Ваша заявка уже на рассмотрении. Ожидайте ответа администратора.")
            else:
                new_user = db.create_user(chat_id, text, username)
                if new_user:
                    send_message(chat_id, f"✅ Логин '{text}' отправлен на модерацию. Ожидайте подтверждения.")
                    notify_admin(
                        f"Новая заявка:\nЛогин: {text}\nID: {chat_id}\nUsername: @{username}\nАдмин‑панель: {BASE_URL}/admin"
                    )
                else:
                    send_message(chat_id, "❌ Ошибка при отправке заявки. Попробуйте ещё раз.")

    return "ok"


# ---------------- Mini App (пер‑вариантные кнопки ДА/НЕТ) ----------------
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
    .opt  { padding: 8px; border: 1px dashed #ccc; border-radius: 8px; margin: 6px 0; }
    .btn  { padding: 8px 10px; margin-right: 8px; border: 0; border-radius: 8px; cursor: pointer; color: #fff; }
    .yes  { background: #2e7d32; }
    .no   { background: #c62828; }
    .row  { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
    small { color: #666; }
  </style>
</head>
<body>
  <h2>Активные мероприятия</h2>
  {% for e in events %}
    <div class="card" data-event="{{ e.event_uuid }}">
      <div><b>{{ e.name }}</b></div>
      <div><small>До: {{ e.end_date[:16] }}</small></div>
      <p>{{ e.description }}</p>

      {% for idx, opt in enumerate(e.options) %}
        {% set md = e.markets.get(idx, {'yes_price': 0.5, 'no_price': 0.5}) %}
        <div class="opt" data-option="{{ idx }}">
          <div><b>Вариант {{ idx + 1 }}:</b> {{ opt.text }}</div>
          <div class="row">
            <div>
              <small>Цена ДА: {{ '%.3f' % md.yes_price }} | НЕТ: {{ '%.3f' % md.no_price }}</small>
            </div>
            <div>
              <button class="btn yes" onclick="buy('{{ e.event_uuid }}', {{ idx }}, 'yes')">Купить ДА</button>
              <button class="btn no"  onclick="buy('{{ e.event_uuid }}', {{ idx }}, 'no')">Купить НЕТ</button>
            </div>
          </div>
        </div>
      {% endfor %}
    </div>
  {% endfor %}

  <script>
    const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    if (tg) tg.ready();

    function getChatId() {
      if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id) {
        return tg.initDataUnsafe.user.id;
      }
      const p = new URLSearchParams(location.search);
      return p.get("chat_id");
    }

    async function buy(event_uuid, option_index, side) {
      const chat_id = getChatId();
      if (!chat_id) { alert("Не удалось получить chat_id."); return; }

      const amount = prompt("Сумма покупки (в кредитах):", "100");
      if (!amount) return;

      try {
        const r = await fetch("/api/market/buy", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id, event_uuid, option_index, side, amount: parseFloat(amount) })
        });
        const data = await r.json();
        if (!r.ok || !data.success) {
          alert("Ошибка: " + (data.error || r.statusText));
          return;
        }

        const card = document.querySelector(`[data-event='${event_uuid}'] [data-option='${option_index}']`);
        if (card) {
          const small = card.querySelector("small");
          if (small) {
            small.textContent = `Цена ДА: ${data.market.yes_price.toFixed(3)} | НЕТ: ${data.market.no_price.toFixed(3)}`;
          }
        }
        if (tg) tg.showPopup({ title: "Успешно", message: `Куплено ${data.trade.got_shares.toFixed(4)} акций (${side.toUpperCase()})` });
        else alert("Успех: куплено " + data.trade.got_shares.toFixed(4) + " акций");
      } catch (e) {
        console.error(e);
        alert("Сетевая ошибка");
      }
    }
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
            yes = float(m["total_yes_reserve"]); no = float(m["total_no_reserve"])
            total = yes + no
            if total == 0:
                yp, np = 0.5, 0.5
            else:
                yp, np = no / total, yes / total
            markets[m["option_index"]] = {"yes_price": yp, "no_price": np}
        e["markets"] = markets
        enriched.append(e)
    return render_template_string(MINI_APP_HTML, events=enriched, enumerate=enumerate)


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

    result, err = db.trade_buy(
        chat_id=chat_id,
        event_uuid=event_uuid,
        option_index=option_index,
        side=side,
        amount=amount
    )
    if err:
        return jsonify(success=False, error=err), 400

    return jsonify(success=True, trade={
        "got_shares": result["got_shares"],
        "trade_price": result["trade_price"],
        "new_balance": result["new_balance"],
    }, market={
        "yes_price": result["yes_price"],
        "no_price": result["no_price"],
        "yes_reserve": result["yes_reserve"],
        "no_reserve": result["no_reserve"],
    })


# ---------------- Admin panel ----------------
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
</body>
</html>
"""

@app.get("/admin")
@requires_auth
def admin_panel():
    pending = db.get_pending_users()
    approved = db.get_approved_users()
    return render_template_string(
        ADMIN_PANEL_HTML, pending_users=pending, approved_users=approved, pending_count=len(pending), approved_count=len(approved)
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
        # Инициализируем рынки по каждому варианту
        for idx in range(len(options)):
            db.create_prediction_market(event_uuid, idx)

        # Уведомим одобренных
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
