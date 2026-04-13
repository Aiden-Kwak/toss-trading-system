#!/usr/bin/env python3
"""
toss-trading-system 대시보드 서버
tossctl 명령을 HTTP API로 래핑하고, 프론트엔드를 서빙합니다.

사용법: python3 dashboard/server.py [--port 8777]
"""

import http.server
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8777
DASHBOARD_DIR = Path(__file__).parent
REPO_DIR = DASHBOARD_DIR.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
PROTECTED_FILE = Path.home() / "Library/Application Support/tossctl/protected-stocks.json"
CONFIG_FILE = Path.home() / "Library/Application Support/tossctl/config.json"

# venv Python (yfinance 등 의존성 포함)
VENV_PYTHON = REPO_DIR / ".venv" / "bin" / "python3"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else "python3"
TOSS_BIN = Path(os.environ.get("PATH", "").split(":")[0]) / "tossctl" if "tossinvest" in os.environ.get("PATH", "") else Path.home() / "Desktop/Auto-trader/tossinvest-cli/bin/tossctl"

# tossctl 환경변수
TOSS_ENV = {
    **os.environ,
    "PATH": f"{Path.home()}/Desktop/Auto-trader/tossinvest-cli/bin:{os.environ.get('PATH', '')}",
    "TOSSCTL_AUTH_HELPER_DIR": str(Path.home() / "Desktop/Auto-trader/tossinvest-cli/auth-helper"),
    "TOSSCTL_AUTH_HELPER_PYTHON": str(Path.home() / "Desktop/Auto-trader/tossinvest-cli/auth-helper/.venv/bin/python3"),
}


def detect_maintenance(error_text: str) -> dict | None:
    """에러 메시지에서 토스증권 점검시간 감지"""
    if "490" in error_text and "unavailable" in error_text.lower():
        return {"maintenance": True, "message": "토스증권 시스템 점검 중", "raw": error_text}
    if "490" in error_text:
        return {"maintenance": True, "message": "토스증권 시스템 점검 중 (490)", "raw": error_text}
    return None


def check_maintenance_curl() -> dict | None:
    """curl로 직접 토스증권 API 상태 확인"""
    try:
        session_file = Path.home() / "Library/Application Support/tossctl/session.json"
        if not session_file.exists():
            return None
        session = json.loads(session_file.read_text())
        cookies = "; ".join(f"{k}={v}" for k, v in session.get("cookies", {}).items())
        result = subprocess.run(
            ["curl", "-s", "-w", "\n---HTTP_STATUS:%{http_code}---",
             "-b", cookies,
             "-H", "User-Agent: Mozilla/5.0",
             "-H", "Accept: application/json",
             "https://wts-cert-api.tossinvest.com/api/v3/my-assets/summaries/markets/all/overview"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout
        if "---HTTP_STATUS:490---" in output:
            body = output.split("\n---HTTP_STATUS:")[0]
            try:
                data = json.loads(body)
                err = data.get("error", {})
                return {
                    "maintenance": True,
                    "code": err.get("code", "unavailable.agency"),
                    "message": err.get("message", "시스템 점검 중"),
                    "from": err.get("data", {}).get("from"),
                    "until": err.get("data", {}).get("until"),
                    "daily": err.get("data", {}).get("daily", False),
                }
            except json.JSONDecodeError:
                return {"maintenance": True, "message": "시스템 점검 중 (490)"}
        return {"maintenance": False}
    except Exception as e:
        return None


def run_tossctl(*args) -> dict:
    """tossctl 명령 실행 후 JSON 반환. 490 에러 시 점검 정보 포함."""
    try:
        result = subprocess.run(
            ["tossctl", *args, "--output", "json"],
            capture_output=True, text=True, timeout=15, env=TOSS_ENV
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        # 점검시간 감지
        maint = detect_maintenance(result.stderr)
        if maint:
            # curl로 상세 점검 정보 가져오기
            detail = check_maintenance_curl()
            if detail and detail.get("maintenance"):
                return {"error": detail["message"], "maintenance": detail}
            return {"error": maint["message"], "maintenance": maint}
        return {"error": result.stderr.strip(), "code": result.returncode}
    except json.JSONDecodeError:
        return {"raw": result.stdout.strip()}
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}


def run_signal_engine(command: str, **kwargs) -> dict:
    """signal-engine.py 실행"""
    try:
        args = [PYTHON, str(SCRIPTS_DIR / "signal-engine.py"), command]
        for k, v in kwargs.items():
            args.extend([f"--{k}", json.dumps(v) if isinstance(v, (dict, list)) else str(v)])
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return json.loads(result.stdout)
        return {"error": result.stderr.strip()}
    except Exception as e:
        return {"error": str(e)}


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))

        if path == "/":
            self.path = "/index.html"
            return super().do_GET()

        if not path.startswith("/api/"):
            return super().do_GET()

        # --- API Routes ---
        if path == "/api/maintenance":
            result = check_maintenance_curl()
            self._json_response(result or {"maintenance": False})
            return

        if path == "/api/auth/status":
            self._json_response(run_tossctl("auth", "status"))

        elif path == "/api/auth/login":
            # 백그라운드에서 auth login 실행 (브라우저 열림)
            try:
                subprocess.Popen(
                    [str(TOSS_BIN), "auth", "login"],
                    env=TOSS_ENV,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                self._json_response({"ok": True, "message": "브라우저가 열립니다. 토스 앱으로 QR을 스캔하세요."})
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)})

        elif path == "/api/account/summary":
            self._json_response(run_tossctl("account", "summary"))

        elif path == "/api/portfolio/positions":
            self._json_response(run_tossctl("portfolio", "positions"))

        elif path == "/api/orders/list":
            self._json_response(run_tossctl("orders", "list"))

        elif path == "/api/quote/batch":
            symbols = params.get("symbols", "").split(",")
            if symbols and symbols[0]:
                self._json_response(run_tossctl("quote", "batch", *symbols))
            else:
                self._json_response({"error": "no symbols"})

        elif path == "/api/quote/get":
            symbol = params.get("symbol", "")
            if symbol:
                self._json_response(run_tossctl("quote", "get", symbol))
            else:
                self._json_response({"error": "no symbol"})

        elif path == "/api/config/show":
            if CONFIG_FILE.exists():
                self._json_response(json.loads(CONFIG_FILE.read_text()))
            else:
                self._json_response({"error": "config not found"})

        elif path == "/api/protected-stocks":
            if PROTECTED_FILE.exists():
                self._json_response(json.loads(PROTECTED_FILE.read_text()))
            else:
                self._json_response({"stocks": []})

        elif path == "/api/signal/check-positions":
            positions = run_tossctl("portfolio", "positions")
            if isinstance(positions, list):
                result = run_signal_engine("check-positions", positions=positions)
                self._json_response(result)
            else:
                self._json_response(positions)

        elif path == "/api/signal/risk-gate":
            summary = run_tossctl("account", "summary")
            positions = run_tossctl("portfolio", "positions")
            active = len(positions) if isinstance(positions, list) else 0
            result = run_signal_engine(
                "risk-gate", portfolio=summary,
                **{"today-pnl": "0", "active-positions": str(active)}
            )
            self._json_response(result)

        elif path == "/api/signal/evaluate-buy":
            symbol = params.get("symbol", "")
            if not symbol:
                self._json_response({"error": "no symbol"})
                return
            quote = run_tossctl("quote", "get", symbol)
            summary = run_tossctl("account", "summary")
            if not isinstance(quote, dict) or "error" in quote:
                self._json_response(quote)
                return
            # 기술적 지표 가져오기 (토스 API, 선택적)
            tech = None
            try:
                product_code = quote.get("product_code", symbol)
                mkt = "kr" if quote.get("market_code", "") in ("KSP", "KSQ") else "us"
                tech_result = subprocess.run(
                    [PYTHON, str(SCRIPTS_DIR / "technical-indicators.py"), "analyze", "--symbol", product_code, "--market", mkt],
                    capture_output=True, text=True, timeout=10
                )
                if tech_result.returncode == 0:
                    tech = json.loads(tech_result.stdout)
            except Exception:
                pass
            result = run_signal_engine("evaluate-buy", quote=quote, portfolio=summary, **({} if tech is None else {"tech": tech}))
            self._json_response(result)

        elif path == "/api/signal/compute-alphas":
            symbol = params.get("symbol", "")
            if not symbol:
                self._json_response({"error": "no symbol"})
                return
            quote = run_tossctl("quote", "get", symbol)
            if isinstance(quote, dict) and "error" not in quote:
                result = run_signal_engine("compute-alphas", quote=quote)
                self._json_response(result)
            else:
                self._json_response(quote)

        elif path == "/api/ai/pipeline":
            # AI 진입 판단 파이프라인: 전체 상태를 한번에 반환
            import datetime
            pipeline = {"timestamp": datetime.datetime.now().isoformat(), "phases": {}}

            # Phase 1: 세션
            auth = run_tossctl("auth", "status")
            session_ok = not (isinstance(auth, dict) and (auth.get("error") or auth.get("raw", "").startswith("No")))
            pipeline["phases"]["session"] = {"ok": session_ok, "detail": auth}

            # Phase 2: 점검 확인
            maint = check_maintenance_curl()
            maint_ok = not (maint and maint.get("maintenance"))
            pipeline["phases"]["maintenance"] = {"ok": maint_ok, "detail": maint}

            if session_ok and maint_ok:
                # Phase 3: 포트폴리오 & 요약
                summary = run_tossctl("account", "summary")
                positions = run_tossctl("portfolio", "positions")
                pipeline["phases"]["data"] = {
                    "ok": isinstance(positions, list),
                    "position_count": len(positions) if isinstance(positions, list) else 0,
                    "total_asset": summary.get("total_asset_amount", 0) if isinstance(summary, dict) else 0,
                    "orderable": summary.get("orderable_amount_krw", 0) if isinstance(summary, dict) else 0,
                }

                # Phase 4: 보유종목 시그널
                if isinstance(positions, list) and positions:
                    signals = run_signal_engine("check-positions", positions=positions)
                    actions = []
                    if isinstance(signals, list):
                        for s in signals:
                            actions.append({
                                "symbol": s.get("symbol", ""),
                                "name": s.get("name", ""),
                                "action": s.get("action", "HOLD"),
                                "reason": s.get("reason", ""),
                                "urgency": s.get("urgency", "NONE"),
                                "profit_rate": s.get("profit_rate", 0),
                            })
                    pipeline["phases"]["position_signals"] = {"ok": True, "signals": actions}

                    # Phase 5: 리스크 게이트
                    active = len([p for p in positions if p.get("quantity", 0) > 0])
                    gate = run_signal_engine(
                        "risk-gate", portfolio=summary,
                        **{"today-pnl": "0", "active-positions": str(active)}
                    )
                    pipeline["phases"]["risk_gate"] = {
                        "ok": gate.get("passed", False) if isinstance(gate, dict) else False,
                        "detail": gate,
                    }

                    # Phase 6: 보유종목 시세로 매수 평가 (관심종목 시뮬레이션)
                    # 보유종목 심볼 추출해서 시세 조회 후 evaluate
                    symbols = [p.get("symbol", p.get("product_code", "")) for p in positions if p.get("quantity", 0) > 0]
                    us_symbols = [s for s in symbols if not s.replace("A","").isdigit()]
                    if us_symbols:
                        quotes = run_tossctl("quote", "batch", *us_symbols[:5])
                        evals = []
                        if isinstance(quotes, list):
                            for q in quotes:
                                ev = run_signal_engine("evaluate-buy", quote=q, portfolio=summary)
                                if isinstance(ev, dict) and "error" not in ev:
                                    evals.append({
                                        "symbol": ev.get("symbol", ""),
                                        "name": ev.get("name", ""),
                                        "score": ev.get("score", 0),
                                        "max_score": ev.get("max_score", 100),
                                        "pct": ev.get("score_pct", 0),
                                        "grade": ev.get("grade", "D"),
                                        "recommendation": ev.get("recommendation", "SKIP"),
                                    })
                        pipeline["phases"]["evaluation"] = {"ok": True, "results": evals}
                else:
                    pipeline["phases"]["position_signals"] = {"ok": True, "signals": []}
                    pipeline["phases"]["risk_gate"] = {"ok": True, "detail": {"passed": True, "checks": []}}
                    pipeline["phases"]["evaluation"] = {"ok": True, "results": []}

                # 최종 판단
                gate_passed = pipeline["phases"]["risk_gate"]["ok"]
                has_sell_signal = any(
                    s["action"].startswith("SELL")
                    for s in pipeline["phases"].get("position_signals", {}).get("signals", [])
                )
                has_buy_candidate = any(
                    e["grade"] in ("A", "B")
                    for e in pipeline["phases"].get("evaluation", {}).get("results", [])
                )
                pipeline["decision"] = {
                    "action": "SELL" if has_sell_signal else ("BUY" if gate_passed and has_buy_candidate else "HOLD"),
                    "gate_passed": gate_passed,
                    "sell_signals": has_sell_signal,
                    "buy_candidates": has_buy_candidate,
                }
            else:
                pipeline["decision"] = {"action": "BLOCKED", "gate_passed": False, "sell_signals": False, "buy_candidates": False}

            self._json_response(pipeline)

        elif path == "/api/trades":
            result = subprocess.run(
                [PYTHON, str(SCRIPTS_DIR / "trade-logger.py"), "list", "--status", params.get("status", "all")],
                capture_output=True, text=True, timeout=5
            )
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        elif path == "/api/trades/analyze":
            result = subprocess.run(
                [PYTHON, str(SCRIPTS_DIR / "trade-analyzer.py"), "analyze"],
                capture_output=True, text=True, timeout=10
            )
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        elif path == "/api/trades/suggest":
            result = subprocess.run(
                [PYTHON, str(SCRIPTS_DIR / "trade-analyzer.py"), "suggest"],
                capture_output=True, text=True, timeout=10
            )
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        elif path == "/api/daemon/status":
            state_file = Path.home() / "Library/Application Support/tossctl/daemon-state.json"
            if state_file.exists():
                self._json_response(json.loads(state_file.read_text()))
            else:
                self._json_response({"status": "stopped", "message": "데몬 미실행"})

        elif path == "/api/report":
            # 일일/주간/월간 보고서 생성
            report_type = params.get("type", "daily")  # daily, weekly, monthly
            result = subprocess.run(
                [PYTHON, str(SCRIPTS_DIR / "report-generator.py"), report_type],
                capture_output=True, text=True, timeout=15
            )
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr[:300]})

        elif path == "/api/resolve-kr":
            query = params.get("q", "")
            result = subprocess.run(
                [PYTHON, str(SCRIPTS_DIR / "kr-stock-resolver.py"), query],
                capture_output=True, text=True, timeout=15
            )
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        elif path == "/api/backtest/day":
            syms = params.get("symbols", "TSLA")
            mkt = params.get("market", "us")
            date = params.get("date", "")
            cmd = [PYTHON, str(SCRIPTS_DIR / "day-simulator.py"), "--symbols", syms, "--market", mkt, "--output", "json"]
            if date: cmd.extend(["--date", date])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=TOSS_ENV)
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr[:500]})

        elif path == "/api/backtest/portfolio":
            syms = params.get("symbols", "TSLA,NVDA,AAPL,MSFT,GOOG,META")
            cmd = [PYTHON, str(SCRIPTS_DIR / "portfolio-backtest.py"), "--symbols", syms, "--output", "json"]
            for p_key in ["period", "capital", "max-positions", "stop-loss", "take-profit", "max-hold", "grades", "cost"]:
                if params.get(p_key): cmd.extend([f"--{p_key}", params[p_key]])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=TOSS_ENV)
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr[:500]})

        elif path == "/api/backtest/period":
            syms = params.get("symbols", "TSLA")
            period = params.get("period", "6mo")
            cmd = [PYTHON, str(SCRIPTS_DIR / "backtest.py"), "--symbol", syms, "--period", period, "--output", "json"]
            if params.get("stop-loss"): cmd.extend(["--stop-loss", params["stop-loss"]])
            if params.get("take-profit"): cmd.extend(["--take-profit", params["take-profit"]])
            if params.get("max-hold"): cmd.extend(["--max-hold", params["max-hold"]])
            if params.get("grades"): cmd.extend(["--grades", params["grades"]])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=TOSS_ENV)
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr[:500]})

        elif path == "/api/technical":
            sym = params.get("symbol", "")
            mkt = params.get("market", "us")
            cmd = [PYTHON, str(SCRIPTS_DIR / "technical-indicators.py"), "analyze", "--symbol", sym, "--market", mkt]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr[:300]})

        elif path == "/api/ranking":
            cmd = [PYTHON, str(SCRIPTS_DIR / "technical-indicators.py"), "ranking", "--size", params.get("size", "20")]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr[:300]})

        elif path == "/api/screener/golden-cross":
            cmd_args = [PYTHON, str(SCRIPTS_DIR / "golden-cross-scanner.py"), "scan"]
            if params.get("market"): cmd_args.extend(["--market", params["market"]])
            result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=180, env=TOSS_ENV)
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr[:500]})

        elif path == "/api/screener/scan":
            cmd_args = [PYTHON, str(SCRIPTS_DIR / "stock-screener.py"), "scan"]
            if params.get("source"): cmd_args.extend(["--source", params["source"]])
            if params.get("market"): cmd_args.extend(["--market", params["market"]])
            if params.get("news-symbols"): cmd_args.extend(["--news-symbols", params["news-symbols"]])
            result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=60, env=TOSS_ENV)
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        elif path == "/api/screener/watchlist":
            result = subprocess.run(
                [PYTHON, str(SCRIPTS_DIR / "stock-screener.py"), "watchlist-list"],
                capture_output=True, text=True, timeout=5
            )
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        elif path == "/api/trades/config":
            config_file = Path.home() / "Library/Application Support/tossctl/signal-config.json"
            if config_file.exists():
                self._json_response(json.loads(config_file.read_text()))
            else:
                self._json_response({"stop_loss_pct": -3.0, "take_profit_pct": 7.0, "max_positions": 2, "max_position_pct": 10, "daily_loss_limit_pct": -2.0, "entry_grades": ["A", "B"]})

        else:
            self._json_response({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        content_len = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_len)) if content_len > 0 else {}

        if path == "/api/protected-stocks/add":
            stocks = json.loads(PROTECTED_FILE.read_text()) if PROTECTED_FILE.exists() else {"stocks": []}
            stocks["stocks"].append({
                "symbol": body.get("symbol", "").upper(),
                "name": body.get("name", ""),
                "reason": body.get("reason", "사용자 지정"),
                "protected_actions": body.get("protected_actions", ["buy", "sell"]),
            })
            stocks["updated_at"] = __import__("datetime").date.today().isoformat()
            PROTECTED_FILE.write_text(json.dumps(stocks, ensure_ascii=False, indent=2))
            self._json_response({"ok": True, "stocks": stocks["stocks"]})

        elif path == "/api/protected-stocks/remove":
            symbol = body.get("symbol", "").upper()
            stocks = json.loads(PROTECTED_FILE.read_text()) if PROTECTED_FILE.exists() else {"stocks": []}
            stocks["stocks"] = [s for s in stocks["stocks"] if s["symbol"].upper() != symbol]
            stocks["updated_at"] = __import__("datetime").date.today().isoformat()
            PROTECTED_FILE.write_text(json.dumps(stocks, ensure_ascii=False, indent=2))
            self._json_response({"ok": True, "stocks": stocks["stocks"]})

        elif path == "/api/trades/log":
            args = [PYTHON, str(SCRIPTS_DIR / "trade-logger.py"), "log"]
            for k, v in body.items():
                args.extend([f"--{k}", str(v)])
            result = subprocess.run(args, capture_output=True, text=True, timeout=5)
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        elif path == "/api/trades/close":
            args = [PYTHON, str(SCRIPTS_DIR / "trade-logger.py"), "close"]
            for k, v in body.items():
                args.extend([f"--{k}", str(v)])
            result = subprocess.run(args, capture_output=True, text=True, timeout=5)
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        elif path == "/api/trades/lesson":
            args = [PYTHON, str(SCRIPTS_DIR / "trade-logger.py"), "lesson"]
            for k, v in body.items():
                args.extend([f"--{k}", str(v)])
            result = subprocess.run(args, capture_output=True, text=True, timeout=5)
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        elif path == "/api/trades/apply-suggestion":
            sid = body.get("id", "")
            result = subprocess.run(
                [PYTHON, str(SCRIPTS_DIR / "trade-analyzer.py"), "apply", sid],
                capture_output=True, text=True, timeout=5
            )
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        elif path == "/api/trades/config/save":
            config_file = Path.home() / "Library/Application Support/tossctl/signal-config.json"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text(json.dumps(body, ensure_ascii=False, indent=2))
            self._json_response({"ok": True, "saved": body})

        elif path == "/api/daemon/start":
            mode = body.get("mode", "dry-run")  # dry-run or live
            market = body.get("market", "us")
            interval = body.get("interval", 300)
            cmd = [PYTHON, str(SCRIPTS_DIR / "autotrade-daemon.py"), "--interval", str(interval), "--market", market]
            if mode == "dry-run":
                cmd.append("--dry-run")
            try:
                import subprocess as sp
                proc = sp.Popen(cmd, stdout=open(str(REPO_DIR / "daemon.log"), "a"), stderr=sp.STDOUT, env=TOSS_ENV)
                self._json_response({"ok": True, "pid": proc.pid, "mode": mode, "market": market})
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)})

        elif path == "/api/daemon/stop":
            import signal as sig
            try:
                result = subprocess.run(["pkill", "-f", "autotrade-daemon"], capture_output=True, text=True)
                self._json_response({"ok": True, "message": "데몬 종료 요청 전송"})
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)})

        elif path == "/api/screener/watchlist-add":
            # 심볼 유효성 검증: tossctl 또는 yfinance로 조회 시도
            sym = body.get("symbol", "").upper()
            valid = False
            name = body.get("name", "")
            # tossctl로 시세 조회 시도
            check = run_tossctl("quote", "get", sym)
            if isinstance(check, dict) and not check.get("_error") and not check.get("error"):
                valid = True
                if not name:
                    name = check.get("name", "")
            else:
                # yfinance 폴백
                try:
                    import subprocess as sp2
                    r2 = sp2.run([PYTHON, "-c", f"import yfinance as yf; t=yf.Ticker('{sym}'); h=t.history(period='5d'); print(len(h))"],
                                 capture_output=True, text=True, timeout=10)
                    if r2.returncode == 0 and int(r2.stdout.strip() or 0) > 0:
                        valid = True
                except Exception:
                    pass

            if not valid:
                self._json_response({"error": f"'{sym}'은(는) 유효하지 않은 심볼입니다. 정확한 티커를 입력해주세요."})
            else:
                body["symbol"] = sym
                if name:
                    body["name"] = name
                args = [PYTHON, str(SCRIPTS_DIR / "stock-screener.py"), "watchlist-add"]
                for k, v in body.items():
                    args.extend([f"--{k}", str(v)])
                result = subprocess.run(args, capture_output=True, text=True, timeout=5)
                self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        elif path == "/api/screener/watchlist-remove":
            result = subprocess.run(
                [PYTHON, str(SCRIPTS_DIR / "stock-screener.py"), "watchlist-remove", "--symbol", body.get("symbol", "")],
                capture_output=True, text=True, timeout=5
            )
            self._json_response(json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr})

        else:
            self._json_response({"error": "not found"}, 404)

    def _json_response(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        if "/api/" in str(args[0]):
            print(f"  API: {args[0]}")


if __name__ == "__main__":
    print(f"🚀 toss-trading-system dashboard")
    print(f"   http://localhost:{PORT}")
    print(f"   Ctrl+C to stop")
    server = http.server.HTTPServer(("", PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
