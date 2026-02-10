import os
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ====== Environment Variables (set in Render) ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
RUN_KEY = os.getenv("RUN_KEY", "").strip()

# Trading rules
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "3"))   # 3%
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "5"))  # 5%

# Daily scan settings
MIN_RESULTS = int(os.getenv("MIN_RESULTS", "3"))   # send at least 3 if available
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "7"))   # send max 7
MIN_PRICE = float(os.getenv("MIN_PRICE", "2"))     # ignore very cheap
MAX_PRICE = float(os.getenv("MAX_PRICE", "300"))   # ignore very expensive (optional)
MIN_AVG_VOL = int(os.getenv("MIN_AVG_VOL", "1500000"))  # liquidity filter
ENABLE_TIME_WINDOW = os.getenv("ENABLE_TIME_WINDOW", "0").strip() == "1"

# Timezone (optional)
try:
    import pytz
    ET = pytz.timezone("America/New_York")
except Exception:
    ET = None

# yfinance scan (optional)
USE_YFINANCE_SCAN = os.getenv("USE_YFINANCE_SCAN", "1").strip() == "1"
try:
    import yfinance as yf
except Exception:
    yf = None

# ====== Helpers ======
def send_telegram(text: str):
    """Send a Telegram message and return (ok, info)."""
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

def within_market_window():
    """Optional: only allow during US regular session (ET)."""
    if not ENABLE_TIME_WINDOW or ET is None:
        return True
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= now <= end

def calc_levels(entry: float):
    sl = entry * (1 - STOP_LOSS_PCT / 100.0)
    tp = entry * (1 + TAKE_PROFIT_PCT / 100.0)
    return round(sl, 4), round(tp, 4)

# ====== Web endpoints ======
@app.get("/")
def home():
    return "OK"

@app.get("/test")
def test():
    ok, info = send_telegram("✅ Test: البوت شغال ويرسل تيليجرام بنجاح")
    return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

def handle_tradingview(payload: dict):
    # Secret check
    if WEBHOOK_SECRET and str(payload.get("secret", "")).strip() != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "bad secret"}), 401

    ticker = payload.get("ticker") or payload.get("symbol") or "UNKNOWN"
    price = payload.get("price") or payload.get("close") or ""
    direction = payload.get("direction") or payload.get("action") or "SIGNAL"
    tf = payload.get("tf") or payload.get("timeframe") or ""
    reason = payload.get.get("reason") if isinstance(payload, dict) else None

    msg = f"📣 تنبيه TradingView\nالسهم: {ticker}\nالفريم: {tf}\nالاتجاه: {direction}\nالسعر: {price}"
    if reason:
        msg += f"\nسبب: {reason}"

    ok, info = send_telegram(msg)
    return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

@app.post("/tv")
def tv():
    payload = request.get_json(silent=True) or {}
    if not within_market_window():
        return jsonify({"ok": True, "ignored": "outside_time_window"}), 200
    return handle_tradingview(payload)

@app.post("/webhook")
def webhook():
    payload = request.get_json(silent=True) or {}
    if not within_market_window():
        return jsonify({"ok": True, "ignored": "outside_time_window"}), 200
    return handle_tradingview(payload)

# ====== Daily scanner ======
def load_universe():
    """Load tickers from tickers.txt."""
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
    return list(dict.fromkeys(tickers))  # unique preserve order

def scan_market_yfinance(tickers):
    """
    Practical scan: not literally 'whole US market' (that needs paid data),
    but scans a large universe list and selects best 3–7 based on momentum + liquidity.
    """
    if yf is None:
        return [], "yfinance not installed"

    results = []
    # We fetch in chunks to reduce failures
    chunk_size = 50
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i+chunk_size]
        # Download last 5 days daily data
        try:
            df = yf.download(
                tickers=" ".join(chunk),
                period="5d",
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False
            )
        except Exception:
            continue

        for t in chunk:
            try:
                # yfinance format differs for single vs multi ticker
                if isinstance(df.columns, type(getattr(df, "columns", None))) and "Close" in df.columns:
                    # single ticker fallback
                    closes = df["Close"].dropna()
                    vols = df["Volume"].dropna()
                else:
                    closes = df[(t, "Close")].dropna()
                    vols = df[(t, "Volume")].dropna()

                if len(closes) < 2 or len(vols) < 2:
                    continue

                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2])
                chg_pct = ((last - prev) / prev) * 100.0

                avg_vol = int(vols.tail(5).mean())

                if last < MIN_PRICE or last > MAX_PRICE:
                    continue
                if avg_vol < MIN_AVG_VOL:
                    continue

                # Score: daily change + log(volume) influence
                score = chg_pct + (avg_vol / 10_000_000)  # simple

                sl, tp = calc_levels(last)
                results.append({
                    "ticker": t,
                    "entry": round(last, 4),
                    "sl": sl,
                    "tp": tp,
                    "chg_pct": round(chg_pct, 2),
                    "avg_vol": avg_vol,
                    "score": score
                })
            except Exception:
                continue

    # Sort by score desc
    results.sort(key=lambda x: x["score"], reverse=True)
    return results, "ok"

@app.get("/run")
def run_daily():
    # Protect endpoint
    key = request.args.get("key", "").strip()
    if not RUN_KEY or key != RUN_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    # Optional: run only in market window (you can disable)
    # if not within_market_window(): ...

    universe = load_universe()
    if not universe:
        ok, info = send_telegram("⚠️ ما لقيت tickers.txt أو القائمة فاضية.")
        return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

    if not USE_YFINANCE_SCAN:
        ok, info = send_telegram("ℹ️ الفحص (scan) مقفّل. فعّل USE_YFINANCE_SCAN=1.")
        return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

    picks, status = scan_market_yfinance(universe)

    if not picks:
        ok, info = send_telegram("📉 اليوم ما فيه فرص مناسبة حسب الفلاتر الحالية.")
        return jsonify({"ok": ok, "info": info, "status": status}), (200 if ok else 500)

    # pick 3–7
    final = picks[:MAX_RESULTS]
    if len(final) < MIN_RESULTS:
        # still send whatever exists
        pass

    # Build message
    lines = []
    lines.append("📌 قائمة فرص اليوم (3% وقف / 5% هدف)")
    lines.append(f"عدد الأسهم المفحوصة: {len(universe)}")
    lines.append(f"أفضل النتائج: {len(final)}")
    lines.append("—")

    for idx, p in enumerate(final, 1):
        lines.append(
            f"{idx}) {p['ticker']} | Δ يومي: {p['chg_pct']}% | AvgVol: {p['avg_vol']}\n"
            f"Entry: {p['entry']}\n"
            f"SL (-{STOP_LOSS_PCT}%): {p['sl']}\n"
            f"TP (+{TAKE_PROFIT_PCT}%): {p['tp']}\n"
            f"—"
        )

    ok, info = send_telegram("\n".join(lines))
    return jsonify({"ok": ok, "info": info, "sent": len(final)}), (200 if ok else 500)
