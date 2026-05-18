"""오늘 상승 종목 추출.

universe의 각 ticker에 대해 ``asof``의 종가와 직전 거래일 종가를 비교해
``pct_chg = (close - prev_close) / prev_close`` 가 ``min_pct`` 이상인
종목만 반환. signals.py와 독립적이라 BUY 게이트와 무관하게 *모든* 상승
종목을 본다.

ARCHITECTURE.md의 재현성 원칙: ``asof`` 명시 필수, 모듈 내부에서
``datetime.now()`` 호출 없음.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import stage as stg
from .data import store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GainerRow:
    """단일 ticker의 일일 변동 (오늘 종가 vs 전일 종가)."""
    ticker: str
    market: str
    name: str
    close: float
    prev_close: float
    pct_chg: float        # (close - prev_close) / prev_close, 0.01 = +1%


def _last_two_closes(bars: pd.DataFrame, asof: str) -> tuple[float, float] | None:
    """asof 이하의 마지막 2봉 종가 (prev, today). 부족하면 None."""
    if bars is None or bars.empty or "close" not in bars.columns or "date" not in bars.columns:
        return None
    clipped = bars[bars["date"] <= asof]
    if len(clipped) < 2:
        return None
    tail2 = clipped.sort_values("date").tail(2)
    prev_close = float(tail2["close"].iloc[0])
    today_close = float(tail2["close"].iloc[1])
    if prev_close <= 0:
        return None
    return prev_close, today_close


def daily_gainers(
    data_dir: Path,
    *,
    asof: str,
    master: pd.DataFrame | None = None,
    market: str | None = None,
    min_pct: float = 0.01,
) -> pd.DataFrame:
    """asof 기준 일일 상승 종목.

    Parameters
    ----------
    data_dir : store 루트.
    asof : YYYYMMDD. 이 날짜 *이하* 마지막 2봉으로 비교 (재현성).
    master : universe 마스터 DataFrame. None이면 store/meta/master.parquet 로드.
        columns에 ['ticker', 'market', 'name'] 필요.
    market : 'KOSPI'/'KOSDAQ'/'US' 등으로 필터. None이면 전체.
    min_pct : 컷오프. 기본 0.01 (+1%). 0 이면 모든 상승 (+0.0001 이상).

    Returns
    -------
    DataFrame with columns ['ticker', 'market', 'name', 'close', 'prev_close', 'pct_chg']
        pct_chg desc 정렬.
    """
    if master is None:
        m = store.read_parquet_safe(store.master_path(data_dir))
        if m is None or m.empty:
            return pd.DataFrame(columns=["ticker", "market", "name", "close", "prev_close", "pct_chg"])
        master = m

    if market is not None:
        master = master[master["market"] == market]

    if "name" not in master.columns:
        master = master.assign(name="")

    rows: list[GainerRow] = []
    for tk, mkt, nm in zip(master["ticker"], master["market"], master["name"], strict=False):
        bars = store.read_parquet_safe(store.bars_path(data_dir, tk))
        twoc = _last_two_closes(bars, asof)
        if twoc is None:
            continue
        prev_close, today_close = twoc
        pct_chg = (today_close - prev_close) / prev_close
        if pct_chg < min_pct:
            continue
        rows.append(GainerRow(
            ticker=tk, market=mkt, name=nm or "",
            close=today_close, prev_close=prev_close, pct_chg=pct_chg,
        ))

    if not rows:
        return pd.DataFrame(columns=["ticker", "market", "name", "close", "prev_close", "pct_chg"])

    df = pd.DataFrame([r.__dict__ for r in rows])
    return df.sort_values("pct_chg", ascending=False).reset_index(drop=True)


def attach_stage(
    gainers_df: pd.DataFrame,
    data_dir: Path,
    *,
    asof: str,
    stage_params: stg.StageParams | None = None,
) -> pd.DataFrame:
    """gainers_df의 각 ticker에 대해 ``asof`` 시점 Weinstein Stage 계산.

    Stage 분류는 ``stage.classify_stage`` 사용 (SMA150 기준, history-dependent
    상태머신). 충분한 봉이 없는 종목은 stage=0 (UNKNOWN).

    Returns
    -------
    gainers_df + ['stage', 'stage_name'] 컬럼.
    """
    params = stage_params or stg.StageParams()
    needed_bars = params.ma_length + params.slope_lookback

    stages: list[int] = []
    for tk in gainers_df["ticker"]:
        bars = store.read_parquet_safe(store.bars_path(data_dir, tk))
        if bars is None or bars.empty:
            stages.append(stg.STAGE_UNKNOWN)
            continue
        clipped = bars[bars["date"] <= asof].sort_values("date")
        if len(clipped) < needed_bars:
            stages.append(stg.STAGE_UNKNOWN)
            continue
        close = clipped["close"].astype(float).reset_index(drop=True)
        stages.append(int(stg.classify_stage(close, params)))

    out = gainers_df.copy()
    out["stage"] = stages
    out["stage_name"] = out["stage"].map(stg.STAGE_NAMES)
    return out


def stage_distribution(stage_series: pd.Series) -> pd.DataFrame:
    """Stage 시리즈를 요약 분포로 변환.

    Returns
    -------
    DataFrame  columns=['stage', 'stage_name', 'count', 'pct']
        stage 0~4 모두 포함 (0건이어도 행 유지).
    """
    counts = stage_series.value_counts()
    total = max(int(counts.sum()), 1)
    rows = []
    for stage_code in sorted(stg.STAGE_NAMES.keys()):
        c = int(counts.get(stage_code, 0))
        rows.append({
            "stage": stage_code,
            "stage_name": stg.STAGE_NAMES[stage_code],
            "count": c,
            "pct": c / total * 100,
        })
    return pd.DataFrame(rows)
