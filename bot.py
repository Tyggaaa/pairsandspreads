#!/usr/bin/env python3
# bot.py — обновлённый Telegram-бот, использует analysis.json
import requests
import time
import json
import os
import threading
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUBSCRIBERS_FILE = "subscribers.txt"

PAIRS_FILE = "pairs.json"
STATE_FILE = "state.json"
ANALYSIS_FILE = "analysis.json"

CHECK_INTERVAL = 10
STATUS_EMOJI = "🐬"

# ---------------- helpers ----------------
def load_pairs():
    with open(PAIRS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

pairs = load_pairs()
state = load_json(STATE_FILE, {})
# ensure cycles structure exists
if "cycles" not in state:
    state["cycles"] = {}
    for p1, info in pairs.items():
        key = f"{p1}-{info['pair2']}"
        if key not in state:
            state[key] = "inactive"
        if key not in state["cycles"]:
            state["cycles"][key] = []
    save_json(STATE_FILE, state)

# --------------- Binance API ---------------
def get_price(symbol, retries=3, delay=0.25):
    url = f"https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol={symbol}"
    for _ in range(retries):
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            if "bidPrice" not in data or "askPrice" not in data:
                time.sleep(delay)
                continue
            bid = float(data["bidPrice"]); ask = float(data["askPrice"])
            return (bid + ask) / 2.0
        except:
            time.sleep(delay)
    return None

def calc_spread(p1, p2):
    try:
        return abs(p1 - p2) / ((p1 + p2) / 2) * 100
    except:
        return 0.0

def fmt_coef(coef):
    try:
        if abs(coef - int(coef)) < 1e-9:
            return str(int(coef))
    except:
        pass
    return f"{coef:.2f}"

def get_direction_names(p1_name, p1_price, p2_name, p2_price):
    if p1_price > p2_price:
        return p2_name, p1_name
    return p1_name, p2_name

def load_subscribers():
    if not os.path.exists(SUBSCRIBERS_FILE):
        return set()
    with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def send_telegram(chat_id, msg):
    if not TELEGRAM_BOT_TOKEN:
        print("[WARN] TELEGRAM_BOT_TOKEN not set")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=10)
    except Exception as e:
        print(f"[ERROR] send_telegram: {e}")

def broadcast(msg):
    for cid in load_subscribers():
        send_telegram(cid, msg)

# --------------- Signal loop (existing behavior) ---------------
def check_pairs_loop():
    print("Signal loop started")
    while True:
        for p1, info in pairs.items():
            p2 = info["pair2"]
            coef = info.get("coef", 1.0)
            open_spread = info.get("open", 9999)
            close_spread = info.get("close", 1.0)
            key = f"{p1}-{p2}"

            price1 = get_price(p1)
            price2 = get_price(p2)
            if price1 is None or price2 is None:
                continue

            if price1 < price2:
                scaled1 = price1 * coef
                scaled2 = price2
                scaled_note = f"{p1} * {fmt_coef(coef)}"
            else:
                scaled1 = price1
                scaled2 = price2 * coef
                scaled_note = f"{p2} * {fmt_coef(coef)}"

            spread = calc_spread(scaled1, scaled2)
            long_name, short_name = get_direction_names(p1, scaled1, p2, scaled2)

            current = state.get(key, "inactive")
            if current == "inactive" and spread >= open_spread:
                state[key] = "active"
                save_json(STATE_FILE, state)
                msg = (
                    f"🚀 {p1}-{p2} {spread:.2f}% — открытие\n"
                    f"{STATUS_EMOJI} Спред: {spread:.2f}% | Коэф: {fmt_coef(coef)}\n"
                    f"📌 LONG: {long_name} | SHORT: {short_name}\n"
                    f"📝 Масштаб: {scaled_note}"
                )
                broadcast(msg)
            elif current == "active" and spread <= close_spread:
                state[key] = "inactive"
                # add cycle timestamp
                state.setdefault("cycles", {})
                state["cycles"].setdefault(key, [])
                state["cycles"][key].append(int(time.time()))
                save_json(STATE_FILE, state)
                msg = (
                    f"🔻 {p1}-{p2} {spread:.2f}% — закрытие\n"
                    f"{STATUS_EMOJI} Спред: {spread:.2f}% | Коэф: {fmt_coef(coef)}\n"
                    f"📌 LONG: {long_name} | SHORT: {short_name}\n"
                    f"📝 Масштаб: {scaled_note}"
                )
                broadcast(msg)
        time.sleep(CHECK_INTERVAL)

# --------------- Analyzer runner (calls analyzer.py) ---------------
def run_analyzer_blocking():
    """
    Запускает analyze прямо (в текущем процессе) — чтобы не требовать установки subprocess.
    Если prefer_subprocess=True можно вызвать python analyzer.py через subprocess.
    """
    # импортировать и вызвать main из analyzer.py если файл доступен
    try:
        import analyzer
        analyzer.main()
        return True, "Analyzer finished (module call)."
    except Exception as e:
        # fallback: try subprocess run analyzer.py
        try:
            import subprocess, sys
            p = subprocess.run([sys.executable, "analyzer.py"], capture_output=True, text=True, timeout=3600)
            if p.returncode == 0:
                return True, "Analyzer finished (subprocess)."
            else:
                return False, f"Analyzer failed: {p.returncode}\n{p.stdout}\n{p.stderr}"
        except Exception as e2:
            return False, f"Analyzer execution error: {e} / {e2}"

# --------------- Commands endpoints ---------------
@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    if "message" not in data:
        return {"ok": True}

    chat_id = str(data["message"]["chat"]["id"])
    text = data["message"].get("text", "").strip()

    if text == "/start":
        subs = load_subscribers()
        if chat_id not in subs:
            with open(SUBSCRIBERS_FILE, "a", encoding="utf-8") as f:
                f.write(chat_id + "\n")
        send_telegram(chat_id, "🔥 Вы подписаны!")
        return {"ok": True}

    if text == "/stop":
        subs = load_subscribers()
        subs.discard(chat_id)
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(subs))
        send_telegram(chat_id, "❌ Подписка отменена")
        return {"ok": True}

    if text == "/status":
        lines = ["📊 Текущее состояние:\n"]
        for p1, info in pairs.items():
            p2 = info["pair2"]
            coef = info.get("coef", 1.0)
            price1 = get_price(p1)
            price2 = get_price(p2)
            if price1 is None or price2 is None:
                lines.append(f"{STATUS_EMOJI} {p1}-{p2} — нет данных\n")
                continue
            if price1 < price2:
                scaled1 = price1 * coef
                scaled2 = price2
            else:
                scaled1 = price1
                scaled2 = price2 * coef
            spread = calc_spread(scaled1, scaled2)
            long_name, short_name = get_direction_names(p1, scaled1, p2, scaled2)
            lines.append(f"{STATUS_EMOJI} {p1}-{p2} — Спред: {spread:.2f}% | Коэф: {fmt_coef(coef)}")
            lines.append(f"📌 LONG → {long_name} | SHORT → {short_name}\n")
        send_telegram(chat_id, "\n".join(lines))
        return {"ok": True}

    if text == "/spread":
        # show cycles in last 30 days (existing state cycles)
        now = int(time.time()); limit = now - 30*24*3600
        cycles = state.get("cycles", {})
        results = []
        for key, arr in cycles.items():
            cnt = sum(1 for t in arr if t >= limit)
            results.append((key, cnt))
        results.sort(key=lambda x: x[1], reverse=True)
        lines = ["🐬 Топ пар по количеству циклов за 30 дней:\n"]
        for k, c in results:
            lines.append(f"{k}: {c} циклов")
        send_telegram(chat_id, "\n".join(lines))
        return {"ok": True}

    if text == "/top":
        # read analysis.json and show top-3 per pair
        if not os.path.exists(ANALYSIS_FILE):
            send_telegram(chat_id, "⚠️ analysis.json не найден. Запустите /analyze чтобы собрать данные.")
            return {"ok": True}
        analysis = load_json(ANALYSIS_FILE, {})
        pairs_info = analysis.get("pairs", {})
        lines = ["📈 TOP по парам (из analysis.json):\n"]
        for pair_key, arr in pairs_info.items():
            lines.append(f"🔹 {pair_key}")
            # print top-3 if available
            for idx, rec in enumerate(arr[:3], start=1):
                medal = "🥇" if idx==1 else "🥈" if idx==2 else "🥉"
                lines.append(f"  {medal} Open {rec['open']:.2f}% Close {rec['close']:.2f}% — {rec['cycles']} циклов")
            lines.append("")
        send_telegram(chat_id, "\n".join(lines))
        return {"ok": True}

    if text == "/analyze":
        send_telegram(chat_id, "⚙️ Запуск анализа... это может занять несколько минут.")
        ok, info = run_analyzer_blocking()
        if ok:
            send_telegram(chat_id, "✅ Анализ завершён. Используйте /top")
        else:
            send_telegram(chat_id, f"❌ Анализ завершился с ошибкой: {info}")
        return {"ok": True}

    # default help
    send_telegram(chat_id, "Команды:\n/start\n/stop\n/status\n/spread\n/top\n/analyze")
    return {"ok": True}

# --------------- run web + loop ---------------
def run_web():
    app.run(host="0.0.0.0", port=10000)

if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    threading.Thread(target=check_pairs_loop).start()
    print("Bot started.")
