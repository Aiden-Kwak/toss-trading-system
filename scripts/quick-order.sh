#!/bin/bash
# quick-order.sh
# preview → grant → place를 원샷으로 실행
# 사용법: quick-order.sh --symbol TSLA --side buy --qty 1 --price 25000 [--market kr] [--fractional --amount 1000]

set -e

export PATH="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/bin:$PATH"
export TOSSCTL_AUTH_HELPER_DIR="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/auth-helper"
export TOSSCTL_AUTH_HELPER_PYTHON="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/auth-helper/.venv/bin/python3"

# 인자 그대로 전달
ARGS="$@"

# Step 1: Preview → confirm_token 추출
PREVIEW=$(tossctl order preview $ARGS --output json 2>&1)

if echo "$PREVIEW" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('confirm_token') else 1)" 2>/dev/null; then
  TOKEN=$(echo "$PREVIEW" | python3 -c "import json,sys; print(json.load(sys.stdin)['confirm_token'])")
  LIVE_READY=$(echo "$PREVIEW" | python3 -c "import json,sys; print(json.load(sys.stdin).get('live_ready', False))")
  MUTATION_READY=$(echo "$PREVIEW" | python3 -c "import json,sys; print(json.load(sys.stdin).get('mutation_ready', False))")
else
  echo '{"error": "preview failed", "detail": '"$(echo "$PREVIEW" | python3 -c "import json,sys; json.dump(sys.stdin.read(), sys.stdout)")"'}'
  exit 1
fi

# Step 2: Grant (이미 활성 상태면 스킵)
PERM_STATUS=$(tossctl order permissions status --output json 2>/dev/null || echo '{"active": false}')
IS_ACTIVE=$(echo "$PERM_STATUS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('active', False))" 2>/dev/null || echo "False")

if [ "$IS_ACTIVE" != "True" ]; then
  tossctl order permissions grant --ttl 600 > /dev/null 2>&1
fi

# Step 3: Execute
RESULT=$(tossctl order place $ARGS --execute --dangerously-skip-permissions --confirm "$TOKEN" --output json 2>&1)

echo "$RESULT"
