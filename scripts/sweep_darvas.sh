#!/bin/bash
# Darvas 원전 충실 파라미터 sweep — 전체 종목 6.5개월.
# 단계적으로 완화해서 어느 변경이 가장 효과적인지 확인.

set -e
source /home/jhwn/anaconda3/etc/profile.d/conda.sh
conda activate moneygold

START=20251101
END=20260514

run_scenario() {
  local NAME=$1
  shift
  echo ""
  echo "==================== Darvas $NAME ===================="
  env "$@" python -m moneygold.cli.backtest \
    --start $START --end $END --equity 10000000 \
    2>&1 | tee "/tmp/sweep_darvas_${NAME}.log" | tail -25
}

# G0: 현 디폴트 (vol 1.5, valid 15, confirm 3)
run_scenario G0

# G1: 거래량 임계만 제거 (Darvas 원전에 없음)
run_scenario G1 BREAKOUT_VOLUME_MULT=1.0

# G2: G1 + 박스 검증 15→5
run_scenario G2 BREAKOUT_VOLUME_MULT=1.0 BOX_VALID_MIN_DAYS=5

# G3: G2 + 천장 확정 3→1
run_scenario G3 BREAKOUT_VOLUME_MULT=1.0 BOX_VALID_MIN_DAYS=5 BOX_HIGH_CONFIRM=1

# G4: G3 + 박스 검증 5→3 (Darvas 원전 최대 충실)
run_scenario G4 BREAKOUT_VOLUME_MULT=1.0 BOX_VALID_MIN_DAYS=3 BOX_HIGH_CONFIRM=1

echo ""
echo "==================== Summary ===================="
for s in G0 G1 G2 G3 G4; do
  echo "[$s]"
  grep -E "(Trades|Total return|MDD|Win rate|Avg R|Alpha)" "/tmp/sweep_darvas_${s}.log" | head -6
  echo ""
done
