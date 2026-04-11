---
name: toss-login
description: 토스증권 로그인, 세션 상태 확인, 로그아웃 등 인증 관리
user_invocable: true
---

# toss-login: 토스증권 인증 관리

토스증권 CLI(`tossctl`)의 로그인/세션 관리를 수행합니다.

## 환경변수 (모든 tossctl 명령 실행 전 설정 필수)

```bash
export PATH="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/bin:$PATH"
export TOSSCTL_AUTH_HELPER_DIR="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/auth-helper"
export TOSSCTL_AUTH_HELPER_PYTHON="/Users/aiden-kwak/Desktop/Personal/Stock/tossinvest-cli/auth-helper/.venv/bin/python3"
```

## 워크플로우

### 1. 세션 상태 확인

먼저 현재 세션 상태를 확인합니다:

```bash
tossctl auth status
```

- 세션이 유효하면 "session is active" 표시
- 세션이 없거나 만료되면 로그인 필요

### 2. 로그인

세션이 없는 경우 Bash 도구의 `run_in_background: true` 옵션으로 로그인을 실행합니다.

**반드시 백그라운드로 실행하세요.** 이 명령은 브라우저를 열고 QR 로그인 완료까지 대기하는 interactive 명령입니다.
- 포그라운드(일반 Bash)로 실행하면 세션이 블로킹되므로 절대 금지
- `run_in_background: true`로 실행하면 브라우저가 열리고, 사용자가 QR 스캔 후 완료 알림을 받음

```bash
# run_in_background: true, timeout: 300000 으로 실행
tossctl auth login
```

사용자에게 "브라우저가 열립니다. 토스 앱으로 QR 코드를 스캔해주세요."라고 안내한 뒤, 완료 알림을 기다립니다.
완료 후 `tossctl auth status`로 세션 확인.

### 3. 인증 환경 진단

문제가 있을 때:

```bash
tossctl auth doctor
```

Python, Chrome, Playwright, auth-helper 모듈 상태를 확인합니다.

### 4. 로그아웃

```bash
tossctl auth logout
```

### 5. 전체 시스템 진단

```bash
tossctl doctor
```

## 사용자 응답 가이드

- 로그인 성공 시: 계좌 요약을 보여줄지 물어보세요
- 로그인 실패 시: `auth doctor` 결과를 확인하고 문제를 안내하세요
- 세션 만료 시: 재로그인을 안내하세요
