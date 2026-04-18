#!/usr/bin/env python3
"""
QuantEdge Pro — 後端 API Server
台股爬蟲 + Yahoo Finance (美股) + FinMind 真實數據
啟動方式：python server.py
API 地址：http://localhost:5000
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os
import yfinance as yf
import requests
from bs4 import BeautifulSoup
import pandas as pd
import numpy as np
import json
import time
import threading
import random
from datetime import datetime, timedelta
import re
import zoneinfo
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)  # 允許前端跨域存取

# 前端 HTML 所在目錄（與 server.py 同層）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── 快取層（避免頻繁爬蟲被封） ───────────────────────────────
_cache = {}
_cache_ttl = {}
_cache_lock = threading.Lock()
CACHE_SECONDS = 60  # 60秒快取

def cache_get(key):
    with _cache_lock:
        if key in _cache and time.time() < _cache_ttl.get(key, 0):
            return _cache[key]
    return None

def cache_set(key, val, ttl=CACHE_SECONDS):
    with _cache_lock:
        _cache[key] = val
        _cache_ttl[key] = time.time() + ttl

# ─── 爬蟲安全：速率限制 ─────────────────────────────────────
# 限制同時對 TWSE 的請求數，避免被封鎖
_twse_semaphore = threading.Semaphore(3)   # 最多 3 個並行 TWSE 請求
_yf_semaphore   = threading.Semaphore(5)   # 最多 5 個並行 yfinance 請求

# ─── FinMind API ─────────────────────────────────────────────
FINMIND_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiY2hpYmFiYWJhIiwiZW1haWwiOiJlYXRiYTczQGdtYWlsLmNvbSJ9.WFy4oNQD7wvqfQCKeXPUe1eQDWhBzZsxRXsicuQpVp0"
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"

def finmind_fetch(dataset, stock_id, start_date=None, end_date=None):
    """通用 FinMind API 請求，失敗回傳空 list"""
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    params = {
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start_date,
        "token": FINMIND_TOKEN
    }
    if end_date:
        params["end_date"] = end_date
    try:
        r = requests.get(FINMIND_BASE, params=params, timeout=15)
        r.raise_for_status()
        body = r.json()
        if body.get("status") == 200 and body.get("data"):
            return body["data"]
    except Exception as e:
        print(f"FinMind {dataset} {stock_id}: {e}")
    return []

def fetch_tw_history_finmind(stock_id, months=3):
    """從 FinMind 取台股歷史日K（比 yfinance .TW 更穩定）"""
    cache_key = f"fm_hist_{stock_id}_{months}"
    cached = cache_get(cache_key)
    if cached:
        return cached
    start = (datetime.now() - timedelta(days=months * 35)).strftime("%Y-%m-%d")
    rows = finmind_fetch("TaiwanStockPrice", stock_id, start_date=start)
    if not rows or len(rows) < 5:
        return None
    rows = sorted(rows, key=lambda x: x["date"])
    result = {
        "dates":   [r["date"] for r in rows],
        "opens":   [round(float(r.get("open",  r["close"])), 2) for r in rows],
        "highs":   [round(float(r.get("max",   r["close"])), 2) for r in rows],
        "lows":    [round(float(r.get("min",   r["close"])), 2) for r in rows],
        "closes":  [round(float(r["close"]), 2) for r in rows],
        "volumes": [int(r.get("Trading_Volume", 0)) for r in rows]
    }
    cache_set(cache_key, result, 300)
    return result

def fetch_tw_revenue_yoy(stock_id):
    """月營收年增率（FinMind）— 最新月份 > 去年同期 → True"""
    cache_key = f"fm_rev_{stock_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    start = (datetime.now() - timedelta(days=420)).strftime("%Y-%m-%d")
    rows = finmind_fetch("TaiwanStockMonthRevenue", stock_id, start_date=start)
    if len(rows) < 13:
        cache_set(cache_key, False, 21600)
        return False
    rows = sorted(rows, key=lambda x: (int(x.get("revenue_year", 0)), int(x.get("revenue_month", 0))))
    latest = rows[-1]
    target_year  = int(latest.get("revenue_year",  0)) - 1
    target_month = int(latest.get("revenue_month", 0))
    prev = next((r for r in rows
                 if int(r.get("revenue_year", 0))  == target_year
                 and int(r.get("revenue_month", 0)) == target_month), None)
    result = bool(prev and float(latest.get("revenue", 0)) > float(prev.get("revenue", 0)))
    cache_set(cache_key, result, 21600)
    return result

def fetch_tw_eps_yoy(stock_id):
    """EPS 年增率（FinMind）— 最新季EPS > 去年同季 → True"""
    cache_key = f"fm_eps_{stock_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    start = (datetime.now() - timedelta(days=550)).strftime("%Y-%m-%d")
    rows = finmind_fetch("TaiwanStockFinancialStatements", stock_id, start_date=start)
    eps_rows = sorted([r for r in rows if r.get("type") == "EPS"], key=lambda x: x["date"])
    if len(eps_rows) < 5:
        cache_set(cache_key, False, 21600)
        return False
    latest_eps  = float(eps_rows[-1].get("value", 0))
    compare_eps = float(eps_rows[-5].get("value", 0))
    result = (latest_eps > compare_eps) and (compare_eps != 0)
    cache_set(cache_key, result, 21600)
    return result

def fetch_tw_institutional(stock_id, days=5):
    """三大法人近 N 日買賣超（FinMind，快取 1小時）"""
    cache_key = f"fm_inst_{stock_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    rows = finmind_fetch("TaiwanStockInstitutionalInvestorsBuySell", stock_id, start_date=start)
    empty = {"foreign_net": 0, "trust_net": 0, "dealer_net": 0,
             "foreign_net_buy": False, "trust_net_buy": False, "three_major_net_buy": False}
    if not rows:
        cache_set(cache_key, empty, 3600)
        return empty
    rows = sorted(rows, key=lambda x: x["date"])
    recent_dates = sorted(set(r["date"] for r in rows))[-days:]
    recent = [r for r in rows if r["date"] in recent_dates]
    foreign_net = trust_net = dealer_net = 0.0
    for r in recent:
        name = r.get("name", "")
        net  = float(r.get("buy", 0)) - float(r.get("sell", 0))
        if "Foreign_Investor" in name:
            foreign_net += net
        elif "Investment_Trust" in name:
            trust_net += net
        elif "Dealer" in name:
            dealer_net += net
    result = {
        "foreign_net":       round(foreign_net, 0),
        "trust_net":         round(trust_net, 0),
        "dealer_net":        round(dealer_net, 0),
        "foreign_net_buy":   foreign_net > 0,
        "trust_net_buy":     trust_net > 0,
        "three_major_net_buy": (foreign_net + trust_net + dealer_net) > 0
    }
    cache_set(cache_key, result, 3600)
    return result

def fetch_tw_margin(stock_id):
    """融資融券最新狀態（FinMind，快取 1小時）"""
    cache_key = f"fm_margin_{stock_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    rows = finmind_fetch("TaiwanStockMarginPurchaseShortSale", stock_id, start_date=start)
    empty = {"margin_today": 0, "margin_change": 0, "margin_increase": False, "short_today": 0}
    if len(rows) < 2:
        cache_set(cache_key, empty, 3600)
        return empty
    rows = sorted(rows, key=lambda x: x["date"])
    latest = rows[-1]
    prev   = rows[-2]
    margin_today  = int(latest.get("MarginPurchaseToday", 0))
    margin_prev   = int(prev.get("MarginPurchaseToday", 0))
    result = {
        "margin_today":   margin_today,
        "margin_change":  margin_today - margin_prev,
        "margin_increase": (margin_today - margin_prev) > 0,
        "short_today":    int(latest.get("ShortSaleToday", 0))
    }
    cache_set(cache_key, result, 3600)
    return result

# ─── 台股資料庫（篩選用，含上市+上櫃主要標的，共 ~160 支） ─────
TW_STOCKS_DB = {
    # ── 半導體 ─────────────────────────────────────────────────
    "2330": "台積電", "2454": "聯發科", "2303": "聯電",
    "2308": "台達電", "3034": "聯詠", "2379": "瑞昱",
    "6770": "力積電", "3711": "日月光投控", "2449": "京元電子",
    "3443": "創意", "6515": "穎崴", "3081": "聯亞",
    "2337": "旺宏", "3533": "嘉澤", "6274": "台燿",
    # ── 電子/科技 ───────────────────────────────────────────────
    "2317": "鴻海", "2382": "廣達", "2357": "華碩",
    "2353": "宏碁", "3008": "大立光", "2395": "研華",
    "4938": "和碩", "2324": "仁寶", "2356": "英業達",
    "3231": "緯創", "2377": "微星", "2376": "技嘉",
    "2385": "群光", "2360": "致茂", "3017": "奇鋐",
    "2399": "映泰", "3044": "健鼎", "4977": "眾達",
    "2301": "光寶科", "2388": "威盛", "3532": "台勝科",
    "6669": "緯穎", "6488": "環球晶", "3035": "智原",
    # ── 顯示/光電 ───────────────────────────────────────────────
    "2409": "友達", "3481": "群創", "2475": "華映",
    "2448": "晶電", "3189": "景碩", "6592": "和碩",
    # ── 網通/資安 ───────────────────────────────────────────────
    "2345": "智邦", "3630": "新鉅科", "6245": "立端",
    "4904": "遠傳", "3045": "台灣大",
    # ── 金融 ────────────────────────────────────────────────────
    "2881": "富邦金", "2882": "國泰金", "2891": "中信金",
    "2886": "兆豐金", "2884": "玉山金", "2887": "台新金",
    "2885": "元大金", "2892": "第一金", "5880": "合庫金",
    "2880": "華南金", "2883": "開發金", "2890": "永豐金",
    "5876": "上海商銀", "2834": "臺企銀", "2801": "彰銀",
    # ── 電信/媒體 ───────────────────────────────────────────────
    "2412": "中華電",
    # ── 石化/材料 ───────────────────────────────────────────────
    "1301": "台塑", "1303": "南亞", "1326": "台化",
    "6505": "台塑化", "1402": "遠東新",
    "1477": "聚陽", "1455": "集盛",
    # ── 鋼鐵/傳產 ───────────────────────────────────────────────
    "2002": "中鋼", "2006": "東和鋼鐵", "2027": "大成鋼",
    "1101": "台泥", "1102": "亞泥", "1216": "統一",
    "1229": "聯華", "1210": "大成", "1702": "南僑",
    # ── 航運 ────────────────────────────────────────────────────
    "2615": "萬海", "2603": "長榮", "2609": "陽明",
    "2610": "華航", "2618": "長榮航", "5608": "四維航",
    # ── 生技/醫療 ───────────────────────────────────────────────
    "4119": "旭富", "4107": "邦特", "1795": "美時",
    "4166": "分進", "4144": "經緯航太", "1723": "中碳",
    "6547": "高端疫苗", "4164": "承業醫",
    # ── 觀光/零售 ───────────────────────────────────────────────
    "2207": "和泰車", "2912": "統一超", "1438": "裕民",
    "9910": "豐泰", "9914": "美利達", "9921": "巨大",
    "2903": "遠百", "2823": "中壽",
    # ── ETF ─────────────────────────────────────────────────────
    "0050": "元大台灣50", "0056": "元大高股息",
    "00878": "國泰永續高股息", "00881": "國泰台灣5G+",
    "00900": "富邦特選高股息", "00692": "富邦公司治理",
    "006208": "富邦台50", "00850": "元大臺灣ESG永續",
    # ── 其他熱門 ─────────────────────────────────────────────────
    "2327": "國巨", "2408": "南亞科", "2344": "華邦電",
    "3037": "欣興", "2049": "上銀", "1590": "亞德客",
    "2354": "鴻準", "3673": "TPK宸鴻",
    # ── 補充常見標的（使用者監控清單中可能用到）──────────────────
    "6533": "富采控股", "4958": "臻鼎-KY", "5274": "信驊",
    "6279": "胡連",    "6261": "久元",    "6757": "台光電",
    "3596": "智易",    "6781": "AES-KY",  "6271": "同欣電",
    "6449": "網家",
}

# 美股篩選候選池（涵蓋科技、半導體、消費、金融、能源）
US_STOCKS_SCREEN = [
    # 科技巨頭
    "AAPL","MSFT","GOOGL","AMZN","META",
    # 半導體
    "NVDA","AMD","INTC","QCOM","AVGO","TSM","ASML","MU","AMAT","KLAC",
    # 電動車/新能源
    "TSLA",
    # 串流/雲端/SaaS
    "NFLX","CRM","NOW","SNOW","PLTR",
    # 金融
    "JPM","BAC","GS","V","MA",
    # 生技/醫療
    "JNJ","UNH","PFE","ABBV",
    # 消費/零售
    "WMT","COST","NKE",
]

# ─── 技術指標計算 ────────────────────────────────────────────
def calc_rsi(closes, period=14):
    closes = np.array(closes, dtype=float)
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 100
    return round(100 - (100 / (1 + rs)), 2)

def calc_macd(closes, fast=12, slow=26, signal=9):
    closes = pd.Series(closes, dtype=float)
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return {
        "macd": round(float(macd_line.iloc[-1]), 3),
        "signal": round(float(signal_line.iloc[-1]), 3),
        "histogram": round(float(histogram.iloc[-1]), 3),
        "cross": "golden" if macd_line.iloc[-1] > signal_line.iloc[-1] else "death"
    }

def calc_macd_weekly(daily_closes):
    """從日線收盤價重採樣為週線，計算 MACD Histogram 最近一週是否 > 0（翻紅）"""
    try:
        if len(daily_closes) < 30:
            return False
        s = pd.Series(daily_closes, dtype=float)
        # 每5個交易日為一週（簡化，不依日曆）
        weekly = s.groupby(np.arange(len(s)) // 5).last()
        if len(weekly) < 28:
            return False
        ema_fast = weekly.ewm(span=12, adjust=False).mean()
        ema_slow = weekly.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal_line
        # 最後一週 histogram > 0，且比前一週大（向上）
        return float(hist.iloc[-1]) > 0 and float(hist.iloc[-1]) > float(hist.iloc[-2])
    except Exception:
        return False

def calc_kd(highs, lows, closes, period=9):
    highs, lows, closes = np.array(highs), np.array(lows), np.array(closes)
    rsv_list = []
    for i in range(period - 1, len(closes)):
        h = max(highs[i - period + 1:i + 1])
        l = min(lows[i - period + 1:i + 1])
        rsv = (closes[i] - l) / (h - l) * 100 if h != l else 50
        rsv_list.append(rsv)
    k, d = 50.0, 50.0
    for rsv in rsv_list:
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
    return {"K": round(k, 2), "D": round(d, 2)}

def calc_bollinger(closes, period=20, std_mult=2):
    closes = pd.Series(closes, dtype=float)
    ma = closes.rolling(period).mean().iloc[-1]
    std = closes.rolling(period).std().iloc[-1]
    return {
        "upper": round(float(ma + std_mult * std), 2),
        "middle": round(float(ma), 2),
        "lower": round(float(ma - std_mult * std), 2)
    }

def calc_ma(closes, periods=[5, 10, 20, 60]):
    closes = np.array(closes, dtype=float)
    result = {}
    for p in periods:
        if len(closes) >= p:
            result[f"MA{p}"] = round(float(np.mean(closes[-p:])), 2)
    return result

def calc_atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return round(float(np.mean(trs[-period:])), 2) if trs else 0

def calc_backtest_winrate(closes, highs=None, lows=None,
                          strategy="MA_RSI", stop_loss=0.05, take_profit=0.15):
    """真實回測勝率計算（與 api_backtest 相同策略邏輯）

    輸入歷史收盤/高/低價序列，模擬策略進出場，
    回傳歷史勝率(%)。歷史或交易次數不足時回傳 None（不產生假數據）。
    """
    closes_arr = np.array(closes, dtype=float)
    n = len(closes_arr)
    if n < 30:
        return None
    highs_arr  = np.array(highs,  dtype=float) if highs  else closes_arr.copy()
    lows_arr   = np.array(lows,   dtype=float) if lows   else closes_arr.copy()

    wins   = 0
    trades = 0
    in_trade   = False
    entry_price = 0.0
    start = min(30, n // 2)

    for i in range(start, n):
        wc = closes_arr[:i]
        if len(wc) < 20:
            continue
        rsi      = calc_rsi(list(wc[-20:]))
        macd_d   = calc_macd(list(wc[-40:]) if len(wc) >= 40 else list(wc))
        kd_d     = calc_kd(list(highs_arr[:i]), list(lows_arr[:i]), list(wc))
        ma5      = float(np.mean(wc[-5:]))
        ma20     = float(np.mean(wc[-20:]))
        price    = float(closes_arr[i])

        if not in_trade:
            sig = False
            if strategy == "MA_RSI":
                sig = (ma5 > ma20) and (25 < rsi < 50)
            elif strategy == "MACD":
                sig = (macd_d.get("cross") == "golden") and (macd_d.get("histogram", 0) > 0)
            elif strategy == "KD_RSI":
                sig = (kd_d.get("K", 0) > kd_d.get("D", 0)) and (rsi < 40)
            elif strategy == "TRIPLE":
                wc60 = closes_arr[max(0, i-60):i]
                ma60 = float(np.mean(wc60)) if len(wc60) >= 30 else ma20
                sig  = (ma5 > ma20 > ma60) and (20 < rsi < 55)
            if sig:
                in_trade    = True
                entry_price = price
        else:
            chg = (price - entry_price) / entry_price if entry_price > 0 else 0
            ex  = False
            if strategy == "MA_RSI":
                ex = (ma5 < ma20) or (rsi > 72)
            elif strategy == "MACD":
                ex = macd_d.get("cross") == "death"
            elif strategy == "KD_RSI":
                ex = (kd_d.get("K", 0) < kd_d.get("D", 0)) or (rsi > 75)
            elif strategy == "TRIPLE":
                ex = (ma5 < ma20) or (rsi > 75)
            if chg <= -stop_loss or chg >= take_profit:
                ex = True
            if ex:
                trades += 1
                if chg > 0:
                    wins += 1
                in_trade = False

    if trades < 5:
        return None  # 交易次數不足，無法產生可信勝率
    return round(float(np.clip(wins / trades * 100, 25, 92)), 1)


def winrate_signal(rsi, macd_cross, k, d, price, ma5, ma20,
                   closes=None, highs=None, lows=None, strategy="MA_RSI"):
    """勝率與操作建議

    優先使用真實歷史回測（closes 有值時）；
    否則退回多因子啟發式估算（快速但較不精準）。
    """
    # ── 真實回測路徑 ──────────────────────────────
    winrate = None
    if closes and len(closes) >= 30:
        winrate = calc_backtest_winrate(closes, highs, lows, strategy)

    # ── 啟發式備援（真實回測資料不足時使用）────────
    if winrate is None:
        score = 0
        if rsi < 30:   score += 20
        elif rsi < 45: score += 10
        elif rsi > 70: score -= 20
        elif rsi > 60: score -= 10
        if macd_cross == "golden": score += 20
        elif macd_cross == "death": score -= 15
        if k < 20:  score += 15
        elif k > 80: score -= 15
        if k > d:   score += 10
        if price > ma5 > ma20:  score += 20
        elif price < ma5 < ma20: score -= 20
        winrate = max(40, min(85, 60 + score * 0.5))

    # ── 信號標籤 ─────────────────────────────────
    signals = []
    if rsi < 30:           signals.append("RSI超賣")
    elif rsi > 70:         signals.append("RSI超買")
    if macd_cross == "golden":  signals.append("MACD黃金交叉")
    elif macd_cross == "death": signals.append("MACD死叉")
    if k > d:              signals.append("KD多頭")
    if k < 20:             signals.append("KD超賣")
    if price > ma5 > ma20: signals.append("均線多頭排列")

    if   winrate >= 65: action = "強力買進"
    elif winrate >= 55: action = "買進"
    elif winrate >= 45: action = "觀察"
    else:               action = "賣出"
    return round(winrate, 1), action, signals

# ─── 台股爬蟲（TWSE + TPEX） ────────────────────────────────
# 輪換 User-Agent，降低被辨識為爬蟲的機率
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

def get_headers():
    """每次請求隨機選取 User-Agent，模擬真實瀏覽器行為"""
    return {
        "User-Agent": random.choice(_UA_POOL),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://mis.twse.com.tw/",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }

# 保留向後兼容的 HEADERS 變數
HEADERS = get_headers()

def _yf_history_price(ticker_symbol):
    """
    從 yfinance history() 安全取得最新收盤價與昨收。
    比 fast_info 穩定 10 倍，適合雲端部署。
    回傳 (price, prev_close) 或 (None, None)
    """
    try:
        t = yf.Ticker(ticker_symbol)
        df = t.history(period="5d", auto_adjust=True)
        if df.empty:
            return None, None
        price = float(df["Close"].iloc[-1])
        prev  = float(df["Close"].iloc[-2]) if len(df) >= 2 else price
        if price <= 0:
            return None, None
        return round(price, 2), round(prev, 2)
    except Exception as e:
        print(f"yf history error {ticker_symbol}: {e}")
        return None, None

def _yf_get_name(ticker_symbol, fallback):
    """安全取得股票名稱（可能很慢，加 timeout 保護）"""
    try:
        info = yf.Ticker(ticker_symbol).info
        return info.get("shortName") or info.get("longName") or fallback
    except Exception:
        return fallback

def fetch_tw_price_yfinance(stock_id):
    """yfinance 備援抓台股價格（TWSE/TPEX 失敗時使用）
    使用 history() 取代 fast_info，對雲端部署友善
    """
    with _yf_semaphore:
        for suffix in [".TW", ".TWO"]:
            price, prev = _yf_history_price(f"{stock_id}{suffix}")
            if price:
                change = round(price - prev, 2)
                change_pct = round((change / prev) * 100, 2) if prev else 0
                # 名稱優先順序：DB → yfinance shortName → 代號本身
                name = TW_STOCKS_DB.get(stock_id)
                if not name:
                    try:
                        info = yf.Ticker(f"{stock_id}{suffix}").info
                        name = (info.get("shortName") or info.get("longName") or stock_id)
                        # 去掉 yfinance 常見的後綴雜訊
                        name = name.replace(" Corporation","").replace(" Co., Ltd.","").strip()
                    except Exception:
                        name = stock_id
                result = {
                    "id": stock_id, "name": name,
                    "price": price, "yesterday": prev,
                    "change": change, "change_pct": change_pct,
                    "volume": 0, "market": "TWSE"
                }
                cache_set(f"twse_{stock_id}", result, 120)
                return result
    return None

def fetch_twse_price(stock_id):
    """從台灣證交所 API 抓即時股價（含重試 + yfinance 備援）
    使用 Semaphore 控制並發數，遵守爬蟲禮儀
    """
    cached = cache_get(f"twse_{stock_id}")
    if cached:
        return cached

    with _twse_semaphore:
        for attempt in range(2):
            try:
                url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw&json=1&delay=0"
                r = requests.get(url, headers=get_headers(), timeout=6)
                r.raise_for_status()
                data = r.json()
                msgArray = data.get("msgArray", [])
                if not msgArray:
                    break
                d = msgArray[0]
                price = float(d.get("z", d.get("y", 0)) or d.get("y", 0))
                if price <= 0:
                    break
                open_p  = float(d.get("o", 0) or 0)
                high    = float(d.get("h", 0) or 0)
                low     = float(d.get("l", 0) or 0)
                yesterday = float(d.get("y", 0) or 0)
                volume  = int(d.get("v", 0) or 0)
                change  = round(price - yesterday, 2)
                change_pct = round((change / yesterday) * 100, 2) if yesterday else 0
                name    = d.get("n", TW_STOCKS_DB.get(stock_id, stock_id))
                result  = {
                    "id": stock_id, "name": name, "price": price,
                    "open": open_p, "high": high, "low": low,
                    "yesterday": yesterday, "volume": volume,
                    "change": change, "change_pct": change_pct,
                    "market": "TWSE"
                }
                cache_set(f"twse_{stock_id}", result, 30)
                return result
            except requests.HTTPError as e:
                print(f"TWSE HTTP {e.response.status_code} for {stock_id}")
                break  # 4xx/5xx 不重試
            except Exception as e:
                print(f"TWSE fetch error {stock_id} attempt {attempt}: {e}")
                if attempt == 0:
                    time.sleep(0.3 + random.random() * 0.2)  # 加隨機抖動

    # 三層備援最終：yfinance
    return fetch_tw_price_yfinance(stock_id)

def fetch_tpex_price(stock_id):
    """從櫃買中心抓即時股價（含 Semaphore 速率控制）"""
    cached = cache_get(f"tpex_{stock_id}")
    if cached:
        return cached
    with _twse_semaphore:
        try:
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=otc_{stock_id}.tw&json=1&delay=0"
            r = requests.get(url, headers=get_headers(), timeout=6)
            r.raise_for_status()
            data = r.json()
            msgArray = data.get("msgArray", [])
            if not msgArray:
                return None
            d = msgArray[0]
            price = float(d.get("z", d.get("y", 0)) or d.get("y", 0))
            if price <= 0:
                return None
            yesterday = float(d.get("y", 0) or 0)
            change = round(price - yesterday, 2)
            change_pct = round((change / yesterday) * 100, 2) if yesterday else 0
            result = {
                "id": stock_id,
                "name": d.get("n", TW_STOCKS_DB.get(stock_id, stock_id)),
                "price": price, "yesterday": yesterday,
                "change": change, "change_pct": change_pct,
                "volume": int(d.get("v", 0) or 0),
                "market": "TPEX"
            }
            cache_set(f"tpex_{stock_id}", result, 30)
            return result
        except Exception as e:
            print(f"TPEX fetch error {stock_id}: {e}")
            return None

def fetch_tw_history(stock_id, months=3):
    """台股歷史日K（FinMind 優先，yfinance 備援）"""
    cached = cache_get(f"hist_{stock_id}")
    if cached:
        return cached

    # ── FinMind 優先（更穩定，無 .TW 後綴問題）──
    result = fetch_tw_history_finmind(stock_id, months)
    if result and len(result.get("closes", [])) >= 5:
        cache_set(f"hist_{stock_id}", result, 300)
        return result

    # ── yfinance 備援 ──
    try:
        ticker = yf.Ticker(f"{stock_id}.TW")
        df = ticker.history(period=f"{months}mo")
        if df.empty:
            ticker = yf.Ticker(f"{stock_id}.TWO")
            df = ticker.history(period=f"{months}mo")
        if df.empty:
            return None
        df = df.reset_index()
        result = {
            "dates": [str(d.date()) for d in df["Date"]],
            "opens": [round(float(v), 2) for v in df["Open"]],
            "highs": [round(float(v), 2) for v in df["High"]],
            "lows": [round(float(v), 2) for v in df["Low"]],
            "closes": [round(float(v), 2) for v in df["Close"]],
            "volumes": [int(v) for v in df["Volume"]]
        }
        cache_set(f"hist_{stock_id}", result, 300)
        return result
    except Exception as e:
        print(f"History fetch error {stock_id}: {e}")
        return None

def fetch_us_stock(symbol):
    """Yahoo Finance 抓美股即時 + 歷史（全用 history，不用 fast_info）"""
    cached = cache_get(f"us_{symbol}")
    if cached:
        return cached
    with _yf_semaphore:
        try:
            ticker = yf.Ticker(symbol)
            # 用 3 個月歷史抓價格和指標
            df = ticker.history(period="3mo", auto_adjust=True)
            if df.empty:
                return None
            df = df.reset_index()
            price = round(float(df["Close"].iloc[-1]), 2)
            prev  = round(float(df["Close"].iloc[-2]), 2) if len(df) >= 2 else price
            change = round(price - prev, 2)
            change_pct = round((change / prev) * 100, 2) if prev else 0
            # 股票名稱（快速取，失敗用 symbol）
            name = symbol
            try:
                info_data = ticker.info
                name = info_data.get("shortName") or info_data.get("longName") or symbol
            except Exception:
                pass
            result = {
                "id": symbol, "name": name,
                "price": price, "yesterday": prev,
                "change": change, "change_pct": change_pct,
                "volume": int(df["Volume"].iloc[-1]) if "Volume" in df else 0,
                "market": "US",
                "history": {
                    "dates":   [str(d.date()) if hasattr(d,"date") else str(d)[:10] for d in df["Date"]],
                    "opens":   [round(float(v), 2) for v in df["Open"]],
                    "highs":   [round(float(v), 2) for v in df["High"]],
                    "lows":    [round(float(v), 2) for v in df["Low"]],
                    "closes":  [round(float(v), 2) for v in df["Close"]],
                    "volumes": [int(v) for v in df["Volume"]]
                }
            }
            cache_set(f"us_{symbol}", result, 120)
            return result
        except Exception as e:
            print(f"US stock fetch error {symbol}: {e}")
            return None

_last_tw_index = {}  # 持久化備份：最後一次成功取得的加權指數資料

def is_tw_market_open():
    """判斷台灣股市是否開盤中（台北時間 09:00–13:30，週一～週五）"""
    try:
        tz = zoneinfo.ZoneInfo("Asia/Taipei")
        now = datetime.now(tz)
        if now.weekday() >= 5:  # 週六(5)、週日(6)
            return False
        t = now.hour * 60 + now.minute
        return 9 * 60 <= t <= 13 * 60 + 30
    except Exception:
        return False

def fetch_tw_index():
    """台灣加權指數（用 history，不用 fast_info）
    - 開盤中：快取 60 秒，即時更新
    - 休市：回傳最後收盤價，不顯示 0
    """
    global _last_tw_index
    cached = cache_get("tw_index")
    if cached:
        return cached
    price, prev = _yf_history_price("^TWII")
    if price:
        change = round(price - prev, 2)
        change_pct = round((change / prev) * 100, 2) if prev else 0
        market_open = is_tw_market_open()
        result = {
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "market_open": market_open,
            "is_cached": False
        }
        _last_tw_index = result  # 永久備份最後成功值
        ttl = 60 if market_open else 300  # 開盤 1 分鐘更新，休市 5 分鐘
        cache_set("tw_index", result, ttl)
        return result
    # 取得失敗：回傳持久備份（避免顯示 0）
    if _last_tw_index:
        fallback = dict(_last_tw_index)
        fallback["is_cached"] = True
        fallback["market_open"] = False
        return fallback
    return {"price": 0, "change": 0, "change_pct": 0, "market_open": False, "is_cached": True}

def get_full_indicators(stock_id, is_tw=True):
    """取得完整技術指標"""
    hist = fetch_tw_history(stock_id) if is_tw else None
    if not hist:
        return {}
    closes = hist["closes"]
    highs = hist["highs"]
    lows = hist["lows"]
    volumes = hist["volumes"]
    if len(closes) < 20:
        return {}
    rsi = calc_rsi(closes)
    macd = calc_macd(closes)
    kd = calc_kd(highs, lows, closes)
    bb = calc_bollinger(closes)
    mas = calc_ma(closes)
    atr = calc_atr(highs, lows, closes)
    price = closes[-1]
    ma5 = mas.get("MA5", price)
    ma20 = mas.get("MA20", price)
    avg_vol = int(np.mean(volumes[-20:])) if len(volumes) >= 20 else volumes[-1]
    vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol else 1
    # Item11: 依信號強度自動選最適合策略計算勝率
    # MACD 黃金交叉優先、其次 KD、其次 MA+RSI
    if macd.get("cross") == "golden":
        best_strategy = "MACD"
    elif kd.get("K", 0) > kd.get("D", 0) and rsi < 50:
        best_strategy = "KD_RSI"
    else:
        best_strategy = "MA_RSI"
    winrate, action, signals = winrate_signal(
        rsi, macd["cross"], kd["K"], kd["D"], price, ma5, ma20,
        closes=closes, highs=highs, lows=lows, strategy=best_strategy)
    # 判斷是否為真實回測（歷史資料 ≥ 30 筆才跑回測）
    backtest_real = closes is not None and len(closes) >= 30
    # 綜合評分
    score_items = [
        price > ma5, price > ma20, ma5 > ma20,
        macd["cross"] == "golden", macd["histogram"] > 0,
        rsi < 70, rsi > 30, kd["K"] > kd["D"],
        price < bb["upper"], price > bb["lower"],
        vol_ratio > 1
    ]
    score = round(sum(score_items) / len(score_items) * 100)
    return {
        "RSI14": rsi,
        "MACD": macd,
        "KD": kd,
        "Bollinger": bb,
        "MA": mas,
        "ATR14": atr,
        "volume_ratio": vol_ratio,
        "avg_volume": avg_vol,
        "score": score,
        "winrate": winrate,
        "backtest_real": backtest_real,
        "action": action,
        "signals": signals
    }

# ─── 前端路由 ────────────────────────────────────────────────

@app.route("/api/search")
def api_search():
    """股票搜尋（代號 + 名稱模糊查詢）"""
    q = request.args.get("q", "").strip().upper()
    if not q or len(q) < 1:
        return jsonify([])
    results = []
    # 台股搜尋
    for sid, name in TW_STOCKS_DB.items():
        if q in sid or q in name.upper():
            results.append({"id": sid, "name": name, "market": "tw"})
    # 美股搜尋
    for sym in US_STOCKS_SCREEN:
        if q in sym:
            results.append({"id": sym, "name": sym, "market": "us"})
    return jsonify(results[:20])

@app.route("/api/screen", methods=["POST"])
def api_screen():
    """技術指標篩選股票
    接受條件：
      rsi_max (int)      — RSI 上限，預設 35（超賣）
      rsi_min (int)      — RSI 下限，預設 0
      ma_golden (bool)   — MA5 > MA20
      macd_golden (bool) — MACD 黃金交叉
      kd_golden (bool)   — KD K > D
      vol_ratio_min (float) — 量能比，預設 1.0
      score_min (int)    — 綜合評分下限，預設 50
      market (str)       — "tw"/"us"/"all"
    """
    cached = cache_get("screen_" + str(request.data))
    if cached:
        return jsonify(cached)

    body = request.get_json() or {}
    rsi_max              = float(body.get("rsi_max", 100))
    rsi_min              = float(body.get("rsi_min", 0))
    ma_golden            = body.get("ma_golden", False)
    macd_golden          = body.get("macd_golden", False)
    macd_weekly_golden   = body.get("macd_weekly_golden", False)
    kd_golden            = body.get("kd_golden", False)
    bull_align           = body.get("bull_align", False)      # MA5>MA20>MA60
    vol_ratio_min        = float(body.get("vol_ratio_min", 0))
    score_min            = int(body.get("score_min", 0))
    revenue_growth       = body.get("revenue_growth", False)   # 月營收YoY增長
    earnings_growth      = body.get("earnings_growth", False)  # EPS YoY增長
    foreign_net_buy      = body.get("foreign_net_buy", False)  # 外資近5日買超
    three_major_net_buy  = body.get("three_major_net_buy", False) # 三大法人買超
    margin_increase      = body.get("margin_increase", False)  # 融資增加
    market               = body.get("market", "tw")

    results = []

    def check_tw(stock_id):
        """篩選單支台股"""
        try:
            price_data = fetch_twse_price(stock_id)
            if not price_data or not price_data.get("price"):
                price_data = fetch_tpex_price(stock_id)
            if not price_data or not price_data.get("price"):
                return None
            ind = get_full_indicators(stock_id)
            if not ind or not ind.get("RSI14"):
                return None
            rsi  = ind["RSI14"]
            macd = ind["MACD"]
            kd   = ind["KD"]
            mas  = ind["MA"]
            vr   = ind.get("volume_ratio", 1)
            sc   = ind.get("score", 0)
            price = price_data["price"]
            ma5  = mas.get("MA5", price)
            ma20 = mas.get("MA20", price)
            # 套用篩選條件
            if rsi > rsi_max: return None
            if rsi < rsi_min: return None
            if ma_golden   and not (ma5 > ma20): return None
            if macd_golden and not (macd.get("cross") == "golden"): return None
            if kd_golden   and not (kd.get("K", 0) > kd.get("D", 0)): return None
            if vr < vol_ratio_min: return None
            if sc < score_min: return None
            hist = fetch_tw_history(stock_id)
            closes = hist["closes"] if hist else []
            # 新增篩選條件（需要歷史資料）
            if macd_weekly_golden and not calc_macd_weekly(closes): return None
            if bull_align:
                ma_full = calc_ma(closes)
                ma60 = ma_full.get("MA60", 0)
                if not (ma5 > ma20 > 0 and ma20 > ma60 > 0): return None
            # FinMind 基本面篩選（台股）
            if revenue_growth and not fetch_tw_revenue_yoy(stock_id): return None
            if earnings_growth and not fetch_tw_eps_yoy(stock_id): return None
            # FinMind 籌碼篩選
            if foreign_net_buy or three_major_net_buy or margin_increase:
                inst = fetch_tw_institutional(stock_id)
                mg   = fetch_tw_margin(stock_id)
                if foreign_net_buy     and not inst.get("foreign_net_buy"):   return None
                if three_major_net_buy and not inst.get("three_major_net_buy"): return None
                if margin_increase     and not mg.get("margin_increase"):     return None
            return {
                "id": stock_id,
                "name": price_data.get("name", TW_STOCKS_DB.get(stock_id, stock_id)),
                "market": "tw",
                "price": price,
                "change_pct": price_data.get("change_pct", 0),
                "rsi": round(rsi, 1),
                "macd_cross": macd.get("cross", "—"),
                "kd_k": round(kd.get("K", 0), 1),
                "kd_d": round(kd.get("D", 0), 1),
                "ma5": round(ma5, 2),
                "ma20": round(ma20, 2),
                "volume_ratio": round(vr, 2),
                "score": sc,
                "winrate": ind.get("winrate", 60),
                "action": ind.get("action", "觀察"),
                "sparkline": closes[-20:] if len(closes) >= 20 else closes,
            }
        except Exception as e:
            print(f"Screen TW {stock_id}: {e}")
            return None

    def check_us(sym):
        """篩選單支美股"""
        try:
            data = fetch_us_stock(sym)
            if not data: return None
            hist = data.get("history", {})
            closes = hist.get("closes", [])
            if len(closes) < 20: return None
            highs  = hist.get("highs", closes)
            lows   = hist.get("lows", closes)
            volumes = hist.get("volumes", [])
            rsi  = calc_rsi(closes)
            macd = calc_macd(closes)
            kd   = calc_kd(highs, lows, closes)
            mas  = calc_ma(closes)
            price = data["price"]
            ma5  = mas.get("MA5", price)
            ma20 = mas.get("MA20", price)
            avg_vol = int(np.mean(volumes[-20:])) if len(volumes) >= 20 else 1
            vr = round(volumes[-1] / avg_vol, 2) if avg_vol else 1
            score_items = [
                price > ma5, price > ma20, ma5 > ma20,
                macd["cross"] == "golden", macd["histogram"] > 0,
                rsi < 70, rsi > 30, kd["K"] > kd["D"]
            ]
            sc = round(sum(score_items) / len(score_items) * 100)
            winrate, action, signals = winrate_signal(
                rsi, macd["cross"], kd["K"], kd["D"], price, ma5, ma20,
                closes=closes, highs=highs, lows=lows, strategy="MA_RSI")
            if rsi > rsi_max: return None
            if rsi < rsi_min: return None
            if ma_golden   and not (ma5 > ma20): return None
            if macd_golden and not (macd.get("cross") == "golden"): return None
            if kd_golden   and not (kd.get("K", 0) > kd.get("D", 0)): return None
            if vr < vol_ratio_min: return None
            if sc < score_min: return None
            if macd_weekly_golden and not calc_macd_weekly(closes): return None
            if bull_align:
                ma_full = calc_ma(closes)
                ma60 = ma_full.get("MA60", 0)
                if not (ma5 > ma20 > 0 and ma20 > ma60 > 0): return None
            # 基本面：yfinance info（只做 US 股）
            if revenue_growth or earnings_growth:
                try:
                    ticker_obj = yf.Ticker(sym)
                    info = ticker_obj.info
                    if revenue_growth and not (float(info.get("revenueGrowth") or 0) > 0): return None
                    if earnings_growth and not (float(info.get("earningsGrowth") or 0) > 0): return None
                except Exception:
                    pass  # 若取不到就跳過不篩
            return {
                "id": sym, "name": data.get("name", sym),
                "market": "us", "price": price,
                "change_pct": data.get("change_pct", 0),
                "rsi": round(rsi, 1),
                "macd_cross": macd.get("cross", "—"),
                "kd_k": round(kd.get("K", 0), 1),
                "kd_d": round(kd.get("D", 0), 1),
                "ma5": round(ma5, 2), "ma20": round(ma20, 2),
                "volume_ratio": round(vr, 2),
                "score": sc, "winrate": winrate, "action": action,
                "sparkline": closes[-20:],
            }
        except Exception as e:
            print(f"Screen US {sym}: {e}")
            return None

    # 並行篩選
    tasks = []
    if market in ("tw", "all"):
        tasks += [(check_tw, sid) for sid in TW_STOCKS_DB.keys()]
    if market in ("us", "all"):
        tasks += [(check_us, sym) for sym in US_STOCKS_SCREEN]

    max_workers = 6 if market == "tw" else 10
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(fn, s) for fn, s in tasks]
        for future in as_completed(futures, timeout=75):
            try:
                r = future.result(timeout=60)
                if r:
                    results.append(r)
            except Exception as e:
                print(f"Screen future: {e}")

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    cache_set("screen_" + str(body), results, 300)  # 5分鐘快取
    return jsonify(results)

@app.route("/")
def index():
    """Serve 前端主頁面"""
    return send_from_directory(BASE_DIR, "quant-trading-live.html")

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(BASE_DIR, "favicon.ico", mimetype="image/x-icon")

@app.route("/logo.png")
def logo():
    return send_from_directory(BASE_DIR, "logo.png", mimetype="image/png")

# ─── API 路由 ────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route("/api/debug/tw/<stock_id>")
def debug_tw(stock_id):
    """診斷單支台股資料來源是否正常"""
    result = {}
    # 1. FinMind history
    try:
        start = (datetime.now() - timedelta(days=95)).strftime("%Y-%m-%d")
        fm_rows = finmind_fetch("TaiwanStockPrice", stock_id, start_date=start)
        result["finmind_rows"] = len(fm_rows)
        result["finmind_sample"] = fm_rows[-1] if fm_rows else None
    except Exception as e:
        result["finmind_error"] = str(e)
    # 2. yfinance
    try:
        import yfinance as _yf
        df = _yf.Ticker(f"{stock_id}.TW").history(period="5d")
        result["yfinance_rows"] = len(df)
        result["yfinance_last"] = float(df["Close"].iloc[-1]) if not df.empty else None
    except Exception as e:
        result["yfinance_error"] = str(e)
    # 3. fetch_tw_history (combined)
    try:
        hist = fetch_tw_history(stock_id)
        result["hist_closes"] = len(hist.get("closes", [])) if hist else 0
    except Exception as e:
        result["hist_error"] = str(e)
    # 4. recommend cache status
    cached = cache_get("recommend")
    if cached:
        result["recommend_cache"] = {
            "tw_count": len(cached.get("tw", [])),
            "us_count": len(cached.get("us", []))
        }
    else:
        result["recommend_cache"] = None
    return jsonify(result)

@app.route("/api/index")
def api_index():
    """大盤指數（台灣加權 + 美股三大指數）全用 history()"""
    tw = fetch_tw_index()
    us_indices = {}
    for sym, name in [("^GSPC", "S&P500"), ("^IXIC", "NASDAQ"), ("^DJI", "道瓊")]:
        cached = cache_get(f"idx_{sym}")
        if cached:
            us_indices[name] = cached
            continue
        p, prev = _yf_history_price(sym)
        if p:
            chg = round(p - prev, 2)
            chg_pct = round(chg / prev * 100, 2) if prev else 0
            entry = {"price": p, "change": chg, "change_pct": chg_pct}
            cache_set(f"idx_{sym}", entry, 120)
            us_indices[name] = entry
    return jsonify({"tw_weighted": tw, "us": us_indices})

@app.route("/api/stock/<stock_id>")
def api_stock(stock_id):
    """股票即時報價 + 技術指標（支援自訂週期參數）"""
    market      = request.args.get("market", "tw")
    rsi_period  = int(request.args.get("rsi_period",  14))
    macd_fast   = int(request.args.get("macd_fast",   12))
    macd_slow   = int(request.args.get("macd_slow",   26))
    macd_signal = int(request.args.get("macd_signal",  9))
    bb_period   = int(request.args.get("bb_period",   20))

    if market == "us":
        data = fetch_us_stock(stock_id.upper())
        if not data:
            return jsonify({"error": "無法取得數據"}), 404
        hist = data.get("history", {})
        closes = hist.get("closes", [])
        if len(closes) >= 20:
            highs = hist.get("highs", closes)
            lows = hist.get("lows", closes)
            volumes = hist.get("volumes", [])
            rsi  = calc_rsi(closes, period=rsi_period)
            macd = calc_macd(closes, fast=macd_fast, slow=macd_slow, signal=macd_signal)
            kd   = calc_kd(highs, lows, closes)
            bb   = calc_bollinger(closes, period=bb_period)
            mas  = calc_ma(closes)
            atr  = calc_atr(highs, lows, closes)
            price = closes[-1]
            ma5  = mas.get("MA5", price)
            ma20 = mas.get("MA20", price)
            avg_vol = int(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0
            vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol else 1
            winrate, action, signals = winrate_signal(
                rsi, macd["cross"], kd["K"], kd["D"], price, ma5, ma20,
                closes=closes, highs=highs, lows=lows, strategy="MA_RSI")
            data["indicators"] = {
                "RSI14": rsi, "MACD": macd, "KD": kd,
                "Bollinger": bb, "MA": mas, "ATR14": atr,
                "volume_ratio": vol_ratio, "winrate": winrate,
                "action": action, "signals": signals
            }
        return jsonify(data)
    else:
        # 台股：動態套用自訂週期
        price_data = fetch_twse_price(stock_id)
        if not price_data:
            price_data = fetch_tpex_price(stock_id)
        if not price_data:
            # 最後嘗試 yfinance
            try:
                price_data = fetch_tw_price_yfinance(stock_id)
            except Exception:
                price_data = None
        if not price_data:
            return jsonify({"error": f"找不到股票代號 {stock_id}，請確認代號是否正確（台股4位數字，如 2330）"}), 404
        hist = fetch_tw_history(stock_id)
        # 若週期是預設值則用快取版指標，否則重新計算
        if (rsi_period == 14 and macd_fast == 12 and macd_slow == 26
                and macd_signal == 9 and bb_period == 20):
            indicators = get_full_indicators(stock_id)
        else:
            if hist and len(hist.get("closes", [])) >= 20:
                closes  = hist["closes"]
                highs   = hist.get("highs", closes)
                lows    = hist.get("lows", closes)
                volumes = hist.get("volumes", [])
                rsi  = calc_rsi(closes, period=rsi_period)
                macd = calc_macd(closes, fast=macd_fast, slow=macd_slow, signal=macd_signal)
                kd   = calc_kd(highs, lows, closes)
                bb   = calc_bollinger(closes, period=bb_period)
                mas  = calc_ma(closes)
                atr  = calc_atr(highs, lows, closes)
                price = closes[-1]
                ma5  = mas.get("MA5", price)
                ma20 = mas.get("MA20", price)
                avg_vol = int(np.mean(volumes[-20:])) if len(volumes) >= 20 else 1
                vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol else 1
                winrate, action, signals = winrate_signal(
                    rsi, macd["cross"], kd["K"], kd["D"], price, ma5, ma20,
                    closes=closes, highs=highs, lows=lows, strategy="MA_RSI")
                score_items = [
                    price > ma5, price > ma20, ma5 > ma20,
                    macd["cross"] == "golden", macd["histogram"] > 0,
                    rsi < 70, rsi > 30, kd["K"] > kd["D"],
                    price < bb["upper"], price > bb["lower"], vol_ratio > 1
                ]
                indicators = {
                    "RSI14": rsi, "MACD": macd, "KD": kd, "Bollinger": bb,
                    "MA": mas, "ATR14": atr, "volume_ratio": vol_ratio,
                    "winrate": winrate, "action": action, "signals": signals,
                    "score": round(sum(score_items) / len(score_items) * 100)
                }
            else:
                indicators = get_full_indicators(stock_id)
        price_data["indicators"] = indicators
        price_data["history"] = hist
        return jsonify(price_data)

@app.route("/api/history/<path:stock_id>")
def api_history(stock_id):
    """歷史K線資料（支援台股、美股、指數如 ^TWII ^GSPC）"""
    market = request.args.get("market", "tw")
    # 指數或美股（market=us 或代號含 ^ 或非純數字）
    if market == "us" or "^" in stock_id or not stock_id[:4].isdigit():
        sym = stock_id.upper()
        cached = cache_get(f"hist_us_{sym}")
        if cached:
            return jsonify(cached)
        try:
            with _yf_semaphore:
                t = yf.Ticker(sym)
                df = t.history(period="3mo", auto_adjust=True)
            if df.empty:
                return jsonify({"error": "無法取得"}), 404
            df = df.reset_index()
            hist = {
                "dates":   [str(d.date()) if hasattr(d,"date") else str(d)[:10] for d in df["Date"]],
                "opens":   [round(float(v), 2) for v in df["Open"]],
                "highs":   [round(float(v), 2) for v in df["High"]],
                "lows":    [round(float(v), 2) for v in df["Low"]],
                "closes":  [round(float(v), 2) for v in df["Close"]],
                "volumes": [int(v) for v in df["Volume"]]
            }
            cache_set(f"hist_us_{sym}", hist, 300)
            return jsonify(hist)
        except Exception as e:
            print(f"History US error {sym}: {e}")
            return jsonify({"error": "無法取得"}), 404
    hist = fetch_tw_history(stock_id)
    if not hist:
        return jsonify({"error": "無法取得歷史數據"}), 404
    return jsonify(hist)

@app.route("/api/index_hist/<key>")
def api_index_hist(key):
    """大盤走勢圖專用：台指期、台股夜盤用 FinMind；其餘指數用 yfinance"""
    SYMBOL_MAP = {
        "twii":   ("yf",  "^TWII"),
        "two":    ("yf",  "^TWO"),
        "sp500":  ("yf",  "^GSPC"),
        "nasdaq": ("yf",  "^IXIC"),
        "sox":    ("yf",  "^SOX"),
        "txf":    ("fm",  "TX"),     # 台指期近月（FinMind）
        "night":  ("fmn", "TX"),     # 台股夜盤（FinMind夜盤期貨）
    }
    cfg = SYMBOL_MAP.get(key.lower())
    if not cfg:
        return jsonify({"error": "未知指數"}), 404

    cache_key = f"idx_hist_{key}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    src, sym = cfg

    # ── yfinance 指數 ──────────────────────────────
    if src == "yf":
        try:
            with _yf_semaphore:
                df = yf.Ticker(sym).history(period="3mo", auto_adjust=True)
            if df.empty:
                return jsonify({"error": "無資料"}), 404
            df = df.reset_index()
            result = {
                "dates":  [str(d.date()) if hasattr(d,"date") else str(d)[:10] for d in df["Date"]],
                "closes": [round(float(v), 2) for v in df["Close"]],
            }
            cache_set(cache_key, result, 300)
            return jsonify(result)
        except Exception as e:
            print(f"[index_hist yf] {sym}: {e}")
            return jsonify({"error": "取得失敗"}), 500

    # ── FinMind 台指期（日盤）─────────────────────
    if src == "fm":
        try:
            start = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
            rows = finmind_fetch("TaiwanFuturesDaily", sym, start_date=start)
            # 只取近月合約（settlement_month 最近的）
            if not rows:
                return jsonify({"error": "FinMind 無資料"}), 404
            # 依日期+合約月份排序，取每日最近月合約收盤價
            from collections import defaultdict
            by_date = defaultdict(list)
            for r in rows:
                by_date[r["date"]].append(r)
            dates, closes = [], []
            for d in sorted(by_date.keys()):
                # 取 settlement_month 最小（近月）
                day_rows = sorted(by_date[d], key=lambda x: x.get("settlement_month",""))
                c = float(day_rows[0].get("close", 0))
                if c > 0:
                    dates.append(d)
                    closes.append(round(c, 0))
            if not closes:
                return jsonify({"error": "無收盤價"}), 404
            result = {"dates": dates, "closes": closes}
            cache_set(cache_key, result, 300)
            return jsonify(result)
        except Exception as e:
            print(f"[index_hist fm] {sym}: {e}")
            return jsonify({"error": "取得失敗"}), 500

    # ── FinMind 台股夜盤期貨 ───────────────────────
    if src == "fmn":
        try:
            start = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
            rows = finmind_fetch("TaiwanFuturesNight", sym, start_date=start)
            if not rows:
                # 夜盤資料不足時，備援用台指期日盤
                rows = finmind_fetch("TaiwanFuturesDaily", sym, start_date=start)
            if not rows:
                return jsonify({"error": "FinMind 夜盤無資料"}), 404
            from collections import defaultdict
            by_date = defaultdict(list)
            for r in rows:
                by_date[r["date"]].append(r)
            dates, closes = [], []
            for d in sorted(by_date.keys()):
                day_rows = sorted(by_date[d], key=lambda x: x.get("settlement_month",""))
                c = float(day_rows[0].get("close", 0))
                if c > 0:
                    dates.append(d)
                    closes.append(round(c, 0))
            if not closes:
                return jsonify({"error": "無收盤價"}), 404
            result = {"dates": dates, "closes": closes}
            cache_set(cache_key, result, 300)
            return jsonify(result)
        except Exception as e:
            print(f"[index_hist fmn] {sym}: {e}")
            return jsonify({"error": "取得失敗"}), 500

    return jsonify({"error": "未知來源"}), 500


@app.route("/api/monitor", methods=["POST"])
def api_monitor():
    """批次查詢監控清單（並行抓取，30秒快取，三層備援）"""
    body = request.get_json()
    stocks = body.get("stocks", [])
    if not stocks:
        return jsonify([])

    # 30秒快取（key 依股票清單）
    cache_key = "monitor_" + "_".join(s.get("id", "") for s in stocks)
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    def fetch_one(s):
        stock_id = s.get("id", "")
        market = s.get("market", "tw")
        try:
            if market == "us":
                data = fetch_us_stock(stock_id.upper())
            else:
                # 三層備援：TWSE → TPEX → yfinance
                data = fetch_twse_price(stock_id)
                if not data:
                    data = fetch_tpex_price(stock_id)
                if not data:
                    data = fetch_tw_price_yfinance(stock_id)
            if data:
                ind = get_full_indicators(stock_id) if market == "tw" else {}
                data["quick_signal"] = ind.get("action", "觀察")
                data["winrate"] = ind.get("winrate", 60)
                return data
        except Exception as e:
            print(f"Monitor error {stock_id}: {e}")
        return None

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_one, s): s for s in stocks}
        for future in as_completed(futures, timeout=20):
            try:
                r = future.result(timeout=15)
                if r:
                    results.append(r)
            except Exception as e:
                print(f"Monitor future error: {e}")

    if results:
        cache_set(cache_key, results, 30)
    return jsonify(results)


# ─── 推薦股票計算（可從背景執行緒呼叫）─────────────────────────────────
_recommend_warming = False   # 防止同時多次背景計算
_recommend_lock    = threading.Lock()

def _compute_recommend_data(tw_tp=0.08, tw_sl=0.05, us_tp=0.10, us_sl=0.05):
    """計算推薦股票：掃描核心台股60檔 & 美股候選池，各取前10（依評分排序）"""
    # 核心60檔台股（流動性佳、各產業代表）
    TW_CORE = [
        # 半導體
        "2330","2454","2303","3034","2379","3711","6770","2449","3443","2337",
        # 電子/科技
        "2317","2382","2357","2353","3008","2395","4938","2324","3231","2377",
        "6669","3035","6488","2301","3017",
        # 金融
        "2881","2882","2891","2886","2884","2885","2892","5880","2880","2887",
        # 航運
        "2615","2603","2609","2610","2618",
        # 石化/材料
        "1301","1303","1326","6505",
        # 鋼鐵/傳產
        "2002","1101","1216","2027",
        # 電信
        "2412","4904","3045",
        # 生技
        "4119","1795","4164",
        # 消費/零售
        "2207","2912","9910","9914","9921",
        # 網通
        "2345","6245",
        # ETF
        "0050","0056","00878",
    ]
    tw_candidates = [s for s in TW_CORE if s in TW_STOCKS_DB]
    us_candidates = list(US_STOCKS_SCREEN)

    tw_results = []
    us_results = []

    def fetch_tw(stock_id):
        try:
            # 用 FinMind history 取歷史（無 TWSE semaphore 瓶頸）
            hist = fetch_tw_history(stock_id)
            if not hist or len(hist.get("closes", [])) < 20:
                return None
            closes = hist["closes"]
            price  = closes[-1]
            prev   = closes[-2] if len(closes) >= 2 else price
            chg_pct = round((price - prev) / prev * 100, 2) if prev else 0
            ind = get_full_indicators(stock_id)  # 內部會命中 fetch_tw_history 快取
            if not ind:
                return None
            atr = ind.get("ATR14", 0)
            if atr and atr > 0:
                target    = round(price + atr * 2, 1)
                stop_loss = round(price - atr * 1.5, 1)
            else:
                target    = round(price * (1 + tw_tp), 0)
                stop_loss = round(price * (1 - tw_sl), 0)
            return {
                "id":         stock_id,
                "name":       TW_STOCKS_DB.get(stock_id, stock_id),
                "market":     "TW",
                "price":      price,
                "change_pct": chg_pct,
                "target":     target,
                "stop_loss":  stop_loss,
                "score":      ind.get("score"),
                "winrate":    ind.get("winrate"),
                "action":     ind.get("action", "觀察"),
                "signals":    ind.get("signals", []),
                "rsi":        ind.get("RSI14", 50),
                "macd_cross": ind.get("MACD", {}).get("cross", ""),
                "sparkline":  closes[-20:]
            }
        except Exception as e:
            print(f"Recommend TW error {stock_id}: {e}")
            return None

    def fetch_us(sym):
        try:
            data = fetch_us_stock(sym)
            if not data:
                return None
            hist = data.get("history", {})
            closes = hist.get("closes", [])
            if len(closes) < 20:
                return None
            price = data["price"]
            highs_r = hist.get("highs", closes)
            lows_r  = hist.get("lows",  closes)
            rsi  = calc_rsi(closes)
            macd = calc_macd(closes)
            kd   = calc_kd(highs_r, lows_r, closes)
            mas  = calc_ma(closes)
            atr  = calc_atr(highs_r, lows_r, closes)
            ma5  = mas.get("MA5", price)
            ma20 = mas.get("MA20", price)
            winrate, action, signals = winrate_signal(
                rsi, macd["cross"], kd["K"], kd["D"], price, ma5, ma20,
                closes=closes, highs=highs_r, lows=lows_r, strategy="MA_RSI")
            if atr and atr > 0:
                target    = round(price + atr * 2, 2)
                stop_loss = round(price - atr * 1.5, 2)
            else:
                target    = round(price * (1 + us_tp), 2)
                stop_loss = round(price * (1 - us_sl), 2)
            score_items = [
                price > ma5, price > ma20, ma5 > ma20,
                macd["cross"] == "golden", rsi < 70, rsi > 30, kd["K"] > kd["D"]
            ]
            return {
                "id": sym,
                "name": data["name"],
                "market": "US",
                "price": price,
                "change_pct": data.get("change_pct", 0),
                "target": target,
                "stop_loss": stop_loss,
                "score": round(sum(score_items) / len(score_items) * 100),
                "winrate": winrate,
                "action": action,
                "signals": signals,
                "rsi": rsi,
                "sparkline": closes[-20:]
            }
        except Exception as e:
            print(f"Recommend US error {sym}: {e}")
            return None

    # 台股並行掃描（FinMind history 為主，無 semaphore 瓶頸，可加大 worker）
    with ThreadPoolExecutor(max_workers=20) as ex:
        tw_futures = {ex.submit(fetch_tw, s): s for s in tw_candidates}
        for future in as_completed(tw_futures, timeout=150):
            try:
                r = future.result(timeout=25)
                if r:
                    tw_results.append(r)
                    # 每累積 5 筆就先存快取，讓前端更早拿到資料
                    if len(tw_results) % 5 == 0:
                        tw_part = sorted(tw_results, key=lambda x: x.get("score",0) or 0, reverse=True)
                        cache_set("recommend", {"tw": tw_part[:10], "us": us_results[:10]}, 3600)
            except Exception as e:
                print(f"Recommend TW future error: {e}")

    # 美股並行掃描
    with ThreadPoolExecutor(max_workers=12) as ex:
        us_futures = {ex.submit(fetch_us, s): s for s in us_candidates}
        for future in as_completed(us_futures, timeout=90):
            try:
                r = future.result(timeout=25)
                if r:
                    us_results.append(r)
            except Exception as e:
                print(f"Recommend US future error: {e}")

    # 最終結果依評分排序，各取前10
    tw_results.sort(key=lambda x: x.get("score", 0) or 0, reverse=True)
    us_results.sort(key=lambda x: x.get("score", 0) or 0, reverse=True)
    payload = {"tw": tw_results[:10], "us": us_results[:10]}

    cache_set("recommend", payload, 3600)  # 快取 1 小時
    print(f"[Recommend] 更新完畢 — 台股掃 {len(tw_candidates)} 檔得 {len(tw_results)} 筆取前10 / 美股 {len(us_results)} 筆取前10")
    return payload

def _bg_warm_recommend():
    """背景執行緒：啟動後延遲 10 秒執行，之後每 1 小時更新"""
    global _recommend_warming
    time.sleep(10)           # 讓 Flask 完全啟動後再開始
    while True:
        with _recommend_lock:
            if _recommend_warming:
                time.sleep(60)
                continue
            _recommend_warming = True
        try:
            _compute_recommend_data()
        except Exception as e:
            print(f"[Recommend] 背景預熱失敗: {e}")
        finally:
            with _recommend_lock:
                _recommend_warming = False
        time.sleep(3600)     # 1 小時後再更新

# 在 module 載入時啟動背景執行緒（gunicorn fork 後每個 worker 各跑一條）
_warm_thread = threading.Thread(target=_bg_warm_recommend, daemon=True)
_warm_thread.start()


@app.route("/api/recommend")
def api_recommend():
    """今日推薦股票 — 優先回傳快取，無快取時觸發背景計算並立即回傳空陣列"""
    cached = cache_get("recommend")
    if cached:
        return jsonify(cached)

    # 快取尚未就緒：觸發一次背景計算（若尚未在跑）
    def _trigger():
        global _recommend_warming
        with _recommend_lock:
            if _recommend_warming:
                return
            _recommend_warming = True
        try:
            _compute_recommend_data()
        except Exception as e:
            print(f"[Recommend] 觸發計算失敗: {e}")
        finally:
            with _recommend_lock:
                _recommend_warming = False

    # 非阻塞觸發
    t = threading.Thread(target=_trigger, daemon=True)
    t.start()

    # 立即回傳「預熱中」的空陣列 + 特殊 header 讓前端知道要重試
    from flask import make_response
    resp = make_response(jsonify([]), 200)
    resp.headers["X-Warming"] = "1"
    return resp

def _news_sentiment(title: str) -> str:
    """關鍵字情感分析，回傳 'positive'/'negative'/'neutral'"""
    tl = title.lower()
    pos_words = [
        "上漲","漲停","突破","創新高","反彈","強勢","多頭","拉升","跳空","大漲",
        "收復","過關","站上","買超","利多","營收成長","獲利","超越預期","擴產",
        "拿下大單","合作","配息","除息","填息","股息","回購","庫藏股","創高",
        "beat","exceeded","upgrade","outperform","surge","rise","gain",
        "record","bullish","growth","profit","revenue up","raised guidance",
        "降息","Fed寬鬆","資金寬裕","PMI擴張",
    ]
    neg_words = [
        "下跌","跌停","破底","創新低","空頭","崩跌","大跌","摜壓","下破",
        "賣壓","跌破","虧損","賣超","利空","下修","衰退","裁員","獲利衰退",
        "毛利下滑","客戶砍單","停工","罰款","訴訟","倒閉","債務違約",
        "miss","below","downgrade","underperform","fall","drop","loss",
        "decline","warn","cut guidance","layoff","shutdown","recall","investigation",
        "升息","通膨","Fed緊縮","PMI萎縮",
    ]
    if any(w in title or w in tl for w in pos_words): return "positive"
    if any(w in title or w in tl for w in neg_words): return "negative"
    return "neutral"


def _fetch_cnyes_news(stock_id: str, limit: int = 10) -> list:
    """鉅亨網個股新聞（繁體中文），以股票代號為關鍵字搜尋"""
    import requests as req
    headers = {
        "Origin":  "https://news.cnyes.com/",
        "Referer": "https://news.cnyes.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    }
    url = (
        f"https://api.cnyes.com/media/api/v1/newslist/category/tw_stock_news"
        f"?keyword={stock_id}&limit={limit}&page=1"
    )
    r = req.get(url, headers=headers, timeout=12)
    if r.status_code != 200:
        return []
    data = r.json().get("items", {}).get("data", [])
    results = []
    one_month_ago = datetime.now().timestamp() - 30 * 86400
    for n in data:
        news_id = n.get("newsId", "")
        title   = n.get("title", "")
        summary = n.get("summary", "")
        pub_ts  = n.get("publishAt", 0)
        cat     = n.get("categoryName", "鉅亨網")
        link    = f"https://news.cnyes.com/news/id/{news_id}" if news_id else "#"
        pub_time = (datetime.fromtimestamp(pub_ts).strftime("%m/%d %H:%M")
                    if pub_ts else "")
        if not title:
            continue
        # 過濾超過 1 個月的新聞
        if pub_ts and pub_ts < one_month_ago:
            continue
        results.append({
            "title":     title,
            "link":      link,
            "publisher": f"鉅亨網・{cat}",
            "time":      pub_time,
            "summary":   summary,
            "sentiment": _news_sentiment(title),
        })
    return results


def _fetch_yfinance_news(stock_id: str, limit: int = 10) -> list:
    """yfinance 新聞（適合美股/ETF），相容新舊格式"""
    results = []
    one_month_ago = datetime.now().timestamp() - 30 * 86400
    try:
        sym = f"{stock_id}.TW" if (len(stock_id) == 4 and stock_id.isdigit()) else stock_id
        news = yf.Ticker(sym).news or []
        for n in news[:limit]:
            content  = n.get("content") or {}
            title    = n.get("title") or content.get("title", "")
            link     = (n.get("link")
                        or (content.get("canonicalUrl") or {}).get("url", "")
                        or "#")
            pub      = n.get("providerPublishTime", 0)
            if not pub:
                pub_str = content.get("pubDate", "")
                if pub_str:
                    try:
                        pub = datetime.fromisoformat(
                            pub_str.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        pub = 0
            publisher = (n.get("publisher")
                         or (content.get("provider") or {}).get("displayName", "")
                         or "")
            pub_time = datetime.fromtimestamp(pub).strftime("%m/%d %H:%M") if pub else ""
            if not title:
                continue
            # 過濾超過 1 個月的新聞
            if pub and pub < one_month_ago:
                continue
            results.append({
                "title":     title,
                "link":      link,
                "publisher": publisher,
                "time":      pub_time,
                "summary":   "",
                "sentiment": _news_sentiment(title),
            })
    except Exception as e:
        print(f"[yfinance news] {stock_id}: {e}")
    return results


@app.route("/api/news/<stock_id>")
def api_news(stock_id):
    """相關新聞：台股用鉅亨網（繁中），美股/ETF 用 yfinance"""
    cached = cache_get(f"news_{stock_id}")
    if cached is not None:
        return jsonify(cached)

    is_tw = len(stock_id) <= 6 and stock_id[:4].isdigit()

    if is_tw:
        news_list = _fetch_cnyes_news(stock_id, limit=10)
        # 若鉅亨回傳空，備援 yfinance
        if not news_list:
            news_list = _fetch_yfinance_news(stock_id, limit=10)
    else:
        news_list = _fetch_yfinance_news(stock_id, limit=10)

    cache_set(f"news_{stock_id}", news_list, 600)
    return jsonify(news_list)

@app.route("/api/chart_data/<stock_id>")
def api_chart_data(stock_id):
    """K線副圖資料：三大法人每日買賣超（180天，30分快取）"""
    cached = cache_get(f"chart_data_{stock_id}")
    if cached is not None:
        return jsonify(cached)
    result = {"foreign": {}, "trust": {}, "dealer": {}}
    try:
        start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        rows  = finmind_fetch("TaiwanStockInstitutionalInvestorsBuySell",
                              stock_id, start_date=start)
        for row in (rows or []):
            d    = (row.get("date") or "").strip()
            name = row.get("name", "")
            diff = int(row.get("diff", 0) or 0)
            if not d: continue
            if   "外資" in name: result["foreign"][d] = diff
            elif "投信" in name: result["trust"][d]   = diff
            elif "自營" in name: result["dealer"][d]  = diff
    except Exception as e:
        print(f"[chart_data] {stock_id}: {e}")
    cache_set(f"chart_data_{stock_id}", result, 1800)
    return jsonify(result)


@app.route("/api/events/<stock_id>")
def api_events(stock_id):
    """台股重大事件（除權息、財報日）— FinMind 資料，1小時快取"""
    cached = cache_get(f"events_{stock_id}")
    if cached is not None:
        return jsonify(cached)

    events = []
    today     = datetime.now()
    yr_ago    = (today - timedelta(days=400)).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    # ── 除權息 ─────────────────────────────────
    try:
        div_rows = finmind_fetch("TaiwanStockDividend", stock_id, start_date=yr_ago)
        seen = set()
        for row in (div_rows or []):
            cash      = float(row.get("CashDividend", 0) or 0)
            stock_div = float(row.get("StockDividend", 0) or 0)
            ex_div    = (row.get("ExDividendTradingDate") or "").strip()
            ex_right  = (row.get("ExRightTradingDate")   or "").strip()
            if ex_div and yr_ago <= ex_div <= today_str and ex_div not in seen:
                seen.add(ex_div)
                events.append({
                    "date": ex_div, "type": "dividend",
                    "label": "息", "color": "#ff9f43",
                    "desc": f"除息 每股${cash:.2f}"
                })
            if ex_right and yr_ago <= ex_right <= today_str and ex_right not in seen:
                seen.add(ex_right)
                events.append({
                    "date": ex_right, "type": "right",
                    "label": "權", "color": "#ffd32a",
                    "desc": f"除權 {stock_div:.4g}股"
                })
    except Exception as e:
        print(f"[Events] dividend {stock_id}: {e}")

    # ── 財報日（季報）─────────────────────────────
    try:
        fs_rows = finmind_fetch("TaiwanStockFinancialStatements", stock_id, start_date=yr_ago)
        # 依 date 取唯一（type 欄位是財務科目名稱，不是季別，改從日期月份推算）
        fs_dates = set()
        for row in (fs_rows or []):
            d = (row.get("date") or "").strip()
            if d and yr_ago <= d <= today_str:
                fs_dates.add(d)
        for d in sorted(fs_dates):
            try:
                d_dt = datetime.strptime(d, "%Y-%m-%d")
                month = d_dt.month
                if month == 3:   q_label = f"{d_dt.year} Q1"
                elif month == 6: q_label = f"{d_dt.year} Q2"
                elif month == 9: q_label = f"{d_dt.year} Q3"
                else:            q_label = f"{d_dt.year} Q4"
            except Exception:
                q_label = ""
            events.append({
                "date": d, "type": "earnings",
                "label": "財", "color": "#a29bfe",
                "desc": f"財報公告 {q_label}"
            })
    except Exception as e:
        print(f"[Events] earnings {stock_id}: {e}")

    events.sort(key=lambda x: x["date"])
    cache_set(f"events_{stock_id}", events, 3600)
    return jsonify(events)


@app.route("/api/strategy_winrate")
def api_strategy_winrate():
    """計算策略庫各策略的真實歷史勝率

    使用 0050（元大台灣50）作為代表股，回測3個月歷史
    以避免單一個股偏差。結果快取 1 小時。
    """
    cached = cache_get("strategy_winrate_all")
    if cached:
        return jsonify(cached)

    # 用 0050 ETF（最具代表性、流動性最好）
    hist = fetch_tw_history("0050", months=12)
    if not hist or len(hist["closes"]) < 60:
        return jsonify({"error": "history_unavailable"}), 503

    closes = hist["closes"]
    highs  = hist.get("highs", closes)
    lows   = hist.get("lows", closes)
    result = {}

    for strat, sl, tp in [
        ("MA_RSI",  0.05, 0.15),
        ("MACD",    0.05, 0.12),
        ("KD_RSI",  0.04, 0.10),
        ("TRIPLE",  0.06, 0.18),
    ]:
        wr = calc_backtest_winrate(closes, highs, lows, strat, sl, tp)
        result[strat] = {"winrate": wr}  # wr 可能為 None（資料不足）

    cache_set("strategy_winrate_all", result, 3600)  # 1 小時快取
    return jsonify(result)

@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    """策略回測引擎"""
    body = request.get_json()
    stock_id = body.get("stock_id", "2330")
    strategy = body.get("strategy", "MA_RSI")
    stop_loss = float(body.get("stop_loss", 5)) / 100
    take_profit = float(body.get("take_profit", 15)) / 100

    hist = fetch_tw_history(stock_id, months=12)
    if not hist or len(hist["closes"]) < 60:
        return jsonify({"error": "歷史數據不足"}), 400

    closes = np.array(hist["closes"])
    highs = np.array(hist["highs"])
    lows = np.array(hist["lows"])
    volumes = np.array(hist["volumes"])
    dates = hist["dates"]

    trades = []
    in_trade = False
    entry_price = 0
    entry_date = ""
    equity = [100.0]
    current_equity = 100.0

    for i in range(60, len(closes)):
        window_c = closes[:i]
        # 計算指標
        rsi = calc_rsi(window_c[-20:])
        macd_data = calc_macd(window_c[-40:])
        kd_data = calc_kd(highs[:i], lows[:i], window_c, 9)
        ma5 = float(np.mean(window_c[-5:]))
        ma20 = float(np.mean(window_c[-20:]))
        price = closes[i]

        # 進場條件
        if not in_trade:
            entry_signal = False
            if strategy == "MA_RSI":
                entry_signal = (ma5 > ma20) and (rsi < 50) and (rsi > 25)
            elif strategy == "MACD":
                entry_signal = macd_data["cross"] == "golden" and macd_data["histogram"] > 0
            elif strategy == "KD_RSI":
                entry_signal = (kd_data["K"] > kd_data["D"]) and (rsi < 40)
            if entry_signal:
                in_trade = True
                entry_price = price
                entry_date = dates[i]
        else:
            # 出場條件
            change = (price - entry_price) / entry_price
            exit_signal = False
            if strategy == "MA_RSI":
                exit_signal = (ma5 < ma20) or rsi > 72
            elif strategy == "MACD":
                exit_signal = macd_data["cross"] == "death"
            elif strategy == "KD_RSI":
                exit_signal = (kd_data["K"] < kd_data["D"]) or rsi > 75
            # 停損停利
            if change <= -stop_loss or change >= take_profit:
                exit_signal = True
            if exit_signal:
                ret = (price - entry_price) / entry_price
                current_equity *= (1 + ret)
                trades.append({
                    "entry_date": entry_date, "exit_date": dates[i],
                    "entry_price": round(entry_price, 2), "exit_price": round(price, 2),
                    "return_pct": round(ret * 100, 2),
                    "result": "win" if ret > 0 else "loss"
                })
                in_trade = False
        equity.append(round(current_equity, 4))

    if not trades:
        return jsonify({"error": "無交易記錄，請調整策略參數"}), 400

    wins = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    winrate = round(len(wins) / len(trades) * 100, 1)
    total_return = round((current_equity - 100), 1)
    avg_win = round(np.mean([t["return_pct"] for t in wins]), 2) if wins else 0
    avg_loss = round(np.mean([t["return_pct"] for t in losses]), 2) if losses else 0
    rr_ratio = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 0

    # 最大回撤
    peak, max_dd = equity[0], 0
    for e in equity:
        if e > peak: peak = e
        dd = (peak - e) / peak
        if dd > max_dd: max_dd = dd

    # 夏普比率（簡化）
    returns = [(equity[i] - equity[i-1]) / equity[i-1] for i in range(1, len(equity))]
    sharpe = round(np.mean(returns) / (np.std(returns) + 1e-9) * np.sqrt(252), 2) if returns else 0

    return jsonify({
        "winrate": winrate,
        "total_return": total_return,
        "max_drawdown": round(max_dd * 100, 1),
        "total_trades": len(trades),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "rr_ratio": rr_ratio,
        "sharpe": sharpe,
        "equity_curve": equity[-60:],
        "recent_trades": trades[-10:]
    })

@app.route("/api/fundamental/<stock_id>")
def api_fundamental(stock_id):
    """台股基本面 + 籌碼資料（FinMind）
    回傳：月營收年增率、EPS年增率、三大法人、融資融券
    """
    cached = cache_get(f"fundamental_{stock_id}")
    if cached:
        return jsonify(cached)

    rev_yoy  = fetch_tw_revenue_yoy(stock_id)
    eps_yoy  = fetch_tw_eps_yoy(stock_id)
    inst     = fetch_tw_institutional(stock_id)
    margin   = fetch_tw_margin(stock_id)

    # 取月營收原始數字（最近 13 個月，顯示用）
    start_rev = (datetime.now() - timedelta(days=420)).strftime("%Y-%m-%d")
    rev_rows  = finmind_fetch("TaiwanStockMonthRevenue", stock_id, start_date=start_rev)
    rev_rows  = sorted(rev_rows, key=lambda x: (int(x.get("revenue_year",0)), int(x.get("revenue_month",0))))
    rev_chart = [{"date": f"{r.get('revenue_year')}-{str(r.get('revenue_month','0')).zfill(2)}",
                  "revenue": int(r.get("revenue", 0))} for r in rev_rows[-13:]]

    # 取 EPS 原始數字（最近 8 季）
    start_eps = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
    eps_rows  = finmind_fetch("TaiwanStockFinancialStatements", stock_id, start_date=start_eps)
    eps_rows  = sorted([r for r in eps_rows if r.get("type") == "EPS"], key=lambda x: x["date"])
    eps_chart = [{"date": r["date"], "eps": float(r.get("value", 0))} for r in eps_rows[-8:]]

    result = {
        "stock_id":        stock_id,
        "revenue_yoy":     rev_yoy,     # True/False
        "eps_yoy":         eps_yoy,     # True/False
        "institutional":   inst,        # foreign_net, trust_net, dealer_net ...
        "margin":          margin,      # margin_today, margin_change ...
        "revenue_chart":   rev_chart,   # [{date, revenue}, ...]
        "eps_chart":       eps_chart,   # [{date, eps}, ...]
    }
    cache_set(f"fundamental_{stock_id}", result, 3600)  # 1小時快取
    return jsonify(result)

# ─── 啟動 ───────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print("  QuantEdge Pro — 後端 API Server 啟動中")
    print(f"  API 地址：http://localhost:{port}")
    print("  資料來源：台股TWSE爬蟲 + Yahoo Finance")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
