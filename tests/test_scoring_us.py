"""US DGI scoring_us 통합 테스트 — 합성 parquet/json을 디스크에 써서 score_ticker_us 검증."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from moneygold.data import store
from moneygold.data import us_dividends as ud
from moneygold.fundamentals import financials_path
from moneygold.strategies.value_long_term import scoring_us


def _bars(n=1400, start=100.0, daily=0.0006, start_date="20200601"):
    dates = pd.date_range(start_date, periods=n, freq="B").strftime("%Y%m%d").tolist()
    closes = start * (1.0 + daily) ** np.arange(n)
    return pd.DataFrame({
        "ticker": "T", "date": dates, "open": closes, "high": closes * 1.01,
        "low": closes * 0.99, "close": closes, "volume": 1000, "value": 100000,
    })


def _write_us_dividends(data_dir, ticker, year_dps: dict[int, float], asof):
    rows = []
    for y, dps in year_dps.items():
        rows.append({
            "ticker": ticker, "record_date": f"{y}1231", "divi_kind": "결산",
            "per_sto_divi_amt": float(dps), "divi_rate_pct": float("nan"),
            "stk_divi_rate_pct": 0.0, "divi_pay_dt": "", "stk_kind": "보통",
            "fetched_at": asof, "fiscal_year": y,
        })
    df = pd.DataFrame(rows, columns=ud.DIV_COLUMNS)
    df["fiscal_year"] = df["fiscal_year"].astype("Int64")
    store.write_parquet_atomic(df, ud.us_dividends_path(data_dir, ticker))


def _write_us_info(data_dir, ticker, info):
    path = ud.us_info_path(data_dir, ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(info, f)


def test_score_ticker_us_dividend_king(tmp_path):
    """50년+ 인상 King + 안정 ROE/EPS + 우상향 → A/B 등급."""
    tk = "KO"
    # bars: 5+ years uptrend
    b = _bars(n=1400, start=40.0, daily=0.0005, start_date="20200601")
    b["ticker"] = tk
    store.write_parquet_atomic(b, store.bars_path(tmp_path, tk))
    # dividends: 매년 증가 (2010~2024, 15년 → 실제 King은 50년이지만 데이터 범위 한정)
    yd = {y: 1.0 * (1.06 ** (y - 2010)) for y in range(2010, 2025)}
    _write_us_dividends(tmp_path, tk, yd, "20260528")
    # info: yield 3%, payout 60%, ROE 40%
    _write_us_info(tmp_path, tk, {"dividend_yield_pct": 3.0, "payout_ratio_pct": 60.0,
                                   "roe_pct": 40.0})
    # financials: 안정 EPS
    fin = pd.DataFrame({"year": [2020]*4 + [2021]*4 + [2022]*4 + [2023]*4 + [2024]*4,
                        "q": [1, 2, 3, 4] * 5, "eps": [0.5] * 20})
    store.write_parquet_atomic(fin, financials_path(tmp_path, tk))

    sb = scoring_us.score_ticker_us(tk, asof="20260101", data_dir=tmp_path, name="Coca-Cola")
    # yield는 배당이력+현재가로 직접 계산 우선 (info 3.0 fallback 아님). 양수·합리적 범위.
    assert sb.dividend_yield_pct is not None and 0 < sb.dividend_yield_pct < 10
    assert sb.consecutive_increase_years == 14   # 2010→2024 (2024 직전 완결까지)
    assert sb.dps_cagr_5y_pct is not None and sb.dps_cagr_5y_pct > 5
    assert sb.roe_pct == 40.0 and sb.score_roe == 8
    assert sb.score_consec == 12                 # 14년 → 12점 (10~15 구간)
    # 14년 → Contender(10년+) → 3점
    assert sb.score_aristocrat == 3
    assert sb.aristocrat_label == "Contender (10년+)"
    assert sb.total > 50
    assert sb.grade in ("A", "B", "C")
    # 카테고리 합 = total
    assert (sb.dividend_total + sb.capital_total
            + sb.fundamental_total + sb.aristocrat_total) == sb.total


def test_score_ticker_us_no_data_graceful(tmp_path):
    sb = scoring_us.score_ticker_us("NONE", asof="20260528", data_dir=tmp_path)
    assert sb.total == 0
    assert sb.grade == "C"
    assert "배당 이력 없음" in "; ".join(sb.notes)


def test_score_ticker_us_yield_fallback_to_computed(tmp_path):
    """info에 yield 없으면 배당이력/현재가로 계산."""
    tk = "X"
    b = _bars(n=300, start=100.0, daily=0.0, start_date="20240101")
    b["ticker"] = tk
    store.write_parquet_atomic(b, store.bars_path(tmp_path, tk))
    # 직전 완결연도(2024) DPS=3, 현재가 100 → yield 3%
    _write_us_dividends(tmp_path, tk, {2023: 2.8, 2024: 3.0}, "20260528")
    _write_us_info(tmp_path, tk, {"payout_ratio_pct": 50.0})  # yield 없음
    sb = scoring_us.score_ticker_us(tk, asof="20250601", data_dir=tmp_path)
    assert sb.dividend_yield_pct is not None
    assert abs(sb.dividend_yield_pct - 3.0) < 0.1


def test_screen_us_sorts_and_saves(tmp_path):
    for tk, daily in [("AAA", 0.0008), ("BBB", 0.0002)]:
        b = _bars(n=1400, start=50.0, daily=daily, start_date="20200601")
        b["ticker"] = tk
        store.write_parquet_atomic(b, store.bars_path(tmp_path, tk))
        _write_us_dividends(tmp_path, tk, {y: 1.0*(1.08**(y-2015)) for y in range(2015, 2025)},
                            "20260528")
        _write_us_info(tmp_path, tk, {"dividend_yield_pct": 2.5, "payout_ratio_pct": 55.0,
                                       "roe_pct": 25.0})
    df = scoring_us.screen_us(["AAA", "BBB"], "20260101", tmp_path)
    assert len(df) == 2
    # total 내림차순
    assert df.iloc[0]["total"] >= df.iloc[1]["total"]
    # 저장
    path = scoring_us.save_us_scores(df, tmp_path, "20260101")
    assert path.exists()
    loaded = store.read_parquet_safe(path)
    assert len(loaded) == 2
