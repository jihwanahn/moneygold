"""DGI score_ticker — 한 종목의 가속화 장기투자 점수.

데이터 의존:
  - 일봉:        ``store/bars/{ticker}.parquet`` (PR1)
  - 배당:        ``store/dividends/{ticker}.parquet`` (PR-B)
  - 펀더멘털:    ``store/financials/{ticker}.parquet`` (PR-A 외 기존)
  - 자사주:      DartClient (PR-A) — Optional. None이면 주주환원 10점은 NaN/0.

설계 원칙:
  - **feature 산출 함수는 순수**: DataFrame in → scalar out. asof 명시.
  - **score_ticker는 순수 함수가 아니지만 결정론적**: 캐시된 데이터만 읽음. 외부 호출은 DART만.
  - **NaN 통과**: 데이터 없는 종목은 해당 항목 0점만, 강제 제외 X (CLAUDE.md 원칙).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ...data import dart_indicators as dart_ind
from ...data import dividends as div_mod
from ...data import store
from ...fundamentals import financials_path
from . import scoring_rules as R
from .dart_client import DartClient

log = logging.getLogger(__name__)


# ============================================================
# Feature computation (pure functions — testable without I/O)
# ============================================================


def annual_dps_per_year(dividends_df: pd.DataFrame) -> dict[int, float]:
    """배당 이력 → 회계연도별 1주당 현금배당 총합.

    Source priority:
      - DataFrame에 fiscal_year가 채워진 행이 하나라도 있으면 **pykrx 출처**로 판단하고
        그 행들만 사용 (KIS legacy 행은 무시). 두 source 합산 시 중복 가산 방지.
      - 없으면 모두 record_date로 추론 (KIS 호환):
          divi_kind='결산'이고 record month ≤ 4 → 전년도 회계연도로 귀속
          분기·반기·중간배당은 record_date 연도 그대로

    우선주(stk_kind='우선' 등)는 제외 — DGI는 보통주 가정.
    """
    if dividends_df is None or dividends_df.empty:
        return {}

    # pykrx 행이 있으면 그것만 사용 (KIS legacy 행 무시)
    if "fiscal_year" in dividends_df.columns:
        pykrx_rows = dividends_df[dividends_df["fiscal_year"].notna()]
        if not pykrx_rows.empty:
            dividends_df = pykrx_rows

    out: dict[int, float] = defaultdict(float)
    has_fiscal = "fiscal_year" in dividends_df.columns
    for _, row in dividends_df.iterrows():
        kind = (row.get("stk_kind") or "").strip()
        if kind and kind != "보통":
            continue
        amt = row.get("per_sto_divi_amt")
        if pd.isna(amt):
            continue

        fy = row.get("fiscal_year") if has_fiscal else None
        if fy is not None and not pd.isna(fy):
            out[int(fy)] += float(amt)
            continue

        rec = str(row.get("record_date") or "")
        if len(rec) < 6 or not rec[:4].isdigit():
            continue
        year = int(rec[:4])
        month = int(rec[4:6]) if rec[4:6].isdigit() else 1
        divi_kind = (row.get("divi_kind") or "").strip()
        if divi_kind == "결산" and month <= 4:
            year -= 1
        out[year] += float(amt)
    return dict(out)


def consecutive_increase_years(annual_dps: dict[int, float], asof: str) -> int:
    """``asof`` 기준 직전 완결 연도부터 거꾸로 셈. 동결도 끊김 (ValueTrader와 동일)."""
    if not annual_dps:
        return 0
    this_year = int(asof[:4])
    years = sorted(y for y in annual_dps if y < this_year and annual_dps[y] > 0)
    if len(years) < 2:
        return 0
    streak = 0
    for i in range(len(years) - 1, 0, -1):
        y_curr, y_prev = years[i], years[i - 1]
        if y_curr - y_prev != 1:
            break
        if annual_dps[y_curr] > annual_dps[y_prev] + 1e-6:
            streak += 1
        else:
            break
    return streak


def dps_cagr_5y_pct(annual_dps: dict[int, float], asof: str) -> float | None:
    """5년 전 DPS → 직전 완결 연도 DPS의 CAGR(%). 둘 다 양수여야."""
    if not annual_dps:
        return None
    this_year = int(asof[:4])
    completed = sorted(y for y in annual_dps if y < this_year and annual_dps[y] > 0)
    if len(completed) < 2:
        return None
    last_year = completed[-1]
    # 5년 전 (또는 그보다 옛날 가장 가까운 해)
    target_year = last_year - 5
    candidates = [y for y in completed if y <= target_year]
    if not candidates:
        # 데이터 부족 — 가장 옛날 해로 fallback (n년 CAGR로 약식 계산)
        start_year = completed[0]
    else:
        start_year = candidates[-1]
    n_years = last_year - start_year
    if n_years <= 0:
        return None
    start_dps = annual_dps[start_year]
    end_dps = annual_dps[last_year]
    if start_dps <= 0 or end_dps <= 0:
        return None
    cagr = (end_dps / start_dps) ** (1.0 / n_years) - 1.0
    return round(cagr * 100, 2)


def dividend_yield_pct(
    annual_dps: dict[int, float], current_price: float, asof: str,
) -> float | None:
    """직전 완결 연도 DPS / 현재가 × 100."""
    if not annual_dps or current_price <= 0:
        return None
    this_year = int(asof[:4])
    completed = sorted(
        (y for y in annual_dps if y < this_year and annual_dps[y] > 0), reverse=True,
    )
    if not completed:
        return None
    basis = annual_dps[completed[0]]
    return round(basis / current_price * 100, 3)


def dividend_frequency_last_year(dividends_df: pd.DataFrame, asof: str) -> int:
    """``asof`` 기준 직전 1년간 보통주 현금배당 공시 건수."""
    if dividends_df is None or dividends_df.empty:
        return 0
    asof_dt = datetime.strptime(asof, "%Y%m%d")
    one_year_ago = asof_dt.replace(year=asof_dt.year - 1)
    cnt = 0
    for _, row in dividends_df.iterrows():
        kind = (row.get("stk_kind") or "").strip()
        if kind and kind != "보통":
            continue
        rec = str(row.get("record_date") or "")
        if len(rec) != 8 or not rec.isdigit():
            continue
        try:
            d = datetime.strptime(rec, "%Y%m%d")
        except ValueError:
            continue
        amt = row.get("per_sto_divi_amt")
        if pd.isna(amt) or float(amt) <= 0:
            continue
        if one_year_ago <= d <= asof_dt:
            cnt += 1
    return cnt


def price_cagr_5y_pct(bars_df: pd.DataFrame, asof: str) -> float | None:
    """5년 전 종가 → asof 시점 종가 CAGR(%). 일봉이 부족하면 가능한 만큼의 CAGR."""
    if bars_df is None or bars_df.empty or "date" not in bars_df.columns:
        return None
    # asof 이하의 종가만
    df = bars_df[bars_df["date"] <= asof].copy()
    if df.empty:
        return None
    df = df.sort_values("date")
    end_close = float(df.iloc[-1]["close"])
    # 5년 전과 가장 가까운 행
    asof_dt = datetime.strptime(asof, "%Y%m%d")
    five_ago = asof_dt.replace(year=asof_dt.year - 5).strftime("%Y%m%d")
    cand = df[df["date"] <= five_ago]
    if cand.empty:
        # 5년치가 없으면 가장 옛날 행으로 fallback
        start_row = df.iloc[0]
    else:
        start_row = cand.iloc[-1]
    start_close = float(start_row["close"])
    start_dt = datetime.strptime(str(start_row["date"]), "%Y%m%d")
    years = (asof_dt - start_dt).days / 365.25
    # 윈도우가 2년 미만이면 '5년 CAGR' 외삽이 무의미 (신규상장·분사주가 132%/yr 같은
    # 과대 CAGR로 자본이득 만점을 부당 수령). 데이터 부족으로 처리.
    if years < 2.0 or start_close <= 0:
        return None
    cagr = (end_close / start_close) ** (1.0 / years) - 1.0
    return round(cagr * 100, 2)


def above_sma200_ratio_5y(bars_df: pd.DataFrame, asof: str) -> float | None:
    """직전 5년 거래일 중 종가 ≥ 200일 SMA였던 비율 (0~1)."""
    if bars_df is None or bars_df.empty:
        return None
    df = bars_df[bars_df["date"] <= asof].copy().sort_values("date")
    if len(df) < 200:
        return None
    df["sma200"] = df["close"].rolling(200, min_periods=200).mean()
    asof_dt = datetime.strptime(asof, "%Y%m%d")
    five_ago = asof_dt.replace(year=asof_dt.year - 5).strftime("%Y%m%d")
    window = df[(df["date"] >= five_ago) & df["sma200"].notna()]
    if window.empty:
        return None
    above = (window["close"] >= window["sma200"]).sum()
    return round(float(above) / len(window), 4)


def total_return_5y_pct(
    bars_df: pd.DataFrame, annual_dps: dict[int, float], asof: str,
) -> float | None:
    """5년 총수익(%) = (말종가 + 누적배당 - 시작종가) / 시작종가 × 100. 근사."""
    cagr = price_cagr_5y_pct(bars_df, asof)
    if cagr is None:
        return None
    # 가격 수익은 CAGR로부터 근사 복원 (사용한 시작-끝 구간이 5년 미만일 수 있음 무시)
    price_return_pct = ((1.0 + cagr / 100) ** 5 - 1.0) * 100  # 5년 등가
    if not annual_dps:
        return round(price_return_pct, 2)
    this_year = int(asof[:4])
    recent = sorted(y for y in annual_dps if y < this_year)[-5:]
    div_sum = sum(annual_dps[y] for y in recent)
    # 배당은 시작 시점 종가 대비 비율로 변환
    if bars_df is None or bars_df.empty:
        return round(price_return_pct, 2)
    df = bars_df[bars_df["date"] <= asof].sort_values("date")
    asof_dt = datetime.strptime(asof, "%Y%m%d")
    five_ago = asof_dt.replace(year=asof_dt.year - 5).strftime("%Y%m%d")
    cand = df[df["date"] <= five_ago]
    start_close = float(cand.iloc[-1]["close"]) if not cand.empty else float(df.iloc[0]["close"])
    if start_close <= 0:
        return round(price_return_pct, 2)
    div_yield_pct = div_sum / start_close * 100
    return round(price_return_pct + div_yield_pct, 2)


def roe_5y_avg_pct(
    financials_df: pd.DataFrame,
    dart_indicators_df: pd.DataFrame | None = None,
) -> float | None:
    """5년 ROE 평균.

    Source priority:
      1. ``dart_indicators_df``의 'ROE' 행이 있으면 그것 우선 (사업보고서 기준, 안정적)
      2. ``financials_df``의 ``roe`` 컬럼(KIS finance, 분기 누적)에서 추출

    한 source라도 최근 3년치 이상이면 평균 계산. 둘 다 부족하면 None.
    """
    # 1) DART 우선
    if dart_indicators_df is not None and not dart_indicators_df.empty:
        annual_roe = dart_ind.extract_annual_roe(dart_indicators_df)
        if annual_roe:
            recent_years = sorted(annual_roe.keys(), reverse=True)[:5]
            values = [annual_roe[y] for y in recent_years]
            if len(values) >= 3:
                return round(sum(values) / len(values), 2)

    # 2) KIS financials fallback
    if financials_df is None or financials_df.empty or "roe" not in financials_df.columns:
        return None
    df = financials_df[["year", "q", "roe"]].dropna(subset=["roe"]).copy()
    if df.empty:
        return None
    df = df.sort_values(["year", "q"])
    annual = df.groupby("year").tail(1)
    recent = annual.tail(5)
    if recent.empty:
        return None
    return round(float(recent["roe"].mean()), 2)


def eps_cv(financials_df: pd.DataFrame) -> float | None:
    """EPS 변동계수 (std/mean). 분기 EPS를 연간 합산 후 5년 CV. 음수 평균은 None."""
    if financials_df is None or financials_df.empty or "eps" not in financials_df.columns:
        return None
    df = financials_df[["year", "eps"]].dropna(subset=["eps"])
    if df.empty:
        return None
    annual = df.groupby("year")["eps"].sum().tail(5)
    if len(annual) < 3:
        return None
    mean = float(annual.mean())
    if mean <= 0:
        return None
    std = float(annual.std(ddof=0))
    return round(std / mean, 4)


# ============================================================
# Score breakdown
# ============================================================

@dataclass
class ScoreBreakdown:
    ticker: str
    asof: str
    name: str = ""
    # Inputs (raw features)
    dividend_yield_pct: float | None = None
    consecutive_increase_years: int | None = None
    dps_cagr_5y_pct: float | None = None
    payout_ratio_pct: float | None = None
    dividend_freq_last_year: int | None = None
    price_cagr_5y_pct: float | None = None
    above_sma200_ratio_5y: float | None = None
    total_return_5y_pct: float | None = None
    roe_5y_avg_pct: float | None = None
    eps_cv: float | None = None
    has_treasury_cancellation: bool | None = None
    cancellation_per_year: float | None = None
    # Scores (점수표)
    score_yield: int = 0
    score_consec: int = 0
    score_dps_cagr: int = 0
    score_payout: int = 0
    score_freq: int = 0
    score_price_cagr: int = 0
    score_sma200: int = 0
    score_total_return: int = 0
    score_roe: int = 0
    score_eps_cv: int = 0
    score_cancel_has: int = 0
    score_cancel_freq: int = 0
    # Totals
    dividend_total: int = 0
    capital_total: int = 0
    fundamental_total: int = 0
    shareholder_total: int = 0
    total: int = 0
    grade: str = ""
    # Diagnostics
    notes: list[str] = field(default_factory=list)

    def finalize(self) -> ScoreBreakdown:
        self.dividend_total = (
            self.score_yield + self.score_consec + self.score_dps_cagr
            + self.score_payout + self.score_freq
        )
        self.capital_total = self.score_price_cagr + self.score_sma200 + self.score_total_return
        self.fundamental_total = self.score_roe + self.score_eps_cv
        self.shareholder_total = self.score_cancel_has + self.score_cancel_freq
        self.total = (
            self.dividend_total + self.capital_total
            + self.fundamental_total + self.shareholder_total
        )
        self.grade = R.grade(self.total)
        return self

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["notes"] = "; ".join(self.notes)
        return d


# ============================================================
# Orchestrator
# ============================================================

def score_ticker(
    ticker: str,
    asof: str,
    data_dir: Path,
    *,
    dart: DartClient | None = None,
    name: str = "",
) -> ScoreBreakdown:
    """단일 종목 DGI 점수 산출.

    Parameters
    ----------
    ticker      : 6자리 코드
    asof        : 기준일 YYYYMMDD
    data_dir    : moneygold store/ 경로
    dart        : DartClient 인스턴스. None이면 주주환원 항목 skip (10점은 NaN).
    name        : 종목명 (로깅·리포트용).
    """
    sb = ScoreBreakdown(ticker=ticker, asof=asof, name=name)

    bars = store.read_parquet_safe(store.bars_path(data_dir, ticker))
    divs = div_mod.load_dividends(data_dir, ticker)
    fins = store.read_parquet_safe(financials_path(data_dir, ticker))
    dart_ind_df = dart_ind.load_indicators(data_dir, ticker)

    # ----- 배당 -----
    annual = annual_dps_per_year(divs)
    if not annual:
        sb.notes.append("배당 이력 없음")
    current_price = None
    if bars is not None and not bars.empty:
        b_asof = bars[bars["date"] <= asof].sort_values("date")
        if not b_asof.empty:
            current_price = float(b_asof.iloc[-1]["close"])

    sb.dividend_yield_pct = (
        dividend_yield_pct(annual, current_price, asof) if current_price else None
    )
    sb.consecutive_increase_years = consecutive_increase_years(annual, asof)
    sb.dps_cagr_5y_pct = dps_cagr_5y_pct(annual, asof)
    sb.dividend_freq_last_year = dividend_frequency_last_year(divs, asof)

    # 배당성향: 직전 완결 연도 DPS / 연 EPS × 100
    if annual and fins is not None and "eps" in fins.columns:
        this_year = int(asof[:4])
        completed = sorted(y for y in annual if y < this_year and annual[y] > 0)
        if completed:
            last_year = completed[-1]
            year_eps = fins[(fins.get("year") == last_year)]["eps"].sum() if "year" in fins.columns else np.nan
            if pd.notna(year_eps) and year_eps > 0:
                sb.payout_ratio_pct = round(annual[last_year] / float(year_eps) * 100, 2)

    sb.score_yield = R.score_dividend_yield(sb.dividend_yield_pct)
    sb.score_consec = R.score_consecutive_increase(sb.consecutive_increase_years)
    sb.score_dps_cagr = R.score_dps_cagr(sb.dps_cagr_5y_pct)
    sb.score_payout = R.score_payout_stability(sb.payout_ratio_pct)
    sb.score_freq = R.score_dividend_frequency(sb.dividend_freq_last_year)

    # ----- 자본이득 -----
    sb.price_cagr_5y_pct = price_cagr_5y_pct(bars, asof)
    sb.above_sma200_ratio_5y = above_sma200_ratio_5y(bars, asof)
    sb.total_return_5y_pct = total_return_5y_pct(bars, annual, asof)
    sb.score_price_cagr = R.score_price_cagr(sb.price_cagr_5y_pct)
    sb.score_sma200 = R.score_above_sma200_ratio(sb.above_sma200_ratio_5y)
    sb.score_total_return = R.score_positive_total_return(sb.total_return_5y_pct)

    # ----- 펀더멘털 -----
    sb.roe_5y_avg_pct = roe_5y_avg_pct(fins, dart_indicators_df=dart_ind_df)
    sb.eps_cv = eps_cv(fins)
    sb.score_roe = R.score_roe_5y_avg(sb.roe_5y_avg_pct)
    sb.score_eps_cv = R.score_eps_stability(sb.eps_cv)

    # ----- 주주환원 (DART) -----
    if dart is not None:
        try:
            ta = dart.treasury_activity(ticker, asof=asof, years=3)
            sb.has_treasury_cancellation = ta["cancel_count"] > 0
            sb.cancellation_per_year = ta["cancel_count"] / max(ta["years_window"], 1)
        except Exception as e:  # noqa: BLE001
            sb.notes.append(f"DART treasury 조회 실패: {e}")

        # 배당 빈도 보강 (DART 배당결정 공시 카운트). pykrx fundamental만으로는
        # 분기 vs 결산 구분이 불가능하므로 DART로 보완.
        # round_half_up — Python 기본 round()는 banker's rounding이라 0.5→0이 되어
        # 연 1회 배당 종목(2년에 1건 = 0.5/yr)이 0점이 됨. int(x + 0.5)로 회피.
        try:
            per_year = dart.dividend_decisions_per_year(ticker, asof=asof, years=2)
            if per_year is not None:
                sb.dividend_freq_last_year = int(per_year + 0.5)
        except Exception as e:  # noqa: BLE001
            sb.notes.append(f"DART dividend 조회 실패: {e}")

    sb.score_cancel_has = R.score_has_cancellation(sb.has_treasury_cancellation)
    sb.score_cancel_freq = R.score_cancellation_frequency(sb.cancellation_per_year)
    # 빈도 점수는 DART 갱신 후 다시 계산 (위에서 sb.dividend_freq_last_year를 덮어쓸 수 있음)
    sb.score_freq = R.score_dividend_frequency(sb.dividend_freq_last_year)

    return sb.finalize()


# ============================================================
# Batch screen + save
# ============================================================

def screen(
    tickers: list[str],
    asof: str,
    data_dir: Path,
    *,
    dart: DartClient | None = None,
    name_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """다종목 일괄 점수 산출 → DataFrame (total 내림차순)."""
    name_map = name_map or {}
    rows: list[dict[str, Any]] = []
    for t in tickers:
        try:
            sb = score_ticker(t, asof, data_dir, dart=dart, name=name_map.get(t, ""))
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] score_ticker failed: %s", t, e)
            continue
        rows.append(sb.to_dict())
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("total", ascending=False).reset_index(drop=True)


def scores_path(data_dir: Path, asof: str) -> Path:
    return Path(data_dir) / "value_scores" / f"{asof}.parquet"


def save_scores(df: pd.DataFrame, data_dir: Path, asof: str) -> Path:
    """DGI 점수 결과를 parquet으로. tmp + atomic rename."""
    path = scores_path(data_dir, asof)
    store.write_parquet_atomic(df, path)
    return path
