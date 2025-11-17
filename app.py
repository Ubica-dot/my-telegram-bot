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
    return Response("Auth required", 401, {"WWW-Authenticate": 'Basic realm="Admin'"})


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
    .btn  { padding: 8px 12px; border: 0; border-radius: 10px; cursor: pointer; color: #fff; font-size: 14px; }
    .yes  { background: #2e7d32; }
    .no   { background: #c62828; }
    .actions { display:flex; gap: 8px; align-items: stretch; height: 100%; }
    .actions .btn { height: 100%; display:flex; align-items:center; justify-content:center; }
    .muted { color: #666; 
