import os
from datetime import datetime
import pytz
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
ET = pytz.timezone("America/New_York")

TELEGRAM_BOT_TOKEN = os.getenv("7333036344:AAGK-i_35ymA6abOIxc-aRT0Y4Zlom8GgwY", "").strip()
TELEGRAM_CHAT_ID = os.getenv("1750462226", "").strip()
WEBHOOK_SECRET = os.getenv("tv_SahmBot_9Xk21Qp7!", "").strip()

MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "2"))
MIN_SCORE_TO_SEND = float(os.getenv("MIN_SCORE_TO_SEND", "0"))

_state = {"day": None, "sent": 0}

def _truthy(x) -> bool:
    return x in (True, "true", "True", 1, "1", "yes", "YES")

def _reset_day_if_needed():
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if _state["day"] != today:
        _state["day"] = today
        _state["sent"] = 0

def _after_first_hour():
    # NYSE 9:30–16:00 ET, نبدأ بعد أول ساعة: 10:30 ET
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    start_ok = now >= now.replace(hour=10, minute=30, second=0, microsecond=0)
    end_ok = now <= now.replace(hour=16, minute=0, second=0, microsecond=0)
    return start_ok and end_ok

def _tg_send(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    r.raise_for_status()

def _score(payload: dict) -> float:
    s = 0.0
    if _truthy(payload.get("vol_spike")):  s += 2.0
    if _truthy(payload.get("above_vwap")): s += 1.5
    if _truthy(payload.get("ema_trend")):  s += 1.0
    q = payload.get("quality")
    if isinstance(q, (int, float)): s += float(q)
    return s

@app.get("/")
def health():
    return "OK"

@app.post("/tv")
def tv_webhook():
    _reset_day_if_needed()

    payload = request.get_json(silent=True) or {}
    if not WEBHOOK_SECRET or str(payload.get("secret", "")).strip() != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    if not _after_first_hour():
        return jsonify({"ok": True, "ignored": "outside_time_window"}), 200

    if _state["sent"] >= MAX_TRADES_PER_DAY:
        return jsonify({"ok": True, "ignored": "daily_limit_reached"}), 200

    sig_score = _score(payload)
    if sig_score < MIN_SCORE_TO_SEND:
        return jsonify({"ok": True, "ignored": "low_score"}), 200

    ticker = payload.get("ticker", "UNKNOWN")
    direction = payload.get("direction", "ENTRY")  # LONG/SHORT
    price = payload.get("price", payload.get("close", ""))
    tf = payload.get("tf", payload.get("timeframe", ""))

    entry = payload.get("entry")
    sl = payload.get("sl")
    tp1 = payload.get("tp1")
    tp2 = payload.get("tp2")
    reason = payload.get("reason", "")

    msg = []
    msg.append("📣 إشارة مضاربة (بعد أول ساعة)")
    msg.append(f"السهم: {ticker}")
    if tf: msg.append(f"الفريم: {tf}")
    msg.append(f"الاتجاه: {direction}")
    if price != "": msg.append(f"السعر الآن: {price}")
    if entry is not None: msg.append(f"الدخول: {entry}")
    if sl is not None: msg.append(f"الوقف: {sl}")
    if tp1 is not None or tp2 is not None:
        tps = []
        if tp1 is not None: tps.append(str(tp1))
        if tp2 is not None: tps.append(str(tp2))
        msg.append("الأهداف: " + " / ".join(tps))
    if reason: msg.append(f"السبب: {reason}")
    msg.append(f"درجة الإشارة: {sig_score:.1f}")
    msg.append("تنبيه: التنفيذ يدوي داخل Sahm. مساعد تعليمي وليس ضمان ربح.")

    _tg_send("\n".join(msg))
    _state["sent"] += 1

    return jsonify({"ok": True, "sent_count_today": _state["sent"]}), 200
