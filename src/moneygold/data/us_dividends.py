"""US 배당 이력 + info(yield/payout/ROE) 동기화 — yfinance 기반.

"US 가속화 장기투자"(DGI) 탭의 배당 카테고리 + 펀더멘털 ROE + DRIP 입력.

배당 이력은 ``dividends.DIV_COLUMNS`` 스키마로 정규화해 저장 → KR DGI의 순수 feature
함수(`scoring.annual_dps_per_year`, `consecutive_increase_years`, `dps_cagr_5y_pct` 등)를
*그대로 재사용*한다.

저장 경로:
  store/us_dividends/{ticker}.parquet   # 배당 이력 (연 단위 DPS, fiscal_year 채워짐)
  store/us_info/{ticker}.json           # dividendYield / payoutRatio / returnOnEquity 등

yfinance Ticker.dividends는 지급일별 배당금(decades). 회계연도별로 합산해 'divi_kind=결산'
1행으로 표현 (US는 분기배당이 보편적이라 빈도는 점수에서 제외 — scoring_rules_us 참조).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from . import store
from .dividends import DEDUP_KEYS, DIV_COLUMNS

log = logging.getLogger(__name__)


def us_dividends_path(data_dir: Path, ticker: str) -> Path:
    return Path(data_dir) / "us_dividends" / f"{ticker}.parquet"


def us_info_path(data_dir: Path, ticker: str) -> Path:
    return Path(data_dir) / "us_info" / f"{ticker}.json"


def load_us_dividends(data_dir: Path, ticker: str) -> pd.DataFrame:
    df = store.read_parquet_safe(us_dividends_path(data_dir, ticker))
    return df if df is not None else pd.DataFrame(columns=DIV_COLUMNS)


def load_us_info(data_dir: Path, ticker: str) -> dict | None:
    path = us_info_path(data_dir, ticker)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ============================================================
# Normalize yfinance dividends → DIV_COLUMNS 스키마
# ============================================================

def _windowed_frequency(
    year: int, payments_by_year: dict[int, list[float]], window: int = 2,
) -> int:
    """``year`` 주변 ±window 연도의 지급 *건수* 최빈값 = 그 시기의 배당 빈도.

    **전역 최빈빈도 대신 연도별 윈도우**를 쓰는 이유: 빈도가 역사적으로 바뀐 종목
    (예: MCD 2005~07 연배당 n=1 → 2008~ 분기 n=4)에 전역 빈도(4)를 옛 연배당 연도에
    곱하면 rate가 4배 부풀려져 인공 삭감/급증이 생김 → 연속인상 streak 붕괴.
    윈도우 최빈값은 각 시기의 실제 빈도를 따라가 전환을 매끄럽게 처리.
    또 한 해에 타이밍상 3/5건 잡혀도 주변 연도 다수결(보통 4)로 보정.

    빈도는 {1,2,4,12} 중 가장 가까운 값으로 스냅 (불규칙 건수 방어). 데이터 없으면 그 해 건수.
    """
    from collections import Counter

    counts = []
    for y in range(year - window, year + window + 1):
        if y in payments_by_year and payments_by_year[y]:
            counts.append(len(payments_by_year[y]))
    if not counts:
        return max(len(payments_by_year.get(year, [])), 1)
    freq = Counter(counts)
    top = max(freq.values())
    mode = sorted((c for c, n in freq.items() if n == top), reverse=True)[0]
    # {1,2,4,12} 스냅
    canonical = min((1, 2, 4, 12), key=lambda c: abs(c - mode))
    return canonical


def normalize_yf_dividends(
    div_series: pd.Series, ticker: str, asof: str,
) -> pd.DataFrame:
    """yfinance Ticker.dividends (지급일 index, 금액 value) → 연 단위 DIV_COLUMNS.

    **연간 배당 'rate' = 그 해 지급액의 중앙값 × 최빈빈도**로 계산 (단순 합산 아님).
    이유: 캘린더 연도 합산은 분기배당 타이밍 시프트(12월↔1월, 한 해에 3회/5회)와
    이중지급 기록에 취약 — 예) KO 2001에 5건(0.18 이중 포함) 잡혀 합계가 부풀려져
    '연속 인상' 연수가 63년 → 23년으로 잘못 끊김. 중앙값×빈도는 이런 아티팩트를 흡수해
    실제 배당 'rate'의 연도별 추이를 안정적으로 복원 (CCC 방법론과 동일 철학).

    divi_kind='결산', stk_kind='보통' (DIV_COLUMNS 스키마 호환). fiscal_year = 지급 연도.
    asof 이후/진행 중 연도도 포함하나 scoring 함수가 'this_year 미만'만 써서 자동 무시.
    """
    if div_series is None or len(div_series) == 0:
        return pd.DataFrame(columns=DIV_COLUMNS)

    payments_by_year: dict[int, list[float]] = {}
    for ts, amt in div_series.items():
        try:
            year = int(pd.Timestamp(ts).year)
            val = float(amt)
        except (ValueError, TypeError):
            continue
        if val <= 0:
            continue
        payments_by_year.setdefault(year, []).append(val)

    if not payments_by_year:
        return pd.DataFrame(columns=DIV_COLUMNS)

    import statistics
    rows: list[dict[str, Any]] = []
    for year, amounts in sorted(payments_by_year.items()):
        # 연간 rate 산정:
        #  - 지급 건수가 그 시기 정상 빈도와 같으면 → sum (연중 인상도 정확히 포착).
        #  - 건수가 이상(타이밍상 3/5건, 이중지급)하면 → median×빈도 (아티팩트 흡수).
        # 이 분기로 KO 2001(5건→median×4) 와 MCD 2008(연중 인상, 4건→sum) 둘 다 정확.
        freq = _windowed_frequency(year, payments_by_year)
        if len(amounts) == freq:
            rate = sum(amounts)
        else:
            rate = statistics.median(amounts) * freq
        rows.append({
            "ticker": ticker,
            "record_date": f"{year}1231",
            "divi_kind": "결산",
            "per_sto_divi_amt": rate,
            "divi_rate_pct": float("nan"),
            "stk_divi_rate_pct": 0.0,
            "divi_pay_dt": "",
            "stk_kind": "보통",
            "fetched_at": asof,
            "fiscal_year": year,
        })
    df = pd.DataFrame(rows, columns=DIV_COLUMNS)
    df["fiscal_year"] = df["fiscal_year"].astype("Int64")
    return df


# ============================================================
# Fetch (yfinance)
# ============================================================

def fetch_us_dividends_and_info(
    ticker: str, asof: str,
) -> tuple[pd.DataFrame, dict]:
    """단일 US 종목 배당 이력 + info를 yfinance에서.

    Returns (dividends_df[DIV_COLUMNS], info_dict).
    info_dict keys: dividend_yield_pct, payout_ratio_pct, roe_pct,
                    five_year_avg_div_yield, shares_outstanding, fetched_at.
    실패 시 (빈 DataFrame, {}) — 호출자가 skip.
    """
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed")
        return pd.DataFrame(columns=DIV_COLUMNS), {}

    try:
        t = yf.Ticker(ticker)
        div_series = t.dividends
    except Exception as e:  # noqa: BLE001
        log.debug("[%s] yf dividends failed: %s", ticker, e)
        div_series = None

    div_df = normalize_yf_dividends(div_series, ticker, asof)

    info_out: dict[str, Any] = {}
    try:
        info = t.info or {}
        # yfinance dividendYield는 현행 버전에서 *이미 퍼센트*로 옴 (WMT 0.83=0.83%,
        # KO 2.64=2.64%). 과거 '< 1.0이면 소수로 보고 ×100' 휴리스틱은 1% 미만 배당주를
        # 83% 등으로 잘못 부풀렸음 → 제거. 그대로 사용. (scoring_us는 어차피 배당이력+현재가로
        # 직접 계산한 yield를 우선하고 이 값은 fallback.)
        dy = info.get("dividendYield")
        dy = float(dy) if dy is not None else None
        # sanity-clip: yfinance가 종목별로 단위 비일관(KO=2.64 정상 vs WMT=0.83을 일부
        # 버전이 80+로 반환) → 25% 초과는 거의 확실히 garbage. 저장 자체를 막아 info
        # 직접 소비자/fallback 오염 방지. (scoring_us는 계산 yield 우선이라 무해하지만 위생.)
        if dy is not None and dy > 25.0:
            dy = None
        info_out = {
            "dividend_yield_pct": dy,
            "payout_ratio_pct": (float(info["payoutRatio"]) * 100.0
                                  if info.get("payoutRatio") is not None else None),
            "roe_pct": (float(info["returnOnEquity"]) * 100.0
                         if info.get("returnOnEquity") is not None else None),
            "five_year_avg_div_yield": info.get("fiveYearAvgDividendYield"),
            "shares_outstanding": info.get("sharesOutstanding"),
            "fetched_at": asof,
        }
    except Exception as e:  # noqa: BLE001
        log.debug("[%s] yf info failed: %s", ticker, e)

    return div_df, info_out


# ============================================================
# Sync orchestrator
# ============================================================

def sync_us_dividends(
    data_dir: Path,
    tickers: list[str],
    asof: str,
    *,
    log_every: int = 50,
) -> dict[str, Any]:
    """여러 US 종목 배당 이력 + info sync.

    배당: upsert (keep='last'). info: atomic JSON 덮어쓰기.

    Returns {'total','updated','no_data','failed':[(tk,err)]}.
    """
    stats: dict[str, Any] = {"total": len(tickers), "updated": 0, "no_data": 0, "failed": []}
    for i, ticker in enumerate(tickers, 1):
        if i % log_every == 0:
            log.info("[us_dividends] progress %d / %d (%s)", i, len(tickers), ticker)
        try:
            div_df, info = fetch_us_dividends_and_info(ticker, asof)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] sync_us_dividends failed: %s", ticker, e)
            stats["failed"].append((ticker, str(e)))
            continue

        wrote = False
        if not div_df.empty:
            store.upsert_dedup(
                us_dividends_path(data_dir, ticker), div_df,
                dedup_keys=DEDUP_KEYS, sort_keys=["record_date", "divi_kind"],
            )
            wrote = True
        if info:
            path = us_info_path(data_dir, ticker)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False)
            tmp.replace(path)
            wrote = True

        if wrote:
            stats["updated"] += 1
        else:
            stats["no_data"] += 1
    return stats
