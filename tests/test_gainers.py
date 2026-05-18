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


# --- attach_features ----------------------------------------------------------

def _write_uptrend_bars(data_dir: Path, ticker: str, n: int = 300, start: float = 100.0, slope: float = 0.5) -> None:
    """선형 상승 추세 봉 n개. 마지막 봉이 가장 큼."""
    rows = []
    for i in range(n):
        price = start + i * slope
        rows.append({
            "ticker": ticker,
            "date": f"2025{i:04d}",
            "open": price, "high": price * 1.005, "low": price * 0.995,
            "close": price,
            "volume": 100_000 + (i % 5) * 1000,
            "value": int(price * 100_000),
            "adj_factor": 1.0,
        })
    df = pd.DataFrame(rows)
    store.write_parquet_atomic(df, store.bars_path(data_dir, ticker))


def _write_downtrend_bars(data_dir: Path, ticker: str, n: int = 300, start: float = 200.0, slope: float = -0.4) -> None:
    """선형 하락 추세 봉 n개. 마지막 봉은 살짝 위로 튐(데드캣 시뮬)."""
    rows = []
    for i in range(n):
        price = start + i * slope
        if i == n - 1:
            # 마지막 봉: 직전 대비 +3%
            prev = rows[-1]["close"]
            price = prev * 1.03
        rows.append({
            "ticker": ticker,
            "date": f"2025{i:04d}",
            "open": price, "high": price * 1.005, "low": price * 0.995,
            "close": price,
            "volume": 100_000,
            "value": int(price * 100_000),
            "adj_factor": 1.0,
        })
    df = pd.DataFrame(rows)
    store.write_parquet_atomic(df, store.bars_path(data_dir, ticker))


def test_attach_features_uptrend_signatures(tmp_path: Path):
    """SMA200 위, 52w 고가 근처, golden cross — Stage 2 시그너처."""
    _write_uptrend_bars(tmp_path, "UP")
    _write_master(tmp_path, [{"ticker": "UP", "market": "US", "name": "Up Co"}])
    g = gainers.daily_gainers(tmp_path, asof="20250299", min_pct=0.0)
    out = gainers.attach_features(g, tmp_path, asof="20250299")
    row = out.iloc[0]
    assert row["close_to_sma200"] > 1.10
    assert row["close_to_52w_high"] > 0.98  # 단조 상승 → 마지막 봉이 52w 고가
    assert row["sma50_over_sma200"] > 1.0
    assert bool(row["golden_cross"]) is True
    assert row["sma200_slope"] > 0


def test_attach_features_downtrend_with_bounce(tmp_path: Path):
    """SMA 우하향 + 마지막에 튐 — Stage 4 데드캣 시그너처."""
    _write_downtrend_bars(tmp_path, "DOWN")
    _write_master(tmp_path, [{"ticker": "DOWN", "market": "US", "name": "Down Co"}])
    g = gainers.daily_gainers(tmp_path, asof="20250299", min_pct=0.01)
    out = gainers.attach_features(g, tmp_path, asof="20250299")
    row = out.iloc[0]
    # 단조 하락 + 마지막만 튐 → 종가는 SMA200 아래
    assert row["close_to_sma200"] < 1.0
    # 52w 고가는 한참 위
    assert row["close_to_52w_high"] < 0.8
    # Dead cross
    assert row["sma50_over_sma200"] < 1.0
    assert bool(row["golden_cross"]) is False
    assert row["sma200_slope"] < 0


def test_attach_features_short_bars_returns_nan(tmp_path: Path):
    """252봉 미만이면 모든 feature NaN."""
    closes = {f"2025{i:04d}": 100.0 + i for i in range(100)}
    _write_master(tmp_path, [{"ticker": "SHORT", "market": "US", "name": ""}])
    _write_bars(tmp_path, "SHORT", closes)
    g = gainers.daily_gainers(tmp_path, asof=f"2025{99:04d}", min_pct=0.0)
    out = gainers.attach_features(g, tmp_path, asof=f"2025{99:04d}")
    assert pd.isna(out["close_to_sma200"].iloc[0])
    assert bool(out["golden_cross"].iloc[0]) is False


def test_attach_features_new_columns_present(tmp_path: Path):
    """PR16 신규 feature 4개가 모두 attach 되는지."""
    _write_uptrend_bars(tmp_path, "UP")
    _write_master(tmp_path, [{"ticker": "UP", "market": "US", "name": ""}])
    g = gainers.daily_gainers(tmp_path, asof="20250299", min_pct=0.0)
    out = gainers.attach_features(g, tmp_path, asof="20250299")
    for col in ["rsi_14", "bb_position", "atr_normalized_move", "pullback_from_50d_high"]:
        assert col in out.columns, f"missing {col}"
        # 단조 상승 ticker는 모든 값 정의되어야 함
        assert not pd.isna(out[col].iloc[0]), f"{col} NaN on uptrend"


def test_attach_features_rsi_high_on_uptrend(tmp_path: Path):
    """단조 상승 → RSI 거의 100."""
    _write_uptrend_bars(tmp_path, "UP")
    _write_master(tmp_path, [{"ticker": "UP", "market": "US", "name": ""}])
    g = gainers.daily_gainers(tmp_path, asof="20250299", min_pct=0.0)
    out = gainers.attach_features(g, tmp_path, asof="20250299")
    assert out["rsi_14"].iloc[0] > 90


def test_attach_features_pullback_zero_on_uptrend(tmp_path: Path):
    """단조 상승 → 50d 고가 = 오늘 → pullback ≈ 0."""
    _write_uptrend_bars(tmp_path, "UP")
    _write_master(tmp_path, [{"ticker": "UP", "market": "US", "name": ""}])
    g = gainers.daily_gainers(tmp_path, asof="20250299", min_pct=0.0)
    out = gainers.attach_features(g, tmp_path, asof="20250299")
    assert abs(out["pullback_from_50d_high"].iloc[0]) < 1e-6


def test_compute_alpha_score_us_pullback_drives_score():
    """US 시장: Pullback ≥30% 단독으로 30~50점."""
    df = pd.DataFrame({
        "market": ["US", "US", "US"],
        "pullback_from_50d_high": [0.0, 0.30, 0.50],
        "atr_normalized_move": [1.0, 1.0, 1.0],
        "bb_position": [0.5, 0.5, 0.5],
        "rsi_14": [50.0, 50.0, 50.0],
        "stage": [2, 2, 2],
        "golden_cross": [True, True, True],
        "close_to_52w_high": [1.0, 1.0, 1.0],
    })
    s = gainers.compute_alpha_score(df)
    assert s.iloc[0] == 0.0
    assert abs(s.iloc[1] - 30.0) < 0.1   # 30% pullback * 100 = 30
    assert abs(s.iloc[2] - 50.0) < 0.1   # 50% pullback × 100 = 50 (cap)


def test_compute_alpha_score_us_best_combo_high():
    """US: Pullback 50% + ATR-norm <0.5 + BB<0.3 + RSI<30 = 50+20+10+10 = 90."""
    df = pd.DataFrame({
        "market": ["US"],
        "pullback_from_50d_high": [0.5],
        "atr_normalized_move": [0.3],
        "bb_position": [0.2],
        "rsi_14": [25.0],
    })
    s = gainers.compute_alpha_score(df)
    assert abs(s.iloc[0] - 90.0) < 0.1


def test_compute_alpha_score_kr_pullback_penalty():
    """KR 시장: Pullback ≥30%는 *페널티* (백테스트 검증)."""
    df = pd.DataFrame({
        "market": ["KOSPI", "KOSPI"],
        "pullback_from_50d_high": [0.0, 0.50],
        "stage": [2, 2],
        "golden_cross": [True, True],
        "close_to_52w_high": [1.0, 1.0],
        "rsi_14": [50.0, 50.0],
        "atr_normalized_move": [1.0, 1.0],
    })
    s = gainers.compute_alpha_score(df)
    # 0% pullback: Stage 2 (25) + GC (15) + 52w high (30) = 70
    # 50% pullback: 같은 + 페널티 -25 = 45
    assert abs(s.iloc[0] - 70.0) < 0.1
    assert s.iloc[1] < s.iloc[0]
    assert abs(s.iloc[1] - 45.0) < 0.1


def test_compute_alpha_score_kr_stage2_momentum():
    """KR: Stage 2 + Golden cross + 52w high near = 25+15+30 = 70."""
    df = pd.DataFrame({
        "market": ["KOSDAQ"],
        "stage": [2],
        "golden_cross": [True],
        "close_to_52w_high": [1.0],
        "rsi_14": [50.0],
        "atr_normalized_move": [1.0],
        "pullback_from_50d_high": [0.0],
    })
    s = gainers.compute_alpha_score(df)
    assert abs(s.iloc[0] - 70.0) < 0.1


def test_compute_alpha_score_clipped_0_100():
    """모든 보너스 + 모든 페널티 → 0~100 사이로 clip."""
    # KR 페널티 극단
    df = pd.DataFrame({
        "market": ["KOSPI"],
        "stage": [0],
        "golden_cross": [False],
        "close_to_52w_high": [0.0],
        "rsi_14": [50.0],
        "atr_normalized_move": [1.0],
        "pullback_from_50d_high": [1.0],
    })
    s = gainers.compute_alpha_score(df)
    assert 0 <= s.iloc[0] <= 100


def test_compute_alpha_score_empty_df():
    s = gainers.compute_alpha_score(pd.DataFrame())
    assert s.empty


def test_compute_alpha_score_no_market_column_treats_as_kr():
    """market 컬럼 없으면 모두 KR로 처리."""
    df = pd.DataFrame({
        "stage": [2],
        "golden_cross": [True],
        "close_to_52w_high": [1.0],
        "rsi_14": [50.0],
        "atr_normalized_move": [1.0],
        "pullback_from_50d_high": [0.0],
    })
    s = gainers.compute_alpha_score(df)
    # KR scoring = 70
    assert abs(s.iloc[0] - 70.0) < 0.1


def test_signature_table_groups_by_stage(tmp_path: Path):
    """signature_table — Stage 코드별 median 분리."""
    df = pd.DataFrame({
        "ticker": ["A", "B", "C", "D"],
        "stage": [2, 2, 4, 4],
        "rvol": [1.0, 2.0, 0.5, 1.5],
        "close_to_sma200": [1.2, 1.3, 0.7, 0.8],
        "close_to_52w_high": [0.9, 0.95, 0.5, 0.6],
        "close_to_sma50": [1.0, 1.1, 0.9, 1.0],
        "close_to_sma150": [1.1, 1.2, 0.8, 0.9],
        "close_to_52w_low": [1.5, 1.7, 1.1, 1.2],
        "sma50_over_sma200": [1.1, 1.2, 0.8, 0.85],
        "sma200_slope": [0.001, 0.002, -0.001, -0.002],
    })
    out = gainers.signature_table(df)
    assert set(out.columns) == {2, 4}
    assert abs(out.loc["close_to_sma200", 2] - 1.25) < 1e-9
    assert abs(out.loc["close_to_sma200", 4] - 0.75) < 1e-9
    assert out.loc["close_to_sma200", 2] > out.loc["close_to_sma200", 4]


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
