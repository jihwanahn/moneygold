"""DART 재무지표 동기화 + 로컬 parquet 캐시.

DGI(가속화 장기투자) 점수표의 펀더멘털 20점 (ROE 5년 평균 + EPS 변동계수)을 채우기 위해
KIS finance-ratio 대신 DART fnlttSinglIndx.json (단일회사 주요재무지표) 사용.

저장 경로: ``store/dart_indicators/{ticker}.parquet``
(ticker, fiscal_year, idx_cl_code, idx_nm) unique.

스키마:
    ticker         (str)
    fiscal_year    (int)         사업연도
    reprt_code     (str)         '11011'=사업보고서(연간), 등
    idx_cl_code    (str)         M210000=수익성, M220000=안정성 등
    idx_cl_nm      (str)         '수익성지표' 등 한글명
    idx_nm         (str)         'ROE' / '순이익률' 등 한글 지표명
    idx_val        (float)       수치. '-' 같은 비숫자는 NaN.
    fetched_at     (str)         YYYYMMDD

원본: OpenDART /api/fnlttSinglIndx.json. 권한 기본키로 접근 가능 (별도 가입 필요 X).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from ..strategies.value_long_term.dart_client import DartClient, DartQuotaExceeded
from . import store

log = logging.getLogger(__name__)


INDICATOR_COLUMNS = [
    "ticker", "fiscal_year", "reprt_code",
    "idx_cl_code", "idx_cl_nm", "idx_nm", "idx_val", "fetched_at",
]
DEDUP_KEYS = ["ticker", "fiscal_year", "reprt_code", "idx_cl_code", "idx_nm"]


def indicators_path(data_dir: Path, ticker: str) -> Path:
    return Path(data_dir) / "dart_indicators" / f"{ticker}.parquet"


def load_indicators(data_dir: Path, ticker: str) -> pd.DataFrame:
    """저장된 DART 재무지표 로드. 없으면 빈 DataFrame."""
    df = store.read_parquet_safe(indicators_path(data_dir, ticker))
    return df if df is not None else pd.DataFrame(columns=INDICATOR_COLUMNS)


def _safe_float(v: Any) -> float:
    """DART 지표 문자열을 float로. '-', '', None은 NaN."""
    if v is None:
        return float("nan")
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "N/A"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


ALL_IDX_CLASSES = (
    DartClient.IDX_PROFITABILITY,   # M210000 수익성 (ROE, ROA, 이익률 등)
    DartClient.IDX_STABILITY,        # M220000 안정성 (부채비율, 자기자본비율 등)
    DartClient.IDX_GROWTH,           # M230000 성장성 (매출/영업이익/순이익 증가율)
    DartClient.IDX_ACTIVITY,         # M240000 활동성 (회전율, 배당성향 등)
)


def fetch_indicators_for_ticker(
    dart: DartClient,
    ticker: str,
    asof: str,
    years: int = 5,
    *,
    idx_classes: tuple[str, ...] = ALL_IDX_CLASSES,
) -> pd.DataFrame:
    """직전 ``years``년 사업보고서(annual)의 재무지표 fetch.

    각 연도마다 ``idx_classes``의 모든 지표분류를 1회씩 호출. 기본은 수익성지표(ROE 등)만.

    Note: DART 사업보고서는 익년 3~5월 공시되므로, asof 시점에 가장 최근 *완결* 사업연도는
    (asof_year - 1)일 수도 있음. 보수적으로 (asof_year - 1) 기준으로 직전 years년치를 받는다.
    """
    asof_year = int(asof[:4])
    # 가장 최근 발표 가능 연도 = asof_year - 1 (사업보고서는 결산 후 3개월 내 공시).
    # 진행 중 사업연도(asof_year)도 한 번 시도 (반기/분기 보고서가 있을 수 있음, 단 annual만 받음).
    end_year = asof_year - 1
    start_year = end_year - years + 1
    rows: list[dict[str, Any]] = []
    for y in range(start_year, end_year + 1):
        for idx_cl in idx_classes:
            items = dart.fetch_financial_indicators(
                ticker, bsns_year=y, idx_cl_code=idx_cl,
                reprt_code=DartClient.REPORT_ANNUAL,
            )
            for item in items:
                rows.append({
                    "ticker": ticker,
                    "fiscal_year": y,
                    "reprt_code": DartClient.REPORT_ANNUAL,
                    "idx_cl_code": item.get("idx_cl_code", idx_cl),
                    "idx_cl_nm": item.get("idx_cl_nm", ""),
                    "idx_nm": (item.get("idx_nm") or "").strip(),
                    "idx_val": _safe_float(item.get("idx_val")),
                    "fetched_at": asof,
                })
    if not rows:
        return pd.DataFrame(columns=INDICATOR_COLUMNS)
    df = pd.DataFrame(rows, columns=INDICATOR_COLUMNS)
    df["fiscal_year"] = df["fiscal_year"].astype("Int64")
    df = df.drop_duplicates(subset=DEDUP_KEYS, keep="last").reset_index(drop=True)
    return df


def sync_dart_indicators(
    dart: DartClient,
    data_dir: Path,
    tickers: list[str],
    asof: str,
    years: int = 5,
) -> dict[str, Any]:
    """여러 종목 DART 재무지표 incremental upsert.

    Returns
    -------
    {'total': N, 'updated': K, 'no_data': M, 'failed': [(ticker, err), ...]}
    """
    stats: dict[str, Any] = {"total": len(tickers), "updated": 0, "no_data": 0, "failed": []}
    for ticker in tickers:
        try:
            df = fetch_indicators_for_ticker(dart, ticker, asof=asof, years=years)
        except DartQuotaExceeded as e:
            log.error("DART quota exceeded — aborting sync at %s. %s", ticker, e)
            stats["failed"].append((ticker, "DART quota exceeded"))
            stats["aborted"] = True
            return stats
        except Exception as e:  # noqa: BLE001 — 한 종목 실패가 전체 중단되면 안 됨
            log.warning("[%s] sync_dart_indicators failed: %s", ticker, e)
            stats["failed"].append((ticker, str(e)))
            continue
        if df.empty:
            stats["no_data"] += 1
            continue
        added, _ = store.upsert_dedup(
            indicators_path(data_dir, ticker), df,
            dedup_keys=DEDUP_KEYS,
            sort_keys=["fiscal_year", "idx_cl_code", "idx_nm"],
        )
        if added > 0:
            stats["updated"] += 1
    return stats


def extract_annual_roe(df: pd.DataFrame) -> dict[int, float]:
    """``store/dart_indicators/{ticker}.parquet`` DataFrame에서 연도별 ROE(%) 추출."""
    if df is None or df.empty:
        return {}
    roe = df[df["idx_nm"] == "ROE"][["fiscal_year", "idx_val"]].dropna()
    if roe.empty:
        return {}
    # 연도별 마지막 값 (가장 최근 fetched_at 우선이지만 dedup 후라 1행)
    return {int(r["fiscal_year"]): float(r["idx_val"]) for _, r in roe.iterrows()}
