"""진입 시그널 합성 — 신고가 돌파 + 거래대금 스파이크 + 필터 게이트.

이 모듈은 *진입 후보*만 만든다. 청산은 position.py 의 상태머신.

핵심 함수: ``find_entry_candidates(asof, bars_by_ticker, master, cfg)``
  → list[BreakoutEntry], 점수 desc 정렬.

룩어헤드 방지:
  - 모든 계산은 asof 이하 봉만 사용.
  - 신고가/스파이크는 *오늘 직전*까지의 분포 + 오늘 값 비교.
  - 진입 가정은 *다음 영업일 시가* (signals 안에선 가정만, 가격은 사용자 결정).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import filters as flt
from . import indicators as ind
from .config import MomentumConfig


@dataclass(frozen=True)
class BreakoutEntry:
    """단일 종목의 진입 후보. find_entry_candidates 반환 원소.

    Fields
    ------
    ticker, name, market : 종목 식별.
    asof : 시그널 발생일 (YYYYMMDD). 진입은 보통 *다음 영업일 시가* 가정.
    close : 시그널 일의 종가.
    new_high_ref : 직전 ``new_high_lookback`` 일 최고 종가 (오늘 제외). close > 이 값.
    new_high_amplitude : (close - new_high_ref) / new_high_ref. 신고가 갱신폭.
    volume_ratio : 당일 거래대금 / 20일 평균. >= cfg.volume_spike_ratio.
    value_today : 당일 거래대금 (KRW).
    value_rank : 통합 거래대금 상위 N 중 자기 순위 (1-based). 상위 진입 안 됐으면 -1.
    suggested_stop : entry × (1 - stop_loss_pct). entry는 close 대용 (다음날 시가 미정).
    score : 종목간 랭킹용. volume_ratio × (1 + new_high_amplitude). 클수록 강세.
    """
    ticker: str
    name: str
    market: str
    asof: str
    close: float
    new_high_ref: float
    new_high_amplitude: float
    volume_ratio: float
    value_today: float
    value_rank: int
    suggested_stop: float
    score: float


def _evaluate_ticker(
    ticker: str,
    name: str,
    market: str,
    bars: pd.DataFrame,
    asof: str,
    cfg: MomentumConfig,
) -> BreakoutEntry | None:
    """단일 종목의 진입 조건 검사. 통과 시 BreakoutEntry, 아니면 None.

    *유동성 (top-N value) 게이트는 호출자가 외부에서 처리* — 종목별 데이터만 보고는
    당일 거래대금 순위를 못 정함. ``value_rank`` 는 호출자가 사후 채워야 함.
    """
    if bars is None or bars.empty:
        return None
    b = bars[bars["date"] <= asof].copy()
    if not flt.min_listed_days_ok(b, asof, cfg.min_listed_days):
        return None
    needed = cfg.new_high_lookback + cfg.fresh_window + 2
    if len(b) < needed:
        return None
    if "close" not in b.columns or "value" not in b.columns:
        return None

    b = b.sort_values("date").reset_index(drop=True)
    close = b["close"].astype(float)
    value = b["value"].astype(float)

    today_close = float(close.iloc[-1])
    nh_ref = ind.rolling_new_high(close, cfg.new_high_lookback).iloc[-1]
    if not np.isfinite(nh_ref) or nh_ref <= 0:
        return None

    # 1) 60일 신고가 돌파
    if today_close <= nh_ref:
        return None

    # 2) Fresh: 직전 fresh_window 봉 어디서도 같은 의미의 돌파 없음
    if not ind.is_fresh_breakout(close, cfg.new_high_lookback, cfg.fresh_window):
        return None

    # 3) 거래대금 스파이크
    passed_spike, vol_ratio = ind.volume_spike(value, cfg.volume_avg_window, cfg.volume_spike_ratio)
    if not passed_spike:
        return None

    amplitude = (today_close - float(nh_ref)) / float(nh_ref)
    score = vol_ratio * (1.0 + amplitude)
    suggested_stop = today_close * (1.0 - cfg.stop_loss_pct)

    return BreakoutEntry(
        ticker=ticker, name=name, market=market, asof=asof,
        close=today_close,
        new_high_ref=float(nh_ref),
        new_high_amplitude=float(amplitude),
        volume_ratio=float(vol_ratio),
        value_today=float(value.iloc[-1]),
        value_rank=-1,                     # 호출자가 사후 채움
        suggested_stop=float(suggested_stop),
        score=float(score),
    )


# 시장별 ranking 그룹 정의 — 각 그룹 내에서 독립 ranking.
# KR: KOSPI+KOSDAQ 통합 (KRW 단위 동일)
# US: 별도 (USD 단위)
# 향후 다른 시장 추가 시 여기에 추가
_MARKET_GROUPS: tuple[tuple[str, ...], ...] = (
    ("KOSPI", "KOSDAQ"),
    ("US",),
)


def find_entry_candidates(
    asof: str,
    bars_by_ticker: dict[str, pd.DataFrame],
    master: pd.DataFrame,
    cfg: MomentumConfig | None = None,
    *,
    apply_filter_master: bool = True,
    markets: tuple[str, ...] | None = None,
) -> list[BreakoutEntry]:
    """``asof`` 시점 진입 후보 리스트. 점수 desc 정렬.

    Parameters
    ----------
    asof : YYYYMMDD. 이 날짜 *이하* 의 종가까지만 본다 (룩어헤드 방지).
    bars_by_ticker : ticker → bars DataFrame (date 컬럼 YYYYMMDD str, value 컬럼 필수).
    master : 종목 마스터. columns ⊇ {'ticker','name','market'}.
        'mcap' 있으면 top_n_marketcap 적용. 시장별 독립 ranking (KR 통합 / US 별도).
        거래대금/시총 rank 컷은 그룹별로 다름 — US는 메가캡 쏠림 때문에 완화된 값
        (top_n_value_us / top_n_marketcap_us). cfg.top_n_for_group() 참조.
    cfg : MomentumConfig. None이면 load_momentum_config() (env 기반).
    apply_filter_master : True면 우선주/스팩/리츠/ETP 제외 (KR universe 필터, US는 영향 없음).
    markets : 처리할 시장 코드들. None이면 전체. 예: ('KOSPI','KOSDAQ') 또는 ('US',).

    Returns
    -------
    list[BreakoutEntry]  score desc 정렬. 각 시장 그룹 내에서 독립적으로 ranking 후 결합.
        value_rank 는 그 시장 그룹 내 거래대금 순위 (1-based, 그룹 전체 종목 기준).
    """
    if cfg is None:
        from .config import load_momentum_config
        cfg = load_momentum_config()

    if master is None or master.empty:
        return []

    # 시장별 독립 처리 후 결합
    out: list[BreakoutEntry] = []
    for group in _MARKET_GROUPS:
        if markets is not None and not any(g in markets for g in group):
            continue
        sub = _find_in_market_group(asof, bars_by_ticker, master, cfg, group, apply_filter_master)
        out.extend(sub)

    out.sort(key=lambda e: -e.score)
    return out


def _find_in_market_group(
    asof: str,
    bars_by_ticker: dict[str, pd.DataFrame],
    master: pd.DataFrame,
    cfg: MomentumConfig,
    group: tuple[str, ...],
    apply_filter_master: bool,
) -> list[BreakoutEntry]:
    """단일 시장 그룹 (KR or US) 내 독립 ranking + 시그널 추출."""
    m = master.copy()

    # 시장 그룹 필터
    if "market" in m.columns:
        m = m[m["market"].isin(group)]
    if m.empty:
        return []

    # 그룹별 거래대금/시총 rank 컷 — US는 메가캡 쏠림 때문에 완화된 값 (config 참조)
    top_n_value, top_n_marketcap = cfg.top_n_for_group(group)

    # 시가총액 게이트 — *그룹 내* 상위 N
    if top_n_marketcap is not None and "mcap" in m.columns:
        m = m.sort_values("mcap", ascending=False).head(top_n_marketcap)

    # KR universe 필터 (우선주/스팩/리츠/ETP). US 그룹엔 효과 적지만 안전하게 호출 가능.
    if apply_filter_master and "KOSPI" in group:
        m = flt.filter_master(m)

    if m.empty:
        return []

    # 종목별 당일 거래대금 추출 — 그룹 내 통화 단위 동일 가정
    today_value: dict[str, float] = {}
    for ticker in m["ticker"].astype(str):
        bars = bars_by_ticker.get(ticker)
        if bars is None or bars.empty or "date" not in bars.columns or "value" not in bars.columns:
            continue
        clipped = bars[bars["date"] <= asof]
        if clipped.empty:
            continue
        last_row = clipped.sort_values("date").iloc[-1]
        v = float(last_row["value"])
        if np.isfinite(v) and v > 0:
            today_value[ticker] = v

    if not today_value:
        return []

    top_value_set = flt.top_n_by_value(today_value, top_n_value)
    value_rank_map = {
        tk: rank for rank, tk in enumerate(
            sorted(today_value.keys(), key=lambda k: -today_value[k]), start=1
        )
    }

    candidate_tickers = m[m["ticker"].astype(str).isin(top_value_set)]
    name_map = dict(zip(candidate_tickers["ticker"].astype(str), candidate_tickers["name"], strict=False))
    market_map = dict(zip(candidate_tickers["ticker"].astype(str), candidate_tickers["market"], strict=False))

    out: list[BreakoutEntry] = []
    for ticker in candidate_tickers["ticker"].astype(str):
        if flt.is_flagged(ticker, asof):
            continue
        bars = bars_by_ticker.get(ticker)
        be = _evaluate_ticker(
            ticker, name_map.get(ticker, ""), market_map.get(ticker, ""),
            bars, asof, cfg,
        )
        if be is None:
            continue
        rank = value_rank_map.get(ticker, -1)
        out.append(BreakoutEntry(
            ticker=be.ticker, name=be.name, market=be.market, asof=be.asof,
            close=be.close, new_high_ref=be.new_high_ref,
            new_high_amplitude=be.new_high_amplitude,
            volume_ratio=be.volume_ratio, value_today=be.value_today,
            value_rank=rank, suggested_stop=be.suggested_stop, score=be.score,
        ))
    return out
