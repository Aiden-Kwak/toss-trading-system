#!/usr/bin/env python3
"""
stock-screener.py — 통합 종목 스크리너

3개 소스에서 종목 후보를 수집하고, signal-engine으로 채점하여 순위를 매깁니다.

소스 1: 워치리스트 (watchlist.json)
소스 2: 자동 스크리닝 (yfinance: 거래량 급증, 갭업/갭다운)
소스 3: 뉴스 키워드 (외부에서 전달된 종목 심볼)

사용법:
  # 전체 스크리닝 (워치리스트 + 자동)
  python3 stock-screener.py scan

  # 워치리스트만
  python3 stock-screener.py scan --source watchlist

  # 자동 스크리닝만
  python3 stock-screener.py scan --source auto --market us

  # 뉴스에서 추출된 종목 추가 평가
  python3 stock-screener.py scan --news-symbols TSLA,NVDA,AAPL

  # 워치리스트 관리
  python3 stock-screener.py watchlist-add --symbol TSLA --name 테슬라 --market us
  python3 stock-screener.py watchlist-remove --symbol TSLA
  python3 stock-screener.py watchlist-list
"""

import json
import sys
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

WATCHLIST_FILE = Path.home() / "Library/Application Support/tossctl/watchlist.json"
SCRIPTS_DIR = Path(__file__).parent

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

# tossctl 환경
TOSS_ENV = {
    **__import__("os").environ,
    "PATH": f"{Path.home()}/Desktop/Personal/Stock/tossinvest-cli/bin:{__import__('os').environ.get('PATH', '')}",
    "TOSSCTL_AUTH_HELPER_DIR": str(Path.home() / "Desktop/Personal/Stock/tossinvest-cli/auth-helper"),
    "TOSSCTL_AUTH_HELPER_PYTHON": str(Path.home() / "Desktop/Personal/Stock/tossinvest-cli/auth-helper/.venv/bin/python3"),
}


# ─── 워치리스트 관리 ───

def load_watchlist() -> list:
    if WATCHLIST_FILE.exists():
        data = json.loads(WATCHLIST_FILE.read_text())
        return data.get("stocks", [])
    return []


def save_watchlist(stocks: list):
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"updated_at": datetime.now().isoformat(), "stocks": stocks}
    WATCHLIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def watchlist_add(args: dict):
    stocks = load_watchlist()
    symbol = args.get("symbol", "").upper()
    if any(s["symbol"] == symbol for s in stocks):
        print(json.dumps({"error": f"{symbol} already in watchlist"}))
        return
    stocks.append({
        "symbol": symbol,
        "name": args.get("name", ""),
        "market": args.get("market", "us").upper(),
        "added_at": datetime.now().isoformat(),
        "source": "manual",
    })
    save_watchlist(stocks)
    print(json.dumps({"ok": True, "count": len(stocks)}))


def watchlist_remove(args: dict):
    stocks = load_watchlist()
    symbol = args.get("symbol", "").upper()
    stocks = [s for s in stocks if s["symbol"] != symbol]
    save_watchlist(stocks)
    print(json.dumps({"ok": True, "count": len(stocks)}))


# ─── 소스 1: 워치리스트 스크리닝 ───

def screen_watchlist() -> list:
    """워치리스트 종목의 현재 시세를 tossctl로 조회"""
    stocks = load_watchlist()
    if not stocks:
        return []

    symbols = [s["symbol"] for s in stocks]
    candidates = []

    # tossctl로 시세 조회
    try:
        result = subprocess.run(
            ["tossctl", "quote", "batch", *symbols, "--output", "json"],
            capture_output=True, text=True, timeout=15, env=TOSS_ENV
        )
        if result.returncode == 0:
            quotes = json.loads(result.stdout)
            for q in quotes:
                q["_source"] = "watchlist"
                candidates.append(q)
    except Exception:
        pass

    return candidates


# ─── 소스 2: 자동 스크리닝 (yfinance) ───

def screen_auto(market: str = "us", top_n: int = 20) -> list:
    """거래량 급증, 갭업 종목을 yfinance로 탐색"""
    if not HAS_YF:
        return []

    # 스크리닝 대상 유니버스
    if market.lower() == "us":
        universe = [
            "TSLA", "NVDA", "AAPL", "MSFT", "GOOG", "AMZN", "META", "AMD",
            "NFLX", "PLTR", "COIN", "MARA", "RIOT", "SOFI", "NIO", "BABA",
            "SQ", "SHOP", "UBER", "SNAP", "PINS", "RBLX", "DKNG", "CRWD",
            "NET", "SNOW", "ENPH", "RIVN", "LCID", "HOOD", "AFRM", "UPST",
            "SPY", "QQQ", "VOO", "IWM", "ARKK", "SOXL", "TQQQ",
        ]
    elif market.lower() == "kr":
        # 한국주식은 야후 코드로
        universe = [
            "005930.KS", "000660.KS", "035420.KS", "035720.KS", "051910.KS",
            "006400.KS", "003670.KS", "105560.KS", "032830.KS", "012330.KS",
            "247540.KS", "352820.KS", "003490.KS", "028260.KS", "066570.KS",
            "055550.KS", "096770.KS", "034730.KS", "086790.KS", "033780.KS",
        ]
    else:
        universe = []

    candidates = []

    # 최근 2일 데이터로 거래량 변화, 갭 확인
    for sym in universe:
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period="5d")
            if len(hist) < 2:
                continue

            today = hist.iloc[-1]
            yesterday = hist.iloc[-2]
            avg_vol_5d = hist["Volume"].mean()

            # 거래량 급증 (오늘 거래량 > 5일 평균의 1.5배)
            vol_spike = today["Volume"] / avg_vol_5d if avg_vol_5d > 0 else 1

            # 갭 (오늘 시가 vs 어제 종가)
            gap_pct = (today["Open"] - yesterday["Close"]) / yesterday["Close"] if yesterday["Close"] > 0 else 0

            # 일일 등락
            change_pct = (today["Close"] - yesterday["Close"]) / yesterday["Close"] if yesterday["Close"] > 0 else 0

            # 필터: 거래량 급증 OR 갭업 2%+ OR 갭다운 -3%+
            is_interesting = (
                vol_spike >= 1.5 or
                gap_pct >= 0.02 or
                gap_pct <= -0.03 or
                abs(change_pct) >= 0.03
            )

            if is_interesting:
                # tossctl 호환 형식으로 변환
                clean_sym = sym.replace(".KS", "").replace(".KQ", "")
                candidates.append({
                    "symbol": clean_sym,
                    "name": ticker.info.get("shortName", sym),
                    "last": round(today["Close"], 2),
                    "reference_price": round(yesterday["Close"], 2),
                    "open": round(today["Open"], 2),
                    "high": round(today["High"], 2),
                    "low": round(today["Low"], 2),
                    "volume": int(today["Volume"]),
                    "change_rate": round(change_pct, 4),
                    "market_code": "KSP" if ".KS" in sym else ("KSQ" if ".KQ" in sym else "NSQ"),
                    "_source": "auto",
                    "_vol_spike": round(vol_spike, 2),
                    "_gap_pct": round(gap_pct * 100, 2),
                })
        except Exception:
            continue

    # 거래량 급증 + 변동폭 기준 정렬
    candidates.sort(key=lambda x: x.get("_vol_spike", 0) * abs(x.get("change_rate", 0)), reverse=True)
    return candidates[:top_n]


# ─── 소스 3: 뉴스 종목 ───

def screen_news(symbols: list) -> list:
    """뉴스에서 추출된 심볼을 tossctl로 조회"""
    if not symbols:
        return []

    candidates = []
    try:
        result = subprocess.run(
            ["tossctl", "quote", "batch", *symbols, "--output", "json"],
            capture_output=True, text=True, timeout=15, env=TOSS_ENV
        )
        if result.returncode == 0:
            quotes = json.loads(result.stdout)
            for q in quotes:
                q["_source"] = "news"
                candidates.append(q)
    except Exception:
        pass

    return candidates


# ─── 통합 채점 ───

def evaluate_candidates(candidates: list, portfolio: dict = None) -> list:
    """모든 후보를 signal-engine으로 채점하고 순위 매김"""
    if not portfolio:
        # 포트폴리오 정보 가져오기
        try:
            result = subprocess.run(
                ["tossctl", "account", "summary", "--output", "json"],
                capture_output=True, text=True, timeout=10, env=TOSS_ENV
            )
            portfolio = json.loads(result.stdout) if result.returncode == 0 else {}
        except Exception:
            portfolio = {}

    scored = []
    seen = set()

    for c in candidates:
        sym = c.get("symbol", "")
        if sym in seen:
            continue
        seen.add(sym)

        try:
            result = subprocess.run(
                ["python3", str(SCRIPTS_DIR / "signal-engine.py"), "evaluate-buy",
                 "--quote", json.dumps(c),
                 "--portfolio", json.dumps(portfolio),
                 "--config", "{}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                ev = json.loads(result.stdout)
                ev["_source"] = c.get("_source", "unknown")
                ev["_vol_spike"] = c.get("_vol_spike", None)
                ev["_gap_pct"] = c.get("_gap_pct", None)
                scored.append(ev)
        except Exception:
            continue

    # 복합 우선순위 정렬
    # 1차: 등급 (A > B > C > D)
    # 2차: 점수 (높을수록 우선)
    # 3차: 거래량 급증 (유동성 높을수록 체결 확실)
    # 4차: 알파 모멘텀 (강한 쪽 우선)
    # 5차: 과거 손실 종목 감점
    loss_symbols = _load_loss_history()

    grade_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    for s in scored:
        # 과거 손실 기록 있으면 점수 감점
        sym = s.get("symbol", "")
        if sym in loss_symbols:
            s["_loss_penalty"] = loss_symbols[sym]
        else:
            s["_loss_penalty"] = 0

    scored.sort(key=lambda x: (
        grade_order.get(x.get("grade", "D"), 3),   # 등급
        -(x.get("score_pct", 0)),                    # 점수 (높을수록)
        -(x.get("_vol_spike", 0) or 0),              # 거래량 급증
        x.get("_loss_penalty", 0),                   # 과거 손실 (적을수록)
    ))
    return scored


def _load_loss_history() -> dict:
    """과거 거래에서 종목별 손실 횟수 조회"""
    log_file = Path.home() / "Library/Application Support/tossctl/trade-log.json"
    if not log_file.exists():
        return {}
    try:
        trades = json.loads(log_file.read_text())
        losses = {}
        for t in trades:
            if t.get("status") == "closed" and (t.get("pnl_pct") or 0) < 0:
                sym = t.get("symbol", "")
                losses[sym] = losses.get(sym, 0) + 1
        return losses
    except Exception:
        return {}


# ─── 메인 스캔 ───

def scan(args: dict) -> dict:
    """통합 스크리닝 실행"""
    source = args.get("source", "all")
    market = args.get("market", "us")
    news_symbols = [s.strip() for s in args.get("news-symbols", "").split(",") if s.strip()]

    all_candidates = []
    sources_used = []

    # 소스 1: 워치리스트
    if source in ("all", "watchlist"):
        wl = screen_watchlist()
        all_candidates.extend(wl)
        sources_used.append(f"watchlist({len(wl)})")

    # 소스 2: 자동 스크리닝
    if source in ("all", "auto"):
        auto = screen_auto(market)
        all_candidates.extend(auto)
        sources_used.append(f"auto({len(auto)})")

    # 소스 3: 뉴스 종목
    if news_symbols:
        news = screen_news(news_symbols)
        all_candidates.extend(news)
        sources_used.append(f"news({len(news)})")

    # 통합 채점
    scored = evaluate_candidates(all_candidates)

    # 등급별 분류
    buy_candidates = [s for s in scored if s.get("grade") in ("A", "B")]
    watch_candidates = [s for s in scored if s.get("grade") == "C"]
    skip_candidates = [s for s in scored if s.get("grade") == "D"]

    return {
        "timestamp": datetime.now().isoformat(),
        "sources": sources_used,
        "total_scanned": len(all_candidates),
        "total_scored": len(scored),
        "buy_candidates": buy_candidates,
        "watch_list": watch_candidates,
        "skip_list": skip_candidates,
        "summary": {
            "buy": len(buy_candidates),
            "watch": len(watch_candidates),
            "skip": len(skip_candidates),
        },
    }


# ─── CLI ───

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "command: scan | watchlist-add | watchlist-remove | watchlist-list"}))
        sys.exit(1)

    command = sys.argv[1]
    args = {}
    i = 2
    while i < len(sys.argv):
        if sys.argv[i].startswith("--"):
            key = sys.argv[i][2:]
            if i + 1 < len(sys.argv):
                args[key] = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        else:
            i += 1

    if command == "scan":
        result = scan(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif command == "watchlist-add":
        watchlist_add(args)

    elif command == "watchlist-remove":
        watchlist_remove(args)

    elif command == "watchlist-list":
        stocks = load_watchlist()
        print(json.dumps(stocks, ensure_ascii=False, indent=2))

    else:
        print(json.dumps({"error": f"unknown: {command}"}))


if __name__ == "__main__":
    main()
