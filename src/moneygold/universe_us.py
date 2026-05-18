"""미국 종목 마스터 (S&P 500 또는 NASDAQ Trader 전종목).

두 가지 소스 지원:
- ``source="sp500"``: Wikipedia 'List_of_S&P_500_companies' (≈505종목) + GitHub CSV 폴백
- ``source="nasdaq_trader"``: NASDAQ Trader Symbol Directory의 nasdaqlisted.txt +
  otherlisted.txt (NYSE/NASDAQ/AMEX listed 합산, Test/ETF/Fund 제외 후 ≈6-7k)

두 소스 모두 yfinance Ticker.info로 sector/industry/marketCap을 보강 가능.
시총은 시가 기준이라 sync 때마다 갱신.
"""
from __future__ import annotations

import io
import logging
import re
import time
import warnings
from typing import Literal

import pandas as pd
import requests

log = logging.getLogger(__name__)


_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_GITHUB_CSV = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "master/data/constituents.csv"
)
_NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
_OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0 Safari/537.36"
)

# Security Name으로 일반주 외(ETF/펀드/우선주/워런트/유닛) 제외
# NASDAQ Trader 'ETF' 컬럼은 일부 펀드 누락이라 이름 기반 보강
_NON_COMMON_PATTERNS = re.compile(
    r"\b(?:"
    r"ETF|ETN|Fund|Trust|SPAC|Acquisition Corp|"
    r"Preferred|Depositary|ADR Preferred|"
    r"Warrant|Right|Rights|Unit|Units|"
    r"Notes?|Debenture|Bond"
    r")\b",
    re.IGNORECASE,
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


def _parse_nasdaq_trader_text(text: str, *, kind: Literal["nasdaq", "other"]) -> pd.DataFrame:
    """nasdaqlisted.txt / otherlisted.txt pipe-delimited 텍스트 파싱.

    파일 마지막 줄은 'File Creation Time: ...|||...' 푸터라 제외.
    """
    df = pd.read_csv(io.StringIO(text), sep="|", dtype=str)
    # 푸터 행은 첫 컬럼에 "File Creation Time"이 들어옴
    first_col = df.columns[0]
    df = df[~df[first_col].astype(str).str.startswith("File Creation Time", na=False)].copy()

    if kind == "nasdaq":
        # Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
        out = pd.DataFrame({
            "ticker": df["Symbol"].astype(str),
            "name": df["Security Name"].astype(str),
            "exchange": "NASDAQ",
            "test_issue": df["Test Issue"].fillna("N").astype(str),
            "etf_flag": df["ETF"].fillna("N").astype(str),
            "financial_status": df.get("Financial Status", pd.Series(["N"] * len(df))).fillna("N").astype(str),
        })
    else:
        # ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
        exch_map = {"A": "NYSE_AMERICAN", "N": "NYSE", "P": "NYSE_ARCA", "Z": "CBOE_BZX", "V": "IEXG"}
        out = pd.DataFrame({
            "ticker": df["ACT Symbol"].astype(str),
            "name": df["Security Name"].astype(str),
            "exchange": df["Exchange"].astype(str).map(exch_map).fillna(df["Exchange"].astype(str)),
            "test_issue": df["Test Issue"].fillna("N").astype(str),
            "etf_flag": df["ETF"].fillna("N").astype(str),
            "financial_status": "N",
        })
    return out


def fetch_nasdaq_trader_listed() -> pd.DataFrame:
    """NASDAQ Trader Symbol Directory에서 NYSE+NASDAQ+AMEX 전종목.

    필터 적용 (보통주만 잔존):
    - Test Issue == 'N'
    - Financial Status == 'N' (정상; nasdaqlisted만 해당)
    - ETF 플래그 'N' AND Security Name에 ETF/Fund/Trust/Preferred/Warrant/Unit 등 비포함
    - ticker 정규화: '.' → '-', '$' / '^' (preferred/right 종목) 제외

    Returns
    -------
    DataFrame  columns = ['ticker', 'name', 'exchange']
    """
    try:
        r = requests.get(_NASDAQ_LISTED_URL, headers={"User-Agent": _UA}, timeout=20)
        r.raise_for_status()
        nasdaq = _parse_nasdaq_trader_text(r.text, kind="nasdaq")
    except Exception as e:
        raise RuntimeError(f"nasdaqlisted.txt fetch 실패: {e}") from e

    try:
        r = requests.get(_OTHER_LISTED_URL, headers={"User-Agent": _UA}, timeout=20)
        r.raise_for_status()
        other = _parse_nasdaq_trader_text(r.text, kind="other")
    except Exception as e:
        raise RuntimeError(f"otherlisted.txt fetch 실패: {e}") from e

    raw = pd.concat([nasdaq, other], ignore_index=True)
    n_raw = len(raw)

    # 필터
    df = raw[
        (raw["test_issue"] == "N")
        & (raw["etf_flag"] == "N")
        & (raw["financial_status"] == "N")
    ].copy()
    # 우선주/워런트/유닛 등 비보통주는 ticker에 $ ^ . 가 포함 — '.'은 BRK.B 같은
    # 클래스 표기라 '-'로 치환, 나머지($, ^)는 제외
    df = df[~df["ticker"].str.contains(r"[\$\^]", regex=True, na=False)]
    df["ticker"] = df["ticker"].str.replace(".", "-", regex=False)
    # 이름 기반 비보통주 제거
    df = df[~df["name"].fillna("").str.contains(_NON_COMMON_PATTERNS, regex=True, na=False)]

    df = df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    log.info(
        "NASDAQ Trader: raw=%d filtered=%d (Test/ETF/비보통주 제외)",
        n_raw, len(df),
    )
    return df[["ticker", "name", "exchange"]]


def enrich_with_mcap(
    df: pd.DataFrame,
    *,
    mcap_min_usd: float | None = None,
    sleep_s: float = 0.1,
    progress: bool = True,
) -> pd.DataFrame:
    """yfinance Ticker.info에서 marketCap 보강 + (필요 시) sector 갱신.

    Parameters
    ----------
    mcap_min_usd
        지정 시 mcap < threshold 종목 제외. None이면 컷 없음.
        info 호출이 실패해 mcap=0인 종목도 제외됨에 주의.

    시총은 시가 기준이라 매번 갱신해야 함. ticker 별 ~0.5-1초.
    """
    import yfinance as yf
    from tqdm import tqdm

    has_industry = "industry" in df.columns
    rows: list[dict] = []
    iterator = (
        tqdm(df.itertuples(index=False), total=len(df), desc="yf info", unit="tk")
        if progress
        else df.itertuples(index=False)
    )
    for row in iterator:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                info = yf.Ticker(row.ticker).info or {}
            mcap = float(info.get("marketCap") or 0)
            sector = info.get("sector") or getattr(row, "sector", "UNKNOWN")
            industry = info.get("industry") or (
                getattr(row, "industry", "UNKNOWN") if has_industry else "UNKNOWN"
            )
        except Exception:
            mcap = 0.0
            sector = getattr(row, "sector", "UNKNOWN")
            industry = getattr(row, "industry", "UNKNOWN") if has_industry else "UNKNOWN"

        if mcap_min_usd is not None and mcap < mcap_min_usd:
            if sleep_s:
                time.sleep(sleep_s)
            continue

        rows.append({
            "ticker": row.ticker,
            "name": getattr(row, "name", ""),
            "sector": sector,
            "industry": industry,
            "mcap": mcap,
        })
        if sleep_s:
            time.sleep(sleep_s)
    out = pd.DataFrame(rows)
    if mcap_min_usd is not None:
        log.info(
            "enrich_with_mcap: input=%d output=%d (mcap >= $%.0fM)",
            len(df), len(out), mcap_min_usd / 1e6,
        )
    return out


def fetch_master_us(
    *,
    source: Literal["sp500", "nasdaq_trader"] = "sp500",
    enrich: bool = True,
    mcap_min_usd: float | None = None,
) -> pd.DataFrame:
    """미국 종목 마스터.

    Parameters
    ----------
    source
        ``"sp500"`` (기본, 호환 유지) 또는 ``"nasdaq_trader"`` (NYSE+NASDAQ+AMEX 전종목).
    enrich
        True면 yfinance Ticker.info로 sector/industry/mcap 보강.
        nasdaq_trader 소스는 보강 없이는 mcap이 0이라 mcap_min_usd 필터가 무의미.
    mcap_min_usd
        지정 시 enrich 단계에서 mcap < threshold 종목 제외. enrich=False면 무시.

    Returns
    -------
    DataFrame  columns = ['ticker', 'name', 'market', 'sector', 'mcap', 'industry']
        market = 'US' 고정.
    """
    if source == "sp500":
        base = fetch_sp500_constituents()
    elif source == "nasdaq_trader":
        base = fetch_nasdaq_trader_listed()
        # sector/industry는 enrich에서 채움
        base["sector"] = "UNKNOWN"
        base["industry"] = "UNKNOWN"
    else:
        raise ValueError(f"Unknown source: {source!r} (expected 'sp500' or 'nasdaq_trader')")

    if enrich:
        base = enrich_with_mcap(base, mcap_min_usd=mcap_min_usd)
    else:
        if "mcap" not in base.columns:
            base["mcap"] = 0.0

    base["market"] = "US"
    cols = ["ticker", "name", "market", "sector", "mcap", "industry"]
    for c in cols:
        if c not in base.columns:
            base[c] = "UNKNOWN" if c in ("sector", "industry") else 0
    return base[cols]
