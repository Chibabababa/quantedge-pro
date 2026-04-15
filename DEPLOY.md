# QuantEdge Pro — 部署指南

## 專案結構

```
quant-deploy/
├── server.py               ← Flask 後端 + 前端路由
├── quant-trading-live.html ← 前端單頁應用
├── requirements.txt        ← Python 依賴
├── render.yaml             ← Render.com 一鍵部署設定
├── Procfile                ← Heroku / Railway 啟動設定
├── Dockerfile              ← Docker 容器化
└── .gitignore
```

---

## 方法一：Render.com（推薦，免費）

### 步驟
1. 到 [render.com](https://render.com) 註冊帳號
2. 建立 GitHub repo，把這個資料夾的所有檔案推上去
3. Render Dashboard → New → Web Service → 選你的 GitHub repo
4. 填入以下設定（`render.yaml` 已自動帶入）：
   - **Runtime**：Python 3
   - **Build Command**：`pip install -r requirements.txt`
   - **Start Command**：`gunicorn server:app --workers 2 --threads 4 --bind 0.0.0.0:$PORT --timeout 120`
5. 點 **Deploy** → 等候約 3 分鐘
6. 完成！取得你的 URL：`https://quantedge-pro-xxxx.onrender.com`

> ⚠️ Render 免費版閒置 15 分鐘後會休眠，首次訪問需要約 30 秒喚醒

---

## 方法二：Railway.app

1. 到 [railway.app](https://railway.app) 登入（用 GitHub）
2. New Project → Deploy from GitHub repo → 選 repo
3. Railway 會自動偵測 Procfile 並啟動
4. Settings → Domains → Generate Domain 取得網址

---

## 方法三：Docker 本地 / VPS 部署

```bash
# Build image
docker build -t quantedge-pro .

# 本地執行
docker run -p 5000:5000 quantedge-pro

# 開啟瀏覽器到 http://localhost:5000
```

### 在 VPS（Ubuntu）上執行

```bash
# 安裝 Docker
curl -fsSL https://get.docker.com | sh

# 複製檔案到伺服器後執行
docker build -t quantedge-pro .
docker run -d -p 80:5000 --restart=always --name quantedge quantedge-pro
```

---

## 方法四：直接在 VPS 執行（無 Docker）

```bash
# 安裝依賴
pip install -r requirements.txt

# 啟動（生產模式）
gunicorn server:app --workers 2 --threads 4 --bind 0.0.0.0:80 --timeout 120

# 背景執行
nohup gunicorn server:app --workers 2 --bind 0.0.0.0:80 &
```

---

## 本地開發

```bash
pip install -r requirements.txt
python server.py
# 開啟 http://localhost:5000
```

---

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/` | 前端主頁 |
| GET | `/api/health` | 健康檢查 |
| GET | `/api/index` | 大盤指數 |
| GET | `/api/stock/<id>?market=tw\|us` | 個股報價+指標 |
| GET | `/api/history/<id>` | 歷史K線 |
| POST | `/api/monitor` | 批次監控 |
| GET | `/api/recommend` | 今日推薦 |
| GET | `/api/news/<id>` | 相關新聞 |
| POST | `/api/backtest` | 策略回測 |

---

## 注意事項

- Yahoo Finance 免費版有請求限制，快取設定為 60 秒
- 台股盤後（非交易時段）顯示昨收價
- Render 免費版每月有 750 小時免費額度
