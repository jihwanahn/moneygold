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


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI (Relative Strength Index).

    RSI = 100 - 100 / (1 + RS) where RS = mean(gain_n) / mean(loss_n)
    using Wilder smoothing (EWMA with alpha = 1/n, equivalent to SMA-seed +
    EMA), 0~100 범위. <30 = oversold, >70 = overbought 가 일반 해석.
    """
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    # avg_loss=0인 경우 rs=inf → out=100, NaN 처리
    out = out.where(avg_loss > 0, 100.0).where(avg_gain.notna() & avg_loss.notna(), float("nan"))
    return out


def bollinger_position(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    """Bollinger Bands 내 종가 위치를 [0, 1]로 정규화.

    bb_pos = (close - lower) / (upper - lower)
    where upper = SMA(n) + k*std(n), lower = SMA(n) - k*std(n).

    0 = 하단 밴드, 0.5 = 중심선(SMA), 1 = 상단 밴드. >1/<0이면 밴드 이탈.
    std=0 (가격 평탄)이면 NaN.
    """
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")
    mid = close.rolling(window=n, min_periods=n).mean()
    std = close.rolling(window=n, min_periods=n).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    width = upper - lower
    out = (close - lower) / width
    out = out.where(width > 0, float("nan"))
    return out


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
    """RS line = (stock/index) 시계열, 시작점을 100으로 정규화.

    시계열 추세 분석용 (Stage 분류기의 RS slope 계산 등). **횡단면 비교용 아님** —
    종목간 RS rank는 `rs_momentum` + `rs_rank` 조합 사용.

    공통 인덱스(날짜)만 사용. 시작점이 NaN/0이면 정규화 못해서 빈 Series.
    """
    df = pd.concat([stock_close.rename("s"), index_close.rename("i")], axis=1, join="inner").dropna()
    if df.empty:
        return pd.Series(dtype=float)
    ratio = df["s"] / df["i"]
    base = ratio.iloc[0]
    if base == 0 or pd.isna(base):
        return pd.Series(dtype=float)
    return ratio / base * 100.0


# IBD 표준 weighting: 직전 1Q 40%, 2Q 20%, 3Q 20%, 4Q 20%
RS_MOMENTUM_PERIODS = (63, 126, 189, 252)
RS_MOMENTUM_WEIGHTS = (0.40, 0.20, 0.20, 0.20)


def rs_momentum(
    close: pd.Series,
    periods: tuple[int, ...] = RS_MOMENTUM_PERIODS,
    weights: tuple[float, ...] = RS_MOMENTUM_WEIGHTS,
) -> float:
    """IBD-style 가중 모멘텀 (스칼라 1개).

    각 period에 대해 last_close / close[-period-1] - 1 계산 후 weights로 가중합.
    데이터 부족(< max(periods)+1)이면 NaN.

    이 값을 종목별로 모아 `rs_rank`에 넣으면 진짜 IBD-style RS rating이 나옴.
    """
    if len(periods) != len(weights):
        raise ValueError("periods와 weights 길이 불일치")
    n = len(close)
    if n < max(periods) + 1:
        return float("nan")
    last = float(close.iloc[-1])
    if last == 0 or pd.isna(last):
        return float("nan")
    weighted = 0.0
    for p, w in zip(periods, weights, strict=False):
        prev = float(close.iloc[-p - 1])
        if prev == 0 or pd.isna(prev):
            return float("nan")
        weighted += w * (last / prev - 1.0)
    return weighted


def rs_rank(scores: pd.Series) -> pd.Series:
    """횡단면 백분위 0~100.

    입력: 종목별 `rs_momentum` 결과 (Series, index=ticker, values=수익률 스칼라).
    출력: 같은 인덱스의 백분위 (높을수록 강세).
    """
    if scores.empty:
        return pd.Series(dtype=float)
    return scores.rank(pct=True, na_option="keep") * 100.0


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
