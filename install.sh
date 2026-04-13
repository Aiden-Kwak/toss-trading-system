#!/bin/bash
set -e

echo "=== toss-trading-system 설치 ==="
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="$HOME/.claude/skills"
STOCK_DIR="$HOME/Desktop/Personal/Stock"

# 1. tossinvest-cli 클론 & 빌드
echo "[1/5] tossinvest-cli 설치..."
if [ ! -d "$STOCK_DIR/tossinvest-cli" ]; then
  git clone https://github.com/JungHoonGhae/tossinvest-cli.git "$STOCK_DIR/tossinvest-cli"
fi

if ! command -v go &> /dev/null; then
  echo "Go가 필요합니다. 설치 중..."
  brew install go
fi

cd "$STOCK_DIR/tossinvest-cli"
make build
echo "  -> tossctl 빌드 완료: $STOCK_DIR/tossinvest-cli/bin/tossctl"

# 2. auth-helper 설치
echo "[2/5] auth-helper 설치..."
cd "$STOCK_DIR/tossinvest-cli/auth-helper"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -e . --quiet
playwright install chromium --quiet 2>/dev/null || true
deactivate
echo "  -> auth-helper 설치 완료"

# 3. toss-* 스킬 설치
echo "[3/5] Claude Code 스킬 설치..."
for skill_dir in "$SCRIPT_DIR"/skills/toss-*; do
  skill_name=$(basename "$skill_dir")
  mkdir -p "$SKILLS_DIR/$skill_name"
  cp -r "$skill_dir"/* "$SKILLS_DIR/$skill_name/"
  echo "  -> $skill_name 설치"
done
echo "  -> 스킬 설치 완료"

# 4. .zshrc 환경변수 추가
echo "[4/5] 환경변수 설정..."
if ! grep -q "tossinvest-cli" "$HOME/.zshrc" 2>/dev/null; then
  cat >> "$HOME/.zshrc" << 'EOF'

# tossinvest-cli (tossctl)
export PATH="$HOME/Desktop/Auto-trader/tossinvest-cli/bin:$PATH"
export TOSSCTL_AUTH_HELPER_DIR="$HOME/Desktop/Auto-trader/tossinvest-cli/auth-helper"
export TOSSCTL_AUTH_HELPER_PYTHON="$HOME/Desktop/Auto-trader/tossinvest-cli/auth-helper/.venv/bin/python3"
EOF
  echo "  -> .zshrc에 환경변수 추가"
else
  echo "  -> 이미 설정됨, 스킵"
fi

# 5. tossctl config 초기화
echo "[5/5] tossctl config 초기화..."
export PATH="$STOCK_DIR/tossinvest-cli/bin:$PATH"
export TOSSCTL_AUTH_HELPER_DIR="$STOCK_DIR/tossinvest-cli/auth-helper"
export TOSSCTL_AUTH_HELPER_PYTHON="$STOCK_DIR/tossinvest-cli/auth-helper/.venv/bin/python3"

if [ ! -f "$HOME/Library/Application Support/tossctl/config.json" ]; then
  tossctl config init
  echo "  -> config.json 생성"
else
  echo "  -> config.json 이미 존재, 스킵"
fi

echo ""
echo "=== 설치 완료! ==="
echo ""
echo "다음 단계:"
echo "  1. 터미널을 재시작하거나: source ~/.zshrc"
echo "  2. 로그인: tossctl auth login"
echo "  3. Claude Code에서: /toss-daytrade"
echo ""
echo "사용 가능한 스킬:"
echo "  /toss-login      - 토스증권 로그인"
echo "  /toss-portfolio   - 포트폴리오 조회"
echo "  /toss-quote       - 시세 조회"
echo "  /toss-order       - 주문 실행"
echo "  /toss-export      - CSV 내보내기"
echo "  /toss-daytrade    - 통합 단타 시스템"
