import os
from datetime import datetime
from flask import Flask, request, jsonify
import requests

# ===== Telegram control imports =====
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

app = Flask(__name__)

# ================= Env helpers =================
def getenv_any(names, default=""):
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
TELEGRAM_BOT_TOKEN = getenv_any(
    ["TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TG_BOT_TOKEN", "TELEGRAM_TOKEN"],
    ""
)
TELEGRAM_CHAT_ID = getenv_any(
    ["TELEGRAM_CHAT_ID", "CHAT_ID", "TG_CHAT_ID", "TELEGRAM_USER_ID"],
    ""
)

WEBHOOK_SECRET = getenv_any(["WEBHOOK_SECRET", "TV_SECRET", "TRADINGVIEW_SECRET", "SECRET_KEY"], "")
RUN_KEY = getenv_any(["RUN_KEY", "SCAN_KEY", "CRON_KEY", "JOB_KEY"], "")

# Telegram webhook secret (for /tg endpoint)
TELEGRAM_WEBHOOK_SECRET = getenv_any(["TELEGRAM_WEBHOOK_SECRET", "TG_WEBHOOK_SECRET"], "").strip()

# Scanner settings
STOP_LOSS_PCT = getenv_float_any(["STOP_LOSS_PCT", "SL_PCT"], 3)
TAKE_PROFIT_PCT = getenv_float_any(["TAKE_PROFIT_PCT", "TP_PCT"], 5)
MAX_RESULTS = getenv_int_any(["MAX_RESULTS", "MAX_PICKS"], 7)
MIN_PRICE = getenv_float_any(["MIN_PRICE"], 2)
MAX_PRICE = getenv_float_any(["MAX_PRICE"], 300)
MIN_AVG_VOL = getenv_int_any(["MIN_AVG_VOL", "MIN_VOLUME"], 1_500_000)

_state = {"day_key": None, "sent_symbols": set()}

# Timezone ET
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

# ================= Telegram sendMessage =================
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
                # Multi-index vs single
                if isinstance(df.columns, list) or "Close" in getattr(df, "columns", []):
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

# ================= Telegram Command Bot (Webhook) =================
tg_app = None
if TELEGRAM_BOT_TOKEN:
    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

def _chat_allowed(update: Update) -> bool:
    """
    لو TELEGRAM_CHAT_ID موجود، نخلي التحكم فقط من نفس الشات.
    """
    if not TELEGRAM_CHAT_ID:
        return True
    try:
        return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)
    except Exception:
        return False

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _chat_allowed(update):
        return await update.message.reply_text("⛔ غير مصرح.")
    await update.message.reply_text("✅ تم. اكتب /scanrun لتشغيل السكان الآن.\nوأوامر الإعدادات: /setsl 3  |  /settp 5")

async def cmd_scanrun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _chat_allowed(update):
        return await update.message.reply_text("⛔ غير مصرح.")

    reset_day()

    if not market_open_now_et():
        return await update.message.reply_text("ℹ️ السوق مغلق الآن (America/New_York).")

    universe = load_universe()
    if not universe:
        return await update.message.reply_text("⚠️ tickers.txt غير موجود أو فاضي.")

    picks, _ = scan_universe(universe)
    if not picks:
        return await update.message.reply_text("ما فيه فرص حالياً.")

    lines = [f"📈 فرص اليوم (SL {STOP_LOSS_PCT}% | TP {TAKE_PROFIT_PCT}%):"]
    for p in picks[:MAX_RESULTS]:
        lines.append(f"- {p['symbol']} | Entry {p['entry']:.2f} | SL {p['sl']:.2f} | TP {p['tp']:.2f}")
    await update.message.reply_text("\n".join(lines))

async def cmd_setsl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global STOP_LOSS_PCT
    if not _chat_allowed(update):
        return await update.message.reply_text("⛔ غير مصرح.")
    if not context.args:
        return await update.message.reply_text(f"SL الحالي: {STOP_LOSS_PCT}%\nاستخدم: /setsl 3")
    STOP_LOSS_PCT = float(context.args[0])
    await update.message.reply_text(f"✅ تم ضبط SL إلى {STOP_LOSS_PCT}%")

async def cmd_settp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TAKE_PROFIT_PCT
    if not _chat_allowed(update):
        return await update.message.reply_text("⛔ غير مصرح.")
    if not context.args:
        return await update.message.reply_text(f"TP الحالي: {TAKE_PROFIT_PCT}%\nاستخدم: /settp 5")
    TAKE_PROFIT_PCT = float(context.args[0])
    await update.message.reply_text(f"✅ تم ضبط TP إلى {TAKE_PROFIT_PCT}%")

if tg_app:
    tg_app.add_handler(CommandHandler("scan", cmd_scan))
    tg_app.add_handler(CommandHandler("scanrun", cmd_scanrun))
    tg_app.add_handler(CommandHandler("setsl", cmd_setsl))
    tg_app.add_handler(CommandHandler("settp", cmd_settp))

@app.post("/tg")
def telegram_webhook():
    if not tg_app:
        return jsonify({"ok": False, "error": "telegram not configured"}), 500

    secret = request.args.get("secret", "").strip()
    if TELEGRAM_WEBHOOK_SECRET and secret != TELEGRAM_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "bad secret"}), 403

    data = request.get_json(force=True, silent=True) or {}
    update = Update.de_json(data, tg_app.bot)

    # تشغيل async بشكل آمن داخل Flask
    asyncio.run(tg_app.process_update(update))
    return jsonify({"ok": True})

# ================= Endpoints =================
@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "service": "trading-bot",
        "endpoints": ["/test", "/webhook", "/tv", "/scan", "/tg"]
    })

@app.get("/test")
def test():
    ok, info = send_telegram("✅ Test: البوت شغال ويرسل تيليجرام بنجاح.")
    return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

def handle_tradingview(payload: dict):
    if WEBHOOK_SECRET:
        incoming = str(payload.get("secret", "")).strip()
        if incoming != WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "bad secret"}), 401

    ticker = payload.get("ticker") or payload.get("symbol") or payload.get("s") or payload.get("tv_ticker") or "UNKNOWN"
    price = payload.get("price") or payload.get("close") or payload.get("last") or payload.get("p") or ""
    tf = payload.get("tf") or payload.get("timeframe") or payload.get("interval") or payload.get("i") or ""
    direction = payload.get("direction") or payload.get("action") or payload.get("side") or payload.get("d") or "SIGNAL"
    reason = payload.get("reason") or payload.get("message") or payload.get("r") or "TV Alert"

    msg = (
        "📣 تنبيه TradingView\n"
        f"السهم: {ticker}\n"
        f"الفريم: {tf}\n"
        f"الاتجاه: {direction}\n"
        f"السعر: {price}\n"
        f"السبب: {reason}\n"
        "—\n"
        "📣 TradingView Alert\n"
        f"Ticker: {ticker}\n"
        f"TF: {tf}\n"
        f"Direction: {direction}\n"
        f"Price: {price}\n"
        f"Reason: {reason}\n"
    )

    ok, info = send_telegram(msg)
    return jsonify({"ok": ok, "info": info, "received": payload}), (200 if ok else 500)

@app.route("/webhook", methods=["GET", "POST"], strict_slashes=False)
def webhook():
    if request.method == "GET":
        return jsonify({"ok": True, "info": "webhook is alive"}), 200

    payload = request.get_json(silent=True) or {}
    if not payload and request.data:
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            payload = {}

    print("=== WEBHOOK HIT ===", payload)
    return handle_tradingview(payload)

@app.route("/tv", methods=["GET", "POST"], strict_slashes=False)
def tv():
    return webhook()

@app.get("/scan")
def scan():
    key = request.args.get("key", "").strip()
    if not RUN_KEY or key != RUN_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    reset_day()

    if not market_open_now_et():
        return jsonify({"ok": True, "ignored": "market_closed"}), 200

    universe = load_universe()
    if not universe:
        ok, info = send_telegram("⚠️ tickers.txt غير موجود أو فاضي.")
        return jsonify({"ok": ok, "info": info}), (200 if ok else 500)

    picks, status = scan_universe(universe)
    if not picks:
        return jsonify({"ok": True, "status": status, "message": "no picks"}), 200

    fresh = []
    for p in picks:
        if p["symbol"] not in _state["sent_symbols"]:
            fresh.append(p)
        if len(fresh) >= MAX_RESULTS:
            break

    if not fresh:
        return jsonify({"ok": True, "message": "no new symbols"}), 200

    lines = [f"📌 Market Picks (SL {STOP_LOSS_PCT}% | TP {TAKE_PROFIT_PCT}%)", f"Count: {len(fresh)}", "—"]
    for i, p in enumerate(fresh, 1):
        lines.append(
            f"{i}) {p['symbol']} | Daily: {p['chg_pct']}% | AvgVol: {p['avg_vol']}\n"
            f"Entry: {p['entry']}\n"
            f"SL: {p['sl']}\n"
            f"TP: {p['tp']}\n"
            "—"
        )

    ok, info = send_telegram("\n".join(lines))
    if ok:
        for p in fresh:
            _state["sent_symbols"].add(p["symbol"])

    return jsonify({"ok": ok, "info": info, "sent": len(fresh)}), (200 if ok else 500)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
