"""Weinstein Stage Analysis 4단계 분류기.

ARCHITECTURE.md §4. TradingView 일반 구현 + 책 "Secrets for Profiting in Bull and
Bear Markets" (Stan Weinstein) 원전과 일치하는 history-dependent 상태머신.

핵심:
  - MA 30주(=일봉 150) + slope (20봉 변화율 정규화)
  - Slope threshold (|r| > 0.1%) — 그 이하는 flat
  - Price band (MA ±3%) — 그 안은 위/아래 미판정
  - **RS는 stage 판정에 들어가지 않음** (Minervini Template의 조건 8로 별도)
  - 상태 전이가 *이전 상태*에 의존:
      Stage 2 (Advance)  → slope > 0 AND close > ma + band
      Stage 4 (Decline)  → slope < 0 AND close < ma - band
      Stage 3 (Top)      → flat AND 직전이 Stage 2
      Stage 1 (Base)     → flat AND 직전이 Stage 1/3/4/초기

상태머신이라 단일 시점만 보고는 Stage 1/3 구분 불가. 시계열 1회 통과 후 마지막 값.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind

# 상태 상수
STAGE_UNKNOWN = 0
STAGE_BASING = 1
STAGE_ADVANCING = 2
STAGE_TOPPING = 3
STAGE_DECLINING = 4

STAGE_NAMES = {
    0: "UNKNOWN",
    1: "BASING",
    2: "ADVANCING",
    3: "TOPPING",
    4: "DECLINING",
}


@dataclass(frozen=True)
class StageParams:
    """Stage 분류 파라미터. 일봉 기준 디폴트는 TradingView Weinstein과 동일."""
    ma_length: int = 150           # 30주 = 일봉 150
    slope_lookback: int = 20       # 20봉 (=4주)
    slope_threshold_pct: float = 0.001    # 0.1% — flat 판정
    band_pct: float = 0.03          # MA 근처 ±3%는 위/아래 미판정
    ma_type: str = "SMA"            # SMA | EMA


def classify_stage_series(close: pd.Series, params: StageParams | None = None) -> pd.Series:
    """시계열 Stage 분류. History-dependent 상태머신.

    Returns
    -------
    pd.Series  index=close.index, dtype=int8. 값 0(UNKNOWN)/1/2/3/4.
    """
    p = params or StageParams()
    if p.ma_type == "EMA":
        ma = ind.ema(close, p.ma_length)
    else:
        ma = ind.sma(close, p.ma_length)

    ma_prev = ma.shift(p.slope_lookback)
    r = (ma - ma_prev) / ma                     # 정규화 변화율
    above = close > ma * (1.0 + p.band_pct)
    below = close < ma * (1.0 - p.band_pct)
    abs_r_flat = r.abs() <= p.slope_threshold_pct

    n = len(close)
    stages = np.zeros(n, dtype=np.int8)         # 0 = UNKNOWN
    prev = STAGE_BASING

    for i in range(n):
        if pd.isna(ma.iloc[i]) or pd.isna(ma_prev.iloc[i]):
            stages[i] = STAGE_UNKNOWN
            continue

        r_i = float(r.iloc[i])
        above_i = bool(above.iloc[i])
        below_i = bool(below.iloc[i])
        flat_i = bool(abs_r_flat.iloc[i])

        if (r_i > p.slope_threshold_pct) and above_i:
            st = STAGE_ADVANCING
        elif (r_i < -p.slope_threshold_pct) and below_i:
            st = STAGE_DECLINING
        elif flat_i:
            # Stage 2 → flat → Stage 3 → flat 지속 → Stage 3 유지
            # Stage 4 → flat → Stage 1 → flat 지속 → Stage 1 유지
            if prev in (STAGE_ADVANCING, STAGE_TOPPING):
                st = STAGE_TOPPING
            else:
                st = STAGE_BASING
        else:
            # 추세는 있으나 가격이 band 안 — 직전 상태 유지
            st = prev

        stages[i] = st
        prev = st

    return pd.Series(stages, index=close.index, dtype="int8")


def classify_stage(close: pd.Series, params: StageParams | None = None) -> int:
    """단일 시점 Stage = 시계열의 마지막 값.

    상태머신이라 history가 필요하므로 시계열 전체를 1회 계산.
    빈 시리즈이거나 워밍업 미완료면 UNKNOWN.
    """
    if close.empty:
        return STAGE_UNKNOWN
    series = classify_stage_series(close, params)
    if series.empty:
        return STAGE_UNKNOWN
    return int(series.iloc[-1])


def stage_since(stage_series: pd.Series, target: int = STAGE_ADVANCING) -> str | None:
    """가장 최근의 *연속* target Stage 구간이 언제 시작됐는지 (index 값 반환).

    target Stage가 마지막 시점에 활성이 아니면 None.
    """
    if stage_series.empty or stage_series.iloc[-1] != target:
        return None
    idx = len(stage_series) - 1
    while idx > 0 and stage_series.iloc[idx - 1] == target:
        idx -= 1
    return str(stage_series.index[idx])
