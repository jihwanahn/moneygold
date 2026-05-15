#!/usr/bin/env bash
# moneygold 대시보드 (Linux / macOS)
# Usage:  bash scripts/run_dashboard.sh
set -e
ENV_NAME="${MONEYGOLD_ENV:-moneygold}"

cd "$(dirname "$0")/.."
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate "$ENV_NAME"

if [ ! -f store/meta/master.parquet ]; then
    echo "⚠ 마스터 데이터가 없습니다. 먼저: bash scripts/init_data.sh"
    exit 1
fi

echo "▶ Starting Streamlit dashboard at http://localhost:8501 ..."
echo "    (종료: Ctrl+C)"
streamlit run src/moneygold/app/streamlit_app.py
