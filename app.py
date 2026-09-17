"""
Streamlit 儀表板：CB 定價日前後股價與籌碼異常監控
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import config

st.set_page_config(page_title="CB 定價事件監控儀表板", layout="wide")


@st.cache_data(ttl=3600)
def load_data():
    def _read(path):
        return pd.read_parquet(path) if path.exists() else pd.DataFrame()

    summary = _read(config.EVENTS_SUMMARY_PARQUET)
    car = _read(config.CAR_TIMESERIES_PARQUET)
    chip = _read(config.CHIP_DETAIL_PARQUET)
    alerts = _read(config.ALERTS_PARQUET)
    return summary, car, chip, alerts


summary_df, car_df, chip_df, alerts_df = load_data()

st.title("可轉換公司債（CB）定價日前後股價與籌碼異常監控儀表板")
st.warning(
    "⚠️ **免責聲明**：本工具僅供學術研究與市場異常型態觀察使用，"
    "所有指標為統計估計值，不構成任何投資建議或買賣依據。"
)

if summary_df.empty:
    st.info("尚無資料，請先執行 `python etl.py` 產生 data/*.parquet。")
    st.stop()

summary_df["pricing_date"] = pd.to_datetime(summary_df["pricing_date"])
today = pd.Timestamp.today().normalize()

# ---------------------------------------------------------------------------
# 側邊欄篩選
# ---------------------------------------------------------------------------
st.sidebar.header("篩選條件")

stage = st.sidebar.radio(
    "階段",
    ["全部", f"即將定價（T-{config.UPCOMING_PRICING_WINDOW_DAYS}內）", f"已定價拉抬期（T0~T+{config.RECENT_POST_PRICING_WINDOW_DAYS}）"],
)

filtered = summary_df.copy()
if stage.startswith("即將定價"):
    lo, hi = today, today + pd.Timedelta(days=config.UPCOMING_PRICING_WINDOW_DAYS)
    filtered = filtered[(filtered["pricing_date"] >= lo) & (filtered["pricing_date"] <= hi)]
elif stage.startswith("已定價拉抬期"):
    lo, hi = today - pd.Timedelta(days=config.RECENT_POST_PRICING_WINDOW_DAYS), today
    filtered = filtered[(filtered["pricing_date"] >= lo) & (filtered["pricing_date"] <= hi)]

if "underwriting_method" in summary_df.columns:
    methods = sorted(summary_df["underwriting_method"].dropna().unique().tolist())
    selected_methods = st.sidebar.multiselect("承銷方式", methods, default=methods)
    if selected_methods:
        filtered = filtered[filtered["underwriting_method"].isin(selected_methods)]

if "guaranteed" in summary_df.columns:
    guarantees = sorted(summary_df["guaranteed"].dropna().unique().tolist())
    selected_guarantees = st.sidebar.multiselect("有無擔保", guarantees, default=guarantees)
    if selected_guarantees:
        filtered = filtered[filtered["guaranteed"].isin(selected_guarantees)]

status_filter = st.sidebar.multiselect(
    "資料狀態", ["ok", "partial", "failed"], default=["ok", "partial"]
)
if status_filter:
    filtered = filtered[filtered["status"].isin(status_filter)]

st.sidebar.caption(f"符合篩選條件：{len(filtered)} / {len(summary_df)} 檔")

# ---------------------------------------------------------------------------
# CAR 散點圖：定價前 vs 定價後
# ---------------------------------------------------------------------------
st.subheader("CAR 散點圖：定價前（壓價期）vs 定價後（拉抬期）")

plot_df = filtered.dropna(subset=["car_pre", "car_post"])
if plot_df.empty:
    st.info("目前篩選結果中沒有足夠資料繪製散點圖。")
else:
    plot_df = plot_df.assign(
        label=plot_df["stock_id"] + " " + plot_df["stock_name"].fillna(""),
        has_alert=plot_df["alerts"].fillna("") != "",
    )
    fig = px.scatter(
        plot_df,
        x="car_pre",
        y="car_post",
        color="has_alert",
        color_discrete_map={True: "#d62728", False: "#1f77b4"},
        hover_name="label",
        hover_data={"car_pre": ":.2%", "car_post": ":.2%", "underwriting_method": True, "alerts": True, "has_alert": False},
        labels={"car_pre": "CAR_pre (T-20~T-1)", "car_post": "CAR_post (T0~T+60)", "has_alert": "有告警"},
    )
    fig.add_hline(y=config.ALERT_CAR_POST_THRESHOLD, line_dash="dot", line_color="gray")
    fig.add_vline(x=config.ALERT_CAR_PRE_THRESHOLD, line_dash="dot", line_color="gray")
    fig.update_xaxes(tickformat=".0%")
    fig.update_yaxes(tickformat=".0%")
    st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------------------
# 個股 CAR 走勢圖
# ---------------------------------------------------------------------------
st.subheader("個股 CAR 走勢圖")

available_ids = filtered["stock_id"].tolist()
label_map = {
    r["stock_id"]: f"{r['stock_id']} {r.get('stock_name', '')} (T0={pd.Timestamp(r['pricing_date']).date()})"
    for _, r in filtered.iterrows()
}
selected_stocks = st.multiselect(
    "選擇要比較的個股",
    available_ids,
    default=available_ids[: min(3, len(available_ids))],
    format_func=lambda x: label_map.get(x, x),
)

if selected_stocks and not car_df.empty:
    sub_car = car_df[car_df["stock_id"].isin(selected_stocks)]
    fig2 = go.Figure()
    for sid in selected_stocks:
        s = sub_car[sub_car["stock_id"] == sid].sort_values("rel_day")
        fig2.add_trace(go.Scatter(x=s["rel_day"], y=s["CAR"], mode="lines+markers", name=label_map.get(sid, sid)))
    fig2.add_vline(x=0, line_dash="dash", line_color="black", annotation_text="T0 定價日")
    fig2.update_yaxes(title="CAR", tickformat=".0%")
    fig2.update_xaxes(title="相對交易日 (T0=0)")
    st.plotly_chart(fig2, use_container_width=True)
else:
    st.info("請選擇至少一檔個股以繪製 CAR 走勢圖。")

# ---------------------------------------------------------------------------
# 籌碼異動明細
# ---------------------------------------------------------------------------
st.subheader("籌碼異動明細（三大法人 / 融券 / 借券）")

if selected_stocks and not chip_df.empty:
    sub_chip = chip_df[chip_df["stock_id"].isin(selected_stocks)].sort_values(["stock_id", "rel_day"])
    st.dataframe(
        sub_chip[[
            "stock_id", "date", "rel_day", "institutional_net",
            "short_sale_balance", "lending_volume", "volume", "volume_ratio",
        ]],
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("請選擇個股以檢視籌碼明細。")

# ---------------------------------------------------------------------------
# 異常告警清單
# ---------------------------------------------------------------------------
st.subheader("異常告警清單")

if alerts_df.empty:
    st.info("目前沒有觸發任何告警。")
else:
    display_alerts = alerts_df[alerts_df["stock_id"].isin(filtered["stock_id"])] if not filtered.empty else alerts_df
    st.dataframe(display_alerts, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# 全部事件總表
# ---------------------------------------------------------------------------
st.subheader("事件總表")
st.dataframe(filtered, use_container_width=True, hide_index=True)

st.caption(
    "資料來源：yfinance（價量）、FinMind（籌碼）、cb_events_manual.csv（事件清單，人工維護）。"
    "AR/CAR 以市場模型（估計期 T-150~T-31，基準 ^TWII）估計。"
)
