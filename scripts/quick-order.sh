#!/bin/bash
# quick-order.sh
# preview → grant → place를 원샷으로 실행
# 사용법: quick-order.sh --symbol TSLA --side buy --qty 1 [--price 25000] [--market kr]

export PATH="/Users/aiden/Desktop/Auto-trader/tossinvest-cli/bin:$PATH"
export TOSSCTL_AUTH_HELPER_DIR="/Users/aiden/Desktop/Auto-trader/tossinvest-cli/auth-helper"
export TOSSCTL_AUTH_HELPER_PYTHON="/Users/aiden/Desktop/Auto-trader/tossinvest-cli/auth-helper/.venv/bin/python3"

ARGS="$@"

# Step 1: Preview → confirm_token 추출
PREVIEW=$(tossctl order preview $ARGS --output json 2>&1)

TOKEN=$(echo "$PREVIEW" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    t = d.get('confirm_token', '')
    if t:
        print(t)
        sys.exit(0)
    else:
        print(json.dumps({'error': 'preview failed', 'detail': 'no confirm_token', 'raw': d}), file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(json.dumps({'error': 'preview failed', 'detail': str(e)}), file=sys.stderr)
    sys.exit(1)
" 2>/tmp/quick-order-err.txt)

if [ $? -ne 0 ]; then
  cat /tmp/quick-order-err.txt 2>/dev/null || echo '{"error": "preview failed", "detail": "unknown"}'
  exit 1
fi

# Step 2: Grant (이미 활성 상태면 스킵)
PERM_STATUS=$(tossctl order permissions status --output json 2>/dev/null || echo '{"active": false}')
IS_ACTIVE=$(echo "$PERM_STATUS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('active', False))" 2>/dev/null || echo "False")

if [ "$IS_ACTIVE" != "True" ]; then
  tossctl order permissions grant --ttl 600 > /dev/null 2>&1
fi

# Step 3: Execute (set +e로 에러 캡처)
RESULT=$(tossctl order place $ARGS --execute --dangerously-skip-permissions --confirm "$TOKEN" --output json 2>&1) || true

# 결과 출력
if [ -z "$RESULT" ]; then
  echo '{"error": "place failed", "detail": "empty response"}'
  exit 1
fi

# JSON 유효성 확인
if echo "$RESULT" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null; then
  echo "$RESULT"
else
  echo "$RESULT" | python3 -c "
import json, sys
raw = sys.stdin.read().strip()
print(json.dumps({'error': 'place failed', 'detail': raw}))
" 2>/dev/null || echo '{"error": "place failed", "detail": "unknown"}'
  exit 1
fi
