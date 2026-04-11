---
name: trading-rules
description: 단타 트레이딩 리스크 관리 규칙 및 거래 기록 형식
type: reference
originSessionId: fc00681f-cef1-4686-9618-7a6469575b07
---
## 리스크 관리 규칙

- 1회 최대 투자: 총 자산의 20% 이하
- 손절선 필수: 진입가 대비 -3% ~ -5%
- 익절선: 진입가 대비 +5% ~ +10%
- 일일 최대 손실: 총 자산의 -3% 도달 시 당일 거래 중단
- 동시 포지션: 최대 3종목
- preview 없이 주문 금지
- 사용자 확인 없이 주문 금지

## 거래 기록 형식

파일: `memory/trades/YYYYMMDD_<symbol>_<buy|sell>.md`

```markdown
---
name: trade-YYYYMMDD-SYMBOL
description: SYMBOL 매수/매도 기록
type: project
---

## 거래 정보
- 종목: SYMBOL (종목명)
- 방향: 매수/매도
- 수량: N주
- 진입가: $XXX / XXX원
- 청산가: $XXX / XXX원 (청산 시 기록)
- 손절선: $XXX
- 목표가: $XXX

## 수익/손실
- 금액: +/-XXX원
- 비율: +/-N%

## 진입 근거
- (분석 내용)

## 결과 평가
- 판단 정확도: (맞음/틀림)
- 교훈: (다음에 적용할 것)
- 점수: N/10
```

## 누적 학습

거래가 쌓이면 패턴을 분석:
- 어떤 유형의 거래에서 수익이 나는가
- 손실 거래의 공통점은 무엇인가
- 시간대별/요일별 성과 차이
- 종목 유형별 승률
