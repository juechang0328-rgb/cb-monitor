"""
對 events_summary.parquet 中的 CAR 樣本做統計檢定：
- 單一樣本 t 檢定：H0: 平均 CAR = 0（分別對 CAR_pre、CAR_post）
- 依承銷方式（競價拍賣 / 詢價圈購）分組檢定

可獨立執行：python backtest.py
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
from scipy import stats

import config


def _one_sample_ttest(series: pd.Series, label: str) -> dict:
    s = series.dropna()
    if len(s) < 2:
        return {"label": label, "n": len(s), "mean": s.mean() if len(s) else None, "t_stat": None, "p_value": None}
    t_stat, p_value = stats.ttest_1samp(s, popmean=0.0)
    return {
        "label": label,
        "n": len(s),
        "mean": float(s.mean()),
        "std": float(s.std(ddof=1)),
        "t_stat": float(t_stat),
        "p_value": float(p_value),
    }


def run_backtest(summary_df: pd.DataFrame) -> pd.DataFrame:
    results = []

    results.append(_one_sample_ttest(summary_df["car_pre"], "整體 CAR_pre (T-20~T-1)"))
    results.append(_one_sample_ttest(summary_df["car_post"], "整體 CAR_post (T0~T+60)"))

    if "underwriting_method" in summary_df.columns:
        for method, sub in summary_df.groupby("underwriting_method"):
            results.append(_one_sample_ttest(sub["car_pre"], f"CAR_pre [{method}]"))
            results.append(_one_sample_ttest(sub["car_post"], f"CAR_post [{method}]"))

    if "guaranteed" in summary_df.columns:
        for g, sub in summary_df.groupby("guaranteed"):
            results.append(_one_sample_ttest(sub["car_pre"], f"CAR_pre [{g}]"))
            results.append(_one_sample_ttest(sub["car_post"], f"CAR_post [{g}]"))

    return pd.DataFrame(results)


def main() -> None:
    if not config.EVENTS_SUMMARY_PARQUET.exists():
        print(f"找不到 {config.EVENTS_SUMMARY_PARQUET}，請先執行 etl.py", file=sys.stderr)
        sys.exit(1)

    summary_df = pd.read_parquet(config.EVENTS_SUMMARY_PARQUET)
    summary_df = summary_df[summary_df["status"].isin(["ok", "partial"])]

    if summary_df.empty:
        print("目前沒有可用的事件資料可供檢定。")
        return

    results_df = run_backtest(summary_df)
    pd.set_option("display.float_format", lambda x: f"{x:.4f}")
    print("=" * 70)
    print("CB 定價事件 CAR 統計檢定結果（H0: 平均 CAR = 0）")
    print("=" * 70)
    print(results_df.to_string(index=False))
    print()
    print("註：本結果僅供學術研究與市場異常型態觀察，不構成投資建議。")


if __name__ == "__main__":
    main()
