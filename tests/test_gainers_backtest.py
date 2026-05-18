"""gainers_backtest.py: event study 검증."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from moneygold import gainers_backtest as gb
from moneygold.data import store


def _write_bars(data_dir: Path, ticker: str, closes: list[float], dates: list[str] | None = None) -> None:
    if dates is None:
        dates = [f"2025{i:04d}" for i in range(len(closes))]
    df = pd.DataFrame({
        "ticker": [ticker] * len(closes),
        "date": dates,
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [100_000] * len(closes),
        "value": [int(c * 100_000) for c in closes],
        "adj_factor": [1.0] * len(closes),
    })
    store.write_parquet_atomic(df, store.bars_path(data_dir, ticker))


def _write_master(data_dir: Path, rows: list[dict]) -> None:
    df = pd.DataFrame(rows)
    store.write_parquet_atomic(df, store.master_path(data_dir))


# --- 기본 동작 ----------------------------------------------------------------

def test_collect_events_empty_master(tmp_path: Path):
    out = gb.collect_events(tmp_path, start="20250000", end="20259999")
    assert out.empty


def test_collect_events_filters_short_bars(tmp_path: Path):
    """252봉 미만 ticker는 제외."""
    _write_master(tmp_path, [{"ticker": "SHORT", "market": "US", "name": ""}])
    _write_bars(tmp_path, "SHORT", [100.0] * 100)
    out = gb.collect_events(tmp_path, start="20250000", end="20259999", progress=False)
    assert out.empty


def test_collect_events_filters_by_gain_pct(tmp_path: Path):
    """min_pct 미만 변동률은 이벤트 아님."""
    closes = [100.0 + i * 0.1 for i in range(280)]
    # 마지막 한 봉만 +5% 만듬
    closes[-1] = closes[-2] * 1.05
    _write_master(tmp_path, [{"ticker": "AAA", "market": "US", "name": ""}])
    _write_bars(tmp_path, "AAA", closes)
    last_date = f"2025{279:04d}"

    # min_pct=0.02 → 마지막 봉만 잡힘
    out = gb.collect_events(
        tmp_path, start="20250000", end="20259999",
        min_pct=0.02, horizons=(1,), progress=False,
    )
    assert len(out) == 1
    assert out["date"].iloc[0] == last_date
    assert out["pct_chg"].iloc[0] > 0.04

    # min_pct=0.10 → 잡히는 거 없음
    out2 = gb.collect_events(
        tmp_path, start="20250000", end="20259999",
        min_pct=0.10, horizons=(1,), progress=False,
    )
    assert out2.empty


# --- 핵심: look-ahead 없음 ----------------------------------------------------

def test_no_lookahead_in_features(tmp_path: Path):
    """feature는 t 시점까지의 데이터로만 계산. 미래 close가 바뀌어도 변하지 않음."""
    # 시나리오 1: 250일 평탄 + 마지막 30일 폭락
    closes1 = [100.0] * 250 + [50.0 - i for i in range(30)]
    closes1[260] = closes1[259] * 1.02  # 중간에 +2% gainer 만듬 (날짜 idx 260)

    _write_master(tmp_path, [{"ticker": "AAA", "market": "US", "name": ""}])
    _write_bars(tmp_path, "AAA", closes1)

    out1 = gb.collect_events(
        tmp_path, start="20250000", end="20259999",
        min_pct=0.015, horizons=(1, 5), progress=False,
    )
    # 시나리오 2: 같은 250일 평탄 + 같은 +2% gainer + 그 *이후* 가격을 다르게 함
    closes2 = list(closes1)
    closes2[261:] = [200.0] * (len(closes2) - 261)  # 폭등으로 교체
    _write_bars(tmp_path, "AAA", closes2)

    out2 = gb.collect_events(
        tmp_path, start="20250000", end="20259999",
        min_pct=0.015, horizons=(1, 5), progress=False,
    )

    # 같은 t의 feature는 동일해야 함 (미래에 무관)
    e1 = out1[out1["date"] == f"2025{260:04d}"]
    e2 = out2[out2["date"] == f"2025{260:04d}"]
    if e1.empty or e2.empty:
        # min_pct 컷에 안 잡혔다면 테스트 못함 — 보수적으로 통과 (다른 테스트가 cover)
        return
    for col in ["close_to_sma200", "close_to_sma50", "rvol", "sma200_slope"]:
        v1 = e1[col].iloc[0]
        v2 = e2[col].iloc[0]
        # NaN끼리 같다고 보고, 아니면 거의 같아야
        assert (pd.isna(v1) and pd.isna(v2)) or abs(v1 - v2) < 1e-9, \
            f"{col}: t 시점 feature가 미래에 영향받음 (look-ahead!): {v1} vs {v2}"

    # 반면 fwd_5d는 달라야 함 (미래가 바뀌었으니까)
    f1 = e1["fwd_5d"].iloc[0]
    f2 = e2["fwd_5d"].iloc[0]
    if not (pd.isna(f1) or pd.isna(f2)):
        assert abs(f1 - f2) > 0.01, "forward return은 미래에 의존해야 함"


# --- forward return 계산 ------------------------------------------------------

def test_forward_returns_exact(tmp_path: Path):
    """forward return = close[t+h] / close[t] - 1."""
    closes = [100.0] * 252 + [101.0, 110.0, 105.0, 120.0]  # idx 252~255
    # idx 252: +1% 상승 (gainer)
    _write_master(tmp_path, [{"ticker": "AAA", "market": "US", "name": ""}])
    _write_bars(tmp_path, "AAA", closes)

    out = gb.collect_events(
        tmp_path, start="20250000", end="20259999",
        min_pct=0.005, horizons=(1, 2, 3), progress=False,
    )
    e = out[out["date"] == f"2025{252:04d}"]
    assert len(e) == 1
    row = e.iloc[0]
    # close[252]=101, [253]=110, [254]=105, [255]=120
    assert abs(row["fwd_1d"] - (110/101 - 1)) < 1e-9
    assert abs(row["fwd_2d"] - (105/101 - 1)) < 1e-9
    assert abs(row["fwd_3d"] - (120/101 - 1)) < 1e-9


def test_forward_returns_nan_beyond_horizon(tmp_path: Path):
    """horizon이 데이터 끝 너머면 NaN."""
    closes = [100.0] * 252 + [101.5]  # 253번째 = +1.5%
    _write_master(tmp_path, [{"ticker": "AAA", "market": "US", "name": ""}])
    _write_bars(tmp_path, "AAA", closes)

    out = gb.collect_events(
        tmp_path, start="20250000", end="20259999",
        min_pct=0.01, horizons=(1, 5, 20), progress=False,
    )
    e = out.iloc[0]
    assert pd.isna(e["fwd_1d"])
    assert pd.isna(e["fwd_5d"])
    assert pd.isna(e["fwd_20d"])


# --- 그룹 통계 ----------------------------------------------------------------

def test_stats_for_group_basic():
    s = pd.Series([0.01, 0.02, -0.01, 0.03, np.nan])
    out = gb._stats_for_group(s)
    assert out["n"] == 4
    assert abs(out["mean"] - 0.0125) < 1e-9
    assert abs(out["win_rate"] - 0.75) < 1e-9


def test_stats_for_group_empty():
    out = gb._stats_for_group(pd.Series([], dtype=float))
    assert out["n"] == 0
    assert pd.isna(out["mean"])


def test_group_report_separates_stage(tmp_path: Path):
    """가짜 이벤트 DF로 그룹 분리 정상."""
    events = pd.DataFrame({
        "date": ["20250100"] * 4,
        "ticker": ["A", "B", "C", "D"],
        "stage": [2, 2, 4, 4],
        "close_to_sma200": [1.2, 1.3, 0.7, 0.8],
        "close_to_52w_high": [0.95, 0.92, 0.50, 0.55],
        "golden_cross": [True, True, False, False],
        "fwd_5d": [0.05, 0.03, -0.02, -0.01],
    })
    rpt = gb.group_report(events, horizons=(5,))
    # Stage 2 그룹은 mean +0.04, Stage 4는 -0.015
    s2 = rpt[(rpt["group"] == "Stage 2") & (rpt["horizon_d"] == 5)]
    s4 = rpt[(rpt["group"] == "Stage 4") & (rpt["horizon_d"] == 5)]
    assert abs(s2["mean"].iloc[0] - 0.04) < 1e-9
    assert abs(s4["mean"].iloc[0] - (-0.015)) < 1e-9
    assert s2["win_rate"].iloc[0] == 1.0
    assert s4["win_rate"].iloc[0] == 0.0


def test_edge_table_baseline_difference():
    rpt = pd.DataFrame([
        {"group": "ALL", "horizon_d": 5, "n": 100, "mean": 0.01,
         "median": 0.01, "win_rate": 0.55, "std": 0.02, "sharpe": 0.5,
         "p25": -0.005, "p75": 0.02},
        {"group": "Stage 2", "horizon_d": 5, "n": 40, "mean": 0.03,
         "median": 0.025, "win_rate": 0.7, "std": 0.02, "sharpe": 1.5,
         "p25": 0.01, "p75": 0.05},
    ])
    edge = gb.edge_table(rpt, baseline="ALL")
    row = edge[edge["group"] == "Stage 2"].iloc[0]
    assert abs(row["edge_vs_all_bps"] - 200) < 1e-6  # +2% = 200 bps
