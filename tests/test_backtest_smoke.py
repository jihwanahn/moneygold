"""백테스트 smoke — 합성 데이터로 시뮬 흐름 + 통계 계산만 검증."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from moneygold import backtest as bt
from moneygold.config import AppConfig, KISConfig, SizingConfig, UniverseFilter, StrategyParams, NotifyConfig
from pathlib import Path


def _make_cfg() -> AppConfig:
    return AppConfig(
        kis=KISConfig(app_key="x", app_secret="x", account_no="x", account_prod_cd="01"),
        sizing=SizingConfig(default_equity_krw=10_000_000, max_risk_per_trade_pct=1.0,
                            max_position_weight_pct=20.0, max_positions=5),
        universe=UniverseFilter(liquidity_min_krw=1_000_000_000, mcap_min_krw=50_000_000_000),
        strategy=StrategyParams(
            stage2_require_inst_flow=False, rs_rank_min=70, sma200_slope_lookback=100,
            box_high_lookback=20, box_high_confirm=3, box_height_max_pct=12.0,
            box_valid_min_days=15, box_stale_days=60,
            breakout_buffer=0.003, breakout_volume_mult=1.5,
            gap_buy_on_pullback=True, fundamental_required=False,
        ),
        notify=NotifyConfig(channels=("console",), slack_webhook_url="", telegram_bot_token="", telegram_chat_id=""),
        mcp_server="x", data_dir=Path("./store"), result_dir=Path("./result"),
        log_level="INFO", timezone="Asia/Seoul", benchmark_index="KOSPI",
    )


def _make_params() -> bt.BacktestParams:
    return bt.BacktestParams(start="20260101", end="20261231", initial_equity=10_000_000.0)


# ----------------- SimPortfolio -----------------

def test_simportfolio_open_and_close_records_trade():
    params = _make_params()
    sim = bt.SimPortfolio(initial_equity=10_000_000, params=params)
    pos = sim.open(
        ticker="T1", name="X", market="KOSPI", date="20260101",
        entry_open=1000.0, stop=900.0, box_top=1000.0, box_bottom=900.0,
        desired_shares=10,
    )
    assert pos is not None
    assert sim.cash < 10_000_000   # 매수로 차감
    assert "T1" in sim.positions

    trade = sim.close(ticker="T1", date="20260201", exit_open=1200.0, reason="STOP_HIT")
    assert trade is not None
    assert trade.exit_reason == "STOP_HIT"
    assert trade.pnl_krw > 0   # 1200 > 1000
    assert "T1" not in sim.positions


def test_simportfolio_close_with_loss_negative_r():
    params = _make_params()
    sim = bt.SimPortfolio(initial_equity=10_000_000, params=params)
    sim.open(ticker="T1", name="X", market="KOSPI", date="20260101",
             entry_open=1000.0, stop=900.0, box_top=1000.0, box_bottom=900.0,
             desired_shares=10)
    # 실제 진입가는 1000 + slip + comm = ~1002
    # stop = 900, risk = 102 per share
    # 손절가 850 → 약 -152 per share = -1.5R
    trade = sim.close(ticker="T1", date="20260201", exit_open=850.0,
                      reason="STOP_HIT", forced_price=900.0)   # 갭다운으로 stop인 900에 체결
    assert trade.pnl_krw < 0
    assert trade.r_multiple < 0


def test_simportfolio_equity_includes_open_positions():
    params = _make_params()
    sim = bt.SimPortfolio(initial_equity=10_000_000, params=params)
    sim.open(ticker="T1", name="X", market="KOSPI", date="20260101",
             entry_open=1000.0, stop=900.0, box_top=1000.0, box_bottom=900.0,
             desired_shares=10)
    eq_after_buy = sim.equity({"T1": 1000.0})
    # 진입에 slip + comm 약간 손실 → 초기 자본보다 작음 (cost 부담)
    assert eq_after_buy < 10_000_000
    eq_with_gain = sim.equity({"T1": 1500.0})
    assert eq_with_gain > eq_after_buy


def test_simportfolio_can_afford_caps_shares():
    params = _make_params()
    sim = bt.SimPortfolio(initial_equity=10_000, params=params)   # 작은 자본
    pos = sim.open(ticker="T1", name="X", market="KOSPI", date="20260101",
                   entry_open=1000.0, stop=900.0, box_top=1000.0, box_bottom=900.0,
                   desired_shares=100)
    # 1만원으로 1000원짜리 100주는 못 삼. 약 9-10주 정도.
    assert pos.shares <= 10
    assert sim.cash >= 0


# ----------------- compute_stats -----------------

def test_compute_stats_basic():
    params = _make_params()
    equity_curve = pd.DataFrame({
        "date": [f"2026{(i//30)+1:02d}{(i%30)+1:02d}" for i in range(252)],
        "equity": np.linspace(10_000_000, 12_000_000, 252),
        "n_positions": [0] * 252,
        "cash": [10_000_000] * 252,
    })
    trades = []
    bench_curve = pd.DataFrame({
        "date": equity_curve["date"],
        "close": np.linspace(2000, 2200, 252),
        "benchmark_equity": np.linspace(10_000_000, 11_000_000, 252),
    })
    stats = bt.compute_stats(equity_curve, trades, bench_curve, params)
    assert stats["total_return_pct"] == pytest.approx(20.0, abs=0.1)
    assert stats["cagr_pct"] > 0
    assert stats["benchmark_total_return_pct"] == pytest.approx(10.0, abs=0.1)
    assert stats["alpha_pct"] == pytest.approx(10.0, abs=0.1)
    assert stats["n_trades"] == 0


def test_compute_stats_mdd_negative():
    params = _make_params()
    # 100 → 110 → 80 (-27%) → 90
    eq_values = [100, 105, 110, 100, 90, 80, 85, 90]
    equity_curve = pd.DataFrame({
        "date": [f"20260{i+1}01" for i in range(len(eq_values))],
        "equity": eq_values,
        "n_positions": [0] * len(eq_values),
        "cash": eq_values,
    })
    stats = bt.compute_stats(equity_curve, [], pd.DataFrame(), params)
    assert stats["mdd_pct"] < -25
    assert stats["mdd_pct"] > -28


def test_compute_stats_empty_curve():
    params = _make_params()
    stats = bt.compute_stats(pd.DataFrame(), [], pd.DataFrame(), params)
    assert stats == {}


# ----------------- run_backtest 통합 (작은 합성) -----------------

def test_run_backtest_no_signals_zero_trades():
    """평탄 데이터 → 박스 미형성 → 0 트레이드."""
    cfg = _make_cfg()
    params = bt.BacktestParams(start="20260101", end="20261231", initial_equity=10_000_000.0)

    n = 300
    dates = [f"2026{(i//30)+1:02d}{(i%30)+1:02d}" for i in range(n)]
    master = pd.DataFrame([{"ticker": "T1", "name": "X", "market": "KOSPI"}])
    bars = pd.DataFrame({
        "date": dates,
        "open": [100.0] * n, "high": [100.5] * n, "low": [99.5] * n, "close": [100.0] * n,
        "volume": [10_000_000] * n, "value": [1_000_000_000] * n,
    })
    idx = pd.Series([2000.0] * n, index=dates)

    result = bt.run_backtest(
        params, master, {"T1": bars},
        {"KOSPI": idx, "KOSPI200": idx, "KOSDAQ": idx, "KOSDAQ150": idx},
        cfg, progress=False,
    )
    assert result.stats["n_trades"] == 0
    assert result.stats["final_equity"] == pytest.approx(10_000_000.0, rel=0.001)
