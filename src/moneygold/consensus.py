"""컨센서스(애널리스트 예상 실적) — yfinance 기반.

Yahoo Finance가 한국 종목(.KS / .KQ 접미)에 대해 분석가 추정치를 제공.
대형주 (시총 1조+) 16~38명, 중형주 1~16명, 소형주 0~1명 — 대형주 위주로 유의미.

수집 항목:
  target_mean_price / target_low_price / target_high_price : 목표가
  recommendation_key      : strong_buy / buy / hold / sell / strong_sell / none
  n_analysts              : 분석가 수
  forward_pe              : 선행 PER (컨센서스 기반)
  earnings_growth         : 예상 EPS 성장률
  revenue_growth          : 예상 매출 성장률
  eps_estimate_0q / +1q / 0y / +1y : 분기/연도 EPS 추정 평균
  revenue_estimate_0q / 0y           : 분기/연도 매출 추정 평균
  recent_eps_surprise_pct            : 최근 분기 실적 서프라이즈 (%)

캐싱: store/consensus/{ticker}.json (JSON 단일 파일 per ticker, 모든 metric 평탄화).

데이터 부재 시 (404): consensus_path는 빈 dict로 저장 후 다음 sync 시 재시도 X.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ConsensusResult:
    available: bool = False
    n_analysts: int = 0
    target_mean: float | None = None
    target_low: float | None = None
    target_high: float | None = None
    recommendation: str | None = None
    forward_pe: float | None = None
    earnings_growth: float | None = None      # YoY 예상 (소수, 0.05 = 5%)
    revenue_growth: float | None = None
    eps_est_0q: float | None = None
    eps_est_next_y: float | None = None
    revenue_est_0q: float | None = None
    revenue_est_next_y: float | None = None
    last_surprise_pct: float | None = None    # 최근 분기 실적 서프라이즈
    # 컨센서스 상향/하향 조정 추세 (yfinance eps_trend + eps_revisions)
    # 'rev_*_30d_pct': 30일 전 추정 대비 *현재* 추정의 변화율 (%). 양수 = 상향 조정.
    rev_eps_0q_30d_pct: float | None = None
    rev_eps_0y_30d_pct: float | None = None
    rev_eps_next_y_30d_pct: float | None = None
    # 'ups/downs': 최근 N일간 추정치 *올린 분석가 수* / *내린 수*
    eps_ups_30d: int | None = None             # 이번 연도 기준 30일간 상향 수
    eps_downs_30d: int | None = None
    eps_ups_7d: int | None = None
    eps_downs_7d: int | None = None
    error: str | None = None


def _yf_symbol(ticker: str, market: str) -> str:
    """Yahoo Finance 한국 종목 접미: KOSPI → .KS, KOSDAQ → .KQ."""
    return f"{ticker}.{'KS' if market == 'KOSPI' else 'KQ'}"


def fetch_consensus(ticker: str, market: str) -> ConsensusResult:
    """yfinance에서 컨센서스 수집. 데이터 없으면 available=False."""
    try:
        import yfinance as yf
    except ImportError:
        return ConsensusResult(error="yfinance not installed")

    sym = _yf_symbol(ticker, market)
    try:
        t = yf.Ticker(sym)
        info = t.info or {}
    except Exception as e:
        return ConsensusResult(error=f"yf.Ticker.info failed: {e}")

    n_analysts = int(info.get("numberOfAnalystOpinions") or 0)
    if n_analysts == 0 and not info.get("targetMeanPrice"):
        # 커버리지 없음
        return ConsensusResult(available=False)

    eps_est_0q = None
    eps_est_next_y = None
    rev_est_0q = None
    rev_est_next_y = None
    try:
        ee = t.earnings_estimate
        if ee is not None and ee.shape[0] >= 1 and "avg" in ee.columns:
            if "0q" in ee.index:
                eps_est_0q = float(ee.loc["0q", "avg"])
            if "+1y" in ee.index:
                eps_est_next_y = float(ee.loc["+1y", "avg"])
    except Exception:
        pass
    try:
        re_ = t.revenue_estimate
        if re_ is not None and re_.shape[0] >= 1 and "avg" in re_.columns:
            if "0q" in re_.index:
                rev_est_0q = float(re_.loc["0q", "avg"])
            if "+1y" in re_.index:
                rev_est_next_y = float(re_.loc["+1y", "avg"])
    except Exception:
        pass

    last_surprise = None
    try:
        eh = t.earnings_history
        if eh is not None and eh.shape[0] >= 1 and "surprisePercent" in eh.columns:
            recent = eh.dropna(subset=["surprisePercent"]).tail(1)
            if not recent.empty:
                last_surprise = float(recent["surprisePercent"].iloc[0]) * 100.0
    except Exception:
        pass

    # 컨센서스 *상향 조정* 정보 — eps_trend (시간별 추정치) + eps_revisions (수)
    rev_0q = rev_0y = rev_next_y = None
    ups_30 = downs_30 = ups_7 = downs_7 = None
    try:
        et = t.eps_trend
        if et is not None and not et.empty and {"current", "30daysAgo"}.issubset(et.columns):
            def _pct(cur, old):
                try:
                    cur, old = float(cur), float(old)
                    if old == 0: return None
                    return (cur / old - 1.0) * 100.0
                except Exception:
                    return None
            if "0q" in et.index:
                rev_0q = _pct(et.loc["0q", "current"], et.loc["0q", "30daysAgo"])
            if "0y" in et.index:
                rev_0y = _pct(et.loc["0y", "current"], et.loc["0y", "30daysAgo"])
            if "+1y" in et.index:
                rev_next_y = _pct(et.loc["+1y", "current"], et.loc["+1y", "30daysAgo"])
    except Exception:
        pass
    try:
        er = t.eps_revisions
        if er is not None and not er.empty:
            # 이번 연도(0y) 기준
            if "0y" in er.index:
                row = er.loc["0y"]
                ups_30 = int(row.get("upLast30days") or 0)
                downs_30 = int(row.get("downLast30days") or 0)
                ups_7 = int(row.get("upLast7days") or 0)
                downs_7 = int(row.get("downLast7Days") or 0)
    except Exception:
        pass

    return ConsensusResult(
        available=True,
        n_analysts=n_analysts,
        target_mean=float(info["targetMeanPrice"]) if info.get("targetMeanPrice") else None,
        target_low=float(info["targetLowPrice"]) if info.get("targetLowPrice") else None,
        target_high=float(info["targetHighPrice"]) if info.get("targetHighPrice") else None,
        recommendation=str(info.get("recommendationKey")) if info.get("recommendationKey") else None,
        forward_pe=float(info["forwardPE"]) if info.get("forwardPE") else None,
        earnings_growth=float(info["earningsGrowth"]) if info.get("earningsGrowth") else None,
        revenue_growth=float(info["revenueGrowth"]) if info.get("revenueGrowth") else None,
        eps_est_0q=eps_est_0q, eps_est_next_y=eps_est_next_y,
        revenue_est_0q=rev_est_0q, revenue_est_next_y=rev_est_next_y,
        last_surprise_pct=last_surprise,
        rev_eps_0q_30d_pct=rev_0q,
        rev_eps_0y_30d_pct=rev_0y,
        rev_eps_next_y_30d_pct=rev_next_y,
        eps_ups_30d=ups_30, eps_downs_30d=downs_30,
        eps_ups_7d=ups_7, eps_downs_7d=downs_7,
    )


def consensus_path(data_dir: Path, ticker: str) -> Path:
    return data_dir / "consensus" / f"{ticker}.json"


_FIELDS = [
    "available", "n_analysts", "target_mean", "target_low", "target_high",
    "recommendation", "forward_pe", "earnings_growth", "revenue_growth",
    "eps_est_0q", "eps_est_next_y", "revenue_est_0q", "revenue_est_next_y",
    "last_surprise_pct",
    "rev_eps_0q_30d_pct", "rev_eps_0y_30d_pct", "rev_eps_next_y_30d_pct",
    "eps_ups_30d", "eps_downs_30d", "eps_ups_7d", "eps_downs_7d",
    "error",
]


def to_dict(r: ConsensusResult) -> dict[str, Any]:
    return {k: getattr(r, k) for k in _FIELDS}


def from_dict(d: dict[str, Any]) -> ConsensusResult:
    return ConsensusResult(**{k: d.get(k) for k in _FIELDS})


def fetch_and_cache(data_dir: Path, ticker: str, market: str, *, force: bool = False) -> ConsensusResult:
    """yfinance 호출 + JSON 캐싱."""
    path = consensus_path(data_dir, ticker)
    if not force and path.exists():
        try:
            return from_dict(json.loads(path.read_text()))
        except Exception:
            pass

    r = fetch_consensus(ticker, market)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(to_dict(r), ensure_ascii=False, indent=2))
    tmp.replace(path)
    return r


def sync_consensus_for(
    data_dir: Path,
    master_rows: list[tuple[str, str]],   # [(ticker, market), ...]
    *,
    force: bool = False,
    sleep_s: float = 0.3,
    progress: bool = True,
) -> dict:
    """전 종목 컨센서스 sync. yfinance는 rate limit 약하니 종목당 0.3초 sleep."""
    from tqdm import tqdm

    stats = {"total": len(master_rows), "available": 0, "no_data": 0,
             "cached": 0, "failed": []}
    it = tqdm(master_rows, desc="consensus", unit="tk") if progress else master_rows
    for tk, mkt in it:
        path = consensus_path(data_dir, tk)
        if path.exists() and not force:
            stats["cached"] += 1
            continue
        try:
            r = fetch_and_cache(data_dir, tk, mkt, force=force)
            if r.error:
                stats["failed"].append((tk, r.error))
            elif r.available:
                stats["available"] += 1
            else:
                stats["no_data"] += 1
        except Exception as e:
            stats["failed"].append((tk, str(e)))
        time.sleep(sleep_s)
    return stats
