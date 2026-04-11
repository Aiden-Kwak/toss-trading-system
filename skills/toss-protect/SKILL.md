---
name: toss-protect
description: 보호 종목 관리 - 사용자가 직접 관리하는 종목을 자동매매 대상에서 제외. 보호 종목 추가/제거/조회.
user_invocable: true
---

# toss-protect: 보호 종목 관리

사용자가 직접 관리하는 종목을 등록하여 자동매매(autotrade) 및 AI 판단 매매(daytrade)에서 제외합니다.

## 보호 종목 파일 경로

```
~/Library/Application Support/tossctl/protected-stocks.json
```

## 명령어

### 보호 종목 조회

```bash
cat ~/Library/Application\ Support/tossctl/protected-stocks.json
```

파일을 읽어서 현재 보호 종목 리스트를 테이블로 보여줍니다.

### 보호 종목 추가

사용자가 "AAPL 보호해줘", "삼성전자 건드리지마" 등 요청 시:

1. `protected-stocks.json`을 읽음
2. 해당 종목을 `stocks` 배열에 추가
3. 파일 저장

추가 시 사용자에게 확인:
- 종목 심볼/이름
- 보호 사유 (장기보유, 직접관리 등)
- 보호 범위: buy만 / sell만 / 둘 다

### 보호 종목 제거

사용자가 "PLTR 보호 해제해줘" 등 요청 시:

1. `protected-stocks.json`을 읽음
2. 해당 종목을 `stocks` 배열에서 제거
3. 파일 저장

### 보유 종목 자동 등록

`/toss-protect auto` 실행 시:
1. `tossctl portfolio positions --output json`으로 보유 종목 조회
2. 현재 보호 리스트에 없는 보유 종목을 추가할지 사용자에게 확인

## JSON 형식

```json
{
  "stocks": [
    {
      "symbol": "PLTR",
      "name": "팔란티어",
      "reason": "장기 보유 종목",
      "protected_actions": ["buy", "sell"]
    }
  ]
}
```

### protected_actions 옵션

- `["buy", "sell"]` - 매수/매도 모두 차단 (기본값)
- `["sell"]` - 매도만 차단 (추가 매수는 허용)
- `["buy"]` - 매수만 차단 (매도는 허용)
