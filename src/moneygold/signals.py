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

from . import consensus as cons
from . import darvas, fundamentals as fund, indicators as ind, stage as stg, template as tmpl
from .config import AppConfig
from .data import store


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
    """Stage + Template 게이트 통과 종목 (Darvas 무관). 사용자 매수 검토용 후보 풀.

    게이트는 cfg.strategy.allowed_stages 와 cfg.strategy.required_template_conditions
    (또는 generate_signals 호출 시 override) 으로 결정된다 — 더 이상 항상 Stage 2 & 8/8 아님.
    """
    ticker: str
    name: str
    market: str
    close: float
    rs_rank: float           # IBD 횡단면 백분위 0~100 (시장별)
    rs_momentum: float       # IBD 4Q 가중 수익률 절대값 (참고용)
    box_state: str           # SEARCHING / FORMING / CONFIRMED / BREAKOUT_TODAY / BREAKOUT_GAP
    box_top: float | None
    box_bottom: float | None
    days_in_box: int
    suggested_stop: float    # box_bottom 있으면 그것, 없으면 close × 0.93
    stage: int = 0           # 현재 Weinstein Stage (1~4). 0 = UNKNOWN.
    template_checks: list[bool] = field(default_factory=lambda: [False] * 8)  # 8조건 결과
    # 펀더멘털 (캐시된 KIS finance 분기). 데이터 없으면 NaN.
    revenue_yoy: float = float("nan")        # 매출 YoY (%)
    op_income_yoy: float = float("nan")      # 영업이익 YoY (%)
    op_margin: float = float("nan")          # 영업이익률 (%)
    net_margin: float = float("nan")         # 순이익률 (%) = net_income / revenue × 100
    growth_quarters: int = 0                  # 연속 매출 성장 분기 수
    op_growth_quarters: int = 0               # 연속 영업이익 성장 분기 수
    accelerating: bool = False                # YoY 가속 여부
    # 거래량 수급 — 20일 평균 거래량 / 60일 평균. >1.1: 누적 매수, <0.9: 이탈.
    # bars[volume]만 가지고 계산하므로 KR/US 모두 적용. 향후 pykrx 외인/기관 수급 데이터로 보강 예정.
    vol_acc_ratio: float = float("nan")
    # 컨센서스 (캐시된 yfinance). 데이터 없으면 NaN/None.
    cons_n_analysts: int = 0
    cons_target_mean: float = float("nan")
    cons_target_upside_pct: float = float("nan")   # (target_mean - close) / close × 100
    cons_recommendation: str | None = None
    cons_forward_pe: float = float("nan")
    cons_earnings_growth: float = float("nan")     # 예상 EPS 성장 (%)
    cons_revenue_growth: float = float("nan")      # 예상 매출 성장 (%)
    cons_last_surprise_pct: float = float("nan")
    # 컨센서스 *상향 조정* 추세
    cons_rev_eps_0y_30d_pct: float = float("nan")     # 이번 연도 EPS 추정 30일 전 대비 변화 (%)
    cons_rev_eps_0q_30d_pct: float = float("nan")     # 이번 분기 EPS 추정 30일 전 대비
    cons_eps_ups_30d: int = 0                          # 30일간 상향 분석가 수 (이번 연도)
    cons_eps_downs_30d: int = 0
    cons_eps_net_revisions_30d: int = 0                # ups - downs
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
    rs_momentum_map: dict[str, float] | None = None,
    fundamentals_map: dict[str, fund.FundamentalsResult] | None = None,
    consensus_map: dict[str, cons.ConsensusResult] | None = None,
    allowed_stages: tuple[int, ...] | None = None,
    required_template_conditions: tuple[int, ...] | None = None,
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
    allowed_stages : 허용 Stage 집합. None이면 cfg.strategy.allowed_stages 사용.
        빈 튜플 () = Stage 게이트 비활성 (모든 Stage 허용).
    required_template_conditions : 반드시 통과해야 할 Minervini 조건 번호 (1~8).
        None이면 cfg.strategy.required_template_conditions 사용.
        빈 튜플 () = Template 게이트 비활성.
    """
    s = cfg.strategy
    u = cfg.universe
    sizing = cfg.sizing

    allowed_stages_set = set(
        allowed_stages if allowed_stages is not None
        else getattr(s, "allowed_stages", (stg.STAGE_ADVANCING,))
    )
    required_conditions = tuple(
        required_template_conditions if required_template_conditions is not None
        else getattr(s, "required_template_conditions", tuple(range(1, tmpl.N_CONDITIONS + 1)))
    )

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
        # 시가총액 — KRW(국내)와 USD(미국)는 단위가 다르므로 시장별 threshold 사용
        if td.market == "US":
            if td.mcap < u.us_mcap_min_usd:
                continue
        else:
            if td.mcap < u.mcap_min_krw:
                continue
        # 유동성: 최근 20봉 평균 거래대금 (USD든 KRW든 동일 단위로 비교)
        # US는 value도 USD 기준이므로 별도 threshold 적용.
        if "value" in bars.columns and len(bars) >= 20:
            avg_value_20 = float(bars["value"].tail(20).mean())
            liq_min = (u.us_mcap_min_usd * 0.001) if td.market == "US" else u.liquidity_min_krw
            # US: ~$300K/day 정도가 적정 유동성 임계 (us_mcap_min × 0.001 ≈ $300K).
            if avg_value_20 < liq_min:
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
        # Stage 게이트 — allowed_stages_set 안에 들어야 통과. 빈 집합은 "전부 허용".
        stage_params = _stage_params_from_cfg(s)
        stage_val, stage_since = _compute_stage(bars, asof, stage_params)
        if allowed_stages_set and stage_val not in allowed_stages_set:
            continue

        # Minervini Template — 조건 검사는 항상 수행 (진단/UI 용), 게이트는 required만.
        rs_rank_value = float(rs_rank_map.get(td.ticker, float("nan")))
        t = tmpl.check_template(
            bars["close"].astype(float),
            rs_rank_value,
            sma200_slope_lookback=s.sma200_slope_lookback,
            rs_rank_min=float(s.rs_rank_min),
            high_series=bars["high"].astype(float) if "high" in bars.columns else None,
            low_series=bars["low"].astype(float) if "low" in bars.columns else None,
        )
        # 빈 required = Template 게이트 비활성. 그 외에는 지정된 조건들이 모두 True여야 통과.
        if required_conditions:
            if not all(
                0 < n <= len(t.checks) and t.checks[n - 1]
                for n in required_conditions
            ):
                continue

        # 게이트 통과 → 워치리스트 후보 (Darvas는 별도 BREAKOUT 시그널용).
        box_for_watch = darvas.current_box(bars, box_params)
        last_close = float(bars["close"].iloc[-1])
        suggested_stop = float(box_for_watch.bottom) if box_for_watch.bottom is not None else last_close * 0.93
        rs_mom_value = float(rs_momentum_map.get(td.ticker, float("nan"))) if rs_momentum_map else float("nan")

        # 거래량 수급/이탈 비율: 최근 20일 평균 / 최근 60일 평균. 1.1↑ 누적, 0.9↓ 이탈.
        # bars[volume]에 기반 — KR/US 공통 적용 가능. NaN-safe.
        vol_acc_ratio = float("nan")
        if "volume" in bars.columns and len(bars) >= 60:
            vol20 = float(bars["volume"].tail(20).mean())
            vol60 = float(bars["volume"].tail(60).mean())
            if vol60 > 0:
                vol_acc_ratio = vol20 / vol60

        f_entry = fundamentals_map.get(td.ticker) if fundamentals_map else None
        if f_entry is not None and f_entry.quarters is not None and not f_entry.quarters.empty:
            f_revenue_yoy = f_entry.latest_revenue_yoy
            f_op_yoy = f_entry.latest_op_income_yoy
            f_op_margin = f_entry.latest_op_margin
            f_net_margin = getattr(f_entry, "latest_net_margin", float("nan"))
            f_growth_q = f_entry.growth_quarters
            f_op_growth_q = f_entry.op_growth_quarters
            f_accelerating = f_entry.accelerating
        else:
            f_revenue_yoy = float("nan")
            f_op_yoy = float("nan")
            f_op_margin = float("nan")
            f_net_margin = float("nan")
            f_growth_q = 0
            f_op_growth_q = 0
            f_accelerating = False

        c_entry = consensus_map.get(td.ticker) if consensus_map else None
        if c_entry is not None and c_entry.available:
            c_n = c_entry.n_analysts
            c_tm = c_entry.target_mean if c_entry.target_mean else float("nan")
            c_upside = ((c_tm - last_close) / last_close * 100.0) if (c_tm and last_close > 0) else float("nan")
            c_rec = c_entry.recommendation
            c_fpe = c_entry.forward_pe if c_entry.forward_pe else float("nan")
            c_eg = (c_entry.earnings_growth * 100.0) if c_entry.earnings_growth is not None else float("nan")
            c_rg = (c_entry.revenue_growth * 100.0) if c_entry.revenue_growth is not None else float("nan")
            c_ls = c_entry.last_surprise_pct if c_entry.last_surprise_pct is not None else float("nan")
            c_rev_0y = c_entry.rev_eps_0y_30d_pct if c_entry.rev_eps_0y_30d_pct is not None else float("nan")
            c_rev_0q = c_entry.rev_eps_0q_30d_pct if c_entry.rev_eps_0q_30d_pct is not None else float("nan")
            c_ups = int(c_entry.eps_ups_30d) if c_entry.eps_ups_30d is not None else 0
            c_dwns = int(c_entry.eps_downs_30d) if c_entry.eps_downs_30d is not None else 0
            c_net = c_ups - c_dwns
        else:
            c_n = 0; c_tm = float("nan"); c_upside = float("nan")
            c_rec = None; c_fpe = float("nan"); c_eg = float("nan")
            c_rg = float("nan"); c_ls = float("nan")
            c_rev_0y = float("nan"); c_rev_0q = float("nan")
            c_ups = 0; c_dwns = 0; c_net = 0

        watchlist.append(WatchlistEntry(
            ticker=td.ticker, name=td.name, market=td.market,
            close=last_close, rs_rank=rs_rank_value, rs_momentum=rs_mom_value,
            box_state=str(box_for_watch.state),
            box_top=float(box_for_watch.top) if box_for_watch.top is not None else None,
            box_bottom=float(box_for_watch.bottom) if box_for_watch.bottom is not None else None,
            days_in_box=int(box_for_watch.days_in_box),
            suggested_stop=suggested_stop,
            stage=int(stage_val),
            template_checks=list(t.checks),
            revenue_yoy=f_revenue_yoy, op_income_yoy=f_op_yoy, op_margin=f_op_margin,
            net_margin=f_net_margin,
            growth_quarters=f_growth_q, op_growth_quarters=f_op_growth_q,
            accelerating=f_accelerating,
            vol_acc_ratio=vol_acc_ratio,
            cons_n_analysts=c_n, cons_target_mean=c_tm, cons_target_upside_pct=c_upside,
            cons_recommendation=c_rec, cons_forward_pe=c_fpe,
            cons_earnings_growth=c_eg, cons_revenue_growth=c_rg,
            cons_last_surprise_pct=c_ls,
            cons_rev_eps_0y_30d_pct=c_rev_0y, cons_rev_eps_0q_30d_pct=c_rev_0q,
            cons_eps_ups_30d=c_ups, cons_eps_downs_30d=c_dwns,
            cons_eps_net_revisions_30d=c_net,
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
