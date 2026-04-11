# toss-trading-system

Claude Code + 토스증권 CLI를 활용한 AI 기반 단타 트레이딩 시스템

> **비공식 프로젝트입니다.** 토스증권 웹 내부 API를 사용하며, 이용약관 위반 가능성이 있습니다.
> 모든 거래는 본인의 판단과 책임 하에 이루어집니다.

## 시스템 구조

```mermaid
flowchart LR
    subgraph Claude["Claude Code"]
        ANALYZE["분석 스킬\n시장/뉴스/기술적"]
        DECIDE["판단 엔진\n진입/청산 시그널"]
        MEMORY["거래 메모리\n학습/복기"]
    end

    subgraph Toss["tossctl (토스증권 CLI)"]
        AUTH["로그인\n(QR)"]
        READ["조회\n계좌/시세/주문"]
        TRADE["거래\n매수/매도/취소"]
    end

    ANALYZE --> DECIDE
    DECIDE --> TRADE
    TRADE --> MEMORY
    MEMORY --> ANALYZE
    AUTH --> READ
    READ --> ANALYZE
```

## 사용 가능한 스킬

### 실행 스킬 (토스증권 연동)

| 스킬 | 명령어 | 설명 |
|------|--------|------|
| 로그인 | `/toss-login` | QR 로그인, 세션 관리 |
| 포트폴리오 | `/toss-portfolio` | 계좌 요약, 보유종목, 주문 내역 |
| 시세 | `/toss-quote` | 실시간 시세 조회 (미국/한국) |
| 주문 | `/toss-order` | 매수/매도/취소 (6단계 안전장치) |
| 내보내기 | `/toss-export` | CSV 내보내기 |
| **단타 통합** | **`/toss-daytrade`** | **분석→발굴→주문→복기 전체 워크플로우** |
| **자율 매매** | **`/toss-autotrade`** | **사용자 확인 없이 자동 분석/판단/실행** |
| **보호 종목** | **`/toss-protect`** | **특정 종목을 자동매매 대상에서 제외** |

### 보호 종목 가드레일

사용자가 직접 관리하는 종목은 AI가 절대 건드릴 수 없도록 **시스템 레벨에서 강제**합니다.

- `protected-stocks.json`에 보호 종목 등록
- Claude Code **PreToolUse hook**으로 `tossctl order place/cancel/amend` 실행 전 자동 차단
- 스킬 프롬프트와 무관하게 **시스템적으로 차단** (우회 불가)
- `/toss-protect`로 보호 종목 추가/제거/조회

```
보호 종목에 대한 주문 시도 →
  hook이 stdin JSON에서 --symbol 추출 →
    protected-stocks.json과 대조 →
      매치 시 exit 2 (차단) + stderr 메시지
```

### 분석 스킬 (claude-trading-skills 연동)

별도 설치 필요. [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) 참고.

| 스킬 | 설명 |
|------|------|
| `/market-news-analyst` | 시장 뉴스 임팩트 분석 |
| `/market-environment-analysis` | 글로벌 시장 환경 평가 |
| `/market-breadth-analyzer` | 시장 건강도 (0-100) |
| `/technical-analyst` | 차트 기술적 분석 |
| `/us-stock-analysis` | 미국 개별종목 심층 분석 |
| `/finviz-screener` | 종목 스크리닝 |
| `/vcp-screener` | VCP 패턴 발굴 |
| `/pead-screener` | 실적 모멘텀 종목 |
| `/position-sizer` | 포지션 사이징 계산 |
| `/trader-memory-core` | 거래 이력 추적 |

## 단타 워크플로우

```mermaid
flowchart TD
    START(["/toss-daytrade 실행"]) --> AUTH{"세션 활성?"}
    AUTH -- No --> LOGIN["/toss-login\n(QR 로그인)"]
    LOGIN --> AUTH
    AUTH -- Yes --> P1

    subgraph P1["Phase 1: 장전 분석"]
        NEWS["시장 뉴스 수집"] --> POSITIONS["보유 포지션 확인"]
        POSITIONS --> BRIEFING["오늘의 브리핑"]
    end

    P1 --> P2

    subgraph P2["Phase 2: 종목 발굴"]
        US_SCAN["미국: 프리마켓/갭"] --> KR_SCAN["한국: 테마/이슈"]
        KR_SCAN --> WATCHLIST["관심 종목 리스트"]
    end

    P2 --> P3

    subgraph P3["Phase 3: 진입 판단"]
        TECH["기술적 분석"] --> SIZING["포지션 사이징"]
        SIZING --> SIGNAL["매수/매도 시그널"]
    end

    P3 --> DECIDE{"사용자 판단"}
    DECIDE -- "관망" --> P2
    DECIDE -- "진행" --> P4

    subgraph P4["Phase 4: 주문 실행"]
        PREVIEW["order preview"] --> CONFIRM{"사용자 확인?"}
        CONFIRM -- Yes --> EXECUTE["order place"]
        CONFIRM -- No --> CANCEL["취소"]
    end

    P4 --> P5

    subgraph P5["Phase 5: 포지션 관리"]
        MONITOR["시세 감시"] --> SL_TP{"손절/익절?"}
        SL_TP -- No --> MONITOR
        SL_TP -- Yes --> EXIT["청산"]
    end

    P5 --> P6

    subgraph P6["Phase 6: 복기"]
        RECORD["거래 기록"] --> LESSON["교훈 추출"]
    end
```

## 자율 트레이딩 모드 (`/toss-autotrade`)

사용자 확인 없이 Claude가 자동으로 분석/판단/실행하는 모드입니다.

```mermaid
flowchart TD
    START(["/loop 5m /toss-autotrade"]) --> SCAN["시장 스캔\n뉴스 + 시세"]
    SCAN --> CHECK["보유종목 손절/익절 체크"]
    CHECK --> SL{"손절/익절\n도달?"}
    SL -- Yes --> AUTO_SELL["자동 매도 실행"]
    SL -- No --> OPPORTUNITY["매수 기회 탐색"]
    OPPORTUNITY --> SIGNAL{"시그널?"}
    SIGNAL -- No --> WAIT["다음 사이클 대기"]
    SIGNAL -- Yes --> GATE{"리스크 게이트"}
    GATE -- "한도 초과" --> WAIT
    GATE -- "통과" --> AUTO_BUY["자동 매수 실행"]
    AUTO_BUY --> LOG["거래 기록"]
    AUTO_SELL --> LOG
    LOG --> DAILY{"일일 손실\n한도 초과?"}
    DAILY -- Yes --> HALT(["자동 중단"])
    DAILY -- No --> WAIT

    style HALT fill:#f8d7da,stroke:#dc3545,color:#842029
    style GATE fill:#fff3cd,stroke:#ffc107,color:#664d03
```

| 비교 | `/toss-daytrade` | `/toss-autotrade` |
|------|-------------------|-------------------|
| 사용자 확인 | 매 주문마다 필요 | 불필요 (자동) |
| 리스크 한도 | 자산 20% / 손절 -5% | 자산 10% / 손절 -3% (더 보수적) |
| 적합한 상황 | 직접 판단하며 거래 | 장시간 자동 운영 |
| `/loop` 연동 | 선택 | 필수 |

## 리스크 관리

| 규칙 | 값 |
|------|---|
| 1회 최대 투자 | 총 자산의 20% |
| 손절선 | 진입가 -3% ~ -5% |
| 익절선 | 진입가 +5% ~ +10% |
| 일일 최대 손실 | 총 자산의 -3% |
| 동시 포지션 | 최대 3종목 |

## 설치

### 요구사항

- macOS (Apple Silicon / Intel)
- [Claude Code](https://claude.ai/code)
- Go 1.25+
- Python 3.10+
- Google Chrome

### 원클릭 설치

```bash
git clone https://github.com/Aiden-Kwak/toss-trading-system.git
cd toss-trading-system
./install.sh
```

### 수동 설치

```bash
# 1. tossinvest-cli 빌드
git clone https://github.com/JungHoonGhae/tossinvest-cli.git
cd tossinvest-cli && make build

# 2. auth-helper 설치
cd auth-helper && python3 -m venv .venv
source .venv/bin/activate && pip install -e .
playwright install chromium

# 3. 스킬 복사
cp -r skills/toss-* ~/.claude/skills/

# 4. 환경변수 (.zshrc에 추가)
export PATH="<path-to>/tossinvest-cli/bin:$PATH"
export TOSSCTL_AUTH_HELPER_DIR="<path-to>/tossinvest-cli/auth-helper"
export TOSSCTL_AUTH_HELPER_PYTHON="<path-to>/tossinvest-cli/auth-helper/.venv/bin/python3"

# 5. 로그인
tossctl auth login
```

## 거래 가능 시간

| 시장 | 거래 시간 (한국시간) |
|------|---------------------|
| 한국 (KOSPI/KOSDAQ) | 09:00 - 15:30 |
| 미국 (NYSE/NASDAQ) | 23:30 - 06:00 (서머타임: 22:30 - 05:00) |

## 참고

- [tossinvest-cli](https://github.com/JungHoonGhae/tossinvest-cli) - 토스증권 비공식 CLI
- [claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) - Claude Code 트레이딩 분석 스킬 50개

## License

MIT
