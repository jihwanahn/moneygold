#!/usr/bin/env bash
# moneygold 초기 셋업 (Linux / macOS)
# Usage:  bash scripts/setup.sh
set -e
ENV_NAME="${MONEYGOLD_ENV:-moneygold}"

cd "$(dirname "$0")/.."

if ! command -v conda &>/dev/null; then
  echo "❌ conda 명령을 찾을 수 없습니다. Miniconda/Anaconda 설치 후 다시 실행하세요."
  echo "    https://docs.conda.io/en/latest/miniconda.html"
  exit 1
fi

source "$(conda info --base)/etc/profile.d/conda.sh"

if ! conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  echo "▶ Creating conda env '$ENV_NAME' (Python 3.11) ..."
  conda create -n "$ENV_NAME" python=3.11 -y
else
  echo "✓ conda env '$ENV_NAME' already exists."
fi

conda activate "$ENV_NAME"

echo "▶ Installing project (editable) with dev + ui extras ..."
pip install -e ".[dev,ui]"

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "✓ .env created from .env.example"
  echo ""
  echo "📝 다음으로 .env 파일을 열어 ⭐ 표시된 4줄을 채우세요:"
  echo "     KRX_ID, KRX_PW, KIS_APP_KEY, KIS_APP_SECRET"
  echo ""
  echo "   에디터 예: nano .env   또는   code .env"
else
  echo "✓ .env already exists — 그대로 사용합니다."
fi

echo ""
echo "▶ KIS 사전검증 실행 (실패해도 .env 채운 후 다시 실행 가능):"
echo "    python scripts/verify_kis.py"
echo ""
echo "✅ Setup 완료. 다음 단계: bash scripts/init_data.sh (~60분)"
