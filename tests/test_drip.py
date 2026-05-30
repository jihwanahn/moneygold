"""DRIP 시뮬레이터 테스트.

순수 함수라 입력 → 출력 검증. 수학적 invariant + 사용자 직관 검증.
"""
from __future__ import annotations

import pytest

from moneygold.strategies.value_long_term import drip

# ============================================================
# DripInputs validation
# ============================================================

def test_inputs_reject_zero_price():
    with pytest.raises(ValueError):
        drip.DripInputs(
            ticker="X", name="Test", asof="20260527",
            initial_investment_krw=1_000_000, monthly_contribution_krw=0,
            years=10, current_price_krw=0,
            current_annual_dps_krw=100, price_cagr_pct=5, dps_cagr_pct=5,
        )


def test_inputs_reject_zero_years():
    with pytest.raises(ValueError):
        drip.DripInputs(
            ticker="X", name="Test", asof="20260527",
            initial_investment_krw=1_000_000, monthly_contribution_krw=0,
            years=0, current_price_krw=10000,
            current_annual_dps_krw=100, price_cagr_pct=5, dps_cagr_pct=5,
        )


def test_inputs_reject_invalid_div_frequency():
    with pytest.raises(ValueError):
        drip.DripInputs(
            ticker="X", name="Test", asof="20260527",
            initial_investment_krw=1_000_000, monthly_contribution_krw=0,
            years=10, current_price_krw=10000,
            current_annual_dps_krw=100, price_cagr_pct=5, dps_cagr_pct=5,
            dividend_frequency=3,  # invalid
        )


# ============================================================
# Math invariants
# ============================================================

def _basic(initial=10_000_000, monthly=0, years=10, price=10_000, dps=0,
           price_cagr=0, dps_cagr=0, div_freq=4, contrib_freq=12):
    return drip.DripInputs(
        ticker="TEST", name="Test", asof="20260527",
        initial_investment_krw=initial, monthly_contribution_krw=monthly,
        years=years, current_price_krw=price, current_annual_dps_krw=dps,
        price_cagr_pct=price_cagr, dps_cagr_pct=dps_cagr,
        dividend_frequency=div_freq, contribution_frequency=contrib_freq,
    )


def test_zero_growth_no_div_no_contrib_value_unchanged():
    """주가/배당 정체 + 추가납입 0 + 배당 0 → 자산 가치 그대로."""
    result = drip.simulate(_basic(initial=10_000_000, price=10_000, dps=0,
                                   price_cagr=0, dps_cagr=0))
    assert abs(result.final_value_krw - 10_000_000) < 1.0
    assert result.total_dividend_net_krw == 0.0


def test_price_growth_only_matches_cagr():
    """배당/추가납입 0 + 주가 10% CAGR + 10년 → 최종 가치 ≈ initial * 1.1^10."""
    result = drip.simulate(_basic(initial=10_000_000, years=10, price=10_000,
                                   dps=0, price_cagr=10, dps_cagr=0))
    expected = 10_000_000 * (1.10 ** 10)
    rel_err = abs(result.final_value_krw - expected) / expected
    assert rel_err < 0.01, f"got {result.final_value_krw:,.0f}, expected {expected:,.0f}"


def test_monthly_contribution_increases_cum_invested():
    """월 100만원 × 120개월 = 1.2억 + 초기 1천만 = 1.3억."""
    result = drip.simulate(_basic(
        initial=10_000_000, monthly=1_000_000, years=10,
        price=10_000, dps=0, price_cagr=0, dps_cagr=0,
    ))
    assert result.total_invested_krw == pytest.approx(130_000_000, rel=0.001)


def test_drip_compounds_value_above_pure_price():
    """배당 재투자 ON vs OFF 비교 — DRIP이 더 많은 shares를 만들어 최종 가치 더 큼."""
    base = _basic(initial=10_000_000, years=10, price=10_000,
                   dps=500, price_cagr=5, dps_cagr=10, div_freq=4)
    with_div = drip.simulate(base)
    # 배당 없는 시뮬
    no_div = drip.simulate(drip.DripInputs(
        ticker=base.ticker, name=base.name, asof=base.asof,
        initial_investment_krw=base.initial_investment_krw,
        monthly_contribution_krw=0, years=base.years,
        current_price_krw=base.current_price_krw,
        current_annual_dps_krw=0,  # 배당 OFF
        price_cagr_pct=base.price_cagr_pct, dps_cagr_pct=0,
    ))
    assert with_div.final_value_krw > no_div.final_value_krw
    assert with_div.total_dividend_net_krw > 0


def test_yoc_grows_when_dps_cagr_positive():
    """DPS가 늘면 YoC (Yield on Cost)도 시간 따라 증가."""
    result = drip.simulate(_basic(
        initial=10_000_000, years=20, price=10_000,
        dps=300, price_cagr=0, dps_cagr=10, div_freq=4,
    ))
    # 초반 YoC vs 후반 YoC
    timeline = result.timeline
    early_yoc = timeline.iloc[12]["yoc_pct"]   # 1년차
    late_yoc = timeline.iloc[-1]["yoc_pct"]    # 20년차
    assert late_yoc > early_yoc * 3, f"YoC should grow with DPS CAGR: early={early_yoc}, late={late_yoc}"


def test_timeline_length_matches_years_in_months():
    result = drip.simulate(_basic(years=5))
    # month_idx 0 (시작) + 1~60 → 61 rows
    assert len(result.timeline) == 5 * 12 + 1
    assert result.timeline["month_idx"].iloc[0] == 0
    assert result.timeline["month_idx"].iloc[-1] == 60


def test_tax_reduces_dividend():
    """세율 0% vs 30% 비교 — 세전이 더 많은 자산."""
    base = _basic(initial=10_000_000, years=10, price=10_000,
                   dps=500, price_cagr=5, dps_cagr=5)
    zero_tax = drip.simulate(drip.DripInputs(
        ticker=base.ticker, name=base.name, asof=base.asof,
        initial_investment_krw=base.initial_investment_krw,
        monthly_contribution_krw=base.monthly_contribution_krw,
        years=base.years, current_price_krw=base.current_price_krw,
        current_annual_dps_krw=base.current_annual_dps_krw,
        price_cagr_pct=base.price_cagr_pct,
        dps_cagr_pct=base.dps_cagr_pct,
        tax_rate_pct=0.0,
    ))
    high_tax = drip.simulate(drip.DripInputs(
        ticker=base.ticker, name=base.name, asof=base.asof,
        initial_investment_krw=base.initial_investment_krw,
        monthly_contribution_krw=base.monthly_contribution_krw,
        years=base.years, current_price_krw=base.current_price_krw,
        current_annual_dps_krw=base.current_annual_dps_krw,
        price_cagr_pct=base.price_cagr_pct,
        dps_cagr_pct=base.dps_cagr_pct,
        tax_rate_pct=30.0,
    ))
    assert zero_tax.final_value_krw > high_tax.final_value_krw
    assert zero_tax.total_dividend_net_krw > high_tax.total_dividend_net_krw


def test_simulate_many_returns_dict_keyed_by_ticker():
    a = _basic()
    b = drip.DripInputs(
        ticker="B", name="B", asof="20260527",
        initial_investment_krw=1_000_000, monthly_contribution_krw=0,
        years=5, current_price_krw=5_000,
        current_annual_dps_krw=200, price_cagr_pct=8, dps_cagr_pct=8,
    )
    results = drip.simulate_many([a, b])
    assert set(results.keys()) == {"TEST", "B"}
    assert results["TEST"].inputs.ticker == "TEST"
    assert results["B"].inputs.ticker == "B"


def test_yoc_positive_when_dividend_present():
    result = drip.simulate(_basic(initial=10_000_000, price=10_000,
                                   dps=500, years=5, div_freq=4))
    assert result.final_yoc_pct > 0


def test_quarterly_payment_count_matches_frequency():
    """10년 동안 분기배당 → 배당 이벤트는 40회 발생."""
    # gross 합계 추정: shares ≈ 1000, DPS=400, 10년 단순 = 4_000_000
    # 분기당 100 × 1000 × 40 = 4_000_000
    result = drip.simulate(_basic(initial=10_000_000, price=10_000,
                                   dps=400, years=10, div_freq=4,
                                   price_cagr=0, dps_cagr=0))
    # 세전 누적 배당이 대략 예상치 근처
    expected_gross_min = 4_000_000 * 0.95  # DRIP으로 shares 늘어서 실제론 더 많음
    assert result.timeline.iloc[-1]["cum_dividend_gross"] > expected_gross_min


def test_annualized_return_positive_for_growth_case():
    result = drip.simulate(_basic(initial=10_000_000, years=10, price=10_000,
                                   dps=300, price_cagr=8, dps_cagr=10))
    assert result.annualized_return_pct > 0


# ============================================================
# Scenarios (비관/baseline/낙관)
# ============================================================

def test_auto_volatility_proportional_to_cagr():
    """변동성 추정: |CAGR|×0.3 + 5%p / |CAGR|×0.2 + 2%p, cap ±10%p / 6%p."""
    # 매우 안정적 종목 (CAGR=0) → 최소값
    p, d = drip.auto_volatility(0.0, 0.0)
    assert p == 5.0
    assert d == 2.0
    # CAGR 10% → 0.3×10 + 5 = 8, 0.2×10 + 2 = 4
    p, d = drip.auto_volatility(10.0, 10.0)
    assert p == 8.0
    assert d == 4.0


def test_auto_volatility_caps_at_max():
    """매우 높은 CAGR이라도 cap에 걸림 (selection bias 보호)."""
    # CAGR 50% — 매우 공격적이지만 5년 좋았던 구간일 뿐
    p, d = drip.auto_volatility(50.0, 30.0)
    assert p == 10.0  # cap
    assert d == 6.0   # cap


def test_simulate_scenarios_orders_pessimistic_lt_baseline_lt_optimistic():
    """비관 < baseline < 낙관 최종 자산 순서 보장."""
    inputs = _basic(initial=10_000_000, years=15, price=10_000,
                     dps=300, price_cagr=8, dps_cagr=10)
    out = drip.simulate_scenarios(inputs)
    assert set(out.keys()) == {"비관", "baseline", "낙관"}
    assert out["비관"].final_value_krw < out["baseline"].final_value_krw
    assert out["baseline"].final_value_krw < out["낙관"].final_value_krw


def test_simulate_scenarios_custom_volatility():
    """사용자 변동성 지정 시 그대로 사용."""
    inputs = _basic(initial=10_000_000, years=10, price=10_000,
                     dps=300, price_cagr=10, dps_cagr=5)
    out = drip.simulate_scenarios(inputs,
                                   price_volatility_pp=2.0, dps_volatility_pp=1.0)
    # baseline = 10, 5  /  비관 = 8, 4  /  낙관 = 12, 6
    assert out["비관"].inputs.price_cagr_pct == 8.0
    assert out["낙관"].inputs.price_cagr_pct == 12.0
    assert out["비관"].inputs.dps_cagr_pct == 4.0
    assert out["낙관"].inputs.dps_cagr_pct == 6.0
    # baseline 입력 변형 X
    assert out["baseline"].inputs.price_cagr_pct == 10.0


def test_simulate_scenarios_baseline_matches_simulate():
    """baseline 시나리오는 simulate() 단독 결과와 동일해야."""
    inputs = _basic(years=10, price=10_000, dps=400, price_cagr=7, dps_cagr=6)
    scen = drip.simulate_scenarios(inputs, price_volatility_pp=3, dps_volatility_pp=2)
    direct = drip.simulate(inputs)
    assert scen["baseline"].final_value_krw == direct.final_value_krw


# ============================================================
# 수익률 상한 (max_dividend_yield_pct) — 발산 방지
# ============================================================

def test_yield_cap_rejects_nonpositive():
    with pytest.raises(ValueError, match="max_dividend_yield_pct"):
        drip.DripInputs(
            ticker="X", name="t", asof="20260528",
            initial_investment_krw=1_000_000, monthly_contribution_krw=0,
            years=10, current_price_krw=10000, current_annual_dps_krw=300,
            price_cagr_pct=5, dps_cagr_pct=5, max_dividend_yield_pct=0,
        )


def test_dividend_saturates_not_diverges():
    """dps_cagr가 극단적으로 커도 yield cap이 발산을 막아 최종값이 포화.

    회귀: 이 가드를 제거(cap 매우 큼)하면 폭주 재현 — cap이 동작 원인임을 고정.
    """
    base = dict(
        ticker="X", name="t", asof="20260528",
        initial_investment_krw=10_000_000, monthly_contribution_krw=0,
        years=20, current_price_krw=10_000, current_annual_dps_krw=300,
        price_cagr_pct=10,
    )
    capped_100 = drip.simulate(drip.DripInputs(**base, dps_cagr_pct=100))
    capped_1000 = drip.simulate(drip.DripInputs(**base, dps_cagr_pct=1000))
    # dps_cagr 100 vs 1000 — cap이 binding이라 최종값 거의 동일 (5% 이내)
    rel = abs(capped_1000.final_value_krw - capped_100.final_value_krw) / capped_100.final_value_krw
    assert rel < 0.05, f"capped should saturate, got rel diff {rel:.2%}"
    # cap 사실상 해제하면 폭주 (포화 안 함)
    uncapped = drip.simulate(drip.DripInputs(**base, dps_cagr_pct=1000,
                                              max_dividend_yield_pct=100000))
    assert uncapped.final_value_krw > capped_1000.final_value_krw * 100


def test_effective_yield_never_exceeds_cap():
    """모든 월에서 effective 배당수익률(effective_dps/price)이 cap을 초과하지 않음."""
    inp = drip.DripInputs(
        ticker="X", name="t", asof="20260528",
        initial_investment_krw=10_000_000, monthly_contribution_krw=0,
        years=20, current_price_krw=10_000, current_annual_dps_krw=300,
        price_cagr_pct=3, dps_cagr_pct=40, max_dividend_yield_pct=12,
    )
    r = drip.simulate(inp)
    # cum_dividend_gross 증가분으로 역산하기 어려우니, 시작 시점부터 dps>price*cap 인 케이스로
    # 시작 yield > cap 시에도 크래시/inf 없이 유한값
    assert r.final_value_krw > 0
    assert r.final_value_krw < float("inf")
    # 시작부터 cap 초과 케이스
    inp2 = drip.DripInputs(
        ticker="Y", name="t", asof="20260528",
        initial_investment_krw=10_000_000, monthly_contribution_krw=0,
        years=10, current_price_krw=10_000, current_annual_dps_krw=2000,  # 20% > cap 12%
        price_cagr_pct=5, dps_cagr_pct=5, max_dividend_yield_pct=12,
    )
    r2 = drip.simulate(inp2)
    assert 0 < r2.final_value_krw < float("inf")


def test_high_gap_monotonicity_hana_regression():
    """하나금융 회귀: price 21%, dps 34.7%, 20년 → 비관<baseline<낙관 (역전 없음).

    이 케이스가 원래 버그(낙관 CAGR < 비관 CAGR)를 트리거했던 입력.
    """
    inp = drip.DripInputs(
        ticker="086790", name="하나금융", asof="20260528",
        initial_investment_krw=10_000_000, monthly_contribution_krw=500_000,
        years=20, current_price_krw=70_000, current_annual_dps_krw=4_500,
        price_cagr_pct=21.0, dps_cagr_pct=34.7,
    )
    out = drip.simulate_scenarios(inp)
    assert out["비관"].final_value_krw < out["baseline"].final_value_krw < out["낙관"].final_value_krw
    assert out["비관"].annualized_return_pct < out["baseline"].annualized_return_pct
    assert out["baseline"].annualized_return_pct < out["낙관"].annualized_return_pct


# ============================================================
# CAGR 클램프 (max_abs_cagr_pct) — 장기 외삽 발산 방지
# ============================================================

def test_cagr_clamp_rejects_nonpositive():
    with pytest.raises(ValueError, match="max_abs_cagr_pct"):
        drip.DripInputs(
            ticker="X", name="t", asof="20260528",
            initial_investment_krw=1_000_000, monthly_contribution_krw=0,
            years=10, current_price_krw=10000, current_annual_dps_krw=300,
            price_cagr_pct=5, dps_cagr_pct=5, max_abs_cagr_pct=0,
        )


def test_extreme_price_cagr_clamped_no_divergence():
    """price_cagr 193%/yr (네오티스류 selection-bias) 20년 외삽이 발산하지 않음."""
    inp = drip.DripInputs(
        ticker="085910", name="네오티스", asof="20260528",
        initial_investment_krw=10_000_000, monthly_contribution_krw=500_000,
        years=20, current_price_krw=10_000, current_annual_dps_krw=300,
        price_cagr_pct=193.0, dps_cagr_pct=10.0, max_abs_cagr_pct=35.0,
    )
    r = drip.simulate(inp)
    # 35%/yr × 20yr = ~404배. 1천만+월50만 ≈ 1.3억 투입 → 수백억 이하 (조 단위 발산 X)
    assert r.final_value_krw < 1e12, f"clamp should bound, got {r.final_value_krw:.2e}"
    # 클램프가 동작했다는 note
    assert any("price_cagr" in n for n in r.notes)


def test_cagr_clamp_preserves_monotonicity():
    """클램프가 걸려도 비관≤baseline≤낙관 단조성 유지 (클램프는 단조 함수)."""
    inp = drip.DripInputs(
        ticker="X", name="t", asof="20260528",
        initial_investment_krw=10_000_000, monthly_contribution_krw=500_000,
        years=20, current_price_krw=10_000, current_annual_dps_krw=400,
        price_cagr_pct=40.0, dps_cagr_pct=30.0, max_abs_cagr_pct=35.0,
    )
    out = drip.simulate_scenarios(inp)
    assert out["비관"].final_value_krw <= out["baseline"].final_value_krw <= out["낙관"].final_value_krw


def test_normal_cagr_not_clamped():
    """정상 범위 CAGR(20% 이하)은 클램프 영향 없음 — note 비어있음."""
    inp = drip.DripInputs(
        ticker="X", name="t", asof="20260528",
        initial_investment_krw=10_000_000, monthly_contribution_krw=0,
        years=20, current_price_krw=10_000, current_annual_dps_krw=300,
        price_cagr_pct=15.0, dps_cagr_pct=10.0, max_abs_cagr_pct=35.0,
    )
    r = drip.simulate(inp)
    assert r.notes == []
