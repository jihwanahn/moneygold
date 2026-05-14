"""Minervini Trend Template + RS rank 게이트.

ARCHITECTURE.md §5. 8조건 + (선택) growth 오버레이.

8조건 (TradingView 일반 구현과 일치, 책 "Trade Like a Stock Market Wizard" 원전 기반):
  1. close > sma150 AND close > sma200
  2. sma150 > sma200
  3. sma200 > sma200[N봉 전] (기본 22봉 = 1개월 우상향)
  4. sma50 > sma150 AND sma50 > sma200
  5. close > sma50
  6. close >= lowest(low, 260) × (1 + low_recovery_pct/100), 기본 25%
  7. close >= highest(high, 260) × (1 - high_proximity_pct/100), 기본 25%
  8. rs_rank >= 70 (IBD-style 4Q 가중 수익률의 시장 내 횡단면 백분위)

조건 6/7은 *고가/저가* 기준 — TradingView·미네비니 원전과 동일.
조건 8 RS는 *횡단면 백분위* — 책의 "IBD RS ranking" 원전 의도.

bars DataFrame이 high/low 컬럼 포함하면 6/7는 그것 사용,
그렇지 않으면 close 기반으로 fallback (구버전 호환).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass(frozen=True)
class TemplateResult:
    passed: bool
    checks: tuple[bool, ...]   # 길이 8, 각 조건 통과 여부
    # 진단용 raw 값
    close: float
    sma50: float
    sma150: float
    sma200: float
    sma200_prev: float          # sma200_lookback봉 전 값 (조건 3)
    high_52w: float
    low_52w: float
    rs_rank: float


N_CONDITIONS = 8


def check_template(
    close_series: pd.Series,
    rs_rank_value: float,
    *,
    sma200_slope_lookback: int = 22,
    weeks_52: int = 260,
    rs_rank_min: float = 70.0,
    low_recovery_pct: float = 25.0,
    high_proximity_pct: float = 25.0,
    high_series: pd.Series | None = None,
    low_series: pd.Series | None = None,
) -> TemplateResult:
    """8 조건 + RS rank 체크. 단일 시점(마지막 봉) 기준 통과 여부.

    Parameters
    ----------
    close_series : pd.Series  date-ordered close prices.
    rs_rank_value : float  사전 계산된 RS rank (0~100). NaN이면 조건 8 자동 fail.
    sma200_slope_lookback : sma200의 우상향 판정용 lookback일.
        TradingView 미네비니 코드는 22일(1개월). 책엔 "preferably 4-5 months"라
        100일도 가능하나 1개월이 일반 구현. 기본 22.
    weeks_52 : 52주 lookback (TV 기본 260, 책 명시는 없음).
    rs_rank_min : 조건 8 임계.
    low_recovery_pct : 조건 6 임계 (저점 대비 %). 책 30, TV 25. 기본 25.
    high_proximity_pct : 조건 7 임계 (고점까지 %). 둘 다 25.
    high_series / low_series : 52주 고/저 계산용 고가/저가 시리즈. None이면 close fallback.

    Returns
    -------
    TemplateResult  passed = all(checks).
    """
    close = close_series.astype(float)
    n = len(close)

    # 데이터 부족이면 NaN result
    min_required = max(200 + sma200_slope_lookback, weeks_52)
    if n < min_required:
        return _nan_result(rs_rank_value)

    sma50 = ind.sma(close, 50)
    sma150 = ind.sma(close, 150)
    sma200 = ind.sma(close, 200)

    # 조건 3: sma200_today > sma200_{lookback}봉 전
    if len(sma200) <= sma200_slope_lookback:
        return _nan_result(rs_rank_value)
    sma200_prev_val = float(sma200.iloc[-1 - sma200_slope_lookback]) if pd.notna(sma200.iloc[-1 - sma200_slope_lookback]) else float("nan")

    # 조건 6/7: 고가/저가 기반 (있으면) 또는 close fallback
    high_for_52 = high_series.astype(float) if high_series is not None else close
    low_for_52 = low_series.astype(float) if low_series is not None else close
    hi52 = ind.rolling_high(high_for_52, weeks_52)
    lo52 = ind.rolling_low(low_for_52, weeks_52)

    c = float(close.iloc[-1])
    s50 = float(sma50.iloc[-1]) if pd.notna(sma50.iloc[-1]) else float("nan")
    s150 = float(sma150.iloc[-1]) if pd.notna(sma150.iloc[-1]) else float("nan")
    s200 = float(sma200.iloc[-1]) if pd.notna(sma200.iloc[-1]) else float("nan")
    h52 = float(hi52.iloc[-1]) if pd.notna(hi52.iloc[-1]) else float("nan")
    l52 = float(lo52.iloc[-1]) if pd.notna(lo52.iloc[-1]) else float("nan")

    # 핵심 값에 NaN이 있으면 fail (data quality 문제)
    if any(np.isnan(x) for x in (s50, s150, s200, sma200_prev_val, h52, l52)):
        return _nan_result(rs_rank_value)

    # 조건 1~8
    c1 = c > s150 and c > s200
    c2 = s150 > s200
    c3 = s200 > sma200_prev_val
    c4 = s50 > s150 and s50 > s200
    c5 = c > s50
    c6 = c >= l52 * (1.0 + low_recovery_pct / 100.0)
    c7 = c >= h52 * (1.0 - high_proximity_pct / 100.0)
    c8 = pd.notna(rs_rank_value) and float(rs_rank_value) >= rs_rank_min

    checks = (c1, c2, c3, c4, c5, c6, c7, c8)
    return TemplateResult(
        passed=all(checks),
        checks=checks,
        close=c,
        sma50=s50,
        sma150=s150,
        sma200=s200,
        sma200_prev=sma200_prev_val,
        high_52w=h52,
        low_52w=l52,
        rs_rank=float(rs_rank_value) if pd.notna(rs_rank_value) else float("nan"),
    )


def _nan_result(rs_rank_value: float) -> TemplateResult:
    return TemplateResult(
        passed=False,
        checks=(False,) * N_CONDITIONS,
        close=float("nan"),
        sma50=float("nan"),
        sma150=float("nan"),
        sma200=float("nan"),
        sma200_prev=float("nan"),
        high_52w=float("nan"),
        low_52w=float("nan"),
        rs_rank=float(rs_rank_value) if pd.notna(rs_rank_value) else float("nan"),
    )
