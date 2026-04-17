#!/usr/bin/env python3
"""
notify.py — Discord 알림 모듈

모든 컴포넌트에서 import하여 사용.

사용법:
  from notify import notify_trade, notify_error, notify_session, notify_report

  # 또는 CLI
  python3 scripts/notify.py trade --symbol TSLA --side buy --price 250 --qty 10 --grade A
  python3 scripts/notify.py error --message "세션 만료"
  python3 scripts/notify.py session --status expired
  python3 scripts/notify.py report --type daily
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# .env 로드
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# 색상
GREEN = 0x22c55e
RED = 0xef4444
YELLOW = 0xeab308
BLUE = 0x3b82f6
PURPLE = 0xa855f7

# 거래 로그 / 데몬 상태 경로
CONFIG_DIR = Path.home() / "Library/Application Support/tossctl"
LOG_FILE = CONFIG_DIR / "trade-log.json"
DAEMON_STATE_FILE = CONFIG_DIR / "daemon-state.json"

# 모드: "live" 또는 "dry-run" — 데몬에서 설정하거나 CLI --mode로 전달
_MODE = os.environ.get("TOSS_TRADE_MODE", "live")

def _set_mode(mode: str):
    global _MODE
    _MODE = mode


def _footer() -> dict:
    label = "DRY RUN" if _MODE == "dry-run" else "LIVE"
    return {"text": f"toss-trading-system | {label}"}


def _send(embeds: list, retries: int = 2):
    """Discord 웹훅 전송 (재시도 포함)"""
    data = json.dumps({"embeds": embeds}).encode("utf-8")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
            return True
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
            else:
                print(f"[Discord 알림 실패] {e} ({retries+1}회 시도)", file=sys.stderr)
                return False


def notify_trade(symbol: str, side: str, price: float, qty: int,
                 grade: str = "", score: float = 0, strategy: str = "",
                 pnl_pct: float = None, exit_reason: str = None):
    """거래 알림 (매수/매도)"""
    if side == "buy":
        title = f"🔵 매수 | {symbol}"
        color = BLUE
        desc = f"**{qty}주** @ **{price:,.2f}**"
        if grade: desc += f"\n등급: **{grade}** ({score:.0f}점)"
        if strategy: desc += f"\n전략: {strategy}"
    else:
        title = f"{'🟢' if (pnl_pct or 0) >= 0 else '🔴'} 매도 | {symbol}"
        color = GREEN if (pnl_pct or 0) >= 0 else RED
        desc = f"**{qty}주** @ **{price:,.2f}**"
        if pnl_pct is not None: desc += f"\n수익: **{pnl_pct:+.2f}%**"
        if exit_reason: desc += f"\n사유: {exit_reason}"

    _send([{"title": title, "description": desc, "color": color,
            "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])


def notify_signal(symbol: str, action: str, reason: str, profit_rate: float = 0):
    """시그널 알림 (손절/익절/트레일링/급락)"""
    icons = {"SELL_STOP_LOSS": "🔴 손절", "SELL_TAKE_PROFIT": "🟢 익절",
             "SELL_TRAILING_STOP": "🟡 트레일링", "ALERT_SHARP_DROP": "⚠️ 급락",
             "TREND_BREAK": "📉 추세파괴"}
    title = f"{icons.get(action, '📡')} | {symbol}"
    color = RED if "LOSS" in action or "DROP" in action or "BREAK" in action else GREEN

    _send([{"title": title, "description": f"{reason}\n수익률: **{profit_rate:+.2f}%**",
            "color": color, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])


def notify_error(message: str, detail: str = ""):
    """에러/경고 알림"""
    _send([{"title": "⚠️ 시스템 경고", "description": f"**{message}**\n{detail}",
            "color": YELLOW, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])


def notify_session(status: str, message: str = ""):
    """세션 상태 알림"""
    if status == "expired":
        _send([{"title": "🔴 세션 만료", "description": f"토스증권 세션이 만료되었습니다.\n{message}\n`tossctl auth login`으로 재로그인하세요.",
                "color": RED, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])
    elif status == "restored":
        _send([{"title": "🟢 세션 복원", "description": "토스증권 세션이 정상 복원되었습니다.",
                "color": GREEN, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])
    elif status == "maintenance":
        _send([{"title": "🔧 시스템 점검", "description": f"토스증권 점검 중\n{message}",
                "color": YELLOW, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])


def notify_daily_report(report: dict):
    """일일 보고서 알림"""
    p = report.get("period", {})
    c = report.get("cumulative", {})
    pnl = p.get("total_pnl", 0)
    color = GREEN if pnl >= 0 else RED

    desc = f"거래: **{p.get('trades', 0)}건** | 승률: **{p.get('win_rate', 0)}%**\n"
    desc += f"수익: **{pnl:+.2f}%**\n"
    desc += f"누적: {c.get('total_trades', 0)}건 {c.get('total_pnl', 0):+.2f}%"

    # 종목별
    by_sym = report.get("by_symbol", {})
    if by_sym:
        desc += "\n\n**종목별:**\n"
        for s, v in list(by_sym.items())[:5]:
            desc += f"  {s}: {v.get('pnl', 0):+.2f}% ({v.get('trades', 0)}건)\n"

    _send([{"title": f"📊 {report.get('label', '일일')} 보고서 | {report.get('date', '')}",
            "description": desc, "color": color,
            "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])


def notify_scan(detail: str = ""):
    """스캔 결과 알림"""
    _send([{"title": "🔍 스캔 결과", "description": detail,
            "color": BLUE, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])


def notify_daemon_status(status: str, detail: str = ""):
    """데몬 상태 변경 알림"""
    icons = {"running": "🟢", "paused_session": "🔴", "paused_maintenance": "🔧",
             "paused_loss_limit": "🚫", "paused_cooldown": "❄️", "stopped": "⬛", "error": "⚠️"}
    labels = {"running": "실행 중", "paused_session": "세션 만료", "paused_maintenance": "점검 대기",
              "paused_loss_limit": "손실 한도", "paused_cooldown": "냉각기", "stopped": "정지", "error": "에러"}

    _send([{"title": f"{icons.get(status, '❓')} 데몬: {labels.get(status, status)}",
            "description": detail or "", "color": YELLOW if "paused" in status else (GREEN if status == "running" else RED),
            "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])


# ─── 조회 명령 (Discord에 결과 전송) ───

def _load_trades(status="all"):
    """DB에서 거래 조회, 실패 시 JSON fallback"""
    try:
        import sys as _sys; _sys.path.insert(0, str(Path(__file__).parent))
        from db import query_trades
        return query_trades(status=status, limit=500)
    except Exception:
        trades = json.loads(LOG_FILE.read_text()) if LOG_FILE.exists() else []
        if status != "all":
            return [t for t in trades if t.get("status") == status]
        return trades


def query_positions():
    """현재 보유 포지션을 Discord에 전송"""
    open_trades = _load_trades(status="open")

    if not open_trades:
        _send([{"title": "📋 보유 포지션", "description": "현재 보유 중인 포지션이 없습니다.",
                "color": PURPLE, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])
        return

    fields = []
    for t in open_trades:
        entry = t.get("entry_price", 0)
        grade = t.get("entry_grade", "?")
        mode = "🤖" if t.get("mode") == "autotrade" else "🔧" if t.get("mode") == "dry-run" else "👤"
        fields.append({
            "name": f"{mode} {t['symbol']}",
            "value": f"{t.get('quantity', 0):.0f}주 @ {entry:,.2f}\n등급: {grade} | {t.get('entry_date', '')}",
            "inline": True
        })

    _send([{"title": f"📋 보유 포지션 ({len(open_trades)}건)", "color": PURPLE,
            "fields": fields[:25], "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])


def query_recent_trades(count: int = 5):
    """최근 청산 거래를 Discord에 전송"""
    closed = _load_trades(status="closed")
    recent = closed[-count:][::-1]  # 최신순

    if not recent:
        _send([{"title": "📜 최근 거래", "description": "청산된 거래가 없습니다.",
                "color": PURPLE, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])
        return

    wins = sum(1 for t in closed if (t.get("pnl_pct") or 0) > 0)
    total_pnl = sum(t.get("pnl_pct", 0) or 0 for t in closed)
    win_rate = (wins / len(closed) * 100) if closed else 0

    desc = f"전체: {len(closed)}건 | 승률: {win_rate:.0f}% | 누적: {total_pnl:+.2f}%\n\n"
    for t in recent:
        pnl = t.get("pnl_pct", 0) or 0
        icon = "🟢" if pnl >= 0 else "🔴"
        reason = t.get("exit_reason", "")
        mode_tag = " `DRY`" if t.get("mode") == "dry-run" else ""
        desc += f"{icon} **{t['symbol']}** {pnl:+.2f}%{mode_tag} — {reason} ({t.get('exit_date', '')})\n"

    _send([{"title": f"📜 최근 거래 (최근 {len(recent)}건)", "description": desc,
            "color": PURPLE, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])


def query_today():
    """오늘 거래 요약을 Discord에 전송"""
    trades = _load_trades(status="all")
    today = datetime.now().strftime("%Y-%m-%d")

    today_trades = [t for t in trades if t.get("entry_date") == today or t.get("exit_date") == today]
    opened = [t for t in today_trades if t.get("entry_date") == today]
    closed = [t for t in today_trades if t.get("exit_date") == today and t.get("status") == "closed"]

    pnl = sum(t.get("pnl_pct", 0) or 0 for t in closed)
    wins = sum(1 for t in closed if (t.get("pnl_pct") or 0) > 0)

    desc = f"**매수**: {len(opened)}건 | **청산**: {len(closed)}건\n"
    if closed:
        desc += f"**수익**: {pnl:+.2f}% | **승률**: {wins}/{len(closed)}\n\n"
        for t in closed:
            p = t.get("pnl_pct", 0) or 0
            icon = "🟢" if p >= 0 else "🔴"
            desc += f"{icon} {t['symbol']} {p:+.2f}% ({t.get('exit_reason', '')})\n"
    else:
        desc += "청산 내역 없음\n"

    # 현재 보유
    still_open = [t for t in today_trades if t.get("status") == "open"]
    if still_open:
        desc += f"\n**보유 중**: "
        desc += ", ".join(t["symbol"] for t in still_open)

    color = GREEN if pnl > 0 else (RED if pnl < 0 else PURPLE)
    _send([{"title": f"📅 오늘 거래 요약 | {today}", "description": desc,
            "color": color, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])


def query_daemon():
    """데몬 상태를 Discord에 전송"""
    if not DAEMON_STATE_FILE.exists():
        _send([{"title": "🤖 데몬 상태", "description": "데몬 상태 파일이 없습니다. 실행 중이 아닐 수 있습니다.",
                "color": YELLOW, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])
        return

    d = json.loads(DAEMON_STATE_FILE.read_text())
    status = d.get("status", "unknown")
    icons = {"running": "🟢", "paused_session": "🔴", "paused_maintenance": "🔧",
             "paused_loss_limit": "🚫", "paused_cooldown": "❄️", "stopped": "⬛", "error": "⚠️"}

    desc = f"**상태**: {icons.get(status, '❓')} {status}\n"
    desc += f"**사이클**: {d.get('cycle_count', 0)}회\n"
    desc += f"**오늘 거래**: {d.get('today_trades', 0)}건\n"
    desc += f"**오늘 손익**: {d.get('today_pnl', 0):+.2f}%\n"
    desc += f"**연속 손절**: {d.get('consecutive_losses', 0)}회\n"
    desc += f"**마지막 사이클**: {d.get('last_cycle_at', '-')}\n"

    errors = d.get("errors", [])
    if errors:
        last = errors[-1]
        desc += f"\n**최근 에러**: {last.get('msg', '')[:100]}\n`{last.get('time', '')}`"

    color = GREEN if status == "running" else (RED if status in ("error", "stopped") else YELLOW)
    _send([{"title": "🤖 데몬 상태", "description": desc,
            "color": color, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])


# CLI
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: notify.py <command> [--mode live|dry-run] [args...]")
        print("  알림: trade|signal|error|session|daemon|report|test")
        print("  조회: positions|trades|today|status")
        print("  기타: help (Discord로 명령어 목록 전송)")
        sys.exit(1)

    cmd = sys.argv[1]
    args = {}
    i = 2
    while i < len(sys.argv):
        if sys.argv[i].startswith("--"):
            args[sys.argv[i][2:]] = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""; i += 2
        else: i += 1

    # 모드 설정 (CLI --mode가 환경변수보다 우선)
    if args.get("mode"):
        _set_mode(args["mode"])

    if cmd == "help":
        _send([{"title": "📖 notify.py 명령어 목록", "description": "\n".join([
            "**알림 명령:**",
            "`trade` — 매수/매도 알림",
            "`signal` — 시그널 알림 (손절/익절/트레일링)",
            "`error` — 에러/경고 알림",
            "`session` — 세션 상태 알림",
            "`daemon` — 데몬 상태 변경 알림",
            "`report` — 일일 보고서 생성 및 전송",
            "",
            "**조회 명령:**",
            "`positions` — 현재 보유 포지션",
            "`trades [--count N]` — 최근 N건 청산 내역 (기본 5)",
            "`today` — 오늘 거래 요약",
            "`status` — 데몬 상태 조회",
            "",
            "**기타:**",
            "`test` — 연동 테스트",
            "`help` — 이 도움말",
            "",
            "**공통 옵션:** `--mode live|dry-run`",
        ]), "color": PURPLE, "footer": _footer()}])
    elif cmd == "test":
        _send([{"title": "✅ 알림 테스트", "description": "Discord 연동이 정상 작동합니다.",
                "color": GREEN, "footer": _footer()}])
    elif cmd == "trade":
        pnl = float(args["pnl_pct"]) if args.get("pnl_pct") else None
        notify_trade(args.get("symbol", "TEST"), args.get("side", "buy"), float(args.get("price", 0)), int(args.get("qty", 0)),
                     args.get("grade", ""), float(args.get("score", 0)), args.get("strategy", ""),
                     pnl_pct=pnl, exit_reason=args.get("exit_reason"))
    elif cmd == "signal":
        notify_signal(args.get("symbol", ""), args.get("action", ""), args.get("reason", ""),
                      float(args.get("profit_rate", 0)))
    elif cmd == "scan":
        _send([{"title": "🔍 스캔 결과", "description": args.get("detail", ""),
                "color": BLUE, "timestamp": datetime.utcnow().isoformat(), "footer": _footer()}])
    elif cmd == "daemon":
        notify_daemon_status(args.get("status", ""), args.get("detail", ""))
    elif cmd == "error":
        notify_error(args.get("message", "테스트 에러"))
    elif cmd == "session":
        notify_session(args.get("status", "expired"), args.get("message", ""))
    elif cmd == "report":
        import subprocess
        r = subprocess.run(["python3", str(Path(__file__).parent / "report-generator.py"), args.get("type", "daily")],
                          capture_output=True, text=True)
        if r.returncode == 0:
            notify_daily_report(json.loads(r.stdout))
    # 조회 명령
    elif cmd == "positions":
        query_positions()
    elif cmd == "trades":
        query_recent_trades(int(args.get("count", "5")))
    elif cmd == "today":
        query_today()
    elif cmd == "status":
        query_daemon()
    else:
        print(f"unknown command: {cmd}")
        sys.exit(1)
    print("sent")
