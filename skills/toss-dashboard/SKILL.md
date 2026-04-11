---
name: toss-dashboard
description: 토스증권 트레이딩 대시보드 실행 - 포트폴리오, 시그널, 보호종목, 매수평가를 웹 UI로 관리
user_invocable: true
disable-model-invocation: true
---

# toss-dashboard: 트레이딩 대시보드

웹 기반 대시보드를 실행하여 트레이딩 시스템을 시각적으로 관리합니다.

## 실행 방법

대시보드 서버를 백그라운드로 실행하고 브라우저를 엽니다:

```bash
python3 /Users/aiden-kwak/Desktop/Personal/Stock/toss-trading-system/dashboard/server.py --port 8777
```

실행 후 브라우저에서: http://localhost:8777

## 대시보드 기능

- 계좌 요약 (총 자산, 수익률, 주문가능금액)
- 보유 종목 테이블 (수익률, 일일 등락)
- 시그널 엔진 (손절/익절 자동 판단)
- 리스크 게이트 (3개 체크 상태)
- 매수 평가 (101 Alphas 포함 130점 채점)
- 보호 종목 관리 (추가/제거)
- 60초 자동 새로고침
