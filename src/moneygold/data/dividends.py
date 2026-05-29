"""배당 이력 동기화 + 로컬 parquet 캐시.

"가속화 장기투자"(DGI) 점수표의 배당 카테고리(40점)와 DRIP 시뮬레이터의 입력.

저장 경로: ``store/dividends/{ticker}.parquet``
(ticker, record_date, divi_kind) unique. tmp+atomic rename via ``store.upsert_dedup``.

스키마:
    ticker            (str)
    record_date       (str, YYYYMMDD)   배당기준일
    divi_kind         (str)             '결산' / '분기' / '중간' / '반기'
    per_sto_divi_amt  (float)           1주당 현금배당금 (원)
    divi_rate_pct     (float)           현금배당률(%)
    stk_divi_rate_pct (float)           주식배당률(%)
    divi_pay_dt       (str)             배당금지급일 (YYYYMMDD or '')
    stk_kind          (str)             '보통' / '우선' 등
    fetched_at        (str)             동기화 시점 YYYYMMDD (재현성용)
    fiscal_year       (Int64, nullable) 회계연도. pykrx 출처는 명시, KIS 출처는 NaN.
                                        scoring.annual_dps_per_year가 있으면 그대로 사용,
                                        없으면 record_date로부터 1~4월 결산은 전년 귀속.

데이터 소스:
  - **기본**: pykrx ``get_market_fundamental_by_date`` (KRX 공식 펀더멘털)
    DPS는 트레일링 12개월 누적값. 매 결산일 (보통 4~5월) 갱신.
    → 매 연도 12월 마지막 거래일 DPS = (그 연도 - 1) 회계연도 결산배당.
    분기·반기 배당은 별도 검출 어려움 — 결산 1행으로 합산 표기.
  - **레거시**: KIS 예탁원정보 (TR_ID HHKDB669102C0). 우리금융·메리츠 등 일부 종목은
    결산/분기 raw row가 정확하나 응답 페이지 잘림이 있어 분기 누락 케이스 보고됨.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from . import store
from .kis_client import KISAPIError, KISClient

log = logging.getLogger(__name__)


DIV_COLUMNS = [
    "ticker", "record_date", "divi_kind",
    "per_sto_divi_amt", "divi_rate_pct", "stk_divi_rate_pct",
    "divi_pay_dt", "stk_kind", "fetched_at", "fiscal_year",
]
DEDUP_KEYS = ["ticker", "record_date", "divi_kind", "stk_kind"]


def _safe_float(s: Any) -> float:
    """KIS 응답의 zero-padded 정수/소수 문자열을 float로. 빈값/이상값은 NaN."""
    if s is None:
        return float("nan")
    raw = str(s).strip().replace(",", "")
    if not raw:
        return float("nan")
    try:
        return float(raw)
    except ValueError:
        return float("nan")


def normalize_dividend_rows(
    rows: list[dict[str, Any]], ticker: str, asof: str,
) -> pd.DataFrame:
    """KIS output1 → 표준 스키마 DataFrame. ticker는 *호출자* 입력으로 강제.

    asof 인자는 fetched_at 컬럼에 기록 (재현성).
    """
    if not rows:
        return pd.DataFrame(columns=DIV_COLUMNS)
    out: list[dict[str, Any]] = []
    for r in rows:
        rec = (r.get("record_date") or "").strip()
        if len(rec) != 8 or not rec.isdigit():
            continue  # 잘못된 날짜는 버림
        out.append({
            "ticker": ticker,
            "record_date": rec,
            "divi_kind": (r.get("divi_kind") or "").strip(),
            "per_sto_divi_amt": _safe_float(r.get("per_sto_divi_amt")),
            "divi_rate_pct": _safe_float(r.get("divi_rate")),
            "stk_divi_rate_pct": _safe_float(r.get("stk_divi_rate")),
            "divi_pay_dt": (r.get("divi_pay_dt") or "").strip(),
            "stk_kind": (r.get("stk_kind") or "").strip(),
            "fetched_at": asof,
            "fiscal_year": pd.NA,  # KIS 출처는 record_date로 추론
        })
    df = pd.DataFrame(out, columns=DIV_COLUMNS)
    if df.empty:
        return df
    df["fiscal_year"] = df["fiscal_year"].astype("Int64")
    df = df.drop_duplicates(subset=DEDUP_KEYS, keep="last").reset_index(drop=True)
    return df


def dividends_path(data_dir: Path, ticker: str) -> Path:
    return Path(data_dir) / "dividends" / f"{ticker}.parquet"


def load_dividends(data_dir: Path, ticker: str) -> pd.DataFrame:
    """저장된 배당 이력 로드. 없으면 빈 DataFrame."""
    df = store.read_parquet_safe(dividends_path(data_dir, ticker))
    return df if df is not None else pd.DataFrame(columns=DIV_COLUMNS)


def _year_windows(start: datetime, end: datetime, window_days: int = 365) -> list[tuple[str, str]]:
    """[start, end]를 window_days 슬라이딩으로 잘라 (F_DT, T_DT) YYYYMMDD 쌍 리스트.

    KIS 배당일정은 연속조회 미지원이라 분할 호출이 필요. 1년 단위가 안전.
    """
    out: list[tuple[str, str]] = []
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=window_days - 1), end)
        out.append((cur.strftime("%Y%m%d"), nxt.strftime("%Y%m%d")))
        cur = nxt + timedelta(days=1)
    return out


def fetch_dividends_for_ticker(
    kis: KISClient,
    ticker: str,
    asof: str,
    years: int = 11,
) -> pd.DataFrame:
    """[Legacy] KIS 예탁원정보 기반 fetcher.

    pykrx fetcher(``fetch_dividends_pykrx``)가 기본. 이 함수는 KIS 응답이 필요한 회귀
    테스트나 KIS-특정 검증 용도로만 호출.

    1년 단위로 슬라이딩 호출 후 합쳐서 정규화. fiscal_year는 NaN으로 두고 record_date로
    추론되도록 둔다.
    """
    end_dt = datetime.strptime(asof, "%Y%m%d")
    start_dt = end_dt - timedelta(days=365 * years)
    all_rows: list[dict[str, Any]] = []
    for f_dt, t_dt in _year_windows(start_dt, end_dt):
        try:
            rows = kis.fetch_dividend_history(ticker, f_dt, t_dt)
        except KISAPIError as e:
            log.warning("[%s] dividend fetch %s~%s failed: %s", ticker, f_dt, t_dt, e)
            continue
        all_rows.extend(rows)
    return normalize_dividend_rows(all_rows, ticker, asof)


def _pykrx_yearend_dps(
    ticker: str, start_year: int, end_year: int, *, asof: str | None = None,
) -> pd.DataFrame:
    """pykrx fundamental에서 각 연도 12월 마지막 거래일의 DPS·DIV·EPS·BPS 추출.

    Parameters
    ----------
    asof : YYYYMMDD. 진행 중인 연도(end_year)의 경우 호출 종료일을 asof로 clip하여
           미래 날짜 호출을 피한다 (pykrx가 KRX 빈 응답에 컬럼 KeyError 던짐).

    Returns
    -------
    DataFrame  columns: year (int), last_date (YYYYMMDD), DPS, DIV, EPS, BPS

    Note: 회계연도 귀속은 *호출자*가 결정. 일반 규칙: 12월말 DPS = (year - 1) 회계연도 결산.
    """
    from pykrx import stock

    rows: list[dict[str, Any]] = []
    asof_dt = datetime.strptime(asof, "%Y%m%d") if asof else None
    for y in range(start_year, end_year + 1):
        # 연도 Q4 윈도우: 10/1 ~ 12/31. asof가 그 사이면 asof로 clip.
        win_start = f"{y}1001"
        win_end = f"{y}1231"
        if asof_dt is not None and asof_dt.year == y:
            if asof_dt < datetime(y, 10, 1):
                # 10월 전 → Q4 데이터 자체가 아직 없음. 그래도 진행 중 연도 마지막
                # 거래일 DPS는 의미 있으니 연초~asof로 폭넓게 fetch.
                win_start = f"{y}0101"
            win_end = asof
        try:
            df = stock.get_market_fundamental_by_date(win_start, win_end, ticker)
        except Exception as e:  # noqa: BLE001 — pykrx 일시 실패 → 해당 연도 skip
            log.debug("[%s] pykrx fundamental %d fetch failed (%s~%s): %s",
                      ticker, y, win_start, win_end, e)
            continue
        if df is None or df.empty:
            continue
        last = df.iloc[-1]
        rows.append({
            "year": y,
            "last_date": df.index[-1].strftime("%Y%m%d"),
            "DPS": float(last.get("DPS", 0) or 0),
            "DIV": float(last.get("DIV", 0) or 0),
            "EPS": float(last.get("EPS", 0) or 0),
            "BPS": float(last.get("BPS", 0) or 0),
        })
    return pd.DataFrame(rows)


def _resolve_yearend_date(year: int, asof_dt: datetime, max_backoff: int = 7) -> str | None:
    """그 연도의 *KRX 마지막 거래일* YYYYMMDD. 12/31부터 거꾸로 영업일 backoff.

    asof가 그 연도 12월 31일 전이면 asof로 clip (미래 호출 회피).
    """
    from pykrx import stock as _pkx

    if year > asof_dt.year:
        return None
    if year == asof_dt.year and asof_dt < datetime(year, 12, 31):
        target = asof_dt
    else:
        target = datetime(year, 12, 31)

    # 휴장일 backoff. pykrx의 nearest_business_day가 KRX 인증 필요한 케이스가 있어
    # 단순 수동 retry로 더 견고.
    for offset in range(max_backoff):
        d = target - timedelta(days=offset)
        try:
            df = _pkx.get_market_fundamental(d.strftime("%Y%m%d"), market="KOSPI")
        except Exception:  # noqa: BLE001
            continue
        if df is not None and not df.empty:
            return d.strftime("%Y%m%d")
    return None


def fetch_dividends_pykrx_batch(
    asof: str, years: int = 11, *, log_progress: bool = True,
) -> dict[str, pd.DataFrame]:
    """**일별 전종목 batch fetch** — 전 KR 종목의 12년치 fundamental을 24 호출로.

    각 연도 12월 마지막 거래일 × KOSPI + KOSDAQ 2시장. 단일 호출에 그 날 모든 종목.
    종목별 호출 (`fetch_dividends_pykrx`)이 2,500종목 × 12년 = 30k 호출인 데 비해
    이 함수는 12 × 2 = 24 호출.

    Returns
    -------
    dict[ticker -> DataFrame] : 종목별 DGI 호환 dividends DataFrame.
    """
    from pykrx import stock as _pkx

    asof_year = int(asof[:4])
    start_year = asof_year - years
    asof_dt = datetime.strptime(asof, "%Y%m%d")

    rows_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for y in range(start_year, asof_year + 1):
        last_date = _resolve_yearend_date(y, asof_dt)
        if last_date is None:
            if log_progress:
                log.info("[dividends batch] %d: skip (no business day)", y)
            continue
        if log_progress:
            log.info("[dividends batch] %d: %s ...", y, last_date)

        for market in ("KOSPI", "KOSDAQ"):
            try:
                df = _pkx.get_market_fundamental(last_date, market=market)
            except Exception as e:  # noqa: BLE001
                log.warning("[dividends batch] %s %s fetch failed: %s",
                            last_date, market, e)
                continue
            if df is None or df.empty:
                continue
            # index = 티커, columns = BPS, PER, PBR, EPS, DIV, DPS
            for ticker, row in df.iterrows():
                dps = float(row.get("DPS", 0) or 0)
                rows_by_ticker.setdefault(str(ticker), []).append({
                    "ticker": str(ticker),
                    "record_date": last_date,
                    "divi_kind": "결산",
                    "per_sto_divi_amt": dps,
                    "divi_rate_pct": float(row.get("DIV", 0) or 0),
                    "stk_divi_rate_pct": 0.0,
                    "divi_pay_dt": "",
                    "stk_kind": "보통",
                    "fetched_at": asof,
                    # 12월말 DPS = (y-1) 회계연도 결산
                    "fiscal_year": int(y) - 1,
                })

    out: dict[str, pd.DataFrame] = {}
    for ticker, rows in rows_by_ticker.items():
        df = pd.DataFrame(rows, columns=DIV_COLUMNS)
        df["fiscal_year"] = df["fiscal_year"].astype("Int64")
        df = df.drop_duplicates(subset=DEDUP_KEYS, keep="last").reset_index(drop=True)
        out[ticker] = df
    return out


def fetch_dividends_pykrx(
    ticker: str, asof: str, years: int = 11,
) -> pd.DataFrame:
    """pykrx fundamental 기반 배당 이력 fetch.

    각 연도 12월 마지막 거래일 DPS를 (그 연도 - 1) 회계연도 결산배당으로 귀속.
    record_date는 그 마지막 거래일을 그대로 사용 (실제 KRX 마지막 거래일이라 안정적).

    divi_kind = '결산' (연 1행). 분기·반기 분리는 미지원 — PR-H2에서 DART 공시로 보완 예정.
    stk_kind = '보통'.
    """
    asof_year = int(asof[:4])
    start_year = asof_year - years
    yearend = _pykrx_yearend_dps(ticker, start_year, asof_year, asof=asof)
    if yearend.empty:
        return pd.DataFrame(columns=DIV_COLUMNS)

    out: list[dict[str, Any]] = []
    for _, row in yearend.iterrows():
        dps = row["DPS"]
        if dps <= 0:
            # 0원도 의미는 있을 수 있음 (배당 중단) — 기록해두면 연속 인상 끊김 정확히 판별.
            pass
        out.append({
            "ticker": ticker,
            "record_date": row["last_date"],
            "divi_kind": "결산",
            "per_sto_divi_amt": dps,
            "divi_rate_pct": row["DIV"],
            "stk_divi_rate_pct": 0.0,
            "divi_pay_dt": "",
            "stk_kind": "보통",
            "fetched_at": asof,
            # 12월말 DPS = 직전 회계연도 결산 → fiscal_year = year - 1
            "fiscal_year": int(row["year"]) - 1,
        })
    df = pd.DataFrame(out, columns=DIV_COLUMNS)
    if not df.empty:
        df["fiscal_year"] = df["fiscal_year"].astype("Int64")
    return df


def sync_dividends(
    data_dir: Path,
    tickers: list[str],
    asof: str,
    years: int = 11,
    *,
    source: str = "pykrx_batch",
    kis: KISClient | None = None,
) -> dict[str, Any]:
    """여러 종목 배당 이력을 incremental upsert.

    Parameters
    ----------
    source : 'pykrx_batch' (기본 — 일별 전종목 24 호출), 'pykrx' (종목별 호출), 'kis'.

    각 종목별로 ``store/dividends/{ticker}.parquet``에 upsert (keep='last' → 신규 fetch가
    과거에 잘못 저장된 행 덮어쓰기).

    Returns
    -------
    {'total': N, 'updated': K, 'no_data': M, 'failed': [(ticker, err), ...]}
    """
    if source not in ("pykrx_batch", "pykrx", "kis"):
        raise ValueError(f"unknown source: {source!r}")
    if source == "kis" and kis is None:
        raise ValueError("source='kis' requires a KISClient instance")

    stats: dict[str, Any] = {"total": len(tickers), "updated": 0, "no_data": 0, "failed": []}
    wanted = set(tickers)

    if source == "pykrx_batch":
        # 한 번에 전 KR 종목 fetch → 요청된 ticker만 필터
        try:
            all_rows = fetch_dividends_pykrx_batch(asof=asof, years=years)
        except Exception as e:  # noqa: BLE001
            log.error("pykrx batch fetch failed: %s", e)
            stats["failed"].append(("__batch__", str(e)))
            return stats
        for ticker in tickers:
            df = all_rows.get(ticker)
            if df is None or df.empty:
                stats["no_data"] += 1
                continue
            added, _ = store.upsert_dedup(
                dividends_path(data_dir, ticker), df,
                dedup_keys=DEDUP_KEYS,
                sort_keys=["record_date", "divi_kind"],
            )
            if added > 0:
                stats["updated"] += 1
        # batch 응답에 있었지만 tickers에 없는 종목은 무시
        del wanted
        return stats

    # 종목별 호출 (legacy)
    for ticker in tickers:
        try:
            if source == "pykrx":
                df = fetch_dividends_pykrx(ticker, asof=asof, years=years)
            else:
                df = fetch_dividends_for_ticker(kis, ticker, asof=asof, years=years)
        except Exception as e:  # noqa: BLE001 — 한 종목 실패가 전체 중단되면 안 됨
            log.warning("[%s] sync_dividends failed: %s", ticker, e)
            stats["failed"].append((ticker, str(e)))
            continue
        if df.empty:
            stats["no_data"] += 1
            continue
        added, _ = store.upsert_dedup(
            dividends_path(data_dir, ticker), df,
            dedup_keys=DEDUP_KEYS,
            sort_keys=["record_date", "divi_kind"],
        )
        if added > 0:
            stats["updated"] += 1
    return stats
