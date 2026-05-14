"""미네비니 Trend Template 진단 도구.

지정 종목의 *각 영업일*에 8조건이 어떻게 통과·실패하는지 추적해
어느 조건이 한국 시장에서 병목인지 정량 분석한다.

사용:
    python scripts/inspect_template.py 005930              # 삼성전자 — 전체 기간
    python scripts/inspect_template.py 000660 --tail 60    # 최근 60봉만
    python scripts/inspect_template.py 005930 035720 000660  # 여러 종목 비교
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from moneygold import indicators as ind, template as tmpl  # noqa: E402
from moneygold.config import load_config  # noqa: E402
from moneygold.data import store  # noqa: E402


COND_DESC = [
    "1: close > sma150 & sma200",
    "2: sma150 > sma200",
    "3: sma200 > sma200[22]",
    "4: sma50 > sma150, sma200",
    "5: close > sma50",
    "6: close >= low_52w × 1.25 (저가 기준)",
    "7: close >= high_52w × 0.75 (고가 기준)",
    "8: rs_rank >= 70",
]


def _diagnose_series(
    close: pd.Series, high: pd.Series, low: pd.Series,
    dates: pd.Series, rs_rank: pd.Series,
) -> pd.DataFrame:
    """각 영업일의 8조건 통과 여부 시계열로 반환."""
    sma50 = ind.sma(close, 50)
    sma150 = ind.sma(close, 150)
    sma200 = ind.sma(close, 200)
    sma200_prev = sma200.shift(22)
    hi52 = ind.rolling_high(high, 260)
    lo52 = ind.rolling_low(low, 260)

    c1 = (close > sma150) & (close > sma200)
    c2 = sma150 > sma200
    c3 = sma200 > sma200_prev
    c4 = (sma50 > sma150) & (sma50 > sma200)
    c5 = close > sma50
    c6 = close >= lo52 * 1.25
    c7 = close >= hi52 * 0.75
    c8 = rs_rank >= 70

    out = pd.DataFrame({
        "date": dates.values,
        "close": close.values,
        "c1": c1.values, "c2": c2.values, "c3": c3.values, "c4": c4.values,
        "c5": c5.values, "c6": c6.values, "c7": c7.values, "c8": c8.values,
    })
    out["passed"] = out[["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]].all(axis=1)
    out["passed_count"] = out[["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]].sum(axis=1)
    return out


def _market_of(ticker: str, master: pd.DataFrame) -> str:
    row = master[master["ticker"] == ticker]
    if row.empty:
        return "KOSPI"
    return row.iloc[0]["market"]


def _compute_rs_rank_for_ticker(
    ticker: str,
    bars_lookup: dict[str, pd.DataFrame],
    master: pd.DataFrame,
) -> pd.Series:
    """각 영업일의 rs_rank 시계열을 계산.

    각 시점에 대해 동일 시장 내 모든 종목의 rs_momentum 계산 후 횡단면 백분위.
    무거워서 모든 종목에 대해 252봉마다 한 번씩만 (5일 간격)으로 샘플링.
    """
    target_bars = bars_lookup.get(ticker)
    if target_bars is None:
        return pd.Series(dtype=float)
    target_bars = target_bars.sort_values("date").reset_index(drop=True)
    market = _market_of(ticker, master)
    universe = master[master["market"] == market]["ticker"].tolist()

    # 종목별 close 시리즈 캐시
    closes = {}
    for tk in universe:
        b = bars_lookup.get(tk)
        if b is None or len(b) < 253:
            continue
        b = b.sort_values("date").reset_index(drop=True)
        closes[tk] = b["close"].astype(float).values, b["date"].values

    # target의 각 날짜에서 rs_rank 계산 (5봉마다 샘플링)
    n = len(target_bars)
    rs_rank_series = pd.Series([np.nan] * n, dtype=float)

    for i in range(252, n, 5):  # 워밍업 252봉 후 5봉마다
        cur_date = target_bars["date"].iloc[i]
        rs_moms = {}
        for tk, (c_arr, d_arr) in closes.items():
            # cur_date 이전 데이터의 rs_momentum
            mask = d_arr <= cur_date
            if mask.sum() < 253:
                continue
            sub_close = c_arr[mask]
            last = sub_close[-1]
            score = 0.0
            ok = True
            for p, w in zip((63, 126, 189, 252), (0.4, 0.2, 0.2, 0.2)):
                if len(sub_close) <= p:
                    ok = False; break
                prev = sub_close[-p - 1]
                if prev == 0 or last == 0:
                    ok = False; break
                score += w * (last / prev - 1.0)
            if ok:
                rs_moms[tk] = score

        if ticker in rs_moms:
            scores_series = pd.Series(rs_moms)
            ranked = ind.rs_rank(scores_series)
            rs_rank_series.iloc[i] = float(ranked[ticker])

    # 결측은 forward-fill로 채움 (5일 간격 샘플링)
    return rs_rank_series.fillna(method="ffill")


def _summarize(df: pd.DataFrame, name: str, ticker: str) -> None:
    print(f"\n=== {ticker} {name} ===")
    print(f"기간: {df['date'].min()} ~ {df['date'].max()} ({len(df)} 봉)")
    valid = df.dropna(subset=["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"])
    if valid.empty:
        print("(데이터 부족)")
        return
    n = len(valid)
    print(f"통과율 (각 조건이 True인 봉 비율):")
    for i, desc in enumerate(COND_DESC, start=1):
        col = f"c{i}"
        rate = valid[col].mean() * 100
        bar = "█" * int(rate / 5)
        print(f"  {desc:<32} {rate:>5.1f}%  {bar}")
    all_pass_rate = valid["passed"].mean() * 100
    print(f"  {'전체 8/8 통과':<32} {all_pass_rate:>5.1f}%")

    # 어느 조건들이 동시에 자주 실패하는지
    failed_counts = {}
    for i in range(1, 9):
        col = f"c{i}"
        n_fail = (~valid[col]).sum()
        failed_counts[i] = n_fail
    print(f"\n실패 횟수가 많은 조건 (총 {n}봉 중):")
    for i, c in sorted(failed_counts.items(), key=lambda x: -x[1])[:5]:
        print(f"  조건 {i}: {c}회 실패 ({c/n*100:.1f}%)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tickers", nargs="+", help="조회할 종목코드")
    parser.add_argument("--tail", type=int, help="최근 N봉만 통계")
    parser.add_argument("--skip-rs", action="store_true", help="RS rank 계산 생략(고정 100, 빠른 진단)")
    args = parser.parse_args()

    cfg = load_config()
    data_dir = Path(cfg.data_dir)
    master = store.read_parquet_safe(store.master_path(data_dir))
    if master is None:
        print("마스터 없음", file=sys.stderr)
        return 1

    # bars lookup
    print("Loading bars ...", file=sys.stderr)
    bars_lookup = {}
    for tk in master["ticker"]:
        b = store.read_parquet_safe(store.bars_path(data_dir, tk))
        if b is not None and not b.empty:
            bars_lookup[tk] = b

    for tk in args.tickers:
        bars = bars_lookup.get(tk)
        if bars is None:
            print(f"\n{tk}: bars 없음")
            continue
        bars = bars.sort_values("date").reset_index(drop=True)
        name = master.loc[master["ticker"] == tk, "name"].iloc[0] if (master["ticker"] == tk).any() else "?"
        if args.skip_rs:
            rs_rank = pd.Series([100.0] * len(bars))   # 항상 통과
        else:
            print(f"\n[{tk} {name}] computing RS rank (cross-sectional) ...", file=sys.stderr)
            rs_rank = _compute_rs_rank_for_ticker(tk, bars_lookup, master)
        df = _diagnose_series(
            bars["close"].astype(float), bars["high"].astype(float),
            bars["low"].astype(float), bars["date"], rs_rank,
        )
        if args.tail:
            df = df.tail(args.tail)
        _summarize(df, name, tk)

        # 최근 5봉 상세
        print("\n최근 5봉 상세:")
        cols = ["date", "close"] + [f"c{i}" for i in range(1, 9)] + ["passed_count", "passed"]
        print(df[cols].tail(5).to_string(index=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
