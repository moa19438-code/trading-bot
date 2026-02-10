import os
import time
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# --- Secrets / Config (from Render Environment Variables) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# Optional tuning
MAX_ALERTS_PER_DAY = int(os.getenv("MAX_ALERTS_PER_DAY", "20"))
ENABLE_TIME_WINDOW = os.getenv("ENABLE_TIME_WINDOW", "0").strip() == "1"

# Time window for US market (NY time) – optional
# If ENABLE_TIME_WINDOW=1, bot only sends between 09:35–15:55 ET (Mon–Fri)
try:
    import pytz
    ET = pytz.timezone("America/New_York")
except Exception:
    ET = None

_state = {"day_key": None, "sent_today": 0}

def _day_key():
    if ET:
        return datetime.now(ET).strftime("%Y-%m-%d")
    return datetime.utcnow().strftime("%Y-%m-%d")

def _reset_daily_counter():
    dk = _day_key()
    if _state["day_key"] != dk:
        _state["day_key"] = dk
        _state["sent_today"] = 0

def _within_time_window():
    if not ET:
        return True
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9, minute=35, second=0, microsecond=0)
    end = now.replace(hour=15, minute=55, second=0, microsecond=0)
    return start <= now <= end

def _tg_send(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    r.raise_for_status()

@app.get("/")
def health():
    return "OK"

@app.post("/tv")
def tv_webhook():
    _reset_daily_counter()

    payload = request.get_json(silent=True) or {}

    # Basic auth via shared secret
    if not WEBHOOK_SECRET or str(payload.get("secret", "")).strip() != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    # Optional: time window filter
    if ENABLE_TIME_WINDOW and not _within_time_window():
        return jsonify({"ok": True, "ignored": "outside_time_window"}), 200

    # Daily limit
    if _state["sent_today"] >= MAX_ALERTS_PER_DAY:
        return jsonify({"ok": True, "ignored": "daily_limit"}), 200

    # Extract fields (TradingView message will send these)
    ticker = payload.get("ticker", "UNKNOWN")
    direction = payload.get("direction", "SIGNAL")  # LONG/SHORT
    price = payload.get("price", payload.get("close", ""))
    tf = payload.get("tf", payload.get("timeframe", ""))
    reason = payload.get("reason", "")

    entry = payload.get("entry")
    sl = payload.get("sl")
    tp1 = payload.get("tp1")
    tp2 = payload.get("tp2")

    lines = []
    lines.append("📣 تنبيه TradingView")
    lines.append(f"السهم: {ticker}")
    if tf: lines.append(f"الفريم: {tf}")
    lines.append(f"الاتجاه: {direction}")
    if price != "": lines.append(f"السعر: {price}")

    if entry is not None: lines.append(f"دخول: {entry}")
    if sl is not None: lines.append(f"وقف: {sl}")
    if tp1 is not None or tp2 is not None:
        tps = []
        if tp1 is not None: tps.append(str(tp1))
        if tp2 is not None: tps.append(str(tp2))
        lines.append("أهداف: " + " / ".join(tps))

    if reason:
        lines.append(f"سبب: {reason}")

    # Add timestamp (UTC)
    lines.append(f"⏱ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")

    _tg_send("\n".join(lines))
    _state["sent_today"] += 1

    return jsonify({"ok": True, "sent_today": _state["sent_today"]}), 200
