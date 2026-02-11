import os
import json
import math
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests

# ===== Telegram control imports =====
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

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

# قناة/جروب الإشعارات (TradingView + scan)
TELEGRAM_CHAT_ID = getenv_any(
    ["TELEGRAM_CHAT_ID", "CHAT_ID", "TG_CHAT_ID", "TELEGRAM_USER_ID"],
    ""
)

# أدمن التحكم من الخاص
ADMIN_USER_ID = getenv_any(["ADMIN_USER_ID", "TG_ADMIN_ID"], "").strip()

WEBHOOK_SECRET = getenv_any(["WEBHOOK_SECRET", "TV_SECRET", "TRADINGVIEW_SECRET", "SECRET_KEY"], "")
RUN_KEY = getenv_any(["RUN_KEY", "SCAN_KEY", "CRON_KEY", "JOB_KEY"], "")

# Telegram webhook secret (for /tg endpoint)
TELEGRAM_WEBHOOK_SECRET = getenv_any(["TELEGRAM_WEBHOOK_SECRET", "TG_WEBHOOK_SECRET"], "").strip()

# Scanner settings (legacy - still used in /scan)
STOP_LOSS_PCT = getenv_float_any(["STOP_LOSS_PCT", "SL_PCT"], 3)
TAKE_PROFIT_PCT = getenv_float_any(["TAKE_PROFIT_PCT", "TP_PCT"], 5)
MAX_RESULTS = getenv_int_any(["MAX_RESULTS", "MAX_PICKS"], 7)
MIN_PRICE = getenv_float_any(["MIN_PRICE"], 2)
MAX_PRICE = getenv_float_any(["MAX_PRICE"], 300)
MIN_AVG_VOL = getenv_int_any(["MIN_AVG_VOL", "MIN_VOLUME"], 1_500_000)

# Cooldown minutes for duplicate alerts (TradingView)
ALERT_COOLDOWN_MIN = getenv_int_any(["ALERT_COOLDOWN_MIN"], 60)

_state = {"day_key": None, "sent_symbols": set()}

# Settings persistence (capital, risk, side...)
DEFAULT_SETTINGS = {
    "capital": 10000.0,
    "risk_pct": 1.0,    # %
    "side": "both",     # long / short / both
    "filter_mode": "enter_only",  # enter_only | enter_wait
    "cooldown_min": ALERT_COOLDOWN_MIN
}
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.json")
_settings_cache = None

def load_settings():
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
    s = DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                s.update(data)
    except Exception:
        pass
    _settings_cache = s
    return s

def save_settings(s: dict):
    global _settings_cache
    _settings_cache = s
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

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
def send_telegram(text: str, chat_id: str | None = None):
    if not TELEGRAM_BOT_TOKEN:
        return False, "Missing TELEGRAM_BOT_TOKEN"

    target = chat_id if chat_id is not None else TELEGRAM_CHAT_ID
    if not target:
        return False, "Missing TELEGRAM_CHAT_ID"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": target, "text": text}, timeout=20)
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

# ================= Indicators (for /analyze) =================
def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n

def rsi_14(closes):
    if len(closes) < 15:
        return None
    gains = []
    losses = []
    for i in range(-14, 0):
        diff = closes[i] - closes[i-1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = sum(gains) / 14.0
    avg_loss = sum(losses) / 14.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def atr_14(highs, lows, closes):
    if len(closes) < 15 or len(highs) < 15 or len(lows) < 15:
        return None
    trs = []
    for i in range(-14, 0):
        h = highs[i]
        l = lows[i]
        prev_c = closes[i-1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return sum(trs) / 14.0

def compute_position_size(capital, risk_pct, entry, sl):
    risk_dollars = float(capital) * (float(risk_pct) / 100.0)
    per_share = abs(float(entry) - float(sl))
    if per_share <= 0:
        return 0
    return max(int(risk_dollars / per_share), 0)

# ====== NEW: Symbol normalize + Yahoo Chart fallback ======
def normalize_symbol(sym: str) -> str:
    s = sym.strip().upper()
    if s in ("SPX", "SP500", "S&P500"):
        return "^GSPC"
    if s in ("NDX", "NASDAQ100"):
        return "^NDX"
    if s in ("DJI", "DOW"):
        return "^DJI"
    return s

def fetch_history_yahoo_chart(symbol: str, range_="6mo", interval="1d"):
    symbol = normalize_symbol(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": range_, "interval": interval, "includePrePost": "false"}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Connection": "keep-alive",
    }

    try:
        r = requests.get(url, params=params, headers=headers, timeout=25)
    except Exception as e:
        return {"ok": False, "error": f"chart request failed: {e}"}

    if r.status_code != 200:
        return {"ok": False, "error": f"chart http {r.status_code}: {r.text[:200]}"}

    try:
        data = r.json()
    except Exception:
        return {"ok": False, "error": "chart bad json"}

    try:
        result = data["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        closes = [float(x) for x in quote.get("close", []) if x is not None]
        highs  = [float(x) for x in quote.get("high", []) if x is not None]
        lows   = [float(x) for x in quote.get("low", []) if x is not None]
    except Exception:
        return {"ok": False, "error": "chart parse failed"}

    if len(closes) < 60 or len(highs) < 60 or len(lows) < 60:
        return {"ok": False, "error": f"chart not enough data (closes={len(closes)})"}

    return {"ok": True, "closes": closes, "highs": highs, "lows": lows, "symbol": symbol}

# ====== UPDATED: analyze_symbol uses yfinance then fallback ======
def analyze_symbol(symbol: str):
    symbol = normalize_symbol(symbol)

    closes = highs = lows = None
    used_source = ""

    # 1) try yfinance
    if yf is not None:
        try:
            df = yf.download(symbol, period="6mo", interval="1d", auto_adjust=True, progress=False)
            if df is not None and (not df.empty) and len(df) >= 60:
                closes = [float(x) for x in df["Close"].dropna().tolist()]
                highs  = [float(x) for x in df["High"].dropna().tolist()]
                lows   = [float(x) for x in df["Low"].dropna().tolist()]
                if len(closes) >= 60 and len(highs) >= 60 and len(lows) >= 60:
                    used_source = "yfinance"
        except Exception:
            pass

    # 2) fallback to yahoo chart API
    if closes is None:
        ch = fetch_history_yahoo_chart(symbol, range_="6mo", interval="1d")
        if not ch.get("ok"):
            return {"ok": False, "error": ch.get("error", "not enough data")}
        closes, highs, lows = ch["closes"], ch["highs"], ch["lows"]
        symbol = ch.get("symbol", symbol)
        used_source = "yahoo_chart"

    entry = closes[-1]
    ma20 = sma(closes, 20)
    ma50 = sma(closes, 50)
    rsi = rsi_14(closes)
    atr = atr_14(highs, lows, closes)

    if ma20 is None or ma50 is None or rsi is None or atr is None:
        return {"ok": False, "error": "indicator calc failed"}

    trend = "up" if entry > ma50 and ma20 > ma50 else ("down" if entry < ma50 and ma20 < ma50 else "neutral")

    last20 = closes[-21:-1]
    breakout_up = entry > max(last20) if len(last20) >= 20 else False
    breakout_down = entry < min(last20) if len(last20) >= 20 else False

    s = load_settings()
    capital = float(s.get("capital", 10000.0))
    risk_pct = float(s.get("risk_pct", 1.0))
    side = str(s.get("side", "both")).lower()

    # Swing ATR multipliers
    sl_mult = 1.5
    tp1_mult = 1.5
    tp2_mult = 3.0

    ideas = []

    # LONG
    if side in ("both", "long"):
        sl = entry - sl_mult * atr
        tp1 = entry + tp1_mult * atr
        tp2 = entry + tp2_mult * atr
        qty = compute_position_size(capital, risk_pct, entry, sl)

        score = 0
        reasons = []
        if trend == "up":
            score += 3; reasons.append("Trend up")
        if breakout_up:
            score += 3; reasons.append("Breakout 20D")
        if 50 <= rsi <= 72:
            score += 2; reasons.append("RSI 50-72")
        if rsi > 72:
            reasons.append("RSI high (pullback risk)")
        if trend == "down":
            reasons.append("Trend down (weak long)")

        decision = "ENTER" if score >= 6 else ("WAIT" if score >= 4 else "SKIP")

        ideas.append({
            "side": "LONG",
            "decision": decision,
            "score": score,
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "qty": qty,
            "reasons": reasons
        })

    # SHORT
    if side in ("both", "short"):
        sl = entry + sl_mult * atr
        tp1 = entry - tp1_mult * atr
        tp2 = entry - tp2_mult * atr
        qty = compute_position_size(capital, risk_pct, entry, sl)

        score = 0
        reasons = []
        if trend == "down":
            score += 3; reasons.append("Trend down")
        if breakout_down:
            score += 3; reasons.append("Breakdown 20D")
        if 28 <= rsi <= 50:
            score += 2; reasons.append("RSI 28-50")
        if rsi < 28:
            reasons.append("RSI very low (bounce risk)")
        if trend == "up":
            reasons.append("Trend up (weak short)")

        decision = "ENTER" if score >= 6 else ("WAIT" if score >= 4 else "SKIP")

        ideas.append({
            "side": "SHORT",
            "decision": decision,
            "score": score,
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "qty": qty,
            "reasons": reasons
        })

    return {
        "ok": True,
        "symbol": symbol,
        "source": used_source,
        "entry": entry,
        "trend": trend,
        "ma20": ma20,
        "ma50": ma50,
        "rsi": rsi,
        "atr": atr,
        "ideas": ideas
    }

# ================= Scanner (legacy) =================
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
                if "Close" in getattr(df, "columns", []):
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
_tg_initialized = False

# in-memory cooldown map
_last_alert_ts = {}

def _ensure_tg_initialized():
    global _tg_initialized
    if tg_app and not _tg_initialized:
        asyncio.run(tg_app.initialize())
        _tg_initialized = True

if TELEGRAM_BOT_TOKEN:
    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

def _is_admin(update: Update) -> bool:
    try:
        if not ADMIN_USER_ID:
            return True
        return str(update.effective_user.id) == str(ADMIN_USER_ID)
    except Exception:
        return False

def _menu():
    s = load_settings()
    kb = [
        [InlineKeyboardButton("📊 Scan الآن (أرسل للقناة)", callback_data="scanrun")],
        [InlineKeyboardButton("🧠 Analyze سهم", callback_data="help_analyze")],
        [InlineKeyboardButton("💰 Capital +1000", callback_data="cap_plus"),
         InlineKeyboardButton("💰 Capital -1000", callback_data="cap_minus")],
        [InlineKeyboardButton("⚠️ Risk +0.25%", callback_data="risk_plus"),
         InlineKeyboardButton("⚠️ Risk -0.25%", callback_data="risk_minus")],
        [InlineKeyboardButton("📈 Long", callback_data="side_long"),
         InlineKeyboardButton("📉 Short", callback_data="side_short"),
         InlineKeyboardButton("🔁 Both", callback_data="side_both")],
        [InlineKeyboardButton("🔔 Filter: ENTER فقط", callback_data="filter_enter_only"),
         InlineKeyboardButton("🔔 ENTER+WAIT", callback_data="filter_enter_wait")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="show_settings")]
    ]
    text = (
        "⚙️ Settings\n"
        f"Capital: {s['capital']}$ | Risk: {s['risk_pct']}% | Side: {s['side']}\n"
        f"TV Filter: {s.get('filter_mode','enter_only')} | Cooldown: {s.get('cooldown_min',60)}m\n"
        "أوامر: /analyze AAPL | /capital 25000 | /risk 1 | /scanrun"
    )
    return text, InlineKeyboardMarkup(kb)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("⛔ غير مصرح.")
    text, markup = _menu()
    await update.message.reply_text(text, reply_markup=markup)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("⛔ غير مصرح.")
    await update.message.reply_text(
        "✅ الأوامر:\n"
        "/start\n"
        "/analyze AAPL\n"
        "/scanrun (يرسل للقناة)\n"
        "/capital 25000\n"
        "/risk 1\n"
        "/status\n"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("⛔ غير مصرح.")
    s = load_settings()
    await update.message.reply_text(
        "📌 Status\n"
        f"Market open: {market_open_now_et()}\n"
        f"Capital: {s['capital']}$ | Risk: {s['risk_pct']}% | Side: {s['side']}\n"
        f"TV Filter: {s.get('filter_mode','enter_only')} | Cooldown: {s.get('cooldown_min',60)}m\n"
        f"Universe size: {len(load_universe())}\n"
    )

async def cmd_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("⛔ غير مصرح.")
    if not context.args:
        return await update.message.reply_text("استخدم: /capital 25000")
    s = load_settings()
    s["capital"] = float(context.args[0])
    save_settings(s)
    text, markup = _menu()
    await update.message.reply_text(f"✅ تم ضبط رأس المال.\n{text}", reply_markup=markup)

async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("⛔ غير مصرح.")
    if not context.args:
        return await update.message.reply_text("استخدم: /risk 1.0  (يعني 1%)")
    s = load_settings()
    s["risk_pct"] = float(context.args[0])
    save_settings(s)
    text, markup = _menu()
    await update.message.reply_text(f"✅ تم ضبط المخاطرة.\n{text}", reply_markup=markup)

async def cmd_scanrun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
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

    lines = [f"📈 فرص اليوم (Legacy Scan) (SL {STOP_LOSS_PCT}% | TP {TAKE_PROFIT_PCT}%):"]
    for p in picks[:MAX_RESULTS]:
        lines.append(f"- {p['symbol']} | Entry {p['entry']:.2f} | SL {p['sl']:.2f} | TP {p['tp']:.2f}")

    ok, info = send_telegram("\n".join(lines))
    await update.message.reply_text(f"✅ تم الإرسال للقناة.\n({info})")

async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("⛔ غير مصرح.")
    if not context.args:
        return await update.message.reply_text("استخدم: /analyze AAPL")

    sym = context.args[0].upper()
    res = analyze_symbol(sym)
    if not res.get("ok"):
        return await update.message.reply_text(f"⚠️ خطأ: {res.get('error')}")

    s = load_settings()
    lines = []
    lines.append(f"🧠 Analyze {res['symbol']} (Swing)")
    lines.append(f"Source: {res.get('source','?')}")
    lines.append(f"Entry: {res['entry']:.2f}")
    lines.append(f"Trend: {res['trend']} | MA20 {res['ma20']:.2f} | MA50 {res['ma50']:.2f}")
    lines.append(f"RSI(14): {res['rsi']:.1f} | ATR(14): {res['atr']:.2f}")
    lines.append(f"Capital: {s['capital']}$ | Risk: {s['risk_pct']}% | Side: {s['side']}")
    lines.append("—")

    for idea in res["ideas"]:
        emoji = "✅" if idea["decision"] == "ENTER" else ("⚠️" if idea["decision"] == "WAIT" else "⛔")
        lines.append(f"{emoji} {idea['side']} | {idea['decision']} | Score {idea['score']}/8")
        lines.append(f"SL: {idea['sl']:.2f} | TP1: {idea['tp1']:.2f} | TP2: {idea['tp2']:.2f}")
        lines.append(f"Qty (risk-based): {idea['qty']}")
        if idea["reasons"]:
            lines.append("• " + " | ".join(idea["reasons"]))
        lines.append("—")

    await update.message.reply_text("\n".join(lines))

def _cooldown_ok(symbol: str, direction: str) -> bool:
    s = load_settings()
    cooldown_min = int(s.get("cooldown_min", ALERT_COOLDOWN_MIN))
    key = f"{symbol}|{direction}".upper()
    now = datetime.utcnow()
    last = _last_alert_ts.get(key)
    if last is None:
        _last_alert_ts[key] = now
        return True
    if (now - last) >= timedelta(minutes=cooldown_min):
        _last_alert_ts[key] = now
        return True
    return False

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    q = update.callback_query
    await q.answer()

    s = load_settings()
    data = q.data

    if data == "help_analyze":
        text, markup = _menu()
        return await q.edit_message_text("اكتب: /analyze AAPL (في الخاص)\nثم راح يعطيك قرار + SL/TP ATR + كمية.", reply_markup=markup)

    if data == "scanrun":
        text, markup = _menu()
        return await q.edit_message_text("لتشغيل Scan وإرساله للقناة اكتب: /scanrun", reply_markup=markup)

    if data == "cap_plus":
        s["capital"] = float(s["capital"]) + 1000.0
    elif data == "cap_minus":
        s["capital"] = max(0.0, float(s["capital"]) - 1000.0)
    elif data == "risk_plus":
        s["risk_pct"] = round(float(s["risk_pct"]) + 0.25, 2)
    elif data == "risk_minus":
        s["risk_pct"] = round(max(0.0, float(s["risk_pct"]) - 0.25), 2)
    elif data == "side_long":
        s["side"] = "long"
    elif data == "side_short":
        s["side"] = "short"
    elif data == "side_both":
        s["side"] = "both"
    elif data == "filter_enter_only":
        s["filter_mode"] = "enter_only"
    elif data == "filter_enter_wait":
        s["filter_mode"] = "enter_wait"
    elif data == "show_settings":
        text, markup = _menu()
        return await q.edit_message_text(text, reply_markup=markup)

    save_settings(s)
    text, markup = _menu()
    await q.edit_message_text(f"✅ تم التحديث\n{text}", reply_markup=markup)

if tg_app:
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_help))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(CommandHandler("capital", cmd_capital))
    tg_app.add_handler(CommandHandler("risk", cmd_risk))
    tg_app.add_handler(CommandHandler("scanrun", cmd_scanrun))
    tg_app.add_handler(CommandHandler("analyze", cmd_analyze))
    tg_app.add_handler(CallbackQueryHandler(on_button))

@app.post("/tg")
def telegram_webhook():
    if not tg_app:
        return jsonify({"ok": False, "error": "telegram not configured"}), 500

    secret = request.args.get("secret", "").strip()
    if TELEGRAM_WEBHOOK_SECRET and secret != TELEGRAM_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "bad secret"}), 403

    _ensure_tg_initialized()

    data = request.get_json(force=True, silent=True) or {}
    update = Update.de_json(data, tg_app.bot)
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
    direction = (payload.get("direction") or payload.get("action") or payload.get("side") or payload.get("d") or "SIGNAL")
    reason = payload.get("reason") or payload.get("message") or payload.get("r") or "TV Alert"

    # ---- Pro filter (analyze + cooldown) ----
    dir_norm = str(direction).upper()
    if dir_norm in ("BUY", "LONG"):
        dir_norm = "BUY"
    elif dir_norm in ("SELL", "SHORT"):
        dir_norm = "SELL"

    if dir_norm in ("BUY", "SELL"):
        if not _cooldown_ok(ticker, dir_norm):
            return jsonify({"ok": True, "ignored": "cooldown"}), 200

    s = load_settings()
    filter_mode = s.get("filter_mode", "enter_only")

    decision_note = ""
    if dir_norm in ("BUY", "SELL"):
        res = analyze_symbol(ticker)
        if res.get("ok"):
            want_side = "LONG" if dir_norm == "BUY" else "SHORT"
            idea = next((x for x in res["ideas"] if x["side"] == want_side), None)
            if idea:
                decision_note = f"{idea['decision']} | Score {idea['score']}/8 | SL {idea['sl']:.2f} TP1 {idea['tp1']:.2f} TP2 {idea['tp2']:.2f} Qty {idea['qty']}"
                if filter_mode == "enter_only" and idea["decision"] != "ENTER":
                    send_telegram(
                        f"⛔ Filtered TV Alert ({want_side})\n{ticker} {tf}\nDecision: {idea['decision']}\n{decision_note}\nReason: {reason}",
                        chat_id=ADMIN_USER_ID if ADMIN_USER_ID else None
                    )
                    return jsonify({"ok": True, "filtered": idea["decision"]}), 200

                if filter_mode == "enter_wait" and idea["decision"] == "SKIP":
                    send_telegram(
                        f"⛔ Filtered TV Alert ({want_side})\n{ticker} {tf}\nDecision: SKIP\n{decision_note}\nReason: {reason}",
                        chat_id=ADMIN_USER_ID if ADMIN_USER_ID else None
                    )
                    return jsonify({"ok": True, "filtered": "SKIP"}), 200

    msg = (
        "📣 TradingView Alert\n"
        f"Ticker: {ticker}\n"
        f"TF: {tf}\n"
        f"Direction: {dir_norm}\n"
        f"Price: {price}\n"
        f"Reason: {reason}\n"
    )
    if decision_note:
        msg += f"—\n🧠 Analyze: {decision_note}\n"

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

    lines = [f"📌 Market Picks (Legacy Scan) (SL {STOP_LOSS_PCT}% | TP {TAKE_PROFIT_PCT}%)", f"Count: {len(fresh)}", "—"]
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
