"""Darvas Box 탐지 + 상태머신.

ARCHITECTURE.md §6 참조.

상태:
  SEARCHING       — 천장 미확정. 내부적으로 천장 후보(top_candidate)와 확정대기일을 추적.
  FORMING         — 천장 확정, 박스 안 체류. BOX_VALID_MIN_DAYS 미달.
  CONFIRMED       — 박스 유효 (BOX_VALID_MIN_DAYS 이상 체류). 돌파/이탈/스테일 대기.
  BREAKOUT_TODAY  — 종가가 천장 × (1+BUFFER) 초과 + 거래량 ≥ 1.5×50일 평균. (터미널)
  BREAKOUT_GAP    — 위 조건 + 시가도 이미 천장 위 (갭상승). (터미널)
  BROKEN_DOWN     — 종가가 박스 바닥 미만. (터미널)
  STALE           — CONFIRMED 후 BOX_STALE_DAYS 경과 무돌파. (터미널)

터미널 상태 이후 다음 봉부터 SEARCHING으로 리셋.

가격 기준:
  - 천장 = 고가(high)의 BOX_HIGH_LOOKBACK일 신고가
  - 바닥 = 박스 내부 저가(low)의 최저
  - 돌파/이탈 판정 = 종가(close)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd

# 상태 상수
SEARCHING = "SEARCHING"
FORMING = "FORMING"
CONFIRMED = "CONFIRMED"
BREAKOUT_TODAY = "BREAKOUT_TODAY"
BREAKOUT_GAP = "BREAKOUT_GAP"
BROKEN_DOWN = "BROKEN_DOWN"
STALE = "STALE"

TERMINAL_STATES = (BREAKOUT_TODAY, BREAKOUT_GAP, BROKEN_DOWN, STALE)


@dataclass
class BoxParams:
    box_high_lookback: int = 20
    box_high_confirm: int = 3
    box_height_max_pct: float = 12.0
    box_valid_min_days: int = 15
    box_stale_days: int = 60
    breakout_buffer: float = 0.003
    breakout_volume_mult: float = 1.5
    volume_avg_window: int = 50


@dataclass
class BoxState:
    state: str
    top: float | None = None
    bottom: float | None = None
    top_idx: int | None = None
    bottom_idx: int | None = None
    form_start_idx: int | None = None
    confirm_idx: int | None = None
    days_in_box: int = 0
    volume_ratio: float | None = None

    @property
    def is_breakout(self) -> bool:
        return self.state in (BREAKOUT_TODAY, BREAKOUT_GAP)

    @property
    def is_gap(self) -> bool:
        return self.state == BREAKOUT_GAP


# ============================================================
# Core: compute box states across a bar series
# ============================================================

def compute_box_states(bars: pd.DataFrame, params: BoxParams | None = None) -> pd.DataFrame:
    """각 봉의 박스 상태를 컬럼으로 부착한 DataFrame.

    필요 컬럼: high, low, close, volume (open은 갭 라벨링에 사용, 없으면 갭 판정 skip).
    bars는 date 순으로 정렬되어 있어야 함.

    출력 컬럼 추가: box_state, box_top, box_bottom, box_top_idx, box_bottom_idx,
                   box_form_start_idx, box_confirm_idx, box_days_in_box, box_volume_ratio.
    """
    p = params or BoxParams()
    out = bars.copy().reset_index(drop=True)
    n = len(out)
    if n == 0:
        for c in ("box_state", "box_top", "box_bottom", "box_top_idx", "box_bottom_idx",
                  "box_form_start_idx", "box_confirm_idx", "box_days_in_box", "box_volume_ratio"):
            out[c] = pd.Series(dtype=object)
        return out

    high = out["high"].astype(float).to_numpy()
    low = out["low"].astype(float).to_numpy()
    close = out["close"].astype(float).to_numpy()
    open_ = out["open"].astype(float).to_numpy() if "open" in out.columns else close.copy()
    volume = out["volume"].astype(float).to_numpy() if "volume" in out.columns else np.zeros(n)

    avg_vol = pd.Series(volume).rolling(window=p.volume_avg_window, min_periods=p.volume_avg_window).mean().to_numpy()

    # 결과 버퍼
    states = np.empty(n, dtype=object)
    tops = np.full(n, np.nan)
    bottoms = np.full(n, np.nan)
    top_idxs = np.full(n, -1, dtype=int)
    bottom_idxs = np.full(n, -1, dtype=int)
    form_start_idxs = np.full(n, -1, dtype=int)
    confirm_idxs = np.full(n, -1, dtype=int)
    days_in_box = np.zeros(n, dtype=int)
    volume_ratios = np.full(n, np.nan)

    # 활성 박스 상태
    state = SEARCHING
    top: float | None = None
    bottom: float | None = None
    top_idx = -1
    bottom_idx = -1
    form_start_idx = -1
    confirm_idx = -1
    # SEARCHING 내부 천장 후보
    top_candidate: float | None = None
    top_candidate_idx = -1
    top_pending_days = 0

    for i in range(n):
        # ---------- 상태 전이 ----------
        if state == SEARCHING:
            # 천장 후보 발견/갱신
            if i >= p.box_high_lookback - 1:
                window_max = high[i - p.box_high_lookback + 1: i + 1].max()
                if high[i] >= window_max:
                    # 오늘이 lookback 내 신고가 → 후보 시작/갱신
                    if top_candidate is None or high[i] > top_candidate:
                        top_candidate = float(high[i])
                        top_candidate_idx = i
                        top_pending_days = 0
            # 후보가 있는 동안 확정 카운터 증가
            if top_candidate is not None and i > top_candidate_idx:
                if high[i] > top_candidate:
                    top_candidate = float(high[i])
                    top_candidate_idx = i
                    top_pending_days = 0
                else:
                    top_pending_days = i - top_candidate_idx
                    if top_pending_days >= p.box_high_confirm:
                        # 천장 확정 → FORMING
                        top = top_candidate
                        top_idx = top_candidate_idx
                        # 천장 확정 직후 바닥 추적 시작 — 천장일 이후 최저값으로 초기화
                        # 천장일+1 ~ i 까지의 최저 low (천장 형성 후 되돌림)
                        window_lows = low[top_idx + 1: i + 1]
                        bottom = float(window_lows.min()) if window_lows.size > 0 else float(low[i])
                        bottom_idx = top_idx + 1 + int(window_lows.argmin()) if window_lows.size > 0 else i
                        form_start_idx = top_idx
                        state = FORMING
                        top_candidate = None
                        top_candidate_idx = -1
                        top_pending_days = 0

        if state == FORMING:
            h_i, l_i, c_i = high[i], low[i], close[i]
            # 1) 박스 내부 갱신 시도
            # 신고가 초과? — 박스 무효, 천장 후보 다시 시작
            if h_i > top:
                _reset_to_searching = True
            # 천장-12% 한도 깸? — 박스 무효
            elif l_i < top * (1.0 - p.box_height_max_pct / 100.0):
                _reset_to_searching = True
            else:
                _reset_to_searching = False
                # 바닥 갱신
                if l_i < bottom:
                    bottom = float(l_i)
                    bottom_idx = i
                # 박스 유효일수 누적
                days = i - form_start_idx
                if days >= p.box_valid_min_days:
                    state = CONFIRMED
                    confirm_idx = i

            if _reset_to_searching:
                state = SEARCHING
                top = bottom = None
                top_idx = bottom_idx = form_start_idx = -1
                # 천장 후보 발견 — 박스 무효 후 오늘 신고가로 즉시 후보 시작
                if i >= p.box_high_lookback - 1:
                    window_max = high[i - p.box_high_lookback + 1: i + 1].max()
                    if high[i] >= window_max:
                        top_candidate = float(high[i])
                        top_candidate_idx = i
                        top_pending_days = 0

        # CONFIRMED 분기는 *별도 elif* (위에서 FORMING→CONFIRMED 전환된 경우, 같은 봉에서 돌파 판정 안 함)
        if state == CONFIRMED and confirm_idx != i:
            # confirm_idx == i 인 봉은 막 CONFIRMED 된 봉이므로 같은 봉에서 돌파 판정 X
            h_i, l_i, c_i, o_i = high[i], low[i], close[i], open_[i]
            breakout_thr = top * (1.0 + p.breakout_buffer)
            vr = float(volume[i] / avg_vol[i]) if (np.isfinite(avg_vol[i]) and avg_vol[i] > 0) else float("nan")

            if c_i > breakout_thr and np.isfinite(vr) and vr >= p.breakout_volume_mult:
                # 돌파
                if o_i > breakout_thr:
                    state = BREAKOUT_GAP
                else:
                    state = BREAKOUT_TODAY
                volume_ratios[i] = vr
            elif c_i < bottom:
                state = BROKEN_DOWN
            elif (i - confirm_idx) >= p.box_stale_days:
                state = STALE
            else:
                # 박스 내부 — 바닥 갱신 가능 (CONFIRMED 동안에도 트레일링)
                if l_i < bottom:
                    bottom = float(l_i)
                    bottom_idx = i

        # ---------- 기록 ----------
        states[i] = state
        tops[i] = top if top is not None else np.nan
        bottoms[i] = bottom if bottom is not None else np.nan
        top_idxs[i] = top_idx
        bottom_idxs[i] = bottom_idx
        form_start_idxs[i] = form_start_idx
        confirm_idxs[i] = confirm_idx
        if state in (FORMING, CONFIRMED) and form_start_idx >= 0:
            days_in_box[i] = i - form_start_idx
        elif state in (BREAKOUT_TODAY, BREAKOUT_GAP, BROKEN_DOWN, STALE) and form_start_idx >= 0:
            days_in_box[i] = i - form_start_idx
        if np.isnan(volume_ratios[i]) and state == CONFIRMED and np.isfinite(avg_vol[i]) and avg_vol[i] > 0:
            volume_ratios[i] = float(volume[i] / avg_vol[i])

        # ---------- 터미널 이후 리셋 ----------
        if state in TERMINAL_STATES:
            # 이 봉의 기록은 끝났고, 다음 봉부터 SEARCHING
            state = SEARCHING
            top = bottom = None
            top_idx = bottom_idx = form_start_idx = confirm_idx = -1
            top_candidate = None
            top_candidate_idx = -1
            top_pending_days = 0

    out["box_state"] = states
    out["box_top"] = tops
    out["box_bottom"] = bottoms
    out["box_top_idx"] = top_idxs
    out["box_bottom_idx"] = bottom_idxs
    out["box_form_start_idx"] = form_start_idxs
    out["box_confirm_idx"] = confirm_idxs
    out["box_days_in_box"] = days_in_box
    out["box_volume_ratio"] = volume_ratios
    return out


def current_box(bars: pd.DataFrame, params: BoxParams | None = None) -> BoxState:
    """마지막 봉의 박스 상태. 시그널 판정용 편의 함수."""
    df = compute_box_states(bars, params)
    if df.empty:
        return BoxState(state=SEARCHING)
    last = df.iloc[-1]
    return BoxState(
        state=str(last["box_state"]),
        top=None if pd.isna(last["box_top"]) else float(last["box_top"]),
        bottom=None if pd.isna(last["box_bottom"]) else float(last["box_bottom"]),
        top_idx=int(last["box_top_idx"]) if last["box_top_idx"] >= 0 else None,
        bottom_idx=int(last["box_bottom_idx"]) if last["box_bottom_idx"] >= 0 else None,
        form_start_idx=int(last["box_form_start_idx"]) if last["box_form_start_idx"] >= 0 else None,
        confirm_idx=int(last["box_confirm_idx"]) if last["box_confirm_idx"] >= 0 else None,
        days_in_box=int(last["box_days_in_box"]),
        volume_ratio=None if pd.isna(last["box_volume_ratio"]) else float(last["box_volume_ratio"]),
    )
