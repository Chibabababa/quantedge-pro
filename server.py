#!/usr/bin/env python3
"""
QuantEdge Pro — 後端 API Server
台股爬蟲 + Yahoo Finance (美股) 真實數據
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

# ─── 台股資料庫（篩選用，含上市+上櫃主要標的） ────────────────
TW_STOCKS_DB = {
    # 半導體
    "2330": "台積電", "2454": "聯發科", "2303": "聯電",
    "2308": "台達電", "3034": "聯詠", "2379": "瑞昱",
    "6770": "力積電", "3711": "日月光投控", "2449": "京元電子",
    # 電子/科技
    "2317": "鴻海", "2382": "廣達", "2357": "華碩",
    "2353": "宏碁", "3008": "大立光", "2395": "研華",
    "4938": "和碩", "2324": "仁寶", "2356": "英業達",
    "3231": "緯創", "2377": "微星", "2376": "技嘉",
    # 金融
    "2881": "富邦金", "2882": "國泰金", "2891": "中信金",
    "2886": "兆豐金", "2884": "玉山金", "2887": "台新金",
    "2885": "元大金", "2892": "第一金", "5880": "合庫金",
    # 電信/傳產
    "2412": "中華電", "3045": "台灣大", "4904": "遠傳",
    "1301": "台塑", "1303": "南亞", "1326": "台化",
    "2002": "中鋼", "1101": "台泥", "1216": "統一",
    # ETF
    "0050": "元大台灣50", "0056": "元大高股息",
    "00878": "國泰永續高股息", "00881": "國泰台灣5G+",
    # 其他熱門
    "6505": "台塑化", "2207": "和泰車", "9910": "豐泰",
    "2912": "統一超", "2327": "國巨", "6669": "緯穎",
    "2408": "南亞科", "2344": "華邦電", "3037": "欣興",
    "2615": "萬海", "2603": "長榮", "2609": "陽明",
}

# 美股篩選候選池
US_STOCKS_SCREEN = [
    "AAPL","MSFT","NVDA","TSLA","META","AMZN","GOOGL",
    "AMD","INTC","QCOM","AVGO","TSM","ASML","NFLX","CRM"
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

def winrate_signal(rsi, macd_cross, k, d, price, ma5, ma20):
    """簡易勝率計算（多因子加權）"""
    score = 0
    signals = []
    # RSI
    if rsi < 30:
        score += 20; signals.append("RSI超賣")
    elif rsi > 70:
        score -= 20; signals.append("RSI超買")
    else:
        score += 5
    # MACD
    if macd_cross == "golden":
        score += 20; signals.append("MACD黃金交叉")
    else:
        score -= 10
    # KD
    if k < 20:
        score += 15; signals.append("KD超賣")
    elif k > 80:
        score -= 15
    if k > d:
        score += 10; signals.append("KD多頭")
    # MA
    if price > ma5 > ma20:
        score += 20; signals.append("均線多頭排列")
    elif price < ma5 < ma20:
        score -= 20
    # 最終勝率（40~85區間）
    winrate = max(40, min(85, 60 + score * 0.5))
    if score > 20:
        action = "強力買進"
    elif score > 5:
        action = "買進"
    elif score > -5:
        action = "觀察"
    else:
        action = "賣出"
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

def fetch_tw_price_yfinance(stock_id):
    """yfinance 備援抓台股價格（TWSE/TPEX 失敗時使用）"""
    with _yf_semaphore:
        try:
            for suffix in [".TW", ".TWO"]:
                ticker = yf.Ticker(f"{stock_id}{suffix}")
                info = ticker.fast_info
                price = float(info.last_price or 0)
                if price > 0:
                    prev = float(info.previous_close or price)
                    change = round(price - prev, 2)
                    change_pct = round((change / prev) * 100, 2) if prev else 0
                    name = TW_STOCKS_DB.get(stock_id, stock_id)
                    try:
                        full_name = ticker.info.get("shortName", name)
                        if full_name:
                            name = full_name
                    except Exception:
                        pass
                    result = {
                        "id": stock_id, "name": name,
                        "price": price, "yesterday": prev,
                        "change": change, "change_pct": change_pct,
                        "volume": int(getattr(info, "three_month_average_volume", 0) or 0),
                        "market": "TWSE"
                    }
                    cache_set(f"twse_{stock_id}", result, 60)
                    return result
        except Exception as e:
            print(f"yfinance TW fallback error {stock_id}: {e}")
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
    """用 yfinance 抓台股歷史資料（Yahoo Finance 有台股支援）"""
    cached = cache_get(f"hist_{stock_id}")
    if cached:
        return cached
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
    """Yahoo Finance 抓美股即時 + 歷史"""
    cached = cache_get(f"us_{symbol}")
    if cached:
        return cached
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        hist = ticker.history(period="3mo")
        price = round(float(info.last_price), 2)
        prev = round(float(info.previous_close), 2)
        change = round(price - prev, 2)
        change_pct = round((change / prev) * 100, 2) if prev else 0
        hist = hist.reset_index()
        result = {
            "id": symbol, "name": ticker.info.get("shortName", symbol),
            "price": price, "yesterday": prev,
            "change": change, "change_pct": change_pct,
            "volume": int(info.three_month_average_volume or 0),
            "market": "US",
            "history": {
                "dates": [str(d.date()) for d in hist["Date"]],
                "opens": [round(float(v), 2) for v in hist["Open"]],
                "highs": [round(float(v), 2) for v in hist["High"]],
                "lows": [round(float(v), 2) for v in hist["Low"]],
                "closes": [round(float(v), 2) for v in hist["Close"]],
                "volumes": [int(v) for v in hist["Volume"]]
            }
        }
        cache_set(f"us_{symbol}", result, 60)
        return result
    except Exception as e:
        print(f"US stock fetch error {symbol}: {e}")
        return None

def fetch_tw_index():
    """台灣大盤指數"""
    cached = cache_get("tw_index")
    if cached:
        return cached
    try:
        ticker = yf.Ticker("^TWII")
        info = ticker.fast_info
        price = round(float(info.last_price), 2)
        prev = round(float(info.previous_close), 2)
        change = round(price - prev, 2)
        change_pct = round((change / prev) * 100, 2) if prev else 0
        result = {"price": price, "change": change, "change_pct": change_pct}
        cache_set("tw_index", result, 60)
        return result
    except:
        return {"price": 0, "change": 0, "change_pct": 0}

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
    winrate, action, signals = winrate_signal(rsi, macd["cross"], kd["K"], kd["D"], price, ma5, ma20)
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
    rsi_max       = float(body.get("rsi_max", 100))
    rsi_min       = float(body.get("rsi_min", 0))
    ma_golden     = body.get("ma_golden", False)
    macd_golden   = body.get("macd_golden", False)
    kd_golden     = body.get("kd_golden", False)
    vol_ratio_min = float(body.get("vol_ratio_min", 0))
    score_min     = int(body.get("score_min", 0))
    market        = body.get("market", "tw")

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
            winrate, action, signals = winrate_signal(rsi, macd["cross"], kd["K"], kd["D"], price, ma5, ma20)
            if rsi > rsi_max: return None
            if rsi < rsi_min: return None
            if ma_golden   and not (ma5 > ma20): return None
            if macd_golden and not (macd.get("cross") == "golden"): return None
            if kd_golden   and not (kd.get("K", 0) > kd.get("D", 0)): return None
            if vr < vol_ratio_min: return None
            if sc < score_min: return None
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
        for future in as_completed(futures, timeout=30):
            try:
                r = future.result(timeout=25)
                if r:
                    results.append(r)
            except Exception as e:
                print(f"Screen future: {e}")

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    cache_set("screen_" + str(body), results, 120)  # 2分鐘快取
    return jsonify(results)

@app.route("/")
def index():
    """Serve 前端主頁面"""
    return send_from_directory(BASE_DIR, "quant-trading-live.html")

# ─── API 路由 ────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route("/api/index")
def api_index():
    """大盤指數（台灣加權 + 美股三大指數）"""
    tw = fetch_tw_index()
    us_indices = {}
    for sym, name in [("^GSPC", "S&P500"), ("^IXIC", "NASDAQ"), ("^DJI", "道瓊")]:
        try:
            t = yf.Ticker(sym).fast_info
            p = round(float(t.last_price), 2)
            prev = round(float(t.previous_close), 2)
            chg = round(p - prev, 2)
            chg_pct = round(chg / prev * 100, 2) if prev else 0
            us_indices[name] = {"price": p, "change": chg, "change_pct": chg_pct}
        except:
            pass
    return jsonify({"tw_weighted": tw, "us": us_indices})

@app.route("/api/stock/<stock_id>")
def api_stock(stock_id):
    """股票即時報價 + 技術指標"""
    market = request.args.get("market", "tw")
    if market == "us":
        data = fetch_us_stock(stock_id.upper())
        if not data:
            return jsonify({"error": "無法取得數據"}), 404
        # 計算US指標
        hist = data.get("history", {})
        closes = hist.get("closes", [])
        if len(closes) >= 20:
            highs = hist.get("highs", closes)
            lows = hist.get("lows", closes)
            volumes = hist.get("volumes", [])
            rsi = calc_rsi(closes)
            macd = calc_macd(closes)
            kd = calc_kd(highs, lows, closes)
            bb = calc_bollinger(closes)
            mas = calc_ma(closes)
            atr = calc_atr(highs, lows, closes)
            price = closes[-1]
            ma5 = mas.get("MA5", price)
            ma20 = mas.get("MA20", price)
            avg_vol = int(np.mean(volumes[-20:])) if len(volumes) >= 20 else 0
            vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol else 1
            winrate, action, signals = winrate_signal(rsi, macd["cross"], kd["K"], kd["D"], price, ma5, ma20)
            data["indicators"] = {
                "RSI14": rsi, "MACD": macd, "KD": kd,
                "Bollinger": bb, "MA": mas, "ATR14": atr,
                "volume_ratio": vol_ratio, "winrate": winrate,
                "action": action, "signals": signals
            }
        return jsonify(data)
    else:
        # 台股
        price_data = fetch_twse_price(stock_id)
        if not price_data:
            price_data = fetch_tpex_price(stock_id)
        if not price_data:
            return jsonify({"error": "無法取得台股數據"}), 404
        indicators = get_full_indicators(stock_id)
        hist = fetch_tw_history(stock_id)
        price_data["indicators"] = indicators
        price_data["history"] = hist
        return jsonify(price_data)

@app.route("/api/history/<stock_id>")
def api_history(stock_id):
    """歷史K線資料"""
    market = request.args.get("market", "tw")
    period = request.args.get("period", "3mo")
    if market == "us":
        data = fetch_us_stock(stock_id.upper())
        return jsonify(data.get("history", {})) if data else jsonify({"error": "無法取得"}), 404
    hist = fetch_tw_history(stock_id)
    if not hist:
        return jsonify({"error": "無法取得歷史數據"}), 404
    return jsonify(hist)

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

@app.route("/api/recommend")
def api_recommend():
    """今日推薦股票（台股 + 美股，並行抓取，10分鐘快取）"""
    cached = cache_get("recommend")
    if cached:
        return jsonify(cached)

    tw_candidates = ["2330", "2317", "2454", "2382", "2412", "3008", "6505", "0050", "2881", "2882"]
    us_candidates = ["NVDA", "TSLA", "AAPL", "MSFT", "META"]
    results = []

    def fetch_tw(stock_id):
        try:
            price_data = fetch_twse_price(stock_id)
            if not price_data or not price_data.get("price"):
                return None
            ind = get_full_indicators(stock_id)
            if not ind:
                return None
            hist = fetch_tw_history(stock_id)
            closes = hist["closes"] if hist else []
            price = price_data["price"]
            target = round(price * 1.08, 0)
            stop_loss = round(price * 0.95, 0)
            return {
                "id": stock_id,
                "name": price_data["name"],
                "market": "TW",
                "price": price,
                "change_pct": price_data["change_pct"],
                "target": target,
                "stop_loss": stop_loss,
                "score": ind.get("score", 60),
                "winrate": ind.get("winrate", 60),
                "action": ind.get("action", "觀察"),
                "signals": ind.get("signals", []),
                "rsi": ind.get("RSI14", 50),
                "macd_cross": ind["MACD"].get("cross", ""),
                "sparkline": closes[-20:] if len(closes) >= 20 else closes
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
            rsi = calc_rsi(closes)
            macd = calc_macd(closes)
            kd = calc_kd(hist.get("highs", closes), hist.get("lows", closes), closes)
            mas = calc_ma(closes)
            ma5 = mas.get("MA5", price)
            ma20 = mas.get("MA20", price)
            winrate, action, signals = winrate_signal(rsi, macd["cross"], kd["K"], kd["D"], price, ma5, ma20)
            return {
                "id": sym,
                "name": data["name"],
                "market": "US",
                "price": price,
                "change_pct": data["change_pct"],
                "target": round(price * 1.1, 2),
                "stop_loss": round(price * 0.95, 2),
                "score": 70,
                "winrate": winrate,
                "action": action,
                "signals": signals,
                "rsi": rsi,
                "sparkline": closes[-20:]
            }
        except Exception as e:
            print(f"Recommend US error {sym}: {e}")
            return None

    # 並行抓取台股 + 美股
    all_tasks = [(fetch_tw, s) for s in tw_candidates[:8]] + [(fetch_us, s) for s in us_candidates[:4]]
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(fn, s) for fn, s in all_tasks]
        for future in as_completed(futures, timeout=25):
            try:
                r = future.result(timeout=20)
                if r:
                    results.append(r)
            except Exception as e:
                print(f"Recommend future error: {e}")

    # 依評分排序
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    cache_set("recommend", results, 600)  # 10分鐘快取
    return jsonify(results)

@app.route("/api/news/<stock_id>")
def api_news(stock_id):
    """爬取相關新聞（Yahoo 財經台灣）"""
    cached = cache_get(f"news_{stock_id}")
    if cached:
        return jsonify(cached)
    news_list = []
    try:
        ticker = yf.Ticker(f"{stock_id}.TW" if len(stock_id) == 4 and stock_id.isdigit() else stock_id)
        news = ticker.news or []
        for n in news[:5]:
            title = n.get("title", "")
            link = n.get("link", "#")
            pub = n.get("providerPublishTime", 0)
            publisher = n.get("publisher", "")
            pub_time = datetime.fromtimestamp(pub).strftime("%m/%d %H:%M") if pub else ""
            # 簡易情緒判斷
            pos_words = ["上漲", "突破", "創新高", "買超", "利多", "營收成長", "獲利", "強勁", "Beat", "surge", "rise", "gain"]
            neg_words = ["下跌", "虧損", "賣超", "利空", "下修", "衰退", "miss", "fall", "drop", "loss"]
            sentiment = "positive" if any(w in title for w in pos_words) else \
                        "negative" if any(w in title for w in neg_words) else "neutral"
            news_list.append({
                "title": title,
                "link": link,
                "publisher": publisher,
                "time": pub_time,
                "sentiment": sentiment
            })
    except Exception as e:
        print(f"News fetch error {stock_id}: {e}")
    cache_set(f"news_{stock_id}", news_list, 600)
    return jsonify(news_list)

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

# ─── 啟動 ───────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print("  QuantEdge Pro — 後端 API Server 啟動中")
    print(f"  API 地址：http://localhost:{port}")
    print("  資料來源：台股TWSE爬蟲 + Yahoo Finance")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
