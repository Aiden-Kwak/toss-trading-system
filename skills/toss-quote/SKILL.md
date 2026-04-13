---
name: toss-quote
description: 주식 시세 조회 (단일/다종목) - 미국주식, 한국주식 모두 지원
user_invocable: true
---

# toss-quote: 주식 시세 조회

토스증권에서 실시간 시세를 조회합니다. 읽기 전용이므로 안전합니다.

## 환경변수 (모든 tossctl 명령 실행 전 설정 필수)

```bash
export PATH="/Users/aiden/Desktop/Auto-trader/tossinvest-cli/bin:$PATH"
export TOSSCTL_AUTH_HELPER_DIR="/Users/aiden/Desktop/Auto-trader/tossinvest-cli/auth-helper"
export TOSSCTL_AUTH_HELPER_PYTHON="/Users/aiden/Desktop/Auto-trader/tossinvest-cli/auth-helper/.venv/bin/python3"
```

## 사전 조건

- `tossctl auth status`로 세션 확인
- 세션이 없으면 `/toss-login` 먼저 실행

## 명령어

### 단일 종목 시세

```bash
# 미국주식 (티커)
tossctl quote get TSLA --output json
tossctl quote get AAPL --output json

# 한국주식 (종목코드)
tossctl quote get 005930 --output json   # 삼성전자
tossctl quote get 000660 --output json   # SK하이닉스
```

### 다종목 시세 (한번에 여러 종목)

```bash
tossctl quote batch TSLA AAPL GOOG MSFT --output table
tossctl quote batch 005930 000660 035420 --output table
tossctl quote batch TSLA 005930 GOOG VOO --output table
```

## 종목 코드 가이드

| 시장 | 형식 | 예시 |
|------|------|------|
| 미국 (NYSE/NASDAQ) | 티커 심볼 | AAPL, TSLA, GOOG, MSFT |
| 한국 (KOSPI/KOSDAQ) | 6자리 숫자 코드 | 005930 (삼성전자), 000660 (SK하이닉스) |

## 워크플로우

1. 사용자가 종목명을 말하면 해당 티커/코드로 변환
2. `quote get` 또는 `quote batch`로 시세 조회
3. 결과를 깔끔하게 정리하여 보여주기
4. 필요시 매수/매도 제안 (사용자 요청 시에만)

## 자주 조회하는 종목 예시

- 미국: AAPL, TSLA, GOOG, MSFT, AMZN, NVDA, META, VOO, QQQ, SPY
- 한국: 005930(삼성전자), 000660(SK하이닉스), 035420(NAVER), 035720(카카오)
