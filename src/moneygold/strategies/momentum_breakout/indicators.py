"""Momentum Breakout pure indicators.

원칙 (CLAUDE.md):
  - pd.Series → pd.Series (또는 스칼라).
  - 외부 호출 없음, 시간 의존 없음, state 없음.
  - NaN은 자연스럽게 전파 (lookback 미달 구간 등).
  - 인덱스 보존.

"신고가" 정의:
  - rolling_new_high(s, n) = 직전 n봉 *포함* 최고값. 오늘 포함 X.
    즉 ``s.shift(1).rolling(n).max()`` — 오늘 종가가 어제까지의 n일 신고를
    초과했는지를 단순 비교로 판정 가능하게.
  - "60일 신고가 돌파"는 ``s.iloc[-1] > rolling_new_high(s, 60).iloc[-1]``.

"Fresh breakout":
  - 직전 ``fresh_window`` 일 동안 위 돌파가 한 번도 없었어야 함.
  - 구현: 직전 fresh_window 봉 중 어떤 t에서도 close[t]가 그 시점의 (t-1까지의)
    new_high를 초과하지 않았어야 함. 즉 ``broken[t] = close[t] > new_high[t]`` 시리즈에서
    오늘 외의 직전 fresh_window 봉 모두 False.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rolling_new_high(s: pd.Series, n: int) -> pd.Series:
    """직전 n봉의 최고값 (*오늘 제외*).

    오늘 종가 vs 직전 n일 최고 비교에 바로 쓰기 위해 shift(1) 적용.
    n 미만 구간(앞부분 + 마지막 shift 한 칸)은 NaN.

    Parameters
    ----------
    s : pd.Series  보통 close 또는 high.
    n : 룩백 봉 수 (>=1).

    Returns
    -------
    pd.Series  index 보존, dtype float.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    return s.shift(1).rolling(window=n, min_periods=n).max()


def breakout_mask(close: pd.Series, lookback: int) -> pd.Series:
    """각 t 시점에서 close[t]가 (t-1까지 lookback일) 신고가를 초과했는가.

    Returns
    -------
    pd.Series[bool]  index 보존. lookback 미달 구간은 False.
    """
    nh = rolling_new_high(close, lookback)
    return (close > nh).fillna(False)


def is_fresh_breakout(
    close: pd.Series,
    lookback: int,
    fresh_window: int,
) -> bool:
    """마지막 봉이 "fresh" N일 신고가 돌파인가.

    True 조건:
      1. close[-1] > max(close[-lookback-1 : -1])    (오늘이 N일 신고가 돌파)
      2. 직전 fresh_window 봉 (오늘 직전 봉 ~ 그 fresh_window-1봉 전 사이) 중
         단 한 봉도 같은 의미의 돌파 (close[t] > max(close[t-lookback : t]))가 없음.

    데이터가 lookback + fresh_window 봉 미만이면 False.

    이 함수는 *불리언 스칼라*만 반환 (단일 시점 판정용).
    """
    if lookback < 1 or fresh_window < 1:
        raise ValueError("lookback, fresh_window must be >= 1")
    n = len(close)
    if n < lookback + fresh_window + 1:
        return False

    mask = breakout_mask(close, lookback)
    # 오늘 돌파해야 함
    if not bool(mask.iloc[-1]):
        return False
    # 직전 fresh_window 봉 (오늘 제외) 모두 돌파 *아니어야* 함
    recent = mask.iloc[-1 - fresh_window : -1]
    return not bool(recent.any())


def volume_spike(
    value_series: pd.Series,
    n: int,
    ratio: float,
) -> tuple[bool, float]:
    """오늘 거래대금이 직전 n일 평균의 ratio배 이상인가.

    Parameters
    ----------
    value_series : 거래대금 시계열 (KRW). bars['value'] 그대로.
    n : 평균 윈도우 (오늘 *제외* 직전 n봉).
    ratio : 임계 배수.

    Returns
    -------
    (passed: bool, actual_ratio: float)
        actual_ratio = today / avg_prev_n. 데이터 부족 또는 avg<=0이면 (False, NaN).
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if len(value_series) < n + 1:
        return False, float("nan")
    today = float(value_series.iloc[-1])
    prev = value_series.iloc[-1 - n : -1].astype(float)
    avg = float(prev.mean())
    if not np.isfinite(avg) or avg <= 0:
        return False, float("nan")
    r = today / avg
    return (r >= ratio), r


def ma20(close: pd.Series, n: int = 20) -> pd.Series:
    """단순 이동평균 (TRAILING stop 갱신용). 기본 20봉.

    indicators.sma와 동일하지만 strategy 모듈 자가완결성을 위해 별도 노출.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    return close.rolling(window=n, min_periods=n).mean()
