#!/bin/bash
# guard-protected-stocks.sh
# Claude Code PreToolUse hook: Bash 도구로 tossctl order 실행 시 보호 종목 체크
# exit 0 = 허용, exit 2 = 차단 (stderr 메시지가 Claude에게 전달됨)

PROTECTED_FILE="$HOME/Library/Application Support/tossctl/protected-stocks.json"

# stdin에서 JSON 읽기
INPUT=$(cat)

# jq가 없으면 python3 사용
if command -v jq &> /dev/null; then
  COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
else
  COMMAND=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null)
fi

# 빈 명령이면 통과
[ -z "$COMMAND" ] && exit 0

# tossctl order place/cancel/amend 명령이 아니면 통과
echo "$COMMAND" | grep -qE 'tossctl\s+order\s+(place|cancel|amend)' || exit 0

# protected-stocks.json이 없으면 통과
[ ! -f "$PROTECTED_FILE" ] && exit 0

# --symbol 값 추출 (macOS grep 호환)
SYMBOL=$(echo "$COMMAND" | sed -n 's/.*--symbol[[:space:]]\+\([^[:space:]]\+\).*/\1/p' | tr '[:lower:]' '[:upper:]')

# sed로 못 찾으면 다른 방식 시도
if [ -z "$SYMBOL" ]; then
  SYMBOL=$(echo "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
m = re.search(r'--symbol\s+(\S+)', cmd)
print(m.group(1).upper() if m else '')
" 2>/dev/null)
fi

[ -z "$SYMBOL" ] && exit 0

# --side 값 추출
SIDE=$(echo "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
m = re.search(r'--side\s+(\S+)', cmd)
print(m.group(1).lower() if m else '')
" 2>/dev/null)

# 보호 종목 체크
RESULT=$(python3 -c "
import json, sys

symbol = '$SYMBOL'
side = '$SIDE'

with open('$PROTECTED_FILE') as f:
    data = json.load(f)

for s in data.get('stocks', []):
    if s['symbol'].upper() == symbol:
        actions = s.get('protected_actions', ['buy', 'sell'])
        if side in actions or (not side):
            name = s.get('name', symbol)
            reason = s.get('reason', 'protected')
            print(f'BLOCKED: {symbol} ({name}) 은(는) 보호 종목입니다. 사유: {reason}')
            print(f'보호 범위: {actions}')
            print(f'/toss-protect 로 보호 설정을 변경할 수 있습니다.')
            sys.exit(1)
" 2>/dev/null)

if [ $? -eq 1 ]; then
  echo "$RESULT" >&2
  exit 2
fi

exit 0
