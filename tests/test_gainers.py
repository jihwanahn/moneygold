"""gainers.py: 일일 상승 종목 추출."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from moneygold import gainers
from moneygold.data import store


def _write_bars(data_dir: Path, ticker: str, closes: dict[str, float]) -> None:
    """{date: close} dict로 bars parquet 생성."""
    df = pd.DataFrame({
        "ticker": [ticker] * len(closes),
        "date": list(closes.keys()),
        "open": list(closes.values()),
        "high": list(closes.values()),
        "low": list(closes.values()),
        "close": list(closes.values()),
        "volume": [1000] * len(closes),
        "value": [1_000_000] * len(closes),
        "adj_factor": [1.0] * len(closes),
    })
    store.write_parquet_atomic(df, store.bars_path(data_dir, ticker))


def _write_master(data_dir: Path, rows: list[dict]) -> None:
    df = pd.DataFrame(rows)
    store.write_parquet_atomic(df, store.master_path(data_dir))


# --- _last_two_closes ---------------------------------------------------------

def test_last_two_closes_basic():
    bars = pd.DataFrame({
        "date": ["20260514", "20260515"],
        "close": [100.0, 105.0],
    })
    prev, today = gainers._last_two_closes(bars, "20260515")
    assert prev == 100.0
    assert today == 105.0


def test_last_two_closes_respects_asof():
    """asof 이전까지만 봐야 함 (재현성)."""
    bars = pd.DataFrame({
        "date": ["20260513", "20260514", "20260515"],
        "close": [90.0, 100.0, 200.0],
    })
    prev, today = gainers._last_two_closes(bars, "20260514")
    assert prev == 90.0
    assert today == 100.0  # 200.0은 asof 이후라 무시


def test_last_two_closes_too_few_bars():
    bars = pd.DataFrame({"date": ["20260515"], "close": [100.0]})
    assert gainers._last_two_closes(bars, "20260515") is None


def test_last_two_closes_zero_prev_close():
    bars = pd.DataFrame({
        "date": ["20260514", "20260515"],
        "close": [0.0, 100.0],
    })
    assert gainers._last_two_closes(bars, "20260515") is None


def test_last_two_closes_empty():
    assert gainers._last_two_closes(pd.DataFrame(), "20260515") is None
    assert gainers._last_two_closes(None, "20260515") is None  # type: ignore[arg-type]


# --- daily_gainers ------------------------------------------------------------

def test_daily_gainers_filters_by_min_pct(tmp_path: Path):
    _write_master(tmp_path, [
        {"ticker": "AAA", "market": "US", "name": "AAA Corp"},
        {"ticker": "BBB", "market": "US", "name": "BBB Inc"},
        {"ticker": "CCC", "market": "US", "name": "CCC Ltd"},
    ])
    _write_bars(tmp_path, "AAA", {"20260514": 100.0, "20260515": 105.0})  # +5%
    _write_bars(tmp_path, "BBB", {"20260514": 100.0, "20260515": 100.5})  # +0.5%
    _write_bars(tmp_path, "CCC", {"20260514": 100.0, "20260515": 90.0})   # -10%

    out = gainers.daily_gainers(tmp_path, asof="20260515", min_pct=0.01)
    assert list(out["ticker"]) == ["AAA"]
    assert abs(out["pct_chg"].iloc[0] - 0.05) < 1e-9


def test_daily_gainers_sorted_desc(tmp_path: Path):
    _write_master(tmp_path, [
        {"ticker": "AAA", "market": "US", "name": ""},
        {"ticker": "BBB", "market": "US", "name": ""},
        {"ticker": "CCC", "market": "US", "name": ""},
    ])
    _write_bars(tmp_path, "AAA", {"20260514": 100.0, "20260515": 103.0})   # +3%
    _write_bars(tmp_path, "BBB", {"20260514": 100.0, "20260515": 110.0})   # +10%
    _write_bars(tmp_path, "CCC", {"20260514": 100.0, "20260515": 105.0})   # +5%

    out = gainers.daily_gainers(tmp_path, asof="20260515", min_pct=0.01)
    assert list(out["ticker"]) == ["BBB", "CCC", "AAA"]


def test_daily_gainers_market_filter(tmp_path: Path):
    _write_master(tmp_path, [
        {"ticker": "AAA", "market": "US", "name": ""},
        {"ticker": "005930", "market": "KOSPI", "name": "삼성전자"},
    ])
    _write_bars(tmp_path, "AAA", {"20260514": 100.0, "20260515": 105.0})
    _write_bars(tmp_path, "005930", {"20260514": 70000.0, "20260515": 73500.0})

    us = gainers.daily_gainers(tmp_path, asof="20260515", min_pct=0.01, market="US")
    assert list(us["ticker"]) == ["AAA"]

    kr = gainers.daily_gainers(tmp_path, asof="20260515", min_pct=0.01, market="KOSPI")
    assert list(kr["ticker"]) == ["005930"]


def test_daily_gainers_handles_missing_bars(tmp_path: Path):
    """master에 있지만 bars 파일이 없는 ticker는 조용히 스킵."""
    _write_master(tmp_path, [
        {"ticker": "AAA", "market": "US", "name": ""},
        {"ticker": "NOBARS", "market": "US", "name": ""},
    ])
    _write_bars(tmp_path, "AAA", {"20260514": 100.0, "20260515": 105.0})

    out = gainers.daily_gainers(tmp_path, asof="20260515", min_pct=0.01)
    assert list(out["ticker"]) == ["AAA"]


def test_daily_gainers_empty_master(tmp_path: Path):
    """master.parquet 없으면 빈 DataFrame."""
    out = gainers.daily_gainers(tmp_path, asof="20260515", min_pct=0.01)
    assert out.empty
    assert list(out.columns) == ["ticker", "market", "name", "close", "prev_close", "pct_chg"]


def test_attach_stage_unknown_for_short_bars(tmp_path: Path):
    """봉이 부족하면 stage=UNKNOWN(0)."""
    _write_master(tmp_path, [{"ticker": "AAA", "market": "US", "name": "AAA"}])
    _write_bars(tmp_path, "AAA", {"20260514": 100.0, "20260515": 105.0})

    g = gainers.daily_gainers(tmp_path, asof="20260515", min_pct=0.01)
    out = gainers.attach_stage(g, tmp_path, asof="20260515")
    assert out["stage"].iloc[0] == 0
    assert out["stage_name"].iloc[0] == "UNKNOWN"


def test_attach_stage_advancing_uptrend(tmp_path: Path):
    """SMA 위 + 상승 추세 → Stage 2."""
    n = 200
    closes = {f"2025{i:04d}": 100.0 + i * 0.5 for i in range(n)}

    _write_master(tmp_path, [{"ticker": "UPONLY", "market": "US", "name": "Up Only"}])
    _write_bars(tmp_path, "UPONLY", closes)

    # 마지막 봉 = asof. 직전봉 대비 +0.5/199.5 ≈ 0.25%, min_pct=0으로 통과
    last_date = f"2025{n-1:04d}"
    g = gainers.daily_gainers(tmp_path, asof=last_date, min_pct=0.0)
    assert "UPONLY" in g["ticker"].values
    out = gainers.attach_stage(g, tmp_path, asof=last_date)
    # 단조 상승 트렌드 + SMA150 위 → Stage 2 (ADVANCING)
    assert out["stage"].iloc[0] == 2


def test_stage_distribution_all_codes_present():
    """0건이어도 모든 stage 코드 행 유지."""
    series = pd.Series([2, 2, 2, 4, 4])
    out = gainers.stage_distribution(series)
    assert set(out["stage"]) == {0, 1, 2, 3, 4}
    by_stage = out.set_index("stage")["count"].to_dict()
    assert by_stage[2] == 3
    assert by_stage[4] == 2
    assert by_stage[0] == 0
    assert by_stage[1] == 0
    assert by_stage[3] == 0
    # pct 합 = 100
    assert abs(out["pct"].sum() - 100.0) < 1e-9


def test_daily_gainers_asof_reproducibility(tmp_path: Path):
    """과거 asof로 호출 시 그 시점 기준으로 계산 (오늘 데이터 무시)."""
    _write_master(tmp_path, [{"ticker": "AAA", "market": "US", "name": ""}])
    _write_bars(tmp_path, "AAA", {
        "20260513": 90.0,
        "20260514": 100.0,
        "20260515": 200.0,
    })

    # 20260514 기준: prev=90, today=100, +11.1%
    out = gainers.daily_gainers(tmp_path, asof="20260514", min_pct=0.05)
    assert list(out["ticker"]) == ["AAA"]
    assert abs(out["pct_chg"].iloc[0] - (100.0 / 90.0 - 1)) < 1e-9
