"""DRIP + 적립식 시뮬레이터.

"가속화 장기투자" 컨셉의 핵심: **배당 재투자(DRIP) + 정기 추가납입 + 주가 우상향 +
배당성장의 복리 선순환**으로 자산이 어떻게 가속화되는지 정량 시뮬.

순수 함수: 입력 (초기금, 월 추가납입, 보유기간, 현재가, 현재 DPS, 주가 CAGR, DPS CAGR,
배당빈도, 세율) → 월별 자산 곡선 DataFrame.

**가정 한계 — 변동성 0**:
  - ``simulate()``는 입력된 CAGR이 보유기간 내내 *동일하게* 유지되는 단일 시나리오만.
  - 약세장/감배/폭락 같은 *최악의 상황*은 반영 안 됨 → 낙관적 편향 있음.
  - 최악/최상 시나리오는 ``simulate_scenarios()`` 사용 (3 시나리오 묶음).

기타 가정:
  - 추가납입은 매월 말 (옵션으로 분기·연).
  - 배당은 분기 또는 연 1회(설정), 지급 즉시 100% 재투자.
  - 세금은 배당세 한 종류만 단순 차감(기본 15.4%, 사용자 조정 가능).
  - 매매수수료/매매세 무시 (장기 보유라 미미).
  - 부분주식 허용 (현실엔 없지만 시뮬 단순화). 결과 해석 시 주의.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import pandas as pd

# ============================================================
# Inputs
# ============================================================

@dataclass(frozen=True)
class DripInputs:
    ticker: str
    name: str
    asof: str                 # 시뮬 시작 기준일 YYYYMMDD
    initial_investment_krw: float
    monthly_contribution_krw: float
    years: int                # 보유 기간
    current_price_krw: float
    current_annual_dps_krw: float
    price_cagr_pct: float     # 연 % (예: 10 = 10%/yr)
    dps_cagr_pct: float       # 연 %
    dividend_frequency: int = 4   # 1=연1회, 2=반기, 4=분기, 12=월배당
    tax_rate_pct: float = 15.4    # 배당세
    contribution_frequency: int = 12  # 12=월, 4=분기, 1=연

    def __post_init__(self):
        if self.current_price_krw <= 0:
            raise ValueError("current_price_krw must be > 0")
        if self.years <= 0:
            raise ValueError("years must be > 0")
        if self.dividend_frequency not in (1, 2, 4, 12):
            raise ValueError("dividend_frequency must be one of {1,2,4,12}")
        if self.contribution_frequency not in (1, 2, 4, 12):
            raise ValueError("contribution_frequency must be one of {1,2,4,12}")


# ============================================================
# Result
# ============================================================

@dataclass
class DripResult:
    inputs: DripInputs
    timeline: pd.DataFrame    # 월별 (month_idx, date, price, shares, value, cum_invested, cum_dividend_gross, cum_dividend_net, yoc_pct)
    final_value_krw: float = 0.0
    total_invested_krw: float = 0.0
    total_dividend_net_krw: float = 0.0
    final_yoc_pct: float = 0.0    # 마지막 시점 연환산 배당수익률 (cost basis 기준)
    annualized_return_pct: float = 0.0   # CAGR of value over total invested cashflows
    notes: list[str] = field(default_factory=list)


# ============================================================
# Core simulation
# ============================================================

def _monthly_growth_from_annual(annual_pct: float) -> float:
    """연 CAGR(%) → 월 복리 성장률 (0.01 = 1%/month)."""
    return (1.0 + annual_pct / 100.0) ** (1.0 / 12.0) - 1.0


def simulate(inputs: DripInputs) -> DripResult:
    """월 단위 DRIP 시뮬레이션.

    매 월말 순서:
      1. 가격을 monthly_price_growth로 업데이트
      2. DPS를 monthly_dps_growth로 업데이트
      3. 추가납입 (contribution_frequency 충족 시) → shares += contrib / price
      4. 배당지급 (dividend_frequency 충족 시) → cash = shares × annual_dps / freq × (1 - tax),
         shares += cash / price (DRIP)
      5. 시점 자산 = shares × price 기록
    """
    n_months = inputs.years * 12
    mg_price = _monthly_growth_from_annual(inputs.price_cagr_pct)
    mg_dps = _monthly_growth_from_annual(inputs.dps_cagr_pct)

    # 추가납입은 contribution_frequency당 1번 — 월에 해당하는 step interval
    contrib_interval = 12 // inputs.contribution_frequency
    contrib_per_event = inputs.monthly_contribution_krw * 12 // inputs.contribution_frequency \
        if inputs.contribution_frequency != 12 else inputs.monthly_contribution_krw
    # 배당
    div_interval = 12 // inputs.dividend_frequency

    asof_dt = datetime.strptime(inputs.asof, "%Y%m%d")
    tax_factor = 1.0 - inputs.tax_rate_pct / 100.0

    # 초기 매입
    price = inputs.current_price_krw
    annual_dps = inputs.current_annual_dps_krw
    shares = inputs.initial_investment_krw / price if price > 0 else 0.0
    cum_invested = inputs.initial_investment_krw
    cum_div_gross = 0.0
    cum_div_net = 0.0
    # 평균 매입 단가는 가중평균. 초기에 initial_investment_krw로 shares 만큼 매입.
    total_cash_into_shares = inputs.initial_investment_krw

    rows = [{
        "month_idx": 0,
        "date": asof_dt.strftime("%Y%m%d"),
        "price": round(price, 2),
        "shares": round(shares, 4),
        "value": round(shares * price, 2),
        "cum_invested": round(cum_invested, 2),
        "cum_dividend_gross": 0.0,
        "cum_dividend_net": 0.0,
        "yoc_pct": 0.0,
    }]

    for m in range(1, n_months + 1):
        # 1. 가격·DPS 성장
        price *= (1.0 + mg_price)
        annual_dps *= (1.0 + mg_dps)

        # 2. 추가납입
        if m % contrib_interval == 0 and contrib_per_event > 0:
            shares_added = contrib_per_event / price if price > 0 else 0.0
            shares += shares_added
            cum_invested += contrib_per_event
            total_cash_into_shares += contrib_per_event

        # 3. 배당 (DRIP)
        if m % div_interval == 0:
            div_per_payment = annual_dps / inputs.dividend_frequency
            gross = shares * div_per_payment
            net = gross * tax_factor
            cum_div_gross += gross
            cum_div_net += net
            # DRIP — 세후 배당으로 추가 매입 (cost basis는 늘지 않음 — 회사 cash이므로)
            shares_added = net / price if price > 0 else 0.0
            shares += shares_added

        # YoC = (이번 연간 DPS) / 평균 매입 단가 × 100. 평균 매입단가는 invest 캐시 / shares-from-cash.
        # 단순화: total_cash_into_shares는 cum_invested와 동일 (DRIP은 cash 추가가 아니므로).
        avg_cost = (cum_invested / shares) if shares > 0 else 0.0
        yoc = (annual_dps / avg_cost * 100.0) if avg_cost > 0 else 0.0

        rows.append({
            "month_idx": m,
            "date": (asof_dt + timedelta(days=int(m * 30.4375))).strftime("%Y%m%d"),
            "price": round(price, 2),
            "shares": round(shares, 4),
            "value": round(shares * price, 2),
            "cum_invested": round(cum_invested, 2),
            "cum_dividend_gross": round(cum_div_gross, 2),
            "cum_dividend_net": round(cum_div_net, 2),
            "yoc_pct": round(yoc, 3),
        })

    timeline = pd.DataFrame(rows)
    final_value = float(timeline.iloc[-1]["value"])
    final_yoc = float(timeline.iloc[-1]["yoc_pct"])
    annualized = compute_money_weighted_return(timeline, inputs)

    return DripResult(
        inputs=inputs,
        timeline=timeline,
        final_value_krw=final_value,
        total_invested_krw=cum_invested,
        total_dividend_net_krw=cum_div_net,
        final_yoc_pct=final_yoc,
        annualized_return_pct=annualized,
    )


def compute_money_weighted_return(timeline: pd.DataFrame, inputs: DripInputs) -> float:
    """현금흐름 기반 연환산 수익률(IRR 근사).

    초기 -investment, 매 추가납입 시점 -contribution, 마지막 +final_value 로 가정하고
    Newton 방식 IRR. 단순 근사: years에 대한 CAGR((final_value / total_invested)^(1/years)-1).
    """
    if timeline.empty:
        return 0.0
    final_value = float(timeline.iloc[-1]["value"])
    total_invested = float(timeline.iloc[-1]["cum_invested"])
    if total_invested <= 0:
        return 0.0
    # 단순 비교 — 정확한 IRR이 아니라 *총 투입 대비 총 누적값*의 연환산. 사용자 직관 우선.
    n_years = max(inputs.years, 1)
    return round(((final_value / total_invested) ** (1.0 / n_years) - 1.0) * 100, 2)


# ============================================================
# Scenarios — 최악/baseline/최상
# ============================================================

def auto_volatility(price_cagr_pct: float, dps_cagr_pct: float) -> tuple[float, float]:
    """변동성 자동 추정 (보수적).

    Heuristic: 가격은 |CAGR|×0.3 + 5%p, DPS는 |CAGR|×0.2 + 2%p, 둘 다 cap ±10%p.
    근거:
      - 5년 CAGR이 좋은 종목은 *selection bias* (좋았던 구간이라 선정됨) → 낙관이
        20년 이상 더 좋을 거라 가정하면 비현실적.
      - 한국 시장 장기 변동성: 코스피 연환산 std ~15%p, 대형주는 ~10%p, 안정 배당주
        ~5-8%p. 따라서 cap 10%p로 *시장 평균 변동성*에 가깝게.
      - 사용자가 종목 특성 안다면 slider로 명시 조정 가능.

    Returns (price_vol_pp, dps_vol_pp), 각각 cap 10%p / 6%p.
    """
    price_vol = min(abs(price_cagr_pct) * 0.3 + 5.0, 10.0)
    dps_vol = min(abs(dps_cagr_pct) * 0.2 + 2.0, 6.0)
    return price_vol, dps_vol


def simulate_scenarios(
    inputs: DripInputs,
    *,
    price_volatility_pp: float | None = None,
    dps_volatility_pp: float | None = None,
) -> dict[str, DripResult]:
    """3 시나리오 묶음 시뮬레이션 — '비관' / 'baseline' / '낙관'.

    Parameters
    ----------
    price_volatility_pp : 주가 CAGR을 ±N%p 흔드는 폭. None이면 auto.
    dps_volatility_pp   : DPS CAGR ±N%p. None이면 auto.

    비관: price_cagr − vol, dps_cagr − vol (감배 가정도 dps 음수면 그대로 차감)
    baseline: 입력 그대로
    낙관: price_cagr + vol, dps_cagr + vol

    Returns dict 순서는 [비관, baseline, 낙관] 으로 정렬돼서 차트 stacking에 유리.
    """
    if price_volatility_pp is None or dps_volatility_pp is None:
        auto_p, auto_d = auto_volatility(inputs.price_cagr_pct, inputs.dps_cagr_pct)
        if price_volatility_pp is None:
            price_volatility_pp = auto_p
        if dps_volatility_pp is None:
            dps_volatility_pp = auto_d

    def _adj(price_delta: float, dps_delta: float) -> DripInputs:
        return replace(
            inputs,
            price_cagr_pct=inputs.price_cagr_pct + price_delta,
            dps_cagr_pct=inputs.dps_cagr_pct + dps_delta,
        )

    return {
        "비관": simulate(_adj(-price_volatility_pp, -dps_volatility_pp)),
        "baseline": simulate(inputs),
        "낙관": simulate(_adj(+price_volatility_pp, +dps_volatility_pp)),
    }


# ============================================================
# Multi-ticker comparison
# ============================================================

def simulate_many(inputs_list: list[DripInputs]) -> dict[str, DripResult]:
    """여러 종목 동시 시뮬레이션. 같은 보유기간/추가납입 가정 시 곡선 비교용."""
    return {inp.ticker: simulate(inp) for inp in inputs_list}
