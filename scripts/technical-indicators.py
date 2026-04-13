#!/usr/bin/env python3
"""
technical-indicators.py — 토스증권 public API 기반 기술적 지표

인증 없이 동작. 20일 일봉 데이터로 RSI, 볼린저밴드, EMA, 거래량비율 계산.
실시간 랭킹 종목 발굴 기능 포함.

사용법:
  # 종목 기술적 분석
  python3 scripts/technical-indicators.py analyze --symbol PLTR

  # 여러 종목
  python3 scripts/technical-indicators.py analyze --symbol PLTR,TSLA,AAPL

  # 실시간 랭킹 (거래량 급등 종목)
  python3 scripts/technical-indicators.py ranking

  # 한국주식
  python3 scripts/technical-indicators.py analyze --symbol A005930 --market kr
"""

import json
import sys
import urllib.request
from pathlib import Path

CHART_URL = "https://wts-info-api.tossinvest.com/api/v1/c-chart/{market}-s/{code}/day:1?count={count}"
PRICE_URL = "https://wts-info-api.tossinvest.com/api/v1/product/stock-prices?meta=true&productCodes={codes}"
RANKING_URL = "https://wts-info-api.tossinvest.com/api/v1/rankings/realtime/stock?size={size}"
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}


def fetch_json(url: str) -> dict:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def fetch_candles(code: str, market: str = "us", count: int = 30) -> list:
    """토스 public API에서 일봉 캔들 데이터 조회"""
    mkt = "kr" if market == "kr" else "us"
    url = CHART_URL.format(market=mkt, code=code, count=count)
    data = fetch_json(url)
    candles = data.get("result", {}).get("candles", [])
    # 오래된 순으로 정렬
    candles.reverse()
    return candles


def compute_rsi(closes: list, period: int = 14) -> float:
    """RSI 계산 (0-100). 30 이하 과매도, 70 이상 과매수"""
    if len(closes) < period + 1:
        return 50.0  # 데이터 부족

    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_bollinger(closes: list, period: int = 20) -> dict:
    """볼린저밴드 계산"""
    if len(closes) < period:
        return {"upper": 0, "middle": 0, "lower": 0, "pct_b": 0.5}

    window = closes[-period:]
    ma = sum(window) / period
    variance = sum((x - ma) ** 2 for x in window) / period
    std = variance ** 0.5

    upper = ma + 2 * std
    lower = ma - 2 * std
    current = closes[-1]

    # %B: 0=하단, 0.5=중간, 1=상단
    pct_b = (current - lower) / (upper - lower) if (upper - lower) > 0 else 0.5

    return {
        "upper": round(upper, 2),
        "middle": round(ma, 2),
        "lower": round(lower, 2),
        "pct_b": round(pct_b, 3),
        "width": round((upper - lower) / ma * 100, 2),  # 밴드폭 (%)
    }


def compute_ema(closes: list, period: int = 9) -> float:
    """지수이동평균"""
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)


def compute_volume_ratio(volumes: list, period: int = 20) -> float:
    """거래량 비율: 당일 / 20일 평균"""
    if len(volumes) < 2:
        return 1.0
    avg = sum(volumes[:-1][-period:]) / min(len(volumes) - 1, period)
    current = volumes[-1]
    return round(current / avg, 2) if avg > 0 else 1.0


def analyze_symbol(code: str, market: str = "us") -> dict:
    """종목 기술적 분석 전체"""
    candles = fetch_candles(code, market, count=30)
    if not candles:
        return {"error": f"데이터 없음: {code}"}

    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    current = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else current

    rsi = compute_rsi(closes)
    bb = compute_bollinger(closes)
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    # 전일 EMA (크로스오버 감지용)
    prev_ema9 = compute_ema(closes[:-1], 9) if len(closes) > 9 else None
    prev_ema21 = compute_ema(closes[:-1], 21) if len(closes) > 21 else None
    vol_ratio = compute_volume_ratio(volumes)

    # EMA 크로스 판단 — 진짜 크로스오버인지 확인
    is_golden_crossover = (
        ema9 > ema21 and
        prev_ema9 is not None and prev_ema21 is not None and
        prev_ema9 <= prev_ema21
    )
    if is_golden_crossover:
        ema_signal = "골든크로스 발생 (교차 확인)"
    elif ema9 > ema21:
        ema_signal = "상승 추세 (단기>장기)"
    elif ema9 < ema21:
        ema_signal = "데드크로스 (단기<장기, 하락)"
    else:
        ema_signal = "중립"

    # RSI 해석
    if rsi >= 70:
        rsi_signal = "과매수 (매도 고려)"
    elif rsi <= 30:
        rsi_signal = "과매도 (매수 기회)"
    elif rsi <= 40:
        rsi_signal = "약세"
    elif rsi >= 60:
        rsi_signal = "강세"
    else:
        rsi_signal = "중립"

    # 볼린저 해석
    if bb["pct_b"] >= 0.95:
        bb_signal = "상단 돌파 (과열/브레이크아웃)"
    elif bb["pct_b"] <= 0.05:
        bb_signal = "하단 터치 (과매도/반등 기대)"
    elif bb["pct_b"] >= 0.8:
        bb_signal = "상단 근처 (주의)"
    elif bb["pct_b"] <= 0.2:
        bb_signal = "하단 근처 (매수 기회)"
    else:
        bb_signal = "중간 (중립)"

    # 거래량 해석
    if vol_ratio >= 2.0:
        vol_signal = f"거래량 폭발 (평소 {vol_ratio}배)"
    elif vol_ratio >= 1.5:
        vol_signal = f"거래량 급증 (평소 {vol_ratio}배)"
    elif vol_ratio <= 0.5:
        vol_signal = f"거래량 감소 (평소 {vol_ratio}배)"
    else:
        vol_signal = f"거래량 보통 (평소 {vol_ratio}배)"

    # 종합 점수 (0-100, 매수 적합도)
    tech_score = 50  # 기본 중립
    # RSI
    if rsi <= 30: tech_score += 20
    elif rsi <= 40: tech_score += 10
    elif rsi >= 70: tech_score -= 20
    elif rsi >= 60: tech_score -= 5
    # EMA
    if ema9 > ema21: tech_score += 15
    elif ema9 < ema21: tech_score -= 15
    # 볼린저
    if bb["pct_b"] <= 0.2: tech_score += 10
    elif bb["pct_b"] >= 0.8: tech_score -= 10
    # 거래량
    if vol_ratio >= 1.5: tech_score += 5
    elif vol_ratio <= 0.5: tech_score -= 5

    tech_score = max(0, min(100, tech_score))

    return {
        "code": code,
        "market": market,
        "current_price": current,
        "change_pct": round((current - prev) / prev * 100, 2) if prev > 0 else 0,
        "rsi": {"value": rsi, "signal": rsi_signal},
        "bollinger": {**bb, "signal": bb_signal},
        "ema": {"ema9": ema9, "ema21": ema21, "signal": ema_signal, "is_golden_crossover": is_golden_crossover},
        "volume_ratio": {"value": vol_ratio, "signal": vol_signal},
        "tech_score": tech_score,
        "tech_grade": "A" if tech_score >= 70 else "B" if tech_score >= 55 else "C" if tech_score >= 40 else "D",
        "candle_count": len(candles),
    }


def fetch_realtime_ranking(size: int = 20) -> list:
    """토스 실시간 랭킹 (거래량/등락률 상위 종목)"""
    url = RANKING_URL.format(size=size)
    data = fetch_json(url)
    result = data.get("result", {})
    stocks = result.get("data", [])

    ranked = []
    for s in stocks:
        ranked.append({
            "symbol": s.get("symbol", ""),
            "name": s.get("name", ""),
            "market": s.get("market", {}).get("code", ""),
            "code": s.get("code", s.get("guid", "")),
        })
    return ranked


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "command: analyze | ranking"}))
        sys.exit(1)

    command = sys.argv[1]
    args = {}
    i = 2
    while i < len(sys.argv):
        if sys.argv[i].startswith("--"):
            key = sys.argv[i][2:]
            if i + 1 < len(sys.argv):
                args[key] = sys.argv[i + 1]; i += 2
            else:
                i += 1
        else:
            i += 1

    if command == "analyze":
        symbols = args.get("symbol", "").split(",")
        market = args.get("market", "us")
        results = []
        for sym in symbols:
            sym = sym.strip()
            if not sym:
                continue
            # 코드 변환: PLTR → US20200930014 는 불필요, 토스 API가 심볼 직접 지원하지 않음
            # 대신 productCode(A005930) 형식으로
            if market == "kr" and not sym.startswith("A"):
                code = f"A{sym}" if sym.isdigit() else sym
            else:
                code = sym
            results.append(analyze_symbol(code, market))
        print(json.dumps(results if len(results) > 1 else results[0] if results else {"error": "no symbol"}, ensure_ascii=False, indent=2))

    elif command == "ranking":
        size = int(args.get("size", "20"))
        ranked = fetch_realtime_ranking(size)
        print(json.dumps(ranked, ensure_ascii=False, indent=2))

    else:
        print(json.dumps({"error": f"unknown: {command}"}))


if __name__ == "__main__":
    main()
