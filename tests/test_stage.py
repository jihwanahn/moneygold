"""stage.py — Weinstein 분류기 sanity tests."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from moneygold import stage


# ----------------- classify_stage (스칼라) -----------------

def test_stage_2_above_ma_up_rs_up():
    """가격 > 30wMA, MA slope 양수, RS slope 양수 → Stage 2."""
    assert stage.classify_stage(100, 90, 0.001, 0.002) == stage.STAGE_ADVANCING


def test_stage_3_above_ma_flat_rs_down():
    """가격 > 30wMA, MA 평탄/우하향, RS 우하향 → Stage 3."""
    assert stage.classify_stage(100, 90, -0.0001, -0.001) == stage.STAGE_TOPPING
    # ma slope == 0 → not ma_up. 결과는 Stage 3.
    assert stage.classify_stage(100, 90, 0.0, -0.001) == stage.STAGE_TOPPING


def test_stage_4_below_ma_down_rs_down():
    """가격 < 30wMA, MA 우하향, RS 우하향 → Stage 4."""
    assert stage.classify_stage(80, 90, -0.001, -0.002) == stage.STAGE_DECLINING


def test_stage_1_fallthrough_below_ma_up():
    """가격 < 30wMA, MA 우상향 (Stage 4 조건 안 맞음) → Stage 1 fall-through."""
    assert stage.classify_stage(80, 90, 0.001, 0.001) == stage.STAGE_BASING


def test_stage_1_fallthrough_mixed_signals():
    """RS 우상향이지만 가격이 MA 아래면 Stage 2 X → Stage 1."""
    assert stage.classify_stage(80, 90, 0.001, 0.001) == stage.STAGE_BASING


def test_stage_unknown_with_nan():
    """입력 NaN → STAGE_UNKNOWN."""
    assert stage.classify_stage(100, np.nan, 0.001, 0.001) == stage.STAGE_UNKNOWN
    assert stage.classify_stage(np.nan, 90, 0.001, 0.001) == stage.STAGE_UNKNOWN
    assert stage.classify_stage(100, 90, np.nan, 0.001) == stage.STAGE_UNKNOWN
    assert stage.classify_stage(100, 90, 0.001, np.nan) == stage.STAGE_UNKNOWN


# ----------------- classify_stage_series -----------------

def test_stage_series_matches_scalar():
    close = pd.Series([100, 100, 80, 80])
    sma = pd.Series([90, 90, 90, 90])
    ma_slope = pd.Series([0.001, -0.001, -0.001, 0.001])
    rs_slope = pd.Series([0.001, -0.001, -0.001, 0.001])
    out = stage.classify_stage_series(close, sma, ma_slope, rs_slope)
    assert out.tolist() == [
        stage.STAGE_ADVANCING,   # 위, ma up, rs up
        stage.STAGE_TOPPING,     # 위, ma down, rs down
        stage.STAGE_DECLINING,   # 아래, ma down, rs down
        stage.STAGE_BASING,      # 아래, ma up, rs up → fall-through
    ]


def test_stage_series_nan_propagates_to_unknown():
    close = pd.Series([100, np.nan, 100])
    sma = pd.Series([90, 90, 90])
    ma_slope = pd.Series([0.001, 0.001, 0.001])
    rs_slope = pd.Series([0.001, 0.001, 0.001])
    out = stage.classify_stage_series(close, sma, ma_slope, rs_slope)
    assert out.iloc[1] == stage.STAGE_UNKNOWN
    assert out.iloc[0] == stage.STAGE_ADVANCING
    assert out.iloc[2] == stage.STAGE_ADVANCING


# ----------------- stage_since -----------------

def test_stage_since_recent_continuous_block():
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"])
    series = pd.Series([1, 1, 2, 2, 2], index=dates)
    ts = stage.stage_since(series, target=stage.STAGE_ADVANCING)
    assert ts == pd.Timestamp("2026-01-03")


def test_stage_since_returns_none_when_not_in_target_today():
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"])
    series = pd.Series([2, 2, 1], index=dates)
    assert stage.stage_since(series, target=stage.STAGE_ADVANCING) is None


def test_stage_since_only_today():
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"])
    series = pd.Series([1, 1, 2], index=dates)
    ts = stage.stage_since(series, target=stage.STAGE_ADVANCING)
    assert ts == pd.Timestamp("2026-01-03")


def test_stage_since_empty():
    assert stage.stage_since(pd.Series(dtype="int8")) is None


# ----------------- compute_stage_for_ticker (통합) -----------------

def test_compute_stage_for_ticker_full_uptrend():
    """가격과 지수가 모두 우상향, 가격이 지수를 outperform → 종반엔 Stage 2."""
    n = 250
    dates = [f"2026{i//30+1:02d}{i%30+1:02d}" for i in range(n)]  # 임의 날짜 문자열 (정렬용)
    dates = [f"202601{i+1:02d}" if i < 30 else f"202602{i-29:02d}" if i < 60 else
             f"2026{(i//30)+1:02d}{(i%30)+1:02d}" for i in range(n)]
    # 단순히 정렬되는 문자열이면 충분
    dates = [f"99{i:05d}" for i in range(n)]
    close_vals = np.linspace(100, 200, n)         # 종목: 100 → 200
    idx_vals = np.linspace(2000, 2400, n)         # 지수: 2000 → 2400 (덜 강하게 상승)

    bars = pd.DataFrame({
        "date": dates,
        "close": close_vals,
        "high": close_vals + 1,
        "low": close_vals - 1,
        "open": close_vals,
        "volume": [1000] * n,
    })
    idx = pd.Series(idx_vals, index=dates)

    out = stage.compute_stage_for_ticker(bars, idx, sma_window=150, slope_lookback=50)
    # 마지막은 충분히 위 + MA 상승 + RS 상승이라 Stage 2 기대
    assert out["stage"].iloc[-1] == stage.STAGE_ADVANCING


def test_compute_stage_for_ticker_empty_bars():
    out = stage.compute_stage_for_ticker(
        pd.DataFrame(columns=["date", "close", "high", "low", "open", "volume"]),
        pd.Series(dtype=float, name="idx"),
    )
    assert out.empty
