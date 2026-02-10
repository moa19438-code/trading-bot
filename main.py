import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

def send_telegram(text: str):
    if not TOKEN or not CHAT_ID:
        return False, "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)

    # مهم: نرجع تفاصيل الخطأ لو فيه
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code != 200:
        return False, f"Telegram error {r.status_code}: {data}"

    return True, "ok"

@app.get("/")
def home():
    return "OK"

# اختبار مباشر
@app.get("/test")
def test():
    ok, info = send_telegram("✅ Test: البوت وصل تيليجرام بنجاح")
    return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

# نستقبل من TradingView (ندعم /tv و /webhook)
def handle_signal(payload):
    if SECRET and str(payload.get("secret", "")).strip() != SECRET:
        return jsonify({"ok": False, "error": "bad secret"}), 401

    ticker = payload.get("ticker") or payload.get("symbol") or "UNKNOWN"
    price = payload.get("price") or payload.get("close") or ""
    direction = payload.get("direction") or payload.get("action") or "SIGNAL"
    tf = payload.get("tf") or payload.get("timeframe") or ""

    msg = f"📣 تنبيه TradingView\nالسهم: {ticker}\nالفريم: {tf}\nالاتجاه: {direction}\nالسعر: {price}"
    ok, info = send_telegram(msg)
    return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

@app.post("/tv")
def tv():
    payload = request.get_json(silent=True) or {}
    return handle_signal(payload)

@app.post("/webhook")
def webhook():
    payload = request.get_json(silent=True) or {}
    return handle_signal(payload)
