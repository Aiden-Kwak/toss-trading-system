#!/usr/bin/env python3
"""
trade-logger.py — 거래 자동 기록

DB를 primary 저장소로 사용합니다. JSON은 백업용으로 병행 기록합니다.

사용법:
  python3 trade-logger.py log \
    --symbol TSLA --side buy --qty 1 --price 250.5 \
    --grade B --score 75 --reason "모멘텀 상승" \
    [--stop-loss 243 --take-profit 268]

  python3 trade-logger.py close \
    --symbol TSLA --exit-price 268 --exit-reason TAKE_PROFIT

  python3 trade-logger.py close-all \
    --symbol TSLA --exit-price 268 --exit-reason STOP_LOSS

  python3 trade-logger.py list [--status open|closed|all]

  python3 trade-logger.py lesson --symbol TSLA --lesson "모멘텀 진입 성공" --score 8
"""

import json
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / "Library/Application Support/tossctl/trade-log.json"

# DB 연동
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from db import (init_db, insert_trade, close_trade_db, update_lesson_db,
                    close_all_trades_by_symbol, query_trades)
    init_db()
    _DB_OK = True
except Exception:
    _DB_OK = False


# ─── JSON 백업 (보조) ───

def _json_load() -> list:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text())
        except Exception:
            return []
    return []


def _json_save(trades: list):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text(json.dumps(trades, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _json_append(trade: dict):
    """JSON에 거래 추가 (백업용)"""
    trades = _json_load()
    trades.append(trade)
    _json_save(trades)


# ─── 명령어 ───

def log_trade(args: dict):
    """새 거래 기록"""
    trade = {
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
        "mode": args.get("mode", "manual"),
        "tags": [],
        "group_id": args.get("group-id"),
        "tranche_seq": int(args.get("tranche-seq", "1")),
        "screener_result_id": int(args["screener-result-id"]) if args.get("screener-result-id") else None,
        "intended_price": float(args["intended-price"]) if args.get("intended-price") else None,
    }

    db_id = None
    if _DB_OK:
        try:
            db_id = insert_trade(trade)
        except Exception:
            pass

    # JSON 백업
    trade["id"] = db_id or (len(_json_load()) + 1)
    _json_append(trade)
    print(json.dumps(trade, ensure_ascii=False, indent=2))


def close_trade(args: dict):
    """거래 청산 기록 (단일)"""
    symbol = args.get("symbol", "").upper()
    exit_price = float(args.get("exit-price", "0"))
    exit_reason = args.get("exit-reason", "MANUAL")
    exit_intended = float(args["exit-intended-price"]) if args.get("exit-intended-price") else None

    if _DB_OK:
        try:
            close_trade_db(symbol, exit_price, exit_reason,
                           exit_intended_price=exit_intended)
        except Exception as e:
            print(json.dumps({"error": f"DB close failed: {e}"}), file=sys.stderr)

    # JSON 백업 업데이트
    trades = _json_load()
    target = None
    for t in reversed(trades):
        if t.get("symbol") == symbol and t.get("status") == "open":
            target = t
            break

    if target:
        entry = target["entry_price"]
        cost_rate = 0.002
        gross_pnl = (exit_price - entry) / entry if entry > 0 else 0
        net_pnl = gross_pnl - cost_rate

        target["exit_price"] = exit_price
        target["exit_date"] = datetime.now().strftime("%Y-%m-%d")
        target["exit_reason"] = exit_reason
        target["pnl_pct"] = round(net_pnl * 100, 2)
        target["pnl_amount"] = round((exit_price - entry) * target["quantity"], 2)
        target["status"] = "closed"
        _json_save(trades)
        print(json.dumps(target, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"warning": f"no open trade in JSON for {symbol}, DB updated"}))


def close_all_trades(args: dict):
    """심볼의 모든 open 거래 일괄 청산"""
    symbol = args.get("symbol", "").upper()
    exit_price = float(args.get("exit-price", "0"))
    exit_reason = args.get("exit-reason", "MANUAL")
    exit_intended = float(args["exit-intended-price"]) if args.get("exit-intended-price") else None

    if _DB_OK:
        try:
            close_all_trades_by_symbol(symbol, exit_price, exit_reason,
                                       exit_intended_price=exit_intended)
        except Exception as e:
            print(json.dumps({"error": f"DB close-all failed: {e}"}), file=sys.stderr)

    # JSON 백업 업데이트
    trades = _json_load()
    closed = []
    for t in trades:
        if t.get("symbol") == symbol and t.get("status") == "open":
            entry = t["entry_price"]
            cost_rate = 0.002
            gross_pnl = (exit_price - entry) / entry if entry > 0 else 0
            net_pnl = gross_pnl - cost_rate
            t["exit_price"] = exit_price
            t["exit_date"] = datetime.now().strftime("%Y-%m-%d")
            t["exit_reason"] = exit_reason
            t["pnl_pct"] = round(net_pnl * 100, 2)
            t["pnl_amount"] = round((exit_price - entry) * t["quantity"], 2)
            t["status"] = "closed"
            closed.append(t)
    _json_save(trades)
    print(json.dumps(closed, ensure_ascii=False, indent=2))


def add_lesson(args: dict):
    """거래에 교훈 추가"""
    symbol = args.get("symbol", "").upper()
    lesson = args.get("lesson", "")
    score = int(args.get("score", "5"))

    if _DB_OK:
        try:
            update_lesson_db(symbol, lesson, score)
        except Exception:
            pass

    # JSON 백업 업데이트
    trades = _json_load()
    target = None
    for t in reversed(trades):
        if t.get("symbol") == symbol and t.get("status") == "closed":
            target = t
            break
    if target:
        target["lesson"] = lesson
        target["lesson_score"] = score
        _json_save(trades)
        print(json.dumps(target, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"info": "DB updated, no matching JSON entry"}))


def list_trades(args: dict):
    """거래 목록 조회 — DB 우선"""
    status = args.get("status", "all")
    if _DB_OK:
        try:
            trades = query_trades(status=status, limit=500)
            print(json.dumps(trades, ensure_ascii=False, indent=2))
            return
        except Exception:
            pass
    # DB 실패 시 JSON fallback
    trades = _json_load()
    if status != "all":
        trades = [t for t in trades if t.get("status") == status]
    print(json.dumps(trades, ensure_ascii=False, indent=2))


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "command required: log | close | close-all | lesson | list"}))
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
    elif command == "close-all":
        close_all_trades(args)
    elif command == "lesson":
        add_lesson(args)
    elif command == "list":
        list_trades(args)
    else:
        print(json.dumps({"error": f"unknown command: {command}"}))


if __name__ == "__main__":
    main()
