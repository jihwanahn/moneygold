"""SEC EDGAR XBRL companyfacts — 미국 상장사 분기 재무 (full history).

yfinance가 분기 데이터를 5개만 제공하는 한계를 해결하기 위한 대체 소스.
SEC EDGAR는:
  - 무료, API 키 불필요
  - 분기 단독 데이터 17+년 (1996 XBRL 도입 이후 모든 10-Q/10-K)
  - S&P500의 99.8% 커버 (US 마스터 3192/3197)
  - Rate limit: 10 req/sec (User-Agent 필수, 연락처 포함)

엔드포인트:
  companyfacts: https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json
  ticker map:   https://www.sec.gov/files/company_tickers.json

XBRL의 함정:
  - 동일한 보고서가 'YTD 누적' + '분기 단독' 양쪽 모두 포함 가능 → (end - start) 일수로 필터
  - 동일 분기를 여러 태그가 보고 (예: ASC 606 회계기준 변경 전후로 'Revenues' →
    'RevenueFromContractWithCustomerExcludingAssessedTax'): TAG 별칭 머지 필요
  - 정정 공시 (10-Q/A) → 같은 (fy, fp) 다중 entry: 최신 filed 날짜 우선

결과 스키마는 yf_client.fetch_quarterly_financials 와 동일 (calendar year/q 기준):
    ['quarter', 'year', 'q', 'revenue', 'op_income', 'net_income',
     'op_margin', 'eps', 'revenue_yoy', 'op_income_yoy', 'eps_yoy']
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# us-gaap XBRL 태그 별칭 — 회사·연도별 사용 태그가 달라 fallback 필요.
# 리스트 순서: 가장 일반/오래된 태그 → 모던 태그. 머지 시 *최신 filed* 우선이라
# 같은 분기를 둘 다 보고하면 자연스레 modern 태그가 이긴다.
_REVENUE_TAGS = [
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
]
_OP_INCOME_TAGS = [
    "OperatingIncomeLoss",
    "IncomeLossFromContinuingOperationsBeforeInterestExpenseInterestIncomeIncomeTaxesExtraordinaryItemsNoncontrollingInterestsNet",
]
_NET_INCOME_TAGS = [
    "NetIncomeLoss",
    "ProfitLoss",
    "NetIncomeLossAvailableToCommonStockholdersBasic",
]
_DILUTED_EPS_TAGS = [
    "EarningsPerShareDiluted",
    "IncomeLossFromContinuingOperationsPerDilutedShare",
]
_BASIC_EPS_TAGS = [
    "EarningsPerShareBasic",
    "IncomeLossFromContinuingOperationsPerBasicShare",
]

_SEC_BASE = "https://data.sec.gov"
_SEC_WWW = "https://www.sec.gov"
_DEFAULT_UA = "moneygold/0.1 contact: jhahn@inventis.co.kr"
_QUARTER_DAYS = (80, 100)        # 분기 단독으로 인정할 end-start 일수 범위
_RATE_LIMIT_SLEEP = 0.12         # 10 req/sec 한도 내


def _get(url: str, *, ua: str = _DEFAULT_UA, timeout: float = 20) -> bytes:
    """SEC HTTP GET with User-Agent. 429/5xx 시 1회 backoff 후 재시도."""
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError(f"SEC request failed after retries: {url}")


# ============================================================
# Ticker → CIK
# ============================================================

def fetch_ticker_cik_map(
    cache_path: Path,
    *,
    ua: str = _DEFAULT_UA,
    max_age_days: int = 7,
) -> dict[str, str]:
    """SEC의 ticker→CIK json을 다운받아 캐싱 (기본 7일 TTL).

    Returns
    -------
    {ticker: zero-padded 10-digit CIK string}
    """
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < max_age_days * 86400:
            try:
                return json.loads(cache_path.read_text())
            except Exception:
                pass

    raw = _get(f"{_SEC_WWW}/files/company_tickers.json", ua=ua)
    data = json.loads(raw)
    m: dict[str, str] = {}
    # data: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    for v in data.values():
        t = str(v.get("ticker", "")).strip().upper()
        cik = v.get("cik_str")
        if not t or cik is None:
            continue
        m[t] = f"{int(cik):010d}"

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False))
    tmp.replace(cache_path)
    log.info("SEC ticker→CIK map cached: %d tickers", len(m))
    return m


def _normalize_ticker_for_sec(ticker: str) -> str:
    """SEC map keys: BRK-B is stored as 'BRK-B' (with hyphen), same as our master.
    yfinance도 BRK-B 형식 사용 → 별도 변환 불필요.
    """
    return ticker.strip().upper()


# ============================================================
# Company facts → quarterly DataFrame
# ============================================================

def fetch_company_facts(cik: str, *, ua: str = _DEFAULT_UA) -> dict:
    """SEC EDGAR companyfacts JSON. 404면 빈 dict."""
    cik = cik.zfill(10)
    try:
        raw = _get(f"{_SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json", ua=ua)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise
    return json.loads(raw)


# 기간 분류 (XBRL duration fact의 일수 기반)
#   Q   : 분기 단독 (~91일)
#   H1  : YTD 6M (~181일)
#   YTD9: YTD 9M (~272일)
#   FY  : 회계연도 (~363일)
_DAYS_Q = (80, 100)
_DAYS_H1 = (175, 195)
_DAYS_Y9 = (260, 285)
_DAYS_FY = (350, 380)


def _classify_period(d: dict) -> str | None:
    s, e = d.get("start"), d.get("end")
    if not s or not e:
        return None
    try:
        days = (date.fromisoformat(e) - date.fromisoformat(s)).days
    except ValueError:
        return None
    if _DAYS_Q[0] <= days <= _DAYS_Q[1]: return "Q"
    if _DAYS_H1[0] <= days <= _DAYS_H1[1]: return "H1"
    if _DAYS_Y9[0] <= days <= _DAYS_Y9[1]: return "Y9"
    if _DAYS_FY[0] <= days <= _DAYS_FY[1]: return "FY"
    return None


def _is_quarterly_standalone(d: dict) -> bool:
    """end - start ∈ [80, 100]일 이면 분기 단독 (1Q ≈ 91일).

    XBRL의 모든 duration fact는 start/end를 가짐. YTD 누적(181/272/363일)을 걸러냄.
    """
    s, e = d.get("start"), d.get("end")
    if not s or not e:
        return False
    try:
        days = (date.fromisoformat(e) - date.fromisoformat(s)).days
    except ValueError:
        return False
    return _QUARTER_DAYS[0] <= days <= _QUARTER_DAYS[1]


def _extract_by_tags(
    facts: dict,
    tags: list[str],
    *,
    unit: str = "USD",
) -> dict[str, dict]:
    """주어진 태그들에서 분기 단독 entries 추출 → {end_date: latest_entry}.

    Standalone Q (~91일)가 SEC에 없는 케이스(주로 회계 Q4 - 10-K에만 FY로 들어감)는
    YTD-누적 차감으로 합성: Q4 = FY - YTD9M.
    Standalone Q가 이미 있으면 그것 우선 (합성보다 정확).

    여러 태그가 같은 end_date를 보고할 경우 최신 filed 우선 (모던 태그가 보통 최신).
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    # 각 기간 유형별 best entry (end_date 기준, 같은 end는 최신 filed)
    by_end_q: dict[str, dict] = {}
    by_end_h1: dict[str, dict] = {}
    by_end_y9: dict[str, dict] = {}
    by_end_fy: dict[str, dict] = {}
    period_map = {"Q": by_end_q, "H1": by_end_h1, "Y9": by_end_y9, "FY": by_end_fy}

    for tag in tags:
        if tag not in us_gaap:
            continue
        units = us_gaap[tag].get("units", {})
        for d in units.get(unit, []):
            kind = _classify_period(d)
            if kind is None:
                continue
            target = period_map[kind]
            end = d["end"]
            cur = target.get(end)
            if cur is None or d.get("filed", "") > cur.get("filed", ""):
                target[end] = d

    # YTD → 분기 단독 합성. start_date(회계연도 시작)로 같은 FY 묶음 찾기.
    # Q4_synth: FY - Y9   (start 같음, end는 fy.end)
    # Q3_synth: Y9 - H1   (start 같음, end는 y9.end)
    # Q2_synth: H1 - Q1   (start 같음, end는 h1.end)
    def _by_start(by_end: dict[str, dict]) -> dict[str, dict]:
        m: dict[str, dict] = {}
        for d in by_end.values():
            s = d.get("start")
            if s and s not in m:
                m[s] = d
        return m

    q1_by_start = _by_start(by_end_q)  # 회계연도 시작 = fiscal Q1 시작
    h1_by_start = _by_start(by_end_h1)
    y9_by_start = _by_start(by_end_y9)

    # FY 엔트리 순회 → Q4 합성
    for fy in by_end_fy.values():
        fy_start = fy.get("start")
        if not fy_start:
            continue
        y9 = y9_by_start.get(fy_start)
        if not y9:
            continue
        try:
            fy_val = float(fy["val"])
            y9_val = float(y9["val"])
        except (KeyError, TypeError, ValueError):
            continue
        q4_end = fy["end"]
        if q4_end in by_end_q:
            continue   # standalone Q4 이미 있음
        q4_start = y9["end"]   # YTD 9M의 끝 = Q4 시작
        by_end_q[q4_end] = {
            "start": q4_start, "end": q4_end,
            "val": fy_val - y9_val,
            "filed": fy.get("filed", ""),
            "fp": "Q4", "form": "synth-FY-Y9",
            "_synthesized": True,
        }

    # Y9 엔트리 → Q3 합성
    for y9 in by_end_y9.values():
        s = y9.get("start")
        h1 = h1_by_start.get(s) if s else None
        if not h1:
            continue
        try:
            y9_val = float(y9["val"]); h1_val = float(h1["val"])
        except (KeyError, TypeError, ValueError):
            continue
        q3_end = y9["end"]
        if q3_end in by_end_q:
            continue
        by_end_q[q3_end] = {
            "start": h1["end"], "end": q3_end,
            "val": y9_val - h1_val,
            "filed": y9.get("filed", ""),
            "fp": "Q3", "form": "synth-Y9-H1",
            "_synthesized": True,
        }

    # H1 엔트리 → Q2 합성
    for h1 in by_end_h1.values():
        s = h1.get("start")
        q1 = q1_by_start.get(s) if s else None
        if not q1:
            continue
        try:
            h1_val = float(h1["val"]); q1_val = float(q1["val"])
        except (KeyError, TypeError, ValueError):
            continue
        q2_end = h1["end"]
        if q2_end in by_end_q:
            continue
        by_end_q[q2_end] = {
            "start": q1["end"], "end": q2_end,
            "val": h1_val - q1_val,
            "filed": h1.get("filed", ""),
            "fp": "Q2", "form": "synth-H1-Q1",
            "_synthesized": True,
        }

    return by_end_q


def extract_quarterly_financials(facts: dict) -> pd.DataFrame:
    """companyfacts JSON → 분기 손익 DataFrame.

    yf_client.fetch_quarterly_financials 와 동일 스키마:
      ['quarter', 'year', 'q', 'revenue', 'op_income', 'net_income',
       'op_margin', 'eps', 'revenue_yoy', 'op_income_yoy', 'eps_yoy']

    빈 facts 입력은 빈 DataFrame 반환.
    """
    if not facts:
        return pd.DataFrame()

    rev_by_end = _extract_by_tags(facts, _REVENUE_TAGS, unit="USD")
    op_by_end = _extract_by_tags(facts, _OP_INCOME_TAGS, unit="USD")
    ni_by_end = _extract_by_tags(facts, _NET_INCOME_TAGS, unit="USD")
    eps_d_by_end = _extract_by_tags(facts, _DILUTED_EPS_TAGS, unit="USD/shares")
    eps_b_by_end = _extract_by_tags(facts, _BASIC_EPS_TAGS, unit="USD/shares")

    all_ends = sorted(set(rev_by_end) | set(op_by_end) | set(ni_by_end)
                      | set(eps_d_by_end) | set(eps_b_by_end))
    if not all_ends:
        return pd.DataFrame()

    rows = []
    for end in all_ends:
        try:
            ed = date.fromisoformat(end)
        except ValueError:
            continue
        # 캘린더 연도/분기 (yf_client 의 'col.year', '(month-1)//3+1' 동일 규약)
        year = ed.year
        q = (ed.month - 1) // 3 + 1

        def _val(by_end: dict[str, dict]) -> float:
            d = by_end.get(end)
            if d is None:
                return float("nan")
            try:
                return float(d["val"])
            except (TypeError, ValueError):
                return float("nan")

        eps_d = _val(eps_d_by_end)
        eps_b = _val(eps_b_by_end)
        eps = eps_d if not np.isnan(eps_d) else eps_b

        rows.append({
            "quarter": f"{year}Q{q}",
            "year": year,
            "q": q,
            "revenue": _val(rev_by_end),
            "op_income": _val(op_by_end),
            "net_income": _val(ni_by_end),
            "eps": eps,
            "_end": end,
        })

    df = pd.DataFrame(rows)
    # 같은 (year, q)에 여러 end_date가 매핑되는 경우 (회계 종료일 변경 등): 최신 end 우선.
    df = df.sort_values("_end").drop_duplicates(["year", "q"], keep="last")
    df = df.drop(columns=["_end"]).sort_values(["year", "q"]).reset_index(drop=True)

    df["op_margin"] = (df["op_income"] / df["revenue"].replace(0, np.nan)) * 100.0
    df["net_margin"] = (df["net_income"] / df["revenue"].replace(0, np.nan)) * 100.0

    # YoY — fundamentals.py 의 동일 로직 재사용 (year, q 매칭, abs(prev) 분모로 부호 보존)
    from ..fundamentals import _attach_yoy_by_year_q
    _attach_yoy_by_year_q(df, [
        ("revenue", "revenue_yoy"),
        ("op_income", "op_income_yoy"),
        ("eps", "eps_yoy"),
    ])
    return df


# ============================================================
# High-level fetcher
# ============================================================

def fetch_financials_for_ticker(
    ticker: str,
    cik_map: dict[str, str],
    *,
    ua: str = _DEFAULT_UA,
) -> pd.DataFrame:
    """티커 → 분기 손익 DataFrame. CIK 없거나 404면 빈 DataFrame."""
    cik = cik_map.get(_normalize_ticker_for_sec(ticker))
    if not cik:
        return pd.DataFrame()
    facts = fetch_company_facts(cik, ua=ua)
    return extract_quarterly_financials(facts)


def sync_financials_us(
    data_dir: Path,
    tickers: list[str],
    *,
    force: bool = False,
    ua: str = _DEFAULT_UA,
    progress: bool = True,
) -> dict:
    """전체 US 종목 분기 손익 sync (SEC EDGAR).

    store/financials/{ticker}.parquet — yfinance 시절과 동일 스키마이므로
    fundamentals.build_fundamentals_from_cache 가 그대로 사용 가능.
    """
    from .. import fundamentals as fund
    from . import store

    cik_cache = data_dir / "meta" / "sec_ticker_cik.json"
    cik_map = fetch_ticker_cik_map(cik_cache, ua=ua)

    stats = {"total": len(tickers), "updated": 0, "cached": 0,
             "no_cik": 0, "no_data": 0, "failed": []}
    if progress:
        from tqdm import tqdm
        it = tqdm(tickers, desc="SEC financials", unit="tk")
    else:
        it = tickers

    for tk in it:
        path = fund.financials_path(data_dir, tk)
        if path.exists() and not force:
            stats["cached"] += 1
            continue
        norm = _normalize_ticker_for_sec(tk)
        if norm not in cik_map:
            stats["no_cik"] += 1
            continue
        try:
            df = fetch_financials_for_ticker(tk, cik_map, ua=ua)
            if df is None or df.empty:
                stats["no_data"] += 1
                continue
            store.write_parquet_atomic(df, path)
            stats["updated"] += 1
            time.sleep(_RATE_LIMIT_SLEEP)
        except Exception as e:
            stats["failed"].append((tk, str(e)))
            time.sleep(_RATE_LIMIT_SLEEP)
    return stats
