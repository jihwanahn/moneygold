"""Weinstein Stage Analysis 4단계 분류기.

상태 정의:
  1 = basing / undefined (fall-through)
  2 = advancing (matter of timing for entry)
  3 = topping / distribution
  4 = declining

판정 입력:
  - close              : 현재 종가
  - sma_30w            : 30주(=일봉 150) SMA
  - sma_30w_slope      : sma_30w의 50영업일(=10주) 정규화 기울기 (slope_normalized 결과)
  - rs_line_slope      : RS line(stock/index)의 50영업일 정규화 기울기

ARCHITECTURE.md §4. 강화 신호 (외인/기관 누적)는 PR3에서 BUY 게이트에 추가.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind


# 상태 코드
STAGE_UNKNOWN = 0     # NaN 등으로 판정 불가
STAGE_BASING = 1
STAGE_ADVANCING = 2
STAGE_TOPPING = 3
STAGE_DECLINING = 4

STAGE_NAMES = {
    0: "UNKNOWN",
    1: "BASING",
    2: "ADVANCING",
    3: "TOPPING",
    4: "DECLINING",
}


def classify_stage(
    close: float,
    sma_30w: float,
    sma_30w_slope: float,
    rs_line_slope: float,
) -> int:
    """단일 시점 Stage 라벨. 입력 NaN이 하나라도 있으면 UNKNOWN(0)."""
    if any(pd.isna(x) for x in (close, sma_30w, sma_30w_slope, rs_line_slope)):
        return STAGE_UNKNOWN

    above = close > sma_30w
    ma_up = sma_30w_slope > 0
    ma_down = sma_30w_slope < 0
    rs_up = rs_line_slope > 0
    rs_down = rs_line_slope < 0

    if above and ma_up and rs_up:
        return STAGE_ADVANCING
    if above and (not ma_up) and rs_down:
        return STAGE_TOPPING
    if (not above) and ma_down and rs_down:
        return STAGE_DECLINING
    return STAGE_BASING


def classify_stage_series(
    close: pd.Series,
    sma_30w: pd.Series,
    sma_30w_slope: pd.Series,
    rs_line_slope: pd.Series,
) -> pd.Series:
    """시계열 버전. 일자별 Stage 라벨.

    모든 입력 시리즈는 같은 인덱스. 출력은 같은 인덱스의 int8 Series.
    """
    df = pd.concat(
        [
            close.rename("close"),
            sma_30w.rename("sma"),
            sma_30w_slope.rename("ma_slope"),
            rs_line_slope.rename("rs_slope"),
        ],
        axis=1,
    )

    out = pd.Series(STAGE_UNKNOWN, index=df.index, dtype="int8")
    has_all = df.notna().all(axis=1)

    above = df["close"] > df["sma"]
    ma_up = df["ma_slope"] > 0
    ma_down = df["ma_slope"] < 0
    rs_up = df["rs_slope"] > 0
    rs_down = df["rs_slope"] < 0

    is_advancing = has_all & above & ma_up & rs_up
    is_topping = has_all & above & (~ma_up) & rs_down
    is_declining = has_all & (~above) & ma_down & rs_down

    # default for has_all but no other branch = BASING
    out.loc[has_all] = STAGE_BASING
    out.loc[is_advancing] = STAGE_ADVANCING
    out.loc[is_topping] = STAGE_TOPPING
    out.loc[is_declining] = STAGE_DECLINING
    return out


def stage_since(stage_series: pd.Series, target: int = STAGE_ADVANCING) -> pd.Timestamp | None:
    """가장 최근의 *연속* target Stage 구간이 언제 시작됐는지.

    Stage 2가 BUY 게이트라 "Stage 2 진입 이후 며칠 됐나"를 시그널에 첨부할 때 사용.
    target Stage가 마지막 시점에 활성이 아니면 None.
    """
    if stage_series.empty or stage_series.iloc[-1] != target:
        return None
    # 끝에서부터 거꾸로 같은 stage가 유지되는 첫 시점
    for ts, v in zip(stage_series.index[::-1], stage_series.values[::-1]):
        if v != target:
            # 직전까지가 target이 유지된 마지막. 한 칸 앞으로.
            break
        last_match = ts
    return last_match


# ============================================================
# Convenience: bars + index DataFrame → Stage series
# ============================================================

def compute_stage_for_ticker(
    bars: pd.DataFrame,
    index_close: pd.Series,
    *,
    sma_window: int = 150,
    slope_lookback: int = 50,
) -> pd.DataFrame:
    """한 종목의 bars + 지수 close → Stage + 주요 지표 부착된 DataFrame.

    Parameters
    ----------
    bars : DataFrame  columns ['date', 'close', ...], date는 정렬된 YYYYMMDD 문자열
    index_close : Series  index=date(YYYYMMDD str), values=지수 종가
    sma_window : 30주(=150 일봉) SMA 윈도우
    slope_lookback : 50영업일 정규화 기울기 lookback

    Returns
    -------
    DataFrame copy of `bars` plus columns:
      sma_30w, sma_30w_slope, rs_line, rs_line_slope, stage
    """
    if bars.empty:
        return bars.copy()

    work = bars.copy()
    work = work.sort_values("date").reset_index(drop=True)

    close = work["close"].astype(float)
    work["sma_30w"] = ind.sma(close, sma_window)
    work["sma_30w_slope"] = ind.slope_normalized(work["sma_30w"], slope_lookback)

    # RS line: 날짜 인덱스 정렬 후 계산
    s_by_date = close.copy()
    s_by_date.index = work["date"]
    rs = ind.rs_line(s_by_date, index_close)
    rs_slope = ind.slope_normalized(rs, slope_lookback)

    # 다시 정수 인덱스로 매핑
    rs_aligned = rs.reindex(work["date"]).reset_index(drop=True)
    rs_slope_aligned = rs_slope.reindex(work["date"]).reset_index(drop=True)
    work["rs_line"] = rs_aligned
    work["rs_line_slope"] = rs_slope_aligned

    work["stage"] = classify_stage_series(
        work["close"].astype(float),
        work["sma_30w"],
        work["sma_30w_slope"],
        work["rs_line_slope"],
    )
    return work
