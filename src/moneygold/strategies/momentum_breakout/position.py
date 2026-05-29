"""포지션 상태머신: INITIAL → TRAILING → EXITED.

순수 함수로 모델링 — 외부 mutable state 없음. 호출자가 (entry, prev_state, today_bar)
를 넘기면 (new_state, action) 반환.

상태:
  INITIAL  : 진입 직후 ~ +profit_trigger_pct 도달 전. stop = entry × (1-stop_loss_pct).
  TRAILING : +profit_trigger_pct 한 번 닿은 이후. stop = max(prev_stop, MA20).
  EXITED   : 청산 완료. 더 이상 step 불필요.

청산 트리거:
  - 종가 ≤ stop  →  exit_reason='STOP_HIT', exit_price = stop (개념적; 실 체결은 다음 봉)
      └ 단, 시초가가 이미 stop보다 낮으면 갭다운 → exit_price 는 cfg.gap_down_exit_policy
        에 따라 시가 or 종가.

step() 은 *상태만* 갱신. 실제 체결가/매매 처리는 호출자 (백테스트 / 사용자) 책임.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from . import indicators as ind
from .config import MomentumConfig

INITIAL = "INITIAL"
TRAILING = "TRAILING"
EXITED = "EXITED"

PhaseT = Literal["INITIAL", "TRAILING", "EXITED"]


@dataclass(frozen=True)
class PositionEntry:
    """진입 시점에 박는 불변 메타.

    Fields
    ------
    ticker, name, market : 식별.
    entry_date : YYYYMMDD.
    entry_price : 진입 단가 (보통 시그널 다음날 시가).
    """
    ticker: str
    name: str
    market: str
    entry_date: str
    entry_price: float


@dataclass(frozen=True)
class PositionState:
    """일별 갱신되는 상태.

    Fields
    ------
    phase : INITIAL | TRAILING | EXITED.
    stop : 현재 손절가. 후퇴 절대 X.
    high_since_entry : 진입 이후 최고 종가 (TRAILING 전환 트리거 추적).
    asof : 이 상태가 계산된 시점 (YYYYMMDD).
    """
    phase: PhaseT
    stop: float
    high_since_entry: float
    asof: str


@dataclass(frozen=True)
class PositionStep:
    """step() 반환. 다음 상태 + (청산 시) 청산 사유/가격.

    exit_reason :
      None         — 보유 지속
      'STOP_HIT'   — 종가/시가 stop 이탈
    exit_price :
      청산 가격 (개념적 — 백테스트는 다음 봉 시가 사용 권장).
      gap_down_exit_policy == 'open' 이고 시가 < stop 이면 시가, 아니면 stop (intraday).
    """
    state: PositionState
    exit_reason: str | None = None
    exit_price: float | None = None
    gap_down: bool = False
    trail_updated: bool = False


def initial_state(entry: PositionEntry, cfg: MomentumConfig) -> PositionState:
    """진입 직후 상태. stop = entry × (1 - stop_loss_pct), phase = INITIAL."""
    stop = entry.entry_price * (1.0 - cfg.stop_loss_pct)
    return PositionState(
        phase=INITIAL,
        stop=float(stop),
        high_since_entry=float(entry.entry_price),
        asof=entry.entry_date,
    )


def step(
    entry: PositionEntry,
    prev: PositionState,
    today_bar: pd.Series | dict,
    cfg: MomentumConfig,
    *,
    ma20_today: float | None = None,
) -> PositionStep:
    """하루 진행. (prev → next, action) 반환.

    Parameters
    ----------
    entry : 불변 메타.
    prev : 어제까지의 상태.
    today_bar : dict-like with keys 'date','open','close' (최소). 'low' 있으면 미사용.
    cfg : MomentumConfig.
    ma20_today : 오늘 종가 포함한 MA20 값. TRAILING 갱신용. None이면 trailing 안 함.

    Returns
    -------
    PositionStep
    """
    if prev.phase == EXITED:
        return PositionStep(state=prev)

    today_date = str(today_bar["date"])
    today_open = float(today_bar["open"])
    today_close = float(today_bar["close"])

    # 1) 갭다운 체크 — 시초가가 stop 아래로 떨어졌나
    gap_down = today_open < prev.stop

    # 2) 청산 트리거: 종가 ≤ stop OR 갭다운
    if gap_down or today_close <= prev.stop:
        if gap_down and cfg.gap_down_exit_policy == "open":
            exit_price = today_open
        elif gap_down and cfg.gap_down_exit_policy == "close":
            exit_price = today_close
        else:
            # 갭다운 아닌데 종가 이탈 — stop 으로 청산 (intraday touched 가정)
            exit_price = prev.stop
        new_state = PositionState(
            phase=EXITED,
            stop=prev.stop,
            high_since_entry=max(prev.high_since_entry, today_close),
            asof=today_date,
        )
        return PositionStep(
            state=new_state,
            exit_reason="STOP_HIT",
            exit_price=float(exit_price),
            gap_down=gap_down,
        )

    # 3) high_since_entry 갱신
    new_high = max(prev.high_since_entry, today_close)

    # 4) INITIAL → TRAILING 전환?
    trigger = entry.entry_price * (1.0 + cfg.profit_trigger_pct)
    new_phase: PhaseT = prev.phase
    if prev.phase == INITIAL and new_high >= trigger:
        new_phase = TRAILING

    # 5) Stop 갱신 (TRAILING 만)
    new_stop = prev.stop
    trail_updated = False
    if new_phase == TRAILING and ma20_today is not None and np.isfinite(ma20_today):
        candidate = float(ma20_today)
        if candidate > new_stop:
            new_stop = candidate
            trail_updated = True

    new_state = PositionState(
        phase=new_phase,
        stop=float(new_stop),
        high_since_entry=float(new_high),
        asof=today_date,
    )
    return PositionStep(state=new_state, trail_updated=trail_updated)


def run_through_bars(
    entry: PositionEntry,
    bars_since_entry: pd.DataFrame,
    cfg: MomentumConfig,
) -> tuple[PositionState, list[PositionStep]]:
    """진입 이후 봉들을 순차 적용. 청산 시 거기서 중단.

    bars_since_entry : entry_date *다음* 봉부터. columns ⊇ {'date','open','close'}.
        MA20 갱신을 위해 ``ma20`` 컬럼 또는 충분한 과거 close가 함께 와도 됨.
        여기선 단순히 close 시계열을 자체적으로 ma20 계산 후 정렬해 사용.

    Returns
    -------
    (final_state, steps)
        steps[-1].state == final_state. 청산되면 steps[-1].exit_reason 가 채워짐.
    """
    state = initial_state(entry, cfg)
    steps: list[PositionStep] = []
    if bars_since_entry is None or bars_since_entry.empty:
        return state, steps

    b = bars_since_entry.sort_values("date").reset_index(drop=True)
    close_series = b["close"].astype(float)
    ma20_series = ind.ma20(close_series, cfg.trailing_ma_period)

    for i, row in b.iterrows():
        ma_val = ma20_series.iloc[i]
        ma_today = float(ma_val) if pd.notna(ma_val) else None
        result = step(entry, state, row, cfg, ma20_today=ma_today)
        steps.append(result)
        state = result.state
        if state.phase == EXITED:
            break
    return state, steps
