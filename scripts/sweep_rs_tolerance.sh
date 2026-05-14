#!/bin/bash
# RS slope tolerance sweep — 200종목 6개월 빠른 비교.
# Stage 2의 rs_slope > -tolerance 조건. 강세장에서 시장 동조 종목도 포함.

set -e
source /home/jhwn/anaconda3/etc/profile.d/conda.sh
conda activate moneygold

START=20251101
END=20260514
LIMIT=200

run_scenario() {
  local NAME=$1
  shift
  echo ""
  echo "==================== $NAME ===================="
  env "$@" python -m moneygold.cli.backtest \
    --start $START --end $END --limit $LIMIT --equity 10000000 \
    2>&1 | tee "/tmp/sweep_${NAME}.log" | tail -28
}

# 베이스 (tolerance=0): 기존 Weinstein 그대로
run_scenario F0 RS_SLOPE_TOLERANCE=0.0

# tolerance 단계적 완화
run_scenario F1 RS_SLOPE_TOLERANCE=0.001
run_scenario F2 RS_SLOPE_TOLERANCE=0.003
run_scenario F3 RS_SLOPE_TOLERANCE=0.005

# tolerance + 다른 완화 조합
run_scenario F4 RS_SLOPE_TOLERANCE=0.003 RS_RANK_MIN=50 BOX_VALID_MIN_DAYS=10

echo ""
echo "==================== Summary ===================="
for s in F0 F1 F2 F3 F4; do
  echo "[$s]"
  grep -E "(Trades|Total return|MDD|Win rate|Avg R|Alpha)" "/tmp/sweep_${s}.log" | head -6
  echo ""
done
