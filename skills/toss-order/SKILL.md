---
name: toss-order
description: 토스증권 주문 실행 - 매수, 매도, 소수점 매수, 주문 취소, 주문 정정. 실거래이므로 반드시 사용자 확인 필요
user_invocable: true
---

# toss-order: 주식 주문

토스증권에서 주식을 매수/매도/취소/정정합니다.

**경고: 이 skill은 실제 돈이 오가는 거래를 실행합니다. 반드시 사용자에게 확인을 받으세요.**

## 환경변수 (모든 tossctl 명령 실행 전 설정 필수)

```bash
export PATH="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/bin:$PATH"
export TOSSCTL_AUTH_HELPER_DIR="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/auth-helper"
export TOSSCTL_AUTH_HELPER_PYTHON="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/auth-helper/.venv/bin/python3"
```

## 사전 조건

1. 세션 활성 상태 확인: `tossctl auth status`
2. config.json에서 필요한 거래 권한이 활성화되어 있어야 함

### config.json 거래 권한 활성화

config 파일 위치: `~/Library/Application Support/tossctl/config.json`

필요한 권한별 설정:

| 거래 유형 | 필요 설정 |
|-----------|-----------|
| 미국주식 매수 | `grant`, `place`, `allow_live_order_actions` = true |
| 미국주식 매도 | 위 + `sell` = true |
| 한국주식 거래 | 위 + `kr` = true |
| 소수점 매수(US) | `grant`, `place`, `fractional`, `allow_live_order_actions` = true |
| 주문 취소 | `cancel`, `allow_live_order_actions` = true |
| 주문 정정 | `amend`, `allow_live_order_actions` = true |

## 워크플로우 (반드시 이 순서를 따르세요)

### Step 1: config 확인 및 권한 활성화

```bash
tossctl config show
```

필요한 권한이 꺼져 있으면 config.json을 수정합니다. 수정 전 반드시 사용자에게 확인:

> "거래를 위해 config에서 [place/sell/kr 등] 권한을 활성화해야 합니다. 진행할까요?"

### Step 2: 주문 미리보기 (preview)

**절대로 preview 없이 주문을 실행하지 마세요.**

```bash
# 미국주식 지정가 매수
tossctl order preview \
  --symbol TSLA --side buy --qty 1 --price 25000 \
  --output json

# 미국주식 지정가 매도
tossctl order preview \
  --symbol TSLA --side sell --qty 1 --price 30000 \
  --output json

# 미국주식 소수점 매수 (금액 기반, 시장가)
tossctl order preview \
  --symbol TSLA --side buy --fractional --amount 10000 --qty 0 \
  --output json

# 한국주식 지정가 매수
tossctl order preview \
  --symbol 005930 --market kr --side buy --qty 1 --price 55000 \
  --output json

# 한국주식 지정가 매도
tossctl order preview \
  --symbol 005930 --market kr --side sell --qty 1 --price 60000 \
  --output json
```

preview 결과에서 `confirm_token`을 추출합니다.

### Step 3: 거래 권한 부여 (TTL)

```bash
tossctl order permissions grant --ttl 300
```

300초(5분) 동안 거래 권한이 부여됩니다.

### Step 4: 사용자 최종 확인

preview 결과를 사용자에게 보여주고 **반드시** 확인을 받으세요:

> "다음 주문을 실행합니다:
> - 종목: TSLA
> - 매수/매도: 매수
> - 수량: 1주
> - 가격: 25,000원
> 
> 진행하시겠습니까?"

### Step 5: 주문 실행

```bash
tossctl order place \
  --symbol TSLA --side buy --qty 1 --price 25000 \
  --execute --dangerously-skip-permissions --confirm <token> \
  --output json
```

`<token>`은 Step 2의 preview에서 받은 `confirm_token`입니다.

### 주문 취소

```bash
# 미체결 주문 확인
tossctl orders list --output json

# 취소
tossctl order cancel \
  --order-id <주문ID> --symbol <종목> \
  --execute --dangerously-skip-permissions --confirm <token> \
  --output json
```

### 주문 정정

```bash
tossctl order amend \
  --order-id <주문ID> \
  --quantity <새수량> --price <새가격> \
  --execute --dangerously-skip-permissions --confirm <token> \
  --output json
```

## 안전 규칙

1. **preview 없이 절대 주문하지 않습니다**
2. **사용자 확인 없이 절대 주문하지 않습니다**
3. **config 권한 변경 시 사용자 확인을 받습니다**
4. **매도 주문은 특히 신중하게 - 보유 수량을 먼저 확인합니다**
5. **금액/수량/가격을 사용자에게 명확히 보여줍니다**
6. **소수점 매수는 시장가(market order)로 실행됨을 안내합니다**

## 가격 참고

- 미국주식: 가격은 KRW 기준 (내부적으로 USD 변환)
- 한국주식: 가격은 KRW 기준
- 소수점 매수: 금액(KRW) 기반, 시장가 주문
