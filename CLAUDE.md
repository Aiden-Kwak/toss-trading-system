# Toss Trading System — Claude Instructions

## 시스템 수정 시 필수 참조

이 프로젝트의 코드를 수정하기 전에 반드시 아래 문서를 먼저 읽어야 합니다:

- `docs/system-architecture.md` — 시스템 아키텍처, 데몬 사이클 흐름, 주문 실행 흐름, 데이터 흐름, DB 스키마 다이어그램

이 문서를 읽지 않고 코드를 수정하면 사이드 이펙트를 놓칠 수 있습니다. 특히:
- **autotrade-daemon.py** 수정 시: 데몬 사이클 흐름도와 주문 실행 흐름도를 반드시 확인
- **db.py** 수정 시: DB 스키마 다이어그램 확인
- **signal-engine.py, stock-screener.py** 수정 시: 데이터 흐름도 확인
- 새 컴포넌트 추가 시: 시스템 아키텍처 다이어그램 업데이트

## 수정 후 다이어그램 업데이트 (필수)

**시스템 구조, 흐름, DB 스키마가 변경되면 `docs/system-architecture.md`의 해당 다이어그램을 반드시 함께 업데이트하세요.**

변경 유형별 업데이트 대상:
- **사이클 흐름 변경** (단계 추가/제거/순서 변경): 데몬 사이클 흐름도 업데이트
- **주문 실행 로직 변경**: 주문 실행 흐름도 업데이트
- **데이터 흐름 변경** (새 데이터 소스, 파이프라인 변경): 데이터 흐름도 업데이트
- **DB 테이블/컬럼 변경**: DB 스키마 ER 다이어그램 업데이트
- **새 컴포넌트 추가/삭제**: 시스템 아키텍처 다이어그램 업데이트

다이어그램 업데이트 없이 코드만 수정하는 것은 허용되지 않습니다.

## 핵심 규칙

- DB가 primary 저장소. JSON(trade-log.json)은 백업용으로만 병행 기록
- notify.py, trade-logger.py는 직접 import 호출 (subprocess 아님). fallback으로만 subprocess 사용
- positions, summary는 사이클당 1회 조회 후 캐시하여 전달 (중복 호출 금지)
- KR 종목 심볼은 A-prefix 매칭 필요 (067310 vs A067310)
- KR 시장은 limit 주문만 지원 (market order 불가). 호가 단위(tick size) 정렬 필수
- 스크리닝은 1시간 간격 풀 스캔, 매 사이클(5분)마다 캐시된 후보 재채점 (scan_interval_minutes 설정)
