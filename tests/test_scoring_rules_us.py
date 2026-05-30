"""US DGI 점수 함수 테스트 (scoring_rules_us)."""
from __future__ import annotations

import pytest

from moneygold.strategies.value_long_term import scoring_rules_us as R


@pytest.mark.parametrize("yld, expected", [
    (None, 0), (0, 0), (-1, 0), (0.5, 0),
    (1.0, 2), (2.0, 4), (3.0, 6), (5.0, 8), (10.0, 8),
])
def test_score_dividend_yield(yld, expected):
    assert R.score_dividend_yield(yld) == expected


@pytest.mark.parametrize("years, expected", [
    (None, 0), (0, 0), (2, 0),
    (3, 4), (5, 8), (10, 12), (15, 15), (50, 15),
])
def test_score_consecutive_increase(years, expected):
    assert R.score_consecutive_increase(years) == expected


@pytest.mark.parametrize("cagr, expected", [
    (None, 0), (-5, 0),
    (0, 1), (3, 3), (5, 6), (7, 9), (10, 12), (50, 12),
])
def test_score_dps_cagr(cagr, expected):
    assert R.score_dps_cagr(cagr) == expected


@pytest.mark.parametrize("payout, expected", [
    (None, 0), (-10, 0),
    (10, 2),                  # 15 미만, 90 이하 → 2
    (20, 6),                  # 15~80 외곽
    (30, 10), (50, 10), (70, 10),   # sweet spot
    (75, 6), (85, 2),
    (95, 0), (120, 0),
])
def test_score_payout_stability(payout, expected):
    assert R.score_payout_stability(payout) == expected


@pytest.mark.parametrize("roe, expected", [
    (None, 0), (3, 0),
    (5, 2), (10, 4), (15, 6), (20, 8), (45, 8),
])
def test_score_roe(roe, expected):
    assert R.score_roe(roe) == expected


@pytest.mark.parametrize("cv, expected", [
    (None, 0), (-0.1, 0),
    (0.0, 7), (0.19, 7), (0.20, 5), (0.29, 5), (0.30, 3), (0.49, 3), (0.50, 0),
])
def test_score_eps_stability(cv, expected):
    assert R.score_eps_stability(cv) == expected


@pytest.mark.parametrize("years, expected", [
    (None, 0), (0, 0), (9, 0),
    (10, 3), (15, 5), (25, 8), (50, 10), (60, 10),
])
def test_score_aristocrat_status(years, expected):
    assert R.score_aristocrat_status(years) == expected


def test_aristocrat_label():
    assert R.aristocrat_label(60) == "King (50년+)"
    assert R.aristocrat_label(30) == "Aristocrat (25년+)"
    assert R.aristocrat_label(15) == "Champion (15년+)"
    assert R.aristocrat_label(10) == "Contender (10년+)"
    assert R.aristocrat_label(5) == ""
    assert R.aristocrat_label(None) == ""


def test_total_max_is_100():
    assert R.TOTAL_MAX == 100
    assert R.DIVIDEND_MAX == 45
    assert R.CAPITAL_MAX == 30
    assert R.FUNDAMENTAL_MAX == 15
    assert R.ARISTOCRAT_MAX == 10


def test_grade_thresholds():
    # income 컴파운더 보정 컷: A75 / B65
    assert R.grade(75) == "A"
    assert R.grade(74) == "B"
    assert R.grade(69) == "B"          # KO/JNJ류 배당킹 — B로 인정
    assert R.grade(65) == "B"
    assert R.grade(64) == "C"


def test_perfect_dividend_aristocrat_scores_high():
    """KO 같은 50년+ 귀족: yield 3% + 50년 인상 + DPS CAGR 6% + payout 65% 가정."""
    total = (
        R.score_dividend_yield(3.0)          # 6
        + R.score_consecutive_increase(60)   # 15
        + R.score_dps_cagr(6.0)              # 6
        + R.score_payout_stability(65.0)     # 10
        + R.score_aristocrat_status(60)      # 10
    )
    # 배당 37 + 귀족 10 = 47, 자본/펀더 빼고도 47 → 자본·펀더 합치면 A 가능
    assert total == 47
