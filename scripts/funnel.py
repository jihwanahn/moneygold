"""시그널 깔때기 (funnel) 진단 — 어느 게이트가 종목을 가장 많이 거르는지.

asof 기준일에 모든 종목을 순서대로 통과시키며 각 게이트에서 살아남은 종목 수를 카운트.
gate 순서:
  1. 데이터 충분 (>=252봉)
  2. MCAP 게이트
  3. 유동성 게이트
  4. Stage 2 (Weinstein)
  5. Template 조건 1
  6. Template 조건 2
  ... (8개 각각)
  13. Template 8/8 통과
  14. Darvas BREAKOUT (TODAY 또는 GAP)

사용:
    python scripts/funnel.py                          # 오늘
    python scripts/funnel.py --asof 20260514
    python scripts/funnel.py --asof 20260514 --markets KOSPI KOSDAQ
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from moneygold import darvas, indicators as ind, signals as sg, stage as stg, template as tmpl  # noqa: E402
from moneygold.config import load_config  # noqa: E402
from moneygold.data import store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", help="기준일 YYYYMMDD")
    parser.add_argument("--markets", nargs="+", default=["KOSPI", "KOSDAQ"])
    args = parser.parse_args()

    cfg = load_config()
    s = cfg.strategy
    u = cfg.universe
    asof = args.asof or datetime.now().strftime("%Y%m%d")
    data_dir = Path(cfg.data_dir)

    master = store.read_parquet_safe(store.master_path(data_dir))
    if master is None:
        print("마스터 없음", file=sys.stderr)
        return 1
    master = master[master["market"].isin(args.markets)]

    stage_params = stg.StageParams(
        ma_length=s.stage_ma_length, slope_lookback=s.stage_slope_lookback,
        slope_threshold_pct=s.stage_slope_threshold_pct, band_pct=s.stage_band_pct,
        ma_type=s.stage_ma_type,
    )
    box_params = darvas.BoxParams(
        box_high_lookback=s.box_high_lookback, box_high_confirm=s.box_high_confirm,
        box_height_max_pct=s.box_height_max_pct, box_valid_min_days=s.box_valid_min_days,
        box_stale_days=s.box_stale_days,
        breakout_buffer=s.breakout_buffer, breakout_volume_mult=s.breakout_volume_mult,
    )

    # 사전 계산: 모든 종목의 rs_momentum + 시장별 rs_rank
    print(f"Computing rs_rank for {len(master)} tickers ...", file=sys.stderr)
    rs_records = []
    bars_cache: dict[str, pd.DataFrame] = {}
    for tk in tqdm(master["ticker"], desc="rs", unit="tk"):
        b = store.read_parquet_safe(store.bars_path(data_dir, tk))
        if b is None or b.empty: continue
        b = b[b["date"] <= asof].sort_values("date").reset_index(drop=True)
        if len(b) < 253: continue
        bars_cache[tk] = b
        rs_records.append({
            "ticker": tk, "market": master.loc[master["ticker"] == tk, "market"].iloc[0],
            "rs_mom": ind.rs_momentum(b["close"].astype(float)),
        })
    rs_df = pd.DataFrame(rs_records)
    rs_df["rs_rank"] = float("nan")
    for market, g in rs_df.groupby("market"):
        rs_df.loc[g.index, "rs_rank"] = ind.rs_rank(g["rs_mom"]).values
    rs_rank_map = dict(zip(rs_df["ticker"], rs_df["rs_rank"]))

    # 깔때기 카운트
    n_total = len(master)
    counters = {
        "0_universe": 0,
        "1_has_bars_252": 0,
        "2_mcap_pass": 0,
        "3_liquidity_pass": 0,
        "4_stage2": 0,
        "5_template_c1": 0,
        "6_template_c2": 0,
        "7_template_c3": 0,
        "8_template_c4": 0,
        "9_template_c5": 0,
        "10_template_c6": 0,
        "11_template_c7": 0,
        "12_template_c8": 0,
        "13_template_all_8": 0,
        "14_darvas_breakout_today_or_gap": 0,
    }

    # cumulative template breakdowns (8조건이 *동시에* 통과하는 비율)
    cum_template = {f"cum_c{i}": 0 for i in range(1, 9)}

    print(f"\nasof {asof}, universe {len(master)} tickers", file=sys.stderr)
    counters["0_universe"] = n_total
    for tk in tqdm(master["ticker"], desc="funnel", unit="tk"):
        b = bars_cache.get(tk)
        if b is None: continue
        counters["1_has_bars_252"] += 1

        # MCAP proxy
        avg_value_20 = float(b["value"].tail(20).mean()) if "value" in b.columns else 0.0
        mcap_proxy = avg_value_20 * 50
        if mcap_proxy < u.mcap_min_krw: continue
        counters["2_mcap_pass"] += 1

        if avg_value_20 < u.liquidity_min_krw: continue
        counters["3_liquidity_pass"] += 1

        # Stage 2
        series = stg.classify_stage_series(b["close"].astype(float), stage_params)
        if int(series.iloc[-1]) != stg.STAGE_ADVANCING: continue
        counters["4_stage2"] += 1

        # Template 8 조건
        rs_v = float(rs_rank_map.get(tk, float("nan")))
        t = tmpl.check_template(
            b["close"].astype(float), rs_v,
            sma200_slope_lookback=s.sma200_slope_lookback,
            rs_rank_min=float(s.rs_rank_min),
            high_series=b["high"].astype(float),
            low_series=b["low"].astype(float),
        )
        # 개별 통과 카운트
        for i, c in enumerate(t.checks, start=1):
            if c:
                counters[f"{4+i}_template_c{i}"] += 1
        # 누적 통과 (1∧2∧...∧i)
        cum = True
        for i, c in enumerate(t.checks, start=1):
            cum = cum and c
            if cum:
                cum_template[f"cum_c{i}"] += 1
        if not t.passed: continue
        counters["13_template_all_8"] += 1

        # Darvas
        box = darvas.current_box(b, box_params)
        if box.is_breakout:
            counters["14_darvas_breakout_today_or_gap"] += 1

    # 출력
    print(f"\n=== Signal Funnel (asof {asof}, markets={args.markets}) ===\n")
    print(f"{'Gate':<45} {'Count':>7}  {'Pass%':>6}  {'Cum%':>6}")
    print("-" * 70)
    prev = n_total
    for k, v in counters.items():
        pct = v / prev * 100 if prev > 0 else 0
        cum = v / n_total * 100 if n_total > 0 else 0
        print(f"{k:<45} {v:>7}  {pct:>5.1f}%  {cum:>5.2f}%")
        prev = v

    print(f"\n=== Template 누적 통과 (조건 1∧2∧...∧N) ===\n")
    for k, v in cum_template.items():
        pct = v / n_total * 100 if n_total > 0 else 0
        print(f"  {k:<10}  {v:>5}  {pct:>5.2f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
