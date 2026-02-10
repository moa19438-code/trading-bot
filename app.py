from flask import Flask, request
import requests
import os

app = Flask(__name__)

TOKEN = "7333036344:AAFAgWQf48Dr83ZrOq4UvtCfDo1kpCUQntA"
CHAT_ID = "1750462226"


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": text
    }
    requests.post(url, data=data)


@app.route("/")
def home():
    return "OK"


@app.route("/test")
def test():
    send_telegram("✅ البوت شغال بنجاح")
    return "sent"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    action = data.get("action", "signal")
    symbol = data.get("symbol", "UNKNOWN")
    price = data.get("price", "0")

    msg = f"📊 إشارة جديدة\n\nالنوع: {action}\nالرمز: {symbol}\nالسعر: {price}"
    send_telegram(msg)

    return "done"


if __name__ == "__main__":
    app.run()
