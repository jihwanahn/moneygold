"""Momentum Breakout 전략 — N일 신고가 돌파 + 거래대금 스파이크.

진입 (당일 종가 기준, AND):
  1. 거래대금 상위 N위 (기본 100)
  2. (옵션) 시가총액 상위 N위
  3. 종가 > 직전 ``new_high_lookback`` 일 최고 종가 (기본 60일)
  4. 직전 ``fresh_window`` 일 내 위 조건이 처음 깨짐 (반복 돌파 제외, 기본 20일)
  5. 당일 거래대금 ≥ 20일 평균 거래대금 × ``volume_spike_ratio`` (기본 1.5)
  6. 상장 ``min_listed_days`` 일 이상 (기본 60일)
  7. 우선주/스팩/리츠/ETF/ETN 제외 (universe.py 재사용)
  8. flagged (관리/거래정지) 제외  ※ 현재 universe.is_flagged()는 placeholder

포지션 (상태머신):
  INITIAL → TRAILING → EXITED
  - INITIAL: entry_price 기록, stop_loss = entry × (1 - stop_loss_pct)
  - high_since_entry ≥ entry × (1 + profit_trigger_pct) 도달 시 TRAILING 전환
  - TRAILING: stop_loss = max(prev_stop, MA20). 절대 후퇴 X.
  - 종가 ≤ stop_loss → EXITED (청산)
  - 갭다운으로 시초가 < stop_loss: gap_down_exit_policy='open' 이면 시가 청산,
    'close' 이면 종가 청산

전체 자동 주문 없음 — 시그널만 생성, 매매 결정은 사용자.

원칙:
  - 모든 함수 ``asof: str`` 명시. ``datetime.now()`` 호출 없음.
  - bars의 ``value`` 컬럼이 거래대금 (KRW) 직접 — close×volume 계산 금지.
  - bars는 이미 수정주가 (adj_factor=1.0 검증됨).
"""
from __future__ import annotations

from .config import MomentumConfig, load_momentum_config
from .indicators import (
    is_fresh_breakout,
    ma20,
    rolling_new_high,
    volume_spike,
)
from .position import (
    EXITED,
    INITIAL,
    TRAILING,
    PositionEntry,
    PositionState,
    PositionStep,
)
from .position import (
    step as position_step,
)
from .signals import BreakoutEntry, find_entry_candidates

__all__ = [
    "MomentumConfig", "load_momentum_config",
    "rolling_new_high", "is_fresh_breakout", "volume_spike", "ma20",
    "INITIAL", "TRAILING", "EXITED",
    "PositionEntry", "PositionState", "PositionStep", "position_step",
    "BreakoutEntry", "find_entry_candidates",
]
