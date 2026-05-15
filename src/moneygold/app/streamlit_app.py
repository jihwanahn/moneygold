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

from moneygold import consensus as cons  # noqa: E402
from moneygold import darvas as dv  # noqa: E402
from moneygold import fundamentals as fund  # noqa: E402
from moneygold import indicators as ind  # noqa: E402
from moneygold import signals as sg  # noqa: E402
from moneygold import stage as stg  # noqa: E402
from moneygold import template as tmpl  # noqa: E402
from moneygold.app import _glossary as g  # noqa: E402
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

    # 캐시된 펀더멘털 로드
    fundamentals_map: dict[str, fund.FundamentalsResult] = {}
    for td in tickers:
        p = fund.financials_path(data_dir, td.ticker)
        if p.exists():
            q = store.read_parquet_safe(p)
            if q is not None and not q.empty:
                fundamentals_map[td.ticker] = fund.build_fundamentals_from_cache(q)

    # 캐시된 컨센서스 로드
    consensus_map: dict[str, cons.ConsensusResult] = {}
    import json
    for td in tickers:
        p = cons.consensus_path(data_dir, td.ticker)
        if p.exists():
            try:
                consensus_map[td.ticker] = cons.from_dict(json.loads(p.read_text()))
            except Exception:
                pass

    sigs = sg.generate_signals(
        asof, tickers, {}, rs_rank_map, indices, cfg,
        rs_momentum_map=rs_mom_map,
        fundamentals_map=fundamentals_map,
        consensus_map=consensus_map,
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
    asof_str = st.text_input("기준일 (YYYYMMDD)", value=latest_master_date, max_chars=8,
                              help=g.TOOLTIP_ASOF)

    st.divider()
    markets = st.multiselect(
        "시장",
        sorted(master["market"].unique()),
        default=sorted(master["market"].unique()),
        help=g.TOOLTIP_MARKET,
    )
    min_rs = st.slider("RS rank 최소", 0, 100, 70, step=1, help=g.TOOLTIP_RS_MIN)
    box_states = st.multiselect(
        "박스 상태",
        ["SEARCHING", "FORMING", "CONFIRMED", "BREAKOUT_TODAY", "BREAKOUT_GAP"],
        default=["SEARCHING", "FORMING", "CONFIRMED", "BREAKOUT_TODAY", "BREAKOUT_GAP"],
        help=g.TOOLTIP_BOX_STATES,
    )

    if "sector" in master.columns:
        sector_options = sorted(s for s in master["sector"].dropna().unique() if s)
        sectors = st.multiselect("업종", sector_options, default=sector_options,
                                  help=g.TOOLTIP_SECTOR)
    else:
        sectors = None

    if "mcap" in master.columns:
        # 단위: 조원
        max_mcap_trillion = max(1, int((master["mcap"].max() / 1e12) + 1))
        mcap_range_trillion = st.slider(
            "시가총액 범위 (조원)",
            min_value=0.0, max_value=float(max_mcap_trillion),
            value=(0.0, float(max_mcap_trillion)), step=0.05,
            help=g.TOOLTIP_MCAP,
        )
        mcap_min_krw = mcap_range_trillion[0] * 1e12
        mcap_max_krw = mcap_range_trillion[1] * 1e12
    else:
        mcap_min_krw = mcap_max_krw = None

    st.divider()
    st.subheader("📊 펀더멘털 필터")
    min_revenue_yoy = st.slider(
        "매출 YoY 최소 (%)", min_value=-50, max_value=100, value=-50, step=5,
        help="가장 최근 분기 매출 YoY 성장률. 25% 이상이 미네비니 권장.",
    )
    min_op_margin = st.slider(
        "영업이익률 최소 (%)", min_value=-30, max_value=50, value=-30, step=1,
        help="가장 최근 분기 영업이익률 (영업이익 ÷ 매출).",
    )
    min_growth_q = st.slider(
        "연속 매출 성장 분기", min_value=0, max_value=12, value=0,
        help="매출 YoY > 0이 연속 N분기 이상. 4 이상이 좋은 신호.",
    )
    only_accelerating = st.checkbox(
        "가속 종목만 (YoY 가속)",
        help="최근 분기 YoY가 직전 분기 YoY보다 큰 종목 (모멘텀 가속).",
    )

    st.divider()
    st.subheader("🎯 컨센서스 필터")
    min_n_analysts = st.slider(
        "최소 애널리스트 수", 0, 40, 0,
        help="0 = 컨센서스 무관. 5+ 권장 (소수만 커버하는 종목은 신뢰도 낮음). yfinance 한국 종목 대형주만 풍부.",
    )
    min_upside_pct = st.slider(
        "목표가 대비 상승여력 최소 (%)", -50, 200, -50, step=5,
        help="(애널 평균 목표가 - 현재가) / 현재가. 양수 = 목표가 위에 있다는 뜻. NaN(데이터 없음)은 통과.",
    )
    min_eps_revision_0y_pct = st.slider(
        "이번 연도 EPS 추정 상향 폭 최소 (%)", -50, 100, -50, step=1,
        help="30일 전 추정 대비 *현재* 추정의 변화. 양수 = 애널들이 추정을 *상향 조정* 중. "
             "이게 양수면서 큰 종목 = CAN SLIM 'C' + 'N' (신추정고가). NaN은 통과.",
    )
    min_net_revisions = st.slider(
        "30일 순 상향 분석가 수 최소", -20, 30, -20,
        help="30일간 상향 조정한 분석가 수 - 하향 조정 수. 양수 = 더 많은 분석가가 추정을 올림.",
    )

    st.divider()
    top_n = st.number_input("워치리스트 표시 개수", min_value=5, max_value=2000, value=30, step=5,
                             help=g.TOOLTIP_TOP_N)

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

with st.expander("❓ 이 대시보드 사용법 (처음 사용자 필독)", expanded=False):
    st.markdown(g.INTRO_HELP)
    st.markdown("---")
    st.markdown(g.BOX_INTRO)
    st.markdown("---")
    st.markdown(g.CHART_LEGEND)
    st.markdown("---")
    st.markdown("**Weinstein 4단계 자세히**:")
    for code, (label, desc, color) in g.STAGE_DESC.items():
        st.markdown(f"- **Stage {code} {label}** ({color}) — {desc}")

c1, c2, c3, c4 = st.columns(4)
c1.metric(
    "후보 풀 (Stage2 + Template)", len(watchlist_df),
    help="Weinstein Stage 2 + Minervini 8조건 *모두* 통과한 종목 수.",
)
c2.metric(
    "⭐ 박스 돌파", len(new_buys_df),
    help="후보 풀 중 오늘 Darvas 박스 천장을 거래량 동반 돌파한 종목 수 (즉시 검토 대상).",
)
if not watchlist_df.empty:
    c3.metric(
        "RS rank 평균", f"{watchlist_df['rs_rank'].mean():.1f}",
        help="워치리스트 종목들의 RS rank 평균 (Template 조건 8에서 이미 70+ 필터됨).",
    )
    c4.metric(
        "rs_mom 최대", f"{watchlist_df['rs_momentum'].max():+.2f}",
        help="가장 강한 모멘텀 종목의 가중 평균 수익률. +5.0 = 가중 500%.",
    )

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
    # 펀더멘털 필터 (NaN은 통과 — 데이터 없는 종목은 거르지 않음)
    if "revenue_yoy" in flt.columns and min_revenue_yoy > -50:
        flt = flt[flt["revenue_yoy"].fillna(min_revenue_yoy) >= min_revenue_yoy]
    if "op_margin" in flt.columns and min_op_margin > -30:
        flt = flt[flt["op_margin"].fillna(min_op_margin) >= min_op_margin]
    if "growth_quarters" in flt.columns and min_growth_q > 0:
        flt = flt[flt["growth_quarters"].fillna(0) >= min_growth_q]
    if only_accelerating and "accelerating" in flt.columns:
        flt = flt[flt["accelerating"] == True]
    # 컨센서스 필터 (NaN/0 = 통과)
    if "cons_n_analysts" in flt.columns and min_n_analysts > 0:
        flt = flt[flt["cons_n_analysts"].fillna(0) >= min_n_analysts]
    if "cons_target_upside_pct" in flt.columns and min_upside_pct > -50:
        flt = flt[flt["cons_target_upside_pct"].fillna(min_upside_pct) >= min_upside_pct]
    if "cons_rev_eps_0y_30d_pct" in flt.columns and min_eps_revision_0y_pct > -50:
        flt = flt[flt["cons_rev_eps_0y_30d_pct"].fillna(min_eps_revision_0y_pct) >= min_eps_revision_0y_pct]
    if "cons_eps_net_revisions_30d" in flt.columns and min_net_revisions > -20:
        flt = flt[flt["cons_eps_net_revisions_30d"].fillna(0) >= min_net_revisions]
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
        if "sector" in flt.columns: base_cols.append("sector")
        if "mcap" in flt.columns: base_cols.append("mcap")
        base_cols.extend(["rs_rank", "rs_momentum", "close", "box_state",
                          "days_in_box", "suggested_stop"])
        # 펀더멘털 컬럼
        for c in ["revenue_yoy", "op_income_yoy", "op_margin",
                  "growth_quarters", "op_growth_quarters", "accelerating"]:
            if c in flt.columns:
                base_cols.append(c)
        # 컨센서스 컬럼 (정적 + 상향 조정)
        for c in ["cons_n_analysts", "cons_target_upside_pct", "cons_recommendation",
                  "cons_earnings_growth", "cons_last_surprise_pct",
                  "cons_rev_eps_0y_30d_pct", "cons_eps_net_revisions_30d"]:
            if c in flt.columns:
                base_cols.append(c)
        disp = flt[base_cols].head(top_n).copy()
        disp["rs_rank"] = disp["rs_rank"].round(1)
        disp["rs_momentum"] = disp["rs_momentum"].round(2)
        if "mcap" in disp.columns:
            disp["mcap_trillion"] = (disp["mcap"] / 1e12).round(3)
            disp = disp.drop(columns=["mcap"])
        for c in ["revenue_yoy", "op_income_yoy", "op_margin",
                  "cons_target_upside_pct", "cons_earnings_growth", "cons_last_surprise_pct",
                  "cons_rev_eps_0y_30d_pct"]:
            if c in disp.columns:
                disp[c] = disp[c].round(1)

        # 컬럼 라벨/포맷 + 툴팁
        col_cfg = {
            "ticker": st.column_config.TextColumn("종목", help=g.COL_TICKER),
            "name": st.column_config.TextColumn("이름", help=g.COL_NAME),
            "market": st.column_config.TextColumn("시장", help=g.COL_MARKET),
            "rs_rank": st.column_config.NumberColumn("RS", format="%.1f", help=g.COL_RS_RANK),
            "rs_momentum": st.column_config.NumberColumn("rs_mom", format="%+.2f", help=g.COL_RS_MOMENTUM),
            "close": st.column_config.NumberColumn("종가", format="%,d", help=g.COL_CLOSE),
            "box_state": st.column_config.TextColumn("박스", help=g.COL_BOX_STATE),
            "days_in_box": st.column_config.NumberColumn("box일", format="%d", help=g.COL_DAYS_IN_BOX),
            "suggested_stop": st.column_config.NumberColumn("stop hint", format="%,d", help=g.COL_SUGGESTED_STOP),
        }
        if "sector" in disp.columns:
            col_cfg["sector"] = st.column_config.TextColumn("업종", help=g.COL_SECTOR)
        if "mcap_trillion" in disp.columns:
            col_cfg["mcap_trillion"] = st.column_config.NumberColumn("시총(조)", format="%.2f", help=g.COL_MCAP_TRILLION)
        # 펀더멘털
        if "revenue_yoy" in disp.columns:
            col_cfg["revenue_yoy"] = st.column_config.NumberColumn(
                "매출YoY%", format="%+.1f",
                help="가장 최근 분기 매출 YoY 성장률. 25% 이상이 미네비니 권장. NaN = 데이터 없음.")
        if "op_income_yoy" in disp.columns:
            col_cfg["op_income_yoy"] = st.column_config.NumberColumn(
                "영익YoY%", format="%+.1f",
                help="가장 최근 분기 영업이익 YoY 성장률.")
        if "op_margin" in disp.columns:
            col_cfg["op_margin"] = st.column_config.NumberColumn(
                "영익률%", format="%.1f",
                help="가장 최근 분기 영업이익률 = 영업이익 ÷ 매출.")
        if "growth_quarters" in disp.columns:
            col_cfg["growth_quarters"] = st.column_config.NumberColumn(
                "연속매출↑", format="%d",
                help="매출 YoY > 0이 연속 N분기. 8 = 2년 연속 성장.")
        if "op_growth_quarters" in disp.columns:
            col_cfg["op_growth_quarters"] = st.column_config.NumberColumn(
                "연속영익↑", format="%d",
                help="영업이익 YoY > 0이 연속 N분기.")
        if "accelerating" in disp.columns:
            col_cfg["accelerating"] = st.column_config.CheckboxColumn(
                "가속",
                help="최근 분기 YoY > 직전 분기 YoY (매출 또는 영업이익). 가속하는 종목은 추세 강세.")
        # 컨센서스
        if "cons_n_analysts" in disp.columns:
            col_cfg["cons_n_analysts"] = st.column_config.NumberColumn(
                "애널수", format="%d",
                help="yfinance가 보유한 애널리스트 추정치 수. 5+ 신뢰도 OK. 한국 대형주는 20~38명.")
        if "cons_target_upside_pct" in disp.columns:
            col_cfg["cons_target_upside_pct"] = st.column_config.NumberColumn(
                "목표가↑%", format="%+.1f",
                help="(애널 평균 목표가 - 현재가) / 현재가. 양수 = 추가 상승여력 있다는 컨센서스.")
        if "cons_recommendation" in disp.columns:
            col_cfg["cons_recommendation"] = st.column_config.TextColumn(
                "추천",
                help="strong_buy / buy / hold / sell / strong_sell / none.")
        if "cons_earnings_growth" in disp.columns:
            col_cfg["cons_earnings_growth"] = st.column_config.NumberColumn(
                "예상 EPS↑%", format="%+.1f",
                help="향후 12개월 예상 EPS 성장률 (애널 평균).")
        if "cons_last_surprise_pct" in disp.columns:
            col_cfg["cons_last_surprise_pct"] = st.column_config.NumberColumn(
                "최근 서프라이즈%", format="%+.1f",
                help="가장 최근 분기 실적 - 컨센서스 예상 = 서프라이즈. 양수 = 어닝비트, 음수 = 어닝미스.")
        if "cons_rev_eps_0y_30d_pct" in disp.columns:
            col_cfg["cons_rev_eps_0y_30d_pct"] = st.column_config.NumberColumn(
                "EPS상향%(30d)", format="%+.1f",
                help="이번 연도 EPS 컨센서스 추정의 30일 전 대비 변화율. "
                     "+10% 이상 = 애널들이 실적을 크게 상향 조정 중 (강한 신호).")
        if "cons_eps_net_revisions_30d" in disp.columns:
            col_cfg["cons_eps_net_revisions_30d"] = st.column_config.NumberColumn(
                "순상향(30d)", format="%+d",
                help="30일간 상향 분석가 수 - 하향 수. 양수 = 더 많은 분석가가 추정을 올림.")

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

            tail = st.slider(
                "차트 기간 (영업일)", min_value=60, max_value=486, value=252, step=10,
                key=f"tail_{selected_ticker}",
                help="60 = 약 3개월, 252 = 1년, 486 = 2년 (백필 한도). 추세 길게 보려면 큰 값.",
            )
            fig = build_detail_chart(
                bars, name=f"{selected_ticker} {sel_name}",
                tail=tail, stage_params=stage_params, box_params=box_params,
            )
            st.plotly_chart(fig, use_container_width=True)

            # 미네비니 8조건 패널
            wl_row = flt[flt["ticker"] == selected_ticker]
            if not wl_row.empty:
                wl = wl_row.iloc[0]
                with st.expander("미네비니 8조건 + 진단 (마우스 오버로 의미 확인)", expanded=True):
                    st.caption(
                        "Mark Minervini의 *Trade Like a Stock Market Wizard*에서 추출한 "
                        "Trend Template. 8개 모두 통과한 종목만 BUY 후보로 인정."
                    )
                    rs_v = float(wl["rs_rank"])
                    t = tmpl.check_template(
                        bars["close"].astype(float), rs_v,
                        sma200_slope_lookback=cfg.strategy.sma200_slope_lookback,
                        rs_rank_min=float(cfg.strategy.rs_rank_min),
                        high_series=bars["high"].astype(float) if "high" in bars.columns else None,
                        low_series=bars["low"].astype(float) if "low" in bars.columns else None,
                    )
                    rows = [
                        {"조건": title, "의미": desc, "통과": "✅" if c else "❌"}
                        for (title, _ko, desc), c in zip(g.MINERVINI_CONDITIONS, t.checks)
                    ]
                    st.dataframe(
                        pd.DataFrame(rows), use_container_width=True, hide_index=True,
                        column_config={
                            "조건": st.column_config.TextColumn("조건", width="medium"),
                            "의미": st.column_config.TextColumn("의미 (한 줄)", width="large"),
                            "통과": st.column_config.TextColumn("통과", width="small"),
                        },
                    )

                    st.markdown(
                        f"**진입 가이드** — 종가 `{wl['close']:,.0f}` · "
                        f"권장 손절 `{wl['suggested_stop']:,.0f}` · "
                        f"박스 `{wl['box_state']}` (top {wl['box_top']} / bottom {wl['box_bottom']})"
                    )
                    st.caption(
                        "💡 권장 손절은 박스 바닥 또는 종가 -7% 중 하나. "
                        "실제 진입가·손절가는 차트 보고 본인이 결정."
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
    st.caption("Darvas 박스 천장 + 0.3% 위로 종가 돌파. 즉시 검토 대상.")
    if new_buys_df.empty:
        st.info("오늘 박스 돌파 종목 없음")
    else:
        nb_disp = new_buys_df[["ticker", "name", "market", "rs_rank", "entry_guide",
                                "stop", "box_top", "box_bottom", "days_in_box",
                                "volume_ratio", "is_gap_breakout"]].copy()
        nb_disp["rs_rank"] = nb_disp["rs_rank"].round(1)
        nb_disp["volume_ratio"] = nb_disp["volume_ratio"].round(2)
        st.dataframe(
            nb_disp, use_container_width=True, hide_index=True,
            column_config={
                "ticker": st.column_config.TextColumn("종목", help=g.COL_TICKER),
                "name": st.column_config.TextColumn("이름"),
                "market": st.column_config.TextColumn("시장"),
                "rs_rank": st.column_config.NumberColumn("RS", format="%.1f", help=g.COL_RS_RANK),
                "entry_guide": st.column_config.NumberColumn("진입가", format="%,d",
                                                              help="박스 천장 × 1.003 (참고)"),
                "stop": st.column_config.NumberColumn("손절", format="%,d",
                                                       help="박스 바닥 — 이 아래로 종가 마감 시 청산"),
                "box_top": st.column_config.NumberColumn("box top", format="%,d"),
                "box_bottom": st.column_config.NumberColumn("box bot", format="%,d"),
                "days_in_box": st.column_config.NumberColumn("box일", format="%d",
                                                              help=g.COL_DAYS_IN_BOX),
                "volume_ratio": st.column_config.NumberColumn("vol×", format="%.2f",
                                                               help=g.COL_VOLUME_RATIO),
                "is_gap_breakout": st.column_config.CheckboxColumn("gap", help=g.COL_IS_GAP),
            },
        )

with b2:
    st.subheader("RS rank 분포 (워치리스트)")
    st.caption("같은 시장(KOSPI 또는 KOSDAQ) 내 종목들의 상대 강도 백분위 0~100.")
    if not watchlist_df.empty:
        st.plotly_chart(
            build_rs_distribution(watchlist_df["rs_rank"]),
            use_container_width=True,
        )

st.caption(
    "⚠️ 이 대시보드는 종목 *추천*만 합니다. 진입 가격·시점·수량은 사용자가 차트를 보고 직접 결정. "
    "백테스트 결과는 자동매매 가정 하의 *상한선* 추정으로 실제 결과와 다릅니다."
)
