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
from moneygold import gainers as gn  # noqa: E402
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
def _run_signals(
    _data_dir_str: str,
    asof: str,
    allowed_stages: tuple[int, ...],
    required_template_conditions: tuple[int, ...],
) -> dict:
    """signals.generate_signals 실행. dict로 캐시 가능하게 직렬화.

    allowed_stages / required_template_conditions 가 캐시 키에 포함되므로
    UI에서 바꾸면 자동 재계산.
    """
    cfg = load_config()
    data_dir = Path(_data_dir_str)
    master = _load_master(_data_dir_str)
    if master.empty:
        return {}

    idx_kospi = _load_index_close(_data_dir_str, "KOSPI200")
    idx_kosdaq = _load_index_close(_data_dir_str, "KOSDAQ150")
    idx_us = _load_index_close(_data_dir_str, "^GSPC")
    if idx_kospi.empty and idx_kosdaq.empty and idx_us.empty:
        return {}
    indices = {}
    if not idx_kospi.empty: indices["KOSPI"] = idx_kospi[idx_kospi.index <= asof]
    if not idx_kosdaq.empty: indices["KOSDAQ"] = idx_kosdaq[idx_kosdaq.index <= asof]
    if not idx_us.empty: indices["US"] = idx_us[idx_us.index <= asof]

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
        allowed_stages=allowed_stages,
        required_template_conditions=required_template_conditions,
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

    if "tradable_kis" in master.columns:
        only_tradable_kis = st.checkbox(
            "🇰🇷 KIS 주문 가능 종목만",
            value=False,
            help="KIS 해외주식 master에 등록된 종목만 표시. "
                 "`python -m moneygold.cli.sync --us-kis-crosscheck`로 동기화 필요.",
        )
    else:
        only_tradable_kis = False
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
    st.subheader("🧭 게이트 (Stage / Trend Template)")
    stage_options = [
        (1, "Stage 1 (Basing — 횡보 바닥다지기)"),
        (2, "Stage 2 (Advancing — 상승 추세, 책 권장)"),
        (3, "Stage 3 (Topping — 상승 끝, 분배)"),
        (4, "Stage 4 (Declining — 하락 추세)"),
    ]
    stage_labels = [lbl for _, lbl in stage_options]
    stage_value_by_label = {lbl: v for v, lbl in stage_options}
    selected_stage_labels = st.multiselect(
        "허용 Weinstein Stage",
        stage_labels,
        default=[stage_options[1][1]],   # Stage 2
        help="후보 풀에 포함시킬 Weinstein Stage. 기본은 Stage 2(원전 권장). "
             "Stage 1을 추가하면 바닥다지기 초기 진입 후보까지 넓어짐. "
             "비워두면 모든 Stage 허용.",
    )
    allowed_stages_sel = tuple(sorted(stage_value_by_label[lbl] for lbl in selected_stage_labels))

    cond_labels_full = [
        (1, "1) close > SMA150 & SMA200"),
        (2, "2) SMA150 > SMA200"),
        (3, "3) SMA200 우상향 (22봉 전 대비)"),
        (4, "4) SMA50 > SMA150 & SMA200"),
        (5, "5) close > SMA50"),
        (6, "6) close ≥ 52w 저점 × 1.25"),
        (7, "7) close ≥ 52w 고점 × 0.75"),
        (8, "8) RS rank ≥ 70 (시장 내 백분위)"),
    ]
    cond_label_strings = [lbl for _, lbl in cond_labels_full]
    cond_value_by_label = {lbl: v for v, lbl in cond_labels_full}
    selected_cond_labels = st.multiselect(
        "필수 통과 조건 (Minervini 8조건 중)",
        cond_label_strings,
        default=cond_label_strings,
        help="8개 모두 기본 선택 (원전 권장 — '8/8 통과'). 일부를 해제하면 그 조건은 *통과하지 않아도* "
             "후보 풀에 포함됨. 8개 모두 해제하면 Template 게이트 비활성 → "
             "Stage만으로 필터링.",
    )
    required_conditions_sel = tuple(sorted(cond_value_by_label[lbl] for lbl in selected_cond_labels))

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
sigs_dict = _run_signals(data_dir_str, asof_str, allowed_stages_sel, required_conditions_sel)
if not sigs_dict:
    st.error("시그널 생성 실패. 데이터 또는 지수가 부족합니다.")
    st.stop()

watchlist_df = pd.DataFrame(sigs_dict.get("watchlist", []))
new_buys_df = pd.DataFrame(sigs_dict.get("new_buys", []))

# KIS 주문 가능 필터 (sidebar 토글). master에 tradable_kis 컬럼이 있을 때만 작동.
if only_tradable_kis and "tradable_kis" in master.columns:
    tradable_set = set(master[master["tradable_kis"]]["ticker"])
    if not watchlist_df.empty:
        watchlist_df = watchlist_df[watchlist_df["ticker"].isin(tradable_set)]
    if not new_buys_df.empty:
        new_buys_df = new_buys_df[new_buys_df["ticker"].isin(tradable_set)]
    master = master[master["tradable_kis"]].reset_index(drop=True)

# ---------- 상단: 요약 카드 ----------
st.title("📊 moneygold — 종목 추천 대시보드")
_stage_caption = (
    "Stage " + "/".join(str(x) for x in allowed_stages_sel) if allowed_stages_sel else "Stage 전체"
)
_tmpl_caption = (
    f"Template {len(required_conditions_sel)}/8" if required_conditions_sel else "Template 비활성"
)
st.caption(f"asof **{asof_str}** · Weinstein {_stage_caption} + Minervini {_tmpl_caption} + Darvas Box")

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


tab_main, tab_gainers = st.tabs(["📋 BUY 후보", "📈 오늘 상승"])

@st.cache_data(show_spinner="오늘 상승 종목 분석 중…")
def _gainers_with_stage_cached(_data_dir_str: str, asof: str, min_pct: float) -> pd.DataFrame:
    """daily_gainers + attach_stage + attach_features + alpha_score 캐시."""
    from moneygold import gainers as _gn
    df_g = _gn.daily_gainers(Path(_data_dir_str), asof=asof, min_pct=min_pct)
    if df_g.empty:
        return df_g
    df_g = _gn.attach_stage(df_g, Path(_data_dir_str), asof=asof)
    df_g = _gn.attach_features(df_g, Path(_data_dir_str), asof=asof)
    df_g["alpha_score"] = _gn.compute_alpha_score(df_g)
    return df_g


with tab_gainers:
    st.subheader("📈 오늘 상승 종목의 패턴")
    st.caption(
        f"asof **{asof_str}**: 전일 대비 +N% 이상 상승한 종목의 Weinstein Stage 분포 + "
        "BUY 후보 풀과의 교집합. 단순 상승이 *진짜* 추세인지, *데드캣 바운스*인지 판별용."
    )

    with st.expander("❓ 이 탭의 의미 (처음 보는 경우)", expanded=False):
        st.markdown(g.GAINERS_TAB_INTRO)
        st.markdown("---")
        st.markdown(g.GAINERS_ALPHA_SCORE_INTRO)
        st.markdown("---")
        st.markdown(g.GAINERS_SIGNATURE_INTRO)

    gctrl1, gctrl2, gctrl3 = st.columns([0.3, 0.4, 0.3])
    with gctrl1:
        gainers_pct = st.slider(
            "최소 상승률 (%)", min_value=0.0, max_value=10.0, value=1.0, step=0.5,
            help=g.GAINERS_TOOLTIP_MIN_PCT,
        )
    with gctrl2:
        market_options = sorted(master["market"].unique())
        gainers_markets = st.multiselect(
            "시장 (오늘 상승 탭 한정)", market_options, default=market_options,
            key="gainers_markets",
            help="이 탭에만 적용. 사이드바 시장 필터와 무관.",
        )
    with gctrl3:
        only_buy_intersection = st.checkbox(
            "BUY 후보 교집합만", value=False,
            help="좌측 분포는 전체, 우측 테이블만 BUY ∩ gainers로 좁힘.",
        )

    # ⭐ Alpha predictor 필터 (PR16 백테스트 검증)
    st.markdown("**⭐ Alpha 필터** (백테스트 검증된 predictor — 위 expander 참조)")
    afctrl1, afctrl2, afctrl3 = st.columns(3)
    with afctrl1:
        min_pullback_pct = st.slider(
            "50d 고가 -N% 이상 (Pullback)",
            min_value=0, max_value=50, value=0, step=5,
            help=g.GAINERS_TOOLTIP_PULLBACK_FILTER,
        )
    with afctrl2:
        max_rsi = st.slider(
            "RSI ≤ N (oversold)",
            min_value=20, max_value=100, value=100, step=5,
            help=g.GAINERS_TOOLTIP_RSI_FILTER,
        )
    with afctrl3:
        max_bb_pos = st.slider(
            "BB position ≤ N (하단 근처)",
            min_value=0.0, max_value=1.0, value=1.0, step=0.1,
            help=g.GAINERS_TOOLTIP_BB_FILTER,
        )

    # 분류 필터 (PR15 — forward return predict 안 함, 분류용)
    st.markdown("**🧪 분류 필터** (현재 상태 필터 — forward return 영향 거의 없음)")
    fctrl1, fctrl2, fctrl3 = st.columns([0.3, 0.35, 0.35])
    with fctrl1:
        only_above_sma200 = st.checkbox(
            "SMA200 위만 (분류용)", value=False,
            help=g.GAINERS_TOOLTIP_SMA200_FILTER,
        )
    with fctrl2:
        max_off_52w_high_pct = st.slider(
            "52w 고가 -N% 이내 (모멘텀)", min_value=0, max_value=60, value=0, step=5,
            help=g.GAINERS_TOOLTIP_52W_FILTER,
        )
    with fctrl3:
        min_off_52w_high_pct = st.slider(
            "52w 고가 -N% 이상 (구버전)",
            min_value=0, max_value=60, value=0, step=5,
            help=g.GAINERS_TOOLTIP_52W_FAR_FILTER,
        )

    # 📍 Top N 검증된 alpha 후보 toggle — 모든 필터 무시하고 alpha_score 정렬 상위 N개만.
    st.markdown("**📍 검증된 alpha 후보 (Top N)** — 시장별 자동 가중치, 모든 필터 무시.")
    pctrl1, pctrl2, _pctrl3 = st.columns([0.3, 0.4, 0.3])
    with pctrl1:
        use_alpha_preset = st.checkbox(
            "Top N alpha 정렬만 보기", value=False,
            help="시장별로 검증된 alpha_score (US: Pullback+ATR / KR: Stage+momentum)로 정렬, "
                 "위의 상승률/필터 무시. 한 번 클릭으로 최강 후보 확인.",
        )
    with pctrl2:
        alpha_top_n = st.number_input(
            "Top N", min_value=5, max_value=100, value=10, step=5,
            key="gainers_alpha_top_n",
            help="alpha_score 상위 몇 개를 볼지. (사이드바 '워치리스트 표시 개수'와 별개)",
        )

    df_g_all = _gainers_with_stage_cached(data_dir_str, asof_str, gainers_pct / 100.0)
    if gainers_markets:
        df_g_all = df_g_all[df_g_all["market"].isin(gainers_markets)]
    # KIS 주문 가능 토글 (사이드바)
    if only_tradable_kis and "tradable_kis" in master.columns:
        _kis_set = set(master[master["tradable_kis"]]["ticker"])
        df_g_all = df_g_all[df_g_all["ticker"].isin(_kis_set)]

    # 필터 적용 (df_g_all → df_g). preset 모드면 필터 무시하고 alpha_score top N.
    if use_alpha_preset and "alpha_score" in df_g_all.columns:
        df_g = df_g_all.sort_values("alpha_score", ascending=False).head(int(alpha_top_n)).copy()
    else:
        df_g = df_g_all.copy()
        # Alpha 필터
        if min_pullback_pct > 0 and "pullback_from_50d_high" in df_g.columns:
            df_g = df_g[df_g["pullback_from_50d_high"].fillna(0) >= min_pullback_pct / 100.0]
        if max_rsi < 100 and "rsi_14" in df_g.columns:
            df_g = df_g[df_g["rsi_14"].fillna(100) <= max_rsi]
        if max_bb_pos < 1.0 and "bb_position" in df_g.columns:
            df_g = df_g[df_g["bb_position"].fillna(1.0) <= max_bb_pos]
        # 분류 필터
        if only_above_sma200 and "close_to_sma200" in df_g.columns:
            df_g = df_g[df_g["close_to_sma200"].fillna(0) >= 1.0]
        if max_off_52w_high_pct > 0 and "close_to_52w_high" in df_g.columns:
            thr = 1.0 - max_off_52w_high_pct / 100.0
            df_g = df_g[df_g["close_to_52w_high"].fillna(0) >= thr]
        if min_off_52w_high_pct > 0 and "close_to_52w_high" in df_g.columns:
            thr = 1.0 - min_off_52w_high_pct / 100.0
            df_g = df_g[df_g["close_to_52w_high"].fillna(1.0) <= thr]

    if df_g_all.empty:
        st.info(f"asof {asof_str} 기준 +{gainers_pct:.1f}% 이상 상승 종목이 없습니다.")
    else:
        # 메트릭
        m1, m2, m3, m4 = st.columns(4)
        buy_set = set(watchlist_df["ticker"]) if not watchlist_df.empty else set()
        inter_df = df_g[df_g["ticker"].isin(buy_set)]
        stage2_df = df_g[df_g["stage"] == stg.STAGE_ADVANCING]
        m1.metric("상승 종목 (필터 후)", len(df_g),
                  delta=f"{len(df_g) - len(df_g_all):+d}" if len(df_g) != len(df_g_all) else None,
                  help=f"+{gainers_pct:.1f}% 이상 종목 수. delta = 시그너처 필터로 제거된 양.")
        m2.metric("Stage 2 (ADVANCING)", len(stage2_df),
                  help="진짜 추세 지속 가능성. SMA150 위 + 상승 기울기.")
        m3.metric("Stage 4 (DECLINING)",
                  int((df_g["stage"] == stg.STAGE_DECLINING).sum()),
                  help="하락 추세 안에서의 반등 — 데드캣 바운스 가능성.")
        m4.metric("BUY ∩ 상승", len(inter_df),
                  help="워치리스트(Stage2+Template8/8) ∩ 오늘 상승 (필터 후).")

        st.divider()

        gl, gr = st.columns([0.4, 0.6])
        with gl:
            st.markdown("**Stage 분포 (필터 후)**")
            st.caption(g.GAINERS_TOOLTIP_STAGE_DIST)
            if df_g.empty:
                st.info("필터 후 남은 종목 없음.")
            else:
                dist = gn.stage_distribution(df_g["stage"])
                chart_df = dist.set_index("stage_name")[["count"]]
                st.bar_chart(chart_df, height=240, color="#3b82f6")

                # 시그너처 비교표 (필터 전 데이터 기준 — Stage 2/4 차이 보기)
                st.markdown("**시그너처 비교 (필터 전, Stage별 median)**")
                st.caption(g.GAINERS_TOOLTIP_SIG_TABLE)
                # Stage 2, 4 둘 다 있어야 의미 있음
                sig = gn.signature_table(df_g_all, stage_col="stage")
                if not sig.empty:
                    # column 이름을 사람이 읽기 좋게 + Stage 2/4만 우선 표시
                    rename_idx = {
                        "rvol": "거래량 (vs 19일)",
                        "close_to_sma50": "종가/SMA50",
                        "close_to_sma150": "종가/SMA150",
                        "close_to_sma200": "종가/SMA200 ⭐",
                        "close_to_52w_high": "종가/52w 고가 ⭐",
                        "close_to_52w_low": "종가/52w 저가",
                        "sma50_over_sma200": "SMA50/SMA200",
                        "sma200_slope": "SMA200 기울기",
                    }
                    sig = sig.rename(index=rename_idx)
                    sig.columns = [f"Stage {c} {stg.STAGE_NAMES.get(int(c), '?')}"
                                   for c in sig.columns]
                    st.dataframe(
                        sig.round(3), use_container_width=True,
                    )

        with gr:
            if only_buy_intersection:
                disp_df = inter_df.copy()
                st.markdown(f"**BUY ∩ 오늘 상승 ({len(disp_df)}건)**")
                st.caption(g.GAINERS_TOOLTIP_BUY_INTERSECT)
            else:
                disp_df = df_g.copy()
                disp_df["is_buy"] = disp_df["ticker"].isin(buy_set)
                st.markdown(f"**오늘 상승 종목 ({len(disp_df)}건, 필터 후)**")
                st.caption("`is_buy` = 워치리스트 교집합 여부. `종가/SMA200` 컬럼이 1.0 이상이면 Stage 2 후보, 미만이면 Stage 4 의심.")

            if disp_df.empty:
                st.info("해당 조건에 맞는 종목 없음.")
            else:
                # preset 모드면 alpha_score 정렬 (이미 head(alpha_top_n) 적용됨), 아니면 상승률 정렬
                if use_alpha_preset and "alpha_score" in disp_df.columns:
                    disp_df = disp_df.sort_values("alpha_score", ascending=False)
                else:
                    disp_df = disp_df.sort_values("pct_chg", ascending=False).head(200)
                disp_df["pct_chg_pct"] = (disp_df["pct_chg"] * 100).round(2)
                if "pullback_from_50d_high" in disp_df.columns:
                    disp_df["pullback_pct"] = (disp_df["pullback_from_50d_high"] * 100).round(1)
                if "rsi_14" in disp_df.columns:
                    disp_df["rsi_14"] = disp_df["rsi_14"].round(1)
                if "close_to_sma200" in disp_df.columns:
                    disp_df["close_to_sma200"] = disp_df["close_to_sma200"].round(3)
                cols = ["ticker", "name", "market", "alpha_score",
                        "stage_name", "pct_chg_pct",
                        "pullback_pct", "rsi_14", "close_to_sma200",
                        "close", "prev_close"]
                if "is_buy" in disp_df.columns:
                    cols.append("is_buy")
                # 누락 컬럼 제거
                cols = [c for c in cols if c in disp_df.columns]
                col_cfg = {
                    "ticker": st.column_config.TextColumn("종목"),
                    "name": st.column_config.TextColumn("이름"),
                    "market": st.column_config.TextColumn("시장"),
                    "alpha_score": st.column_config.NumberColumn(
                        "Alpha ⭐", format="%.1f",
                        help="시장별 검증된 가중합 (0-100). US=Pullback+ATR+BB+RSI, "
                             "KR=Stage+GC+52w high+RSI - Pullback페널티.",
                    ),
                    "stage_name": st.column_config.TextColumn("Stage",
                        help="0 UNKNOWN / 1 BASING / 2 ADVANCING / 3 TOPPING / 4 DECLINING"),
                    "pct_chg_pct": st.column_config.NumberColumn("상승률%", format="%+.2f"),
                    "pullback_pct": st.column_config.NumberColumn(
                        "Pullback%", format="%.1f", help=g.COL_PULLBACK_PCT,
                    ),
                    "rsi_14": st.column_config.NumberColumn(
                        "RSI(14)", format="%.1f", help=g.COL_RSI_14,
                    ),
                    "close_to_sma200": st.column_config.NumberColumn(
                        "종가/SMA200", format="%.3f", help=g.COL_CLOSE_TO_SMA200,
                    ),
                    "close": st.column_config.NumberColumn("종가", format="%,.2f"),
                    "prev_close": st.column_config.NumberColumn("전일", format="%,.2f"),
                }
                if "is_buy" in disp_df.columns:
                    col_cfg["is_buy"] = st.column_config.CheckboxColumn(
                        "BUY 후보", help="signals.py 워치리스트(Stage2+Template8/8)에 포함되는 종목.",
                    )
                st.dataframe(
                    disp_df[cols], hide_index=True, use_container_width=True, height=600,
                    column_config=col_cfg,
                )

with tab_main:
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
        # mcap slider는 KRW(조원) 단위. US 종목은 USD라 단위 mismatch — KR/KOSDAQ만 적용.
        if mcap_min_krw is not None and "mcap" in flt.columns:
            kr_mask = flt["market"].isin(["KOSPI", "KOSDAQ"])
            kr_keep = (flt["mcap"] >= mcap_min_krw) & (flt["mcap"] <= mcap_max_krw)
            flt = flt[(~kr_mask) | kr_keep]
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
            if "mcap" in flt.columns: base_cols.append("mcap")
            base_cols.extend(["rs_rank", "rs_momentum", "close", "box_state",
                              "days_in_box", "suggested_stop"])
            # Stage 컬럼 — 여러 stage 허용 시 어떤 종목이 어느 stage인지 식별
            if "stage" in flt.columns and (not allowed_stages_sel or len(allowed_stages_sel) != 1):
                base_cols.insert(3, "stage")
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
                "stage": st.column_config.NumberColumn(
                    "Stage", format="%d",
                    help="현재 Weinstein Stage (1=Basing / 2=Advancing / 3=Topping / 4=Declining). "
                         "사이드바 '허용 Stage'에서 선택한 것들만 여기 나타남."),
                "rs_rank": st.column_config.NumberColumn("RS", format="%.1f", help=g.COL_RS_RANK),
                "rs_momentum": st.column_config.NumberColumn("rs_mom", format="%+.2f", help=g.COL_RS_MOMENTUM),
                "close": st.column_config.NumberColumn("종가", format="%,d", help=g.COL_CLOSE),
                "box_state": st.column_config.TextColumn("박스", help=g.COL_BOX_STATE),
                "days_in_box": st.column_config.NumberColumn("box일", format="%d", help=g.COL_DAYS_IN_BOX),
                "suggested_stop": st.column_config.NumberColumn("stop hint", format="%,d", help=g.COL_SUGGESTED_STOP),
            }
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
                        _req_set = set(required_conditions_sel)
                        if not _req_set:
                            _gate_desc = "현재 게이트는 *Template 비활성* — 8조건은 진단용으로만 표시."
                        elif _req_set == {1, 2, 3, 4, 5, 6, 7, 8}:
                            _gate_desc = "현재 게이트는 *8/8 모두 필수* — 원전 권장."
                        else:
                            _gate_desc = (f"현재 게이트는 *조건 {sorted(_req_set)} 필수*. "
                                          "필수 외 조건이 fail이어도 후보 풀에 포함됨 (회색 ❌).")
                        st.caption(
                            "Mark Minervini의 *Trade Like a Stock Market Wizard*에서 추출한 "
                            "Trend Template. " + _gate_desc
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
                            {
                                "조건": title,
                                "의미": desc,
                                "필수": "✓" if (i + 1) in _req_set else "—",
                                "통과": "✅" if c else "❌",
                            }
                            for i, ((title, _ko, desc), c) in enumerate(
                                zip(g.MINERVINI_CONDITIONS, t.checks))
                        ]
                        st.dataframe(
                            pd.DataFrame(rows), use_container_width=True, hide_index=True,
                            column_config={
                                "조건": st.column_config.TextColumn("조건", width="medium"),
                                "의미": st.column_config.TextColumn("의미 (한 줄)", width="large"),
                                "필수": st.column_config.TextColumn(
                                    "필수", width="small",
                                    help="✓ = 사이드바에서 '필수 통과 조건'으로 선택된 조건. — = 진단용."),
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
