import os
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ================= Env helpers =================
def getenv_any(names, default=""):
    """Return first non-empty env var value from a list of names."""
    for n in names:
        v = os.getenv(n)
        if v is not None:
            v = str(v).strip()
            if v != "":
                return v
    return default

def getenv_float_any(names, default):
    v = getenv_any(names, "")
    try:
        return float(v) if v != "" else float(default)
    except Exception:
        return float(default)

def getenv_int_any(names, default):
    v = getenv_any(names, "")
    try:
        return int(v) if v != "" else int(default)
    except Exception:
        return int(default)

# ================= Config =================
# Telegram (يدعم عدة أسماء للمتغيرات)
TELEGRAM_BOT_TOKEN = getenv_any(
    ["TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TG_BOT_TOKEN", "TELEGRAM_TOKEN"],
    ""
)
TELEGRAM_CHAT_ID = getenv_any(
    ["TELEGRAM_CHAT_ID", "CHAT_ID", "TG_CHAT_ID", "TELEGRAM_USER_ID"],
    ""
)

# Secrets
WEBHOOK_SECRET = getenv_any(["WEBHOOK_SECRET", "TV_SECRET", "TRADINGVIEW_SECRET", "SECRET_KEY"], "")
RUN_KEY = getenv_any(["RUN_KEY", "SCAN_KEY", "CRON_KEY", "JOB_KEY"], "")

# إعدادات الفرص (اختياري)
STOP_LOSS_PCT = getenv_float_any(["STOP_LOSS_PCT", "SL_PCT"], 3)     # 3%
TAKE_PROFIT_PCT = getenv_float_any(["TAKE_PROFIT_PCT", "TP_PCT"], 5) # 5%
MAX_RESULTS = getenv_int_any(["MAX_RESULTS", "MAX_PICKS"], 7)
MIN_PRICE = getenv_float_any(["MIN_PRICE"], 2)
MAX_PRICE = getenv_float_any(["MAX_PRICE"], 300)
MIN_AVG_VOL = getenv_int_any(["MIN_AVG_VOL", "MIN_VOLUME"], 1500000)

# منع تكرار نفس السهم بنفس اليوم
_state = {"day_key": None, "sent_symbols": set()}

# Timezone ET (يتعامل مع DST تلقائيًا)
try:
    import pytz
    ET = pytz.timezone("America/New_York")
except Exception:
    ET = None

# yfinance
try:
    import yfinance as yf
except Exception:
    yf = None

# ================= Telegram =================
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False, "Missing Telegram env vars"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
    except Exception as e:
        return False, f"Telegram request failed: {e}"

    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code != 200:
        return False, f"Telegram error {r.status_code}: {data}"

    return True, "ok"

# ================= Market / state =================
def market_open_now_et() -> bool:
    # Regular session 09:30–16:00 ET (Mon–Fri)
    if ET is None:
        return True
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= now <= end

def reset_day():
    dk = (datetime.now(ET).strftime("%Y-%m-%d") if ET else datetime.utcnow().strftime("%Y-%m-%d"))
    if _state["day_key"] != dk:
        _state["day_key"] = dk
        _state["sent_symbols"] = set()

def calc_levels(entry: float):
    sl = entry * (1 - STOP_LOSS_PCT / 100.0)
    tp = entry * (1 + TAKE_PROFIT_PCT / 100.0)
    return round(sl, 4), round(tp, 4)

def load_universe():
    # tickers.txt في نفس الريبو
    path = os.path.join(os.path.dirname(__file__), "tickers.txt")
    tickers = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                t = line.strip().upper()
                if t and not t.startswith("#"):
                    tickers.append(t)
    except Exception:
        pass
    return list(dict.fromkeys(tickers))

def scan_universe(tickers):
    """
    سكانر بسيط:
    - بيانات يومية 1mo
    - فلتر سعر + متوسط فوليوم
    - ترتيب Score = (تغير يومي %) + عامل سيولة
    """
    if yf is None:
        return [], "yfinance not installed"

    results = []
    chunk = 60

    for i in range(0, len(tickers), chunk):
        group = tickers[i:i+chunk]
        try:
            df = yf.download(
                tickers=" ".join(group),
                period="1mo",
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False
            )
        except Exception:
            continue

        for sym in group:
            try:
                # Multi-index handling
                if "Close" in df.columns:
                    closes = df["Close"].dropna()
                    vols = df["Volume"].dropna()
                else:
                    closes = df[(sym, "Close")].dropna()
                    vols = df[(sym, "Volume")].dropna()

                if len(closes) < 2 or len(vols) < 5:
                    continue

                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2])
                chg_pct = ((last - prev) / prev) * 100.0
                avg_vol = int(vols.tail(20).mean())

                if last < MIN_PRICE or last > MAX_PRICE:
                    continue
                if avg_vol < MIN_AVG_VOL:
                    continue

                score = chg_pct + (avg_vol / 10_000_000)
                sl, tp = calc_levels(last)

                results.append({
                    "symbol": sym,
                    "entry": round(last, 4),
                    "sl": sl,
                    "tp": tp,
                    "chg_pct": round(chg_pct, 2),
                    "avg_vol": avg_vol,
                    "score": score
                })
            except Exception:
                continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return results, "ok"

# ================= Endpoints =================
@app.get("/")
def home():
    # JSON دائمًا عشان ما يطلع "Not Found" أو نص مبهم
    return jsonify({"ok": True, "service": "trading-bot", "endpoints": ["/test", "/webhook", "/tv", "/scan"]})

@app.get("/test")
def test():
    ok, info = send_telegram("✅ Test / اختبار: البوت شغال ويرسل تيليجرام بنجاح\nBot is running and Telegram works.")
    return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

# ============ TradingView webhooks (/webhook و /tv) ============
def handle_tradingview(payload: dict):
    # تحقق من السر
    if WEBHOOK_SECRET:
        incoming = str(payload.get("secret", "")).strip()
        if incoming != WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "bad secret"}), 401

    ticker = payload.get("ticker") or payload.get("symbol") or "UNKNOWN"
    price = payload.get("price") or payload.get("close") or payload.get("last") or ""
    tf = payload.get("tf") or payload.get("timeframe") or payload.get("interval") or ""
    direction = payload.get("direction") or payload.get("action") or "SIGNAL"
    reason = payload.get("reason") or payload.get("message") or "TradingView Alert"

    msg = (
        "📣 تنبيه TradingView / TradingView Alert\n"
        f"السهم / Ticker: {ticker}\n"
        f"الفريم / TF: {tf}\n"
        f"الاتجاه / Direction: {direction}\n"
        f"السعر / Price: {price}\n"
        f"السبب / Reason: {reason}"
    )

    ok, info = send_telegram(msg)
    return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

@app.post("/webhook")
def webhook():
    payload = request.get_json(silent=True) or {}
    return handle_tradingview(payload)

@app.post("/tv")
def tv():
    payload = request.get_json(silent=True) or {}
    return handle_tradingview(payload)

# ============ Scanner (/scan) ============
@app.get("/scan")
def scan():
    # حماية التشغيل
    key = request.args.get("key", "").strip()
    if not RUN_KEY or key != RUN_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    reset_day()

    # يشتغل فقط وقت السوق الأمريكي (ET)
    if not market_open_now_et():
        return jsonify({"ok": True, "ignored": "market_closed"}), 200

    universe = load_universe()
    if not universe:
        ok, info = send_telegram(
            "⚠️ خطأ: ملف tickers.txt غير موجود أو فاضي.\n"
            "Error: tickers.txt is missing or empty."
        )
        return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

    picks, status = scan_universe(universe)
    if not picks:
        return jsonify({"ok": True, "status": status, "message": "no picks"}), 200

    # تجنب تكرار نفس السهم
    fresh = []
    for p in picks:
        if p["symbol"] not in _state["sent_symbols"]:
            fresh.append(p)
        if len(fresh) >= MAX_RESULTS:
            break

    if not fresh:
        return jsonify({"ok": True, "message": "no new symbols"}), 200

    # رسالة واحدة فيها النتائج (AR + EN)
    lines = []
    lines.append(f"📌 فرص أثناء السوق / Market Picks (SL {STOP_LOSS_PCT}% | TP {TAKE_PROFIT_PCT}%)")
    lines.append(f"عدد الفرص / Count: {len(fresh)}")
    lines.append("—")

    for i, p in enumerate(fresh, 1):
        lines.append(
            f"{i}) {p['symbol']} | Δ يومي/Daily: {p['chg_pct']}% | AvgVol: {p['avg_vol']}\n"
            f"دخول/Entry: {p['entry']}\n"
            f"وقف/SL (-{STOP_LOSS_PCT}%): {p['sl']}\n"
            f"هدف/TP (+{TAKE_PROFIT_PCT}%): {p['tp']}\n"
            "—"
        )

    ok, info = send_telegram("\n".join(lines))
    if ok:
        for p in fresh:
            _state["sent_symbols"].add(p["symbol"])

    return jsonify({"ok": ok, "info": info, "sent": len(fresh)}), (200 if ok else 500)

if __name__ == "__main__":
    app.run()
