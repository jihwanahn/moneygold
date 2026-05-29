"""indicators.py 단위 테스트."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from moneygold.strategies.momentum_breakout import indicators as ind


def test_rolling_new_high_excludes_today():
    """rolling_new_high(n) 는 오늘 *제외* 직전 n봉 최고. 마지막 값은 close[-2..-n-1] max."""
    s = pd.Series([10, 20, 15, 30, 25])
    nh = ind.rolling_new_high(s, 3)
    # idx=4 의 직전 3봉은 [20, 15, 30] → 30
    assert nh.iloc[4] == 30
    # idx=3 의 직전 3봉은 [10,20,15] → 20
    assert nh.iloc[3] == 20
    # n 미달 구간 NaN
    assert pd.isna(nh.iloc[0])
    assert pd.isna(nh.iloc[2])


def test_rolling_new_high_invalid_n():
    with pytest.raises(ValueError):
        ind.rolling_new_high(pd.Series([1, 2, 3]), 0)


def test_breakout_mask_basic():
    s = pd.Series([10, 11, 12, 13, 14, 20])
    m = ind.breakout_mask(s, 3)
    # idx=5: max(11,12,13,14)=14, today=20 > 14 → True. 단 n=3 이므로 직전 3봉만 = [12,13,14]=14
    assert m.iloc[5] is np.True_ or m.iloc[5] == True   # noqa: E712


def test_is_fresh_breakout_simple():
    """직전 60봉 평탄 + 직전 20봉 안 깬 채로 오늘 첫 돌파 → True."""
    # 100봉 평탄 (90~99) + 마지막 +1봉 110 돌파
    closes = [95.0] * 100 + [110.0]
    s = pd.Series(closes)
    assert ind.is_fresh_breakout(s, lookback=60, fresh_window=20) is True


def test_is_fresh_breakout_repeated_recent_breakout_filtered():
    """직전 20봉 안에 이미 한 번 60일 신고가 돌파한 적 있으면 fresh 아님."""
    # 60봉 90 → 1봉 105 (1차 돌파) → 19봉 100 (돌파 후 잔존) → 1봉 110 (오늘)
    closes = (
        [90.0] * 60
        + [105.0]              # 1차 돌파 (직전 20봉 안에 있음)
        + [100.0] * 19
        + [110.0]              # 오늘 — 다시 신고가지만 fresh 아님
    )
    s = pd.Series(closes)
    assert ind.is_fresh_breakout(s, lookback=60, fresh_window=20) is False


def test_is_fresh_breakout_old_breakout_does_not_block():
    """fresh_window 보다 오래된 과거 돌파는 무시."""
    # 30봉 90 → 1봉 105 (오래된 돌파) → 70봉 95 (충분히 옛날) → 1봉 110 (오늘)
    closes = (
        [90.0] * 30
        + [105.0]
        + [95.0] * 70
        + [110.0]
    )
    s = pd.Series(closes)
    # fresh_window=20 이므로 직전 20봉만 본다. 70봉짜리 [95.0] 안에는 돌파 없음.
    assert ind.is_fresh_breakout(s, lookback=60, fresh_window=20) is True


def test_is_fresh_breakout_insufficient_data():
    s = pd.Series([100.0] * 50)
    assert ind.is_fresh_breakout(s, lookback=60, fresh_window=20) is False


def test_volume_spike_pass():
    # 20봉 평균 100, 오늘 200 → 2.0배
    v = pd.Series([100.0] * 20 + [200.0])
    passed, r = ind.volume_spike(v, 20, 1.5)
    assert passed is True
    assert r == pytest.approx(2.0)


def test_volume_spike_fail_below_ratio():
    v = pd.Series([100.0] * 20 + [120.0])
    passed, r = ind.volume_spike(v, 20, 1.5)
    assert passed is False
    assert r == pytest.approx(1.2)


def test_volume_spike_insufficient_history():
    v = pd.Series([100.0] * 10 + [500.0])
    passed, r = ind.volume_spike(v, 20, 1.5)
    assert passed is False
    assert np.isnan(r)


def test_volume_spike_zero_average():
    v = pd.Series([0.0] * 20 + [100.0])
    passed, r = ind.volume_spike(v, 20, 1.5)
    assert passed is False
    assert np.isnan(r)


def test_ma20_basic():
    s = pd.Series(range(40))
    m = ind.ma20(s, 20)
    assert pd.isna(m.iloc[18])
    # idx=19: mean(0..19) = 9.5
    assert m.iloc[19] == pytest.approx(9.5)
    assert m.iloc[39] == pytest.approx(np.mean(range(20, 40)))
