"""US DGI score_ticker — 한 미국 종목의 가속화 장기투자 점수.

KR(`scoring.py`)과 *동일한 순수 feature 함수*를 재사용하되, 데이터 소스와 점수 임계값만
US용으로 교체:
  - 배당:     store/us_dividends/{ticker}.parquet (yfinance, DIV_COLUMNS 스키마)
  - yield/payout/ROE: store/us_info/{ticker}.json (yfinance info)
  - 일봉:     store/bars/{ticker}.parquet (yfinance, 5년 backfill 필요)
  - EPS:      store/financials/{ticker}.parquet (SEC EDGAR XBRL)
  - 점수표:   scoring_rules_us (US 튜닝) — 주주환원 대신 배당귀족 지위
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from ...data import store
from ...data import us_dividends as ud
from ...fundamentals import financials_path
from . import scoring  # 순수 feature 함수 재사용
from . import scoring_rules_us as R

log = logging.getLogger(__name__)


@dataclass
class ScoreBreakdownUS:
    ticker: str
    asof: str
    name: str = ""
    # raw features
    dividend_yield_pct: float | None = None
    consecutive_increase_years: int | None = None
    dps_cagr_5y_pct: float | None = None
    payout_ratio_pct: float | None = None
    price_cagr_5y_pct: float | None = None
    above_sma200_ratio_5y: float | None = None
    total_return_5y_pct: float | None = None
    roe_pct: float | None = None
    eps_cv: float | None = None
    aristocrat_label: str = ""
    # scores
    score_yield: int = 0
    score_consec: int = 0
    score_dps_cagr: int = 0
    score_payout: int = 0
    score_price_cagr: int = 0
    score_sma200: int = 0
    score_total_return: int = 0
    score_roe: int = 0
    score_eps_cv: int = 0
    score_aristocrat: int = 0
    # totals
    dividend_total: int = 0
    capital_total: int = 0
    fundamental_total: int = 0
    aristocrat_total: int = 0
    total: int = 0
    grade: str = ""
    notes: list[str] = field(default_factory=list)

    def finalize(self) -> ScoreBreakdownUS:
        self.dividend_total = (
            self.score_yield + self.score_consec + self.score_dps_cagr + self.score_payout
        )
        self.capital_total = self.score_price_cagr + self.score_sma200 + self.score_total_return
        self.fundamental_total = self.score_roe + self.score_eps_cv
        self.aristocrat_total = self.score_aristocrat
        self.total = (
            self.dividend_total + self.capital_total
            + self.fundamental_total + self.aristocrat_total
        )
        self.grade = R.grade(self.total)
        return self

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["notes"] = "; ".join(self.notes)
        return d


def score_ticker_us(
    ticker: str, asof: str, data_dir: Path, *, name: str = "",
) -> ScoreBreakdownUS:
    """단일 US 종목 DGI 점수. KR score_ticker와 같은 구조, US 데이터/임계값."""
    sb = ScoreBreakdownUS(ticker=ticker, asof=asof, name=name)

    bars = store.read_parquet_safe(store.bars_path(data_dir, ticker))
    divs = ud.load_us_dividends(data_dir, ticker)
    info = ud.load_us_info(data_dir, ticker) or {}
    fins = store.read_parquet_safe(financials_path(data_dir, ticker))

    annual = scoring.annual_dps_per_year(divs)
    if not annual:
        sb.notes.append("배당 이력 없음")

    # 현재가
    current_price = None
    if bars is not None and not bars.empty:
        b = bars[bars["date"] <= asof].sort_values("date")
        if not b.empty:
            current_price = float(b.iloc[-1]["close"])

    # ----- 배당 (45) -----
    # yield: 배당이력+현재가로 직접 계산 우선 (일관·신뢰). yfinance info yield는 포맷
    # 모호성(% vs 소수)이 있어 *계산 불가 시에만* fallback.
    sb.dividend_yield_pct = None
    if current_price:
        sb.dividend_yield_pct = scoring.dividend_yield_pct(annual, current_price, asof)
    if sb.dividend_yield_pct is None:
        sb.dividend_yield_pct = info.get("dividend_yield_pct")
    sb.consecutive_increase_years = scoring.consecutive_increase_years(annual, asof)
    sb.dps_cagr_5y_pct = scoring.dps_cagr_5y_pct(annual, asof)
    sb.payout_ratio_pct = info.get("payout_ratio_pct")

    sb.score_yield = R.score_dividend_yield(sb.dividend_yield_pct)
    sb.score_consec = R.score_consecutive_increase(sb.consecutive_increase_years)
    sb.score_dps_cagr = R.score_dps_cagr(sb.dps_cagr_5y_pct)
    sb.score_payout = R.score_payout_stability(sb.payout_ratio_pct)

    # ----- 자본이득 (30) -----
    sb.price_cagr_5y_pct = scoring.price_cagr_5y_pct(bars, asof)
    sb.above_sma200_ratio_5y = scoring.above_sma200_ratio_5y(bars, asof)
    sb.total_return_5y_pct = scoring.total_return_5y_pct(bars, annual, asof)
    sb.score_price_cagr = R.score_price_cagr(sb.price_cagr_5y_pct)
    sb.score_sma200 = R.score_above_sma200_ratio(sb.above_sma200_ratio_5y)
    sb.score_total_return = R.score_positive_total_return(sb.total_return_5y_pct)

    # ----- 펀더멘털 (15) -----
    sb.roe_pct = info.get("roe_pct")          # yfinance 현재 ROE
    sb.eps_cv = scoring.eps_cv(fins)          # SEC EDGAR 분기 EPS
    sb.score_roe = R.score_roe(sb.roe_pct)
    sb.score_eps_cv = R.score_eps_stability(sb.eps_cv)

    # ----- 배당귀족 지위 (10) -----
    sb.score_aristocrat = R.score_aristocrat_status(sb.consecutive_increase_years)
    sb.aristocrat_label = R.aristocrat_label(sb.consecutive_increase_years)

    return sb.finalize()


def screen_us(
    tickers: list[str], asof: str, data_dir: Path,
    *, name_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """다종목 US DGI 점수 → total 내림차순 DataFrame."""
    name_map = name_map or {}
    rows: list[dict[str, Any]] = []
    for t in tickers:
        try:
            sb = score_ticker_us(t, asof, data_dir, name=name_map.get(t, ""))
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] score_ticker_us failed: %s", t, e)
            continue
        rows.append(sb.to_dict())
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("total", ascending=False).reset_index(drop=True)


def us_scores_path(data_dir: Path, asof: str) -> Path:
    return Path(data_dir) / "us_value_scores" / f"{asof}.parquet"


def save_us_scores(df: pd.DataFrame, data_dir: Path, asof: str) -> Path:
    path = us_scores_path(data_dir, asof)
    store.write_parquet_atomic(df, path)
    return path
