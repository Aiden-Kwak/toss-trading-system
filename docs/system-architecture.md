# toss-trading-system Architecture Diagrams

## 1. System Architecture (시스템 아키텍처)

```mermaid
graph TB
    subgraph CLI["tossctl CLI"]
        TOSS_BIN["tossctl<br/>(토스증권 API)"]
    end

    subgraph Core["Core Engine"]
        DAEMON["autotrade-daemon.py<br/>자동매매 데몬"]
        SIGNAL["signal-engine.py<br/>시그널 엔진"]
        SCREENER["stock-screener.py<br/>종목 스크리너"]
        TECH["technical-indicators.py<br/>기술적 지표"]
    end

    subgraph Execution["Order Execution"]
        QUICK["quick-order.sh<br/>preview→grant→place"]
    end

    subgraph Recording["Recording & Notification"]
        LOGGER["trade-logger.py<br/>거래 기록"]
        NOTIFY["notify.py<br/>Discord 알림"]
        REPORT["report-generator.py<br/>보고서 생성"]
        ANALYZER["trade-analyzer.py<br/>거래 분석/학습"]
    end

    subgraph Storage["Data Storage"]
        DB[("SQLite DB<br/>trading.db")]
        JSON["trade-log.json<br/>(백업)"]
        STATE["daemon-state.json<br/>(상태)"]
        CONFIG["signal-config.json<br/>(설정)"]
        WATCHLIST["watchlist.json<br/>(관심종목)"]
        PROTECTED["protected-stocks.json<br/>(보호종목)"]
    end

    subgraph External["External"]
        DISCORD["Discord<br/>Webhook"]
        YFINANCE["yfinance<br/>(시세 데이터)"]
        TOSS_API["토스증권 API<br/>(주문/시세/계좌)"]
    end

    subgraph Dashboard["Dashboard"]
        SERVER["server.py<br/>HTTP API :8777"]
        WEB["index.html<br/>프론트엔드"]
    end

    %% Daemon orchestrates everything
    DAEMON -->|"evaluate_buy()"| SIGNAL
    DAEMON -->|"check_positions()"| SIGNAL
    DAEMON -->|"risk_gate()"| SIGNAL
    DAEMON -->|"find_buy_candidates()"| SCREENER
    DAEMON -->|"execute_buy/sell()"| QUICK
    DAEMON -->|"_log_trade()"| LOGGER
    DAEMON -->|"_notify()"| NOTIFY
    DAEMON -->|"insert_cycle()"| DB
    DAEMON -->|"상태 저장"| STATE
    DAEMON -->|"설정 로드"| CONFIG
    DAEMON -->|"보호종목 로드"| PROTECTED

    %% Screener data flow
    SCREENER -->|"screen_auto()"| YFINANCE
    SCREENER -->|"screen_watchlist()"| TOSS_BIN
    SCREENER -->|"evaluate_candidates()"| SIGNAL
    SCREENER -->|"insert_screener_run()"| DB
    SCREENER -->|"watchlist 로드"| WATCHLIST

    %% Signal engine
    SIGNAL -->|"compute_alphas()"| SIGNAL
    SIGNAL -->|"query_open_entry_grades()"| DB
    SIGNAL -->|"get_active_tranche_plans()"| DB

    %% Order execution
    QUICK -->|"preview/grant/place"| TOSS_BIN
    TOSS_BIN -->|"REST API"| TOSS_API

    %% Recording
    LOGGER -->|"insert_trade()"| DB
    LOGGER -->|"JSON 백업"| JSON
    NOTIFY -->|"Webhook POST"| DISCORD

    %% Dashboard
    SERVER -->|"DB 조회"| DB
    SERVER -->|"tossctl 실행"| TOSS_BIN
    SERVER -->|"signal-engine 호출"| SIGNAL
    SERVER -->|"screener 호출"| SCREENER
    SERVER --> WEB

    %% Auto-learning
    DAEMON -.->|"매 20사이클"| ANALYZER
    ANALYZER -->|"거래 데이터 분석"| DB

    %% Styling
    style DAEMON fill:#4f46e5,color:#fff
    style DB fill:#f59e0b,color:#000
    style DISCORD fill:#5865f2,color:#fff
    style TOSS_API fill:#1e40af,color:#fff
    style QUICK fill:#dc2626,color:#fff
```

---

## 2. Daemon Cycle Flow (데몬 사이클 흐름)

```mermaid
flowchart TD
    START(["run_cycle() 시작"]) --> INC["cycle_count += 1"]
    INC --> S1{"1. 세션 체크<br/>check_session()"}

    S1 -->|"만료"| S1_FAIL["status = PAUSED_SESSION<br/>Discord: 세션 만료 알림"]
    S1_FAIL --> RETURN_F([return False])

    S1 -->|"정상"| S1_RESTORE{"이전 상태가<br/>PAUSED_SESSION?"}
    S1_RESTORE -->|"Yes"| S1_NOTIFY["Discord: 세션 복원 알림"]
    S1_RESTORE -->|"No"| S2
    S1_NOTIFY --> S2

    S2{"2. 점검 체크<br/>check_maintenance()"}
    S2 -->|"점검 중"| S2_FAIL["status = PAUSED_MAINTENANCE<br/>Discord: 점검 대기 알림"]
    S2_FAIL --> RETURN_F

    S2 -->|"정상"| S3{"3. 장 시간 체크<br/>is_market_open()"}
    S3 -->|"휴장"| SKIP_CLOSED(["return True<br/>(스킵)"])
    S3 -->|"개장"| S4

    S4["4. 데이터 조회<br/>account summary + positions"]
    S4 --> S4_LOSS{"일일 손실 한도<br/>today_pnl <= limit?"}
    S4_LOSS -->|"한도 초과"| S4_FAIL["status = PAUSED_LOSS_LIMIT<br/>Discord: 손실 한도 알림"]
    S4_FAIL --> RETURN_F

    S4_LOSS -->|"정상"| S4_MDD{"4.5 주간 MDD<br/>최근 7일 MDD <= limit?"}
    S4_MDD -->|"한도 초과"| S4_MDD_FAIL["status = PAUSED_MDD<br/>Discord: MDD 알림"]
    S4_MDD_FAIL --> RETURN_F

    S4_MDD -->|"정상"| S5{"5. 연속 손절 냉각<br/>consecutive_losses >= N?"}
    S5 -->|"냉각 필요"| S5_COOL["status = PAUSED_COOLDOWN<br/>sleep(cooldown_minutes)<br/>consecutive_losses = 0"]
    S5_COOL --> S6
    S5 -->|"정상"| S6

    S6["6. 보유 포지션 모니터링<br/>monitor_positions()"]
    S6 --> S6_MFE["6.1 MFE/MAE 갱신<br/>update_trade_mfe_mae()"]
    S6_MFE --> S6_SELL{"매도 시그널<br/>있는가?"}

    S6_SELL -->|"Yes"| S6_PROT{"보호종목?"}
    S6_PROT -->|"Yes"| S6_SKIP["매도 스킵"]
    S6_PROT -->|"No"| S6_EXEC["notify signal → execute_sell()"]
    S6_SKIP --> S7
    S6_EXEC --> S7
    S6_SELL -->|"No"| S7

    S7{"7. 리스크 게이트<br/>check_risk_gate()"}
    S7 -->|"미통과"| S7_SKIP(["신규 매수 스킵<br/>return True"])

    S7 -->|"통과"| S7_5{"7.5 분할매수<br/>트랜치 처리<br/>scaled_entry?"}
    S7_5 -->|"활성"| S7_5_PROC["process_pending_tranches()"]
    S7_5 -->|"비활성"| S8
    S7_5_PROC --> S8

    S8{"8. 매수 후보 탐색<br/>find_buy_candidates()"}
    S8 --> S8_SCAN{"풀 스캔 필요?<br/>last_scan >= 1시간?"}
    S8_SCAN -->|"Yes"| S8_FULL["풀 스크리닝<br/>stock-screener.py scan<br/>(yfinance 데이터 수집)"]
    S8_FULL --> S8_CACHE["후보 캐시 저장<br/>state.cached_candidates"]
    S8_CACHE --> S8_RESCORE
    S8_SCAN -->|"No"| S8_RESCORE["캐시된 후보 재채점<br/>technical-indicators.py<br/>(현재가/지표 갱신)"]
    S8_RESCORE --> S8_CHECK{"후보 있는가?"}
    S8_CHECK -->|"없음"| S8_NONE(["return True"])
    S8_CHECK -->|"있음"| S8_NOTIFY["Discord: 스캔 결과 알림"]

    S8_NOTIFY --> S9["9. 매수 실행 루프<br/>시장별 슬롯 확인"]
    S9 --> S9_LOOP{"후보 순회"}
    S9_LOOP -->|"각 후보"| S9_FILTER{"보호종목?<br/>이미 보유?<br/>트랜치 활성?<br/>슬롯 잔여?"}
    S9_FILTER -->|"스킵"| S9_LOOP
    S9_FILTER -->|"매수 가능"| S9_BUY["execute_buy()<br/>+ 트랜치 플랜 생성"]
    S9_BUY --> S9_LOOP
    S9_LOOP -->|"완료"| S10

    S10["10. 사이클 DB 기록<br/>insert_cycle()"]
    S10 --> S11{"매 20사이클?"}
    S11 -->|"Yes"| S11_LEARN["자동 학습<br/>_auto_learn()"]
    S11 -->|"No"| RETURN_T
    S11_LEARN --> RETURN_T(["return True"])

    style START fill:#4f46e5,color:#fff
    style RETURN_F fill:#dc2626,color:#fff
    style RETURN_T fill:#22c55e,color:#fff
    style SKIP_CLOSED fill:#6b7280,color:#fff
    style S7_SKIP fill:#6b7280,color:#fff
    style S8_NONE fill:#6b7280,color:#fff
```

---

## 3. Order Execution Flow (주문 실행 흐름)

### 3a. execute_buy() 흐름

```mermaid
flowchart TD
    START(["execute_buy(symbol, price, qty)"]) --> DRY{"dry_run?"}

    DRY -->|"Yes"| DRY_LOG["trade-logger: log (mode=dry-run)<br/>today_trades += 1"]
    DRY_LOG --> DRY_RET(["return True"])

    DRY -->|"No"| CALC["Price Chase 시작<br/>US: +0.3% → +0.6% → +1.0%<br/>KR: +0.5% → +1.0% → +1.5%"]
    CALC --> QUICK["quick-order.sh 실행<br/>각 단계 20초 대기 후 미체결시 취소+상향"]

    subgraph QUICK_ORDER["quick-order.sh 내부"]
        Q1["Step 1: tossctl order preview<br/>→ confirm_token 추출"]
        Q1 --> Q2{"permissions<br/>active?"}
        Q2 -->|"No"| Q2_GRANT["tossctl order permissions grant<br/>--ttl 600"]
        Q2 -->|"Yes"| Q3
        Q2_GRANT --> Q3
        Q3["Step 3: tossctl order place<br/>--execute --confirm TOKEN"]
    end

    QUICK --> Q1
    Q3 --> RESULT{"returncode == 0?"}

    RESULT -->|"실패"| FAIL["에러 기록<br/>state.record_error()"]
    FAIL --> FAIL_RET(["return False"])

    RESULT -->|"성공"| PARSE["order_id 추출"]
    PARSE --> WAIT["_wait_for_fill()<br/>60초, 10초 간격"]

    subgraph FILL_CHECK["체결 확인 루프"]
        W1["portfolio positions 조회"]
        W1 --> W2{"종목 보유<br/>확인?"}
        W2 -->|"Yes"| W3["체결가 반환"]
        W2 -->|"No"| W4["order show 조회"]
        W4 --> W5{"filled?"}
        W5 -->|"Yes"| W3
        W5 -->|"No/timeout"| W6{"60초<br/>초과?"}
        W6 -->|"No"| W7["sleep(10)"]
        W7 --> W1
        W6 -->|"Yes"| W8["None 반환"]
    end

    WAIT --> W1
    W3 --> FILLED["BUY FILLED"]
    FILLED --> NOTIFY_BUY["Discord: 매수 알림<br/>notify_trade()"]
    NOTIFY_BUY --> LOG_BUY["trade-logger: log<br/>(mode=autotrade)"]
    LOG_BUY --> FILLED_RET(["return True"])

    W8 --> CANCEL["_cancel_pending()<br/>tossctl order cancel"]
    CANCEL --> CANCEL_NOTIFY["Discord: 미체결 취소 알림"]
    CANCEL_NOTIFY --> CANCEL_RET(["return False"])

    style START fill:#4f46e5,color:#fff
    style DRY_RET fill:#eab308,color:#000
    style FILLED_RET fill:#22c55e,color:#fff
    style FAIL_RET fill:#dc2626,color:#fff
    style CANCEL_RET fill:#dc2626,color:#fff
```

### 3b. execute_sell() 흐름

```mermaid
flowchart TD
    START(["execute_sell(symbol, signal)"]) --> DRY{"dry_run?"}

    DRY -->|"Yes"| DRY_PNL["today_pnl += pnl<br/>트랜치 플랜 정리"]
    DRY_PNL --> DRY_RET(["return True"])

    DRY -->|"No"| POS["보유 수량 확인<br/>portfolio positions"]
    POS --> POS_CHECK{"수량 > 0?"}
    POS_CHECK -->|"No"| POS_FAIL(["return False"])
    POS_CHECK -->|"Yes"| CALC["Price Chase 시작<br/>US: -0.3% → -0.6% → -1.0%<br/>KR: -0.5% → -1.0% → -1.5%"]

    CALC --> QUICK["quick-order.sh 실행<br/>각 단계 20초 대기 후 미체결시 취소+하향"]
    QUICK --> RESULT{"성공?"}

    RESULT -->|"실패"| SELL_FAIL(["return False"])
    RESULT -->|"성공"| WAIT["_wait_for_sell_fill()<br/>60초 대기"]

    WAIT --> FILL{"체결?"}
    FILL -->|"타임아웃"| CANCEL["취소 + Discord 알림"]
    CANCEL --> CANCEL_RET(["return False"])

    FILL -->|"체결"| PNL["PnL 계산<br/>(체결가 기준)"]
    PNL --> NOTIFY_SELL["Discord: 매도 알림"]
    NOTIFY_SELL --> CLOSE_ALL["trade-logger: close-all<br/>(전체 트랜치 일괄 청산)"]
    CLOSE_ALL --> TRANCHE["트랜치 플랜 완료<br/>예약 예산 해제"]
    TRANCHE --> LESSON["자동 교훈 생성<br/>_generate_auto_lesson()"]
    LESSON --> LOSS{"pnl < 0?"}
    LOSS -->|"Yes"| INC_LOSS["consecutive_losses += 1"]
    LOSS -->|"No"| RESET_LOSS["consecutive_losses = 0"]
    INC_LOSS --> SELL_RET(["return True"])
    RESET_LOSS --> SELL_RET

    style START fill:#4f46e5,color:#fff
    style SELL_RET fill:#22c55e,color:#fff
    style SELL_FAIL fill:#dc2626,color:#fff
    style CANCEL_RET fill:#dc2626,color:#fff
    style POS_FAIL fill:#dc2626,color:#fff
```

---

## 4. Data Flow (데이터 흐름)

```mermaid
flowchart LR
    subgraph Sources["데이터 소스"]
        WL["Watchlist<br/>watchlist.json"]
        YF["yfinance<br/>거래량 급증/갭"]
        NEWS["뉴스 종목<br/>(외부 전달)"]
    end

    subgraph Screening["스크리닝"]
        SC_WL["screen_watchlist()<br/>tossctl quote batch"]
        SC_AUTO["screen_auto()<br/>yfinance 1mo history"]
        SC_NEWS["screen_news()<br/>tossctl quote batch"]
    end

    subgraph Evaluation["평가"]
        EVAL["evaluate_candidates()<br/>signal-engine 채점"]
        GRADE["등급 분류<br/>A/B: BUY<br/>C: WATCH<br/>D: SKIP"]
    end

    subgraph Decision["매매 판단"]
        RISK["risk_gate()<br/>일일손실/포지션/여력"]
        MONITOR["check_positions()<br/>손절/익절/트레일링"]
    end

    subgraph Execution["주문 실행"]
        BUY["execute_buy()<br/>quick-order.sh"]
        SELL["execute_sell()<br/>quick-order.sh"]
    end

    subgraph Recording["기록"]
        DB_TRADE[("trades<br/>테이블")]
        DB_SCREEN[("screener_results<br/>테이블")]
        DB_CYCLE[("daemon_cycles<br/>테이블")]
        DB_TRANCHE[("tranche_plans<br/>테이블")]
        DISCORD["Discord<br/>알림"]
    end

    subgraph Display["조회/표시"]
        DASH["Dashboard<br/>server.py"]
        DB_CLI["db.py CLI"]
        NOTIFY_CLI["notify.py CLI"]
    end

    %% Source to Screening
    WL --> SC_WL
    YF --> SC_AUTO
    NEWS --> SC_NEWS

    %% Screening to Evaluation
    SC_WL --> EVAL
    SC_AUTO --> EVAL
    SC_NEWS --> EVAL
    EVAL -->|"insert_screener_run()"| DB_SCREEN
    EVAL --> GRADE

    %% Evaluation to Decision
    GRADE -->|"A/B 후보"| RISK
    RISK -->|"통과"| BUY

    %% Position monitoring
    MONITOR -->|"SELL 시그널"| SELL

    %% Execution to Recording
    BUY -->|"trade-logger log"| DB_TRADE
    BUY -->|"create_tranche_plan()"| DB_TRANCHE
    BUY -->|"notify_trade()"| DISCORD
    SELL -->|"trade-logger close-all"| DB_TRADE
    SELL -->|"complete_tranche_plan()"| DB_TRANCHE
    SELL -->|"notify_trade()"| DISCORD

    %% Cycle recording
    BUY --> DB_CYCLE
    SELL --> DB_CYCLE

    %% Display reads from DB
    DB_TRADE --> DASH
    DB_SCREEN --> DASH
    DB_CYCLE --> DASH
    DB_TRANCHE --> DASH
    DB_TRADE --> DB_CLI
    DB_TRADE --> NOTIFY_CLI

    style DB_TRADE fill:#f59e0b,color:#000
    style DB_SCREEN fill:#f59e0b,color:#000
    style DB_CYCLE fill:#f59e0b,color:#000
    style DB_TRANCHE fill:#f59e0b,color:#000
    style DISCORD fill:#5865f2,color:#fff
```

---

## 5. DB Schema (데이터베이스 스키마)

```mermaid
erDiagram
    trades {
        INTEGER id PK
        TEXT symbol "종목 심볼"
        TEXT name "종목명"
        TEXT side "buy/sell"
        TEXT market "US/KR"
        REAL quantity "수량"
        REAL entry_price "진입가"
        TEXT entry_date "진입일"
        TEXT entry_time "진입시각"
        TEXT entry_grade "등급 A/B/C/D/T"
        REAL entry_score "점수 0-100"
        TEXT entry_reason "진입 사유"
        REAL stop_loss "손절가"
        REAL take_profit "익절가"
        REAL exit_price "청산가"
        TEXT exit_date "청산일"
        TEXT exit_time "청산시각"
        TEXT exit_reason "청산 사유"
        REAL pnl_pct "수익률 %"
        REAL pnl_amount "수익금"
        TEXT lesson "교훈"
        INTEGER lesson_score "교훈 점수"
        TEXT status "open/closed"
        TEXT mode "autotrade/dry-run/manual"
        TEXT tags "JSON 태그"
        TEXT group_id "트랜치 그룹 ID"
        INTEGER tranche_seq "트랜치 순번"
        INTEGER screener_result_id FK
        REAL max_profit_pct "MFE 최대수익"
        REAL max_loss_pct "MAE 최대손실"
        REAL holding_hours "보유 시간"
        REAL intended_price "매수 의도가 (TCA)"
        REAL entry_slippage_pct "매수 슬리피지 %"
        REAL exit_intended_price "매도 의도가 (TCA)"
        REAL exit_slippage_pct "매도 슬리피지 %"
    }

    tranche_plans {
        INTEGER id PK
        TEXT group_id UK "그룹 ID (unique)"
        TEXT symbol "종목 심볼"
        TEXT market "US/KR"
        INTEGER total_tranches "총 트랜치 수"
        INTEGER planned_qty "계획 수량"
        REAL planned_budget "계획 예산"
        REAL entry_price_t1 "1차 진입가"
        TEXT ratios "분할 비율 JSON"
        INTEGER tranches_filled "체결된 트랜치"
        INTEGER next_tranche "다음 트랜치 번호"
        TEXT next_condition "다음 조건"
        REAL next_target_price "다음 목표가"
        INTEGER cycles_waited "대기 사이클"
        INTEGER max_wait_cycles "최대 대기"
        TEXT status "active/completed/expired"
        TEXT grade "등급"
        REAL score "점수"
        TEXT entry_reason "진입 사유"
    }

    screener_runs {
        INTEGER id PK
        TEXT run_at "실행 시각"
        TEXT source "소스 (watchlist,auto,news)"
        TEXT market "시장"
        INTEGER total_scanned "스캔 종목 수"
        INTEGER total_scored "채점 종목 수"
        INTEGER buy_count "매수 후보 수"
        INTEGER watch_count "관심 종목 수"
        INTEGER skip_count "스킵 종목 수"
    }

    screener_results {
        INTEGER id PK
        INTEGER run_id FK
        TEXT symbol "종목 심볼"
        TEXT name "종목명"
        TEXT grade "등급 A/B/C/D"
        REAL score "점수"
        TEXT source "소스"
        REAL vol_spike "거래량 급증 배수"
        REAL gap_pct "갭 비율"
        REAL current_price "현재가"
        REAL change_rate "등락률"
        TEXT recommendation "추천"
        TEXT regime "시장 상황"
        REAL rsi "RSI"
        REAL bb_pctb "볼린저 %B"
        INTEGER score_direction "방향성 점수"
        INTEGER score_momentum "모멘텀 점수"
        INTEGER score_price_action "가격액션 점수"
        INTEGER score_volume "거래량 점수"
        INTEGER score_context "컨텍스트 점수"
        INTEGER score_technical "기술적 점수"
        TEXT strategy "전략"
    }

    daemon_cycles {
        INTEGER id PK
        INTEGER cycle_num "사이클 번호"
        TEXT market "시장"
        TEXT status "ok/error"
        INTEGER positions_count "보유 종목 수"
        INTEGER sells_executed "매도 건수"
        INTEGER buys_attempted "매수 시도"
        INTEGER buys_filled "매수 체결"
        REAL today_pnl "금일 손익"
        INTEGER today_trades "금일 거래수"
        INTEGER consecutive_losses "연속 손절"
        REAL duration_sec "소요 시간(초)"
        TEXT note "메모"
    }

    errors {
        INTEGER id PK
        TEXT source "소스 (daemon/screener/...)"
        TEXT message "에러 메시지"
        TEXT detail "상세 정보"
        TEXT created_at "발생 시각"
    }

    %% Relationships
    screener_results ||--o{ screener_runs : "run_id"
    trades ||--o| screener_results : "screener_result_id"
    trades }o--o| tranche_plans : "group_id"
```

---

## Component Summary (컴포넌트 요약)

| 컴포넌트 | 파일 | 역할 |
|----------|------|------|
| **데몬** | `autotrade-daemon.py` | 메인 루프. 사이클 반복하며 전체 매매 오케스트레이션 |
| **시그널 엔진** | `signal-engine.py` | 매수 평가(0-100점), 손절/익절 체크, 리스크 게이트, Kelly Criterion |
| **스크리너** | `stock-screener.py` | 워치리스트 + yfinance 자동 스크리닝 + 뉴스 종목 통합 채점 |
| **주문 실행** | `quick-order.sh` | tossctl preview -> grant -> place 원샷 실행 |
| **거래 기록** | `trade-logger.py` | DB + JSON 이중 기록 (open/close/close-all/lesson) |
| **알림** | `notify.py` | Discord Webhook (거래/시그널/에러/세션/데몬/보고서) |
| **대시보드** | `dashboard/server.py` | HTTP API 서버 + 프론트엔드 (포트 8777) |
| **성과 지표** | `performance-metrics.py` | Sharpe, Sortino, MDD, Equity Curve (서킷 브레이커/대시보드) |
| **사이징 (Vol Target)** | `_vol_targeted_invest()` in daemon | ATR 기반 변동성 타겟팅 (risk parity). `vol_target_enabled` 설정으로 토글 |
| **DB** | `db.py` | SQLite 관리 (trades, screener, cycles, errors, tranche_plans) |
| **분석** | `trade-analyzer.py` | 거래 데이터 분석, 파라미터 자동 조정 제안 |
