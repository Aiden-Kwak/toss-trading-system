#!/usr/bin/env python3
"""
golden-cross-scanner.py — 골든크로스 스캐너

NASDAQ 100 + 한국 주요 종목을 순회하며 EMA 골든크로스를 탐지합니다.

사용법:
  python3 scripts/golden-cross-scanner.py scan                    # 전체 (nasdaq100 + kr)
  python3 scripts/golden-cross-scanner.py scan --market nasdaq100  # NASDAQ 100만
  python3 scripts/golden-cross-scanner.py scan --market kr         # 한국만
  python3 scripts/golden-cross-scanner.py scan --market us         # 기존 US 유니버스
"""

import json
import sys
import numpy as np
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

UNIVERSE_FILE = Path(__file__).parent.parent / "screener-universe.json"


def _ema(data: list, period: int) -> float:
    if len(data) < period:
        return 0
    k = 2 / (period + 1)
    result = data[0]
    for d in data[1:]:
        result = d * k + result * (1 - k)
    return result


def _rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50
    deltas = np.diff(closes[-(period + 1):])
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = np.mean(gains) if gains else 0
    avg_loss = np.mean(losses) if losses else 0.01
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    return round(100 - 100 / (1 + rs), 1)


def analyze_symbol(sym: str) -> dict | None:
    """단일 종목 EMA/RSI 분석"""
    try:
        h = yf.Ticker(sym).history(period="3mo")
        if len(h) < 22:
            return None

        closes = h["Close"].tolist()
        volumes = h["Volume"].tolist()

        today_ema9 = _ema(closes, 9)
        today_ema21 = _ema(closes, 21)
        prev_ema9 = _ema(closes[:-1], 9)
        prev_ema21 = _ema(closes[:-1], 21)

        is_crossover = today_ema9 > today_ema21 and prev_ema9 <= prev_ema21
        is_uptrend = today_ema9 > today_ema21
        is_near_cross = not is_uptrend and (today_ema21 - today_ema9) / today_ema21 < 0.005 if today_ema21 > 0 else False

        rsi_val = _rsi(closes)
        price = round(closes[-1], 2)
        change_pct = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if len(closes) >= 2 else 0

        # 거래량 급증 비율
        avg_vol = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
        vol_spike = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1

        return {
            "symbol": sym.replace(".KS", "").replace(".KQ", ""),
            "yf_symbol": sym,
            "price": price,
            "change_pct": change_pct,
            "ema9": round(today_ema9, 2),
            "ema21": round(today_ema21, 2),
            "ema_gap_pct": round((today_ema9 - today_ema21) / today_ema21 * 100, 2) if today_ema21 > 0 else 0,
            "prev_ema9": round(prev_ema9, 2),
            "prev_ema21": round(prev_ema21, 2),
            "rsi": rsi_val,
            "vol_spike": vol_spike,
            "is_golden_crossover": is_crossover,
            "is_uptrend": is_uptrend,
            "is_near_cross": is_near_cross,
            "status": "GOLDEN_CROSS" if is_crossover else "UPTREND" if is_uptrend else "NEAR_CROSS" if is_near_cross else "DOWNTREND",
        }
    except Exception:
        return None


def scan(market: str = "all") -> dict:
    """유니버스 전체 스캔"""
    if not HAS_YF:
        return {"error": "yfinance not installed"}

    universe = {}
    if UNIVERSE_FILE.exists():
        universe = json.loads(UNIVERSE_FILE.read_text())

    symbols = []
    markets_used = []

    if market in ("all", "nasdaq100"):
        syms = universe.get("nasdaq100", [])
        symbols.extend(syms)
        markets_used.append(f"nasdaq100({len(syms)})")

    if market in ("all", "kr"):
        syms = universe.get("kr", [])
        symbols.extend(syms)
        markets_used.append(f"kr({len(syms)})")

    if market == "us":
        syms = universe.get("us", [])
        symbols.extend(syms)
        markets_used.append(f"us({len(syms)})")

    # 중복 제거
    symbols = list(dict.fromkeys(symbols))

    # 병렬 스캔 (최대 10 스레드)
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(analyze_symbol, sym): sym for sym in symbols}
        for future in as_completed(futures):
            r = future.result()
            if r:
                results.append(r)

    # 분류
    crossovers = sorted([r for r in results if r["is_golden_crossover"]],
                        key=lambda x: -x["vol_spike"])
    uptrend = sorted([r for r in results if r["is_uptrend"] and not r["is_golden_crossover"]],
                     key=lambda x: -x["ema_gap_pct"])
    near_cross = sorted([r for r in results if r["is_near_cross"]],
                        key=lambda x: x["ema_gap_pct"])
    downtrend = [r for r in results if r["status"] == "DOWNTREND"]

    return {
        "timestamp": datetime.now().isoformat(),
        "markets": markets_used,
        "total_scanned": len(symbols),
        "total_analyzed": len(results),
        "golden_crossovers": crossovers,
        "uptrend": uptrend[:20],
        "near_crossover": near_cross[:10],
        "summary": {
            "golden_cross": len(crossovers),
            "uptrend": len(uptrend),
            "near_cross": len(near_cross),
            "downtrend": len(downtrend),
        },
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "command: scan"}))
        sys.exit(1)

    args = {}
    i = 2
    while i < len(sys.argv):
        if sys.argv[i].startswith("--"):
            key = sys.argv[i][2:]
            if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
                args[key] = sys.argv[i + 1]
                i += 2
            else:
                args[key] = "true"
                i += 1
        else:
            i += 1

    command = sys.argv[1]
    if command == "scan":
        market = args.get("market", "all")
        result = scan(market)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"error": f"unknown: {command}"}))


if __name__ == "__main__":
    main()
