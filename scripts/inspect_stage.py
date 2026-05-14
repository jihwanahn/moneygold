"""Weinstein Stage 진단 도구.

지정 종목의 최근 N봉 Stage 분류 변화를 출력한다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from moneygold import stage as stg  # noqa: E402
from moneygold.config import load_config  # noqa: E402
from moneygold.data import store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--tail", type=int, default=30)
    args = parser.parse_args()

    cfg = load_config()
    data_dir = Path(cfg.data_dir)
    master = store.read_parquet_safe(store.master_path(data_dir))
    if master is None:
        print("마스터 없음", file=sys.stderr)
        return 1

    s = cfg.strategy
    params = stg.StageParams(
        ma_length=s.stage_ma_length,
        slope_lookback=s.stage_slope_lookback,
        slope_threshold_pct=s.stage_slope_threshold_pct,
        band_pct=s.stage_band_pct,
        ma_type=s.stage_ma_type,
    )

    for tk in args.tickers:
        bars = store.read_parquet_safe(store.bars_path(data_dir, tk))
        if bars is None or bars.empty:
            print(f"{tk}: bars 없음")
            continue
        bars = bars.sort_values("date").reset_index(drop=True)
        name = master.loc[master["ticker"] == tk, "name"].iloc[0] if (master["ticker"] == tk).any() else "?"
        series = stg.classify_stage_series(bars["close"].astype(float), params)

        full_dist = series.value_counts().sort_index()
        last = series.iloc[-1]
        last_name = stg.STAGE_NAMES.get(int(last), "?")
        print(f"\n=== {tk} {name} ===")
        print(f"  현재: Stage {last} ({last_name})")
        print(f"  전체 분포: {full_dist.to_dict()}")
        print(f"  최근 {args.tail}봉:")
        for d, st in zip(bars["date"].astype(str).tail(args.tail), series.tail(args.tail)):
            print(f"    {d}  Stage {int(st)} {stg.STAGE_NAMES.get(int(st), '?')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
