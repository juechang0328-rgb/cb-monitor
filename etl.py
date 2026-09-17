"""
主 ETL：對 cb_events_manual.csv 中每一檔 CB 事件，抓取價量（yfinance）與
籌碼（FinMind）資料，計算事件研究法 AR/CAR 與籌碼異動率，輸出到
data/*.parquet。

設計原則：任何單一標的的任何一個步驟失敗，都只記錄警告並讓該指標留空，
不可讓整支程式中斷（其他事件仍需正常跑完）。
"""
from __future__ import annotations

import logging
import time
import warnings
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("cb_monitor.etl")

warnings.filterwarnings("ignore", category=FutureWarning)


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------
@dataclass
class EventResult:
    summary: dict
    car_rows: list = field(default_factory=list)
    chip_rows: list = field(default_factory=list)
    alert_rows: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 事件清單讀取
# ---------------------------------------------------------------------------
def read_events() -> pd.DataFrame:
    df = pd.read_csv(config.EVENTS_CSV, comment="#", dtype={"stock_id": str, "cb_code": str})
    df["pricing_date"] = pd.to_datetime(df["pricing_date"], errors="coerce")
    bad = df["pricing_date"].isna()
    if bad.any():
        logger.warning("以下事件列的 pricing_date 無法解析，將被略過:\n%s", df[bad])
        df = df[~bad]
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 價量資料（yfinance）
# ---------------------------------------------------------------------------
def fetch_stock_price(stock_id: str, start: pd.Timestamp, end: pd.Timestamp) -> tuple[Optional[pd.DataFrame], Optional[str]]:
    """依序嘗試 .TW（上市）與 .TWO（上櫃），回傳 (價量 DataFrame, 使用的 ticker)。"""
    for suffix in config.TICKER_SUFFIXES:
        ticker = f"{stock_id}{suffix}"
        try:
            df = yf.download(
                ticker,
                start=start.strftime("%Y-%m-%d"),
                end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
                auto_adjust=True,
                progress=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("yfinance 下載 %s 失敗: %s", ticker, e)
            continue
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Close", "Volume"]].rename(columns={"Close": "close", "Volume": "volume"})
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df, ticker
    return None, None


def fetch_market_price(start: pd.Timestamp, end: pd.Timestamp) -> Optional[pd.DataFrame]:
    try:
        df = yf.download(
            config.MARKET_TICKER,
            start=start.strftime("%Y-%m-%d"),
            end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
            auto_adjust=True,
            progress=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("yfinance 下載大盤 %s 失敗: %s", config.MARKET_TICKER, e)
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Close"]].rename(columns={"Close": "market_close"})
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


# ---------------------------------------------------------------------------
# 籌碼資料（FinMind）
# ---------------------------------------------------------------------------
_last_request_ts = 0.0


def _finmind_get(dataset: str, data_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    global _last_request_ts
    elapsed = time.time() - _last_request_ts
    if elapsed < config.FINMIND_REQUEST_INTERVAL_SEC:
        time.sleep(config.FINMIND_REQUEST_INTERVAL_SEC - elapsed)

    params = {
        "dataset": dataset,
        "data_id": data_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    if config.FINMIND_TOKEN:
        params["token"] = config.FINMIND_TOKEN

    try:
        resp = requests.get(config.FINMIND_API_URL, params=params, timeout=20)
        _last_request_ts = time.time()
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("FinMind 請求失敗 dataset=%s data_id=%s: %s", dataset, data_id, e)
        return pd.DataFrame()

    if payload.get("status") != 200:
        logger.warning(
            "FinMind 回傳非 200 status dataset=%s data_id=%s: %s",
            dataset, data_id, payload.get("msg"),
        )
        return pd.DataFrame()

    data = payload.get("data", [])
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)


def safe_col(df: pd.DataFrame, *candidates: str) -> Optional[str]:
    """FinMind 欄位名稱偶爾隨版本調整，依候選清單找出實際存在的欄位。"""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def fetch_institutional_net(stock_id: str, start: str, end: str) -> pd.DataFrame:
    """三大法人合計買賣超（張），依日期加總 buy - sell。"""
    df = _finmind_get(config.DATASET_INSTITUTIONAL, stock_id, start, end)
    if df.empty:
        return pd.DataFrame(columns=["date", "institutional_net"])
    date_col = safe_col(df, "date")
    buy_col = safe_col(df, "buy")
    sell_col = safe_col(df, "sell")
    if not all([date_col, buy_col, sell_col]):
        logger.warning("三大法人資料欄位不符預期，stock_id=%s，欄位=%s", stock_id, list(df.columns))
        return pd.DataFrame(columns=["date", "institutional_net"])
    df["date"] = pd.to_datetime(df[date_col])
    df["institutional_net"] = pd.to_numeric(df[buy_col], errors="coerce") - pd.to_numeric(df[sell_col], errors="coerce")
    out = df.groupby("date", as_index=False)["institutional_net"].sum()
    return out


def fetch_margin_short(stock_id: str, start: str, end: str) -> pd.DataFrame:
    """融資融券資料，取融券今日餘額 ShortSaleTodayBalance。"""
    df = _finmind_get(config.DATASET_MARGIN_SHORT, stock_id, start, end)
    if df.empty:
        return pd.DataFrame(columns=["date", "short_sale_balance"])
    date_col = safe_col(df, "date")
    balance_col = safe_col(df, "ShortSaleTodayBalance", "short_sale_today_balance", "ShortSaleTodayBalanceShare")
    if not all([date_col, balance_col]):
        logger.warning("融資融券資料欄位不符預期，stock_id=%s，欄位=%s", stock_id, list(df.columns))
        return pd.DataFrame(columns=["date", "short_sale_balance"])
    out = pd.DataFrame({
        "date": pd.to_datetime(df[date_col]),
        "short_sale_balance": pd.to_numeric(df[balance_col], errors="coerce"),
    })
    return out.groupby("date", as_index=False)["short_sale_balance"].sum()


def fetch_securities_lending(stock_id: str, start: str, end: str) -> pd.DataFrame:
    """借券賣出資料，加總當日成交（賣出）張數 volume。"""
    df = _finmind_get(config.DATASET_SECURITIES_LENDING, stock_id, start, end)
    if df.empty:
        return pd.DataFrame(columns=["date", "lending_volume"])
    date_col = safe_col(df, "date")
    volume_col = safe_col(df, "volume", "Volume")
    if not all([date_col, volume_col]):
        logger.warning("借券賣出資料欄位不符預期，stock_id=%s，欄位=%s", stock_id, list(df.columns))
        return pd.DataFrame(columns=["date", "lending_volume"])
    out = pd.DataFrame({
        "date": pd.to_datetime(df[date_col]),
        "lending_volume": pd.to_numeric(df[volume_col], errors="coerce"),
    })
    return out.groupby("date", as_index=False)["lending_volume"].sum()


# ---------------------------------------------------------------------------
# 事件研究法核心計算
# ---------------------------------------------------------------------------
def locate_t0_position(calendar: pd.DatetimeIndex, t0: pd.Timestamp) -> Optional[int]:
    """回傳交易日曆中，第一個 >= t0 的交易日之整數位置。"""
    pos = calendar.searchsorted(t0)
    if pos >= len(calendar):
        return None
    return int(pos)


def market_model_regress(stock_ret: np.ndarray, market_ret: np.ndarray) -> tuple[Optional[float], Optional[float], Optional[float]]:
    mask = ~(np.isnan(stock_ret) | np.isnan(market_ret))
    stock_ret, market_ret = stock_ret[mask], market_ret[mask]
    if len(stock_ret) < 30:
        return None, None, None
    beta, alpha = np.polyfit(market_ret, stock_ret, 1)
    pred = alpha + beta * market_ret
    ss_res = np.sum((stock_ret - pred) ** 2)
    ss_tot = np.sum((stock_ret - stock_ret.mean()) ** 2)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else None
    return float(alpha), float(beta), (float(r_squared) if r_squared is not None else None)


def pct_change_rate(old: Optional[float], new: Optional[float]) -> Optional[float]:
    if old is None or new is None or pd.isna(old) or pd.isna(new) or old == 0:
        return None
    return float((new - old) / abs(old))


# ---------------------------------------------------------------------------
# 單一事件處理
# ---------------------------------------------------------------------------
def process_event(row: pd.Series) -> EventResult:
    stock_id = str(row["stock_id"])
    stock_name = row.get("stock_name", "")
    cb_code = row.get("cb_code", "")
    t0_date = row["pricing_date"]
    underwriting_method = row.get("underwriting_method", "")
    guaranteed = row.get("guaranteed", "")

    warnings_list: list[str] = []
    summary = {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "cb_code": cb_code,
        "pricing_date": t0_date,
        "underwriting_method": underwriting_method,
        "guaranteed": guaranteed,
        "ticker_used": None,
        "status": "ok",
        "alpha": None,
        "beta": None,
        "r_squared": None,
        "car_pre": None,
        "car_post": None,
        "car_post_5": None,
        "car_post_10": None,
        "car_post_20": None,
        "margin_short_change_pre": None,
        "margin_short_change_post": None,
        "lending_change_pre": None,
        "institutional_net_pre_sum": None,
        "institutional_net_post_sum": None,
        "volume_ratio_max": None,
        "volume_ratio_max_date": None,
        "alerts": "",
        "warnings": "",
    }

    fetch_start = t0_date - timedelta(days=config.CALENDAR_BUFFER_BEFORE_DAYS)
    fetch_end = min(t0_date + timedelta(days=config.CALENDAR_BUFFER_AFTER_DAYS), pd.Timestamp.today())

    market_df = fetch_market_price(fetch_start, fetch_end)
    if market_df is None:
        summary["status"] = "failed"
        summary["warnings"] = "大盤(^TWII)價格資料下載失敗"
        return EventResult(summary=summary)

    stock_df, ticker_used = fetch_stock_price(stock_id, fetch_start, fetch_end)
    if stock_df is None:
        summary["status"] = "failed"
        summary["warnings"] = f"個股 {stock_id} 價格資料下載失敗（已嘗試 .TW / .TWO）"
        return EventResult(summary=summary)
    summary["ticker_used"] = ticker_used

    prices = stock_df.join(market_df, how="inner").sort_index()
    if prices.empty:
        summary["status"] = "failed"
        summary["warnings"] = "個股與大盤交易日曆無交集"
        return EventResult(summary=summary)

    calendar = prices.index
    pos0 = locate_t0_position(calendar, t0_date)
    if pos0 is None:
        summary["status"] = "failed"
        summary["warnings"] = "T0 定價日之後無足夠交易日資料（可能尚未到或資料不足）"
        return EventResult(summary=summary)

    returns = prices[["close", "market_close"]].pct_change()
    returns.columns = ["stock_ret", "market_ret"]
    volume = prices["volume"]
    volume_ma20 = volume.rolling(config.VOLUME_MA_WINDOW).mean()

    n = len(prices)

    def slice_by_offset(start_off: int, end_off: int) -> pd.DataFrame:
        lo, hi = pos0 + start_off, pos0 + end_off
        lo_clamped, hi_clamped = max(lo, 0), min(hi, n - 1)
        if lo_clamped > hi_clamped:
            return returns.iloc[0:0]
        if lo_clamped != lo or hi_clamped != hi:
            warnings_list.append(f"窗口 [{start_off},{end_off}] 資料不足，已截斷至可用範圍")
        return returns.iloc[lo_clamped: hi_clamped + 1]

    est_slice = slice_by_offset(*config.ESTIMATION_WINDOW)
    alpha, beta, r_squared = market_model_regress(
        est_slice["stock_ret"].to_numpy(), est_slice["market_ret"].to_numpy()
    )
    if alpha is None:
        summary["status"] = "partial"
        warnings_list.append("估計期樣本不足（<30 筆），無法估計市場模型 alpha/beta")
        summary["warnings"] = "; ".join(warnings_list)
        return EventResult(summary=summary)

    summary["alpha"], summary["beta"], summary["r_squared"] = alpha, beta, r_squared

    def compute_ar_car(start_off: int, end_off: int) -> tuple[pd.DataFrame, Optional[float]]:
        sl = slice_by_offset(start_off, end_off)
        if sl.empty:
            return sl, None
        ar = sl["stock_ret"] - (alpha + beta * sl["market_ret"])
        car = ar.cumsum()
        out = pd.DataFrame({"date": sl.index, "AR": ar.to_numpy(), "CAR": car.to_numpy()})
        out["rel_day"] = range(start_off, start_off + len(out))
        return out, (float(car.iloc[-1]) if len(car) else None)

    car_rows: list[dict] = []

    pre_df, car_pre = compute_ar_car(*config.PRE_WINDOW)
    post_df, car_post = compute_ar_car(*config.POST_WINDOW)
    summary["car_pre"] = car_pre
    summary["car_post"] = car_post

    for horizon, key in [(5, "car_post_5"), (10, "car_post_10"), (20, "car_post_20")]:
        sub, val = compute_ar_car(config.POST_WINDOW[0], min(horizon, config.POST_WINDOW[1]))
        summary[key] = val

    for df_ in (pre_df, post_df):
        for _, r in df_.iterrows():
            car_rows.append({
                "stock_id": stock_id,
                "cb_code": cb_code,
                "rel_day": int(r["rel_day"]),
                "date": r["date"],
                "AR": r["AR"],
                "CAR": r["CAR"],
            })

    # ------------------------------------------------------------------
    # 籌碼資料
    # ------------------------------------------------------------------
    fs, fe = fetch_start.strftime("%Y-%m-%d"), fetch_end.strftime("%Y-%m-%d")

    chip_rows: list[dict] = []
    try:
        inst_df = fetch_institutional_net(stock_id, fs, fe)
        margin_df = fetch_margin_short(stock_id, fs, fe)
        lending_df = fetch_securities_lending(stock_id, fs, fe)

        chip = pd.DataFrame(index=calendar)
        chip.index.name = "date"
        for name, d, col in [
            ("institutional_net", inst_df, "institutional_net"),
            ("short_sale_balance", margin_df, "short_sale_balance"),
            ("lending_volume", lending_df, "lending_volume"),
        ]:
            if not d.empty:
                d = d.set_index("date")
                chip[name] = d[col].reindex(calendar)
            else:
                chip[name] = np.nan
                warnings_list.append(f"{name} 無資料")

        chip["volume"] = volume
        chip["volume_ma20"] = volume_ma20
        chip["volume_ratio"] = chip["volume"] / chip["volume_ma20"]

        window_lo = max(pos0 + config.PRE_WINDOW[0], 0)
        window_hi = min(pos0 + config.POST_WINDOW[1], n - 1)
        chip_window = chip.iloc[window_lo: window_hi + 1]

        for date_idx, r in chip_window.iterrows():
            chip_rows.append({
                "stock_id": stock_id,
                "cb_code": cb_code,
                "date": date_idx,
                "rel_day": int(calendar.get_loc(date_idx)) - pos0,
                "institutional_net": r["institutional_net"],
                "short_sale_balance": r["short_sale_balance"],
                "lending_volume": r["lending_volume"],
                "volume": r["volume"],
                "volume_ma20": r["volume_ma20"],
                "volume_ratio": r["volume_ratio"],
            })

        def value_at(offset: int, col: str) -> Optional[float]:
            pos = pos0 + offset
            if pos < 0 or pos >= n:
                return None
            v = chip[col].iloc[pos]
            return None if pd.isna(v) else float(v)

        margin_pre_start = value_at(config.PRE_WINDOW[0], "short_sale_balance")
        margin_pre_end = value_at(config.PRE_WINDOW[1], "short_sale_balance")
        margin_post_end = value_at(config.POST_WINDOW[1], "short_sale_balance")
        summary["margin_short_change_pre"] = pct_change_rate(margin_pre_start, margin_pre_end)
        summary["margin_short_change_post"] = pct_change_rate(margin_pre_end, margin_post_end)

        lending_pre_start = value_at(config.PRE_WINDOW[0], "lending_volume")
        lending_pre_end = value_at(config.PRE_WINDOW[1], "lending_volume")
        summary["lending_change_pre"] = pct_change_rate(lending_pre_start, lending_pre_end)

        pre_inst = chip["institutional_net"].iloc[max(pos0 + config.PRE_WINDOW[0], 0): pos0 + config.PRE_WINDOW[1] + 1]
        post_inst = chip["institutional_net"].iloc[pos0: min(pos0 + config.POST_WINDOW[1], n - 1) + 1]
        summary["institutional_net_pre_sum"] = float(pre_inst.sum(skipna=True)) if not pre_inst.empty else None
        summary["institutional_net_post_sum"] = float(post_inst.sum(skipna=True)) if not post_inst.empty else None

        post_ratio = chip["volume_ratio"].iloc[pos0: min(pos0 + config.POST_WINDOW[1], n - 1) + 1]
        if not post_ratio.dropna().empty:
            idxmax = post_ratio.idxmax()
            summary["volume_ratio_max"] = float(post_ratio.max())
            summary["volume_ratio_max_date"] = idxmax
    except Exception as e:  # noqa: BLE001
        logger.warning("計算 %s 籌碼指標時發生例外: %s", stock_id, e)
        warnings_list.append(f"籌碼指標計算例外: {e}")

    # ------------------------------------------------------------------
    # 告警
    # ------------------------------------------------------------------
    alert_rows: list[dict] = []

    def add_alert(alert_type: str, detail: str) -> None:
        alert_rows.append({
            "stock_id": stock_id,
            "stock_name": stock_name,
            "cb_code": cb_code,
            "pricing_date": t0_date,
            "alert_type": alert_type,
            "detail": detail,
        })

    car_pre_v = summary["car_pre"]
    car_post_v = summary["car_post"]
    margin_chg_pre = summary["margin_short_change_pre"]
    margin_chg_post = summary["margin_short_change_post"]
    lending_chg_pre = summary["lending_change_pre"]
    vol_ratio_max = summary["volume_ratio_max"]

    if car_pre_v is not None and car_pre_v < config.ALERT_CAR_PRE_THRESHOLD and (
        (margin_chg_pre is not None and margin_chg_pre > config.ALERT_MARGIN_SHORT_INCREASE_THRESHOLD)
        or (lending_chg_pre is not None and lending_chg_pre > config.ALERT_LENDING_INCREASE_THRESHOLD)
    ):
        add_alert(
            "定價前疑似壓低基準價",
            f"CAR_pre={car_pre_v:.2%}, 融券變化率={margin_chg_pre}, 借券變化率={lending_chg_pre}",
        )

    if car_post_v is not None and car_post_v > config.ALERT_CAR_POST_THRESHOLD and (
        margin_chg_post is not None and margin_chg_post < config.ALERT_MARGIN_SHORT_DECREASE_THRESHOLD
    ):
        add_alert(
            "定價後疑似籌碼回補拉抬",
            f"CAR_post={car_post_v:.2%}, 融券變化率={margin_chg_post}",
        )

    if vol_ratio_max is not None and vol_ratio_max > config.ALERT_VOLUME_RATIO_THRESHOLD:
        add_alert(
            "定價後量能異常放大",
            f"最大量比={vol_ratio_max:.2f}x on {summary['volume_ratio_max_date']}",
        )

    summary["alerts"] = "; ".join(a["alert_type"] for a in alert_rows)
    if warnings_list:
        summary["status"] = "partial" if summary["status"] == "ok" else summary["status"]
    summary["warnings"] = "; ".join(warnings_list)

    return EventResult(summary=summary, car_rows=car_rows, chip_rows=chip_rows, alert_rows=alert_rows)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    events = read_events()
    logger.info("共讀取 %d 筆 CB 事件", len(events))

    all_summary, all_car, all_chip, all_alerts = [], [], [], []

    for i, row in events.iterrows():
        logger.info("[%d/%d] 處理 %s %s (T0=%s)", i + 1, len(events), row["stock_id"], row.get("stock_name", ""), row["pricing_date"].date())
        try:
            result = process_event(row)
        except Exception as e:  # noqa: BLE001
            logger.error("處理 %s 發生未預期例外，略過此事件: %s", row["stock_id"], e)
            all_summary.append({
                "stock_id": row["stock_id"],
                "stock_name": row.get("stock_name", ""),
                "cb_code": row.get("cb_code", ""),
                "pricing_date": row["pricing_date"],
                "status": "failed",
                "warnings": str(e),
            })
            continue

        all_summary.append(result.summary)
        all_car.extend(result.car_rows)
        all_chip.extend(result.chip_rows)
        all_alerts.extend(result.alert_rows)

    summary_df = pd.DataFrame(all_summary)
    car_df = pd.DataFrame(all_car)
    chip_df = pd.DataFrame(all_chip)
    alerts_df = pd.DataFrame(all_alerts)

    summary_df.to_parquet(config.EVENTS_SUMMARY_PARQUET, index=False)
    car_df.to_parquet(config.CAR_TIMESERIES_PARQUET, index=False)
    chip_df.to_parquet(config.CHIP_DETAIL_PARQUET, index=False)
    alerts_df.to_parquet(config.ALERTS_PARQUET, index=False)

    logger.info(
        "ETL 完成：%d 個事件成功, %d 個部分成功, %d 個失敗，共 %d 筆告警",
        (summary_df["status"] == "ok").sum() if not summary_df.empty else 0,
        (summary_df["status"] == "partial").sum() if not summary_df.empty else 0,
        (summary_df["status"] == "failed").sum() if not summary_df.empty else 0,
        len(alerts_df),
    )


if __name__ == "__main__":
    main()
