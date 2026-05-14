"""darvas.py — 박스 상태머신 sanity tests."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from moneygold import darvas


def _make_bars(highs, lows, closes, vols=None, opens=None) -> pd.DataFrame:
    n = len(highs)
    if opens is None:
        opens = closes
    if vols is None:
        vols = [1000] * n
    return pd.DataFrame({
        "date": [f"2026{i//30+1:02d}{i%30+1:02d}" for i in range(n)],
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": vols,
    })


def _params_fast() -> darvas.BoxParams:
    """테스트용 짧은 파라미터."""
    return darvas.BoxParams(
        box_high_lookback=5,
        box_high_confirm=2,
        box_height_max_pct=10.0,
        box_valid_min_days=5,
        box_stale_days=20,
        breakout_buffer=0.003,
        breakout_volume_mult=1.5,
        volume_avg_window=10,
    )


# ----------------- 박스 형성 -----------------

def test_box_forms_after_high_confirm_and_consolidation():
    """랠리 후 횡보 → 박스 형성. 마지막엔 CONFIRMED 상태."""
    p = _params_fast()
    # 1-15: 랠리 (high 50 → 100)
    # 16: 신고가 100 형성 (top 후보)
    # 17-18: 신고가 미경신 → top 확정 (confirm=2)
    # 19-30: 95~99 사이 횡보 (박스 안)
    n = 35
    highs = [50 + i * 3 for i in range(15)]   # 50→92
    highs += [100]                              # i=15: 신고가
    highs += [98, 97]                           # i=16,17: pending 확정
    highs += [99, 98, 97, 98, 99, 98, 97, 98, 99, 98, 97, 96, 95, 96, 97, 98, 99]  # 박스 안
    highs = highs[:n]

    lows = [h - 3 for h in highs]
    closes = [(h + l) / 2 for h, l in zip(highs, lows)]
    bars = _make_bars(highs, lows, closes)

    df = darvas.compute_box_states(bars, p)
    states = df["box_state"].tolist()
    # 마지막은 FORMING 또는 CONFIRMED
    assert states[-1] in (darvas.FORMING, darvas.CONFIRMED)
    # top은 100 (또는 그 근처)
    assert df["box_top"].iloc[-1] == pytest.approx(100.0, abs=2.0)


def test_box_breakout_with_volume_triggers_breakout_today():
    """박스 형성 후 거래량 동반 돌파 → BREAKOUT_TODAY."""
    p = _params_fast()
    # 15봉 랠리 + 천장 100 형성 후 18봉 횡보 + 마지막 돌파
    highs = list(np.linspace(50, 100, 15))
    highs += [98, 97, 98, 99, 97, 96, 98, 99, 98, 97, 96, 98, 99, 97, 98, 99, 98, 97]
    # 마지막 봉: 종가 105 + 거래량 폭증
    highs += [105]
    n = len(highs)

    lows = [h - 3 for h in highs]
    closes = list(highs)
    closes[-1] = 105.0
    vols = [1000] * (n - 1) + [3000]
    bars = _make_bars(highs, lows, closes, vols=vols)

    df = darvas.compute_box_states(bars, p)
    states = df["box_state"].tolist()
    assert states[-1] in (darvas.BREAKOUT_TODAY, darvas.BREAKOUT_GAP)


def test_box_gap_breakout_when_open_above_top():
    """돌파 + 시가가 천장 위 → BREAKOUT_GAP."""
    p = _params_fast()
    highs = list(np.linspace(50, 100, 15))
    highs += [98, 97, 98, 99, 97, 96, 98, 99, 98, 97, 96, 98, 99, 97, 98, 99, 98, 97]
    highs += [110]
    n = len(highs)

    lows = [h - 3 for h in highs]
    closes = list(highs)
    opens = [c for c in closes]
    opens[-1] = 108.0
    vols = [1000] * (n - 1) + [3000]
    bars = _make_bars(highs, lows, closes, vols=vols, opens=opens)

    df = darvas.compute_box_states(bars, p)
    assert df["box_state"].iloc[-1] == darvas.BREAKOUT_GAP


# ----------------- 박스 무효화 -----------------

def test_box_height_exceeded_resets_to_searching():
    """천장-12% 한도 초과 하락 → 박스 무효."""
    p = _params_fast()   # box_height_max_pct=10
    n = 25
    # 랠리 후 천장 100 형성, 그 후 갑작스런 큰 하락
    highs = list(np.linspace(50, 100, 15))
    highs += [98, 97]   # confirm 완료
    highs += [95, 92, 85, 84]   # 큰 하락 (low가 80대 → 천장-10%인 90 깸)
    highs += list(np.linspace(85, 95, 6))
    highs = highs[:n]

    lows = [h - 5 for h in highs]
    # 정확히 한도 깨도록 강제: 18번 봉 low를 88로
    lows[18] = 88   # 천장 100 * (1-0.10) = 90, low 88 < 90 → 한도 깸
    closes = [(h + l) / 2 for h, l in zip(highs, lows)]
    bars = _make_bars(highs, lows, closes)

    df = darvas.compute_box_states(bars, p)
    states = df["box_state"].tolist()
    # 18번 봉 또는 직후엔 SEARCHING으로 돌아가야 함
    assert darvas.SEARCHING in states[18:22]


def test_breakdown_when_close_below_bottom_in_confirmed():
    """CONFIRMED 박스에서 close < bottom → BROKEN_DOWN."""
    p = _params_fast()
    n = 35
    # 사전 랠리 + 횡보 (박스 형성)
    highs = list(np.linspace(50, 100, 15))
    highs += [98, 97]   # confirm
    highs += [96, 97, 98, 95, 96, 97, 95]   # 7봉 박스 형성 — CONFIRMED
    # 박스 안 bottom ≈ 92 (95-3) 정도 형성된 후
    highs += [85]   # 큰 하락 — 종가가 박스 바닥 미만
    highs = highs[:n] if len(highs) > n else highs + [85] * (n - len(highs))
    highs = highs[:n]

    lows = [h - 3 for h in highs]
    # 마지막 봉의 close를 강제로 박스 바닥 미만으로
    closes = list(highs)
    closes[-1] = 80   # 박스 바닥 (95-3=92 또는 더 낮음) 미만
    bars = _make_bars(highs, lows, closes)

    df = darvas.compute_box_states(bars, p)
    states = df["box_state"].tolist()
    # 어디선가 BROKEN_DOWN 발생해야 함
    assert darvas.BROKEN_DOWN in states


# ----------------- 데이터 부족 -----------------

def test_short_series_stays_searching():
    p = _params_fast()
    bars = _make_bars([50, 51, 52], [49, 50, 51], [50, 51, 52])
    df = darvas.compute_box_states(bars, p)
    assert (df["box_state"] == darvas.SEARCHING).all()


def test_empty_bars_returns_empty():
    p = _params_fast()
    bars = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = darvas.compute_box_states(bars, p)
    assert df.empty


# ----------------- current_box 편의 함수 -----------------

def test_current_box_returns_last_state():
    p = _params_fast()
    bars = _make_bars([50, 51, 52], [49, 50, 51], [50, 51, 52])
    box = darvas.current_box(bars, p)
    assert box.state == darvas.SEARCHING
    assert box.top is None


def test_current_box_dataclass_breakout_flags():
    box = darvas.BoxState(state=darvas.BREAKOUT_TODAY, top=100.0, bottom=90.0)
    assert box.is_breakout
    assert not box.is_gap

    gap = darvas.BoxState(state=darvas.BREAKOUT_GAP)
    assert gap.is_breakout
    assert gap.is_gap

    none_box = darvas.BoxState(state=darvas.SEARCHING)
    assert not none_box.is_breakout
