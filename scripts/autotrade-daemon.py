#!/usr/bin/env python3
"""
autotrade-daemon.py — Claude 없이 독립 실행되는 자동매매 데몬

사용법:
  python3 scripts/autotrade-daemon.py
  python3 scripts/autotrade-daemon.py --interval 300 --market us
  python3 scripts/autotrade-daemon.py --dry-run  # 주문 없이 시뮬레이션

환경변수 또는 signal-config.json에서 파라미터 로드.
"""

import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from enum import Enum

# ─── 경로 ───
HOME = Path.home()
SCRIPTS_DIR = Path(__file__).parent
CONFIG_DIR = HOME / "Library/Application Support/tossctl"
LOG_FILE = CONFIG_DIR / "trade-log.json"
SIGNAL_CONFIG = CONFIG_DIR / "signal-config.json"
PROTECTED_FILE = CONFIG_DIR / "protected-stocks.json"
WATCHLIST_FILE = CONFIG_DIR / "watchlist.json"
DAEMON_STATE_FILE = CONFIG_DIR / "daemon-state.json"

TOSS_BIN = HOME / "Desktop/Personal/Stock/tossinvest-cli/bin/tossctl"
TOSS_ENV = {
    **os.environ,
    "PATH": f"{TOSS_BIN.parent}:{os.environ.get('PATH', '')}",
    "TOSSCTL_AUTH_HELPER_DIR": str(HOME / "Desktop/Personal/Stock/tossinvest-cli/auth-helper"),
    "TOSSCTL_AUTH_HELPER_PYTHON": str(HOME / "Desktop/Personal/Stock/tossinvest-cli/auth-helper/.venv/bin/python3"),
}


# ─── 상태 ───

class DaemonStatus(str, Enum):
    RUNNING = "running"
    PAUSED_SESSION = "paused_session"     # 세션 만료
    PAUSED_MAINTENANCE = "paused_maintenance"  # 점검
    PAUSED_LOSS_LIMIT = "paused_loss_limit"    # 일일 손실 한도
    PAUSED_COOLDOWN = "paused_cooldown"        # 연속 손절 냉각
    STOPPED = "stopped"
    ERROR = "error"


class DaemonState:
    def __init__(self):
        self.status = DaemonStatus.RUNNING
        self.cycle_count = 0
        self.today_pnl = 0.0
        self.today_trades = 0
        self.consecutive_losses = 0
        self.last_error = None
        self.last_cycle_at = None
        self.started_at = datetime.now().isoformat()
        self.errors = []  # 최근 에러 로그
        self.load()

    def load(self):
        if DAEMON_STATE_FILE.exists():
            try:
                d = json.loads(DAEMON_STATE_FILE.read_text())
                # 날짜가 바뀌면 일일 통계 리셋
                if d.get("date") != datetime.now().strftime("%Y-%m-%d"):
                    self.today_pnl = 0.0
                    self.today_trades = 0
                    self.consecutive_losses = 0
                else:
                    self.today_pnl = d.get("today_pnl", 0)
                    self.today_trades = d.get("today_trades", 0)
                    self.consecutive_losses = d.get("consecutive_losses", 0)
            except Exception:
                pass

    def save(self):
        DAEMON_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DAEMON_STATE_FILE.write_text(json.dumps({
            "status": self.status.value,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "cycle_count": self.cycle_count,
            "today_pnl": round(self.today_pnl, 2),
            "today_trades": self.today_trades,
            "consecutive_losses": self.consecutive_losses,
            "last_error": self.last_error,
            "last_cycle_at": self.last_cycle_at,
            "started_at": self.started_at,
            "errors": self.errors[-10:],
        }, ensure_ascii=False, indent=2))

    def record_error(self, msg: str):
        entry = {"time": datetime.now().isoformat(), "msg": msg}
        self.errors.append(entry)
        self.last_error = msg
        log(f"  ERROR: {msg}")


# ─── 유틸리티 ───

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def run_tossctl(*args, timeout=15) -> dict | list | None:
    """tossctl 실행. 실패 시 None 반환."""
    try:
        result = subprocess.run(
            [str(TOSS_BIN), *args, "--output", "json"],
            capture_output=True, text=True, timeout=timeout, env=TOSS_ENV
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        # 490 = 점검
        if "490" in result.stderr:
            return {"_error": "maintenance", "detail": result.stderr.strip()}
        return {"_error": result.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"_error": "timeout"}
    except json.JSONDecodeError:
        return {"_error": "invalid_json", "raw": result.stdout[:200] if result else ""}
    except Exception as e:
        return {"_error": str(e)}


def run_script(script_name: str, *args, timeout=30) -> dict | list | None:
    """scripts/ 디렉토리의 Python 스크립트 실행"""
    try:
        result = subprocess.run(
            ["python3", str(SCRIPTS_DIR / script_name), *args],
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        return {"_error": result.stderr.strip()}
    except Exception as e:
        return {"_error": str(e)}


def load_protected() -> set:
    """보호 종목 심볼 세트"""
    if PROTECTED_FILE.exists():
        data = json.loads(PROTECTED_FILE.read_text())
        return {s["symbol"].upper() for s in data.get("stocks", [])}
    return set()


def load_config() -> dict:
    defaults = {
        "stop_loss_pct": -3.0, "take_profit_pct": 7.0,
        "max_positions": 2, "max_position_pct": 10,
        "daily_loss_limit_pct": -2.0, "entry_grades": ["A", "B"],
        "cooldown_after_consecutive_losses": 3,
        "cooldown_minutes": 30,
    }
    if SIGNAL_CONFIG.exists():
        try:
            user = json.loads(SIGNAL_CONFIG.read_text())
            defaults.update(user)
        except Exception:
            pass
    return defaults


def send_notification(title: str, msg: str):
    """macOS 알림 전송"""
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{msg}" with title "Toss Trading" subtitle "{title}"'
        ], timeout=5)
    except Exception:
        pass


def is_market_open(market: str = "us") -> bool:
    """장 시간인지 확인 (대략적)"""
    now = datetime.now()
    hour = now.hour
    weekday = now.weekday()
    if weekday >= 5:  # 주말
        return False
    if market == "kr":
        return 9 <= hour < 15 or (hour == 15 and now.minute <= 30)
    else:  # us (한국시간 기준)
        return hour >= 22 or hour < 6  # 대략적 (서머타임 변동 있음)


# ─── 핵심 사이클 ───

def check_session() -> bool:
    """세션 유효성 확인"""
    result = run_tossctl("auth", "status")
    if result is None or isinstance(result, dict) and result.get("_error"):
        return False
    if isinstance(result, dict):
        return result.get("valid", False) or result.get("active", False) or "active" in str(result)
    return False


def check_maintenance() -> bool:
    """점검 중인지 확인. True = 점검 중"""
    result = run_tossctl("account", "summary")
    if isinstance(result, dict) and result.get("_error") == "maintenance":
        return True
    if isinstance(result, dict) and "490" in str(result.get("_error", "")):
        return True
    return False


def monitor_positions(state: DaemonState, config: dict) -> list:
    """보유 포지션 손절/익절 체크. 매도 필요한 시그널 반환."""
    positions = run_tossctl("portfolio", "positions")
    if not isinstance(positions, list):
        return []

    result = run_script("signal-engine.py", "check-positions",
                        "--positions", json.dumps(positions),
                        "--config", json.dumps(config))

    if not isinstance(result, list):
        return []

    sell_signals = [s for s in result if s.get("action", "").startswith("SELL")]
    return sell_signals


def check_risk_gate(state: DaemonState, config: dict) -> bool:
    """리스크 게이트 통과 여부"""
    summary = run_tossctl("account", "summary")
    positions = run_tossctl("portfolio", "positions")
    if not isinstance(summary, dict) or summary.get("_error"):
        return False

    active = len(positions) if isinstance(positions, list) else 0

    result = run_script("signal-engine.py", "risk-gate",
                        "--portfolio", json.dumps(summary),
                        "--config", json.dumps(config),
                        "--today-pnl", str(state.today_pnl),
                        "--active-positions", str(active))

    if isinstance(result, dict):
        return result.get("passed", False)
    return False


def find_buy_candidates(config: dict) -> list:
    """스크리너로 매수 후보 탐색"""
    result = run_script("stock-screener.py", "scan",
                        "--source", "watchlist",
                        timeout=30)

    if not isinstance(result, dict):
        return []

    candidates = result.get("buy_candidates", [])
    entry_grades = config.get("entry_grades", ["A", "B"])
    return [c for c in candidates if c.get("grade") in entry_grades]


def execute_buy(symbol: str, price: float, qty: int, state: DaemonState, dry_run: bool) -> bool:
    """매수 실행"""
    log(f"  BUY: {symbol} x{qty} @ {price}")

    if dry_run:
        log(f"  [DRY RUN] 주문 스킵")
        return True

    # quick-order.sh 실행
    result = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "quick-order.sh"),
         "--symbol", symbol, "--side", "buy",
         "--qty", str(qty), "--price", str(int(price))],
        capture_output=True, text=True, timeout=30, env=TOSS_ENV
    )

    if result.returncode == 0:
        log(f"  BUY SUCCESS: {symbol}")
        # 거래 기록
        run_script("trade-logger.py", "log",
                    "--symbol", symbol, "--side", "buy",
                    "--qty", str(qty), "--price", str(price),
                    "--grade", "A", "--mode", "autotrade")
        state.today_trades += 1
        return True
    else:
        log(f"  BUY FAILED: {result.stderr[:200]}")
        state.record_error(f"BUY {symbol} failed: {result.stderr[:100]}")
        return False


def execute_sell(symbol: str, signal: dict, state: DaemonState, dry_run: bool) -> bool:
    """매도 실행"""
    price = signal.get("current_price", 0)
    log(f"  SELL: {symbol} @ {price} ({signal.get('action', '')})")

    if dry_run:
        log(f"  [DRY RUN] 주문 스킵")
        return True

    # 보유 수량 확인
    positions = run_tossctl("portfolio", "positions")
    if not isinstance(positions, list):
        return False

    pos = next((p for p in positions if p.get("symbol") == symbol), None)
    if not pos or pos.get("quantity", 0) <= 0:
        return False

    qty = int(pos["quantity"]) if pos["quantity"] == int(pos["quantity"]) else pos["quantity"]

    result = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "quick-order.sh"),
         "--symbol", symbol, "--side", "sell",
         "--qty", str(qty), "--price", str(int(price))],
        capture_output=True, text=True, timeout=30, env=TOSS_ENV
    )

    if result.returncode == 0:
        log(f"  SELL SUCCESS: {symbol}")
        pnl = signal.get("profit_rate", 0)
        state.today_pnl += pnl

        # 거래 기록
        run_script("trade-logger.py", "close",
                    "--symbol", symbol,
                    "--exit-price", str(price),
                    "--exit-reason", signal.get("action", "MANUAL"))

        if pnl < 0:
            state.consecutive_losses += 1
        else:
            state.consecutive_losses = 0

        state.today_trades += 1
        return True
    else:
        log(f"  SELL FAILED: {result.stderr[:200]}")
        state.record_error(f"SELL {symbol} failed: {result.stderr[:100]}")
        return False


# ─── 메인 루프 ───

def run_cycle(state: DaemonState, config: dict, dry_run: bool, market: str):
    """1회 사이클 실행"""
    state.cycle_count += 1
    state.last_cycle_at = datetime.now().isoformat()
    protected = load_protected()

    log(f"═══ 사이클 #{state.cycle_count} ═══")

    # 1. 세션 체크
    if not check_session():
        state.status = DaemonStatus.PAUSED_SESSION
        log("  세션 만료! 재로그인 필요.")
        send_notification("세션 만료", "tossctl auth login으로 재로그인하세요")
        return False

    # 2. 점검 체크
    if check_maintenance():
        state.status = DaemonStatus.PAUSED_MAINTENANCE
        log("  시스템 점검 중. 대기.")
        return False

    # 3. 장 시간 체크
    if not is_market_open(market):
        log(f"  장 휴장 ({market}). 스킵.")
        return True  # 에러는 아님

    # 4. 일일 손실 한도 체크 (데몬 운용 금액 기준, 보호종목 무관)
    total_asset = 0
    orderable_krw = 0
    summary = run_tossctl("account", "summary")
    if isinstance(summary, dict) and not summary.get("_error"):
        total_asset = summary.get("total_asset_amount", 0)
        orderable_krw = summary.get("orderable_amount_krw", 0)
    # 분모: 데몬이 운용하는 규모만 (주문가능금액 + 오늘 실현 손익)
    daemon_capital = orderable_krw + abs(state.today_pnl)
    if daemon_capital > 0:
        loss_pct = state.today_pnl / daemon_capital * 100
        limit = config.get("daily_loss_limit_pct", -2.0)
        if loss_pct <= limit:
            state.status = DaemonStatus.PAUSED_LOSS_LIMIT
            log(f"  일일 손실 한도 도달: {loss_pct:.2f}% (한도: {limit}%, 운용금: {daemon_capital:,.0f}원)")
            send_notification("거래 중단", f"일일 손실 {loss_pct:.1f}%로 한도 도달")
            return False

    # 5. 연속 손절 냉각
    cooldown_limit = config.get("cooldown_after_consecutive_losses", 3)
    if state.consecutive_losses >= cooldown_limit:
        state.status = DaemonStatus.PAUSED_COOLDOWN
        cooldown_min = config.get("cooldown_minutes", 30)
        log(f"  연속 {state.consecutive_losses}회 손절. {cooldown_min}분 냉각.")
        send_notification("냉각기", f"연속 {state.consecutive_losses}회 손절")
        time.sleep(cooldown_min * 60)
        state.consecutive_losses = 0
        state.status = DaemonStatus.RUNNING

    # 6. 보유 포지션 손절/익절 체크
    sell_signals = monitor_positions(state, config)
    for sig in sell_signals:
        sym = sig.get("symbol", "")
        if sym in protected:
            log(f"  {sym} 보호종목 → 매도 스킵")
            continue
        execute_sell(sym, sig, state, dry_run)

    # 7. 리스크 게이트
    if not check_risk_gate(state, config):
        log("  리스크 게이트 미통과 → 신규 매수 스킵")
        state.status = DaemonStatus.RUNNING
        return True

    # 8. 매수 후보 탐색
    candidates = find_buy_candidates(config)
    if not candidates:
        log("  매수 후보 없음")
        state.status = DaemonStatus.RUNNING
        return True

    # 9. 매수 실행 — 포지션 한도까지 병렬 진입
    max_pos = config.get("max_positions", 2)
    # 현재 보유 포지션 수 (보호종목 제외)
    current_positions = set()
    if isinstance(positions, list):
        for p in positions:
            sym_p = p.get("symbol", p.get("product_code", ""))
            if p.get("quantity", 0) > 0 and sym_p not in protected:
                current_positions.add(sym_p)

    slots = max_pos - len(current_positions)
    if slots <= 0:
        log(f"  포지션 한도 도달 ({len(current_positions)}/{max_pos}) → 매수 스킵")
    else:
        orderable = 0
        if isinstance(summary, dict) and not summary.get("_error"):
            orderable = summary.get("orderable_amount_krw", 0)

        # 잔여 슬롯만큼 후보 순회하며 매수
        bought = 0
        for c in candidates:
            if bought >= slots:
                break

            sym = c.get("symbol", "")
            if sym in protected or sym in current_positions:
                log(f"  {sym} 보호/보유 종목 → 스킵")
                continue

            price = c.get("current_price", 0)
            if price <= 0:
                continue

            # 주문가능금액을 남은 슬롯에 균등 배분
            per_slot = orderable / (slots - bought) if (slots - bought) > 0 else 0
            max_invest = min(per_slot, total_asset * config.get("max_position_pct", 10) / 100)

            if max_invest < price:
                log(f"  {sym} 금액 부족 (슬롯당 {per_slot:,.0f}원 < {price:,.0f}원) → 스킵")
                continue

            qty = max(1, int(max_invest / price))
            cost = qty * price

            if execute_buy(sym, price, qty, state, dry_run):
                orderable -= cost  # 잔여 주문가능금액 차감
                current_positions.add(sym)
                bought += 1

        if bought > 0:
            log(f"  {bought}종목 매수 완료 (포지션: {len(current_positions)}/{max_pos})")

    state.status = DaemonStatus.RUNNING
    return True


def main():
    # CLI 인자 파싱
    args = {}
    for i in range(1, len(sys.argv)):
        if sys.argv[i].startswith("--"):
            key = sys.argv[i][2:]
            if i + 1 < len(sys.argv) and not sys.argv[i+1].startswith("--"):
                args[key] = sys.argv[i + 1]
            else:
                args[key] = "true"

    interval = int(args.get("interval", "300"))  # 5분 기본
    market = args.get("market", "us")
    dry_run = "dry-run" in args

    config = load_config()
    state = DaemonState()
    state.status = DaemonStatus.RUNNING

    print("╔══════════════════════════════════════╗")
    print("║   toss-trading-system autotrade      ║")
    print("╠══════════════════════════════════════╣")
    print(f"║  간격: {interval}초 | 시장: {market.upper()}")
    print(f"║  손절: {config['stop_loss_pct']}% | 익절: {config['take_profit_pct']}%")
    print(f"║  등급: {','.join(config['entry_grades'])} | 포지션: {config['max_positions']}개")
    print(f"║  모드: {'DRY RUN (시뮬레이션)' if dry_run else 'LIVE (실거래)'}")
    print("╚══════════════════════════════════════╝")

    if not dry_run:
        print("\n  ⚠️  실거래 모드입니다. 10초 후 시작합니다...")
        print("  Ctrl+C로 취소할 수 있습니다.\n")
        time.sleep(10)

    retry_count = 0
    max_retries = 5

    while True:
        try:
            ok = run_cycle(state, config, dry_run, market)
            state.save()

            if ok:
                retry_count = 0  # 성공 시 리셋
                log(f"  다음 사이클: {interval}초 후")
                time.sleep(interval)
            else:
                # 실패 시 백오프
                retry_count += 1
                if retry_count >= max_retries:
                    log(f"  연속 {max_retries}회 실패. 5분 대기 후 재시도.")
                    send_notification("에러", f"연속 {max_retries}회 실패")
                    time.sleep(300)
                    retry_count = 0
                else:
                    wait = min(60 * retry_count, 300)
                    log(f"  {wait}초 후 재시도 ({retry_count}/{max_retries})")
                    time.sleep(wait)

            # 설정 리로드 (파라미터 조정 반영)
            config = load_config()

        except KeyboardInterrupt:
            log("\n  사용자 중단")
            state.status = DaemonStatus.STOPPED
            state.save()
            break

        except Exception as e:
            state.record_error(str(e))
            state.status = DaemonStatus.ERROR
            state.save()
            log(f"  예외: {e}")
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    main()
