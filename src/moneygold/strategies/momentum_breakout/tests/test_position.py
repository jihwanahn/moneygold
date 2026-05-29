"""position.py 상태머신 테스트."""
from __future__ import annotations

import pytest

from moneygold.strategies.momentum_breakout import (
    EXITED,
    INITIAL,
    TRAILING,
    MomentumConfig,
    PositionEntry,
    position_step,
)
from moneygold.strategies.momentum_breakout.position import (
    initial_state,
    run_through_bars,
)
from moneygold.strategies.momentum_breakout.tests.fixtures.synthetic import make_bars


def _entry(price: float = 100.0, ticker: str = "T") -> PositionEntry:
    return PositionEntry(
        ticker=ticker, name="n", market="KOSPI",
        entry_date="20250101", entry_price=price,
    )


def _cfg(**ov) -> MomentumConfig:
    return MomentumConfig(
        stop_loss_pct=ov.get("stop_loss_pct", 0.10),
        profit_trigger_pct=ov.get("profit_trigger_pct", 0.20),
        new_high_lookback=60, fresh_window=20, trailing_ma_period=20,
        top_n_value=100, top_n_marketcap=None,
        volume_spike_ratio=1.5, volume_avg_window=20, min_listed_days=60,
        gap_down_exit_policy=ov.get("gap_down_exit_policy", "open"),
    )


def test_initial_state_stop_at_minus_10pct():
    s = initial_state(_entry(100.0), _cfg())
    assert s.phase == INITIAL
    assert s.stop == pytest.approx(90.0)
    assert s.high_since_entry == 100.0


def test_initial_to_trailing_on_plus_20pct():
    """종가가 entry × 1.20 도달 → INITIAL → TRAILING."""
    e = _entry(100.0)
    cfg = _cfg()
    state = initial_state(e, cfg)
    bar = {"date": "20250102", "open": 119.0, "close": 120.0}
    res = position_step(e, state, bar, cfg, ma20_today=110.0)
    assert res.state.phase == TRAILING
    # ma20 (110) > prev stop (90) → 갱신
    assert res.state.stop == pytest.approx(110.0)
    assert res.trail_updated is True
    assert res.exit_reason is None


def test_trailing_stop_ratchet_never_decreases():
    """TRAILING 상태에서 MA20 이 prev_stop 보다 *낮으면* 갱신 안 함."""
    e = _entry(100.0)
    cfg = _cfg()
    # 강제로 TRAILING 상태에서 시작
    from moneygold.strategies.momentum_breakout.position import PositionState
    state = PositionState(phase=TRAILING, stop=115.0, high_since_entry=125.0, asof="20250110")
    bar = {"date": "20250111", "open": 122.0, "close": 121.0}
    res = position_step(e, state, bar, cfg, ma20_today=112.0)  # MA20 < stop
    assert res.state.stop == 115.0   # 후퇴 X
    assert res.trail_updated is False


def test_stop_hit_on_close_below_stop():
    """종가가 stop 이하 → STOP_HIT, 청산."""
    e = _entry(100.0)
    cfg = _cfg()
    state = initial_state(e, cfg)  # stop=90
    bar = {"date": "20250105", "open": 92.0, "close": 89.0}
    res = position_step(e, state, bar, cfg, ma20_today=None)
    assert res.state.phase == EXITED
    assert res.exit_reason == "STOP_HIT"
    # 갭다운 아님 (open >= stop). 종가 < stop → exit_price = stop.
    assert res.exit_price == pytest.approx(90.0)
    assert res.gap_down is False


def test_gap_down_exit_price_open_policy():
    """gap_down_exit_policy='open': 시초가가 stop 아래 → 시가 청산."""
    e = _entry(100.0)
    cfg = _cfg(gap_down_exit_policy="open")
    state = initial_state(e, cfg)   # stop=90
    bar = {"date": "20250105", "open": 85.0, "close": 87.0}
    res = position_step(e, state, bar, cfg, ma20_today=None)
    assert res.state.phase == EXITED
    assert res.exit_reason == "STOP_HIT"
    assert res.exit_price == pytest.approx(85.0)
    assert res.gap_down is True


def test_gap_down_exit_price_close_policy():
    """gap_down_exit_policy='close': 시초가 갭다운이라도 종가 청산."""
    e = _entry(100.0)
    cfg = _cfg(gap_down_exit_policy="close")
    state = initial_state(e, cfg)
    bar = {"date": "20250105", "open": 85.0, "close": 87.0}
    res = position_step(e, state, bar, cfg, ma20_today=None)
    assert res.exit_price == pytest.approx(87.0)
    assert res.gap_down is True


def test_exited_state_is_terminal():
    """EXITED 상태에선 step() 이 상태 그대로 반환, 추가 청산 X."""
    e = _entry(100.0)
    cfg = _cfg()
    from moneygold.strategies.momentum_breakout.position import PositionState
    state = PositionState(phase=EXITED, stop=90.0, high_since_entry=100.0, asof="20250105")
    bar = {"date": "20250106", "open": 50.0, "close": 50.0}
    res = position_step(e, state, bar, cfg, ma20_today=None)
    assert res.state == state
    assert res.exit_reason is None


def test_run_through_bars_full_cycle():
    """100→120 (TRAILING 전환) → 130 (MA20 ratchet) → 종가 stop 이탈 청산."""
    e = _entry(100.0)
    cfg = _cfg()
    # 종가: 100, 105, 110, 115, 120 (트리거), 125, 130, 128, 125, 110 (STOP_HIT 후보)
    # MA20 은 close 부족하면 NaN → trailing 미작동. 그래서 의도적으로 충분한 bars 사용.
    closes = [100, 102, 104, 106, 108, 110, 112, 114, 116, 118,
              119, 120, 122, 124, 126, 128, 130, 128, 124, 120,
              125, 130, 135, 140, 100]   # 마지막 봉 종가 100 = stop_loss(90)/(?) — 다음 검증
    bars = make_bars(closes, ticker="X")
    # 진입 다음 봉부터 (= bars 전체)
    final, steps = run_through_bars(e, bars, cfg)
    # 어딘가에서 TRAILING 으로 전환되어야 함
    phases = [s.state.phase for s in steps]
    assert TRAILING in phases
    # MA20 이 prev_stop 보다 커지는 시점이 있어 trailing 갱신 일어남
    assert any(s.trail_updated for s in steps)


def test_run_through_bars_no_trailing_if_below_target():
    """종가가 entry+20% 한 번도 못 닿으면 phase 는 INITIAL 만."""
    e = _entry(100.0)
    cfg = _cfg()
    closes = [100, 102, 104, 106, 108, 110, 108, 106, 104, 102] + [105] * 30
    bars = make_bars(closes, ticker="STK")
    final, steps = run_through_bars(e, bars, cfg)
    phases = {s.state.phase for s in steps}
    assert phases == {INITIAL}
    assert final.stop == pytest.approx(90.0)   # 후퇴 X
