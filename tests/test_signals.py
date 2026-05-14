"""signals.py — 합성 시그널 분기 + 게이트 + 우선순위."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from moneygold import signals as sg
from moneygold.config import AppConfig, KISConfig, SizingConfig, UniverseFilter, StrategyParams, NotifyConfig
from pathlib import Path


def _make_cfg(
    *,
    rs_rank_min: int = 70,
    liquidity_min_krw: int = 1_000_000_000,
    mcap_min_krw: int = 50_000_000_000,
    max_positions: int = 10,
) -> AppConfig:
    return AppConfig(
        kis=KISConfig(app_key="x", app_secret="x", account_no="x", account_prod_cd="01"),
        sizing=SizingConfig(
            default_equity_krw=10_000_000,
            max_risk_per_trade_pct=1.0,
            max_position_weight_pct=20.0,
            max_positions=max_positions,
        ),
        universe=UniverseFilter(liquidity_min_krw=liquidity_min_krw, mcap_min_krw=mcap_min_krw),
        strategy=StrategyParams(
            stage2_require_inst_flow=False,
            rs_rank_min=rs_rank_min,
            sma200_slope_lookback=22,
            stage_ma_length=150, stage_ma_type="SMA",
            stage_slope_lookback=20, stage_slope_threshold_pct=0.001, stage_band_pct=0.03,
            box_high_lookback=20, box_high_confirm=3,
            box_height_max_pct=12.0, box_valid_min_days=15, box_stale_days=60,
            breakout_buffer=0.003, breakout_volume_mult=1.5,
            gap_buy_on_pullback=True, fundamental_required=False,
        ),
        notify=NotifyConfig(channels=("console",), slack_webhook_url="", telegram_bot_token="", telegram_chat_id=""),
        mcp_server="x",
        data_dir=Path("./store"),
        result_dir=Path("./result"),
        log_level="INFO",
        timezone="Asia/Seoul",
        benchmark_index="KOSPI",
    )


def _build_uptrend_bars(n: int = 320, daily_pct: float = 0.003) -> pd.DataFrame:
    """우상향 시계열. 거래대금이 유동성 게이트(1B) 충분히 넘도록 vol 큼."""
    base = 100.0
    closes = base * (1.0 + daily_pct) ** np.arange(n)
    highs = closes * 1.005
    lows = closes * 0.995
    opens = closes
    vols = np.full(n, 100_000_000)   # 1억주
    return pd.DataFrame({
        "date": [f"2026{i//30+1:02d}{i%30+1:02d}" for i in range(n)],
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": vols,
        "value": closes * vols,
    })


def _build_breakout_bars(n: int = 320) -> pd.DataFrame:
    """장기 우상향 + 후반 박스 + 마지막 봉 돌파. 길이 정확히 n으로 보장."""
    daily = 0.003
    base = 100.0
    pre_n = n - 24   # 사전 랠리 + 박스 + 마지막
    pre_rally = list(base * (1.0 + daily) ** np.arange(pre_n))
    top = pre_rally[-1] * 1.0
    bottom = top * 0.93
    # 박스 23봉
    box_range = [(top + bottom) / 2 + ((i % 5) - 2) * (top - bottom) / 10 for i in range(23)]
    closes = pre_rally + box_range + [top * 1.02]   # 총 pre_n + 23 + 1 = n
    closes = closes[:n]
    assert len(closes) == n

    highs = [c * 1.01 for c in closes]
    highs[-1] = top * 1.025
    lows = [c * 0.99 for c in closes]
    opens = closes.copy()
    vols = [100_000_000] * (n - 1) + [300_000_000]   # 1억 → 3억

    return pd.DataFrame({
        "date": [f"2026{i//30+1:02d}{i%30+1:02d}" for i in range(n)],
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": vols,
        "value": [c * v for c, v in zip(closes, vols)],
    })


def _build_idx_series(n: int = 320, daily_pct: float = 0.0001) -> pd.Series:
    """매우 약하게 우상향. 박스 구간에서도 stock의 RS slope이 음으로 안 떨어지도록."""
    dates = [f"2026{i//30+1:02d}{i%30+1:02d}" for i in range(n)]
    return pd.Series(2000.0 * (1.0 + daily_pct) ** np.arange(n), index=dates)


# ----------------- 게이트 -----------------

def test_flagged_excluded():
    cfg = _make_cfg()
    bars = _build_uptrend_bars()
    td = sg.TickerData(ticker="T1", name="X", market="KOSPI", bars=bars, mcap=100_000_000_000, flagged=True)
    out = sg.generate_signals("99999999", [td], {}, {"T1": 90.0}, {"KOSPI": _build_idx_series()}, cfg)
    assert out.new_buys == []


def test_mcap_gate_excludes_smallcap():
    cfg = _make_cfg(mcap_min_krw=50_000_000_000)
    bars = _build_uptrend_bars()
    td = sg.TickerData(ticker="T1", name="X", market="KOSPI", bars=bars, mcap=1_000_000_000)
    out = sg.generate_signals("99999999", [td], {}, {"T1": 90.0}, {"KOSPI": _build_idx_series()}, cfg)
    assert out.new_buys == []


def test_liquidity_gate_excludes_illiquid():
    cfg = _make_cfg(liquidity_min_krw=10**13)   # 10조원 — 거의 불가능
    bars = _build_uptrend_bars()
    td = sg.TickerData(ticker="T1", name="X", market="KOSPI", bars=bars, mcap=10**12)
    out = sg.generate_signals("99999999", [td], {}, {"T1": 90.0}, {"KOSPI": _build_idx_series()}, cfg)
    assert out.new_buys == []


# ----------------- BUY 합성 -----------------

def test_buy_signal_on_breakout_with_all_conditions():
    cfg = _make_cfg()
    bars = _build_breakout_bars()
    td = sg.TickerData(ticker="T1", name="X", market="KOSPI", bars=bars, mcap=10**12)
    rs = {"T1": 90.0}
    idx = {"KOSPI": _build_idx_series()}

    out = sg.generate_signals("99999999", [td], {}, rs, idx, cfg)
    assert len(out.new_buys) == 1
    b = out.new_buys[0]
    assert b.ticker == "T1"
    assert b.entry_guide > 0
    assert b.stop > 0
    assert b.entry_guide > b.stop
    assert b.template_pass == list(b.template_pass)   # serializable list
    assert b.rs_rank == 90.0


def test_buy_skipped_when_rs_below_threshold():
    cfg = _make_cfg(rs_rank_min=70)
    bars = _build_breakout_bars()
    td = sg.TickerData(ticker="T1", name="X", market="KOSPI", bars=bars, mcap=10**12)
    out = sg.generate_signals("99999999", [td], {}, {"T1": 50.0},
                              {"KOSPI": _build_idx_series()}, cfg)
    assert out.new_buys == []


# ----------------- 보유: HOLD/SELL -----------------

def test_position_stop_hit_emits_sell():
    cfg = _make_cfg()
    bars = _build_uptrend_bars()
    bars.loc[bars.index[-1], "close"] = 50.0   # 큰 하락
    bars.loc[bars.index[-1], "open"] = 50.0
    td = sg.TickerData(ticker="T1", name="X", market="KOSPI", bars=bars, mcap=10**12)
    pos = sg.PositionMeta(ticker="T1", entry_date="20260101", entry_price=80.0, current_stop=100.0)
    out = sg.generate_signals("99999999", [td], {"T1": pos}, {"T1": 90.0},
                              {"KOSPI": _build_idx_series()}, cfg)
    assert len(out.sells) == 1
    assert out.sells[0].reason == "STOP_HIT"
    # 갭다운 라벨 (open 50 < stop 100 × 0.97 = 97)
    assert out.sells[0].label == "URGENT_GAP_DOWN"


def test_position_hold_when_trending():
    cfg = _make_cfg()
    bars = _build_uptrend_bars()
    td = sg.TickerData(ticker="T1", name="X", market="KOSPI", bars=bars, mcap=10**12)
    pos = sg.PositionMeta(ticker="T1", entry_date="20260101", entry_price=100.0,
                          current_stop=10.0)   # 매우 낮은 스톱
    out = sg.generate_signals("99999999", [td], {"T1": pos}, {"T1": 90.0},
                              {"KOSPI": _build_idx_series()}, cfg)
    assert len(out.sells) == 0
    assert len(out.holds) == 1


def test_position_excluded_from_new_buys_no_pyramiding():
    cfg = _make_cfg()
    bars = _build_breakout_bars()
    td = sg.TickerData(ticker="T1", name="X", market="KOSPI", bars=bars, mcap=10**12)
    pos = sg.PositionMeta(ticker="T1", entry_date="20260101", entry_price=100.0, current_stop=10.0)
    out = sg.generate_signals("99999999", [td], {"T1": pos}, {"T1": 90.0},
                              {"KOSPI": _build_idx_series()}, cfg)
    # 이미 보유 중 → new_buys에 안 들어감
    assert out.new_buys == []
    # HOLD로 들어감
    assert len(out.holds) == 1


# ----------------- 사이즈 / tick rounding -----------------

def test_suggest_size_basic():
    sizing = type("S", (), {
        "default_equity_krw": 10_000_000,
        "max_risk_per_trade_pct": 1.0,
        "max_position_weight_pct": 20.0,
    })()
    shares, notional = sg._suggest_size(entry=1000.0, risk_per_share=100.0, sizing=sizing)
    # max_loss = 100,000원, risk 100원 → 1000주
    # cap by weight = 20% × 10M = 2M, /1000 = 2000주
    # min(1000, 2000) = 1000
    assert shares == 1000
    assert notional == 1_000_000


def test_tick_round_kospi_kosdaq():
    # 가격대별 tick
    assert sg._tick_round(1234.7, "KOSPI") == 1234.0     # <2000 tick=1
    assert sg._tick_round(3456.0, "KOSPI") == 3455.0     # <5000 tick=5
    assert sg._tick_round(12345.0, "KOSDAQ") == 12340.0  # <20000 tick=10
    assert sg._tick_round(34567.0, "KOSPI") == 34550.0   # <50000 tick=50
    assert sg._tick_round(123456.0, "KOSPI") == 123400.0 # <200k tick=100
    assert sg._tick_round(345678.0, "KOSPI") == 345500.0 # <500k tick=500
    assert sg._tick_round(1234567.0, "KOSPI") == 1234000.0  # >=500k tick=1000
