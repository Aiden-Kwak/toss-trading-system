#!/usr/bin/env python3
"""
trade-logger.py — 거래 자동 기록

모든 거래(매수/매도)를 trade-log.json에 자동 기록합니다.
PostToolUse hook 또는 대시보드 API에서 호출됩니다.

사용법:
  # 거래 기록
  python3 trade-logger.py log \
    --symbol TSLA --side buy --qty 1 --price 250.5 \
    --grade B --score 75 --reason "모멘텀 상승" \
    [--stop-loss 243 --take-profit 268]

  # 청산 기록 (기존 매수에 매칭)
  python3 trade-logger.py close \
    --symbol TSLA --exit-price 268 --exit-reason TAKE_PROFIT

  # 전체 로그 조회
  python3 trade-logger.py list [--status open|closed|all]

  # 교훈 추가 (청산 후)
  python3 trade-logger.py lesson --symbol TSLA --lesson "모멘텀 진입 성공, 타이밍 적절" --score 8
"""

import json
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / "Library/Application Support/tossctl/trade-log.json"
CONFIG_FILE = Path.home() / "Library/Application Support/tossctl/signal-config.json"


def load_log() -> list:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return []


def save_log(trades: list):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(json.dumps(trades, ensure_ascii=False, indent=2))


def log_trade(args: dict):
    """새 거래 기록"""
    trades = load_log()

    trade = {
        "id": len(trades) + 1,
        "symbol": args.get("symbol", "").upper(),
        "name": args.get("name", ""),
        "side": args.get("side", "buy"),
        "market": args.get("market", "US"),
        "quantity": float(args.get("qty", "0")),
        "entry_price": float(args.get("price", "0")),
        "entry_date": datetime.now().strftime("%Y-%m-%d"),
        "entry_time": datetime.now().strftime("%H:%M:%S"),
        "entry_grade": args.get("grade", ""),
        "entry_score": float(args.get("score", "0")),
        "entry_reason": args.get("reason", ""),
        "stop_loss": float(args.get("stop-loss", "0")),
        "take_profit": float(args.get("take-profit", "0")),
        "exit_price": None,
        "exit_date": None,
        "exit_reason": None,
        "pnl_pct": None,
        "pnl_amount": None,
        "lesson": None,
        "lesson_score": None,
        "status": "open",
        "mode": args.get("mode", "manual"),  # manual / autotrade
        "tags": [],
    }

    trades.append(trade)
    save_log(trades)
    print(json.dumps(trade, ensure_ascii=False, indent=2))


def close_trade(args: dict):
    """거래 청산 기록"""
    trades = load_log()
    symbol = args.get("symbol", "").upper()
    exit_price = float(args.get("exit-price", "0"))
    exit_reason = args.get("exit-reason", "MANUAL")

    # 해당 심볼의 가장 최근 open 거래 찾기
    target = None
    for t in reversed(trades):
        if t["symbol"] == symbol and t["status"] == "open":
            target = t
            break

    if not target:
        print(json.dumps({"error": f"no open trade found for {symbol}"}))
        return

    entry = target["entry_price"]
    cost_rate = 0.002  # 왕복 수수료 0.2%
    gross_pnl = (exit_price - entry) / entry if entry > 0 else 0
    net_pnl = gross_pnl - cost_rate

    target["exit_price"] = exit_price
    target["exit_date"] = datetime.now().strftime("%Y-%m-%d")
    target["exit_reason"] = exit_reason
    target["pnl_pct"] = round(net_pnl * 100, 2)
    target["pnl_amount"] = round((exit_price - entry) * target["quantity"], 2)
    target["status"] = "closed"

    # 자동 태깅
    if net_pnl > 0:
        target["tags"].append("win")
    else:
        target["tags"].append("loss")
    if exit_reason == "STOP_LOSS":
        target["tags"].append("stopped_out")
    if exit_reason == "TAKE_PROFIT":
        target["tags"].append("target_hit")

    save_log(trades)
    print(json.dumps(target, ensure_ascii=False, indent=2))


def add_lesson(args: dict):
    """거래에 교훈 추가"""
    trades = load_log()
    symbol = args.get("symbol", "").upper()
    lesson = args.get("lesson", "")
    score = int(args.get("score", "5"))

    # 해당 심볼의 가장 최근 closed 거래 찾기
    target = None
    for t in reversed(trades):
        if t["symbol"] == symbol and t["status"] == "closed":
            target = t
            break

    if not target:
        print(json.dumps({"error": f"no closed trade found for {symbol}"}))
        return

    target["lesson"] = lesson
    target["lesson_score"] = score

    # 교훈 기반 태깅
    keywords = {
        "모멘텀": "momentum", "역추세": "mean_reversion", "급등": "spike",
        "뉴스": "news_driven", "실적": "earnings", "테마": "theme",
        "타이밍": "timing", "사이징": "sizing", "손절": "stop_loss_related",
    }
    for k, tag in keywords.items():
        if k in lesson:
            target["tags"].append(tag)

    save_log(trades)
    print(json.dumps(target, ensure_ascii=False, indent=2))


def list_trades(args: dict):
    """거래 목록 조회"""
    trades = load_log()
    status = args.get("status", "all")
    if status != "all":
        trades = [t for t in trades if t["status"] == status]
    print(json.dumps(trades, ensure_ascii=False, indent=2))


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "command required: log | close | lesson | list"}))
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

    if command == "log":
        log_trade(args)
    elif command == "close":
        close_trade(args)
    elif command == "lesson":
        add_lesson(args)
    elif command == "list":
        list_trades(args)
    else:
        print(json.dumps({"error": f"unknown command: {command}"}))


if __name__ == "__main__":
    main()
