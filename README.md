# CB 定價日前後股價與籌碼異常監控儀表板

> ⚠️ **免責聲明**：本工具僅供學術研究與市場異常型態觀察，所有指標
> （AR、CAR、籌碼變化率等）皆為統計估計值，**不構成任何投資建議**，
> 使用者須自行承擔依此資訊做出決策的風險。

## 研究假設與方法論摘要

驗證市場假設：「可轉換公司債（CB）定價前壓低基準價、定價後拉抬股價」。

- **事件日 T0**：CB 定價基準日（人工維護於 `cb_events_manual.csv`，
  因公開資訊觀測站無穩定結構化 API 可長期依賴）
- **窗口定義**（皆以「交易日」相對位移計算，非日曆天）：
  - 估計期：T-150 ~ T-31（估計市場模型 alpha/beta）
  - 壓價期：T-20 ~ T-1
  - 拉抬期：T0 ~ T+60
- **市場模型**：`R_i = alpha + beta * R_m + epsilon`，以 `^TWII`
  （加權指數，還原股價）為市場基準，OLS 回歸估計 alpha/beta 後，
  計算事件窗口的 AR（實際報酬 - 預期報酬）與 CAR（累積 AR）。
- **籌碼指標**：融券餘額變化率、借券賣出張數變化率、三大法人買賣超、
  成交量 / 20 日均量比。
- **統計檢定**（`backtest.py`）：對 CAR 樣本做單一樣本 t 檢定
  （H0: 平均 CAR = 0），並依承銷方式（競價拍賣 / 詢價圈購）、
  有無擔保分組檢定。

## 專案結構

```
cb-monitor/
├── config.py                  # 全域參數：窗口、告警門檻、FinMind 設定
├── cb_events_manual.csv       # CB 事件清單（人工維護，ground truth）
├── etl.py                     # 主 ETL：抓價量+籌碼、算 AR/CAR、輸出 parquet
├── app.py                     # Streamlit 儀表板
├── backtest.py                # CAR 統計檢定（t 檢定）
├── requirements.txt
├── .github/workflows/daily_etl.yml
└── data/                      # ETL 輸出的 parquet（events_summary / car_timeseries / chip_detail / alerts）
```

## 本機測試

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

pip install -r requirements.txt

# 編輯 cb_events_manual.csv，填入實際 CB 定價事件（刪除範例列）

# （選填）若要提高 FinMind 請求額度：
# Windows PowerShell: $env:FINMIND_TOKEN = "your_token"
python etl.py

# 檢視統計檢定結果
python backtest.py

# 啟動儀表板
streamlit run app.py
```

驗證程式碼可執行（不需要網路即可跑語法檢查）：

```bash
python -m py_compile config.py etl.py app.py backtest.py
```

## 部署到 GitHub + Streamlit Community Cloud

1. **建立 GitHub repo**：在 GitHub 建立一個新 repo（建議 public，
   Actions 分鐘數無限），把 `cb-monitor/` 整個資料夾 push 上去。
2. **（選填）設定 FinMind token**：若有 FinMind Backer/Sponsor 帳號，
   到 repo → Settings → Secrets and variables → Actions →
   New repository secret，新增名稱 `FINMIND_TOKEN`。
3. **啟用 GitHub Actions**：`.github/workflows/daily_etl.yml` 會在每個
   交易日台灣時間 16:30（UTC 08:30）自動執行 ETL，並把更新後的
   `data/*.parquet` commit/push 回 repo。也可以到 Actions 頁籤手動
   觸發（workflow_dispatch）先跑一次，確認資料能正常產生。
4. **部署 Streamlit 儀表板**：到 https://share.streamlit.io 用
   GitHub 帳號登入 → New app → 選擇這個 repo、branch、`app.py` →
   Deploy。Streamlit Cloud 每次讀取的都是 repo 目前的內容，
   GitHub Actions 更新 parquet 後，重新整理網頁即可看到最新資料
   （或等 Streamlit 自動偵測到 repo 變更重新部署）。

## 免費額度說明

- **GitHub Actions**：public repo 無限分鐘數；private repo 每月
  2,000 分鐘免費額度。本任務單次執行約 1~5 分鐘，遠低於額度。
- **FinMind API**：不登入每小時 300 次請求，註冊 token 後 600 次/小時。
  `TaiwanStockConvertibleBondInfo`（承銷方式/擔保自動補全）需要
  Backer/Sponsor 權限，屬選用功能，預設仍以人工維護的 CSV 為準。
- **yfinance / Streamlit Community Cloud**：皆免費，無需 API key。

## 已知限制

- **yfinance 除權息資料延遲**：個股/大盤的還原股價偶爾在除權息當天
  有 1~2 個交易日的資料延遲或修正，可能造成該期間 AR/CAR 有微小誤差。
- **FinMind 欄位/資料集名稱可能隨版本調整**：`etl.py` 已針對常見欄位
  名稱做防呆比對（`safe_col`），抓不到欄位時會記錄警告、該指標留空，
  不會讓整支程式中斷；但若 FinMind 大幅改版，仍建議定期核對
  `config.py` 中的資料集名稱。
- **定價日仍需人工核對**：CB 定價基準日屬重大訊息公告性質，公開資訊
  觀測站無穩定結構化 API，`cb_events_manual.csv` 為 ground truth，
  ETL 不會自動產生或修改事件清單。
- **上市/上櫃自動判斷**：`etl.py` 會依序嘗試 `.TW` 與 `.TWO`
  後綴，若兩者皆抓不到資料（例如股票已下市），該事件會被標記為
  `failed` 並記錄警告，不影響其他事件。
- **借券賣出資料的 transaction_type**：目前實作對
  `TaiwanStockSecuritiesLending` 回傳的所有列直接加總 `volume`
  作為當日借券賣出張數；若 FinMind 未來針對不同交易型態
  （如證金／證券商）拆分欄位，需重新檢視是否要依 `transaction_type`
  篩選後再加總。
- **樣本數與統計檢定力**：CB 定價事件本身數量有限，`backtest.py`
  的 t 檢定在樣本數過小（如各分組 < 5 筆）時，檢定力不足，結果僅供
  參考，不應過度解讀 p 值。
- **融券/借券基期過低時，變化率會被放大**：實測發現部分權值股
  （如台積電）融券餘額基期常只有個位數到幾十張，此時百分比變化率
  可能出現數百甚至上千趴的極端值（例如從 7 張增加到 90 張＝
  +1186%），這是資料本身特性，並非計算錯誤。解讀「融券變化率」類
  告警時，建議同時參考 `chip_detail.parquet` 中的絕對張數，避免被
  低基期放大的百分比誤導。
