"""moneygold 대시보드.

실행:
    streamlit run src/moneygold/app/streamlit_app.py

ARCHITECTURE.md §0의 "종목 추천기" 목적을 시각화. 사용자가 워치리스트를 보고,
종목을 클릭해 차트·8조건·박스 상태를 한 화면에서 검토.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# 직접 실행 시 src/ 경로 보강
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from moneygold import darvas as dv  # noqa: E402
from moneygold import indicators as ind  # noqa: E402
from moneygold import signals as sg  # noqa: E402
from moneygold import stage as stg  # noqa: E402
from moneygold import template as tmpl  # noqa: E402
from moneygold.app.charts import build_detail_chart, build_rs_distribution  # noqa: E402
from moneygold.config import load_config  # noqa: E402
from moneygold.data import store  # noqa: E402

st.set_page_config(page_title="moneygold — 종목 추천 대시보드", layout="wide")


# ---------- cached loaders ----------

@st.cache_data(show_spinner=False)
def _load_master(data_dir_str: str) -> pd.DataFrame:
    df = store.read_parquet_safe(store.master_path(Path(data_dir_str)))
    return df if df is not None else pd.DataFrame()


@st.cache_data(show_spinner=False)
def _load_bars(data_dir_str: str, ticker: str) -> pd.DataFrame:
    df = store.read_parquet_safe(store.bars_path(Path(data_dir_str), ticker))
    return df if df is not None else pd.DataFrame()


@st.cache_data(show_spinner=False)
def _load_index_close(data_dir_str: str, code: str) -> pd.Series:
    df = store.read_parquet_safe(store.index_path(Path(data_dir_str), code))
    if df is None or df.empty:
        return pd.Series(dtype=float)
    return df.set_index("date")["close"].astype(float)


@st.cache_data(show_spinner="시그널 생성 중 (~30초)…")
def _run_signals(_data_dir_str: str, asof: str) -> dict:
    """signals.generate_signals 실행. dict로 캐시 가능하게 직렬화."""
    cfg = load_config()
    data_dir = Path(_data_dir_str)
    master = _load_master(_data_dir_str)
    if master.empty:
        return {}

    idx_kospi = _load_index_close(_data_dir_str, "KOSPI200")
    idx_kosdaq = _load_index_close(_data_dir_str, "KOSDAQ150")
    if idx_kospi.empty or idx_kosdaq.empty:
        return {}
    idx_kospi = idx_kospi[idx_kospi.index <= asof]
    idx_kosdaq = idx_kosdaq[idx_kosdaq.index <= asof]
    indices = {"KOSPI": idx_kospi, "KOSDAQ": idx_kosdaq}

    has_real_mcap = "mcap" in master.columns
    mcap_map = dict(zip(master["ticker"], master["mcap"])) if has_real_mcap else {}

    # TickerData 구성
    tickers = []
    rs_rows = []
    for row in master.itertuples(index=False):
        bars = _load_bars(_data_dir_str, row.ticker)
        if bars.empty:
            continue
        bars = bars[bars["date"] <= asof].sort_values("date").reset_index(drop=True)
        if len(bars) < 253:
            continue
        if has_real_mcap and mcap_map.get(row.ticker, 0) > 0:
            mcap = float(mcap_map[row.ticker])
        else:
            avg_value_20 = float(bars["value"].tail(20).mean()) if "value" in bars.columns else 0.0
            mcap = avg_value_20 * 50
        tickers.append(sg.TickerData(
            ticker=row.ticker, name=row.name, market=row.market,
            bars=bars, mcap=mcap, flagged=False,
        ))
        rs_rows.append({"ticker": row.ticker, "market": row.market,
                        "rs_mom": ind.rs_momentum(bars["close"].astype(float))})

    rs_df = pd.DataFrame(rs_rows)
    rs_df["rs_rank"] = float("nan")
    for mkt, g in rs_df.groupby("market"):
        rs_df.loc[g.index, "rs_rank"] = ind.rs_rank(g["rs_mom"]).values
    rs_rank_map = dict(zip(rs_df["ticker"], rs_df["rs_rank"]))
    rs_mom_map = dict(zip(rs_df["ticker"], rs_df["rs_mom"]))

    sigs = sg.generate_signals(
        asof, tickers, {}, rs_rank_map, indices, cfg, rs_momentum_map=rs_mom_map,
    )
    return sg.to_dict(sigs)


# ---------- UI ----------

cfg = load_config()
data_dir_str = str(cfg.data_dir)
master = _load_master(data_dir_str)

if master.empty:
    st.error("마스터가 비어있습니다. 먼저 데이터 동기화 필요:\n\n"
             "```bash\npython -m moneygold.cli.sync\n```")
    st.stop()

# ---------- 사이드바 ----------
with st.sidebar:
    st.title("⚙️ 필터")

    # asof
    latest_master_date = datetime.now().strftime("%Y%m%d")
    asof_str = st.text_input("기준일 (YYYYMMDD)", value=latest_master_date, max_chars=8)

    st.divider()
    markets = st.multiselect(
        "시장",
        sorted(master["market"].unique()),
        default=sorted(master["market"].unique()),
    )
    min_rs = st.slider("RS rank 최소", 0, 100, 70, step=1)
    box_states = st.multiselect(
        "박스 상태",
        ["SEARCHING", "FORMING", "CONFIRMED", "BREAKOUT_TODAY", "BREAKOUT_GAP"],
        default=["SEARCHING", "FORMING", "CONFIRMED", "BREAKOUT_TODAY", "BREAKOUT_GAP"],
    )

    if "sector" in master.columns:
        sector_options = sorted(s for s in master["sector"].dropna().unique() if s)
        sectors = st.multiselect("업종", sector_options, default=sector_options)
    else:
        sectors = None

    if "mcap" in master.columns:
        # 단위: 조원
        max_mcap_trillion = max(1, int((master["mcap"].max() / 1e12) + 1))
        mcap_range_trillion = st.slider(
            "시가총액 범위 (조원)",
            min_value=0.0, max_value=float(max_mcap_trillion),
            value=(0.0, float(max_mcap_trillion)), step=0.05,
        )
        mcap_min_krw = mcap_range_trillion[0] * 1e12
        mcap_max_krw = mcap_range_trillion[1] * 1e12
    else:
        mcap_min_krw = mcap_max_krw = None

    st.divider()
    top_n = st.number_input("워치리스트 표시 개수", min_value=5, max_value=2000, value=30, step=5,
                             help="전체 후보 풀까지 표시하려면 큰 값 (예: 2000)")

    st.divider()
    if st.button("🔄 시그널 재계산 (캐시 초기화)"):
        _run_signals.clear()
        _load_master.clear()
        _load_bars.clear()
        st.rerun()

# ---------- 시그널 생성 ----------
sigs_dict = _run_signals(data_dir_str, asof_str)
if not sigs_dict:
    st.error("시그널 생성 실패. 데이터 또는 지수가 부족합니다.")
    st.stop()

watchlist_df = pd.DataFrame(sigs_dict.get("watchlist", []))
new_buys_df = pd.DataFrame(sigs_dict.get("new_buys", []))

# ---------- 상단: 요약 카드 ----------
st.title("📊 moneygold — 종목 추천 대시보드")
st.caption(
    f"asof **{asof_str}** · Weinstein Stage 2 + Minervini Trend Template 8/8 + Darvas Box"
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("후보 풀 (Stage2 + Template)", len(watchlist_df))
c2.metric("⭐ 박스 돌파", len(new_buys_df))
if not watchlist_df.empty:
    c3.metric("RS rank 평균", f"{watchlist_df['rs_rank'].mean():.1f}")
    c4.metric("rs_mom 최대", f"{watchlist_df['rs_momentum'].max():+.2f}")

st.divider()

# master에서 sector/mcap을 워치리스트에 join
if not watchlist_df.empty and {"sector", "mcap"}.issubset(master.columns):
    watchlist_df = watchlist_df.merge(
        master[["ticker", "sector", "mcap"]], on="ticker", how="left",
    )

# ---------- 필터 적용 ----------
if not watchlist_df.empty:
    flt = watchlist_df.copy()
    if markets:
        flt = flt[flt["market"].isin(markets)]
    flt = flt[flt["rs_rank"] >= min_rs]
    if box_states:
        flt = flt[flt["box_state"].isin(box_states)]
    if sectors is not None and "sector" in flt.columns:
        flt = flt[flt["sector"].isin(sectors)]
    if mcap_min_krw is not None and "mcap" in flt.columns:
        flt = flt[(flt["mcap"] >= mcap_min_krw) & (flt["mcap"] <= mcap_max_krw)]
    flt = flt.sort_values("rs_rank", ascending=False).reset_index(drop=True)
else:
    flt = watchlist_df

# ---------- 메인 2단: 좌측 워치리스트 / 우측 차트 ----------
left, right = st.columns([0.42, 0.58])

with left:
    st.subheader(f"BUY 후보 풀 ({len(flt)}건 필터링)")

    if flt.empty:
        st.info("필터 조건에 맞는 종목이 없습니다.")
        selected_ticker = None
    else:
        base_cols = ["ticker", "name", "market"]
        extra_cols = []
        if "sector" in flt.columns: extra_cols.append("sector")
        if "mcap" in flt.columns: extra_cols.append("mcap")
        base_cols.extend(extra_cols)
        base_cols.extend(["rs_rank", "rs_momentum", "close", "box_state",
                          "days_in_box", "suggested_stop"])
        disp = flt[base_cols].head(top_n).copy()
        disp["rs_rank"] = disp["rs_rank"].round(1)
        disp["rs_momentum"] = disp["rs_momentum"].round(2)
        if "mcap" in disp.columns:
            disp["mcap_trillion"] = (disp["mcap"] / 1e12).round(3)
            disp = disp.drop(columns=["mcap"])

        # 컬럼 라벨/포맷
        col_cfg = {
            "ticker": "종목", "name": "이름", "market": "시장",
            "rs_rank": st.column_config.NumberColumn("RS", format="%.1f"),
            "rs_momentum": st.column_config.NumberColumn("rs_mom", format="%+.2f"),
            "close": st.column_config.NumberColumn("종가", format="%,d"),
            "box_state": "박스",
            "days_in_box": st.column_config.NumberColumn("box일", format="%d"),
            "suggested_stop": st.column_config.NumberColumn("stop hint", format="%,d"),
        }
        if "sector" in disp.columns:
            col_cfg["sector"] = "업종"
        if "mcap_trillion" in disp.columns:
            col_cfg["mcap_trillion"] = st.column_config.NumberColumn("시총(조)", format="%.2f")

        evt = st.dataframe(
            disp, use_container_width=True, hide_index=True, height=620,
            on_select="rerun", selection_mode="single-row",
            column_config=col_cfg,
        )
        sel = evt.selection.rows if evt and evt.selection else []
        selected_ticker = disp.iloc[sel[0]]["ticker"] if sel else (disp.iloc[0]["ticker"] if not disp.empty else None)

with right:
    if selected_ticker:
        row = master[master["ticker"] == selected_ticker]
        sel_name = row.iloc[0]["name"] if not row.empty else "?"
        sel_market = row.iloc[0]["market"] if not row.empty else "?"
        st.subheader(f"{selected_ticker} {sel_name} ({sel_market})")

        bars = _load_bars(data_dir_str, selected_ticker)
        if not bars.empty:
            bars = bars[bars["date"] <= asof_str].sort_values("date").reset_index(drop=True)

            # 차트
            stage_params = stg.StageParams(
                ma_length=cfg.strategy.stage_ma_length,
                slope_lookback=cfg.strategy.stage_slope_lookback,
                slope_threshold_pct=cfg.strategy.stage_slope_threshold_pct,
                band_pct=cfg.strategy.stage_band_pct,
                ma_type=cfg.strategy.stage_ma_type,
            )
            box_params = dv.BoxParams(
                box_high_lookback=cfg.strategy.box_high_lookback,
                box_high_confirm=cfg.strategy.box_high_confirm,
                box_height_max_pct=cfg.strategy.box_height_max_pct,
                box_valid_min_days=cfg.strategy.box_valid_min_days,
                box_stale_days=cfg.strategy.box_stale_days,
                breakout_buffer=cfg.strategy.breakout_buffer,
                breakout_volume_mult=cfg.strategy.breakout_volume_mult,
            )

            tail = st.slider("차트 기간 (영업일)", min_value=60, max_value=486, value=252, step=10,
                             key=f"tail_{selected_ticker}")
            fig = build_detail_chart(
                bars, name=f"{selected_ticker} {sel_name}",
                tail=tail, stage_params=stage_params, box_params=box_params,
            )
            st.plotly_chart(fig, use_container_width=True)

            # 미네비니 8조건 패널
            wl_row = flt[flt["ticker"] == selected_ticker]
            if not wl_row.empty:
                wl = wl_row.iloc[0]
                with st.expander("미네비니 8조건 + 진단 (재계산)", expanded=True):
                    idx_code = "KOSPI200" if sel_market == "KOSPI" else "KOSDAQ150"
                    rs_v = float(wl["rs_rank"])
                    t = tmpl.check_template(
                        bars["close"].astype(float), rs_v,
                        sma200_slope_lookback=cfg.strategy.sma200_slope_lookback,
                        rs_rank_min=float(cfg.strategy.rs_rank_min),
                        high_series=bars["high"].astype(float) if "high" in bars.columns else None,
                        low_series=bars["low"].astype(float) if "low" in bars.columns else None,
                    )
                    cond_labels = [
                        "1. close > sma150 & sma200",
                        "2. sma150 > sma200",
                        "3. sma200 > sma200[22] (1개월 우상향)",
                        "4. sma50 > sma150, sma200",
                        "5. close > sma50",
                        "6. close >= low_52w × 1.25 (저가)",
                        "7. close >= high_52w × 0.75 (고가)",
                        f"8. RS rank ≥ {cfg.strategy.rs_rank_min}",
                    ]
                    rows = [{"조건": lbl, "통과": "✅" if c else "❌"}
                            for lbl, c in zip(cond_labels, t.checks)]
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                    st.write(
                        f"**진입 가이드**: close `{wl['close']:,.0f}` · "
                        f"stop hint `{wl['suggested_stop']:,.0f}` · "
                        f"box `{wl['box_state']}` (top {wl['box_top']} / bottom {wl['box_bottom']})"
                    )
        else:
            st.warning("선택한 종목의 bars 데이터를 찾을 수 없습니다.")
    else:
        st.info("좌측 워치리스트에서 종목을 선택하면 차트가 나타납니다.")

# ---------- 하단: 박스 돌파 + RS 분포 ----------
st.divider()
b1, b2 = st.columns([0.5, 0.5])

with b1:
    st.subheader("⭐ 오늘 박스 돌파")
    if new_buys_df.empty:
        st.info("오늘 박스 돌파 종목 없음")
    else:
        nb_disp = new_buys_df[["ticker", "name", "market", "rs_rank", "entry_guide",
                                "stop", "box_top", "box_bottom", "days_in_box",
                                "volume_ratio", "is_gap_breakout"]].copy()
        nb_disp["rs_rank"] = nb_disp["rs_rank"].round(1)
        nb_disp["volume_ratio"] = nb_disp["volume_ratio"].round(2)
        st.dataframe(nb_disp, use_container_width=True, hide_index=True)

with b2:
    st.subheader("RS rank 분포 (워치리스트)")
    if not watchlist_df.empty:
        st.plotly_chart(
            build_rs_distribution(watchlist_df["rs_rank"]),
            use_container_width=True,
        )

st.caption(
    "⚠️ 이 대시보드는 종목 *추천*만 합니다. 진입 가격·시점·수량은 사용자가 차트를 보고 직접 결정. "
    "백테스트 결과는 자동매매 가정 하의 *상한선* 추정으로 실제 결과와 다릅니다."
)
