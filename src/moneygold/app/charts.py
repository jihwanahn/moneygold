"""Plotly chart builders for the dashboard.

종목 상세 차트:
  - 캔들 + 거래량 (subplot)
  - SMA 50 / 150 / 200 오버레이
  - Darvas 박스 천장/바닥 가로선 + 박스 영역 음영
  - Stage 배경색 (Stage 2 녹색, Stage 3 주황, Stage 4 빨강, Stage 1 회색)
  - Minervini Template 통과/실패 시점 마커 (선택)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .. import darvas as dv
from .. import indicators as ind
from .. import stage as stg


STAGE_COLORS = {
    stg.STAGE_BASING: "rgba(150, 150, 150, 0.12)",
    stg.STAGE_ADVANCING: "rgba(46, 204, 113, 0.12)",
    stg.STAGE_TOPPING: "rgba(230, 126, 34, 0.12)",
    stg.STAGE_DECLINING: "rgba(231, 76, 60, 0.12)",
}


def build_detail_chart(
    bars: pd.DataFrame,
    *,
    name: str = "",
    tail: int = 252,
    stage_params: stg.StageParams | None = None,
    box_params: dv.BoxParams | None = None,
    show_stage_bg: bool = True,
) -> go.Figure:
    """종목 상세 차트.

    bars: date 오름차순 정렬, columns=[date, open, high, low, close, volume, ...]
    tail: 마지막 N봉만 차트에 그림 (전체 인디케이터는 전 구간으로 계산)
    """
    if bars is None or bars.empty:
        return _empty_fig("데이터 없음")

    df = bars.sort_values("date").reset_index(drop=True).copy()
    close = df["close"].astype(float)

    # 인디케이터
    sma50 = ind.sma(close, 50)
    sma150 = ind.sma(close, 150)
    sma200 = ind.sma(close, 200)

    # Stage 시계열
    stage_series = stg.classify_stage_series(close, stage_params)

    # Darvas 박스 시계열
    box_df = dv.compute_box_states(df, box_params)

    # tail 자르기 (인덱스 보존)
    if tail and len(df) > tail:
        sl = slice(len(df) - tail, len(df))
        df = df.iloc[sl].reset_index(drop=True)
        sma50 = sma50.iloc[sl].reset_index(drop=True)
        sma150 = sma150.iloc[sl].reset_index(drop=True)
        sma200 = sma200.iloc[sl].reset_index(drop=True)
        stage_series = stage_series.iloc[sl].reset_index(drop=True)
        box_df = box_df.iloc[sl].reset_index(drop=True)

    dates = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.78, 0.22],
        vertical_spacing=0.02,
        subplot_titles=(name or "Price", "Volume"),
    )

    # ---------- 캔들 ----------
    fig.add_trace(
        go.Candlestick(
            x=dates, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            name="OHLC",
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
            showlegend=False,
        ),
        row=1, col=1,
    )

    # ---------- SMA ----------
    for series, color, label in [
        (sma50, "#42a5f5", "SMA 50"),
        (sma150, "#ffa726", "SMA 150"),
        (sma200, "#ab47bc", "SMA 200"),
    ]:
        fig.add_trace(
            go.Scatter(x=dates, y=series, line=dict(color=color, width=1.4),
                       name=label, hovertemplate="%{y:,.0f}"),
            row=1, col=1,
        )

    # ---------- Stage 배경 ----------
    if show_stage_bg and not stage_series.empty:
        runs = _runs(stage_series.values)
        for st_val, start, end in runs:
            if st_val == 0: continue
            fig.add_vrect(
                x0=dates.iloc[start], x1=dates.iloc[min(end, len(dates) - 1)],
                fillcolor=STAGE_COLORS.get(int(st_val), "rgba(0,0,0,0)"),
                opacity=1.0, line_width=0, layer="below",
                row=1, col=1,
            )

    # ---------- 박스 영역 (CONFIRMED + BREAKOUT 구간만 음영) ----------
    if "box_state" in box_df.columns:
        # 최근 박스 천장/바닥 가로선 (현재 형성된 박스만)
        last_state = str(box_df["box_state"].iloc[-1])
        last_top = box_df["box_top"].iloc[-1]
        last_bottom = box_df["box_bottom"].iloc[-1]
        if last_state in (dv.FORMING, dv.CONFIRMED) and pd.notna(last_top) and pd.notna(last_bottom):
            fig.add_hline(y=float(last_top), line=dict(color="#ef5350", dash="dash", width=1),
                          annotation_text=f"box top {last_top:,.0f}", annotation_position="top right",
                          row=1, col=1)
            fig.add_hline(y=float(last_bottom), line=dict(color="#26a69a", dash="dash", width=1),
                          annotation_text=f"box bottom {last_bottom:,.0f}", annotation_position="bottom right",
                          row=1, col=1)

        # BREAKOUT 봉 마커
        bo_idx = box_df.index[box_df["box_state"].isin([dv.BREAKOUT_TODAY, dv.BREAKOUT_GAP])]
        if len(bo_idx) > 0:
            fig.add_trace(
                go.Scatter(
                    x=dates.iloc[bo_idx], y=df["high"].iloc[bo_idx] * 1.02,
                    mode="markers", marker=dict(symbol="star", size=12, color="#ff9800"),
                    name="Breakout", hovertemplate="박스 돌파<br>%{x|%Y-%m-%d}",
                ),
                row=1, col=1,
            )

    # ---------- 거래량 ----------
    vol_colors = [
        "#26a69a" if df["close"].iloc[i] >= df["open"].iloc[i] else "#ef5350"
        for i in range(len(df))
    ]
    fig.add_trace(
        go.Bar(x=dates, y=df["volume"], marker_color=vol_colors, showlegend=False, name="Volume"),
        row=2, col=1,
    )

    fig.update_layout(
        height=620,
        margin=dict(l=20, r=20, t=40, b=20),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", y=1.04, x=0),
        hovermode="x unified",
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(0,0,0,0.06)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(0,0,0,0.06)")
    return fig


def _empty_fig(msg: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=msg, xref="paper", yref="paper", x=0.5, y=0.5,
                       showarrow=False, font=dict(size=14, color="#888"))
    fig.update_layout(height=400, margin=dict(l=20, r=20, t=20, b=20))
    return fig


def _runs(arr) -> list[tuple[int, int, int]]:
    """(value, start_idx, end_idx) 연속 구간 추출."""
    out = []
    if len(arr) == 0:
        return out
    start = 0
    cur = arr[0]
    for i in range(1, len(arr)):
        if arr[i] != cur:
            out.append((int(cur), start, i - 1))
            cur = arr[i]
            start = i
    out.append((int(cur), start, len(arr) - 1))
    return out


def build_rs_distribution(rs_ranks: pd.Series) -> go.Figure:
    """전체 종목 RS rank 분포 히스토그램."""
    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=rs_ranks.dropna(), nbinsx=20, marker_color="#42a5f5",
        name="RS rank",
    ))
    fig.update_layout(
        title="RS rank distribution (전체 종목)",
        xaxis_title="RS rank", yaxis_title="종목 수",
        height=300, margin=dict(l=20, r=20, t=40, b=20),
        showlegend=False,
    )
    return fig
