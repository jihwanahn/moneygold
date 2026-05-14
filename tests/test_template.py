"""template.py — Minervini 8 조건."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from moneygold import template


def _strong_uptrend_series(n: int = 320, daily_pct: float = 0.003) -> pd.Series:
    """매일 0.3% 우상향. 350봉이면 ~+186%, 252일 +112%."""
    return pd.Series(100.0 * (1.0 + daily_pct) ** np.arange(n))


def _flat_series(n: int = 320, value: float = 100.0) -> pd.Series:
    return pd.Series([value] * n)


def _downtrend_series(n: int = 320, daily_pct: float = -0.003) -> pd.Series:
    return pd.Series(100.0 * (1.0 + daily_pct) ** np.arange(n))


# ----------------- pass / fail bulk -----------------

def test_template_passes_for_strong_uptrend_with_high_rs():
    close = _strong_uptrend_series()
    r = template.check_template(close, rs_rank_value=85.0)
    assert r.passed
    assert all(r.checks)


def test_template_fails_when_rs_below_70():
    close = _strong_uptrend_series()
    r = template.check_template(close, rs_rank_value=50.0)
    assert not r.passed
    # 조건 8(인덱스 7)만 fail, 나머지 7 통과
    assert r.checks[7] is False
    assert sum(r.checks) == 7


def test_template_fails_for_flat_series():
    """평탄선: SMA들이 같음 → 조건 2(sma150>sma200)·4(sma50>sma150>sma200) 등 fail."""
    close = _flat_series()
    r = template.check_template(close, rs_rank_value=80.0)
    assert not r.passed


def test_template_fails_for_downtrend():
    close = _downtrend_series()
    r = template.check_template(close, rs_rank_value=80.0)
    assert not r.passed
    # close < sma50 < sma150 < sma200 — 거의 모든 조건 fail
    assert sum(r.checks) <= 2


# ----------------- 개별 조건 분기 -----------------

def test_condition_1_close_above_sma150_and_sma200():
    """가격이 SMA150/200 아래로 떨어지면 조건 1 fail."""
    n = 320
    # 200일 우상향 후 마지막 1봉만 강한 갭다운으로 SMA 아래로
    arr = (100.0 * (1.003 ** np.arange(n))).tolist()
    arr[-1] = arr[-1] * 0.5   # 마지막 봉 -50%
    close = pd.Series(arr)
    r = template.check_template(close, rs_rank_value=85.0)
    assert r.checks[0] is False


def test_condition_6_low_recovery_default_25pct():
    """디폴트 25% 임계: 저점 대비 25% 미만 회복이면 조건 6 fail.

    저점 80, 현재 99이면 +23.75% → fail.
    """
    n = 320
    arr = [100.0] * 100 + list(np.linspace(100, 80, 100)) + list(np.linspace(80, 99, 120))
    arr = arr[:n]
    close = pd.Series(arr)
    r = template.check_template(close, rs_rank_value=85.0)
    assert r.checks[5] is False


def test_condition_6_low_recovery_passes_at_threshold():
    """저점 80, 현재 100이면 +25% → 디폴트 임계에 정확히 도달, 통과."""
    n = 320
    arr = [100.0] * 100 + list(np.linspace(100, 80, 100)) + list(np.linspace(80, 100, 120))
    arr = arr[:n]
    close = pd.Series(arr)
    r = template.check_template(close, rs_rank_value=85.0)
    assert r.checks[5] is True


def test_condition_7_within_25pct_of_high():
    """52주 고점 대비 75% 미만이면 조건 7 fail."""
    n = 320
    arr = list(np.linspace(50, 150, 200)) + list(np.linspace(150, 100, 120))
    arr = arr[:n]
    close = pd.Series(arr)
    r = template.check_template(close, rs_rank_value=85.0)
    # high_52w ≈ 150, close ≈ 100. 100/150 = 0.667 < 0.75 → fail
    assert r.checks[6] is False


# ----------------- 데이터 부족 -----------------

def test_template_short_series_returns_all_fail():
    close = pd.Series([100.0] * 100)   # 200+slope_lookback 미달
    r = template.check_template(close, rs_rank_value=85.0)
    assert not r.passed
    assert all(c is False for c in r.checks)
    assert np.isnan(r.sma200)


def test_template_nan_rs_rank_fails_condition_8():
    close = _strong_uptrend_series()
    r = template.check_template(close, rs_rank_value=float("nan"))
    assert r.checks[7] is False
    assert not r.passed


# ----------------- 파라미터 조정 -----------------

def test_template_lower_rs_threshold_can_pass():
    """rs_rank_min을 60으로 낮추면 RS 60도 통과."""
    close = _strong_uptrend_series()
    r = template.check_template(close, rs_rank_value=60.0, rs_rank_min=60.0)
    assert r.passed
