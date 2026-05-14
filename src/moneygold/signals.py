"""시그널 합성 — Stage 2 ∧ Minervini Template ∧ Darvas BREAKOUT.

ARCHITECTURE.md §7 참조.

- 유동성·시가총액·플래그 게이트
- Stage 분류기 (PR2 결과)
- Minervini Template (8 조건 + RS rank)
- Darvas 박스 (BREAKOUT_TODAY / BREAKOUT_GAP)
- 동시 BUY 후보 우선순위: (RS rank desc, days_in_box desc, mcap desc)
- 보유 종목은 HOLD/SELL 판정 (SELL 사유: STOP_HIT / 30W_MA_BREAK / STAGE_3 / STAGE_4)
- 갭다운 스톱 라벨 (URGENT_GAP_DOWN)
- 피라미딩 없음 (v1) — 이미 보유 중인 종목은 BUY 후보에서 자동 제외

수급(외인+기관 누적 순매수)은 데이터 파일이 있을 때만 강화 신호로 사용.
없으면 자동 우회 — 시그널 생성 자체는 정상 동작.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import darvas, indicators as ind, stage as stg, template as tmpl
from .config import AppConfig


# ============================================================
# Input contracts
# ============================================================

@dataclass
class PositionMeta:
    """portfolio.json의 메타데이터 캐시 (entry/stop/box)."""
    ticker: str
    entry_date: str
    entry_price: float
    current_stop: float
    current_box_top: float | None = None
    current_box_bottom: float | None = None
    highest_close_since_entry: float | None = None


@dataclass
class TickerData:
    """한 종목의 분석 입력 묶음 (signals 함수 외부에서 채워서 전달)."""
    ticker: str
    name: str
    market: str           # KOSPI | KOSDAQ
    bars: pd.DataFrame    # date 오름차순, columns: open/high/low/close/volume/value
    mcap: float           # 시가총액 (KRW), 보통 close × shares_outstanding
    flagged: bool = False # 관리/경고/거래정지


# ============================================================
# Output schema
# ============================================================

@dataclass
class BuySignal:
    ticker: str
    name: str
    market: str
    entry_guide: float       # 박스 천장 × (1+buffer)
    stop: float              # 박스 바닥
    risk_per_share: float
    suggested_shares: int
    suggested_size_krw: int
    box_top: float
    box_bottom: float
    days_in_box: int
    volume_ratio: float
    is_gap_breakout: bool
    rs_rank: float
    stage_2_since: str | None
    template_pass: list[bool]
    asof: str


@dataclass
class SellSignal:
    ticker: str
    name: str
    market: str
    reason: str              # STOP_HIT / 30W_MA_BREAK / STAGE_3 / STAGE_4
    exit_guide: float        # 다음 영업일 시장가 (참고용 = 오늘 종가)
    label: str | None = None # URGENT_GAP_DOWN 등
    asof: str = ""


@dataclass
class HoldSignal:
    ticker: str
    name: str
    market: str
    current_close: float
    current_stop: float
    new_stop: float          # 트레일링 갱신된 스톱 (변경 없으면 current_stop)
    trail_updated: bool
    days_held: int
    asof: str = ""


@dataclass
class WatchlistEntry:
    """Stage 2 + Template 통과 종목 (Darvas 무관). 사용자 매수 검토용 후보 풀."""
    ticker: str
    name: str
    market: str
    close: float
    rs_rank: float
    box_state: str           # SEARCHING / FORMING / CONFIRMED / BREAKOUT_TODAY / BREAKOUT_GAP
    box_top: float | None
    box_bottom: float | None
    days_in_box: int
    suggested_stop: float    # box_bottom 있으면 그것, 없으면 close × 0.93
    asof: str = ""


@dataclass
class DailySignals:
    asof: str
    new_buys: list[BuySignal]          # Darvas 돌파 종목 (즉시 검토 대상)
    holds: list[HoldSignal]
    sells: list[SellSignal]
    watchlist: list["WatchlistEntry"] = field(default_factory=list)   # 전체 후보 풀


# ============================================================
# Core generator
# ============================================================

def generate_signals(
    asof: str,
    tickers: list[TickerData],
    portfolio: dict[str, PositionMeta],
    rs_rank_map: dict[str, float],
    idx_close_by_market: dict[str, pd.Series],
    cfg: AppConfig,
) -> DailySignals:
    """일일 시그널 생성.

    Parameters
    ----------
    asof : YYYYMMDD 기준일
    tickers : 후보 종목 입력 (마스터 + 시가총액 + 플래그)
    portfolio : 현재 보유 (메타데이터)
    rs_rank_map : ticker -> rs_rank 백분위 (사전 계산)
    idx_close_by_market : 'KOSPI'/'KOSDAQ' -> 지수 종가 시계열 (date 인덱스, asof까지)
    cfg : AppConfig (전략 파라미터 / 유동성 게이트 / 사이즈)
    """
    s = cfg.strategy
    u = cfg.universe
    sizing = cfg.sizing

    new_buys: list[BuySignal] = []
    holds: list[HoldSignal] = []
    sells: list[SellSignal] = []
    watchlist: list[WatchlistEntry] = []

    box_params = darvas.BoxParams(
        box_high_lookback=s.box_high_lookback,
        box_high_confirm=s.box_high_confirm,
        box_height_max_pct=s.box_height_max_pct,
        box_valid_min_days=s.box_valid_min_days,
        box_stale_days=s.box_stale_days,
        breakout_buffer=s.breakout_buffer,
        breakout_volume_mult=s.breakout_volume_mult,
    )

    for td in tickers:
        bars = td.bars
        if bars is None or bars.empty or len(bars) < 252:
            continue

        # asof 이전으로 클립
        bars = bars[bars["date"] <= asof].copy().sort_values("date").reset_index(drop=True)
        if len(bars) < 252:
            continue

        # ---------- 게이트 ----------
        if td.flagged:
            continue
        if td.mcap < u.mcap_min_krw:
            continue
        # 유동성: 최근 20봉 평균 거래대금
        if "value" in bars.columns and len(bars) >= 20:
            avg_value_20 = float(bars["value"].tail(20).mean())
            if avg_value_20 < u.liquidity_min_krw:
                continue
        else:
            continue

        # ---------- 보유 종목: HOLD/SELL ----------
        if td.ticker in portfolio:
            pos = portfolio[td.ticker]
            sell, hold = _evaluate_position(bars, pos, asof, td)
            if sell is not None:
                sells.append(sell)
            elif hold is not None:
                holds.append(hold)
            continue

        # ---------- 신규 후보: BUY ----------
        # Stage 2 확인 (RS는 Stage 판정에 안 들어감)
        stage_params = _stage_params_from_cfg(s)
        stage_val, stage_since = _compute_stage(bars, asof, stage_params)
        if stage_val != stg.STAGE_ADVANCING:
            continue

        # Minervini Template (조건 6/7는 고가/저가 기준)
        rs_rank_value = float(rs_rank_map.get(td.ticker, float("nan")))
        t = tmpl.check_template(
            bars["close"].astype(float),
            rs_rank_value,
            sma200_slope_lookback=s.sma200_slope_lookback,
            rs_rank_min=float(s.rs_rank_min),
            high_series=bars["high"].astype(float) if "high" in bars.columns else None,
            low_series=bars["low"].astype(float) if "low" in bars.columns else None,
        )
        if not t.passed:
            continue

        # 여기까지 통과 = Stage 2 + Template 8/8. Darvas 무관하게 워치리스트 후보.
        box_for_watch = darvas.current_box(bars, box_params)
        last_close = float(bars["close"].iloc[-1])
        suggested_stop = float(box_for_watch.bottom) if box_for_watch.bottom is not None else last_close * 0.93
        watchlist.append(WatchlistEntry(
            ticker=td.ticker, name=td.name, market=td.market,
            close=last_close, rs_rank=rs_rank_value,
            box_state=str(box_for_watch.state),
            box_top=float(box_for_watch.top) if box_for_watch.top is not None else None,
            box_bottom=float(box_for_watch.bottom) if box_for_watch.bottom is not None else None,
            days_in_box=int(box_for_watch.days_in_box),
            suggested_stop=suggested_stop,
            asof=asof,
        ))

        # Darvas 박스 — SKIP_DARVAS=true면 우회 (Stage 2 + Template만으로 매수)
        if getattr(s, "skip_darvas", False):
            last_close = float(bars["close"].iloc[-1])
            entry_guide = last_close
            stop_pct = float(getattr(s, "no_darvas_stop_pct", 7.0))
            stop = entry_guide * (1.0 - stop_pct / 100.0)
            box_top = entry_guide
            box_bottom = stop
            days_in_box = 0
            vol_ratio = float("nan")
            is_gap = False
        else:
            box = darvas.current_box(bars, box_params)
            if not box.is_breakout or box.top is None or box.bottom is None:
                continue
            entry_guide = box.top * (1.0 + s.breakout_buffer)
            stop = float(box.bottom)
            box_top = float(box.top)
            box_bottom = stop
            days_in_box = int(box.days_in_box)
            vol_ratio = float(box.volume_ratio) if box.volume_ratio is not None else float("nan")
            is_gap = box.is_gap

        if entry_guide <= stop:
            continue
        risk_per_share = entry_guide - stop
        size = _suggest_size(entry_guide, risk_per_share, sizing)

        new_buys.append(BuySignal(
            ticker=td.ticker, name=td.name, market=td.market,
            entry_guide=_tick_round(entry_guide, td.market),
            stop=_tick_round(stop, td.market),
            risk_per_share=risk_per_share,
            suggested_shares=size[0],
            suggested_size_krw=size[1],
            box_top=box_top, box_bottom=box_bottom,
            days_in_box=days_in_box,
            volume_ratio=vol_ratio,
            is_gap_breakout=is_gap,
            rs_rank=rs_rank_value,
            stage_2_since=stage_since,
            template_pass=list(t.checks),
            asof=asof,
        ))

    # ---------- 우선순위 정렬 + 슬롯 제한 ----------
    new_buys.sort(key=lambda b: (-b.rs_rank, -b.days_in_box, -_lookup_mcap(b.ticker, tickers)))
    free_slots = max(0, sizing.max_positions - len(portfolio))
    new_buys = new_buys[:free_slots]

    # 워치리스트는 RS rank desc 정렬, 슬롯 무관 (사용자가 보고 선택)
    watchlist.sort(key=lambda w: (-w.rs_rank if not pd.isna(w.rs_rank) else 0, -w.days_in_box))

    return DailySignals(asof=asof, new_buys=new_buys, holds=holds, sells=sells, watchlist=watchlist)


# ============================================================
# Helpers
# ============================================================

def _compute_stage(
    bars: pd.DataFrame, asof: str,
    stage_params: stg.StageParams | None = None,
) -> tuple[int, str | None]:
    """오늘의 Stage + Stage 2 진입일(있으면).

    Weinstein 분류기는 RS 없이 가격+MA만 사용 (TV 원전 일치).
    RS는 Minervini Template 조건 8로 별도.
    """
    close = bars["close"].astype(float)
    if close.empty:
        return stg.STAGE_UNKNOWN, None

    series = stg.classify_stage_series(close, stage_params)
    stage_val = int(series.iloc[-1])

    stage_since = None
    if stage_val == stg.STAGE_ADVANCING:
        dates = bars["date"].astype(str).values
        idx = len(series) - 1
        while idx > 0 and series.iloc[idx - 1] == stg.STAGE_ADVANCING:
            idx -= 1
        stage_since = str(dates[idx])

    return stage_val, stage_since


def _stage_params_from_cfg(s) -> stg.StageParams:
    return stg.StageParams(
        ma_length=getattr(s, "stage_ma_length", 150),
        slope_lookback=getattr(s, "stage_slope_lookback", 20),
        slope_threshold_pct=getattr(s, "stage_slope_threshold_pct", 0.001),
        band_pct=getattr(s, "stage_band_pct", 0.03),
        ma_type=getattr(s, "stage_ma_type", "SMA"),
    )


def _evaluate_position(
    bars: pd.DataFrame,
    pos: PositionMeta,
    asof: str,
    td: TickerData,
) -> tuple[SellSignal | None, HoldSignal | None]:
    close = bars["close"].astype(float)
    last_close = float(close.iloc[-1])
    last_open = float(bars["open"].iloc[-1]) if "open" in bars.columns else last_close

    sma_30w = ind.sma(close, 150)
    last_sma = float(sma_30w.iloc[-1]) if pd.notna(sma_30w.iloc[-1]) else float("nan")

    # 1) STOP_HIT
    if last_close <= pos.current_stop:
        label = None
        if last_open < pos.current_stop * 0.97:
            label = "URGENT_GAP_DOWN"
        return SellSignal(
            ticker=td.ticker, name=td.name, market=td.market,
            reason="STOP_HIT", exit_guide=last_close, label=label, asof=asof,
        ), None

    # 2) 30주 MA 이탈
    if np.isfinite(last_sma) and last_close < last_sma:
        return SellSignal(
            ticker=td.ticker, name=td.name, market=td.market,
            reason="30W_MA_BREAK", exit_guide=last_close, asof=asof,
        ), None

    # 3) Stage 3 / 4 전환
    stage_val, _ = _compute_stage(bars, asof)
    if stage_val in (stg.STAGE_TOPPING, stg.STAGE_DECLINING):
        return SellSignal(
            ticker=td.ticker, name=td.name, market=td.market,
            reason=f"STAGE_{stage_val}", exit_guide=last_close, asof=asof,
        ), None

    # HOLD — 트레일링 스톱 갱신 시도
    new_stop = pos.current_stop
    trail_updated = False
    # 새 박스 확정 시 그 바닥으로 갱신 (단 스톱은 후퇴 X)
    box = darvas.current_box(bars)
    if box.state == darvas.CONFIRMED and box.bottom is not None and box.bottom > pos.current_stop:
        new_stop = float(box.bottom)
        trail_updated = True

    days_held = 0
    try:
        days_held = (pd.to_datetime(asof) - pd.to_datetime(pos.entry_date)).days
    except Exception:
        pass

    return None, HoldSignal(
        ticker=td.ticker, name=td.name, market=td.market,
        current_close=last_close, current_stop=pos.current_stop,
        new_stop=new_stop, trail_updated=trail_updated, days_held=days_held, asof=asof,
    )


def _suggest_size(entry: float, risk_per_share: float, sizing) -> tuple[int, int]:
    """(shares, notional_krw). DEFAULT_EQUITY_KRW 기반 추천."""
    if risk_per_share <= 0 or entry <= 0:
        return 0, 0
    equity = float(sizing.default_equity_krw)
    max_loss = equity * sizing.max_risk_per_trade_pct / 100.0
    shares_by_risk = int(max_loss // risk_per_share)
    cap_notional = equity * sizing.max_position_weight_pct / 100.0
    shares_by_weight = int(cap_notional // entry)
    shares = max(0, min(shares_by_risk, shares_by_weight))
    return shares, int(shares * entry)


def _tick_round(price: float, market: str) -> float:
    """KRX 호가 단위로 가격 라운드 (KOSPI/KOSDAQ 공통 룰).

    가격대별 tick:
       <  2,000     1원
       <  5,000     5원
       < 20,000    10원
       < 50,000    50원
       <200,000   100원
       <500,000   500원
       >=500,000 1000원
    """
    if pd.isna(price) or price <= 0:
        return price
    p = float(price)
    if p < 2000: tick = 1
    elif p < 5000: tick = 5
    elif p < 20000: tick = 10
    elif p < 50000: tick = 50
    elif p < 200000: tick = 100
    elif p < 500000: tick = 500
    else: tick = 1000
    return float(int(p // tick) * tick)


def _lookup_mcap(ticker: str, tickers: list[TickerData]) -> float:
    for t in tickers:
        if t.ticker == ticker:
            return float(t.mcap)
    return 0.0


# ============================================================
# Serialization
# ============================================================

def to_dict(sigs: DailySignals) -> dict[str, Any]:
    return {
        "asof": sigs.asof,
        "new_buys": [b.__dict__ for b in sigs.new_buys],
        "holds": [h.__dict__ for h in sigs.holds],
        "sells": [s.__dict__ for s in sigs.sells],
        "watchlist": [w.__dict__ for w in sigs.watchlist],
    }
