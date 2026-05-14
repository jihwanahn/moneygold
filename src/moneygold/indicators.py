"""기술적 지표 — pure functions.

원칙:
  - 모든 함수는 pd.Series → pd.Series (또는 스칼라 → 스칼라).
  - 외부 호출 / 시간 의존 / state 없음.
  - NaN은 자연스럽게 전파 (lookback 미달 구간 등).
  - 입력 인덱스(날짜)는 보존.

ARCHITECTURE.md §3 참조.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ============================================================
# Moving averages
# ============================================================

def sma(close: pd.Series, n: int) -> pd.Series:
    """단순이동평균. 마지막 n개 평균. n 미만 구간은 NaN."""
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")
    return close.rolling(window=n, min_periods=n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    """지수이동평균. span=n 기준."""
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")
    return close.ewm(span=n, adjust=False, min_periods=n).mean()


# ============================================================
# Range / volatility
# ============================================================

def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 20) -> pd.Series:
    """Wilder Average True Range. alpha = 1/n EMA on TR."""
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rolling_high(high: pd.Series, n: int) -> pd.Series:
    """롤링 윈도우 내 최고가."""
    return high.rolling(window=n, min_periods=n).max()


def rolling_low(low: pd.Series, n: int) -> pd.Series:
    """롤링 윈도우 내 최저가."""
    return low.rolling(window=n, min_periods=n).min()


# ============================================================
# Slope (정규화 선형회귀 기울기)
# ============================================================

def _slope_normalized_window(arr: np.ndarray) -> float:
    """단일 윈도우의 정규화 기울기. NaN 또는 mean=0이면 NaN."""
    if len(arr) < 2 or np.isnan(arr).any():
        return np.nan
    mean_y = float(np.mean(arr))
    if mean_y == 0.0:
        return np.nan
    x = np.arange(len(arr), dtype=float)
    # OLS slope = cov(x,y) / var(x). polyfit은 작은 윈도우에서 충분히 빠름.
    slope, _ = np.polyfit(x, arr, 1)
    return float(slope) / mean_y


def slope_normalized(series: pd.Series, lookback: int) -> pd.Series:
    """롤링 lookback 봉의 OLS 기울기를 평균값으로 정규화. 단위: per-bar fraction.

    값의 부호로 우상향/우하향, 크기로 추세 강도 비교.
    """
    if lookback < 2:
        raise ValueError(f"lookback must be >= 2, got {lookback}")
    return series.rolling(window=lookback, min_periods=lookback).apply(
        _slope_normalized_window, raw=True
    )


# ============================================================
# Relative Strength
# ============================================================

def rs_line(stock_close: pd.Series, index_close: pd.Series) -> pd.Series:
    """RS line = stock / index * 100. 두 시리즈의 인덱스(date) 정렬 후 계산.

    공통 인덱스만 사용. NaN 일자는 결과에서 제외.
    """
    df = pd.concat([stock_close.rename("s"), index_close.rename("i")], axis=1, join="inner").dropna()
    if df.empty:
        return pd.Series(dtype=float)
    return (df["s"] / df["i"]) * 100.0


def rs_rank(rs_lines_today: pd.Series) -> pd.Series:
    """횡단면 백분위 0~100.

    입력: 같은 날짜의 종목별 RS line 값(Series, index=ticker, values=RS line scalar).
    출력: 같은 인덱스의 백분위 (높을수록 강세).
    """
    if rs_lines_today.empty:
        return pd.Series(dtype=float)
    return rs_lines_today.rank(pct=True, na_option="keep") * 100.0


# ============================================================
# Volume
# ============================================================

def volume_ratio(vol: pd.Series, n: int = 50) -> pd.Series:
    """오늘 거래량 / n일 평균 거래량. NaN/0으로 나누는 경우 inf → NaN."""
    v = vol.astype(float)
    avg = sma(v, n)
    out = v / avg
    return out.replace([np.inf, -np.inf], np.nan)


# ============================================================
# Misc
# ============================================================

def dist_pct(a: pd.Series | float, b: pd.Series | float) -> pd.Series | float:
    """(a - b) / b * 100. b가 0이면 NaN."""
    if isinstance(b, pd.Series):
        return (a - b) / b.replace(0, np.nan) * 100.0
    if b == 0:
        return float("nan")
    return (a - b) / b * 100.0
