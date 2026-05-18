"""Gainers 시그너처 검증용 event study 백테스트.

목적: '오늘 상승' 종목에 대해 우리가 정의한 시그너처(SMA200/52w 고가 대비
위치, Stage 등)가 *forward return*과 실제로 연관 있는지 검증.

기존 ``backtest.py``는 signals.py 전체 전략(BUY/HOLD/SELL)을 walk-forward로
시뮬레이션 — 이 모듈은 더 가벼운 event study:

1. 과거 [start, end] 구간의 각 거래일 t에 대해
2. 각 ticker가 +``min_pct`` 이상 상승했는지 체크
3. 상승했다면 t 시점 feature(close_to_sma200 등) + Stage 스냅샷
4. ``horizons``의 각 h에 대해 forward return ``close[t+h] / close[t] - 1``

Look-ahead 방지: feature는 ``bars[:t+1]``만 사용, forward return은 ``t+1..t+h``
의 close만. ticker별로 feature 시계열을 한 번에 계산해 1.6M 조회를 indexing
으로 처리.

ARCHITECTURE.md 재현성 원칙: 모든 함수는 ``start``, ``end`` 명시 필수.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import indicators as ind
from . import stage as stg
from .data import store

log = logging.getLogger(__name__)


# ============================================================
# Feature/forward return 계산 (단일 ticker)
# ============================================================

@dataclass(frozen=True)
class TickerSeries:
    """단일 ticker의 full-history feature + forward return 시계열."""
    ticker: str
    market: str
    df: pd.DataFrame  # index=date 순서, columns 아래 참고

    @property
    def dates(self) -> pd.Series:
        return self.df["date"]


_FEATURE_COLS = [
    "close_to_sma50", "close_to_sma150", "close_to_sma200",
    "close_to_52w_high", "close_to_52w_low",
    "sma50_over_sma200", "sma200_slope", "rvol",
    "golden_cross",
]


def _build_ticker_series(
    bars: pd.DataFrame,
    ticker: str,
    market: str,
    horizons: Sequence[int],
    stage_params: stg.StageParams,
) -> TickerSeries | None:
    """단일 ticker에 대해 모든 date의 feature + forward return을 한 번에 계산.

    Returns None: 252봉 미만이면 SMA200 계산 불가, 무시.
    """
    if bars is None or bars.empty or len(bars) < 252:
        return None
    b = bars.sort_values("date").reset_index(drop=True)
    close = b["close"].astype(float)
    high = b["high"].astype(float) if "high" in b.columns else close
    low = b["low"].astype(float) if "low" in b.columns else close
    vol = b["volume"].astype(float)

    # SMA들
    sma50 = ind.sma(close, 50)
    sma150 = ind.sma(close, 150)
    sma200 = ind.sma(close, 200)
    sma200_slope = ind.slope_normalized(sma200, 22)

    # 52w rolling high/low (포함 252봉)
    hi52 = close.rolling(window=252, min_periods=252).max()
    lo52 = close.rolling(window=252, min_periods=252).min()

    # 50일 rolling high (pullback 계산용)
    hi50 = close.rolling(window=50, min_periods=50).max()

    # Relative volume: today / avg of prior 19 bars
    # rolling(20).mean() include today. So shift(1).rolling(19).mean() = prior 19 mean.
    avg_vol19_prior = vol.shift(1).rolling(window=19, min_periods=19).mean()

    # 1봉 변동률 (전일 종가 대비)
    pct_chg = close.pct_change(1)

    # Stage 분류 (전체 시계열)
    stage_series = stg.classify_stage_series(close, stage_params)

    # PR16: 추가 feature 시계열
    rsi_series = ind.rsi(close, 14)
    bb_series = ind.bollinger_position(close, 20, 2.0)
    atr_series = ind.atr(high, low, close, 14)
    atr_pct_series = atr_series / close
    atr_norm_move = pct_chg / atr_pct_series
    pullback_50d = 1.0 - close / hi50

    # Forward returns: close[t+h] / close[t] - 1
    fwd_data = {}
    for h in horizons:
        fwd_data[f"fwd_{h}d"] = close.shift(-h) / close - 1.0

    out = pd.DataFrame({
        "date": b["date"].astype(str),
        "close": close,
        "pct_chg": pct_chg,
        "stage": stage_series.astype("Int8"),
        "close_to_sma50": close / sma50,
        "close_to_sma150": close / sma150,
        "close_to_sma200": close / sma200,
        "close_to_52w_high": close / hi52,
        "close_to_52w_low": close / lo52,
        "sma50_over_sma200": sma50 / sma200,
        "sma200_slope": sma200_slope,
        "rvol": vol / avg_vol19_prior,
        "golden_cross": (sma50 > sma200),
        # PR16
        "rsi_14": rsi_series,
        "bb_position": bb_series,
        "atr_normalized_move": atr_norm_move,
        "pullback_from_50d_high": pullback_50d,
        **fwd_data,
    })
    out["ticker"] = ticker
    out["market"] = market
    return TickerSeries(ticker=ticker, market=market, df=out)


# ============================================================
# 이벤트 수집
# ============================================================

def collect_events(
    data_dir: Path,
    *,
    start: str,
    end: str,
    market: str | None = None,
    min_pct: float = 0.01,
    horizons: Sequence[int] = (1, 5, 20),
    stage_params: stg.StageParams | None = None,
    progress: bool = True,
) -> pd.DataFrame:
    """기간 [start, end]의 gainers 이벤트 + features + forward returns 수집.

    Parameters
    ----------
    data_dir
        store 루트.
    start, end
        YYYYMMDD inclusive.
    market
        'US' / 'KOSPI' / 'KOSDAQ' / None(전체).
    min_pct
        gainer 컷오프 (0.01 = +1%).
    horizons
        forward return 일수 (default 1, 5, 20).
    stage_params
        Stage 분류 파라미터. None이면 default.

    Returns
    -------
    DataFrame long format: row = (date, ticker) gainer 이벤트.
        columns = [date, ticker, market, close, pct_chg, stage,
                   close_to_sma{50,150,200}, close_to_52w_{high,low},
                   sma50_over_sma200, sma200_slope, rvol, golden_cross,
                   fwd_{h}d for h in horizons]

    Look-ahead 방지: 각 row의 feature는 그 row의 ``date`` 시점까지만 사용한
    SMA. forward return은 그 시점 이후 close. NaN은 horizon 너머라 측정 불가.
    """
    sp = stage_params or stg.StageParams()
    horizons = tuple(horizons)

    master = store.read_parquet_safe(store.master_path(data_dir))
    if master is None or master.empty:
        return pd.DataFrame()
    if market is not None:
        master = master[master["market"] == market]

    iterator = master.itertuples(index=False)
    if progress:
        try:
            from tqdm import tqdm
            iterator = tqdm(iterator, total=len(master), desc="ticker", unit="tk")
        except ImportError:
            pass

    all_events: list[pd.DataFrame] = []
    for row in iterator:
        bars = store.read_parquet_safe(store.bars_path(data_dir, row.ticker))
        if bars is None or bars.empty:
            continue
        ts = _build_ticker_series(
            bars, ticker=row.ticker, market=row.market,
            horizons=horizons, stage_params=sp,
        )
        if ts is None:
            continue
        df = ts.df
        # 이벤트 필터: 기간 + gainer 컷
        mask = (
            (df["date"] >= start)
            & (df["date"] <= end)
            & (df["pct_chg"] >= min_pct)
            & df["close_to_sma200"].notna()  # 252봉 이상 데이터 필요
        )
        if mask.any():
            all_events.append(df.loc[mask].copy())

    if not all_events:
        return pd.DataFrame()
    events = pd.concat(all_events, ignore_index=True)
    log.info("collect_events: %d events from %s..%s", len(events), start, end)
    return events


# ============================================================
# 그룹별 통계
# ============================================================

def _stats_for_group(returns: pd.Series) -> dict:
    """단일 그룹 + 단일 horizon의 forward return 통계."""
    r = returns.dropna()
    if r.empty:
        return {"n": 0, "mean": np.nan, "median": np.nan, "win_rate": np.nan,
                "std": np.nan, "sharpe": np.nan, "p25": np.nan, "p75": np.nan}
    mean = float(r.mean())
    std = float(r.std(ddof=1)) if len(r) > 1 else float("nan")
    return {
        "n": int(len(r)),
        "mean": mean,
        "median": float(r.median()),
        "win_rate": float((r > 0).sum() / len(r)),
        "std": std,
        "sharpe": float(mean / std) if std and std > 0 else float("nan"),
        "p25": float(r.quantile(0.25)),
        "p75": float(r.quantile(0.75)),
    }


def group_report(
    events: pd.DataFrame,
    horizons: Sequence[int],
    *,
    groups: dict[str, pd.Series | None] | None = None,
) -> pd.DataFrame:
    """그룹별 × horizon별 forward return 통계.

    Parameters
    ----------
    events
        collect_events 결과.
    horizons
        compare할 horizon 리스트.
    groups
        ``{label: mask_series}`` dict. None이면 default 셋업
        (All, Stage 2, Stage 4, SMA200 above, SMA200 below, 52w high near,
        52w high far, Golden cross).

    Returns
    -------
    DataFrame  rows=(group, horizon), columns=stats.
    """
    if events.empty:
        return pd.DataFrame()
    if groups is None:
        groups = _default_groups(events)

    rows = []
    for label, mask in groups.items():
        sub = events if mask is None else events[mask]
        for h in horizons:
            col = f"fwd_{h}d"
            if col not in sub.columns:
                continue
            stats = _stats_for_group(sub[col])
            rows.append({"group": label, "horizon_d": h, **stats})
    return pd.DataFrame(rows)


def _default_groups(events: pd.DataFrame) -> dict[str, pd.Series | None]:
    """기본 그룹: Stage / SMA200 / 52w high / golden cross + PR16 신규 feature buckets."""
    groups: dict[str, pd.Series | None] = {
        "ALL": None,
        "Stage 2": events["stage"] == stg.STAGE_ADVANCING,
        "Stage 4": events["stage"] == stg.STAGE_DECLINING,
        "SMA200 above (≥1.0)": events["close_to_sma200"] >= 1.0,
        "SMA200 below (<1.0)": events["close_to_sma200"] < 1.0,
        "52w high near (≥0.9)": events["close_to_52w_high"] >= 0.9,
        "52w high far (<0.7)": events["close_to_52w_high"] < 0.7,
        "Golden cross": events["golden_cross"] == True,  # noqa: E712
        "Dead cross": events["golden_cross"] == False,   # noqa: E712
        "Stage 2 + SMA200 above": (
            (events["stage"] == stg.STAGE_ADVANCING)
            & (events["close_to_sma200"] >= 1.0)
        ),
    }
    # --- PR16: RSI / BB / ATR-norm / Pullback buckets ---
    if "rsi_14" in events.columns:
        r = events["rsi_14"]
        groups["RSI<30 (oversold)"] = r < 30
        groups["RSI 30-50"] = (r >= 30) & (r < 50)
        groups["RSI 50-70"] = (r >= 50) & (r < 70)
        groups["RSI≥70 (overbought)"] = r >= 70
    if "bb_position" in events.columns:
        b = events["bb_position"]
        groups["BB<0.2 (하단 근처)"] = b < 0.2
        groups["BB 0.2-0.5"] = (b >= 0.2) & (b < 0.5)
        groups["BB 0.5-0.8"] = (b >= 0.5) & (b < 0.8)
        groups["BB≥0.8 (상단 근처)"] = b >= 0.8
    if "atr_normalized_move" in events.columns:
        a = events["atr_normalized_move"]
        groups["ATR-move <0.5 (잠잠)"] = a < 0.5
        groups["ATR-move 0.5-1.5 (보통)"] = (a >= 0.5) & (a < 1.5)
        groups["ATR-move 1.5-3 (big)"] = (a >= 1.5) & (a < 3.0)
        groups["ATR-move ≥3 (이상치)"] = a >= 3.0
    if "pullback_from_50d_high" in events.columns:
        p = events["pullback_from_50d_high"]
        groups["Pullback 0-5%"] = (p >= 0) & (p < 0.05)
        groups["Pullback 5-15%"] = (p >= 0.05) & (p < 0.15)
        groups["Pullback 15-30%"] = (p >= 0.15) & (p < 0.30)
        groups["Pullback ≥30%"] = p >= 0.30
    return groups


def edge_table(report: pd.DataFrame, baseline: str = "ALL") -> pd.DataFrame:
    """그룹별 mean return의 baseline 대비 edge (차이 + t-stat 근사).

    Returns
    -------
    DataFrame  (group × horizon)에 edge_bps, sample_ratio 컬럼 추가.
    """
    if report.empty:
        return report
    base = report[report["group"] == baseline].set_index("horizon_d")
    rows = []
    for _, r in report.iterrows():
        h = r["horizon_d"]
        if h not in base.index:
            continue
        base_mean = base.loc[h, "mean"]
        edge = (r["mean"] - base_mean) * 10000  # bps
        rows.append({
            "group": r["group"],
            "horizon_d": h,
            "n": r["n"],
            "mean_pct": r["mean"] * 100,
            "edge_vs_all_bps": edge,
            "win_rate": r["win_rate"],
            "sharpe": r["sharpe"],
        })
    return pd.DataFrame(rows)
