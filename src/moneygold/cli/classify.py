"""Stage 분류 CLI.

전체 유니버스에 대해 오늘(또는 --asof 기준일)의 Weinstein Stage를 계산하고
분포·Stage 2 종목 리스트·CSV export를 제공한다.

사용:
    python -m moneygold.cli.classify
    python -m moneygold.cli.classify --asof 20260514
    python -m moneygold.cli.classify --asof 20260514 --export result/stage_20260514.csv
    python -m moneygold.cli.classify --top 50   # Stage 2 종목 상위 50개만 출력
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from .. import indicators as ind
from .. import stage as stg
from ..config import load_config
from ..data import store


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_index_close(data_dir: Path, index_code: str) -> pd.Series:
    """지수 일봉 → Series(index=date YYYYMMDD str, values=close)."""
    p = store.index_path(data_dir, index_code)
    df = store.read_parquet_safe(p)
    if df is None or df.empty:
        raise FileNotFoundError(f"지수 {index_code} 없음: {p}. `--indices`로 sync 먼저.")
    s = df.set_index("date")["close"].astype(float)
    return s


def _compute_one(
    ticker: str,
    market: str,
    data_dir: Path,
    asof: str,
    idx_close_by_market: dict[str, pd.Series],
    sma_window: int,
    slope_lookback: int,
    rs_slope_lookback: int,
) -> dict | None:
    p = store.bars_path(data_dir, ticker)
    bars = store.read_parquet_safe(p)
    if bars is None or bars.empty:
        return None
    # asof 이전 데이터만
    bars = bars[bars["date"] <= asof].copy()
    if len(bars) < sma_window + slope_lookback:
        return None

    idx_close = idx_close_by_market.get(market)
    if idx_close is None:
        return None

    bars = bars.sort_values("date").reset_index(drop=True)
    close = bars["close"].astype(float)
    sma_30w = ind.sma(close, sma_window)
    sma_slope = ind.slope_normalized(sma_30w, slope_lookback)

    # RS line
    close_by_date = close.copy()
    close_by_date.index = bars["date"]
    rs = ind.rs_line(close_by_date, idx_close)
    rs_slope = ind.slope_normalized(rs, rs_slope_lookback)

    if rs.empty:
        return None

    last_close = float(close.iloc[-1])
    last_sma = float(sma_30w.iloc[-1]) if pd.notna(sma_30w.iloc[-1]) else float("nan")
    last_sma_slope = float(sma_slope.iloc[-1]) if pd.notna(sma_slope.iloc[-1]) else float("nan")
    last_rs = float(rs.iloc[-1]) if not rs.empty else float("nan")
    last_rs_slope = float(rs_slope.iloc[-1]) if not rs_slope.empty and pd.notna(rs_slope.iloc[-1]) else float("nan")

    stage_val = stg.classify_stage(last_close, last_sma, last_sma_slope, last_rs_slope)
    return {
        "ticker": ticker,
        "market": market,
        "close": last_close,
        "sma_30w": last_sma,
        "sma_30w_slope": last_sma_slope,
        "rs_line": last_rs,
        "rs_line_slope": last_rs_slope,
        "stage": stage_val,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weinstein Stage classifier")
    parser.add_argument("--asof", help="기준일 YYYYMMDD. 기본은 오늘.")
    parser.add_argument("--export", help="CSV 출력 경로")
    parser.add_argument("--top", type=int, default=30, help="Stage 2 종목 출력 상위 N (기본 30)")
    parser.add_argument("--sma-window", type=int, default=150, help="30주 SMA 윈도우 (기본 150)")
    parser.add_argument("--slope-lookback", type=int, default=50, help="MA slope lookback (기본 50)")
    parser.add_argument("--limit", type=int, help="첫 N개 종목만 (디버그)")
    args = parser.parse_args(argv)

    cfg = load_config()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("moneygold.cli.classify")

    asof = args.asof or datetime.now().strftime("%Y%m%d")
    data_dir = Path(cfg.data_dir)

    # 마스터 + 지수 로드
    master = store.read_parquet_safe(store.master_path(data_dir))
    if master is None or master.empty:
        log.error("마스터 없음. `python -m moneygold.cli.sync --universe` 먼저.")
        return 1

    try:
        idx_kospi = _load_index_close(data_dir, "KOSPI200")
        idx_kosdaq = _load_index_close(data_dir, "KOSDAQ150")
    except FileNotFoundError as e:
        log.error(str(e))
        return 1

    # asof 이전으로 제한
    idx_kospi = idx_kospi[idx_kospi.index <= asof]
    idx_kosdaq = idx_kosdaq[idx_kosdaq.index <= asof]

    idx_close_by_market = {"KOSPI": idx_kospi, "KOSDAQ": idx_kosdaq}

    rows = master.to_dict(orient="records")
    if args.limit:
        rows = rows[: args.limit]

    log.info("Classifying %d tickers as of %s ...", len(rows), asof)
    results = []
    for row in tqdm(rows, desc="stage", unit="tk"):
        r = _compute_one(
            row["ticker"], row["market"], data_dir, asof,
            idx_close_by_market,
            sma_window=args.sma_window,
            slope_lookback=args.slope_lookback,
            rs_slope_lookback=args.slope_lookback,
        )
        if r is not None:
            results.append(r)

    if not results:
        log.warning("판정 가능한 종목 없음.")
        return 1

    out = pd.DataFrame(results)

    # RS rank 시장별 분리 계산
    out["rs_rank"] = float("nan")
    for market, group in out.groupby("market"):
        ranked = ind.rs_rank(group["rs_line"])
        out.loc[ranked.index, "rs_rank"] = ranked

    # 분포 출력
    print(f"\n=== Stage distribution as of {asof} (n={len(out)}) ===")
    dist = out["stage"].value_counts().sort_index()
    for stage_code, count in dist.items():
        name = stg.STAGE_NAMES.get(int(stage_code), "?")
        pct = count / len(out) * 100
        print(f"  Stage {stage_code} ({name:>9}): {count:>5}  ({pct:5.1f}%)")

    # Stage 2 상위 N개
    stage2 = out[out["stage"] == stg.STAGE_ADVANCING].copy()
    stage2 = stage2.sort_values("rs_rank", ascending=False)
    name_map = master.set_index("ticker")["name"].to_dict()
    stage2["name"] = stage2["ticker"].map(name_map)

    print(f"\n=== Stage 2 top {min(args.top, len(stage2))} by RS rank ===")
    cols_to_print = ["ticker", "name", "market", "close", "rs_rank", "rs_line_slope", "sma_30w_slope"]
    print(stage2[cols_to_print].head(args.top).to_string(index=False, float_format=lambda x: f"{x:>10.3f}"))

    if args.export:
        out_path = Path(args.export)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out["name"] = out["ticker"].map(name_map)
        out.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"\nExported: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
