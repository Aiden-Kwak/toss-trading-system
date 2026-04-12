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

# 경로: 환경변수 우선, 없으면 기본값 사용
_default_bin = HOME / "Desktop/Personal/Stock/tossinvest-cli/bin/tossctl"
_default_helper = HOME / "Desktop/Personal/Stock/tossinvest-cli/auth-helper"
TOSS_BIN = Path(os.environ.get("TOSSCTL_BIN", str(_default_bin)))
TOSS_HELPER_DIR = os.environ.get("TOSSCTL_AUTH_HELPER_DIR", str(_default_helper))
TOSS_HELPER_PY = os.environ.get("TOSSCTL_AUTH_HELPER_PYTHON", str(Path(TOSS_HELPER_DIR) / ".venv/bin/python3"))

TOSS_ENV = {
    **os.environ,
    "PATH": f"{TOSS_BIN.parent}:{os.environ.get('PATH', '')}",
    "TOSSCTL_AUTH_HELPER_DIR": TOSS_HELPER_DIR,
    "TOSSCTL_AUTH_HELPER_PYTHON": TOSS_HELPER_PY,
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
        env = {**os.environ, "TOSS_TRADE_MODE": os.environ.get("TOSS_TRADE_MODE", "live")}
        result = subprocess.run(
            ["python3", str(SCRIPTS_DIR / script_name), *args],
            capture_output=True, text=True, timeout=timeout, env=env
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
    """macOS + Discord 알림 전송"""
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{msg}" with title "Toss Trading" subtitle "{title}"'
        ], timeout=5)
    except Exception:
        pass
    # Discord 알림
    try:
        run_script("notify.py", "error", "--message", f"{title}: {msg}")
    except Exception:
        pass


def is_market_open(market: str = "us") -> bool:
    """장 시간인지 확인 (KST 기준 명시)"""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Seoul"))
    except ImportError:
        now = datetime.now()  # 폴백: 시스템 시간 (KST 가정)

    hour, minute = now.hour, now.minute
    weekday = now.weekday()
    if weekday >= 5:  # 주말
        return False
    if market == "kr":
        # 코스피/코스닥: 09:00~15:30 KST
        return 9 <= hour < 15 or (hour == 15 and minute <= 30)
    else:
        # US: 22:30~05:00 KST (겨울) / 21:30~04:00 KST (서머타임)
        # 넉넉하게 21:00~06:00으로 설정
        return hour >= 21 or hour < 6


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
    """스크리너로 매수 후보 탐색 + 기술적 지표 + 추세추종"""
    result = run_script("stock-screener.py", "scan",
                        "--source", "watchlist",
                        timeout=30)

    if not isinstance(result, dict):
        return []

    candidates = result.get("buy_candidates", []) + result.get("watch_list", [])
    entry_grades = config.get("entry_grades", ["A", "B"])
    enriched = []

    for c in candidates:
        sym = c.get("symbol", "")
        product_code = c.get("product_code", sym)

        # 기술적 지표 가져오기 (토스 public API)
        tech = run_script("technical-indicators.py", "analyze",
                          "--symbol", product_code,
                          "--market", "kr" if c.get("market_code", "") in ("KSP", "KSQ") else "us")

        if isinstance(tech, dict) and not tech.get("_error") and not tech.get("error"):
            # 추세추종 판단: 골든크로스 + 상승 추세
            ema9 = tech.get("ema", {}).get("ema9", 0)
            ema21 = tech.get("ema", {}).get("ema21", 0)
            is_uptrend = ema9 > ema21 and tech.get("current_price", 0) > ema21

            if is_uptrend:
                # 추세추종 후보 (등급 무관)
                c["grade"] = "T"
                c["strategy"] = "trend_follow"
                c["score"] = 99
                c["tech"] = tech
                enriched.append(c)
                continue

        # 일반 단타 후보 (등급 기반)
        if c.get("grade") in entry_grades:
            c["strategy"] = "daytrade"
            c["tech"] = tech if isinstance(tech, dict) else None
            enriched.append(c)

    return enriched


def _generate_auto_lesson(symbol: str, pnl: float, exit_reason: str, signal: dict) -> str:
    """청산 결과에서 자동 교훈 생성"""
    profit_rate = signal.get("profit_rate", 0)
    daily_rate = signal.get("daily_rate", 0)

    if exit_reason == "SELL_STOP_LOSS":
        if abs(profit_rate) > 5:
            return f"손절 {profit_rate:.1f}%. 손절선 도달 전 이미 큰 하락. 진입 타이밍 검토 필요"
        return f"손절 {profit_rate:.1f}%. 진입 후 빠른 하락. 모멘텀/추세 확인 강화 필요"

    elif exit_reason == "SELL_TAKE_PROFIT":
        return f"익절 +{abs(profit_rate):.1f}%. 목표가 도달 성공. 진입 판단 적절"

    elif exit_reason == "SELL_TRAILING_STOP":
        return f"트레일링 스톱 {profit_rate:.1f}%. 수익 보호 성공. 고점 대비 하락으로 청산"

    elif exit_reason == "MARKET_CLOSE":
        if pnl > 0:
            return f"장마감 청산 +{pnl:.1f}%. 익절선 미도달이지만 수익. 익절선 하향 검토"
        else:
            return f"장마감 청산 {pnl:.1f}%. 최대보유기간 내 회복 실패"

    return f"청산 {pnl:.1f}% ({exit_reason})"


def execute_buy(symbol: str, price: float, qty: int, state: DaemonState, dry_run: bool,
                grade: str = "A", score: float = 0, reason: str = "") -> bool:
    """매수 실행"""
    log(f"  BUY: {symbol} x{qty} @ {price} ({grade}등급, {score}점)")

    if dry_run:
        log(f"  [DRY RUN] 주문 스킵")
        # dry-run에서도 기록 (시뮬레이션 추적용)
        run_script("trade-logger.py", "log",
                    "--symbol", symbol, "--side", "buy",
                    "--qty", str(qty), "--price", str(price),
                    "--grade", grade, "--score", str(score),
                    "--reason", reason or "autotrade-dry-run",
                    "--mode", "dry-run")
        state.today_trades += 1
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
        run_script("notify.py", "trade", "--symbol", symbol, "--side", "buy",
                    "--price", str(price), "--qty", str(qty), "--grade", grade, "--score", str(score), "--strategy", reason)
        log_result = run_script("trade-logger.py", "log",
                    "--symbol", symbol, "--side", "buy",
                    "--qty", str(qty), "--price", str(price),
                    "--grade", grade, "--score", str(score),
                    "--reason", reason or "autotrade",
                    "--mode", "autotrade")
        if isinstance(log_result, dict) and log_result.get("_error"):
            state.record_error(f"BUY {symbol} 성공했지만 기록 실패: {log_result['_error']}")
            send_notification("기록 오류", f"{symbol} 매수 기록 실패 - 수동 확인 필요")
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
        exit_reason = signal.get("action", "MANUAL")
        run_script("notify.py", "trade", "--symbol", symbol, "--side", "sell",
                    "--price", str(price), "--qty", str(qty),
                    "--pnl_pct", str(round(pnl, 2)), "--exit_reason", exit_reason)

        # 거래 기록
        close_result = run_script("trade-logger.py", "close",
                    "--symbol", symbol,
                    "--exit-price", str(price),
                    "--exit-reason", exit_reason)
        if isinstance(close_result, dict) and close_result.get("_error"):
            state.record_error(f"SELL {symbol} 성공했지만 기록 실패: {close_result['_error']}")

        # 자동 교훈 생성
        auto_lesson = _generate_auto_lesson(symbol, pnl, exit_reason, signal)
        if auto_lesson:
            lesson_score = 7 if pnl > 0 else 3
            run_script("trade-logger.py", "lesson",
                        "--symbol", symbol,
                        "--lesson", auto_lesson,
                        "--score", str(lesson_score))

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
        prev = state.status
        state.status = DaemonStatus.PAUSED_SESSION
        log("  세션 만료! 재로그인 필요.")
        run_script("notify.py", "session", "--status", "expired")
        if prev != DaemonStatus.PAUSED_SESSION:
            run_script("notify.py", "daemon", "--status", "paused_session", "--detail", "세션 만료로 거래 중단")
        return False

    # 세션이 복원된 경우 (이전에 만료였다면)
    if state.status == DaemonStatus.PAUSED_SESSION:
        run_script("notify.py", "session", "--status", "restored")

    # 2. 점검 체크
    if check_maintenance():
        prev = state.status
        state.status = DaemonStatus.PAUSED_MAINTENANCE
        log("  시스템 점검 중. 대기.")
        if prev != DaemonStatus.PAUSED_MAINTENANCE:
            run_script("notify.py", "session", "--status", "maintenance")
            run_script("notify.py", "daemon", "--status", "paused_maintenance", "--detail", "토스증권 시스템 점검 중")
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
            run_script("notify.py", "daemon", "--status", "paused_loss_limit",
                        "--detail", f"일일 손실 {loss_pct:.1f}% (한도 {limit}%)")
            return False

    # 5. 연속 손절 냉각
    cooldown_limit = config.get("cooldown_after_consecutive_losses", 3)
    if state.consecutive_losses >= cooldown_limit:
        state.status = DaemonStatus.PAUSED_COOLDOWN
        cooldown_min = config.get("cooldown_minutes", 30)
        log(f"  연속 {state.consecutive_losses}회 손절. {cooldown_min}분 냉각.")
        send_notification("냉각기", f"연속 {state.consecutive_losses}회 손절")
        run_script("notify.py", "daemon", "--status", "paused_cooldown",
                    "--detail", f"연속 {state.consecutive_losses}회 손절. {cooldown_min}분 대기")
        time.sleep(cooldown_min * 60)
        state.consecutive_losses = 0
        state.status = DaemonStatus.RUNNING
        run_script("notify.py", "daemon", "--status", "running", "--detail", "냉각기 종료. 거래 재개")

    # 6. 보유 포지션 손절/익절 체크
    sell_signals = monitor_positions(state, config)
    for sig in sell_signals:
        sym = sig.get("symbol", "")
        if sym in protected:
            log(f"  {sym} 보호종목 → 매도 스킵")
            continue
        # 시그널 감지 알림 (매도 전)
        run_script("notify.py", "signal", "--symbol", sym,
                    "--action", sig.get("action", ""),
                    "--reason", sig.get("reason", ""),
                    "--profit_rate", str(sig.get("profit_rate", 0)))
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

            strategy = c.get("strategy", "daytrade")
            if execute_buy(sym, price, qty, state, dry_run,
                           grade=c.get("grade", "?"), score=c.get("score", 0),
                           reason=f"{strategy}:{c.get('recommendation', '')}"):
                orderable -= cost  # 잔여 주문가능금액 차감
                current_positions.add(sym)
                bought += 1

        if bought > 0:
            log(f"  {bought}종목 매수 완료 (포지션: {len(current_positions)}/{max_pos})")

    state.status = DaemonStatus.RUNNING

    # 매 20사이클마다 자동 학습 실행
    if state.cycle_count % 20 == 0 and state.cycle_count > 0:
        _auto_learn(config)

    return True


def _auto_learn(config: dict):
    """축적된 거래 데이터로 자동 학습 & 파라미터 조정"""
    log("  [학습] 자동 분석 실행...")
    result = run_script("trade-analyzer.py", "suggest")
    if not isinstance(result, dict):
        return

    analysis = result.get("analysis", {})
    suggestions = result.get("suggestions", [])
    closed = analysis.get("closed_trades", 0)

    if closed < 10:
        log(f"  [학습] 데이터 부족 ({closed}건 < 10건). 스킵")
        return

    # 자동 적용 가능한 제안만 처리
    applied = []
    for s in suggestions:
        if s.get("param") and s.get("suggested") is not None and s["type"] in ("warning", "success"):
            # config 파일에 반영
            config[s["param"]] = s["suggested"]
            applied.append(f"{s['param']}: {s.get('current')} → {s['suggested']}")

    if applied:
        # config 저장
        SIGNAL_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        for a in applied:
            log(f"  [학습] 자동 적용: {a}")
        send_notification("학습 완료", f"{len(applied)}개 파라미터 조정됨")
    else:
        log("  [학습] 조정 불필요. 현재 설정 유지")

    # Kelly 계산 (충분한 데이터가 있을 때)
    if closed >= 20:
        win_rate = analysis.get("win_rate", 50) / 100
        avg_win = abs(analysis.get("avg_win", 5))
        avg_loss = abs(analysis.get("avg_loss", 3))
        if avg_loss > 0:
            b = avg_win / avg_loss
            kelly = (b * win_rate - (1 - win_rate)) / b
            half_kelly = max(2, min(25, kelly * 0.5 * 100))
            current_pct = config.get("max_position_pct", 10)
            if abs(half_kelly - current_pct) > 2:  # 2%p 이상 차이나면
                config["max_position_pct"] = round(half_kelly, 1)
                SIGNAL_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2))
                log(f"  [학습] Kelly 사이징: {current_pct}% → {half_kelly:.1f}%")


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

    # notify.py에 모드 전달
    os.environ["TOSS_TRADE_MODE"] = "dry-run" if dry_run else "live"

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

    # 데몬 시작 알림
    run_script("notify.py", "daemon", "--status", "running",
                "--detail", f"{'DRY RUN' if dry_run else 'LIVE'} | {market.upper()} | 간격 {interval}초")

    retry_count = 0
    max_retries = 5
    _last_market_open = None  # 장 마감 감지용

    while True:
        try:
            # 장 마감 감지 → 일일 보고서
            currently_open = is_market_open(market)
            if _last_market_open is True and currently_open is False and state.today_trades > 0:
                log("  장 마감 감지 → 일일 보고서 전송")
                run_script("notify.py", "report", "--type", "daily")
            _last_market_open = currently_open

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
            run_script("notify.py", "daemon", "--status", "stopped", "--detail", "사용자 중단 (Ctrl+C)")
            break

        except Exception as e:
            state.record_error(str(e))
            state.status = DaemonStatus.ERROR
            state.save()
            log(f"  예외: {e}")
            traceback.print_exc()
            run_script("notify.py", "daemon", "--status", "error", "--detail", str(e)[:200])
            time.sleep(60)


if __name__ == "__main__":
    main()
