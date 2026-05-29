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
def _prev_trading_day(asof_str: str, _data_dir_str: str) -> str | None:
    """asof_str 직전 영업일 (지수 calendar 기준). 없으면 None.

    엔진의 *예측력* 검증용 — '어제 BUY 후보였던 종목이 오늘 진짜 올랐나' 의
    '어제' = 직전 영업일 (주말/공휴일 자동 skip)."""
    for code in ["KOSPI200", "^GSPC", "KOSDAQ150"]:
        idx_p = store.index_path(Path(_data_dir_str), code)
        if not idx_p.exists():
            continue
        df = store.read_parquet_safe(idx_p)
        if df is None or df.empty or "date" not in df.columns:
            continue
        dates = sorted(df["date"].astype(str).unique())
        prior = [d for d in dates if d < asof_str]
        if prior:
            return prior[-1]
    return None


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


@st.cache_data(show_spinner=False)
def _available_trading_days(data_dir_str: str) -> list[str]:
    """store 에 실제로 있는 거래일 리스트 (YYYYMMDD asc).

    여러 지수 (KOSPI200/KOSDAQ150/^GSPC) 의 거래일 *합집합* 사용. 즉 어떤 시장이라도
    거래한 날이면 picker 에서 선택 가능. KR 5/27 / US 5/27 휴장 같은 비대칭 처리 OK.

    이전 구현은 *가장 긴 캘린더* 채택이라 ^GSPC(1607일) > KOSPI200(1570일) 시 미국 캘린더
    latest 가 picker max 가 돼서 *한국 거래일* 선택 못 하는 버그가 있었음.
    """
    all_dates: set[str] = set()
    for code in ("KOSPI200", "KOSDAQ150", "^GSPC"):
        p = store.index_path(Path(data_dir_str), code)
        df = store.read_parquet_safe(p)
        if df is None or df.empty or "date" not in df.columns:
            continue
        all_dates.update(df["date"].astype(str).tolist())
    return sorted(all_dates)


# WatchlistEntry 스키마 버전. 필드 추가/제거 시 bump → @st.cache_data 자동 무효화.
# 이 값을 안 바꾸면 사용자가 옛 캐시(예: net_margin 컬럼 없는 dict)를 보고 새 필터가
# 작동 안 하는 것처럼 보임 — 그 때문에 sidebar의 '🔄 시그널 재계산' 버튼을 누르거나
# Streamlit 프로세스 재시작이 필요했음.
_WATCHLIST_SCHEMA_VERSION = "2026-05-22-vol-acc-ratio"


@st.cache_data(show_spinner="시그널 생성 중 (~30초)…")
def _run_signals(
    _data_dir_str: str,
    asof: str,
    allowed_stages: tuple[int, ...],
    required_template_conditions: tuple[int, ...],
    schema_version: str = _WATCHLIST_SCHEMA_VERSION,
) -> dict:
    """signals.generate_signals 실행. dict로 캐시 가능하게 직렬화.

    allowed_stages / required_template_conditions 가 캐시 키에 포함되므로
    UI에서 바꾸면 자동 재계산. schema_version은 코드 측에서 필드 추가/제거 시 bump되며,
    값이 바뀌면 @st.cache_data가 자동 무효화 → 옛 결과 재사용 방지.
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
    mcap_map = dict(zip(master["ticker"], master["mcap"], strict=False)) if has_real_mcap else {}

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
    rs_rank_map = dict(zip(rs_df["ticker"], rs_df["rs_rank"], strict=False))
    rs_mom_map = dict(zip(rs_df["ticker"], rs_df["rs_mom"], strict=False))

    # 섹터 내 RS rank — 같은 섹터·시장 종목들끼리만 백분위. peer < 10이면 NaN.
    sector_rs_rank_map: dict[str, float] = {}
    if "sector" in master.columns:
        sector_lookup = dict(zip(master["ticker"], master["sector"], strict=False))
        rs_df["sector"] = rs_df["ticker"].map(sector_lookup).fillna("UNKNOWN").astype(str)
        rs_df["sector_rs_rank"] = float("nan")
        for (_mkt, sector), g in rs_df.groupby(["market", "sector"]):
            if sector == "UNKNOWN":
                continue
            if g["rs_mom"].notna().sum() < 10:
                continue
            rs_df.loc[g.index, "sector_rs_rank"] = ind.rs_rank(g["rs_mom"]).values
        sector_rs_rank_map = {
            t: float(v) for t, v in zip(rs_df["ticker"], rs_df["sector_rs_rank"], strict=False)
            if pd.notna(v)
        }

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
        sector_rs_rank_map=sector_rs_rank_map,
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

    # 🔍 종목 검색 — ticker / 종목명 부분일치. 비우면 일반 모드.
    # 검색어 있을 때: 워치리스트 표는 매칭 행으로 좁히고, 게이트 미통과 종목도 차트 패널에서 볼 수 있음.
    search_query = st.text_input(
        "🔍 종목 검색",
        value="",
        placeholder="ticker 또는 종목명 (예: AAPL, 005930, 삼성, NVDA)",
        help="ticker(prefix) 또는 종목명(substring) 부분일치 검색. "
             "검색하면 다른 필터/게이트는 무시되고 매칭 종목의 차트와 진단을 바로 확인할 수 있음.",
    )

    # asof — 실제 store 에 있는 최신 거래일을 기본값으로 + date picker + 퀵버튼
    _trading_days = _available_trading_days(data_dir_str)
    _latest_data_date = _trading_days[-1] if _trading_days else datetime.now().strftime("%Y%m%d")
    _earliest_data_date = _trading_days[0] if _trading_days else "20200101"

    # 세션 상태로 asof_str 보관 — 버튼/picker 어느 쪽이든 단일 출처
    if "asof_str" not in st.session_state:
        st.session_state["asof_str"] = _latest_data_date
    # 데이터 영역 밖이면 클램프
    if _trading_days and st.session_state["asof_str"] > _latest_data_date:
        st.session_state["asof_str"] = _latest_data_date

    def _jump_days(offset_days: int) -> None:
        """available 캘린더 안에서 offset 만큼 점프. -ve = 과거."""
        if not _trading_days:
            return
        cur = st.session_state["asof_str"]
        if cur not in _trading_days:
            # 현재값이 캘린더에 없으면 가장 가까운 이하 거래일로 보정
            below = [d for d in _trading_days if d <= cur]
            cur = below[-1] if below else _trading_days[-1]
        idx = _trading_days.index(cur)
        new_idx = max(0, min(len(_trading_days) - 1, idx + offset_days))
        st.session_state["asof_str"] = _trading_days[new_idx]

    st.markdown("**기준일**")
    bcols = st.columns(4)
    bcols[0].button("최신", on_click=lambda: st.session_state.update(asof_str=_latest_data_date),
                    use_container_width=True, help="store 의 가장 최근 거래일로 점프")
    bcols[1].button("-1일", on_click=_jump_days, args=(-1,),
                    use_container_width=True, help="직전 거래일")
    bcols[2].button("-5일", on_click=_jump_days, args=(-5,),
                    use_container_width=True, help="5거래일 전")
    bcols[3].button("-30일", on_click=_jump_days, args=(-30,),
                    use_container_width=True, help="30거래일 전")

    # date_input — 달력 picker. session_state 와 양방향.
    _cur_str = st.session_state["asof_str"]
    try:
        _cur_date = datetime.strptime(_cur_str, "%Y%m%d").date()
    except ValueError:
        _cur_date = datetime.strptime(_latest_data_date, "%Y%m%d").date()
    _min_date = datetime.strptime(_earliest_data_date, "%Y%m%d").date()
    _max_date = datetime.strptime(_latest_data_date, "%Y%m%d").date()
    picked = st.date_input(
        "달력에서 선택", value=_cur_date,
        min_value=_min_date, max_value=_max_date,
        help=g.TOOLTIP_ASOF + "  · 비거래일(주말/공휴일) 선택 시 자동으로 직전 거래일 사용.",
        key="asof_date_picker",
    )
    picked_str = picked.strftime("%Y%m%d")
    # picker 가 비거래일을 골랐을 수도 있음 → 가장 가까운 이하 거래일로 보정
    if _trading_days and picked_str not in _trading_days:
        below = [d for d in _trading_days if d <= picked_str]
        picked_str = below[-1] if below else _trading_days[0]
    if picked_str != st.session_state["asof_str"]:
        st.session_state["asof_str"] = picked_str
        st.rerun()

    asof_str = st.session_state["asof_str"]
    if asof_str != _latest_data_date:
        st.caption(
            f"📅 선택: **{asof_str}** "
            f"(최신 데이터: {_latest_data_date}, {_trading_days.index(asof_str) - _trading_days.index(_latest_data_date) if asof_str in _trading_days else 0}일 차이)"
            if asof_str in _trading_days else
            f"📅 선택: **{asof_str}** (최신: {_latest_data_date})"
        )
    else:
        st.caption(f"📅 최신 거래일: **{asof_str}**")

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
    min_net_margin = st.slider(
        "순이익률 최소 (%)", min_value=-30, max_value=50, value=-30, step=1,
        help="가장 최근 분기 순이익률 (당기순이익 ÷ 매출). 영익률이 영업 효율이라면 "
             "순익률은 *최종* 수익성 — 영업외손익·세금·이자 다 반영된 결과.",
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
    st.subheader("📈 수급 필터")
    min_vol_acc_ratio = st.slider(
        "수급비 최소 (vol_acc_ratio)",
        min_value=0.0, max_value=2.0, value=0.0, step=0.05,
        help="거래량 수급/이탈 비율 = 최근 20일 평균 거래량 ÷ 60일 평균. "
             "📊 데이터 기반 (52K 관측, KR 시총상위200, 6년): U-shape — "
             "**≥1.4 강한 누적** (+30d alpha +2.97%p, +60d +6.26%p), "
             "**<0.7 조용한 누적** (+30d alpha +2.72%p, +60d +6.03%p — 직관 반대). "
             "**1.0~1.3 평범** (+30d alpha ~+1.5%p, 시장과 큰 차이 없음). "
             "임의 1.1/1.2 임계는 의미 미미 — *극단값* 만 의미 있음. "
             "단순 ≥ 필터로는 *조용한 누적 (<0.7)* 못 잡으니, '강한 누적' 만 추려려면 1.4 권장. "
             "0.0 = 필터 비활성. NaN(데이터 부족)은 통과.",
    )

    st.divider()
    top_n = st.number_input("워치리스트 표시 개수", min_value=5, max_value=2000, value=30, step=5,
                             help=g.TOOLTIP_TOP_N)

    st.divider()
    st.subheader("🔄 데이터 갱신")

    # 현재 보유 데이터의 가장 최근 날짜 (KOSPI200 / ^GSPC 인덱스 기준)
    def _latest_local_date(code: str) -> str | None:
        p = store.index_path(Path(data_dir_str), code)
        if not p.exists():
            return None
        df = store.read_parquet_safe(p)
        if df is None or df.empty or "date" not in df.columns:
            return None
        return str(df["date"].max())

    _kr_latest = _latest_local_date("KOSPI200") or "없음"
    _us_latest = _latest_local_date("^GSPC") or "없음"
    st.caption(f"보유 데이터 최신: 🇰🇷 **{_kr_latest}** · 🇺🇸 **{_us_latest}**")

    _bcol1, _bcol2 = st.columns(2)
    with _bcol1:
        _do_kr = st.button("🇰🇷 한국 (~5분)", use_container_width=True,
                            help="pykrx + KIS로 KR 마스터/지수/종목 일봉 incremental sync.")
    with _bcol2:
        _do_us = st.button("🇺🇸 미국 (~3분)", use_container_width=True,
                            help="yfinance로 US 지수/일봉 incremental sync. 분기재무/컨센서스는 제외.")

    if _do_kr or _do_us:
        from moneygold.data import sync as _ds
        from moneygold.data.kis_client import KISClient as _KIS
        _sync = _ds.DataSync(_KIS(cfg.kis), Path(cfg.data_dir))

        if _do_kr:
            status = st.status("🇰🇷 한국 데이터 sync 중 (pykrx batch)…", expanded=True)
            with status:
                try:
                    st.write("1️⃣ 마스터 갱신 중 (pykrx)…")
                    m = _sync.sync_universe()
                    tickers = m[m["market"].isin(["KOSPI", "KOSDAQ"])]["ticker"].tolist()
                    st.write(f"   ✓ 마스터 {len(m)} (KR {len(tickers)})")

                    st.write("2️⃣ 일봉 sync (pykrx batch — KIS 500 회피, 200배 빠름)…")
                    st.caption(
                        "일자별 × 시장별 batch fetch (5d × 2 시장 = 10회). "
                        "KIS 종목별 호출 (2580회) 대신. 거래대금/시총 정확, 휴장 패딩 없음."
                    )
                    bars_prog = st.progress(0.0, text="시작…")
                    def _bar_cb(cur, total, msg):
                        bars_prog.progress(min(cur/total, 1.0), text=f"{cur}/{total} · {msg}")
                    bs = _sync.sync_bars_kr_pykrx_batch(recent_days=5, progress_callback=_bar_cb)
                    bars_prog.empty()
                    st.write(
                        f"   ✓ {bs['tickers_touched']} 종목 · {bs['total_rows']:,} 행 · "
                        f"{bs['days_fetched']} 일자 fetch · errors {len(bs['errors'])}"
                    )
                    if bs["errors"]:
                        with st.expander(f"⚠️ {len(bs['errors'])} 에러 (상위 5)"):
                            for k, v in bs["errors"][:5]:
                                st.text(f"  {k}: {v}")
                    # 아래 호환 변수
                    ok, no_change, failed = bs["tickers_touched"], 0, bs["errors"]

                    st.write("3️⃣ 지수 갱신 중 (pykrx)…")
                    isn = _sync.sync_indices_kr_pykrx(recent_days=5)
                    st.write(f"   ✓ 지수 updated {isn['updated']} · no_change {isn['no_change']} · "
                              f"failed {len(isn['failed'])}")

                    # 갱신 후 실제 store 최신 일자 확인
                    new_latest = _latest_local_date("KOSPI200") or "없음"

                    # 4️⃣ streamlit 캐시 무효화 — parquet 갱신 후 화면이 옛 데이터 안 들고 있도록
                    st.write("4️⃣ 대시보드 캐시 무효화 중…")
                    st.cache_data.clear()
                    st.write("   ✓ 캐시 클리어 완료.")

                    # 5️⃣ 사이드바 기준일(asof) 도 새 최신 일자로 자동 갱신.
                    # session_state 는 cache_data.clear() 영향 안 받기 때문에 명시적 갱신 필수.
                    # 사용자가 옛 날짜로 고정해둔 상태에서 sync 해도 차트가 그 옛 날짜
                    # 이전까지만 보이는 흔한 문제 해결.
                    prev_asof = st.session_state.get("asof_str", "")
                    if new_latest and new_latest != "없음":
                        st.session_state["asof_str"] = new_latest
                        if prev_asof and prev_asof != new_latest:
                            st.write(f"5️⃣ 기준일 자동 갱신: **{prev_asof} → {new_latest}**")
                        else:
                            st.write(f"5️⃣ 기준일: **{new_latest}** (변경 없음)")

                    status.update(label=
                        f"🇰🇷 완료 — 마스터 {len(m)} · 일봉 updated {ok}/no_change {no_change}/failed {len(failed)} "
                        f"· 지수 updated {isn['updated']} · 최신 일자 **{new_latest}**",
                        state="complete",
                    )
                    if new_latest == _kr_latest:
                        st.info(
                            f"💡 store 최신 일자가 갱신 전과 동일 (**{new_latest}**)입니다. "
                            "이는 KIS API 가 *오늘 일봉을 아직 publish 하지 않아서* 발생합니다. "
                            "한국 장 마감 (15:30) 후 보통 1~2시간 이내, 늦으면 다음날 새벽에 확정됨 — "
                            "그 후 다시 갱신 버튼을 누르세요. 지금은 force_refresh_recent=5 로 *최근 5거래일은 정정*됐습니다."
                        )
                    st.success(
                        f"✅ 갱신 완료. 기준일이 **{new_latest}** 로 설정됨. "
                        "아래 버튼을 누르면 즉시 새 데이터가 표시됩니다."
                    )
                    st.button("🔄 지금 새로고침", on_click=lambda: st.rerun(), type="primary")
                except Exception as e:
                    status.update(label=f"🇰🇷 sync 실패: {e}", state="error")
                    st.exception(e)

        if _do_us:
            with st.spinner("🇺🇸 미국 데이터 sync 중 (지수/일봉)…"):
                try:
                    isn = _sync.sync_indices_us()
                    m = _sync.load_universe()
                    us_tk = m[m["market"] == "US"]["ticker"].tolist()
                    if not us_tk:
                        # 마스터에 US가 비어 있으면 일단 마스터부터 채움
                        us_master = _sync.sync_universe_us()
                        us_tk = us_master["ticker"].tolist()
                    bs = _sync.sync_bars_all_us(us_tk, batch=True)
                    st.success(
                        f"🇺🇸 완료 — 지수 {isn['updated']} · "
                        f"일봉 updated {bs['updated']} no_change {bs['no_change']} "
                        f"failed {len(bs['failed'])}"
                    )
                except Exception as e:
                    st.error(f"🇺🇸 sync 실패: {e}")

        # sync 후 모든 캐시 무효화
        _run_signals.clear()
        _load_master.clear()
        _load_bars.clear()
        _load_index_close.clear()
        _prev_trading_day.clear()
        st.rerun()

    st.divider()
    if st.button("🔄 시그널 재계산 (캐시만 초기화)", use_container_width=True,
                  help="parquet 데이터는 그대로 두고 Streamlit 캐시만 비움. "
                       "필터/게이트 변경이 안 반영될 때 사용."):
        _run_signals.clear()
        _load_master.clear()
        _load_bars.clear()
        _load_index_close.clear()
        _prev_trading_day.clear()
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


tab_main, tab_gainers, tab_momentum, tab_backtest, tab_dgi = st.tabs(
    ["📋 BUY 후보", "📈 오늘 상승", "⚡ Momentum Breakout", "🔬 백테스트", "💎 가속화 장기투자"]
)

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
        # 어제 BUY 후보 풀 — 엔진 예측력 검증용 (오늘 상승했다면 우리 어제 추천이 맞았다는 증거)
        prev_day = _prev_trading_day(asof_str, data_dir_str)
        if prev_day:
            yesterday_sigs = _run_signals(
                data_dir_str, prev_day, allowed_stages_sel, required_conditions_sel,
            )
            yesterday_buy_set = {e.get("ticker") for e in yesterday_sigs.get("watchlist", [])}
        else:
            yesterday_buy_set = set()

        # 메트릭
        m1, m2, m3, m4 = st.columns(4)
        inter_df = df_g[df_g["ticker"].isin(yesterday_buy_set)]
        stage2_df = df_g[df_g["stage"] == stg.STAGE_ADVANCING]
        m1.metric("상승 종목 (필터 후)", len(df_g),
                  delta=f"{len(df_g) - len(df_g_all):+d}" if len(df_g) != len(df_g_all) else None,
                  help=f"+{gainers_pct:.1f}% 이상 종목 수. delta = 시그너처 필터로 제거된 양.")
        m2.metric("Stage 2 (ADVANCING)", len(stage2_df),
                  help="진짜 추세 지속 가능성. SMA150 위 + 상승 기울기.")
        m3.metric("Stage 4 (DECLINING)",
                  int((df_g["stage"] == stg.STAGE_DECLINING).sum()),
                  help="하락 추세 안에서의 반등 — 데드캣 바운스 가능성.")
        m4.metric(
            f"어제({prev_day or '?'}) BUY ∩ 오늘 상승" if prev_day else "어제 BUY ∩ 오늘 상승",
            len(inter_df),
            help="**엔진 예측력 검증** — 어제 워치리스트(Stage+Template+필터 통과)에 있던 "
                 "종목 중 오늘 N% 이상 상승한 수. 많을수록 우리 엔진이 *다음 날* 움직임을 "
                 "잘 예측한 것.",
        )

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
                st.markdown(
                    f"**어제({prev_day or '?'}) BUY ∩ 오늘 상승 ({len(disp_df)}건)** — "
                    "엔진이 적중한 종목"
                )
                st.caption(
                    "어제 워치리스트에 있던 종목 중 오늘 상승한 것. "
                    "이 종목들은 우리 엔진의 *다음 날 예측이 맞은* 사례 — 같은 패턴을 "
                    "오늘 BUY 후보 풀에서 골라 내일 검증."
                )
            else:
                disp_df = df_g.copy()
                disp_df["is_buy"] = disp_df["ticker"].isin(yesterday_buy_set)
                st.markdown(f"**오늘 상승 종목 ({len(disp_df)}건, 필터 후)**")
                st.caption(
                    f"`is_buy` = *어제({prev_day or '?'})* BUY 후보 풀 교집합 — "
                    "체크돼 있으면 엔진이 어제 추천했고 오늘 상승한 *예측 적중* 사례. "
                    "`종가/SMA200` 1.0 이상이면 Stage 2, 미만이면 Stage 4 의심."
                )

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
                    "close": st.column_config.NumberColumn("종가", format="localized"),
                    "prev_close": st.column_config.NumberColumn("전일", format="localized"),
                }
                if "is_buy" in disp_df.columns:
                    col_cfg["is_buy"] = st.column_config.CheckboxColumn(
                        "어제 BUY", help="*어제* 워치리스트(Stage+Template+필터 통과)에 있던 종목. "
                        "체크 = 엔진이 어제 추천했고 오늘 N% 이상 상승 → *다음 날 예측 적중* 사례.",
                    )
                st.dataframe(
                    disp_df[cols], hide_index=True, use_container_width=True, height=600,
                    column_config=col_cfg,
                )

# ============================================================
# ⚡ Momentum Breakout 탭
# ============================================================

@st.cache_data(show_spinner="Momentum Breakout 후보 계산 중…")
def _momentum_candidates_cached(
    _data_dir_str: str,
    asof: str,
    new_high_lookback: int,
    fresh_window: int,
    volume_spike_ratio: float,
    top_n_value: int,
    top_n_marketcap: int | None,
    min_listed_days: int,
    stop_loss_pct: float,
    search_window_days: int = 1,
) -> list[dict]:
    """find_entry_candidates 결과를 dict 리스트로 직렬화해 캐시.

    search_window_days >= 1: asof 이하 마지막 N 거래일을 *각각* 검사해
    종목별로 가장 최근 시그널 발생일(`signal_date`)을 보관. 같은 종목이 여러 날
    잡히면 *가장 최근* 발생만 남김 (관측 의도: "최근 N일 안에 한 번이라도 시그널").
    """
    from moneygold.data import store as _st
    from moneygold.strategies.momentum_breakout import (
        MomentumConfig,
        find_entry_candidates,
    )

    data_dir = Path(_data_dir_str)
    master_all = _load_master(_data_dir_str)
    if master_all.empty:
        return []
    # KR + US 통합 — find_entry_candidates 가 시장별 그룹 분리 처리.
    master_all = master_all[master_all["market"].isin(("KOSPI", "KOSDAQ", "US"))].copy()
    if master_all.empty:
        return []

    bars_by_ticker: dict[str, pd.DataFrame] = {}
    for tk in master_all["ticker"].astype(str):
        b = _st.read_parquet_safe(_st.bars_path(data_dir, tk))
        if b is not None and not b.empty:
            bars_by_ticker[tk] = b

    cfg_m = MomentumConfig(
        new_high_lookback=new_high_lookback,
        fresh_window=fresh_window,
        volume_spike_ratio=volume_spike_ratio,
        top_n_value=top_n_value,
        top_n_marketcap=top_n_marketcap,
        min_listed_days=min_listed_days,
        stop_loss_pct=stop_loss_pct,
    )

    # 검사 대상 날짜 결정 — asof 이하 마지막 N 거래일
    trading_days = _available_trading_days(_data_dir_str)
    asof_below = [d for d in trading_days if d <= asof]
    n = max(1, int(search_window_days))
    days_to_check = asof_below[-n:] if asof_below else [asof]
    # 최신 → 옛날 순으로 순회 (먼저 만난 쪽이 "가장 최근" 발생)
    days_to_check = list(reversed(days_to_check))

    asof_dt = pd.to_datetime(asof)
    seen: dict[str, dict] = {}
    for d in days_to_check:
        entries = find_entry_candidates(d, bars_by_ticker, master_all, cfg_m)
        for e in entries:
            if e.ticker in seen:
                continue   # 이미 더 최근 발생 등록됨
            d_age = (asof_dt - pd.to_datetime(d)).days
            seen[e.ticker] = {
                "ticker": e.ticker, "name": e.name, "market": e.market,
                "signal_date": d, "days_ago": int(d_age),
                "close": e.close, "new_high_ref": e.new_high_ref,
                "new_high_amplitude_pct": e.new_high_amplitude * 100.0,
                "volume_ratio": e.volume_ratio,
                "value_today": e.value_today,
                "value_rank": e.value_rank,
                "suggested_stop": e.suggested_stop,
                "score": e.score,
            }
    return list(seen.values())


@st.cache_data(show_spinner=False)
def _last_momentum_signal_date(
    _data_dir_str: str,
    asof: str,
    new_high_lookback: int,
    fresh_window: int,
    volume_spike_ratio: float,
    top_n_value: int,
    top_n_marketcap: int | None,
    min_listed_days: int,
    stop_loss_pct: float,
    max_search_days: int = 60,
) -> str | None:
    """asof 이하 최대 max_search_days 거래일 안에서 *한 종목이라도* 시그널이 난
    가장 최근 거래일을 찾는다. 없으면 None.

    "후보 0개" 상황에서 빠른 점프 버튼용. 검사는 최신 → 옛날 순으로 진행하다가
    첫 발견 시 즉시 반환 — 60일 전체 스캔이 아니다.
    """
    from moneygold.data import store as _st
    from moneygold.strategies.momentum_breakout import (
        MomentumConfig,
        find_entry_candidates,
    )
    data_dir = Path(_data_dir_str)
    master_all = _load_master(_data_dir_str)
    if master_all.empty:
        return None
    master_all = master_all[master_all["market"].isin(("KOSPI", "KOSDAQ", "US"))].copy()
    if master_all.empty:
        return None
    bars_by_ticker: dict[str, pd.DataFrame] = {}
    for tk in master_all["ticker"].astype(str):
        b = _st.read_parquet_safe(_st.bars_path(data_dir, tk))
        if b is not None and not b.empty:
            bars_by_ticker[tk] = b
    cfg_m = MomentumConfig(
        new_high_lookback=new_high_lookback, fresh_window=fresh_window,
        volume_spike_ratio=volume_spike_ratio, top_n_value=top_n_value,
        top_n_marketcap=top_n_marketcap, min_listed_days=min_listed_days,
        stop_loss_pct=stop_loss_pct,
    )
    trading_days = _available_trading_days(_data_dir_str)
    asof_below = [d for d in trading_days if d <= asof]
    for d in reversed(asof_below[-max_search_days:]):
        entries = find_entry_candidates(d, bars_by_ticker, master_all, cfg_m)
        if entries:
            return d
    return None


with tab_momentum:
    st.subheader("⚡ Momentum Breakout 후보")
    st.caption(
        f"asof **{asof_str}** · N일 신고가 돌파 + 거래대금 스파이크 + 거래대금/시총 상위 N. "
        "Stage/Template 게이트와 *별개* — KOSPI+KOSDAQ 통합 순수 모멘텀 신호."
    )

    with st.expander("❓ 이 탭은 뭔가요? (처음 보는 경우)", expanded=False):
        st.markdown(
            "**진입 시그널** (당일 종가 기준, 모두 AND):\n\n"
            "1. **신고가 돌파** — 종가 > 직전 N일 (기본 60) 최고 종가\n"
            "2. **Fresh** — 직전 M일 (기본 20) 안에 같은 의미의 돌파가 없었음 (반복 돌파 제외)\n"
            "3. **거래대금 스파이크** — 당일 거래대금 ≥ 20일 평균 × ratio (기본 1.5)\n"
            "4. **유동성** — 당일 KOSPI+KOSDAQ 통합 거래대금 상위 N위 (기본 100)\n"
            "5. **시가총액** — 상위 N위 (기본 100, off 가능)\n"
            "6. **상장 기간** — N일 이상 (기본 60)\n"
            "7. 우선주/스팩/리츠/ETF·ETN 제외\n\n"
            "**Score** = `volume_ratio × (1 + new_high_amplitude)` — 거래대금 폭증과 "
            "신고가 갱신폭의 곱. 클수록 강세.\n\n"
            "**진입 가정**: 시그널은 *오늘 종가* 기준. 실제 진입은 *다음 영업일 시가* 권장. "
            "**자동 주문 없음** — 사용자가 차트 보고 직접 결정.\n\n"
            "*Stage/Template/Darvas 와 무관*. 이 탭에 뜬다고 BUY 후보 풀에 뜨는 건 아님."
        )

    mctrl1, mctrl2, mctrl3, mctrl4 = st.columns(4)
    with mctrl1:
        momo_lookback = st.slider(
            "신고가 lookback (영업일)", min_value=20, max_value=120, value=60, step=5,
            help="직전 N봉 최고 종가를 오늘이 초과하면 신고가 돌파.",
        )
    with mctrl2:
        momo_fresh = st.slider(
            "Fresh window (영업일)", min_value=5, max_value=40, value=20, step=5,
            help="직전 M봉 안에 같은 의미의 돌파가 *없어야* fresh — 반복 돌파 컷.",
        )
    with mctrl3:
        momo_vol_ratio = st.slider(
            "거래대금 스파이크 배수", min_value=1.0, max_value=5.0, value=1.5, step=0.1,
            help="오늘 거래대금 / 직전 20일 평균. 이상이어야 통과.",
        )
    with mctrl4:
        momo_top_value = st.slider(
            "거래대금 상위 N위", min_value=20, max_value=300, value=100, step=10,
            help="당일 KOSPI+KOSDAQ 통합 거래대금 랭킹 상위 N에 들어야 통과.",
        )

    mctrl5, mctrl6, mctrl7, mctrl8 = st.columns(4)
    with mctrl5:
        use_mcap_filter = st.checkbox(
            "시총 상위 100 필터 적용", value=True,
            help="off면 시총 무관 (소형주도 후보 가능).",
        )
    with mctrl6:
        momo_min_listed = st.slider(
            "최소 상장 영업일", min_value=20, max_value=120, value=60, step=10,
            help="신규상장 종목 컷오프 (bars 행 수 기준).",
        )
    with mctrl7:
        momo_stop_pct = st.slider(
            "초기 손절 비율 (%)", min_value=5.0, max_value=20.0, value=10.0, step=0.5,
            help="entry × (1 - X/100) — 진입 후 절대 손절선.",
        )
    with mctrl8:
        momo_search_days = st.slider(
            "탐색 기간 (거래일)", min_value=1, max_value=45, value=20, step=1,
            help="asof 마지막 N 거래일 안에 *한 번이라도* 시그널 난 종목 표시. "
                 "📊 데이터 기반 임계 (6년 백테스트, 227 시그널, +30d alpha): "
                 "0~15d = alpha 거의 유지 (+3~5%p), 15~30d = alpha 양수 유지 (+4~5%p), "
                 "30~45d = alpha 깎이기 시작 (+1.4%p). "
                 "45+ 거래일 = alpha 반감, 사실상 무의미한 새 사이클. "
                 "오늘 후보 0개면 이 값을 늘려보세요. 단, 오래될수록 "
                 "현재 시세가 발생일 종가에서 멀어져 -10% 손절선이 무력화됐을 수 있음 — "
                 "종가/발생일 종가 컬럼 비교 필수.",
        )

    momo_top_mcap_arg = 100 if use_mcap_filter else None
    momo_entries = _momentum_candidates_cached(
        data_dir_str, asof_str,
        momo_lookback, momo_fresh, momo_vol_ratio,
        momo_top_value, momo_top_mcap_arg,
        momo_min_listed, momo_stop_pct / 100.0,
        momo_search_days,
    )

    if not momo_entries:
        st.warning(
            f"⚠ asof **{asof_str}** 직전 **{momo_search_days} 거래일** 안에 조건 통과 종목 없음."
        )
        # 0개인 경우 → 가장 최근 시그널 발생일 안내 + 점프 버튼
        # alpha 의미 있는 범위 (~45거래일) 내에서만 검색.
        last_signal_d = _last_momentum_signal_date(
            data_dir_str, asof_str,
            momo_lookback, momo_fresh, momo_vol_ratio,
            momo_top_value, momo_top_mcap_arg,
            momo_min_listed, momo_stop_pct / 100.0,
            max_search_days=45,
        )
        rc1, rc2 = st.columns([0.55, 0.45])
        with rc1:
            if last_signal_d is None:
                st.info(
                    "최근 45거래일(~9주, alpha 유효 범위) 안에 시그널 없음. "
                    "그 이상 옛 시그널은 alpha 반감으로 무의미. "
                    "파라미터를 완화해보세요 (vol_ratio ↓, fresh window ↓, top_n_value ↑, 시총 필터 off)."
                )
            else:
                _trading_days_for_jump = _available_trading_days(data_dir_str)
                _last_age = (
                    pd.to_datetime(asof_str) - pd.to_datetime(last_signal_d)
                ).days
                st.info(
                    f"📅 가장 최근 시그널 발생일: **{last_signal_d}** "
                    f"({_last_age}일 전). 오른쪽 버튼으로 점프하거나 *탐색 기간*을 "
                    f"늘리세요."
                )
        with rc2:
            if last_signal_d is not None:
                def _jump_to_last_signal() -> None:
                    st.session_state["asof_str"] = last_signal_d  # noqa: B023
                st.button(
                    f"→ 사이드바 기준일을 {last_signal_d} 로 점프",
                    on_click=_jump_to_last_signal,
                    use_container_width=True,
                    type="primary",
                )
    else:
        # 최신 발생일 desc → 같은 날 안에서는 점수 desc 정렬
        momo_entries.sort(key=lambda x: (-pd.Timestamp(x["signal_date"]).value, -x["score"]))
        only_today = sum(1 for e in momo_entries if e["days_ago"] == 0)
        if momo_search_days == 1 or only_today == len(momo_entries):
            st.success(f"⚡ {len(momo_entries)}개 종목 (asof 당일 시그널)")
        else:
            st.success(
                f"⚡ {len(momo_entries)}개 종목 — 최근 {momo_search_days}거래일 누적. "
                f"오늘({asof_str}) 신규: {only_today}개."
            )

        momo_df = pd.DataFrame(momo_entries)
        momo_df["거래대금(억)"] = momo_df["value_today"] / 1e8
        display_df = momo_df[[
            "ticker", "name", "market",
            "signal_date", "days_ago",
            "close",
            "new_high_amplitude_pct", "volume_ratio",
            "value_rank", "거래대금(억)",
            "suggested_stop", "score",
        ]].rename(columns={
            "ticker": "종목", "name": "종목명", "market": "시장",
            "signal_date": "발생일", "days_ago": "asof - 발생",
            "close": "종가(발생일)",
            "new_high_amplitude_pct": "신고가 갱신폭(%)",
            "volume_ratio": "거래대금 배수",
            "value_rank": "거래대금 순위",
            "suggested_stop": "권장 손절가",
            "score": "점수",
        })

        st.dataframe(
            display_df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "종가(발생일)": st.column_config.NumberColumn(format="%,.0f"),
                "asof - 발생": st.column_config.NumberColumn(
                    format="%d일", help="시그널 발생일이 asof로부터 며칠 전인가. 0 = 당일.",
                ),
                "신고가 갱신폭(%)": st.column_config.NumberColumn(format="%+.2f%%"),
                "거래대금 배수": st.column_config.NumberColumn(format="%.2fx"),
                "거래대금(억)": st.column_config.NumberColumn(format="%,.0f"),
                "권장 손절가": st.column_config.NumberColumn(format="%,.0f"),
                "점수": st.column_config.NumberColumn(format="%.2f"),
            },
            height=min(600, 50 + len(display_df) * 36),
        )

        st.caption(
            "📌 **점수** = 거래대금 배수 × (1 + 신고가 갱신폭). 클수록 강세. "
            "권장 손절가 = 종가 × (1 - 손절비율). 진입은 *다음 영업일 시가* 권장."
        )


with tab_main:
    # ---------- ⚡ Momentum overlay ----------
    # 사용자 명제 검증 (6년 백테스트: alpha +303%p, 모든 regime expectancy 양수) 결과,
    # BUY 후보 풀에 momentum breakout 시그널 정보를 함께 표시. 진입/손절/익절 룰:
    #   진입: 최근 N거래일 안에 60일 신고가 막 돌파 (거래대금 1.5×, 통합 거래대금/시총 상위 100)
    #   손절: entry × (1 - 10%)
    #   익절: +20% 도달 후 MA20 이탈
    mo_col1, mo_col2, mo_col3 = st.columns([0.3, 0.35, 0.35])
    with mo_col1:
        show_momo_overlay = st.checkbox(
            "⚡ Momentum 정보 표시", value=True,
            help="BUY 후보 풀 각 종목에 momentum breakout 시그널 발생 여부 + "
                 "-10% 손절가 + +20% 익절 트리거 가격 추가 표시.",
        )
    with mo_col2:
        momo_main_window = st.slider(
            "⚡ Momentum 탐색 기간 (거래일)", 1, 45, 5,
            disabled=not show_momo_overlay,
            help="asof 직전 N거래일 안에 시그널 난 종목 표시. "
                 "📊 데이터 기반: 0~15d alpha 거의 유지, 15~30d 양수 유지, 30~45d 깎임. "
                 "45+ 거래일은 alpha 반감으로 무의미. "
                 "오래된 시그널은 현재 시세가 발생일에서 멀어져 -10% 손절선이 무력화됐을 수 있음 — "
                 "종가/발생일 종가 컬럼 비교 필수.",
            key="momo_main_window",
        )
    with mo_col3:
        momo_only_main = st.checkbox(
            "⚡ Momentum 발생 종목만", value=False,
            disabled=not show_momo_overlay,
            help="체크 시 momentum breakout 시그널 *발생한* watchlist 종목만 표시 (강한 필터).",
        )

    # momentum 정보 fetch (cached) — 기본 파라미터 사용 (사이드바 매수 명제와 정합)
    momo_signal_map: dict = {}
    if show_momo_overlay:
        momo_entries_main = _momentum_candidates_cached(
            data_dir_str, asof_str,
            new_high_lookback=60, fresh_window=20, volume_spike_ratio=1.5,
            top_n_value=100, top_n_marketcap=100, min_listed_days=60,
            stop_loss_pct=0.10, search_window_days=momo_main_window,
        )
        momo_signal_map = {e["ticker"]: e for e in momo_entries_main}

        # watchlist 에 momentum 정보 left-merge
        if not watchlist_df.empty and momo_signal_map:
            momo_df_main = pd.DataFrame([
                {
                    "ticker": e["ticker"],
                    "momo_signal_date": e["signal_date"],
                    "momo_days_ago": e["days_ago"],
                    "momo_score": e["score"],
                    "momo_volume_ratio": e["volume_ratio"],
                    "momo_new_high_amp_pct": e["new_high_amplitude_pct"],
                    # 발생일 종가 — entry price 근사 (다음 영업일 시가의 정직한 proxy)
                    "momo_signal_close": e["close"],
                }
                for e in momo_entries_main
            ])
            watchlist_df = watchlist_df.merge(momo_df_main, on="ticker", how="left")
            # 손절/익절 = *발생일 종가* 기준 (명제 정합).
            # 시그널 미발생 종목은 NaN — 진입 가정 없으니 가격 산출 의미 없음.
            watchlist_df["momo_stop_10pct"] = watchlist_df["momo_signal_close"] * 0.90
            watchlist_df["momo_profit_trigger_20pct"] = watchlist_df["momo_signal_close"] * 1.20

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "후보 풀 (Stage2 + Template)", len(watchlist_df),
        help="Weinstein Stage 2 + Minervini 8조건 *모두* 통과한 종목 수.",
    )
    c2.metric(
        "⭐ 박스 돌파", len(new_buys_df),
        help="후보 풀 중 오늘 Darvas 박스 천장을 거래량 동반 돌파한 종목 수 (즉시 검토 대상).",
    )
    if show_momo_overlay and "momo_signal_date" in watchlist_df.columns:
        momo_hit = watchlist_df["momo_signal_date"].notna().sum()
        c3.metric(
            "⚡ Momentum 시그널", int(momo_hit),
            help=f"후보 풀 중 최근 {momo_main_window} 거래일 안에 momentum breakout "
                 "시그널 발생한 종목 수. -10% 손절 + +20% 익절 룰 적용 가능 종목.",
        )
    elif not watchlist_df.empty:
        c3.metric(
            "RS rank 평균", f"{watchlist_df['rs_rank'].mean():.1f}",
            help="워치리스트 종목들의 RS rank 평균.",
        )
    if not watchlist_df.empty:
        c4.metric(
            "rs_mom 최대", f"{watchlist_df['rs_momentum'].max():+.2f}",
            help="가장 강한 모멘텀 종목의 가중 평균 수익률.",
        )

    if show_momo_overlay:
        st.caption(
            "ℹ️ ⚡ 표시 종목은 *최근 신고가 막 돌파 + 거래대금 급증*. "
            "**손절/익절 가격은 발생일 종가 기준** (진입 = 다음 영업일 시가 가정의 proxy). "
            "**-10% 손절** = 발생일 종가 × 0.90, **+20% 이익 도달 후 20일선 이탈 시 익절**. "
            "**시그널 유효 기간 ~30거래일** (15d 이내 알파 거의 유지, 30~45d 깎임, 45+d 무의미). "
            "6년 백테스트 검증: alpha +303%p, 모든 regime expectancy 양수, 2022 베어 -15% (KOSPI -25% 대비 방어)."
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
        if "net_margin" in flt.columns and min_net_margin > -30:
            flt = flt[flt["net_margin"].fillna(min_net_margin) >= min_net_margin]
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
        # 수급 필터 (NaN 통과)
        if min_vol_acc_ratio > 0 and "vol_acc_ratio" in flt.columns:
            flt = flt[flt["vol_acc_ratio"].fillna(min_vol_acc_ratio) >= min_vol_acc_ratio]
        # ⚡ Momentum 발생 종목만 필터
        if show_momo_overlay and momo_only_main and "momo_signal_date" in flt.columns:
            flt = flt[flt["momo_signal_date"].notna()]
        flt = flt.sort_values("rs_rank", ascending=False).reset_index(drop=True)
    else:
        flt = watchlist_df

    # ---------- 🔍 검색 적용 ----------
    # 검색어 있으면: 워치리스트 표를 매칭 행으로 좁히고, 게이트 미통과 종목 정보도 별도로 수집.
    search_q = (search_query or "").strip().upper()
    search_master_hits = pd.DataFrame()
    if search_q:
        if not flt.empty:
            wl_match = (
                flt["ticker"].astype(str).str.upper().str.contains(search_q, regex=False)
                | flt["name"].astype(str).str.upper().str.contains(search_q, regex=False)
            )
            flt = flt[wl_match].reset_index(drop=True)
        # 마스터에서도 검색 — 게이트 미통과인 종목까지 잡힘 (차트 패널 진입용)
        m_match = (
            master["ticker"].astype(str).str.upper().str.contains(search_q, regex=False)
            | master["name"].astype(str).str.upper().str.contains(search_q, regex=False)
        )
        search_master_hits = master[m_match].copy()

    # ---------- 메인 상하 배치: 위 워치리스트 (full width) / 아래 차트 (full width) ----------
    top_section = st.container()
    chart_section = st.container()

    with top_section:
        if search_q:
            st.subheader(f"🔍 검색 결과: '{search_query}'")
            st.caption(f"워치리스트 매칭 {len(flt)}건 · 마스터 매칭 {len(search_master_hits)}건")
        else:
            st.subheader(f"BUY 후보 풀 ({len(flt)}건 필터링)")

        # 검색 결과 중 워치리스트에 *없는* 종목 표시 (게이트 미통과)
        non_wl_hits = pd.DataFrame()
        if search_q and not search_master_hits.empty:
            wl_set = set(flt["ticker"]) if not flt.empty else set()
            non_wl_hits = search_master_hits[~search_master_hits["ticker"].isin(wl_set)]

        if flt.empty and search_q and non_wl_hits.empty:
            st.info(f"'{search_query}' 와 일치하는 종목이 없습니다.")
            selected_ticker = None
        elif flt.empty and search_q:
            # 검색 매칭은 있지만 모두 게이트 미통과 → 첫 매칭 종목을 차트 패널에 표시
            st.warning(
                f"매칭된 {len(non_wl_hits)}개 종목 모두 현재 게이트/필터를 통과하지 못합니다. "
                f"차트와 진단은 우측에서 확인 가능합니다."
            )
            # 간단한 표로 매칭 종목들 나열
            mcols = ["ticker", "name", "market"]
            if "sector" in non_wl_hits.columns: mcols.append("sector")
            st.dataframe(
                non_wl_hits[mcols].head(20), use_container_width=True, hide_index=True,
                height=min(400, 40 + 35 * len(non_wl_hits)),
            )
            selected_ticker = non_wl_hits.iloc[0]["ticker"]
        elif flt.empty:
            st.info("필터 조건에 맞는 종목이 없습니다.")
            selected_ticker = None
        else:
            base_cols = ["ticker", "name", "market"]
            if "mcap" in flt.columns: base_cols.append("mcap")
            base_cols.extend(["rs_rank", "sector_rs_rank", "rs_momentum", "close", "box_state",
                              "days_in_box", "suggested_stop"])
            # Stage 컬럼 — 여러 stage 허용 시 어떤 종목이 어느 stage인지 식별
            if "stage" in flt.columns and (not allowed_stages_sel or len(allowed_stages_sel) != 1):
                base_cols.insert(3, "stage")
            # ⚡ Momentum 컬럼 — overlay 옵션 활성 시
            if show_momo_overlay:
                for c in ["momo_signal_date", "momo_days_ago", "momo_score",
                          "momo_signal_close",
                          "momo_stop_10pct", "momo_profit_trigger_20pct"]:
                    if c in flt.columns:
                        base_cols.append(c)
            # 거래량 수급 + 펀더멘털 컬럼
            for c in ["vol_acc_ratio",
                      "revenue_yoy", "op_income_yoy", "op_margin", "net_margin",
                      "growth_quarters", "op_growth_quarters", "accelerating"]:
                if c in flt.columns:
                    base_cols.append(c)
            # 컨센서스 (애널) 컬럼 — *사용자 요청으로 BUY 후보 풀 표에서 숨김*.
            # 사이드바 컨센서스 필터는 그대로 유지 (필터링은 여전히 작동).
            disp = flt[base_cols].head(top_n).copy()
            disp["rs_rank"] = disp["rs_rank"].round(1)
            disp["rs_momentum"] = disp["rs_momentum"].round(2)
            if "sector_rs_rank" in disp.columns:
                disp["sector_rs_rank"] = disp["sector_rs_rank"].round(1)
            if "mcap" in disp.columns:
                disp["mcap_trillion"] = (disp["mcap"] / 1e12).round(3)
                disp = disp.drop(columns=["mcap"])
            # momentum 컬럼 정리 — 미발생 행은 NaN/None 표시
            if "momo_score" in disp.columns:
                disp["momo_score"] = disp["momo_score"].round(2)
            if "momo_days_ago" in disp.columns:
                # Int64 (nullable) 로 변환해 NaN 유지하면서 정수 표시
                disp["momo_days_ago"] = disp["momo_days_ago"].astype("Int64")
            for c in ["momo_signal_close", "momo_stop_10pct", "momo_profit_trigger_20pct"]:
                if c in disp.columns:
                    disp[c] = disp[c].round(0)
            for c in ["vol_acc_ratio",
                      "revenue_yoy", "op_income_yoy", "op_margin", "net_margin",
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
                "sector_rs_rank": st.column_config.NumberColumn(
                    "섹터RS", format="%.1f", help=g.COL_SECTOR_RS_RANK,
                ),
                "rs_momentum": st.column_config.NumberColumn("rs_mom", format="%+.2f", help=g.COL_RS_MOMENTUM),
                "close": st.column_config.NumberColumn("종가", format="localized", help=g.COL_CLOSE),
                "box_state": st.column_config.TextColumn("박스", help=g.COL_BOX_STATE),
                "days_in_box": st.column_config.NumberColumn("box일", format="%d", help=g.COL_DAYS_IN_BOX),
                "suggested_stop": st.column_config.NumberColumn("stop hint", format="localized", help=g.COL_SUGGESTED_STOP),
            }
            if "mcap_trillion" in disp.columns:
                col_cfg["mcap_trillion"] = st.column_config.NumberColumn("시총(조)", format="%.2f", help=g.COL_MCAP_TRILLION)
            # ⚡ Momentum overlay 컬럼
            if "momo_signal_date" in disp.columns:
                col_cfg["momo_signal_date"] = st.column_config.TextColumn(
                    "⚡ 발생일", help="최근 N거래일 안에 momentum breakout 시그널이 *발생한* 날짜. "
                    "값 있으면 60일 신고가 막 돌파 + 거래대금 1.5× 조건 통과."
                )
            if "momo_days_ago" in disp.columns:
                col_cfg["momo_days_ago"] = st.column_config.NumberColumn(
                    "⚡ N일전", format="%d", help="시그널 발생일이 asof 로부터 며칠 전인가. 0 = 오늘."
                )
            if "momo_score" in disp.columns:
                col_cfg["momo_score"] = st.column_config.NumberColumn(
                    "⚡ 점수", format="%.2f",
                    help="거래대금 배수 × (1 + 신고가 갱신폭). 클수록 강한 돌파."
                )
            if "momo_signal_close" in disp.columns:
                col_cfg["momo_signal_close"] = st.column_config.NumberColumn(
                    "⚡ 발생일 종가", format="localized",
                    help="시그널 발생일의 종가. 진입 가격(다음 영업일 시가)의 정직한 proxy. "
                         "손절/익절 가격은 이 값 기준."
                )
            if "momo_stop_10pct" in disp.columns:
                col_cfg["momo_stop_10pct"] = st.column_config.NumberColumn(
                    "-10% 손절", format="localized",
                    help="명제 1 손절가 = *발생일 종가* × 0.90. 진입 후 이 가격에 STOP 주문."
                )
            if "momo_profit_trigger_20pct" in disp.columns:
                col_cfg["momo_profit_trigger_20pct"] = st.column_config.NumberColumn(
                    "+20% 익절 트리거", format="localized",
                    help="명제 3 트리거 = *발생일 종가* × 1.20. 이 가격 도달 후 종가가 20일선 이탈하면 익절."
                )
            # 펀더멘털
            if "revenue_yoy" in disp.columns:
                col_cfg["revenue_yoy"] = st.column_config.NumberColumn(
                    "매출YoY%", format="%+.1f",
                    help="가장 최근 분기 매출 YoY 성장률. 25% 이상이 미네비니 권장. NaN = 데이터 없음.")
            if "op_income_yoy" in disp.columns:
                col_cfg["op_income_yoy"] = st.column_config.NumberColumn(
                    "영익YoY%", format="%+.1f",
                    help="가장 최근 분기 영업이익 YoY 성장률.")
            if "vol_acc_ratio" in disp.columns:
                col_cfg["vol_acc_ratio"] = st.column_config.NumberColumn(
                    "수급비", format="%.2f",
                    help="거래량 수급/이탈 비율 = 최근 20일 평균 거래량 ÷ 60일 평균. "
                         "📊 데이터 (52K obs, 6년): U-shape — "
                         "≥1.4 강한 누적 (+30d alpha +2.97%p), "
                         "<0.7 조용한 누적 (+30d alpha +2.72%p — 직관 반대), "
                         "1.0~1.3 평범 (+30d alpha ~+1.5%p). "
                         "임의 1.1/1.2 임계는 의미 미미 — *극단값* 만 의미 있음.")
            if "op_margin" in disp.columns:
                col_cfg["op_margin"] = st.column_config.NumberColumn(
                    "영익률%", format="%.1f",
                    help="가장 최근 분기 영업이익률 = 영업이익 ÷ 매출.")
            if "net_margin" in disp.columns:
                col_cfg["net_margin"] = st.column_config.NumberColumn(
                    "순익률%", format="%.1f",
                    help="가장 최근 분기 순이익률 = 당기순이익 ÷ 매출. "
                         "영익률보다 낮으면 영업외손익·이자·세금에서 큰 손실. "
                         "영익률보다 높으면 일회성 이익(자산매각·환차익 등) 가능성.")
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

    st.divider()
    with chart_section:
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
                in_watchlist = not wl_row.empty
                if in_watchlist:
                    rs_v = float(wl_row.iloc[0]["rs_rank"])
                else:
                    # 검색으로 들어온 게이트 미통과 종목 — rs_rank 알 수 없으니 NaN.
                    # 조건 8(RS≥70)은 자동 fail이지만 나머지 1-7은 정상 진단됨.
                    rs_v = float("nan")
                    st.warning(
                        f"⚠️ {selected_ticker}는 현재 워치리스트에 없습니다 (게이트 미통과). "
                        "차트와 1-7 조건 진단은 정상 표시되지만 조건 8(RS rank)은 자동 fail로 표시됩니다."
                    )
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
                            zip(g.MINERVINI_CONDITIONS, t.checks, strict=False))
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

                if in_watchlist:
                    wl = wl_row.iloc[0]
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
                    # 검색된 게이트 미통과 종목 — 진입 가이드 대신 간단 정보만
                    last_close = float(bars["close"].iloc[-1])
                    st.markdown(f"**현재 종가** `{last_close:,.0f}` · 박스/진입 가이드는 워치리스트 종목 한정.")
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
                    "entry_guide": st.column_config.NumberColumn("진입가", format="localized",
                                                                  help="박스 천장 × 1.003 (참고)"),
                    "stop": st.column_config.NumberColumn("손절", format="localized",
                                                           help="박스 바닥 — 이 아래로 종가 마감 시 청산"),
                    "box_top": st.column_config.NumberColumn("box top", format="localized"),
                    "box_bottom": st.column_config.NumberColumn("box bot", format="localized"),
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


# ============================================================
# 🔬 백테스트 탭
# ============================================================
with tab_backtest:
    st.subheader("🔬 우리 엔진의 신뢰성 — Forward Return 백테스트")
    st.caption(
        "과거 N개 시점에 워치리스트(Stage + Template + 펀더멘털)를 재현하고, 각 종목의 "
        "+5d/+10d/+20d 수익률을 측정해 factor 별로 집계합니다. "
        "현재 사이드바의 게이트/필터 설정이 그대로 적용됩니다."
    )

    bc1, bc2, bc3, bc4 = st.columns([0.28, 0.28, 0.22, 0.22])
    with bc1:
        bt_start = st.text_input(
            "시작일 (YYYYMMDD)",
            value=(datetime.now() - pd.Timedelta(days=180)).strftime("%Y%m%d"),
            max_chars=8, key="bt_start",
            help="180일 전부터 시작이 기본. 데이터 보존 한도(2년) 안에서 자유 선택.",
        )
    with bc2:
        bt_end = st.text_input(
            "종료일 (YYYYMMDD)",
            value=(datetime.now() - pd.Timedelta(days=30)).strftime("%Y%m%d"),
            max_chars=8, key="bt_end",
            help="forward return 계산 위해 *최소 20영업일 buffer* 필요 → 오늘로부터 30일 전이 기본.",
        )
    with bc3:
        bt_stride = st.number_input(
            "샘플 주기 (영업일)", min_value=1, max_value=20, value=5, key="bt_stride",
            help="1 = 매일, 5 = 매주 월요일, 10 = 격주. 매일 돌리면 정밀하지만 시간 5배.",
        )
    with bc4:
        bt_run = st.button("▶ 백테스트 실행", type="primary", key="bt_run_btn")

    @st.cache_data(show_spinner=False)
    def _run_backtest_cached(
        _data_dir: str, start: str, end: str, stride: int,
        allowed: tuple[int, ...], required: tuple[int, ...],
        schema_version: str = _WATCHLIST_SCHEMA_VERSION,
    ) -> dict:
        from moneygold import backtest_engine as bte
        result = bte.run_backtest(
            Path(_data_dir), cfg, start, end,
            stride_days=int(stride),
            allowed_stages=allowed,
            required_template_conditions=required,
        )
        return {
            "entries": result.entries.to_dict("records") if not result.entries.empty else [],
            "asofs": result.asofs,
            "market_returns": {k: v.to_dict("records") for k, v in result.market_returns.items()},
            "horizons": list(result.horizons),
        }

    if bt_run:
        with st.spinner(f"백테스트 실행 중 ({bt_start} ~ {bt_end}, stride={bt_stride})…"):
            bt_dict = _run_backtest_cached(
                data_dir_str, bt_start, bt_end, int(bt_stride),
                allowed_stages_sel, required_conditions_sel,
            )
        entries = pd.DataFrame(bt_dict["entries"])
        horizons = bt_dict["horizons"]
        market_returns = {k: pd.DataFrame(v) for k, v in bt_dict["market_returns"].items()}

        if entries.empty:
            st.warning(
                f"기간 {bt_start} ~ {bt_end} 동안 게이트 통과 종목이 없습니다. "
                "기간 확장 또는 사이드바 게이트 완화 시도해보세요."
            )
        else:
            n_snapshots = len(bt_dict["asofs"])
            n_picks = len(entries)
            n_unique = entries["ticker"].nunique()
            st.success(
                f"**{n_snapshots}** snapshots · **{n_picks}** picks "
                f"(unique tickers {n_unique}) · 기간 {bt_start} ~ {bt_end}"
            )

            # ---------- 1) 종합 forward return ----------
            st.markdown("### 1️⃣ 종합 forward return")
            r1 = st.columns(len(horizons))
            for i, h in enumerate(horizons):
                col = f"fwd_{h}d"
                if col not in entries.columns:
                    continue
                vals = entries[col].dropna()
                if vals.empty:
                    continue
                mean_r = vals.mean()
                hit_r = (vals > 0).mean() * 100
                with r1[i]:
                    st.metric(
                        f"+{h}영업일 평균 수익률",
                        f"{mean_r:+.2f}%",
                        delta=f"적중률 {hit_r:.1f}%",
                        help=f"{h}영업일 후 수익률의 *평균* (각 pick 동일 가중). "
                             f"적중률 = 양수 수익 비율.",
                    )

            # ---------- 2) 시장 비교 (alpha) ----------
            st.markdown("### 2️⃣ 시장 대비 alpha")
            alpha_rows = []
            for mkt in entries["market"].unique():
                mkt_picks = entries[entries["market"] == mkt]
                mkt_idx = market_returns.get(mkt)
                if mkt_idx is None or mkt_idx.empty:
                    continue
                row = {"market": mkt, "n_picks": len(mkt_picks)}
                for h in horizons:
                    col = f"fwd_{h}d"
                    pick_mean = float(mkt_picks[col].dropna().mean()) if col in mkt_picks.columns else float("nan")
                    idx_mean = float(mkt_idx[col].dropna().mean()) if col in mkt_idx.columns else float("nan")
                    row[f"pick_{h}d"] = pick_mean
                    row[f"idx_{h}d"] = idx_mean
                    row[f"alpha_{h}d"] = pick_mean - idx_mean
                alpha_rows.append(row)
            if alpha_rows:
                alpha_df = pd.DataFrame(alpha_rows)
                alpha_df = alpha_df.round(2)
                st.dataframe(alpha_df, use_container_width=True, hide_index=True)
                st.caption(
                    "**alpha = pick 평균 − 시장지수 평균.** 양수면 우리 엔진이 단순 BUY-and-HOLD "
                    "시장지수보다 잘 골랐다는 뜻. 음수면 운빨이거나 선택 알고리즘 결함."
                )

            # ---------- 3) Factor 분석 ----------
            st.markdown("### 3️⃣ Factor 별 평균 수익률")
            st.caption("같은 게이트를 통과한 종목 안에서, *추가 변수*가 forward return을 예측하는지.")

            from moneygold import backtest_engine as bte

            with st.expander("📊 Weinstein Stage", expanded=True):
                fs = bte.factor_summary(entries, "stage", horizons=tuple(horizons))
                if not fs.empty:
                    st.dataframe(fs.round(2), use_container_width=True, hide_index=True)

            with st.expander("📊 RS rank quintile"):
                fs = bte.factor_summary(
                    entries, "rs_rank", horizons=tuple(horizons),
                    bins=[0, 20, 40, 60, 80, 100],
                    bin_labels=["Q1 (0-20)", "Q2 (20-40)", "Q3 (40-60)", "Q4 (60-80)", "Q5 (80-100)"],
                )
                if not fs.empty:
                    st.dataframe(fs.round(2), use_container_width=True, hide_index=True)

            with st.expander("📊 연속 매출 성장 분기 (growth_quarters)"):
                fs = bte.factor_summary(
                    entries, "growth_quarters", horizons=tuple(horizons),
                    bins=[-1, 0, 2, 4, 8, 100],
                    bin_labels=["0", "1-2", "3-4", "5-8", "9+"],
                )
                if not fs.empty:
                    st.dataframe(fs.round(2), use_container_width=True, hide_index=True)

            with st.expander("📊 거래량 수급/이탈 (vol_acc_ratio = 20일 평균 / 60일 평균)", expanded=True):
                st.caption(
                    "1.0보다 크면 최근 거래 *증가* (누적 매수 가능성), 작으면 *감소* (이탈). "
                    "한국 시장에서 주목할 변수 — 외인/기관 매수가 들어오면 거래량이 먼저 늘어남."
                )
                fs = bte.factor_summary(
                    entries, "vol_acc_ratio", horizons=tuple(horizons),
                    bins=[0, 0.8, 1.0, 1.2, 1.5, 10],
                    bin_labels=["이탈 강 (<0.8)", "이탈 약 (0.8-1.0)", "보합 (1.0-1.2)",
                                "누적 약 (1.2-1.5)", "누적 강 (>1.5)"],
                )
                if not fs.empty:
                    st.dataframe(fs.round(2), use_container_width=True, hide_index=True)

                # KR 시장만 따로
                kr_entries = entries[entries["market"].isin(["KOSPI", "KOSDAQ"])]
                if not kr_entries.empty:
                    st.markdown("**🇰🇷 한국 시장만 (KOSPI + KOSDAQ)**")
                    fs_kr = bte.factor_summary(
                        kr_entries, "vol_acc_ratio", horizons=tuple(horizons),
                        bins=[0, 0.8, 1.0, 1.2, 1.5, 10],
                        bin_labels=["이탈 강", "이탈 약", "보합", "누적 약", "누적 강"],
                    )
                    if not fs_kr.empty:
                        st.dataframe(fs_kr.round(2), use_container_width=True, hide_index=True)

            with st.expander("📊 영업이익률 (op_margin)"):
                fs = bte.factor_summary(
                    entries, "op_margin", horizons=tuple(horizons),
                    bins=[-100, 0, 5, 10, 20, 1000],
                    bin_labels=["적자", "0-5%", "5-10%", "10-20%", "20%+"],
                )
                if not fs.empty:
                    st.dataframe(fs.round(2), use_container_width=True, hide_index=True)

            with st.expander("📊 순이익률 (net_margin)"):
                fs = bte.factor_summary(
                    entries, "net_margin", horizons=tuple(horizons),
                    bins=[-1000, 0, 5, 10, 20, 1000],
                    bin_labels=["적자", "0-5%", "5-10%", "10-20%", "20%+"],
                )
                if not fs.empty:
                    st.dataframe(fs.round(2), use_container_width=True, hide_index=True)

            with st.expander("📊 가속 (accelerating)"):
                fs = bte.factor_summary(entries, "accelerating", horizons=tuple(horizons))
                if not fs.empty:
                    st.dataframe(fs.round(2), use_container_width=True, hide_index=True)

            # ---------- 4) Top / Bottom picks ----------
            st.markdown("### 4️⃣ 개별 종목 결과 (상/하위)")
            primary_horizon = horizons[1] if len(horizons) >= 2 else horizons[0]
            primary_col = f"fwd_{primary_horizon}d"
            if primary_col in entries.columns:
                ranked = entries.dropna(subset=[primary_col]).sort_values(primary_col, ascending=False)
                show_cols = [c for c in ["asof", "ticker", "name", "market", "rs_rank",
                                          "stage", "growth_quarters", "op_margin",
                                          "vol_acc_ratio", primary_col]
                             if c in ranked.columns]
                tl, tr = st.columns(2)
                with tl:
                    st.markdown(f"**Top 20 (+{primary_horizon}d)**")
                    st.dataframe(ranked.head(20)[show_cols].round(2),
                                  use_container_width=True, hide_index=True)
                with tr:
                    st.markdown(f"**Bottom 20 (+{primary_horizon}d)**")
                    st.dataframe(ranked.tail(20)[show_cols].round(2),
                                  use_container_width=True, hide_index=True)

            st.divider()
            st.caption(
                "⚠️ **한계**: forward return은 *시가 진입 종가 청산* 가정이라 슬리피지·세금·거래비용 미반영. "
                "실제 매매에서는 fwd return의 70-80% 정도만 기대 — *상한선* 참고."
            )
    else:
        st.info(
            "👆 기간을 지정하고 **▶ 백테스트 실행** 버튼을 누르세요. "
            "초회 실행은 1-3분 소요 (snapshot 수 × ~10초). 이후 캐시되어 즉시 표시."
        )


# ============================================================
# 💎 가속화 장기투자 (DGI) 탭
# ============================================================

@st.cache_data(show_spinner="DGI 점수 계산 중…")
def _dgi_screen_cached(_data_dir_str: str, asof: str, use_dart: bool) -> pd.DataFrame:
    """전체 KR 마스터에 대해 DGI 점수 산출. master + scoring.screen."""
    from moneygold.strategies.value_long_term import scoring as dgi_scoring
    from moneygold.strategies.value_long_term.dart_client import DartClient

    data_dir = Path(_data_dir_str)
    master_path = store.master_path(data_dir)
    master = store.read_parquet_safe(master_path)
    if master is None or master.empty:
        return pd.DataFrame()
    kr = master[master["market"].isin(["KOSPI", "KOSDAQ"])].copy()
    tickers = kr["ticker"].tolist()
    name_map = dict(zip(kr["ticker"], kr.get("name", kr["ticker"]), strict=False))

    dart = None
    if use_dart:
        try:
            dart = DartClient(load_config().dart, data_dir)
        except ValueError:
            dart = None
    return dgi_scoring.screen(tickers, asof, data_dir, dart=dart, name_map=name_map)


with tab_dgi:
    st.subheader("💎 가속화 장기투자")
    st.caption(
        f"asof **{asof_str}** — DGI(Dividend Growth Investing). "
        "배당 재투자(DRIP) + 정기 추가납입 + 주가 우상향 + 배당성장의 **복리 선순환**으로 "
        "자산을 가속화하는 종목 선별. 모멘텀 시스템(Stage/Template)과 *독립된* 별도 점수표."
    )

    with st.expander("ℹ️ 점수표 (100점 만점)", expanded=False):
        st.markdown(
            "| 카테고리 | 항목 | 배점 |\n"
            "|---|---|---|\n"
            "| **배당 (40)** | 현재 배당수익률 (>7/5/3%) | 10 |\n"
            "|  | 연속 인상 연수 (≥10/5/3) | 10 |\n"
            "|  | 5년 DPS CAGR (≥15/10/5%) | 10 |\n"
            "|  | 배당성향 안정 (20~70%) | 5 |\n"
            "|  | 분기/월 배당 빈도 | 5 |\n"
            "| **자본이득 (30)** | 5년 주가 CAGR (≥15/10/5%) | 15 |\n"
            "|  | 200일선 위 거래일 비율 (≥80/60/40%) | 10 |\n"
            "|  | 5년 총수익 양수 | 5 |\n"
            "| **펀더멘털 (20)** | 5년 ROE 평균 (≥15/12/8%) | 10 |\n"
            "|  | EPS 변동계수 (<0.20/0.30/0.50) | 10 |\n"
            "| **주주환원 (10)** | 자사주 소각 이력 | 5 |\n"
            "|  | 연간 소각 빈도 (≥0.7/0.3/yr) | 5 |\n\n"
            "**등급**: A ≥ 80점 (우량 DGI) · B ≥ 70점 (매수 고려) · C 미달.\n\n"
            "ValueTrader 75점 점수표와 달리 **PER/PBR 저평가 게이트 없음** — "
            "DGI 종목은 PER 15~25가 정상 범위.\n\n"
            "사전 데이터 동기화 필요:\n"
            "- `python -m moneygold.cli.sync --dividends` (KIS 예탁원 배당 이력)\n"
            "- `python -m moneygold.cli.sync --financials` (KIS 재무, ROE 포함)\n"
            "- `python -m moneygold.cli.sync --backfill` (일봉, 5년 이상)\n"
        )

    dgi_ctrl1, dgi_ctrl2, dgi_ctrl3, dgi_ctrl4 = st.columns([0.25, 0.25, 0.25, 0.25])
    with dgi_ctrl1:
        dgi_min_grade = st.selectbox("최소 등급", ["A", "B", "C"], index=1,
                                      key="dgi_min_grade",
                                      help="A=우량 DGI(80+), B=매수 고려(70+), C=전체")
    with dgi_ctrl2:
        dgi_min_yield = st.slider("최소 배당수익률 (%)", 0.0, 10.0, 0.0, 0.5,
                                    key="dgi_min_yield")
    with dgi_ctrl3:
        dgi_min_consec = st.slider("최소 연속 인상 (년)", 0, 15, 0, 1,
                                    key="dgi_min_consec")
    with dgi_ctrl4:
        dgi_use_dart = st.checkbox("DART 주주환원 항목 포함", value=True,
                                    help="DART_API_KEY 설정 시. 미설정이면 자동 skip.",
                                    key="dgi_use_dart")

    cfg = load_config()
    data_dir_str = str(cfg.data_dir)
    scores_dir = Path(data_dir_str) / "value_scores"
    # 캐시 정책: 정확한 asof 파일 우선, 없으면 가장 최근 파일 fallback.
    # fresh screen은 전체 2,580종목에 ~88분 걸리므로 *명시 버튼*으로만 트리거.
    cached_path = scores_dir / f"{asof_str}.parquet"
    fallback_path: Path | None = None
    if not cached_path.exists() and scores_dir.exists():
        candidates = sorted(scores_dir.glob("*.parquet"))
        if candidates:
            fallback_path = candidates[-1]

    refresh_clicked = st.button("🔄 DGI 점수 강제 재계산 (~88분, 전체 2,580종목)",
                                  key="dgi_refresh",
                                  help="현재 asof에 대해 fresh screen. 시간이 매우 길음.")
    if refresh_clicked:
        _dgi_screen_cached.clear()
        with st.spinner(f"DGI 점수 계산 중 (asof={asof_str}, ~88분)..."):
            dgi_df = _dgi_screen_cached(data_dir_str, asof_str, dgi_use_dart)
    elif cached_path.exists():
        dgi_df = store.read_parquet_safe(cached_path)
        st.caption(
            f"💾 캐시: `{cached_path.relative_to(Path(data_dir_str).parent)}` "
            f"(asof={asof_str}). 갱신: CLI `python -m moneygold.cli.dgi --screen --asof {asof_str}`"
        )
    elif fallback_path is not None:
        dgi_df = store.read_parquet_safe(fallback_path)
        fallback_asof = fallback_path.stem
        st.warning(
            f"⚠️ asof={asof_str}에 대한 DGI 캐시가 없어 가장 최근 결과 "
            f"(asof={fallback_asof})를 표시합니다. "
            f"새로 계산하려면 CLI: `python -m moneygold.cli.dgi --screen --asof {asof_str}` "
            "(또는 위 강제 재계산 버튼, ~88분)"
        )
    else:
        dgi_df = None

    if dgi_df is None or dgi_df.empty:
        st.warning(
            "DGI 데이터 없음. 다음을 먼저 실행:\n\n"
            "```bash\npython -m moneygold.cli.sync --dividends\n"
            "python -m moneygold.cli.dgi --screen --asof " + asof_str + "\n```"
        )
    else:
        # 필터
        grade_order = {"A": 3, "B": 2, "C": 1}
        filtered = dgi_df[dgi_df["grade"].map(grade_order) >= grade_order[dgi_min_grade]].copy()
        if "dividend_yield_pct" in filtered.columns:
            filtered = filtered[
                (filtered["dividend_yield_pct"].fillna(0) >= dgi_min_yield)
            ]
        if "consecutive_increase_years" in filtered.columns:
            filtered = filtered[
                (filtered["consecutive_increase_years"].fillna(0) >= dgi_min_consec)
            ]

        st.markdown(
            f"**{len(filtered)}개 종목** (A={int((filtered['grade']=='A').sum())} · "
            f"B={int((filtered['grade']=='B').sum())} · C={int((filtered['grade']=='C').sum())})"
        )

        display_cols = [
            "ticker", "name", "total", "grade",
            "dividend_yield_pct", "consecutive_increase_years", "dps_cagr_5y_pct",
            "price_cagr_5y_pct", "roe_5y_avg_pct",
            "dividend_total", "capital_total", "fundamental_total", "shareholder_total",
        ]
        avail = [c for c in display_cols if c in filtered.columns]
        st.dataframe(filtered[avail].head(100), use_container_width=True, hide_index=True,
                     column_config={
                         "total": st.column_config.NumberColumn("총점", format="%d"),
                         "dividend_yield_pct": st.column_config.NumberColumn("yield(%)", format="%.2f"),
                         "consecutive_increase_years": st.column_config.NumberColumn("연속(년)", format="%d"),
                         "dps_cagr_5y_pct": st.column_config.NumberColumn("DPS CAGR(%)", format="%.1f"),
                         "price_cagr_5y_pct": st.column_config.NumberColumn("주가 CAGR(%)", format="%.1f"),
                         "roe_5y_avg_pct": st.column_config.NumberColumn("ROE 5yr(%)", format="%.1f"),
                     })

        st.divider()

        # ----- DRIP 시뮬레이터 -----
        st.markdown("### 🌱 DRIP 시뮬레이터 (복리 가속 효과)")
        st.caption(
            "선택한 종목들에 대해 **비관 / baseline / 낙관** 3 시나리오로 N년 자산 곡선 추정. "
            "초기금 + 월 추가납입 + 배당 100% 재투자 가정. "
            "변동성 polynomial 없는 단순 CAGR 외삽이라 실제 수익률과 다름 — *순위/방향* 참고용."
        )

        drip_left, drip_right = st.columns([0.55, 0.45])
        with drip_left:
            picks = st.multiselect(
                "시뮬할 종목 (최대 5개)",
                filtered["ticker"].tolist(),
                default=filtered["ticker"].head(3).tolist(),
                format_func=lambda t: f"{t} {filtered[filtered['ticker']==t]['name'].iloc[0] if not filtered[filtered['ticker']==t].empty else ''}",
                max_selections=5,
                key="dgi_picks",
            )
        with drip_right:
            drip_c1, drip_c2 = st.columns(2)
            with drip_c1:
                init_krw = st.number_input("초기 투자금 (원)", min_value=0,
                                            value=10_000_000, step=1_000_000,
                                            key="drip_init")
                years = st.slider("보유 기간 (년)", 1, 40, 20, 1, key="drip_years")
            with drip_c2:
                monthly_krw = st.number_input("월 추가납입 (원)", min_value=0,
                                                value=500_000, step=100_000,
                                                key="drip_monthly")
                tax_pct = st.slider("배당세 (%)", 0.0, 30.0, 15.4, 0.1,
                                     key="drip_tax",
                                     help="기본 15.4%. 분리과세 대상은 9~15.4%, 종합과세는 더 높을 수 있음.")

        # 시나리오 변동성 (낙관·비관 곡선 폭)
        st.markdown("**시나리오 변동성** — baseline CAGR을 ±N%p 흔들어 낙관/비관 만듦")
        vol_c1, vol_c2, vol_c3 = st.columns([0.35, 0.35, 0.3])
        with vol_c1:
            price_vol = st.slider(
                "주가 CAGR ± (%p)", 0.0, 30.0, 0.0, 0.5, key="drip_price_vol",
                help="0이면 종목 CAGR로 자동 추정 (|CAGR|×0.5 + 5%p 최소). "
                     "한국 시장 평균 변동성이 ~15%p임을 참고.",
            )
        with vol_c2:
            dps_vol = st.slider(
                "DPS CAGR ± (%p)", 0.0, 20.0, 0.0, 0.5, key="drip_dps_vol",
                help="0이면 자동 추정 (|CAGR|×0.3 + 2%p 최소). 배당은 주가보다 부드러움.",
            )
        with vol_c3:
            show_scenarios = st.checkbox(
                "3 시나리오 표시", value=True, key="drip_show_scenarios",
                help="끄면 baseline만 (이전 단일 시나리오 모드).",
            )

        if picks:
            from moneygold.strategies.value_long_term import drip as drip_mod

            # 입력 구성 — 종목별
            sim_inputs = []
            for tk in picks:
                row = filtered[filtered["ticker"] == tk].iloc[0]
                bars = store.read_parquet_safe(store.bars_path(Path(data_dir_str), tk))
                cur_price = float(bars.iloc[-1]["close"]) if bars is not None and not bars.empty else 10_000.0
                yld = row.get("dividend_yield_pct") or 0.0
                cur_dps = cur_price * float(yld) / 100.0 if yld else 0.0
                if cur_dps <= 0:
                    continue
                sim_inputs.append(drip_mod.DripInputs(
                    ticker=tk, name=str(row.get("name") or tk), asof=asof_str,
                    initial_investment_krw=float(init_krw),
                    monthly_contribution_krw=float(monthly_krw),
                    years=int(years),
                    current_price_krw=cur_price,
                    current_annual_dps_krw=cur_dps,
                    price_cagr_pct=float(row.get("price_cagr_5y_pct") or 0.0),
                    dps_cagr_pct=float(row.get("dps_cagr_5y_pct") or 0.0),
                    tax_rate_pct=float(tax_pct),
                ))

            if not sim_inputs:
                st.info("선택한 종목 중 배당 데이터가 충분한 종목이 없습니다. 다른 종목을 선택하세요.")
            else:
                # 종목별 시나리오 결과 — {ticker: {scenario: DripResult}}
                p_vol = None if price_vol == 0.0 else price_vol
                d_vol = None if dps_vol == 0.0 else dps_vol
                results: dict[str, dict[str, "drip_mod.DripResult"]] = {}
                for inp in sim_inputs:
                    if show_scenarios:
                        results[inp.ticker] = drip_mod.simulate_scenarios(
                            inp, price_volatility_pp=p_vol, dps_volatility_pp=d_vol,
                        )
                    else:
                        results[inp.ticker] = {"baseline": drip_mod.simulate(inp)}

                # 요약 테이블 — 종목 × 시나리오 별 최종 자산
                summary_rows = []
                for tk, scen_map in results.items():
                    name = next(iter(scen_map.values())).inputs.name
                    row_dict = {"ticker": tk, "name": name}
                    for scen in ("비관", "baseline", "낙관"):
                        if scen in scen_map:
                            r = scen_map[scen]
                            row_dict[f"{scen} 최종(만원)"] = round(r.final_value_krw / 10_000)
                            row_dict[f"{scen} CAGR(%)"] = round(r.annualized_return_pct, 1)
                    if "baseline" in scen_map:
                        r = scen_map["baseline"]
                        row_dict["총투입(만원)"] = round(r.total_invested_krw / 10_000)
                        row_dict["baseline YoC(%)"] = round(r.final_yoc_pct, 2)
                    summary_rows.append(row_dict)
                st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

                # 자산 곡선 차트 — 종목 × 시나리오
                value_parts = []
                for tk, scen_map in results.items():
                    name = next(iter(scen_map.values())).inputs.name
                    for scen, r in scen_map.items():
                        line = r.timeline[["month_idx", "value"]].copy()
                        line["label"] = (
                            f"{tk} {name}" if not show_scenarios
                            else f"{tk} {name} · {scen}"
                        )
                        value_parts.append(line)
                if value_parts:
                    chart_df = pd.concat(value_parts, ignore_index=True)
                    pivot = chart_df.pivot(index="month_idx", columns="label", values="value")
                    st.line_chart(pivot, height=380,
                                   y_label="자산 가치 (원)",
                                   x_label="개월")

                # YoC 곡선 — baseline만 (시나리오 곱이면 너무 많음)
                yoc_parts = []
                for tk, scen_map in results.items():
                    if "baseline" not in scen_map:
                        continue
                    r = scen_map["baseline"]
                    line = r.timeline[["month_idx", "yoc_pct"]].copy()
                    line["label"] = f"{tk} {r.inputs.name}"
                    yoc_parts.append(line)
                if yoc_parts:
                    yoc_df = pd.concat(yoc_parts, ignore_index=True)
                    yoc_pivot = yoc_df.pivot(index="month_idx", columns="label", values="yoc_pct")
                    st.markdown("**YoC (Yield on Cost) 추이** — 매입 단가 기준 연환산 배당수익률 (baseline)")
                    st.line_chart(yoc_pivot, height=250, y_label="YoC (%)", x_label="개월")

                # 시나리오 가정 noting
                if show_scenarios:
                    sample = sim_inputs[0]
                    if p_vol is None or d_vol is None:
                        auto_p, auto_d = drip_mod.auto_volatility(
                            sample.price_cagr_pct, sample.dps_cagr_pct,
                        )
                        used_p = p_vol if p_vol is not None else auto_p
                        used_d = d_vol if d_vol is not None else auto_d
                        st.caption(
                            f"📐 변동성 자동 추정 (첫 종목 기준): 주가 ±{used_p:.1f}%p, DPS ±{used_d:.1f}%p. "
                            "종목별로 다를 수 있음 (CAGR 기반)."
                        )
                    else:
                        st.caption(f"📐 변동성: 주가 ±{p_vol}%p, DPS ±{d_vol}%p (수동 지정)")

        # ------------------------------------------------------------------
        # 📑 종목 상세 정보 (DART 사업보고서)
        # KOSPI200 + KOSDAQ150 대상으로 미리 sync. 다른 종목은 미수집 안내.
        # ------------------------------------------------------------------
        st.divider()
        st.markdown("### 📑 종목 상세 정보 (DART 사업보고서)")
        st.caption(
            "선택한 종목의 회사 개요·증자/감자 이력·자사주 흐름·재무제표 원본을 표시. "
            "KOSPI200+KOSDAQ150 약 280종목 한정으로 사전 sync 됨. "
            "(`python -m moneygold.cli.sync --dart-business --scope k200kq150` 로 갱신)"
        )

        from moneygold.data import dart_business as db_mod

        detail_ticker = st.selectbox(
            "상세 정보 종목",
            options=filtered["ticker"].tolist(),
            format_func=lambda t: (
                f"{t} {filtered[filtered['ticker']==t]['name'].iloc[0]}"
                if not filtered[filtered["ticker"] == t].empty else t
            ),
            key="dgi_detail_ticker",
        )

        if detail_ticker:
            data_dir = Path(data_dir_str)
            info = db_mod.load_company_info(data_dir, detail_ticker)
            si_df = db_mod.load_share_issuance(data_dir, detail_ticker)
            ts_df = db_mod.load_treasury_status(data_dir, detail_ticker)
            fr_df = db_mod.load_financials_raw(data_dir, detail_ticker)

            if info is None and si_df.empty and ts_df.empty and fr_df.empty:
                st.info(
                    f"⚠️ {detail_ticker}은 DART 사업보고서 데이터가 없습니다. "
                    f"KOSPI200/KOSDAQ150 대상 sync에서 제외됐을 가능성 (소형주/신규상장/외국기업)."
                )
            else:
                # 1) 회사 개요
                with st.expander("🏢 회사 개요", expanded=True):
                    if info:
                        c1, c2, c3 = st.columns(3)
                        with c1:
                            st.markdown(f"**회사명**: {info.get('corp_name','-')}")
                            st.markdown(f"**대표이사**: {info.get('ceo_nm','-')}")
                            st.markdown(f"**설립일**: {info.get('est_dt','-')}")
                        with c2:
                            st.markdown(f"**업종코드**: {info.get('induty_code','-')}")
                            st.markdown(f"**결산월**: {info.get('acc_mt','-')}")
                            st.markdown(f"**법인등록번호**: {info.get('jurir_no','-')}")
                        with c3:
                            hm = info.get("hm_url") or ""
                            ir = info.get("ir_url") or ""
                            st.markdown(f"**홈페이지**: [{hm}](https://{hm})" if hm else "**홈페이지**: -")
                            st.markdown(f"**IR**: [{ir}]({ir if ir.startswith('http') else 'https://' + ir})" if ir else "**IR**: -")
                            st.markdown(f"**연락처**: {info.get('phn_no','-')}")
                        st.caption(f"📮 {info.get('adres','-')}")
                    else:
                        st.write("회사 개요 정보 없음")

                # 2) 자사주 흐름 — 보통주 + 의미 있는 행만
                with st.expander("💼 자기주식 현황 (사업보고서 누적)", expanded=False):
                    if not ts_df.empty:
                        meaningful = ts_df.copy()
                        # 보통주 + 기초/기말 수량이 채워진 행 우선 (총계 행)
                        if "stock_knd" in meaningful.columns:
                            meaningful = meaningful[
                                meaningful["stock_knd"].astype(str).str.contains("보통", na=False)
                            ]
                        for q in ("bsis_qy", "trmend_qy"):
                            if q in meaningful.columns:
                                meaningful = meaningful[
                                    meaningful[q].astype(str).str.strip().replace("-", "") != ""
                                ]
                        meaningful = meaningful.sort_values("fiscal_year", ascending=False)
                        show_cols = [c for c in [
                            "fiscal_year", "stock_knd", "acqs_mth1", "acqs_mth2",
                            "bsis_qy", "change_qy_acqs", "change_qy_dsps", "trmend_qy",
                        ] if c in meaningful.columns]
                        if not meaningful.empty:
                            st.dataframe(meaningful[show_cols].head(20),
                                          use_container_width=True, hide_index=True)
                            st.caption(f"보통주 + 수량 채워진 행만. 전체 {len(ts_df)}행 중 {len(meaningful)}행. "
                                       "bsis_qy=기초수량, change_qy_acqs=취득량, "
                                       "change_qy_dsps=처분량, trmend_qy=기말수량.")
                        else:
                            st.dataframe(ts_df.head(20), use_container_width=True, hide_index=True)
                            st.caption(f"의미 있는 행 필터 실패. raw 표시. 총 {len(ts_df)}행.")
                    else:
                        st.write("자기주식 데이터 없음")

                # 3) 증자/감자 이력
                with st.expander("📈 증자 / 감자 이력", expanded=False):
                    if not si_df.empty:
                        # 일자 + 형식 + 종류 + 신주발행가 등 주요 컬럼
                        show_cols = [c for c in [
                            "fiscal_year", "isu_dcrs_de", "isu_dcrs_stle",
                            "isu_dcrs_de_stk_knd", "isu_dcrs_qy", "isu_dcrs_mstvdv_fval_amount",
                            "isu_dcrs_mstvdv_amount",
                        ] if c in si_df.columns]
                        # 중복 가능 — fiscal_year 다른데 같은 isu_dcrs_de
                        dedup = si_df.drop_duplicates(subset=[c for c in [
                            "isu_dcrs_de", "isu_dcrs_stle",
                        ] if c in si_df.columns], keep="last")
                        st.dataframe(dedup[show_cols].sort_values("isu_dcrs_de", ascending=False),
                                      use_container_width=True, hide_index=True)
                        st.caption(f"총 {len(si_df)}행 중 중복 제거: {len(dedup)}건. "
                                   "isu_dcrs_stle=형식 (유상증자/주식배당/무상증자 등).")
                    else:
                        st.write("증자/감자 이력 없음")

                # 4) 재무비율 (수익성·안정성·성장성·활동성) — DART fnlttSinglIndx 통합
                with st.expander("📐 재무비율 (수익성·안정성·성장성·활동성)", expanded=False):
                    from moneygold.data import dart_indicators as di_mod
                    ind_df = di_mod.load_indicators(data_dir, detail_ticker)
                    if not ind_df.empty:
                        # fiscal_year × idx_nm pivot, 분류별 그룹화
                        ind_df["idx_val"] = pd.to_numeric(ind_df["idx_val"], errors="coerce")
                        # 같은 (fiscal_year, idx_nm) 중복은 마지막 값
                        ind_df = ind_df.drop_duplicates(
                            subset=["fiscal_year", "idx_cl_code", "idx_nm"], keep="last",
                        )
                        cls_label = {
                            "M210000": "수익성", "M220000": "안정성",
                            "M230000": "성장성", "M240000": "활동성",
                        }
                        for cls_code, cls_name in cls_label.items():
                            sub = ind_df[ind_df["idx_cl_code"] == cls_code]
                            if sub.empty:
                                continue
                            # NaN 비율 너무 높은 지표는 제외 (금융업 등은 회전율 N/A 많음)
                            pivot = sub.pivot_table(
                                index="idx_nm", columns="fiscal_year",
                                values="idx_val", aggfunc="last",
                            )
                            non_nan_ratio = pivot.notna().mean(axis=1)
                            pivot = pivot[non_nan_ratio >= 0.4].sort_index(axis=1)
                            if pivot.empty:
                                continue
                            st.markdown(f"**{cls_name} ({cls_code})**")
                            st.dataframe(pivot.round(2), use_container_width=True)
                    else:
                        st.write("재무비율 데이터 없음. "
                                 "`python -m moneygold.cli.sync --dart-indicators --scope k200kq150` 로 sync.")

                # 5) 재무제표 raw — 핵심 계정만 (자본변동표 제외, 다년 비교)
                with st.expander("📊 재무제표 raw (핵심 계정 · 5년 추이)", expanded=False):
                    if not fr_df.empty and "sj_nm" in fr_df.columns and "account_nm" in fr_df.columns:
                        key_accounts = [
                            "자산총계", "부채총계", "자본총계",
                            "매출액", "영업수익", "영업이익", "당기순이익",
                            "영업활동현금흐름", "영업활동 현금흐름",
                            "투자활동현금흐름", "투자활동 현금흐름",
                            "재무활동현금흐름", "재무활동 현금흐름",
                        ]
                        # 자본변동표 제외 — 노이즈 (여러 항목별 자본총계 행 발생)
                        no_chg = fr_df[fr_df["sj_nm"] != "자본변동표"]
                        no_chg["acc_clean"] = no_chg["account_nm"].astype(str).str.strip()
                        core = no_chg[no_chg["acc_clean"].isin(key_accounts)].copy()
                        if not core.empty:
                            # 동일 fiscal_year + 계정 중복 시 첫 행 (연결 우선)
                            core["amt"] = pd.to_numeric(core["thstrm_amount"], errors="coerce")
                            pivot = core.pivot_table(
                                index="acc_clean", columns="fiscal_year",
                                values="amt", aggfunc="first",
                            ).sort_index(axis=1)
                            # 단위 변환 — 백만원
                            pivot = (pivot / 1e6).round(0).astype("Int64")
                            st.markdown("**단위: 백만원**")
                            st.dataframe(pivot, use_container_width=True)
                            st.caption(
                                f"5년 사업보고서 ({core['fiscal_year'].min()}~{core['fiscal_year'].max()}) "
                                f"기준. 자본변동표 제외 — 전체 {len(fr_df)} 항목은 "
                                "`store/dart_business/financials_raw/`."
                            )
                        else:
                            st.write("핵심 계정 데이터 없음 (계정명 매핑 확인 필요)")
                    else:
                        st.write("재무제표 raw 없음")
