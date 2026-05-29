"""DART 사업보고서 주요사항 + 회사정보 + 원본 재무제표 sync.

DGI 점수표 보강이 아닌 *상세 정보 캐시* 용도. Streamlit 상세 화면 또는 별도 분석에 사용.

저장 구조 (모두 ``store/dart_business/`` 하위):
  share_issuance/{ticker}.parquet     # 증자/감자 이력 (irdsSttus)
  treasury_status/{ticker}.parquet    # 자기주식 흐름 (tesstkAcqsDspsSttus)
  financials_raw/{ticker}.parquet     # 전체 재무제표 raw (fnlttSinglAcntAll)
  company_info/{ticker}.json          # 회사 기본정보 (company)

대상 종목 범위 helper도 함께 제공.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..strategies.value_long_term.dart_client import DartClient, DartQuotaExceeded
from . import store

log = logging.getLogger(__name__)


# ============================================================
# Scope helpers — KOSPI200 + KOSDAQ150 등 종목 범위 정의
# ============================================================

def kospi200_kosdaq150_tickers(asof: str | None = None) -> list[str]:
    """KOSPI200 + KOSDAQ150 구성 종목 합집합. pykrx 직접 호출.

    asof는 YYYYMMDD. None이면 가장 최근 영업일 (pykrx 내부 기본).
    """
    from pykrx import stock as _pkx

    out: set[str] = set()
    for code in ("1028", "2203"):  # KOSPI200, KOSDAQ150
        try:
            if asof:
                tickers = _pkx.get_index_portfolio_deposit_file(date=asof, ticker=code)
            else:
                tickers = _pkx.get_index_portfolio_deposit_file(ticker=code)
        except Exception as e:  # noqa: BLE001
            log.warning("KOSPI200/KOSDAQ150 fetch failed for %s: %s", code, e)
            continue
        out.update(tickers)
    return sorted(out)


# ============================================================
# Storage layout
# ============================================================

def share_issuance_path(data_dir: Path, ticker: str) -> Path:
    return Path(data_dir) / "dart_business" / "share_issuance" / f"{ticker}.parquet"


def treasury_status_path(data_dir: Path, ticker: str) -> Path:
    return Path(data_dir) / "dart_business" / "treasury_status" / f"{ticker}.parquet"


def financials_raw_path(data_dir: Path, ticker: str) -> Path:
    return Path(data_dir) / "dart_business" / "financials_raw" / f"{ticker}.parquet"


def company_info_path(data_dir: Path, ticker: str) -> Path:
    return Path(data_dir) / "dart_business" / "company_info" / f"{ticker}.json"


# ============================================================
# Normalize raw DART rows → DataFrame
# ============================================================

def _normalize_rows(
    rows: list[dict[str, Any]],
    ticker: str, fiscal_year: int, asof: str,
) -> pd.DataFrame:
    """DART list 응답 → DataFrame. ticker/fiscal_year/fetched_at 추가."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ticker"] = ticker
    df["fiscal_year"] = fiscal_year
    df["fetched_at"] = asof
    return df


# ============================================================
# Per-ticker fetchers
# ============================================================

def fetch_business_for_ticker(
    dart: DartClient, ticker: str, asof: str, years: int = 5,
) -> dict[str, pd.DataFrame]:
    """단일 종목 × ``years`` 사업연도 × 3개 endpoint 호출.

    Returns
    -------
    dict with keys 'share_issuance', 'treasury_status', 'financials_raw'.
    각 값은 다년치 합친 DataFrame. fiscal_year 컬럼 포함.
    """
    asof_year = int(asof[:4])
    end_year = asof_year - 1  # 사업보고서 직전 완결
    start_year = end_year - years + 1

    out: dict[str, list[pd.DataFrame]] = {
        "share_issuance": [], "treasury_status": [], "financials_raw": [],
    }
    for y in range(start_year, end_year + 1):
        si_rows = dart.fetch_share_issuance(ticker, y)
        out["share_issuance"].append(_normalize_rows(si_rows, ticker, y, asof))
        ts_rows = dart.fetch_treasury_status(ticker, y)
        out["treasury_status"].append(_normalize_rows(ts_rows, ticker, y, asof))
        fr_rows = dart.fetch_financial_statements_all(ticker, y)
        out["financials_raw"].append(_normalize_rows(fr_rows, ticker, y, asof))

    return {k: pd.concat([df for df in v if not df.empty], ignore_index=True)
                if any(not df.empty for df in v) else pd.DataFrame()
            for k, v in out.items()}


# ============================================================
# Sync orchestrator
# ============================================================

def sync_dart_business(
    dart: DartClient,
    data_dir: Path,
    tickers: list[str],
    asof: str,
    years: int = 5,
    *,
    include_company_info: bool = True,
    log_every: int = 50,
) -> dict[str, Any]:
    """여러 종목 사업보고서 주요사항 + 회사정보 sync.

    Returns
    -------
    {'total': N, 'updated': K, 'no_data': M, 'failed': [(ticker, err), ...]}
    """
    stats: dict[str, Any] = {
        "total": len(tickers), "updated": 0, "no_data": 0, "failed": [],
    }
    for i, ticker in enumerate(tickers, 1):
        if i % log_every == 0:
            log.info("[dart_business] progress %d / %d (%s)", i, len(tickers), ticker)
        try:
            dfs = fetch_business_for_ticker(dart, ticker, asof=asof, years=years)
            if include_company_info:
                info = dart.fetch_company_info(ticker)
                if info:
                    path = company_info_path(data_dir, ticker)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(path.suffix + ".tmp")
                    with tmp.open("w", encoding="utf-8") as f:
                        json.dump({**info, "fetched_at": asof}, f, ensure_ascii=False)
                    tmp.replace(path)
        except DartQuotaExceeded as e:
            log.error("DART quota exceeded — aborting sync at %s. %s", ticker, e)
            stats["failed"].append((ticker, "DART quota exceeded"))
            stats["aborted"] = True
            return stats
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] sync_dart_business failed: %s", ticker, e)
            stats["failed"].append((ticker, str(e)))
            continue

        any_written = False
        if not dfs["share_issuance"].empty:
            store.write_parquet_atomic(dfs["share_issuance"],
                                        share_issuance_path(data_dir, ticker))
            any_written = True
        if not dfs["treasury_status"].empty:
            store.write_parquet_atomic(dfs["treasury_status"],
                                        treasury_status_path(data_dir, ticker))
            any_written = True
        if not dfs["financials_raw"].empty:
            store.write_parquet_atomic(dfs["financials_raw"],
                                        financials_raw_path(data_dir, ticker))
            any_written = True

        if any_written:
            stats["updated"] += 1
        else:
            stats["no_data"] += 1
    return stats


# ============================================================
# Loaders (UI / 분석용)
# ============================================================

def load_share_issuance(data_dir: Path, ticker: str) -> pd.DataFrame:
    df = store.read_parquet_safe(share_issuance_path(data_dir, ticker))
    return df if df is not None else pd.DataFrame()


def load_treasury_status(data_dir: Path, ticker: str) -> pd.DataFrame:
    df = store.read_parquet_safe(treasury_status_path(data_dir, ticker))
    return df if df is not None else pd.DataFrame()


def load_financials_raw(data_dir: Path, ticker: str) -> pd.DataFrame:
    df = store.read_parquet_safe(financials_raw_path(data_dir, ticker))
    return df if df is not None else pd.DataFrame()


def load_company_info(data_dir: Path, ticker: str) -> dict | None:
    path = company_info_path(data_dir, ticker)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
