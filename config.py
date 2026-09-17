"""
全域參數設定：事件窗口、告警門檻、FinMind 資料集名稱、路徑。
所有窗口單位皆為「交易日」相對位移（以 T0 在市場交易日曆中的位置為基準），
而非日曆天，以避開週末/假日造成的位移誤差。
"""
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 路徑
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

EVENTS_CSV = BASE_DIR / "cb_events_manual.csv"

EVENTS_SUMMARY_PARQUET = DATA_DIR / "events_summary.parquet"
CAR_TIMESERIES_PARQUET = DATA_DIR / "car_timeseries.parquet"
CHIP_DETAIL_PARQUET = DATA_DIR / "chip_detail.parquet"
ALERTS_PARQUET = DATA_DIR / "alerts.parquet"

# ---------------------------------------------------------------------------
# 事件研究窗口（交易日相對位移，0 = T0 定價基準日）
# ---------------------------------------------------------------------------
ESTIMATION_WINDOW = (-150, -31)   # 估計期：估 alpha/beta
PRE_WINDOW = (-20, -1)            # 壓價期：定價前
POST_WINDOW = (0, 60)             # 拉抬期：定價後

# 下載價量資料時，為了涵蓋上述所有窗口，抓取的日曆天緩衝範圍
# （交易日 ≈ 日曆天 * 0.68，這裡抓寬一點避免邊界不足）
CALENDAR_BUFFER_BEFORE_DAYS = 260   # 涵蓋 T-150 往前還要有 estimation 的資料起點
CALENDAR_BUFFER_AFTER_DAYS = 100    # 涵蓋 T+60 之後留一些餘裕

# 市場基準指數（大盤，還原股價）
MARKET_TICKER = "^TWII"

# 個股 ticker 嘗試後綴：上市 .TW 優先，其次上櫃 .TWO
TICKER_SUFFIXES = [".TW", ".TWO"]

# 20 日均量視窗
VOLUME_MA_WINDOW = 20

# ---------------------------------------------------------------------------
# FinMind API
# ---------------------------------------------------------------------------
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

DATASET_INSTITUTIONAL = "TaiwanStockInstitutionalInvestorsBuySell"
DATASET_MARGIN_SHORT = "TaiwanStockMarginPurchaseShortSale"
DATASET_SECURITIES_LENDING = "TaiwanStockSecuritiesLending"
DATASET_CONVERTIBLE_BOND_INFO = "TaiwanStockConvertibleBondInfo"  # 需 Backer/Sponsor 權限，選用

# FinMind 免費額度（未登入 300 次/小時，帶 token 600 次/小時）
FINMIND_REQUEST_INTERVAL_SEC = 0.6  # 每次請求間隔，避免瞬間超過額度

# ---------------------------------------------------------------------------
# 告警門檻（可調整）
# ---------------------------------------------------------------------------
# 定價前疑似壓低基準價：CAR_pre < 門檻 且 (融券變化率 > 門檻 或 借券變化率 > 門檻)
ALERT_CAR_PRE_THRESHOLD = -0.03
ALERT_MARGIN_SHORT_INCREASE_THRESHOLD = 0.30
ALERT_LENDING_INCREASE_THRESHOLD = 0.30

# 定價後疑似籌碼回補拉抬：CAR_post > 門檻 且 融券變化率 < 門檻（大幅減少 = 回補）
ALERT_CAR_POST_THRESHOLD = 0.08
ALERT_MARGIN_SHORT_DECREASE_THRESHOLD = -0.30

# 定價後量能異常放大：成交量 / 20日均量 > 門檻
ALERT_VOLUME_RATIO_THRESHOLD = 2.0

# ---------------------------------------------------------------------------
# 儀表板階段篩選
# ---------------------------------------------------------------------------
UPCOMING_PRICING_WINDOW_DAYS = 10   # 即將定價：T0 在未來 10 天內
RECENT_POST_PRICING_WINDOW_DAYS = 30  # 已定價拉抬期：T0 ~ T+30
