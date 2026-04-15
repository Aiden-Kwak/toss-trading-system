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

try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    from db import init_db, insert_screener_run
    init_db()
    _DB_OK = True
except Exception:
    _DB_OK = False

WATCHLIST_FILE = Path.home() / "Library/Application Support/tossctl/watchlist.json"
SCRIPTS_DIR = Path(__file__).parent
_VENV_PY = SCRIPTS_DIR.parent / ".venv" / "bin" / "python3"
PYTHON = str(_VENV_PY) if _VENV_PY.exists() else "python3"

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

# tossctl 환경
TOSS_ENV = {
    **__import__("os").environ,
    "PATH": f"{Path.home()}/Desktop/Auto-trader/tossinvest-cli/bin:{__import__('os').environ.get('PATH', '')}",
    "TOSSCTL_AUTH_HELPER_DIR": str(Path.home() / "Desktop/Auto-trader/tossinvest-cli/auth-helper"),
    "TOSSCTL_AUTH_HELPER_PYTHON": str(Path.home() / "Desktop/Auto-trader/tossinvest-cli/auth-helper/.venv/bin/python3"),
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
    """거래량 급증, 갭업 종목을 yfinance로 탐색 (병렬 처리)"""
    if not HAS_YF:
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 스크리닝 유니버스: 외부 파일에서 로드
    universe_file = Path(__file__).parent.parent / "screener-universe.json"
    if universe_file.exists():
        try:
            udata = json.loads(universe_file.read_text())
            universe = udata.get(market.lower(), [])
        except Exception:
            universe = []
    else:
        # 폴백: 기본값
        if market.lower() == "us":
            universe = ["TSLA", "NVDA", "AAPL", "MSFT", "GOOG", "AMZN", "META", "AMD", "SPY", "QQQ"]
        elif market.lower() == "kr":
            universe = ["005930.KS", "000660.KS", "035420.KS", "035720.KS"]
        else:
            universe = []

    def _check_symbol(sym):
        try:
            hist = yf.Ticker(sym).history(period="1mo")
            if len(hist) < 5:
                return None

            today = hist.iloc[-1]
            yesterday = hist.iloc[-2]

            # 거래량 분석: 20일 평균 기반 (더 안정적인 베이스라인)
            avg_vol_20d = hist["Volume"].tail(20).mean() if len(hist) >= 20 else hist["Volume"].mean()
            avg_vol_5d = hist["Volume"].tail(5).mean()
            today_vol = today["Volume"]

            vol_spike_20d = today_vol / avg_vol_20d if avg_vol_20d > 0 else 1
            vol_spike_5d = today_vol / avg_vol_5d if avg_vol_5d > 0 else 1

            # 거래량 추세: 최근 5일 평균이 20일 평균보다 높은지 (거래량 증가 추세)
            vol_trend = avg_vol_5d / avg_vol_20d if avg_vol_20d > 0 else 1

            gap_pct = (today["Open"] - yesterday["Close"]) / yesterday["Close"] if yesterday["Close"] > 0 else 0
            change_pct = (today["Close"] - yesterday["Close"]) / yesterday["Close"] if yesterday["Close"] > 0 else 0

            # 거래량-가격 동조: 상승 + 거래량 급증 = 강한 시그널
            vol_price_confirm = bool(change_pct > 0 and vol_spike_20d >= 1.3)

            is_interesting = (
                vol_spike_20d >= 1.5 or          # 20일 평균 대비 1.5배
                vol_price_confirm or              # 상승 + 거래량 증가
                gap_pct >= 0.02 or
                gap_pct <= -0.03 or
                abs(change_pct) >= 0.03
            )

            if is_interesting:
                clean_sym = sym.replace(".KS", "").replace(".KQ", "")
                return {
                    "symbol": clean_sym,
                    "name": clean_sym,
                    "last": round(today["Close"], 2),
                    "reference_price": round(yesterday["Close"], 2),
                    "open": round(today["Open"], 2),
                    "high": round(today["High"], 2),
                    "low": round(today["Low"], 2),
                    "volume": int(today_vol),
                    "avg_volume_20d": int(avg_vol_20d),
                    "change_rate": round(change_pct, 4),
                    "market_code": "KSP" if ".KS" in sym else ("KSQ" if ".KQ" in sym else "NSQ"),
                    "_source": "auto",
                    "_vol_spike": round(vol_spike_20d, 2),
                    "_vol_spike_5d": round(vol_spike_5d, 2),
                    "_vol_trend": round(vol_trend, 2),
                    "_vol_price_confirm": vol_price_confirm,
                    "_gap_pct": round(gap_pct * 100, 2),
                }
            return None
        except Exception:
            return None

    candidates = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_check_symbol, sym): sym for sym in universe}
        for future in as_completed(futures):
            result = future.result()
            if result:
                candidates.append(result)

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
                [PYTHON, str(SCRIPTS_DIR / "signal-engine.py"), "evaluate-buy",
                 "--quote", json.dumps(c),
                 "--portfolio", json.dumps(portfolio),
                 "--config", "{}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                ev = json.loads(result.stdout)
                ev["_source"] = c.get("_source", "unknown")
                ev["_vol_spike"] = c.get("_vol_spike", None)
                ev["_vol_spike_5d"] = c.get("_vol_spike_5d", None)
                ev["_vol_trend"] = c.get("_vol_trend", None)
                ev["_vol_price_confirm"] = c.get("_vol_price_confirm", False)
                ev["_gap_pct"] = c.get("_gap_pct", None)
                ev["avg_volume_20d"] = c.get("avg_volume_20d", None)
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
    market = args.get("market", "all")
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
        if market == "all":
            auto_us = screen_auto("us")
            auto_nasdaq = screen_auto("nasdaq100")
            auto_kr = screen_auto("kr")
            auto = auto_us + auto_nasdaq + auto_kr
        elif market == "us":
            auto_us = screen_auto("us")
            auto_nasdaq = screen_auto("nasdaq100")
            auto = auto_us + auto_nasdaq
        else:
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
        if _DB_OK:
            try:
                insert_screener_run(result)
            except Exception:
                pass
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
