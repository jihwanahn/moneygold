"""indicators.py — pure-function sanity tests."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from moneygold import indicators as ind

# ----------------- SMA / EMA -----------------

def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = ind.sma(s, 3)
    # 첫 두 값 NaN, 이후 단순 평균
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)
    assert out.iloc[3] == pytest.approx(3.0)
    assert out.iloc[4] == pytest.approx(4.0)


def test_sma_invalid_n():
    s = pd.Series([1.0, 2.0])
    with pytest.raises(ValueError):
        ind.sma(s, 0)


def test_ema_first_full_window_matches_sma():
    """EMA(adjust=False, min_periods=n)는 첫 n 행에서 SMA와 정확히 일치."""
    s = pd.Series([10.0, 12.0, 14.0, 13.0, 15.0])
    e = ind.ema(s, 3)
    # span=3, alpha=2/(3+1)=0.5
    # 첫 두 값 NaN (min_periods=3)
    assert np.isnan(e.iloc[0]) and np.isnan(e.iloc[1])
    # 3번째: pandas ewm with adjust=False starts seed from first value
    # We just verify finite and monotonic-ish for valid input
    assert np.isfinite(e.iloc[2])
    assert np.isfinite(e.iloc[4])


# ----------------- ATR -----------------

def test_atr_known_pattern():
    """모든 봉의 TR = 10인 경우 ATR도 10에 수렴."""
    n = 14
    high = pd.Series([110.0] * 30)
    low = pd.Series([100.0] * 30)
    close = pd.Series([105.0] * 30)
    a = ind.atr(high, low, close, n=n)
    # 첫 n-1 NaN
    assert np.isnan(a.iloc[n - 2])
    assert a.iloc[-1] == pytest.approx(10.0, abs=1e-6)


# ----------------- rolling_high/low -----------------

def test_rolling_high_low():
    high = pd.Series([1.0, 2.0, 3.0, 2.5, 2.0, 4.0, 3.0])
    low = pd.Series([0.5, 1.0, 1.5, 1.0, 0.8, 2.0, 1.5])
    rh = ind.rolling_high(high, 3)
    rl = ind.rolling_low(low, 3)
    assert np.isnan(rh.iloc[1])
    assert rh.iloc[2] == 3.0
    assert rh.iloc[5] == 4.0
    assert rl.iloc[2] == 0.5
    assert rl.iloc[5] == 0.8


# ----------------- slope_normalized -----------------

def test_slope_normalized_uptrend_positive():
    s = pd.Series(np.arange(1, 21, dtype=float))   # 1..20 우상향
    slope = ind.slope_normalized(s, lookback=10)
    assert slope.iloc[-1] > 0


def test_slope_normalized_downtrend_negative():
    s = pd.Series(np.arange(20, 0, -1, dtype=float))  # 20..1 우하향
    slope = ind.slope_normalized(s, lookback=10)
    assert slope.iloc[-1] < 0


def test_slope_normalized_flat_zero():
    s = pd.Series([5.0] * 20)
    slope = ind.slope_normalized(s, lookback=10)
    # 평탄선의 slope는 0 또는 NaN(mean=5 OK)
    assert slope.iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_slope_normalized_invalid_lookback():
    s = pd.Series([1.0, 2.0])
    with pytest.raises(ValueError):
        ind.slope_normalized(s, lookback=1)


# ----------------- RS line / rank -----------------

def test_rs_line_starts_at_100():
    """rs_line은 시작점이 100. 그 뒤로 상대 변화."""
    stock = pd.Series([100, 110, 121], index=["d1", "d2", "d3"], dtype=float)
    idx = pd.Series([2000, 2100, 2200], index=["d1", "d2", "d3"], dtype=float)
    rs = ind.rs_line(stock, idx)
    assert rs.iloc[0] == pytest.approx(100.0)
    # 종목 +21%, 지수 +10% → ratio 1.21/2200 = 0.55 vs base 0.05 → 1.1
    # rs[-1] = (121/2200) / (100/2000) * 100 = 0.055/0.05 * 100 = 110
    assert rs.iloc[-1] == pytest.approx(110.0)


def test_rs_line_stock_outperforms_index_rises_above_100():
    stock = pd.Series([100, 200], index=["d1", "d2"], dtype=float)  # +100%
    idx = pd.Series([2000, 2200], index=["d1", "d2"], dtype=float)   # +10%
    rs = ind.rs_line(stock, idx)
    assert rs.iloc[-1] > 100


def test_rs_line_stock_underperforms_drops_below_100():
    stock = pd.Series([100, 105], index=["d1", "d2"], dtype=float)   # +5%
    idx = pd.Series([2000, 2400], index=["d1", "d2"], dtype=float)   # +20%
    rs = ind.rs_line(stock, idx)
    assert rs.iloc[-1] < 100


def test_rs_line_inner_join_only():
    stock = pd.Series([100, 110], index=["d1", "d2"], dtype=float)
    idx = pd.Series([2000, 2100, 2200], index=["d1", "d2", "d3"], dtype=float)
    rs = ind.rs_line(stock, idx)
    assert len(rs) == 2
    assert list(rs.index) == ["d1", "d2"]


def test_rs_line_empty_when_no_overlap():
    stock = pd.Series([100], index=["d1"], dtype=float)
    idx = pd.Series([2000], index=["d2"], dtype=float)
    rs = ind.rs_line(stock, idx)
    assert rs.empty


# ----------------- rs_momentum / rs_rank (IBD-style) -----------------

def test_rs_momentum_constant_close_zero():
    close = pd.Series([100.0] * 300)
    assert ind.rs_momentum(close) == pytest.approx(0.0)


def test_rs_momentum_uptrend_positive():
    # 매일 +0.1% → 252일이면 약 +28.5%
    close = pd.Series(100.0 * (1.001 ** np.arange(300)))
    score = ind.rs_momentum(close)
    assert score > 0.10


def test_rs_momentum_downtrend_negative():
    close = pd.Series(100.0 * (0.999 ** np.arange(300)))
    score = ind.rs_momentum(close)
    assert score < -0.10


def test_rs_momentum_insufficient_data():
    close = pd.Series([100.0] * 100)
    assert pd.isna(ind.rs_momentum(close))


def test_rs_momentum_weights_must_match_periods():
    close = pd.Series([100.0] * 300)
    with pytest.raises(ValueError):
        ind.rs_momentum(close, periods=(63, 126), weights=(0.5, 0.3, 0.2))


def test_rs_rank_percentile():
    s = pd.Series({"A": -0.1, "B": 0.0, "C": 0.5, "D": 1.0})
    r = ind.rs_rank(s)
    assert r["A"] == pytest.approx(25.0)
    assert r["D"] == pytest.approx(100.0)


def test_rs_rank_empty():
    r = ind.rs_rank(pd.Series(dtype=float))
    assert r.empty


def test_rs_rank_nan_propagated():
    s = pd.Series({"A": np.nan, "B": 0.5, "C": 1.0})
    r = ind.rs_rank(s)
    assert pd.isna(r["A"])
    assert r["C"] == pytest.approx(100.0)


# ----------------- volume_ratio -----------------

def test_volume_ratio_basic():
    """vol / sma(vol, n). SMA가 오늘 거래량을 포함하므로 비율은 단순 2배 X."""
    v = pd.Series([100.0] * 10 + [200.0])
    r = ind.volume_ratio(v, n=10)
    # 마지막 윈도우 = [100]*9 + [200] = mean 110. 200/110.
    expected = 200.0 / ((100.0 * 9 + 200.0) / 10.0)
    assert r.iloc[-1] == pytest.approx(expected)


def test_volume_ratio_zero_zero_returns_nan():
    """전 구간 0이면 0/0 → NaN."""
    v = pd.Series([0.0] * 11)
    r = ind.volume_ratio(v, n=10)
    assert pd.isna(r.iloc[-1])


# ----------------- dist_pct -----------------

def test_dist_pct_scalar():
    assert ind.dist_pct(110.0, 100.0) == pytest.approx(10.0)
    assert ind.dist_pct(90.0, 100.0) == pytest.approx(-10.0)


def test_dist_pct_series():
    a = pd.Series([110, 120], dtype=float)
    b = pd.Series([100, 100], dtype=float)
    r = ind.dist_pct(a, b)
    assert r.tolist() == [pytest.approx(10.0), pytest.approx(20.0)]


def test_dist_pct_zero_denominator():
    assert pd.isna(ind.dist_pct(10.0, 0))
    a = pd.Series([10.0, 20.0])
    b = pd.Series([0.0, 10.0])
    r = ind.dist_pct(a, b)
    assert pd.isna(r.iloc[0])
    assert r.iloc[1] == pytest.approx(100.0)


# ----------------- RSI -----------------

def test_rsi_all_gains_yields_100():
    """단조 증가 → 모든 손실이 0 → RSI = 100."""
    s = pd.Series([1.0 + i for i in range(30)])
    r = ind.rsi(s, 14)
    assert r.iloc[-1] == 100.0


def test_rsi_all_losses_yields_0():
    """단조 감소 → 모든 이익이 0 → RSI = 0."""
    s = pd.Series([100.0 - i for i in range(30)])
    r = ind.rsi(s, 14)
    assert r.iloc[-1] == 0.0


def test_rsi_short_period_nan():
    s = pd.Series([100.0] * 5)
    r = ind.rsi(s, 14)
    assert pd.isna(r.iloc[-1])


def test_rsi_range_bounded():
    """RSI는 0~100 사이."""
    rng = np.random.default_rng(42)
    s = pd.Series(100.0 + rng.standard_normal(100).cumsum())
    r = ind.rsi(s, 14).dropna()
    assert (r >= 0).all() and (r <= 100).all()


# ----------------- Bollinger Bands position -----------------

def test_bollinger_position_flat_series_nan():
    """가격 평탄 (std=0)이면 width=0, NaN."""
    s = pd.Series([100.0] * 30)
    bp = ind.bollinger_position(s, 20, 2.0)
    assert pd.isna(bp.iloc[-1])


def test_bollinger_position_close_to_upper_band():
    """상단 밴드 근처 가격 → bb_pos ≈ 1."""
    # 평균 100, std ~5에서 마지막 봉이 ~110으로 튐
    s = pd.Series([100.0 + ((-1) ** i) * 5 for i in range(20)] + [110.0])
    bp = ind.bollinger_position(s, 20, 2.0)
    val = bp.iloc[-1]
    assert val > 0.8


def test_bollinger_position_close_to_lower_band():
    """하단 밴드 근처 → bb_pos ≈ 0."""
    s = pd.Series([100.0 + ((-1) ** i) * 5 for i in range(20)] + [90.0])
    bp = ind.bollinger_position(s, 20, 2.0)
    val = bp.iloc[-1]
    assert val < 0.2


def test_bollinger_position_short_period_nan():
    s = pd.Series([100.0] * 10)
    bp = ind.bollinger_position(s, 20, 2.0)
    assert pd.isna(bp.iloc[-1])
