---
name: toss-export
description: 토스증권 포지션/주문 내역을 CSV 파일로 내보내기
user_invocable: true
---

# toss-export: CSV 내보내기

토스증권의 보유 종목이나 체결 내역을 CSV 파일로 내보냅니다.

## 환경변수 (모든 tossctl 명령 실행 전 설정 필수)

```bash
export PATH="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/bin:$PATH"
export TOSSCTL_AUTH_HELPER_DIR="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/auth-helper"
export TOSSCTL_AUTH_HELPER_PYTHON="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/auth-helper/.venv/bin/python3"
```

## 사전 조건

- `tossctl auth status`로 세션 확인

## 명령어

### 보유 종목 내보내기

```bash
# 미국주식
tossctl export positions --market us

# 한국주식
tossctl export positions --market kr

# 전체
tossctl export positions --market all
```

### 체결 내역 내보내기

```bash
# 미국주식 체결 내역
tossctl export orders --market us

# 한국주식 체결 내역
tossctl export orders --market kr

# 전체
tossctl export orders --market all
```

## 워크플로우

1. 사용자가 내보내기를 요청하면 시장(us/kr/all)을 확인
2. export 명령 실행
3. 생성된 CSV 파일 경로를 안내
4. 필요시 CSV 데이터를 읽어서 분석 제안
