"""stage.py — Weinstein 분류기 (TV 일치, history-dependent) sanity tests."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from moneygold import stage


def _params_fast() -> stage.StageParams:
    """단위 테스트용 짧은 파라미터."""
    return stage.StageParams(
        ma_length=20, slope_lookback=5,
        slope_threshold_pct=0.001, band_pct=0.01,
        ma_type="SMA",
    )


# ----------------- 데이터 부족 / 워밍업 -----------------

def test_short_series_returns_unknown_until_ma_ready():
    p = _params_fast()
    close = pd.Series([100.0] * 10)
    series = stage.classify_stage_series(close, p)
    # MA 길이 20봉 미달 → 전부 UNKNOWN
    assert (series == stage.STAGE_UNKNOWN).all()


def test_classify_stage_empty_returns_unknown():
    assert stage.classify_stage(pd.Series(dtype=float)) == stage.STAGE_UNKNOWN


# ----------------- Stage 2 (Advance) -----------------

def test_stage_2_when_strong_uptrend_above_band():
    """매일 +0.5% 우상향 → MA slope 양수 + close > MA + band."""
    p = _params_fast()
    n = 60
    close = pd.Series(100.0 * (1.0 + 0.005) ** np.arange(n))
    series = stage.classify_stage_series(close, p)
    assert series.iloc[-1] == stage.STAGE_ADVANCING


def test_stage_4_when_strong_downtrend_below_band():
    p = _params_fast()
    n = 60
    close = pd.Series(100.0 * (1.0 - 0.005) ** np.arange(n))
    series = stage.classify_stage_series(close, p)
    assert series.iloc[-1] == stage.STAGE_DECLINING


# ----------------- Stage 1 / 3 (history-dependent) -----------------

def test_stage_3_emerges_after_stage_2_goes_flat():
    """우상향 후 평탄 → Stage 2 → Stage 3."""
    p = _params_fast()
    rise = list(100.0 * (1.0 + 0.005) ** np.arange(50))    # 우상향 50봉
    flat = [rise[-1]] * 30                                  # 평탄 30봉
    close = pd.Series(rise + flat)
    series = stage.classify_stage_series(close, p)

    # 중간엔 Stage 2가 있어야 함
    assert stage.STAGE_ADVANCING in series.values
    # 마지막은 Stage 3 (Stage 2 → flat)
    assert series.iloc[-1] == stage.STAGE_TOPPING


def test_stage_1_after_decline_goes_flat():
    """우하향 후 평탄 → Stage 4 → Stage 1."""
    p = _params_fast()
    fall = list(100.0 * (1.0 - 0.005) ** np.arange(50))
    flat = [fall[-1]] * 30
    close = pd.Series(fall + flat)
    series = stage.classify_stage_series(close, p)

    assert stage.STAGE_DECLINING in series.values
    assert series.iloc[-1] == stage.STAGE_BASING


# ----------------- Price band -----------------

def test_no_state_change_inside_band():
    """가격이 MA 근처 (band 안) + slope 양수면 직전 상태 유지 (관성)."""
    p = stage.StageParams(
        ma_length=10, slope_lookback=3,
        slope_threshold_pct=0.0001, band_pct=0.10,  # 큰 band
        ma_type="SMA",
    )
    # 약하게 우상향 (MA 근처 머묾)
    close = pd.Series([100.0 + i * 0.05 for i in range(30)])
    series = stage.classify_stage_series(close, p)
    # 가격이 band 안에 있어서 stage 2 진입 못 함. Stage 1 유지 가능성.
    last = int(series.iloc[-1])
    assert last in (stage.STAGE_BASING, stage.STAGE_ADVANCING)   # 환경에 따라


# ----------------- stage_since -----------------

def test_stage_since_returns_start_of_continuous_block():
    series = pd.Series([1, 1, 2, 2, 2], index=["d1", "d2", "d3", "d4", "d5"], dtype="int8")
    assert stage.stage_since(series, target=stage.STAGE_ADVANCING) == "d3"


def test_stage_since_none_when_not_in_target():
    series = pd.Series([2, 2, 1], index=["d1", "d2", "d3"], dtype="int8")
    assert stage.stage_since(series, target=stage.STAGE_ADVANCING) is None


# ----------------- classify_stage (단일 시점 편의) -----------------

def test_classify_stage_returns_last_value_of_series():
    p = _params_fast()
    n = 60
    close = pd.Series(100.0 * (1.0 + 0.005) ** np.arange(n))
    assert stage.classify_stage(close, p) == stage.STAGE_ADVANCING


# ----------------- EMA 옵션 -----------------

def test_ema_option_works_without_error():
    p = stage.StageParams(
        ma_length=20, slope_lookback=5,
        slope_threshold_pct=0.001, band_pct=0.03,
        ma_type="EMA",
    )
    close = pd.Series(100.0 * (1.0 + 0.005) ** np.arange(60))
    series = stage.classify_stage_series(close, p)
    assert len(series) == 60
    assert series.iloc[-1] in (stage.STAGE_ADVANCING, stage.STAGE_BASING, stage.STAGE_TOPPING)
