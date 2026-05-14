#!/bin/bash
# 임계값 sweep — 200종목 6개월 빠른 비교.
# 결과는 /tmp/sweep_*.log + result/backtest/{name}/
#
# 시나리오:
#   A: baseline (현행 ARCHITECTURE 디폴트)
#   B: RS rank min 70→50 (Minervini 조건 8 완화)
#   C: B + BOX_VALID_MIN_DAYS 15→8 (박스 검증 단축)
#   D: C + BREAKOUT_VOLUME_MULT 1.5→1.2 (거래량 임계 완화)
#   E: D + BOX_HIGH_CONFIRM 3→2 (천장 확정 단축)

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
    2>&1 | tee "/tmp/sweep_${NAME}.log" | tail -25
}

run_scenario A
run_scenario B RS_RANK_MIN=50
run_scenario C RS_RANK_MIN=50 BOX_VALID_MIN_DAYS=8
run_scenario D RS_RANK_MIN=50 BOX_VALID_MIN_DAYS=8 BREAKOUT_VOLUME_MULT=1.2
run_scenario E RS_RANK_MIN=50 BOX_VALID_MIN_DAYS=8 BREAKOUT_VOLUME_MULT=1.2 BOX_HIGH_CONFIRM=2

echo ""
echo "==================== Summary ===================="
for s in A B C D E; do
  echo "[$s]"
  grep -E "(Trades|Total return|MDD|Win rate|Avg R|Alpha)" "/tmp/sweep_${s}.log" | head -6
  echo ""
done
