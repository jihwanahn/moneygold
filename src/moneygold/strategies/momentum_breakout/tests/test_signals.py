"""find_entry_candidates 통합 테스트 (synthetic 데이터)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from moneygold.strategies.momentum_breakout import (
    MomentumConfig,
    find_entry_candidates,
)
from moneygold.strategies.momentum_breakout.tests.fixtures.synthetic import (
    make_bars,
    trending_then_breakout,
)


def _master(tickers: list[str], market: str = "KOSPI") -> pd.DataFrame:
    return pd.DataFrame({
        "ticker": tickers,
        "name": [f"name_{t}" for t in tickers],
        "market": [market] * len(tickers),
        "mcap": [1e12] * len(tickers),
    })


def _cfg(**overrides) -> MomentumConfig:
    base = MomentumConfig(
        new_high_lookback=60,
        fresh_window=20,
        volume_spike_ratio=1.5,
        volume_avg_window=20,
        top_n_value=100,
        top_n_marketcap=None,
        min_listed_days=60,
    )
    return MomentumConfig(
        stop_loss_pct=overrides.get("stop_loss_pct", base.stop_loss_pct),
        profit_trigger_pct=overrides.get("profit_trigger_pct", base.profit_trigger_pct),
        new_high_lookback=overrides.get("new_high_lookback", base.new_high_lookback),
        fresh_window=overrides.get("fresh_window", base.fresh_window),
        trailing_ma_period=overrides.get("trailing_ma_period", base.trailing_ma_period),
        top_n_value=overrides.get("top_n_value", base.top_n_value),
        top_n_marketcap=overrides.get("top_n_marketcap", base.top_n_marketcap),
        volume_spike_ratio=overrides.get("volume_spike_ratio", base.volume_spike_ratio),
        volume_avg_window=overrides.get("volume_avg_window", base.volume_avg_window),
        min_listed_days=overrides.get("min_listed_days", base.min_listed_days),
        gap_down_exit_policy=overrides.get("gap_down_exit_policy", base.gap_down_exit_policy),
    )


def test_fresh_breakout_passes_all_gates():
    """100봉 평탄 + 마지막 봉 신고가 + 거래대금 2배 → 후보 진입."""
    bars = trending_then_breakout(
        n_pre=100, pre_level=100, pre_amplitude=3,
        breakout_close=110.0, breakout_volume_mult=2.0,
        ticker="ACE",
    )
    master = _master(["ACE"])
    asof = bars["date"].iloc[-1]
    out = find_entry_candidates(asof, {"ACE": bars}, master, _cfg(), apply_filter_master=False)
    assert len(out) == 1
    e = out[0]
    assert e.ticker == "ACE"
    assert e.close == pytest.approx(110.0)
    # value=close×volume 이므로 ratio는 단순 volume_mult 가 아니라 close 차이까지 반영.
    # breakout_volume_mult=2 + close 상승까지 합쳐서 ~2.2.
    assert e.volume_ratio >= 1.5
    assert e.new_high_amplitude > 0
    assert e.suggested_stop == pytest.approx(110.0 * 0.90)
    assert e.value_rank == 1


def test_repeated_breakout_within_fresh_window_filtered():
    """직전 20봉 안에 이미 60일 신고가 돌파 발생 → 오늘 다시 돌파해도 fresh 아님."""
    # 60 평탄 (90) + 1차 돌파 105 + 19 평탄 100 + 오늘 110
    closes = np.concatenate([
        np.full(60, 90.0),
        np.array([105.0]),
        np.full(19, 100.0),
        np.array([110.0]),
    ])
    # 마지막 봉 거래대금 spike 도 줘서 거래대금/spike 게이트는 통과시킨다 — 그래도
    # fresh 게이트가 막아야 함.
    vols = np.concatenate([
        np.full(80, 1_000_000.0),
        np.array([3_000_000.0]),
    ])
    bars = make_bars(closes, volumes=vols, ticker="REP")
    master = _master(["REP"])
    asof = bars["date"].iloc[-1]
    out = find_entry_candidates(asof, {"REP": bars}, master, _cfg(), apply_filter_master=False)
    assert out == []


def test_volume_spike_below_threshold_filtered():
    """가격은 신고가지만 거래대금 1.2배(<1.5) → 컷."""
    closes = np.concatenate([np.full(100, 100.0), [110.0]])
    # 1.5 미만의 spike. 평균 1_000_000 → 오늘 1_200_000.
    vols = np.concatenate([np.full(100, 1_000_000.0), [1_200_000.0]])
    bars = make_bars(closes, volumes=vols, ticker="LOW")
    out = find_entry_candidates(
        bars["date"].iloc[-1], {"LOW": bars}, _master(["LOW"]),
        _cfg(), apply_filter_master=False,
    )
    assert out == []


def test_min_listed_days_filters_new_listing():
    """상장 < 60일 — bars 50봉밖에 없음. 후보 진입 X."""
    closes = np.concatenate([np.full(45, 100.0), [110.0]])
    vols = np.concatenate([np.full(45, 1_000_000.0), [3_000_000.0]])
    bars = make_bars(closes, volumes=vols, ticker="NEW")
    out = find_entry_candidates(
        bars["date"].iloc[-1], {"NEW": bars}, _master(["NEW"]),
        _cfg(min_listed_days=60), apply_filter_master=False,
    )
    assert out == []


def test_top_n_value_cuts_low_value_ticker():
    """ACE / SLOW 두 종목 모두 신고가 돌파지만 top_n_value=1 이면 ACE 만.

    ACE 의 당일 거래대금이 SLOW 보다 큼 → top 1 = ACE.
    """
    bars_ace = trending_then_breakout(
        n_pre=100, pre_level=100, pre_amplitude=3,
        breakout_close=110.0, breakout_volume_mult=3.0, ticker="ACE",
    )
    bars_slow = trending_then_breakout(
        n_pre=100, pre_level=50, pre_amplitude=2,
        breakout_close=55.0, breakout_volume_mult=2.0, seed=99, ticker="SLO",
    )
    master = _master(["ACE", "SLO"])
    asof = bars_ace["date"].iloc[-1]
    out = find_entry_candidates(
        asof, {"ACE": bars_ace, "SLO": bars_slow}, master,
        _cfg(top_n_value=1), apply_filter_master=False,
    )
    assert len(out) == 1
    assert out[0].ticker == "ACE"


def test_no_breakout_returns_empty():
    """오늘 종가가 신고가 미달 → 빈 리스트."""
    closes = np.concatenate([np.full(100, 100.0), [99.0]])
    vols = np.concatenate([np.full(100, 1_000_000.0), [3_000_000.0]])
    bars = make_bars(closes, volumes=vols, ticker="FLT")
    out = find_entry_candidates(
        bars["date"].iloc[-1], {"FLT": bars}, _master(["FLT"]),
        _cfg(), apply_filter_master=False,
    )
    assert out == []


def test_us_market_independent_ranking():
    """US 시장은 *별도 그룹*으로 독립 ranking. KR과 분리되어 같은 시그널 통과 가능."""
    bars = trending_then_breakout(
        n_pre=100, pre_level=100, pre_amplitude=3, breakout_close=110.0,
        breakout_volume_mult=3.0, ticker="USX",
    )
    master = pd.DataFrame({"ticker": ["USX"], "name": ["us"], "market": ["US"], "mcap": [1e12]})
    out = find_entry_candidates(
        bars["date"].iloc[-1], {"USX": bars}, master,
        _cfg(), apply_filter_master=False,
    )
    assert len(out) == 1
    assert out[0].ticker == "USX"
    assert out[0].market == "US"


def test_markets_argument_filters():
    """markets 인자로 KR 만 또는 US 만 선택 가능."""
    kr_bars = trending_then_breakout(
        n_pre=100, pre_level=100, pre_amplitude=3, breakout_close=110.0,
        breakout_volume_mult=3.0, seed=1, ticker="KR1",
    )
    us_bars = trending_then_breakout(
        n_pre=100, pre_level=100, pre_amplitude=3, breakout_close=110.0,
        breakout_volume_mult=3.0, seed=2, ticker="US1",
    )
    master = pd.DataFrame({
        "ticker": ["KR1", "US1"],
        "name": ["kr", "us"],
        "market": ["KOSPI", "US"],
        "mcap": [1e12, 1e12],
    })
    asof = kr_bars["date"].iloc[-1]
    bars_map = {"KR1": kr_bars, "US1": us_bars}
    # KR 만
    out_kr = find_entry_candidates(asof, bars_map, master, _cfg(),
                                    apply_filter_master=False, markets=("KOSPI","KOSDAQ"))
    assert all(e.market in ("KOSPI","KOSDAQ") for e in out_kr)
    # US 만
    out_us = find_entry_candidates(asof, bars_map, master, _cfg(),
                                    apply_filter_master=False, markets=("US",))
    assert all(e.market == "US" for e in out_us)
    # 전체
    out_all = find_entry_candidates(asof, bars_map, master, _cfg(), apply_filter_master=False)
    assert {e.market for e in out_all} == {"KOSPI", "US"}


def test_ranking_by_score_desc():
    """여러 후보 시 score desc 정렬 (vol_ratio × (1+amplitude))."""
    bars_a = trending_then_breakout(
        n_pre=100, pre_level=100, pre_amplitude=2,
        breakout_close=105.0, breakout_volume_mult=2.0, seed=1, ticker="A",
    )
    bars_b = trending_then_breakout(
        n_pre=100, pre_level=100, pre_amplitude=2,
        breakout_close=120.0, breakout_volume_mult=3.0, seed=2, ticker="B",
    )
    master = _master(["A", "B"])
    asof = bars_a["date"].iloc[-1]
    out = find_entry_candidates(
        asof, {"A": bars_a, "B": bars_b}, master, _cfg(), apply_filter_master=False,
    )
    assert [e.ticker for e in out] == ["B", "A"]
    assert out[0].score > out[1].score
