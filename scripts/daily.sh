#!/usr/bin/env bash
# moneygold 일일 운영 (Linux / macOS)
# 일봉·지수 incremental sync + 워치리스트 JSON export.
# cron 예: 0 17 * * 1-5  cd /path/to/moneygold && bash scripts/daily.sh >> store/logs/daily.log 2>&1
set -e
ENV_NAME="${MONEYGOLD_ENV:-moneygold}"

cd "$(dirname "$0")/.."
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate "$ENV_NAME"

mkdir -p store/logs

echo "[$(date +'%F %T')] sync --daily"
python -m moneygold.cli.sync --daily

echo "[$(date +'%F %T')] signals --export"
python -m moneygold.cli.signals --export --top 50

echo "[$(date +'%F %T')] Done."
