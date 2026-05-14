"""Minervini Trend Template + RS rank 게이트.

ARCHITECTURE.md §5. 8조건 + (선택) growth 오버레이.

8조건:
  1. close > sma150 and close > sma200
  2. sma150 > sma200
  3. sma200 slope (100d normalized) > 0
  4. sma50 > sma150 > sma200
  5. close > sma50
  6. close >= low_52w * 1.30
  7. close >= high_52w * 0.75
  8. rs_rank >= 70 (시장별 횡단면 백분위)
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
    sma200_slope: float
    high_52w: float
    low_52w: float
    rs_rank: float


N_CONDITIONS = 8


def check_template(
    close_series: pd.Series,
    rs_rank_value: float,
    *,
    sma200_slope_lookback: int = 100,
    weeks_52: int = 252,
    rs_rank_min: float = 70.0,
    low_recovery_pct: float = 30.0,
    high_proximity_pct: float = 25.0,
) -> TemplateResult:
    """8 조건 + RS rank 체크. 단일 시점(마지막 봉) 기준 통과 여부.

    Parameters
    ----------
    close_series : pd.Series  date-ordered close prices. 최소 252+slope_lookback 봉 필요.
    rs_rank_value : float  사전 계산된 RS rank (0~100). NaN이면 조건 8 자동 fail.
    sma200_slope_lookback : 200 SMA의 정규화 기울기 lookback (기본 100d = ~5개월).
    weeks_52 : 52주 = 252 영업일.
    rs_rank_min : 조건 8 임계.
    low_recovery_pct / high_proximity_pct : 조건 6/7 임계.

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
    sma200_slope = ind.slope_normalized(sma200, sma200_slope_lookback)
    hi52 = ind.rolling_high(close, weeks_52)   # 52주 최고 종가
    lo52 = ind.rolling_low(close, weeks_52)    # 52주 최저 종가

    c = float(close.iloc[-1])
    s50 = float(sma50.iloc[-1]) if pd.notna(sma50.iloc[-1]) else float("nan")
    s150 = float(sma150.iloc[-1]) if pd.notna(sma150.iloc[-1]) else float("nan")
    s200 = float(sma200.iloc[-1]) if pd.notna(sma200.iloc[-1]) else float("nan")
    s200_slope = float(sma200_slope.iloc[-1]) if pd.notna(sma200_slope.iloc[-1]) else float("nan")
    h52 = float(hi52.iloc[-1]) if pd.notna(hi52.iloc[-1]) else float("nan")
    l52 = float(lo52.iloc[-1]) if pd.notna(lo52.iloc[-1]) else float("nan")

    # 핵심 값에 NaN이 있으면 fail (data quality 문제)
    if any(np.isnan(x) for x in (s50, s150, s200, s200_slope, h52, l52)):
        return _nan_result(rs_rank_value)

    # 조건 1~8
    c1 = c > s150 and c > s200
    c2 = s150 > s200
    c3 = s200_slope > 0
    c4 = s50 > s150 and s150 > s200
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
        sma200_slope=s200_slope,
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
        sma200_slope=float("nan"),
        high_52w=float("nan"),
        low_52w=float("nan"),
        rs_rank=float(rs_rank_value) if pd.notna(rs_rank_value) else float("nan"),
    )
