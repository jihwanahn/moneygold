"""S&P 500 종목 마스터 (미국).

Wikipedia 'List_of_S&P_500_companies' 첫 표를 파싱. User-Agent 헤더 필수
(yfinance / pandas requests 기본 UA는 차단됨). 실패 시 GitHub의
datasets/s-and-p-500-companies CSV로 폴백.

yfinance Ticker.info에서 sector/industry/marketCap 보강 (시총은 동적이라 sync 때마다).
"""
from __future__ import annotations

import io
import logging
import time
import warnings
from typing import Sequence

import pandas as pd
import requests

log = logging.getLogger(__name__)


_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_GITHUB_CSV = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "master/data/constituents.csv"
)
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"
)


def fetch_sp500_constituents() -> pd.DataFrame:
    """S&P 500 종목 마스터.

    Returns
    -------
    DataFrame  columns = ['ticker', 'name', 'sector', 'industry']
        ticker는 yfinance 호환 형식 (BRK.B → BRK-B 변환).
    """
    # 1) Wikipedia (UA 헤더로 우회)
    try:
        r = requests.get(_WIKI_URL, headers={"User-Agent": _UA}, timeout=15)
        r.raise_for_status()
        tables = pd.read_html(io.StringIO(r.text))
        df = tables[0]
        col_sym = next((c for c in df.columns if str(c).lower() in ("symbol", "ticker")), None)
        col_name = next((c for c in df.columns if "Security" in str(c) or "company" in str(c).lower()), None)
        col_sec = next((c for c in df.columns if "Sector" in str(c) and "Sub" not in str(c)), None)
        col_ind = next((c for c in df.columns if "Sub-Industry" in str(c) or "Industry" in str(c)), None)
        out = pd.DataFrame({
            "ticker": df[col_sym].astype(str).str.replace(".", "-", regex=False),
            "name": df[col_name] if col_name else "",
            "sector": df[col_sec] if col_sec else "UNKNOWN",
            "industry": df[col_ind] if col_ind else "UNKNOWN",
        })
        log.info("Fetched %d S&P 500 tickers from Wikipedia", len(out))
        return out
    except Exception as e:
        log.warning("Wikipedia fetch failed (%s) — falling back to GitHub CSV", e)

    # 2) GitHub fallback
    try:
        r = requests.get(_GITHUB_CSV, headers={"User-Agent": _UA}, timeout=15)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        out = pd.DataFrame({
            "ticker": df["Symbol"].astype(str).str.replace(".", "-", regex=False),
            "name": df.get("Security", df.get("Name", "")),
            "sector": df.get("GICS Sector", df.get("Sector", "UNKNOWN")),
            "industry": df.get("GICS Sub-Industry", df.get("Industry", "UNKNOWN")),
        })
        log.info("Fetched %d S&P 500 tickers from GitHub CSV", len(out))
        return out
    except Exception as e:
        raise RuntimeError(f"S&P 500 마스터 수집 실패 (Wiki + GitHub 모두): {e}")


def enrich_with_mcap(
    df: pd.DataFrame,
    *,
    sleep_s: float = 0.1,
    progress: bool = True,
) -> pd.DataFrame:
    """yfinance Ticker.info에서 marketCap 보강 + (필요 시) sector 갱신.

    시총은 시가 기준이라 매번 갱신해야 함. ticker 별 1초 미만.
    """
    import yfinance as yf
    from tqdm import tqdm

    rows = []
    iterator = tqdm(df.itertuples(index=False), total=len(df), desc="yf info", unit="tk") if progress else df.itertuples(index=False)
    for row in iterator:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                info = yf.Ticker(row.ticker).info or {}
            mcap = float(info.get("marketCap") or 0)
            sector = info.get("sector") or row.sector
            industry = info.get("industry") or row.industry
        except Exception:
            mcap = 0.0
            sector = row.sector
            industry = row.industry
        rows.append({
            "ticker": row.ticker, "name": row.name,
            "sector": sector, "industry": industry, "mcap": mcap,
        })
        if sleep_s:
            time.sleep(sleep_s)
    return pd.DataFrame(rows)


def fetch_master_us(*, enrich: bool = True) -> pd.DataFrame:
    """S&P 500 마스터 + (선택) yfinance mcap 보강.

    Returns
    -------
    DataFrame  columns = ['ticker', 'name', 'market', 'sector', 'mcap']
        market = 'US' 고정.
    """
    base = fetch_sp500_constituents()
    if enrich:
        base = enrich_with_mcap(base)
    base["market"] = "US"
    cols = ["ticker", "name", "market", "sector", "mcap"]
    if "industry" in base.columns:
        cols.append("industry")
    for c in cols:
        if c not in base.columns:
            base[c] = "UNKNOWN" if c in ("sector", "industry") else 0
    return base[cols]
