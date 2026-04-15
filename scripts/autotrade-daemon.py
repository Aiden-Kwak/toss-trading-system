#!/usr/bin/env python3
"""
autotrade-daemon.py — Claude 없이 독립 실행되는 자동매매 데몬

사용법:
  python3 scripts/autotrade-daemon.py
  python3 scripts/autotrade-daemon.py --interval 300 --market us
  python3 scripts/autotrade-daemon.py --dry-run  # 주문 없이 시뮬레이션

환경변수 또는 signal-config.json에서 파라미터 로드.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from enum import Enum

try:
    sys.path.insert(0, str(Path(__file__).parent))
    from db import (init_db, insert_cycle, insert_error as _db_insert_error,
                    create_tranche_plan, get_active_tranche_plans,
                    update_tranche_plan, complete_tranche_plan,
                    complete_tranche_plan_by_symbol)
    init_db()
    _DB_OK = True
except Exception:
    _DB_OK = False
    def insert_cycle(_): return None
    def _db_insert_error(*_a, **_k): return None
    def create_tranche_plan(_): return ""
    def get_active_tranche_plans(_s=None): return []
    def update_tranche_plan(_g, _u): return False
    def complete_tranche_plan(_g, _s="completed"): return False
    def complete_tranche_plan_by_symbol(_s): return 0

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
_default_bin = HOME / "Desktop/Auto-trader/tossinvest-cli/bin/tossctl"
_default_helper = HOME / "Desktop/Auto-trader/tossinvest-cli/auth-helper"
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
        self.reserved_budgets = {}  # {"TSLA": {"usd": 150.0, "krw": 0}, ...}
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
                self.reserved_budgets = d.get("reserved_budgets", {})
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
            "reserved_budgets": self.reserved_budgets,
        }, ensure_ascii=False, indent=2))

    def record_error(self, msg: str):
        entry = {"time": datetime.now().isoformat(), "msg": msg}
        self.errors.append(entry)
        self.last_error = msg
        log(f"  ERROR: {msg}")
        try:
            _db_insert_error("daemon", msg)
        except Exception:
            pass


# ─── 유틸리티 ───

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def _load_session_cookies() -> str | None:
    """session.json에서 쿠키 문자열 로드"""
    try:
        sf = Path.home() / "Library/Application Support/tossctl/session.json"
        if not sf.exists():
            return None
        s = json.loads(sf.read_text())
        return "; ".join(f"{k}={v}" for k, v in s.get("cookies", {}).items())
    except Exception:
        return None


def _curl_toss_api(url: str, method: str = "GET", body: str | None = None) -> dict | None:
    """curl로 토스증권 API 직접 호출 (tossctl 폴백)"""
    cookies = _load_session_cookies()
    if not cookies:
        return None
    try:
        cmd = ["curl", "-s", "-w", "\n---HTTP_STATUS:%{http_code}---",
               "-b", cookies,
               "-H", "User-Agent: Mozilla/5.0",
               "-H", "Accept: application/json"]
        if method == "POST":
            cmd.extend(["-X", "POST", "-H", "Content-Type: application/json", "-d", body or "{}"])
        cmd.append(url)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        output = r.stdout
        marker = output.rfind("\n---HTTP_STATUS:")
        if marker == -1:
            return None
        raw_body = output[:marker]
        code = int(output[marker:].replace("\n---HTTP_STATUS:", "").replace("---", "").strip())
        if code == 490:
            return {"_error": "maintenance"}
        if 200 <= code < 300:
            return json.loads(raw_body)
        return None
    except Exception:
        return None


# tossctl → curl 폴백 매핑
_CURL_FALLBACK = {
    ("account", "summary"): ("https://wts-cert-api.tossinvest.com/api/v3/my-assets/summaries/markets/all/overview", "GET"),
    ("account", "list"): ("https://wts-api.tossinvest.com/api/v1/account/list", "GET"),
    ("portfolio", "positions"): ("https://wts-cert-api.tossinvest.com/api/v2/dashboard/asset/sections/all", "POST"),
}


def _transform_fallback(args_key: tuple, data: dict) -> dict | list | None:
    """curl 응답 → tossctl 형식 변환"""
    result = data.get("result", data)
    if args_key == ("account", "summary"):
        overview = result.get("overviewByMarket", {})
        kr = overview.get("kr", {})
        us = overview.get("us", {})
        return {
            "total_asset_amount": result.get("totalAssetAmount", 0),
            "evaluated_profit_amount": result.get("evaluatedProfitAmount", 0),
            "profit_rate": result.get("profitRate", 0),
            "orderable_amount_krw": kr.get("orderableAmount", {}).get("krw", 0),
            "orderable_amount_usd": us.get("orderableAmount", {}).get("usd", 0),
            "markets": {
                m: {
                    "market": m,
                    "orderable_amount_krw": v.get("orderableAmount", {}).get("krw", 0),
                    "orderable_amount_usd": v.get("orderableAmount", {}).get("usd", 0),
                    "evaluated_amount": v.get("evaluatedAmount", 0),
                    "principal_amount": v.get("principalAmount", 0),
                    "evaluated_profit_amount": v.get("evaluatedProfitAmount", 0),
                    "profit_rate": v.get("profitRate", 0),
                    "total_asset_amount": v.get("totalAssetAmount", 0),
                }
                for m, v in overview.items()
            },
        }
    if args_key == ("portfolio", "positions"):
        positions = []
        for sec in result.get("sections", []):
            if sec.get("type") != "SORTED_OVERVIEW":
                continue
            for prod in sec.get("data", {}).get("products", []):
                mtype = prod.get("marketType", "")
                mcode = "US" if "US" in mtype else "KR"
                for item in prod.get("items", []):
                    sym = item.get("stockSymbol") or item.get("stockName", "")
                    cur = item.get("currentPrice", {})
                    buy = item.get("purchasePrice", {})
                    pnl_amt = item.get("profitLossAmount", {})
                    pnl_rate = item.get("profitLossRate", {})
                    positions.append({
                        "product_code": item.get("stockCode", ""),
                        "symbol": sym,
                        "name": item.get("stockName", ""),
                        "market_type": mtype,
                        "market_code": mcode,
                        "quantity": item.get("quantity", 0),
                        "average_price": (buy or {}).get("krw", 0),
                        "current_price": (cur or {}).get("krw", 0),
                        "average_price_usd": (buy or {}).get("usd"),
                        "current_price_usd": (cur or {}).get("usd"),
                        "unrealized_pnl": (pnl_amt or {}).get("krw", 0),
                        "profit_rate": (pnl_rate or {}).get("krw", 0),
                    })
        return positions
    return result


def run_tossctl(*args, timeout=15) -> dict | list | None:
    """tossctl 실행. 실패 시 curl 폴백, 그래도 실패 시 None 반환."""
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
        # curl 폴백 시도
        args_key = tuple(args[:2])
        if args_key in _CURL_FALLBACK:
            curl_url, curl_method = _CURL_FALLBACK[args_key]
            curl_result = _curl_toss_api(curl_url, method=curl_method)
            if curl_result and "_error" not in curl_result:
                return _transform_fallback(args_key, curl_result)
            if curl_result and curl_result.get("_error") == "maintenance":
                return {"_error": "maintenance"}
        return {"_error": result.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"_error": "timeout"}
    except json.JSONDecodeError:
        return {"_error": "invalid_json", "raw": result.stdout[:200] if result else ""}
    except Exception as e:
        return {"_error": str(e)}


def run_script(script_name: str, *args, timeout=30) -> dict | list | None:
    """scripts/ 디렉토리의 Python 스크립트 실행 (venv python 우선)"""
    try:
        venv_py = SCRIPTS_DIR.parent / ".venv" / "bin" / "python3"
        python_bin = str(venv_py) if venv_py.exists() else "python3"
        env = {**os.environ, "TOSS_TRADE_MODE": os.environ.get("TOSS_TRADE_MODE", "live")}
        result = subprocess.run(
            [python_bin, str(SCRIPTS_DIR / script_name), *args],
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
    """장 시간인지 확인 (KST 기준)"""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Seoul"))
    except ImportError:
        now = datetime.now()

    hour, minute = now.hour, now.minute
    weekday = now.weekday()  # 0=Mon ~ 6=Sun

    if market == "auto":
        return get_active_market() is not None
    if market == "kr":
        return weekday < 5 and _is_kr_open(hour, minute)
    else:
        # US: hour >= 21 → 같은 요일(월~금), hour < 6 → 전날 세션(화~토)
        if hour >= 21 and weekday < 5:
            return True
        if hour < 6 and 1 <= weekday <= 5:
            return True
        return False


def _is_kr_open(hour: int, minute: int) -> bool:
    """코스피/코스닥: 09:00~15:30 KST"""
    return 9 <= hour < 15 or (hour == 15 and minute <= 30)


def _is_us_open(hour: int) -> bool:
    """US: 21:00~06:00 KST (서머타임 포함 넉넉하게)"""
    return hour >= 21 or hour < 6


def get_active_market() -> str | None:
    """현재 열려 있는 시장 반환. 둘 다 닫혀있으면 None."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Seoul"))
    except ImportError:
        now = datetime.now()
    hour, minute = now.hour, now.minute
    weekday = now.weekday()  # 0=Mon ~ 6=Sun

    # KR: 평일(월~금) 09:00~15:30 KST
    if weekday < 5 and _is_kr_open(hour, minute):
        return "kr"

    # US: KST 기준 장시간은 21:00~06:00이지만,
    #   hour >= 21 → US 같은 요일 → KST 월~금(0~4)이면 OK
    #   hour < 6  → US 전날 세션 → KST 화~토(1~5)이어야 US 평일
    if hour >= 21 and weekday < 5:
        return "us"
    if hour < 6 and 1 <= weekday <= 5:
        return "us"

    return None


def _seconds_until_next_market_open() -> int:
    """다음 장 시작(KST)까지 남은 초. KR 09:00 / US 21:00 중 가까운 쪽."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Seoul"))
    except ImportError:
        now = datetime.now()

    today = now.date()
    candidates = []

    for day_offset in range(7):
        d = today + timedelta(days=day_offset)
        wd = d.weekday()
        # KR: 평일 09:00
        if wd < 5:
            target = datetime(d.year, d.month, d.day, 9, 0, tzinfo=now.tzinfo)
            if target > now:
                candidates.append(target)
        # US: 평일 21:00 (KST) → US 같은 요일
        if wd < 5:
            target = datetime(d.year, d.month, d.day, 21, 0, tzinfo=now.tzinfo)
            if target > now:
                candidates.append(target)

    if not candidates:
        return 600

    nearest = min(candidates)
    return max(int((nearest - now).total_seconds()), 60)


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


def monitor_positions(state: DaemonState, config: dict) -> tuple:
    """보유 포지션 손절/익절 체크. (매도시그널 리스트, 전체포지션 리스트) 반환."""
    positions = run_tossctl("portfolio", "positions")
    if not isinstance(positions, list):
        return [], []

    result = run_script("signal-engine.py", "check-positions",
                        "--positions", json.dumps(positions),
                        "--config", json.dumps(config))

    if not isinstance(result, list):
        return [], positions

    sell_signals = [s for s in result if s.get("action", "").startswith("SELL")]
    return sell_signals, positions


def check_risk_gate(state: DaemonState, config: dict) -> bool:
    """리스크 게이트 통과 여부 (보호종목은 포지션 한도에서 제외)"""
    summary = run_tossctl("account", "summary")
    positions = run_tossctl("portfolio", "positions")
    if not isinstance(summary, dict) or summary.get("_error"):
        return False

    protected = load_protected()
    active = sum(
        1 for p in positions
        if isinstance(positions, list)
        and p.get("quantity", 0) > 0
        and p.get("symbol", p.get("product_code", "")) not in protected
    ) if isinstance(positions, list) else 0

    result = run_script("signal-engine.py", "risk-gate",
                        "--portfolio", json.dumps(summary),
                        "--config", json.dumps(config),
                        "--today-pnl", str(state.today_pnl),
                        "--active-positions", str(active))

    if isinstance(result, dict):
        return result.get("passed", False)
    return False


def find_buy_candidates(config: dict, market: str = "us") -> list:
    """스크리너로 매수 후보 탐색 + 기술적 지표 + 추세추종"""
    result = run_script("stock-screener.py", "scan",
                        "--source", "all",
                        "--market", market,
                        timeout=180)

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
            # tech API(tossctl, KRW 기준) 현재가로 덮어쓰기 — yfinance는 USD라 수량 계산 오류 방지
            tech_price = tech.get("current_price", 0)
            if tech_price > 0:
                c["current_price"] = tech_price

            # 추세추종 판단: 진짜 골든크로스(교차 확인) + 채점 B 이상
            is_crossover = tech.get("ema", {}).get("is_golden_crossover", False)

            if is_crossover and c.get("grade") in ("A", "B"):
                c["grade"] = "T"
                c["strategy"] = "trend_follow"
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
                grade: str = "A", score: float = 0, reason: str = "",
                market: str = "us", group_id: str = None, tranche_seq: int = 1) -> bool:
    """매수 실행 (시장가 주문, 미체결 시 자동 취소)"""
    tranche_label = f" [T{tranche_seq}]" if tranche_seq > 1 else ""
    order_type_label = "지정가" if market == "us" else "시장가"
    log(f"  BUY: {symbol} x{qty} @ ~{price} ({grade}등급, {score}점) [{order_type_label}]{tranche_label}")

    # trade-logger 공통 인자
    _logger_extra = []
    if group_id:
        _logger_extra += ["--group-id", group_id]
    if tranche_seq:
        _logger_extra += ["--tranche-seq", str(tranche_seq)]

    if dry_run:
        log(f"  [DRY RUN] 주문 스킵")
        run_script("trade-logger.py", "log",
                    "--symbol", symbol, "--side", "buy",
                    "--qty", str(qty), "--price", str(price),
                    "--grade", grade, "--score", str(score),
                    "--reason", reason or "autotrade-dry-run",
                    "--mode", "dry-run",
                    *_logger_extra)
        state.today_trades += 1
        return True

    # 주문 실행 (US: 지정가 +1% 슬리피지, KR: 시장가)
    if market == "us":
        limit_price = int(price * 1.01)  # +1% 슬리피지: 현재가보다 높게 걸어 즉시 체결
        order_args = ["bash", str(SCRIPTS_DIR / "quick-order.sh"),
                      "--symbol", symbol, "--side", "buy",
                      "--qty", str(qty), "--price", str(limit_price),
                      "--market", market]
    else:
        order_args = ["bash", str(SCRIPTS_DIR / "quick-order.sh"),
                      "--symbol", symbol, "--side", "buy",
                      "--qty", str(qty), "--type", "market",
                      "--market", market]

    result = subprocess.run(
        order_args, capture_output=True, text=True, timeout=30, env=TOSS_ENV
    )

    if result.returncode == 0:
        # 주문 접수 후 체결 확인 (최대 60초, 10초 간격)
        order_info = {}
        try:
            order_info = json.loads(result.stdout)
        except Exception:
            pass
        order_id = order_info.get("order_id", "") or order_info.get("raw", {}).get("orderId", "")

        filled_price = _wait_for_fill(symbol, order_id, timeout=60)
        if filled_price:
            log(f"  BUY FILLED: {symbol} @ {filled_price}")
            run_script("notify.py", "trade", "--symbol", symbol, "--side", "buy",
                        "--price", str(filled_price), "--qty", str(qty),
                        "--grade", grade, "--score", str(score), "--strategy", reason)
            run_script("trade-logger.py", "log",
                        "--symbol", symbol, "--side", "buy",
                        "--qty", str(qty), "--price", str(filled_price),
                        "--grade", grade, "--score", str(score),
                        "--reason", reason or "autotrade",
                        "--mode", "autotrade",
                        *_logger_extra)
            state.today_trades += 1
            return True
        else:
            # 미체결 → 자동 취소
            log(f"  BUY TIMEOUT: {symbol} 60초 미체결 → 취소 시도")
            _cancel_pending(symbol, order_id)
            run_script("notify.py", "error", "--message", f"{symbol} 매수 미체결 → 자동 취소")
            return False
    else:
        log(f"  BUY FAILED: {result.stderr[:200]}")
        state.record_error(f"BUY {symbol} failed: {result.stderr[:100]}")
        return False


def _wait_for_fill(symbol: str, order_id: str, timeout: int = 60) -> float | None:
    """주문 체결 대기. 체결되면 체결가 반환, 타임아웃이면 None."""
    elapsed = 0
    interval = 10
    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval
        # 포지션에 해당 종목이 생겼는지 확인
        positions = run_tossctl("portfolio", "positions")
        if isinstance(positions, list):
            for p in positions:
                p_sym = p.get("symbol", p.get("product_code", ""))
                if p_sym == symbol and p.get("quantity", 0) > 0:
                    price_val = (p.get("average_price") or p.get("avg_price")
                                 or p.get("purchase_price") or p.get("current_price") or 1)
                    return float(price_val)
        # order show로 직접 확인
        if order_id:
            order = run_tossctl("order", "show", order_id)
            if isinstance(order, dict):
                status = order.get("status", "").lower()
                if status in ("filled", "executed", "complete"):
                    return order.get("filled_price", order.get("price", 0))
                if status in ("cancelled", "rejected", "expired"):
                    return None
    return None


def _wait_for_sell_fill(symbol: str, order_id: str, timeout: int = 60) -> bool:
    """매도 체결 대기. 포지션이 사라지면 True, 타임아웃이면 False."""
    elapsed = 0
    interval = 10
    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval
        # 포지션에서 해당 종목이 사라졌는지 확인
        positions = run_tossctl("portfolio", "positions")
        if isinstance(positions, list):
            found = any(
                p.get("symbol", p.get("product_code", "")) == symbol and p.get("quantity", 0) > 0
                for p in positions
            )
            if not found:
                return True
        # order show로 직접 확인
        if order_id:
            order = run_tossctl("order", "show", order_id)
            if isinstance(order, dict):
                status = order.get("status", "").lower()
                if status in ("filled", "executed", "complete"):
                    return True
                if status in ("cancelled", "rejected", "expired"):
                    return False
    return False


def _cancel_pending(symbol: str, order_id: str):
    """미체결 주문 취소"""
    if order_id:
        subprocess.run(
            ["bash", "-c", f"tossctl order cancel --order-id {order_id}"],
            capture_output=True, text=True, timeout=15, env=TOSS_ENV
        )
        log(f"  CANCEL: {symbol} (order_id={order_id})")
    else:
        log(f"  CANCEL: {symbol} order_id 없음 — 수동 확인 필요")


def execute_sell(symbol: str, signal: dict, state: DaemonState, dry_run: bool,
                 market: str = "us") -> bool:
    """매도 실행 (시장가 주문, 미체결 시 자동 취소)"""
    price = signal.get("current_price", 0)
    order_type_label = "지정가" if market == "us" else "시장가"
    log(f"  SELL: {symbol} @ ~{price} ({signal.get('action', '')}) [{order_type_label}]")

    if dry_run:
        log(f"  [DRY RUN] 주문 스킵")
        pnl = signal.get("profit_rate", 0)
        state.today_pnl += pnl
        state.today_trades += 1
        if pnl < 0:
            state.consecutive_losses += 1
        else:
            state.consecutive_losses = 0
        # dry-run에서도 트랜치 플랜 정리
        try:
            complete_tranche_plan_by_symbol(symbol)
        except Exception:
            pass
        state.reserved_budgets.pop(symbol, None)
        return True

    # 보유 수량 확인
    positions = run_tossctl("portfolio", "positions")
    if not isinstance(positions, list):
        return False

    pos = next((p for p in positions if p.get("symbol") == symbol), None)
    if not pos or pos.get("quantity", 0) <= 0:
        return False

    qty = int(pos["quantity"]) if pos["quantity"] == int(pos["quantity"]) else pos["quantity"]

    # 주문 실행 (US: 지정가 -1% 슬리피지, KR: 시장가)
    if market == "us":
        limit_price = int(price * 0.99)  # -1% 슬리피지: 현재가보다 낮게 걸어 즉시 체결
        sell_args = ["bash", str(SCRIPTS_DIR / "quick-order.sh"),
                     "--symbol", symbol, "--side", "sell",
                     "--qty", str(qty), "--price", str(limit_price),
                     "--market", market]
    else:
        sell_args = ["bash", str(SCRIPTS_DIR / "quick-order.sh"),
                     "--symbol", symbol, "--side", "sell",
                     "--qty", str(qty), "--type", "market",
                     "--market", market]

    result = subprocess.run(
        sell_args,
        capture_output=True, text=True, timeout=30, env=TOSS_ENV
    )

    if result.returncode == 0:
        # 체결 확인 (최대 60초)
        order_info = {}
        try:
            order_info = json.loads(result.stdout)
        except Exception:
            pass
        order_id = order_info.get("order_id", "") or order_info.get("raw", {}).get("orderId", "")

        filled = _wait_for_sell_fill(symbol, order_id, timeout=60)
        if not filled:
            log(f"  SELL TIMEOUT: {symbol} 60초 미체결 → 취소 시도")
            _cancel_pending(symbol, order_id)
            run_script("notify.py", "error", "--message", f"{symbol} 매도 미체결 → 자동 취소")
            return False
        log(f"  SELL SUCCESS: {symbol}")
        pnl = signal.get("profit_rate", 0)
        state.today_pnl += pnl
        exit_reason = signal.get("action", "MANUAL")
        run_script("notify.py", "trade", "--symbol", symbol, "--side", "sell",
                    "--price", str(price), "--qty", str(qty),
                    "--pnl_pct", str(round(pnl, 2)), "--exit_reason", exit_reason)

        # 거래 기록 (분할매수 → close-all로 전체 트랜치 일괄 청산)
        close_result = run_script("trade-logger.py", "close-all",
                    "--symbol", symbol,
                    "--exit-price", str(price),
                    "--exit-reason", exit_reason)
        if isinstance(close_result, dict) and close_result.get("_error"):
            state.record_error(f"SELL {symbol} 성공했지만 기록 실패: {close_result['_error']}")
        elif isinstance(close_result, list) and len(close_result) == 0:
            state.record_error(f"SELL {symbol} 성공했지만 DB에 open 트레이드 없음")

        # 트랜치 플랜 정리 + 예약 예산 해제
        try:
            complete_tranche_plan_by_symbol(symbol)
        except Exception:
            pass
        state.reserved_budgets.pop(symbol, None)

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


# ─── 분할매수 트랜치 처리 ───

def _estimate_fx_rate() -> float:
    """KRW/USD 환율 추정. account summary에서 조회, 실패 시 기본값."""
    try:
        summary = run_tossctl("account", "summary")
        if isinstance(summary, dict) and not summary.get("_error"):
            markets = summary.get("markets", {})
            usd = markets.get("us", {}).get("orderable_amount_usd", 0)
            krw = markets.get("us", {}).get("orderable_amount_krw", 0)
            if usd > 0 and krw > 0:
                return krw / usd
    except Exception:
        pass
    return 1450  # 기본 환율

def process_pending_tranches(state: DaemonState, config: dict, positions: list,
                              dry_run: bool, effective_market: str):
    """대기 중인 트랜치 플랜을 처리: 조건 평가 → 추가 매수 또는 타임아웃."""
    plans = get_active_tranche_plans()
    if not plans:
        return

    log(f"  [트랜치] 활성 플랜 {len(plans)}개 처리")

    for plan in plans:
        group_id = plan["group_id"]
        symbol = plan["symbol"]
        entry_t1 = plan["entry_price_t1"]
        next_tranche = plan["next_tranche"]
        total_tranches = plan["total_tranches"]
        cycles_waited = plan["cycles_waited"]
        max_wait = plan["max_wait_cycles"]
        next_condition = plan["next_condition"]
        ratios = json.loads(plan["ratios"]) if isinstance(plan["ratios"], str) else plan["ratios"]
        plan_market = plan.get("market", "us").lower()

        if next_tranche > total_tranches:
            complete_tranche_plan(group_id)
            state.reserved_budgets.pop(symbol, None)
            continue

        # 현재가 조회: positions에서 찾거나 tossctl quote로 조회
        current_price_raw = 0
        if isinstance(positions, list):
            for p in positions:
                p_sym = p.get("symbol", p.get("product_code", ""))
                if p_sym == symbol:
                    current_price_raw = p.get("current_price", 0)
                    break
        if current_price_raw <= 0:
            quote = run_tossctl("quote", symbol)
            if isinstance(quote, dict) and not quote.get("_error"):
                current_price_raw = quote.get("last", quote.get("current_price", 0))
        if current_price_raw <= 0:
            log(f"  [트랜치] {symbol} 시세 조회 실패 → 스킵")
            update_tranche_plan(group_id, {"cycles_waited": cycles_waited + 1})
            continue

        # US 종목: positions의 current_price는 KRW → entry_price_t1(USD)과 비교하려면 USD 변환
        # entry_price_t1이 USD 단위인지 판별: 5000 미만이면 USD
        if plan_market == "us" and entry_t1 < 5000 and current_price_raw > 5000:
            fx_rate = _estimate_fx_rate()
            current_price = current_price_raw / fx_rate  # KRW → USD
        else:
            current_price = current_price_raw

        # 타임아웃 체크
        if cycles_waited >= max_wait:
            timeout_action = config.get("tranche_timeout_action", "cancel")
            if timeout_action == "execute":
                log(f"  [트랜치] {symbol} T{next_tranche} 대기 초과 → 시장가 실행")
                _execute_tranche(state, config, plan, current_price, dry_run, plan_market)
            else:
                log(f"  [트랜치] {symbol} T{next_tranche} 대기 초과 → 취소")
                complete_tranche_plan(group_id, "expired")
                state.reserved_budgets.pop(symbol, None)
            continue

        # 조건 평가
        condition_met = False
        if next_condition == "dip_buy":
            dip_pct = config.get("tranche2_dip_pct", -1.5) if next_tranche == 2 else config.get("tranche3_dip_pct", -1.5)
            target_price = entry_t1 * (1 + dip_pct / 100)
            if current_price <= target_price:
                condition_met = True
                log(f"  [트랜치] {symbol} T{next_tranche} 눌림목 도달: {current_price:.2f} <= {target_price:.2f}")

        elif next_condition == "breakout_confirm":
            breakout_pct = config.get("tranche3_breakout_pct", 1.0) if next_tranche == 3 else config.get("tranche2_breakout_pct", 1.0)
            target_price = entry_t1 * (1 + breakout_pct / 100)
            if current_price >= target_price:
                condition_met = True
                log(f"  [트랜치] {symbol} T{next_tranche} 돌파 확인: {current_price:.2f} >= {target_price:.2f}")

        elif next_condition == "time_based":
            # N 사이클 후 무조건 실행
            wait_cycles = config.get(f"tranche{next_tranche}_wait_cycles", 6)
            if cycles_waited >= wait_cycles:
                condition_met = True
                log(f"  [트랜치] {symbol} T{next_tranche} 시간 기반 실행: {cycles_waited} >= {wait_cycles} 사이클")

        if condition_met:
            _execute_tranche(state, config, plan, current_price, dry_run, plan_market)
        else:
            update_tranche_plan(group_id, {"cycles_waited": cycles_waited + 1})
            log(f"  [트랜치] {symbol} T{next_tranche} 대기 ({cycles_waited + 1}/{max_wait})")


def _execute_tranche(state: DaemonState, config: dict, plan: dict,
                      current_price: float, dry_run: bool, market: str):
    """트랜치 매수 실행.

    current_price: US 종목은 USD 가격, KR 종목은 KRW 가격으로 전달되어야 함.
    process_pending_tranches()에서 entry_price_t1(USD) 기준으로 조건 평가하므로,
    여기서도 USD 기준으로 예산 계산 후, 주문 시 KRW 변환.
    """
    group_id = plan["group_id"]
    symbol = plan["symbol"]
    next_tranche = plan["next_tranche"]
    total_tranches = plan["total_tranches"]
    ratios = json.loads(plan["ratios"]) if isinstance(plan["ratios"], str) else plan["ratios"]

    # 예약 예산에서 이번 트랜치 비율 산출
    reserved = state.reserved_budgets.get(symbol, {})
    budget_usd = reserved.get("usd", 0)
    budget_krw = reserved.get("krw", 0)

    # 남은 트랜치 비율 합
    remaining_ratios = ratios[next_tranche - 1:]
    ratio_sum = sum(remaining_ratios)
    this_ratio = ratios[next_tranche - 1] if next_tranche - 1 < len(ratios) else 0

    if ratio_sum <= 0 or this_ratio <= 0:
        complete_tranche_plan(group_id)
        state.reserved_budgets.pop(symbol, None)
        return

    # 이번 트랜치에 배분할 예산 비율 (남은 예산 중)
    alloc_frac = this_ratio / ratio_sum

    if market == "us":
        alloc_usd = budget_usd * alloc_frac
        # current_price는 USD 단위 (entry_price_t1과 동일 단위)
        price_usd = current_price
        if price_usd > 0:
            qty = max(1, int(alloc_usd / price_usd))
        else:
            qty = 1
        cost_usd = qty * price_usd
        reserved["usd"] = max(0, budget_usd - cost_usd)
        # 주문은 KRW 변환 필요 — 환율 추정
        fx_rate = _estimate_fx_rate()
        order_price = int(price_usd * fx_rate)
    else:
        alloc_krw = budget_krw * alloc_frac
        if current_price > 0:
            qty = max(1, int(alloc_krw / current_price))
        else:
            qty = 1
        cost = qty * current_price
        reserved["krw"] = max(0, budget_krw - cost)
        order_price = current_price

    state.reserved_budgets[symbol] = reserved

    success = execute_buy(
        symbol, order_price, qty, state, dry_run,
        grade=plan.get("grade", ""), score=plan.get("score", 0),
        reason=f"tranche_t{next_tranche}:{plan.get('entry_reason', '')}",
        market=market, group_id=group_id, tranche_seq=next_tranche,
    )

    if success:
        new_filled = plan["tranches_filled"] + 1
        new_next = next_tranche + 1
        updates = {
            "tranches_filled": new_filled,
            "next_tranche": new_next,
            "cycles_waited": 0,
        }
        # 다음 트랜치 조건 설정
        if new_next <= total_tranches:
            cond_key = f"tranche{new_next}_condition"
            updates["next_condition"] = config.get(cond_key, "dip_buy")
            wait_key = f"tranche{new_next}_max_wait_cycles"
            updates["max_wait_cycles"] = config.get(wait_key, 24)
        else:
            updates["status"] = "completed"
            state.reserved_budgets.pop(symbol, None)

        update_tranche_plan(group_id, updates)
        log(f"  [트랜치] {symbol} T{next_tranche} 매수 완료 ({new_filled}/{total_tranches})")
    else:
        update_tranche_plan(group_id, {"cycles_waited": plan["cycles_waited"] + 1})


# ─── 메인 루프 ───

def run_cycle(state: DaemonState, config: dict, dry_run: bool, market: str):
    """1회 사이클 실행"""
    state.cycle_count += 1
    state.last_cycle_at = datetime.now().isoformat()
    protected = load_protected()
    _cycle_start = time.time()
    _cycle_sells = 0
    _cycle_buys_attempted = 0
    _cycle_buys_filled = 0
    effective_market = market  # 기본값; auto 모드에서 실제 시장으로 덮어씀

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

    # 3. 장 시간 체크 (auto 모드: 열려 있는 시장 자동 판별)
    if market == "auto":
        active = get_active_market()
        if not active:
            log("  장 휴장 (KR/US 모두). 스킵.")
            return True
        effective_market = active
        log(f"  활성 시장: {effective_market.upper()}")
    else:
        if not is_market_open(market):
            log(f"  장 휴장 ({market}). 스킵.")
            return True
        effective_market = market

    # 4. 일일 손실 한도 체크 (데몬 운용 금액 기준, 보호종목 무관)
    total_asset = 0
    orderable_krw = 0
    summary = run_tossctl("account", "summary")
    if isinstance(summary, dict) and not summary.get("_error"):
        total_asset = summary.get("total_asset_amount", 0)
        orderable_krw = summary.get("orderable_amount_krw", 0)
    # today_pnl은 퍼센트 누적값 (예: -3.0 = -3%). 직접 한도와 비교.
    if True:
        loss_pct = state.today_pnl  # 이미 퍼센트 단위
        limit = config.get("daily_loss_limit_pct", -2.0)
        if loss_pct <= limit:
            state.status = DaemonStatus.PAUSED_LOSS_LIMIT
            log(f"  일일 손실 한도 도달: {loss_pct:.2f}% (한도: {limit}%)")
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
    sell_signals, positions = monitor_positions(state, config)
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
        # 한국 종목인지 판별
        sell_market = "kr" if (sym.isdigit() and len(sym) == 6) or sym.startswith("A") else "us"
        if execute_sell(sym, sig, state, dry_run, market=sell_market):
            _cycle_sells += 1

    # 7. 리스크 게이트
    if not check_risk_gate(state, config):
        log("  리스크 게이트 미통과 → 신규 매수 스킵")
        state.status = DaemonStatus.RUNNING
        return True

    # 7.5. 대기 트랜치 처리 (분할매수)
    if config.get("scaled_entry_enabled", False):
        try:
            process_pending_tranches(state, config, positions, dry_run, effective_market)
        except Exception as e:
            state.record_error(f"트랜치 처리 오류: {e}")

    # 8. 매수 후보 탐색
    candidates = find_buy_candidates(config, effective_market)

    # 스캔/채점 결과 알림
    if candidates:
        top3 = candidates[:3]
        scan_detail = ", ".join(f"{c.get('symbol','')}({c.get('grade','?')}/{c.get('score_pct',0)}%)" for c in top3)
        log(f"  스캔 결과: {len(candidates)}종목 — {scan_detail}")
        run_script("notify.py", "scan", "--detail",
                   f"{effective_market.upper()} {len(candidates)}종목 발견: {scan_detail}")
    else:
        log("  매수 후보 없음")
        state.status = DaemonStatus.RUNNING
        return True

    # 9. 매수 실행 — 시장별 포지션 한도
    max_pos_kr = config.get("max_positions_kr", config.get("max_positions", 3))
    max_pos_us = config.get("max_positions_us", config.get("max_positions", 3))
    # 현재 보유 포지션 수 (보호종목 제외, 시장별 분류)
    current_positions = set()
    current_kr = set()
    current_us = set()
    if isinstance(positions, list):
        for p in positions:
            sym_p = p.get("symbol", p.get("product_code", ""))
            if p.get("quantity", 0) > 0 and sym_p not in protected:
                current_positions.add(sym_p)
                mkt_code = p.get("market_code", p.get("market_type", ""))
                if mkt_code in ("KSP", "KSQ") or (sym_p.isdigit() and len(sym_p) == 6) or sym_p.startswith("A"):
                    current_kr.add(sym_p)
                else:
                    current_us.add(sym_p)

    slots_kr = max_pos_kr - len(current_kr)
    slots_us = max_pos_us - len(current_us)
    log(f"  포지션: KR {len(current_kr)}/{max_pos_kr} | US {len(current_us)}/{max_pos_us}")
    if slots_kr <= 0 and slots_us <= 0:
        log(f"  포지션 한도 도달 → 매수 스킵")
    else:
        # 예약 예산 차감 (분할매수 대기분)
        reserved_usd = sum(v.get("usd", 0) for v in state.reserved_budgets.values())
        reserved_krw = sum(v.get("krw", 0) for v in state.reserved_budgets.values())

        # 통화별 예산 분리: KR=원화, US=달러 (강제 환전 방지)
        orderable_krw = 0  # 국내 주문가능 원화
        orderable_usd = 0  # 해외 주문가능 달러
        fx_rate = 1450     # KRW/USD 환율 (주문가 변환용)
        if isinstance(summary, dict) and not summary.get("_error"):
            markets = summary.get("markets", {})
            orderable_krw = markets.get("kr", {}).get("orderable_amount_krw", 0)
            orderable_usd = markets.get("us", {}).get("orderable_amount_usd", 0)
            # 환율 추정: total KRW / total USD
            us_krw = markets.get("us", {}).get("orderable_amount_krw", 0)
            if orderable_usd > 0 and us_krw > 0:
                fx_rate = us_krw / orderable_usd
        # 예약분 차감
        orderable_usd = max(0, orderable_usd - reserved_usd)
        orderable_krw = max(0, orderable_krw - reserved_krw)
        log(f"  예산: KR {orderable_krw:,.0f}원 | US ${orderable_usd:,.2f} (환율 {fx_rate:,.0f})")
        if reserved_usd > 0 or reserved_krw > 0:
            log(f"  예약: KR {reserved_krw:,.0f}원 | US ${reserved_usd:,.2f} (트랜치 대기분)")

        # 활성 트랜치 플랜이 있는 종목은 신규 매수 스킵
        tranche_active_symbols = set()
        if config.get("scaled_entry_enabled", False):
            try:
                tranche_active_symbols = {p["symbol"] for p in get_active_tranche_plans()}
            except Exception:
                pass

        # 잔여 슬롯만큼 후보 순회하며 매수
        bought_kr = 0
        bought_us = 0
        for c in candidates:
            sym = c.get("symbol", "")
            if sym in protected or sym in current_positions or sym in tranche_active_symbols:
                continue

            price = c.get("current_price", 0)
            if price <= 0:
                continue

            buy_market = "kr" if c.get("market_code", "") in ("KSP", "KSQ") else "us"

            # 시장별 슬롯 잔여 체크
            if buy_market == "us" and (slots_us - bought_us) <= 0:
                continue
            if buy_market == "kr" and (slots_kr - bought_kr) <= 0:
                continue

            if buy_market == "us":
                # --- US: 달러 예산으로 계산, 주문은 KRW 변환 ---
                price_usd = price if price < 5000 else price / fx_rate
                price_krw = int(price_usd * fx_rate)

                remaining_us = slots_us - bought_us
                per_slot_usd = orderable_usd / remaining_us if remaining_us > 0 else 0
                full_invest_usd = min(per_slot_usd,
                                      orderable_usd * config.get("max_position_pct", 30) / 100)

                if full_invest_usd < price_usd:
                    log(f"  {sym} USD 잔고 부족 (슬롯당 ${per_slot_usd:.2f} < ${price_usd:.2f}) → 스킵")
                    continue

                # 분할매수: 1차 트랜치만 매수, 나머지 예약
                scaled = config.get("scaled_entry_enabled", False)
                ratios = config.get("scaled_entry_ratios", [0.4, 0.35, 0.25])
                num_tranches = config.get("scaled_entry_tranches", 3)

                if scaled and len(ratios) >= 2:
                    t1_invest = full_invest_usd * ratios[0]
                    reserve_usd = full_invest_usd - t1_invest
                else:
                    t1_invest = full_invest_usd
                    reserve_usd = 0

                qty = max(1, int(t1_invest / price_usd))
                cost_usd = qty * price_usd
                log(f"  {sym} ${price_usd:.2f} x{qty} = ${cost_usd:.2f} (→ 주문가 {price_krw:,}원)")

                strategy = c.get("strategy", "daytrade")
                group_id = None
                if scaled and len(ratios) >= 2:
                    group_id = f"{sym}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

                _cycle_buys_attempted += 1
                if execute_buy(sym, price_krw, qty, state, dry_run,
                               grade=c.get("grade", "?"), score=c.get("score", 0),
                               reason=f"{strategy}:{c.get('recommendation', '')}",
                               market=buy_market,
                               group_id=group_id, tranche_seq=1):
                    orderable_usd -= cost_usd
                    current_positions.add(sym)
                    current_us.add(sym)
                    bought_us += 1
                    _cycle_buys_filled += 1

                    # 분할매수: 예산 예약 + 트랜치 플랜 생성
                    if scaled and reserve_usd > 0 and group_id:
                        state.reserved_budgets[sym] = {"usd": reserve_usd, "krw": 0}
                        try:
                            create_tranche_plan({
                                "group_id": group_id,
                                "symbol": sym,
                                "market": "US",
                                "total_tranches": num_tranches,
                                "planned_qty": max(1, int(full_invest_usd / price_usd)),
                                "planned_budget": full_invest_usd,
                                "entry_price_t1": price_usd,
                                "ratios": json.dumps(ratios),
                                "tranches_filled": 1,
                                "next_tranche": 2,
                                "next_condition": config.get("tranche2_condition", "dip_buy"),
                                "max_wait_cycles": config.get("tranche2_max_wait_cycles", 12),
                                "grade": c.get("grade", ""),
                                "score": c.get("score", 0),
                                "entry_reason": f"{strategy}:{c.get('recommendation', '')}",
                            })
                            log(f"  [트랜치] {sym} 플랜 생성: {num_tranches}단계, 예약 ${reserve_usd:.2f}")
                        except Exception as e:
                            state.record_error(f"트랜치 플랜 생성 실패: {e}")
            else:
                # --- KR: 원화 예산으로 계산 ---
                remaining_kr = slots_kr - bought_kr
                per_slot_krw = orderable_krw / remaining_kr if remaining_kr > 0 else 0
                full_invest_krw = min(per_slot_krw,
                                      orderable_krw * config.get("max_position_pct", 30) / 100)

                if full_invest_krw < price:
                    log(f"  {sym} 원화 잔고 부족 (슬롯당 {per_slot_krw:,.0f}원 < {price:,.0f}원) → 스킵")
                    continue

                # 분할매수: 1차 트랜치만 매수, 나머지 예약
                scaled = config.get("scaled_entry_enabled", False)
                ratios = config.get("scaled_entry_ratios", [0.4, 0.35, 0.25])
                num_tranches = config.get("scaled_entry_tranches", 3)

                if scaled and len(ratios) >= 2:
                    t1_invest = full_invest_krw * ratios[0]
                    reserve_krw = full_invest_krw - t1_invest
                else:
                    t1_invest = full_invest_krw
                    reserve_krw = 0

                qty = max(1, int(t1_invest / price))
                cost_krw = qty * price

                strategy = c.get("strategy", "daytrade")
                group_id = None
                if scaled and len(ratios) >= 2:
                    group_id = f"{sym}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

                _cycle_buys_attempted += 1
                if execute_buy(sym, price, qty, state, dry_run,
                               grade=c.get("grade", "?"), score=c.get("score", 0),
                               reason=f"{strategy}:{c.get('recommendation', '')}",
                               market=buy_market,
                               group_id=group_id, tranche_seq=1):
                    orderable_krw -= cost_krw
                    current_positions.add(sym)
                    current_kr.add(sym)
                    bought_kr += 1
                    _cycle_buys_filled += 1

                    # 분할매수: 예산 예약 + 트랜치 플랜 생성
                    if scaled and reserve_krw > 0 and group_id:
                        state.reserved_budgets[sym] = {"usd": 0, "krw": reserve_krw}
                        try:
                            create_tranche_plan({
                                "group_id": group_id,
                                "symbol": sym,
                                "market": "KR",
                                "total_tranches": num_tranches,
                                "planned_qty": max(1, int(full_invest_krw / price)),
                                "planned_budget": full_invest_krw,
                                "entry_price_t1": price,
                                "ratios": json.dumps(ratios),
                                "tranches_filled": 1,
                                "next_tranche": 2,
                                "next_condition": config.get("tranche2_condition", "dip_buy"),
                                "max_wait_cycles": config.get("tranche2_max_wait_cycles", 12),
                                "grade": c.get("grade", ""),
                                "score": c.get("score", 0),
                                "entry_reason": f"{strategy}:{c.get('recommendation', '')}",
                            })
                            log(f"  [트랜치] {sym} 플랜 생성: {num_tranches}단계, 예약 {reserve_krw:,.0f}원")
                        except Exception as e:
                            state.record_error(f"트랜치 플랜 생성 실패: {e}")

        bought = bought_kr + bought_us
        if bought > 0:
            log(f"  {bought}종목 매수 (KR {len(current_kr)}/{max_pos_kr} | US {len(current_us)}/{max_pos_us})")

    state.status = DaemonStatus.RUNNING

    # 사이클 DB 기록
    try:
        positions_now = run_tossctl("portfolio", "positions")
        pos_count = len([p for p in (positions_now or [])
                         if p.get("quantity", 0) > 0]) if isinstance(positions_now, list) else 0
        insert_cycle({
            "cycle_num":          state.cycle_count,
            "market":             effective_market,
            "status":             "ok",
            "positions_count":    pos_count,
            "sells_executed":     _cycle_sells,
            "buys_attempted":     _cycle_buys_attempted,
            "buys_filled":        _cycle_buys_filled,
            "today_pnl":          state.today_pnl,
            "today_trades":       state.today_trades,
            "consecutive_losses": state.consecutive_losses,
            "duration_sec":       round(time.time() - _cycle_start, 1),
        })
    except Exception:
        pass

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
    market = args.get("market", "auto")
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

            # config에서 모드 동적 반영 (Discord 봇에서 전환 가능)
            cfg_mode = config.get("trade_mode")
            if cfg_mode in ("live", "dry-run"):
                new_dry = cfg_mode == "dry-run"
                if new_dry != dry_run:
                    dry_run = new_dry
                    os.environ["TOSS_TRADE_MODE"] = cfg_mode
                    log(f"  모드 전환: {'DRY RUN' if dry_run else 'LIVE'}")
                    run_script("notify.py", "daemon", "--status", "running",
                                "--detail", f"모드 전환: {'DRY RUN' if dry_run else 'LIVE'}")

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
