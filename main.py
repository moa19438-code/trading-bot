import os
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ===== Render Env Vars =====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# حماية تشغيل السكانر (GitHub Actions)
RUN_KEY = os.getenv("RUN_KEY", "").strip()

# إعدادات الفرص
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "3"))     # 3%
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "5")) # 5%
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "7"))           # 3-7 فرص (نحدد 7 كحد أعلى)
MIN_PRICE = float(os.getenv("MIN_PRICE", "2"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "300"))

# فلترة السيولة (اختياري – ارفعها/خفّضها لاحقًا)
MIN_AVG_VOL = int(os.getenv("MIN_AVG_VOL", "1500000"))

# منع تكرار نفس السهم بنفس اليوم
_state = {
    "day_key": None,
    "sent_symbols": set(),
}

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


# ================= Helpers =================
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False, "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)

    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code != 200:
        return False, f"Telegram error {r.status_code}: {data}"

    return True, "ok"


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
    if ET is None:
        dk = datetime.utcnow().strftime("%Y-%m-%d")
    else:
        dk = datetime.now(ET).strftime("%Y-%m-%d")
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
    # unique
    return list(dict.fromkeys(tickers))


def scan_universe(tickers):
    """
    سكانر بسيط وعملي:
    - يستخدم yfinance لجلب بيانات يومية سريعة (آخر يومين) + متوسط فوليوم 20 يوم
    - يرتّب حسب (ارتفاع يومي + سيولة)
    ملاحظة: هذا ليس "كل السوق حرفيًا" لكنه يغطي قائمة كبيرة تحددها في tickers.txt
    """
    if yf is None:
        return [], "yfinance not installed"

    results = []

    # نخليها دفعات لتقليل الأعطال
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
                # التعامل مع multi-index
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

                # Score بسيط: ارتفاع يومي + عامل سيولة
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
    return "OK"


@app.get("/test")
def test():
    ok, info = send_telegram("✅ Test: البوت شغال ويرسل تيليجرام بنجاح")
    return jsonify({"ok": ok, "info": info}), (200 if ok else 500)


# TradingView webhooks (/webhook و /tv)
def handle_tradingview(payload: dict):
    if WEBHOOK_SECRET and str(payload.get("secret", "")).strip() != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "bad secret"}), 401

    ticker = payload.get("ticker") or payload.get("symbol") or "UNKNOWN"
    price = payload.get("price") or payload.get("close") or ""
    direction = payload.get("direction") or payload.get("action") or "SIGNAL"
    tf = payload.get("tf") or payload.get("timeframe") or ""

    msg = f"📣 تنبيه TradingView\nالسهم: {ticker}\nالفريم: {tf}\nالاتجاه: {direction}\nالسعر: {price}"
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


# السكانر اللحظي: GitHub Actions يناديه كل 5 دقائق
@app.get("/scan")
def scan():
    # حماية
    key = request.args.get("key", "").strip()
    if not RUN_KEY or key != RUN_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    reset_day()

    # يشتغل فقط وقت السوق الأمريكي (يتعامل مع التوقيت الصيفي تلقائياً)
    if not market_open_now_et():
        return jsonify({"ok": True, "ignored": "market_closed"}), 200

    universe = load_universe()
    if not universe:
        ok, info = send_telegram("⚠️ ملف tickers.txt غير موجود أو فاضي.")
        return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

    picks, status = scan_universe(universe)
    if not picks:
        return jsonify({"ok": True, "status": status, "message": "no picks"}), 200

    # خذ أفضل نتائج، وتجنب إعادة إرسال نفس السهم في نفس اليوم
    fresh = []
    for p in picks:
        if p["symbol"] not in _state["sent_symbols"]:
            fresh.append(p)
        if len(fresh) >= MAX_RESULTS:
            break

    if not fresh:
        return jsonify({"ok": True, "message": "no new symbols"}), 200

    # أرسل رسالة واحدة فيها 3-7 فرص
    lines = []
    lines.append("📌 فرص أثناء السوق (SL 3% / TP 5%)")
    lines.append(f"عدد الفرص: {len(fresh)}")
    lines.append("—")
    for i, p in enumerate(fresh, 1):
        lines.append(
            f"{i}) {p['symbol']} | Δ يومي: {p['chg_pct']}% | AvgVol: {p['avg_vol']}\n"
            f"Entry: {p['entry']}\n"
            f"SL (-{STOP_LOSS_PCT}%): {p['sl']}\n"
            f"TP (+{TAKE_PROFIT_PCT}%): {p['tp']}\n"
            "—"
        )

    ok, info = send_telegram("\n".join(lines))
    if ok:
        for p in fresh:
            _state["sent_symbols"].add(p["symbol"])

    return jsonify({"ok": ok, "info": info, "sent": len(fresh)}), (200 if ok else 500)
