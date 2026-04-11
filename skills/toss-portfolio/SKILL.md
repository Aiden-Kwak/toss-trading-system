---
name: toss-portfolio
description: 토스증권 계좌 목록, 요약, 포트폴리오 포지션, 배분, 미체결/체결 주문 조회
user_invocable: true
---

# toss-portfolio: 포트폴리오 조회

토스증권 계좌 및 포트폴리오 정보를 조회합니다. 읽기 전용이므로 안전합니다.

## 환경변수 (모든 tossctl 명령 실행 전 설정 필수)

```bash
export PATH="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/bin:$PATH"
export TOSSCTL_AUTH_HELPER_DIR="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/auth-helper"
export TOSSCTL_AUTH_HELPER_PYTHON="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/auth-helper/.venv/bin/python3"
```

## 사전 조건

- `tossctl auth status`로 세션이 활성 상태인지 확인
- 세션이 없으면 `/toss-login` skill을 먼저 실행

## 명령어

### 계좌 목록

```bash
tossctl account list --output json
```

### 계좌 요약 (잔고, 수익률 등)

```bash
tossctl account summary --output json
```

### 보유 종목 (포지션)

```bash
tossctl portfolio positions --output json
```

US 주식은 USD 환산가도 병기됩니다.

### 포트폴리오 배분

```bash
tossctl portfolio allocation --output json
```

### 미체결 주문 목록

```bash
tossctl orders list --output json
```

### 체결 내역

```bash
# 미국주식 체결 내역
tossctl orders completed --market us --output json

# 한국주식 체결 내역
tossctl orders completed --market kr --output json

# 전체
tossctl orders completed --market all --output json
```

### 특정 주문 상세

```bash
tossctl order show <주문ID> --output json
```

### 관심 종목

```bash
tossctl watchlist list --output json
```

## 워크플로우

1. 사용자가 포트폴리오 조회를 요청하면, 먼저 `auth status`로 세션 확인
2. `--output json`으로 데이터를 받아 깔끔하게 정리하여 보여주기
3. 필요하면 `--output table`로 테이블 형식도 가능
4. 사용자에게 추가 조회(시세, 주문 등)를 제안

## 출력 형식

- `--output json`: 프로그래밍적 처리에 적합
- `--output table`: 사람이 읽기 좋은 형식 (기본값)
- `--output csv`: CSV 형식
