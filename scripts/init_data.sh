#!/usr/bin/env bash
# moneygold 초기 데이터 수집 (Linux / macOS)
# 첫 실행 시 ~60-100분 소요. 자동 재개 가능 (이미 받은 데이터는 skip).
# Usage:  bash scripts/init_data.sh
set -e
ENV_NAME="${MONEYGOLD_ENV:-moneygold}"

cd "$(dirname "$0")/.."

source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate "$ENV_NAME"

echo "=========================================================="
echo " moneygold 초기 데이터 수집"
echo "=========================================================="
echo ""
echo "[1/4] KIS 사전검증 ..."
python scripts/verify_kis.py || { echo "❌ KIS 검증 실패. .env의 KIS_APP_KEY/SECRET 확인."; exit 1; }

echo ""
echo "[2/4] 종목 마스터·일봉 2년 백필·지수 (~40분)"
python -m moneygold.cli.sync --backfill

echo ""
echo "[3/4] 분기 펀더멘털 (KIS finance, ~15분)"
python -m moneygold.cli.sync --financials

echo ""
echo "[4/4] 컨센서스 — 애널 목표가·EPS 추정·상향 조정 (yfinance, ~43분, 선택)"
read -r -p "    컨센서스 sync 진행? (Y/n): " yn
if [[ -z "$yn" || "$yn" =~ ^[Yy] ]]; then
    python -m moneygold.cli.sync --consensus
else
    echo "    skip — 나중에 'python -m moneygold.cli.sync --consensus'로 실행 가능."
fi

echo ""
echo "✅ 초기 데이터 수집 완료."
echo ""
echo "다음 단계:"
echo "  - 대시보드:        bash scripts/run_dashboard.sh"
echo "  - 콘솔 시그널:     python -m moneygold.cli.signals --top 30"
echo "  - 매일 운영:       bash scripts/daily.sh"
