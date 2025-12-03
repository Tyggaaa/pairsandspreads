#!/usr/bin/env python3
# analyzer.py
# Собирает исторические часовые закрытия (30d) с Binance Futures,
# считает циклы (open -> close) для каждой комбинации open/close,
# сохраняет TOP-5 комбинаций (open, close, cycles) для каждой пары в analysis.json.

import requests
import time
import json
import os
from math import isclose
from datetime import datetime, timedelta

PAIRS_FILE = "pairs.json"
ANALYSIS_FILE = "analysis.json"

# Параметры перебора (утверждены тобой)
OPEN_MIN = 4.0
OPEN_MAX = 30.0
OPEN_STEP = 0.5
CLOSE_STEP = 0.5
DAYS = 30
HOURS = DAYS * 24

# Binance kline endpoint (futures)
KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"

def load_pairs():
    with open(PAIRS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_analysis(data):
    with open(ANALYSIS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def fetch_klines_close(symbol, limit=HOURS, interval="1h"):
    """
    Возвращает список (timestamp_ms, close_price) длиной <= limit.
    Если ошибка — возвращает None.
    """
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        r = requests.get(KLINES_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        res = []
        for row in data:
            ts = int(row[0])  # open time ms
            close = float(row[4])
            res.append((ts, close))
        return res
    except Exception as e:
        print(f"[ERROR] fetch_klines_close {symbol}: {e}")
        return None

def align_series(a, b):
    """
    a, b: lists of (ts_ms, price)
    Возвращает две списочные последовательности цен, выровненные по общим timestamp (по порядку возрастания).
    Если мало пересечений, вернёт минимально возможное количество точек.
    """
    dict_a = {ts: p for ts, p in a}
    dict_b = {ts: p for ts, p in b}
    common = sorted(set(dict_a.keys()) & set(dict_b.keys()))
    series_a = [dict_a[ts] for ts in common]
    series_b = [dict_b[ts] for ts in common]
    return common, series_a, series_b

def calc_spread_list(series_a, series_b, coef):
    """Возвращает список spread% для каждой точки. Масштабируем дешевую монету (как обсуждалось)."""
    spreads = []
    for pa, pb in zip(series_a, series_b):
        # масштабируем дешевую монету чтобы приблизить цены
        if pa < pb:
            pa_s = pa * coef
            pb_s = pb
        else:
            pa_s = pa
            pb_s = pb * coef
        # процент от среднего
        denom = (pa_s + pb_s) / 2.0
        if denom == 0:
            spreads.append(0.0)
        else:
            spreads.append(abs(pa_s - pb_s) / denom * 100.0)
    return spreads

def count_cycles_for_thresholds(spreads, open_thr, close_thr):
    """
    Простой state machine:
    - waiting for open: ищем spread >= open_thr
    - затем waiting for close: ищем spread <= close_thr
    когда найден close, count++ и возвращаемся в waiting for open
    Возвращаем число завершённых циклов.
    """
    state = "waiting_open"
    count = 0
    for s in spreads:
        if state == "waiting_open":
            if s >= open_thr:
                state = "waiting_close"
        elif state == "waiting_close":
            if s <= close_thr:
                count += 1
                state = "waiting_open"
    return count

def analyze_pair(p1_sym, p2_sym, coef, klines_cache):
    """
    Загружает/использует кешированные свечи и считает топ-5 (open,close,cycles).
    Возвращает список словарей sorted by cycles desc (top5).
    """
    # Получаем/скачиваем klines
    key1 = p1_sym
    key2 = p2_sym
    if key1 not in klines_cache:
        kl = fetch_klines_close(key1)
        if kl is None:
            return None
        klines_cache[key1] = kl
    if key2 not in klines_cache:
        kl = fetch_klines_close(key2)
        if kl is None:
            return None
        klines_cache[key2] = kl

    common_ts, series1, series2 = align_series(klines_cache[key1], klines_cache[key2])
    if len(common_ts) < 24:
        print(f"[WARN] мало общих часов для {p1_sym}-{p2_sym}: {len(common_ts)}")
        # всё равно пытаемся
    # рассчитываем спреды
    spreads = calc_spread_list(series1, series2, coef)
    # перебор open/close
    results = []
    open_val = OPEN_MIN
    while open_val <= OPEN_MAX + 1e-9:
        # close range: 0 .. (open - 4) inclusive
        max_close = round(open_val - 4.0 + 1e-9, 10)
        if max_close < 0:
            open_val = round(open_val + OPEN_STEP, 10)
            continue
        close_val = 0.0
        while close_val <= max_close + 1e-9:
            # считаем циклы
            cycles = count_cycles_for_thresholds(spreads, open_val, close_val)
            if cycles > 0:
                results.append((open_val, close_val, cycles))
            close_val = round(close_val + CLOSE_STEP, 10)
        open_val = round(open_val + OPEN_STEP, 10)
    if not results:
        return []
    # сортируем по cycles desc, возьмем top5
    results.sort(key=lambda x: x[2], reverse=True)
    top5 = results[:5]
    out = [{"open": float(round(r[0], 2)), "close": float(round(r[1], 2)), "cycles": int(r[2])} for r in top5]
    return out

def main():
    pairs = load_pairs()
    klines_cache = {}
    analysis = {}
    t0 = time.time()
    total = len(pairs)
    i = 0
    for p1, info in pairs.items():
        i += 1
        p2 = info["pair2"]
        coef = info.get("coef", 1.0)
        print(f"\n[{i}/{total}] Анализ пары {p1}-{p2} (coef={coef}) ...")
        try:
            res = analyze_pair(p1, p2, coef, klines_cache)
            if res is None:
                print(f"[SKIP] Не удалось получить данные для {p1}-{p2}")
                continue
            analysis[f"{p1}-{p2}"] = res
            print(f"  → Найдено {len(res)} комбинаций (top5 saved).")
        except Exception as e:
            print(f"[ERROR] {p1}-{p2}: {e}")
    # сохраняем
    save_analysis({
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pairs": analysis
    })
    dt = time.time() - t0
    print(f"\nГотово. analysis.json сохранён ({len(analysis)} пар). Время: {dt:.1f}s")

if __name__ == "__main__":
    main()
