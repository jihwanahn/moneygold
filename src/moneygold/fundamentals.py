"""펀더멘털 (KIS 재무 엔드포인트 기반).

KIS 손익계산서 응답은 *YTD 누적* (1Q / 1H / 3Q / FY 순으로 같은 회계연도 누적).
이를 분기 단독으로 정규화하고, Minervini growth 시그널을 산출.

산출 컬럼:
  quarter         : '2025Q1' / '2025Q2' / '2025Q3' / '2025Q4'
  revenue         : 분기 단독 매출 (백만원)
  op_income       : 분기 단독 영업이익 (백만원)
  net_income      : 분기 단독 순이익 (백만원, 가능 시)
  op_margin       : 영업이익률 = op_income / revenue × 100
  revenue_yoy     : 매출 YoY (%) — 같은 분기 작년 대비
  op_income_yoy   : 영업이익 YoY (%)
  eps             : 분기 단독 EPS (원)

캐싱: store/financials/{ticker}.parquet

ARCHITECTURE §5의 Minervini 조건 9 (분기 EPS YoY ≥ 25% 또는 가속)에 직접 대응.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .data import store
from .data.kis_client import KISAPIError, KISClient

log = logging.getLogger(__name__)


# ============================================================
# Periodize: YTD 누적 → 분기 단독
# ============================================================

def _yyyymm_to_quarter(yyyymm: str) -> tuple[int, int]:
    """'202509' → (2025, 3). 분기 = (월-1)//3 + 1."""
    y = int(str(yyyymm)[:4])
    m = int(str(yyyymm)[4:6])
    q = (m - 1) // 3 + 1
    return y, q


def _periodize_cumulative(
    df: pd.DataFrame,
    cum_columns: list[str],
) -> pd.DataFrame:
    """YTD 누적 컬럼을 분기 단독으로 변환.

    각 회계연도 내에서: Q1 = 그대로, Q2 = Q2_YTD - Q1, Q3 = Q3_YTD - Q2_YTD, Q4 = FY - Q3_YTD.

    KIS는 일부 종목(연 1회 보고하는 작은 종목, 지주사 등 ~319개)에 분기 없이 *연간만*
    반환한다 (stac_yymm 마다 12월). 그 경우 차감할 직전 분기 YTD가 없어 NaN으로 떨어지는데,
    각 row 자체가 이미 FY 단독값이므로 그대로 보존하는 게 옳다. → 연간-only 데이터셋이라고
    판단되면 (모든 row가 같은 회계 q 1행씩) 차감 단계를 건너뛴다.
    """
    out = df.copy()
    if "stac_yymm" not in out.columns:
        return out

    out["_year"] = out["stac_yymm"].astype(str).str[:4].astype(int)
    out["_q"] = out["stac_yymm"].astype(str).str[4:6].astype(int).apply(lambda m: (m - 1) // 3 + 1)
    out = out.sort_values(["_year", "_q"]).reset_index(drop=True)

    # 각 연도 내에서: '첫 행 = YTD 그대로 (= 시작부터 그 분기까지 표준)', 이후 행 = 직전 YTD 차감.
    # 이 규칙은 reporter 타입을 자동 처리:
    #   - 연간 (각 연도 1행, q=4):       각 row 보존 (FY 단독값)
    #   - 반기 (각 연도 2행, q=2,4):     H1 보존, Q4 = FY - H1 (= 2H)
    #   - 분기 (각 연도 4행, q=1,2,3,4): Q1 보존, Q2=Q2YTD-Q1, Q3=Q3YTD-Q2YTD, Q4=FY-Q3YTD
    #   - 부분 분기 (예: Samsung 2018Q3부터 시작): 2018Q3을 standalone로 *잘못* 라벨할 수
    #     있으나 (실제로는 9M YTD), 후속 행들은 정확. 워치리스트는 최근 데이터를 보므로 영향 작음.
    for col in cum_columns:
        if col not in out.columns:
            continue
        ser = pd.to_numeric(out[col], errors="coerce")
        single = ser.copy()
        for year, grp in out.groupby("_year"):
            idxs = grp.index.tolist()
            for j, i in enumerate(idxs):
                if j == 0:
                    # 그 연도의 첫 행 = YTD 시작부터 그 시점까지 (=standalone for partial year)
                    single.iloc[i] = ser.iloc[i]
                else:
                    prev_i = idxs[j - 1]
                    if pd.notna(ser.iloc[i]) and pd.notna(ser.iloc[prev_i]):
                        single.iloc[i] = ser.iloc[i] - ser.iloc[prev_i]
                    else:
                        single.iloc[i] = np.nan
        out[col] = single
    return out


# ============================================================
# YoY by (year-1, same q) — positional shift(4) breaks when quarters are missing
# ============================================================

def _attach_yoy_by_year_q(
    df: pd.DataFrame,
    pairs: list[tuple[str, str]],
) -> None:
    """Compute YoY in-place by joining each row to the (year-1, same q) row.

    df must contain integer 'year' and 'q' columns. Each (src, dst) pair
    overwrites df[dst] with (cur - prev) / abs(prev) * 100.

    Uses abs(prev) as the denominator (not prev itself) so the sign is
    preserved when the prior period is negative — e.g., turnaround from
    loss to profit reads as positive growth, not a fake -10000% decline.
    Standard finance convention for negative-base YoY.

    Rows without a matching prior-year quarter get NaN. Prior=0 → NaN.
    """
    if "year" not in df.columns or "q" not in df.columns:
        for _, dst in pairs:
            df[dst] = np.nan
        return
    prev_year = df["year"] - 1
    idx_by_key = {(int(y), int(q)): i for i, (y, q) in enumerate(zip(df["year"], df["q"]))}
    for src, dst in pairs:
        if src not in df.columns:
            df[dst] = np.nan
            continue
        cur = pd.to_numeric(df[src], errors="coerce").values
        prev = np.full(len(df), np.nan)
        for i, (py, q) in enumerate(zip(prev_year.astype(int), df["q"].astype(int))):
            j = idx_by_key.get((int(py), int(q)))
            if j is not None:
                prev[i] = cur[j]
        with np.errstate(divide="ignore", invalid="ignore"):
            denom = np.where(prev == 0, np.nan, np.abs(prev))
            yoy = (cur - prev) / denom * 100.0
        df[dst] = yoy


# ============================================================
# Build quarterly metrics for one ticker
# ============================================================

@dataclass
class FundamentalsResult:
    quarters: pd.DataFrame                  # 분기별 단독 매출, 영업이익, op_margin, eps, YoY 등
    latest_op_margin: float = float("nan")  # 가장 최근 분기 영업이익률
    latest_revenue_yoy: float = float("nan")
    latest_op_income_yoy: float = float("nan")
    latest_eps_yoy: float = float("nan")
    growth_quarters: int = 0                # 연속 매출 YoY > 0 분기 수 (최근부터 뒤로)
    op_growth_quarters: int = 0             # 연속 영업이익 YoY > 0 분기 수
    accelerating: bool = False              # 최근 YoY > 직전 YoY (매출 또는 영업이익)
    latest_net_margin: float = float("nan") # 가장 최근 분기 순이익률 = net_income / revenue × 100
    error: str | None = None


def build_fundamentals(
    income_stmt_q: pd.DataFrame,
    financial_ratios_q: pd.DataFrame,
) -> FundamentalsResult:
    """KIS 분기 응답 → 정규화된 펀더멘털.

    income_stmt_q: fetch_income_statement(quarterly=True) 결과 (YTD 누적)
    financial_ratios_q: fetch_financial_ratios(quarterly=True) (눈에 띄게 eps 누적)
    """
    if income_stmt_q is None or income_stmt_q.empty:
        return FundamentalsResult(quarters=pd.DataFrame(), error="no income statement")

    # 손익계산서: 누적 → 단독 정규화. thtr_ntin = 당기순이익 (KIS 필드).
    cum_cols_inc = ["sale_account", "sale_cost", "sale_totl_prfi", "bsop_prti", "thtr_ntin"]
    ic = _periodize_cumulative(income_stmt_q, cum_cols_inc)

    # 재무비율 (eps 등)도 누적
    if financial_ratios_q is not None and not financial_ratios_q.empty:
        rr = _periodize_cumulative(financial_ratios_q, ["eps", "sps"])
    else:
        rr = pd.DataFrame()

    # 분기 라벨
    ic["quarter"] = ic["_year"].astype(str) + "Q" + ic["_q"].astype(str)

    # 단독 분기 컬럼들
    revenue = pd.to_numeric(ic.get("sale_account"), errors="coerce")
    op_income = pd.to_numeric(ic.get("bsop_prti"), errors="coerce")
    net_income = pd.to_numeric(ic.get("thtr_ntin"), errors="coerce")
    op_margin = (op_income / revenue.replace(0, np.nan)) * 100.0
    net_margin = (net_income / revenue.replace(0, np.nan)) * 100.0

    out = pd.DataFrame({
        "quarter": ic["quarter"],
        "year": ic["_year"],
        "q": ic["_q"],
        "revenue": revenue.values,
        "op_income": op_income.values,
        "net_income": net_income.values,
        "op_margin": op_margin.values,
        "net_margin": net_margin.values,
    })

    if not rr.empty:
        merged = pd.merge(
            out, rr[["stac_yymm", "eps"]].rename(columns={"eps": "eps"}),
            left_on=out["year"].astype(str) + (out["q"] * 3).astype(str).str.zfill(2),
            right_on="stac_yymm", how="left",
        )
        if "eps" in merged.columns:
            out["eps"] = pd.to_numeric(merged["eps"], errors="coerce").values

    # YoY 계산: (year-1, same q) 매칭. shift(4)는 분기 누락 시 엉뚱한 행과 비교됨.
    out = out.sort_values(["year", "q"]).reset_index(drop=True)
    _attach_yoy_by_year_q(out, [("revenue", "revenue_yoy"),
                                 ("op_income", "op_income_yoy"),
                                 ("eps", "eps_yoy")])

    # 연속 성장 분기 수 + 가속
    last = out.iloc[-1] if not out.empty else None
    growth_q = 0
    op_growth_q = 0
    accelerating = False
    if last is not None:
        for i in range(len(out) - 1, -1, -1):
            v = out["revenue_yoy"].iloc[i]
            if pd.notna(v) and v > 0:
                growth_q += 1
            else:
                break
        for i in range(len(out) - 1, -1, -1):
            v = out["op_income_yoy"].iloc[i]
            if pd.notna(v) and v > 0:
                op_growth_q += 1
            else:
                break
        if len(out) >= 2:
            cur_rev_yoy = out["revenue_yoy"].iloc[-1]
            prev_rev_yoy = out["revenue_yoy"].iloc[-2]
            cur_op_yoy = out["op_income_yoy"].iloc[-1]
            prev_op_yoy = out["op_income_yoy"].iloc[-2]
            if (pd.notna(cur_rev_yoy) and pd.notna(prev_rev_yoy) and cur_rev_yoy > prev_rev_yoy) or (
                pd.notna(cur_op_yoy) and pd.notna(prev_op_yoy) and cur_op_yoy > prev_op_yoy
            ):
                accelerating = True

    latest_op_margin = float(last["op_margin"]) if last is not None and pd.notna(last["op_margin"]) else float("nan")
    latest_net_margin = float(last["net_margin"]) if last is not None and "net_margin" in out.columns and pd.notna(last.get("net_margin", np.nan)) else float("nan")
    latest_rev_yoy = float(last["revenue_yoy"]) if last is not None and pd.notna(last.get("revenue_yoy", np.nan)) else float("nan")
    latest_op_yoy = float(last["op_income_yoy"]) if last is not None and pd.notna(last.get("op_income_yoy", np.nan)) else float("nan")
    latest_eps_yoy = float(last["eps_yoy"]) if last is not None and "eps_yoy" in out.columns and pd.notna(last.get("eps_yoy", np.nan)) else float("nan")

    return FundamentalsResult(
        quarters=out,
        latest_op_margin=latest_op_margin,
        latest_net_margin=latest_net_margin,
        latest_revenue_yoy=latest_rev_yoy,
        latest_op_income_yoy=latest_op_yoy,
        latest_eps_yoy=latest_eps_yoy,
        growth_quarters=growth_q,
        op_growth_quarters=op_growth_q,
        accelerating=accelerating,
    )


# ============================================================
# Caching
# ============================================================

def financials_path(data_dir: Path, ticker: str) -> Path:
    return data_dir / "financials" / f"{ticker}.parquet"


def fetch_and_cache(
    client: KISClient,
    data_dir: Path,
    ticker: str,
    *,
    force: bool = False,
) -> FundamentalsResult:
    """KIS 호출 + 정규화 + 디스크 캐싱.

    force=True가 아니면 캐시가 있으면 그대로 반환.
    """
    path = financials_path(data_dir, ticker)
    if not force and path.exists():
        cached = store.read_parquet_safe(path)
        if cached is not None and not cached.empty:
            return build_fundamentals_from_cache(cached)

    try:
        inc = client.fetch_income_statement(ticker, quarterly=True)
        rr = client.fetch_financial_ratios(ticker, quarterly=True)
    except KISAPIError as e:
        return FundamentalsResult(quarters=pd.DataFrame(), error=f"KIS {e.rt_cd}: {e.msg}")
    except Exception as e:
        return FundamentalsResult(quarters=pd.DataFrame(), error=str(e))

    result = build_fundamentals(inc, rr)
    if not result.quarters.empty:
        store.write_parquet_atomic(result.quarters, path)
    return result


def build_fundamentals_from_cache(df: pd.DataFrame) -> FundamentalsResult:
    """캐시된 quarters DF에서 latest 지표 + 연속 성장 분기 재계산.

    분기 누락 종목의 잘못된 YoY (shift(4) 버그)를 캐시 읽을 때 (year, q) 매칭으로
    재계산해 덮어쓴다 — 기존 parquet은 재 sync 없이도 자동 보정됨.
    """
    if df.empty:
        return FundamentalsResult(quarters=df)
    df = df.sort_values(["year", "q"]).reset_index(drop=True) if {"year","q"}.issubset(df.columns) else df
    # net_margin 컬럼 보강 (구 parquet은 없을 수 있음). net_income/revenue가 있으면 계산.
    if "net_margin" not in df.columns and "net_income" in df.columns and "revenue" in df.columns:
        df["net_margin"] = (
            pd.to_numeric(df["net_income"], errors="coerce")
            / pd.to_numeric(df["revenue"], errors="coerce").replace(0, np.nan)
        ) * 100.0
    _attach_yoy_by_year_q(df, [("revenue", "revenue_yoy"),
                                ("op_income", "op_income_yoy"),
                                ("eps", "eps_yoy")])
    last = df.iloc[-1]
    growth_q = 0
    op_growth_q = 0
    for i in range(len(df) - 1, -1, -1):
        v = df["revenue_yoy"].iloc[i] if "revenue_yoy" in df.columns else np.nan
        if pd.notna(v) and v > 0:
            growth_q += 1
        else:
            break
    for i in range(len(df) - 1, -1, -1):
        v = df["op_income_yoy"].iloc[i] if "op_income_yoy" in df.columns else np.nan
        if pd.notna(v) and v > 0:
            op_growth_q += 1
        else:
            break
    accelerating = False
    if len(df) >= 2:
        cur_rev = df["revenue_yoy"].iloc[-1] if "revenue_yoy" in df.columns else np.nan
        prev_rev = df["revenue_yoy"].iloc[-2] if "revenue_yoy" in df.columns else np.nan
        cur_op = df["op_income_yoy"].iloc[-1] if "op_income_yoy" in df.columns else np.nan
        prev_op = df["op_income_yoy"].iloc[-2] if "op_income_yoy" in df.columns else np.nan
        if (pd.notna(cur_rev) and pd.notna(prev_rev) and cur_rev > prev_rev) or (
            pd.notna(cur_op) and pd.notna(prev_op) and cur_op > prev_op
        ):
            accelerating = True

    return FundamentalsResult(
        quarters=df,
        latest_op_margin=float(last["op_margin"]) if pd.notna(last.get("op_margin", np.nan)) else float("nan"),
        latest_net_margin=float(last["net_margin"]) if "net_margin" in df.columns and pd.notna(last.get("net_margin", np.nan)) else float("nan"),
        latest_revenue_yoy=float(last["revenue_yoy"]) if "revenue_yoy" in df.columns and pd.notna(last["revenue_yoy"]) else float("nan"),
        latest_op_income_yoy=float(last["op_income_yoy"]) if "op_income_yoy" in df.columns and pd.notna(last["op_income_yoy"]) else float("nan"),
        latest_eps_yoy=float(last["eps_yoy"]) if "eps_yoy" in df.columns and pd.notna(last.get("eps_yoy", np.nan)) else float("nan"),
        growth_quarters=growth_q,
        op_growth_quarters=op_growth_q,
        accelerating=accelerating,
    )
