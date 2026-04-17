#!/usr/bin/env python3
"""
performance-metrics.py — 성과 지표 계산 (Sharpe, MDD, Equity Curve)

전문 퀀트 펀드에서 표준으로 사용하는 리스크-조정 수익률 지표를 DB의 거래 이력으로부터 계산합니다.

주요 지표:
  - Daily Returns: 일별 합산 pnl_pct (%)
  - Equity Curve: 복리 누적 자본 곡선
  - Sharpe Ratio: 연율화(√252) Sharpe = mean(r)/std(r) * √252
  - Sortino Ratio: 하방 편차만 사용한 Sharpe 변형
  - Max Drawdown: peak 대비 최대 하락폭 (%)
  - Weekly MDD: 최근 7일 창의 MDD
  - Rolling Sharpe: 최근 N일 창의 Sharpe

사용법 (CLI):
  python3 performance-metrics.py all [--days 30]
  python3 performance-metrics.py sharpe [--days 30]
  python3 performance-metrics.py mdd [--days 7]
  python3 performance-metrics.py equity [--days 90]
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import get_conn, init_db

TRADING_DAYS_PER_YEAR = 252


# ─── 데이터 로드 ───

def fetch_daily_returns(days: int | None = None) -> list[dict]:
    """일별 수익률 시계열 반환.

    각 요소: {"date": "YYYY-MM-DD", "pnl_pct": float, "n_trades": int}
    오름차순 (오래된 날짜가 먼저) 정렬.
    """
    sql = """
        SELECT exit_date AS d,
               SUM(pnl_pct) AS total_pnl,
               COUNT(*) AS n
        FROM trades
        WHERE status = 'closed'
          AND exit_date IS NOT NULL
          AND pnl_pct IS NOT NULL
    """
    params: list = []
    if days:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        sql += " AND exit_date >= ?"
        params.append(cutoff)
    sql += " GROUP BY exit_date ORDER BY exit_date ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"date": r["d"], "pnl_pct": r["total_pnl"] or 0.0, "n_trades": r["n"]}
            for r in rows]


# ─── Equity Curve ───

def equity_curve(daily_returns: list[dict], start_capital: float = 100.0) -> list[dict]:
    """복리 기준 자본 곡선.
    각 point: {"date", "equity", "return_pct", "drawdown_pct"}
    drawdown_pct는 해당 시점까지의 peak 대비 하락률 (%, 음수).
    """
    out = []
    equity = start_capital
    peak = start_capital
    for row in daily_returns:
        r = row["pnl_pct"] / 100.0
        equity *= (1 + r)
        peak = max(peak, equity)
        dd = (equity / peak - 1) * 100 if peak > 0 else 0.0
        out.append({
            "date": row["date"],
            "equity": round(equity, 4),
            "return_pct": round(row["pnl_pct"], 4),
            "drawdown_pct": round(dd, 4),
        })
    return out


# ─── Sharpe / Sortino ───

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: list[float], ddof: int = 1) -> float:
    n = len(xs)
    if n <= ddof:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var)


def sharpe_ratio(daily_returns: list[dict], rf_annual: float = 0.0) -> dict:
    """연율화 Sharpe Ratio.
    rf_annual: 무위험수익률 (소수, 예: 0.04 = 4%)
    """
    returns = [r["pnl_pct"] / 100.0 for r in daily_returns]
    if len(returns) < 2:
        return {"sharpe": None, "n": len(returns), "mean_daily": 0.0, "std_daily": 0.0}

    rf_daily = rf_annual / TRADING_DAYS_PER_YEAR
    excess = [r - rf_daily for r in returns]
    mu = _mean(excess)
    sd = _stdev(excess)
    if sd == 0:
        return {"sharpe": None, "n": len(returns), "mean_daily": mu, "std_daily": 0.0}
    sharpe = mu / sd * math.sqrt(TRADING_DAYS_PER_YEAR)
    return {
        "sharpe": round(sharpe, 3),
        "n": len(returns),
        "mean_daily_pct": round(mu * 100, 4),
        "std_daily_pct": round(sd * 100, 4),
        "annualized_return_pct": round(mu * TRADING_DAYS_PER_YEAR * 100, 2),
        "annualized_vol_pct": round(sd * math.sqrt(TRADING_DAYS_PER_YEAR) * 100, 2),
    }


def sortino_ratio(daily_returns: list[dict], rf_annual: float = 0.0) -> dict:
    returns = [r["pnl_pct"] / 100.0 for r in daily_returns]
    if len(returns) < 2:
        return {"sortino": None, "n": len(returns)}
    rf_daily = rf_annual / TRADING_DAYS_PER_YEAR
    excess = [r - rf_daily for r in returns]
    downside = [min(0, e) for e in excess]
    n = len(downside)
    mean_downside_sq = sum(d * d for d in downside) / n
    dsd = math.sqrt(mean_downside_sq)
    mu = _mean(excess)
    if dsd == 0:
        return {"sortino": None, "n": n, "downside_dev_pct": 0.0}
    sortino = mu / dsd * math.sqrt(TRADING_DAYS_PER_YEAR)
    return {
        "sortino": round(sortino, 3),
        "n": n,
        "downside_dev_pct": round(dsd * 100, 4),
    }


# ─── Max Drawdown ───

def max_drawdown(daily_returns: list[dict]) -> dict:
    """전체 기간 MDD.

    Returns: {"mdd_pct", "peak_date", "trough_date", "peak_equity", "trough_equity"}
    """
    if not daily_returns:
        return {"mdd_pct": 0.0, "peak_date": None, "trough_date": None,
                "peak_equity": 100.0, "trough_equity": 100.0}

    curve = equity_curve(daily_returns)
    mdd = 0.0
    peak_eq = curve[0]["equity"]
    peak_date = curve[0]["date"]
    trough_date = curve[0]["date"]
    trough_eq = curve[0]["equity"]
    run_peak_eq = curve[0]["equity"]
    run_peak_date = curve[0]["date"]

    for p in curve:
        if p["equity"] > run_peak_eq:
            run_peak_eq = p["equity"]
            run_peak_date = p["date"]
        dd = (p["equity"] / run_peak_eq - 1) * 100
        if dd < mdd:
            mdd = dd
            peak_eq = run_peak_eq
            peak_date = run_peak_date
            trough_eq = p["equity"]
            trough_date = p["date"]

    return {
        "mdd_pct": round(mdd, 4),
        "peak_date": peak_date,
        "trough_date": trough_date,
        "peak_equity": round(peak_eq, 4),
        "trough_equity": round(trough_eq, 4),
    }


def weekly_mdd() -> dict:
    """최근 7일 MDD. 서킷 브레이커용."""
    rows = fetch_daily_returns(days=7)
    return max_drawdown(rows)


# ─── 통합 리포트 ───

def summary(days: int = 30, rf_annual: float = 0.0) -> dict:
    """대시보드/알림용 종합 리포트."""
    rows = fetch_daily_returns(days=days)
    curve = equity_curve(rows)
    s = sharpe_ratio(rows, rf_annual=rf_annual)
    so = sortino_ratio(rows, rf_annual=rf_annual)
    mdd = max_drawdown(rows)
    wmdd = weekly_mdd()

    total_return = (curve[-1]["equity"] / 100.0 - 1) * 100 if curve else 0.0
    n_trading_days = len(rows)
    winning_days = sum(1 for r in rows if r["pnl_pct"] > 0)
    losing_days = sum(1 for r in rows if r["pnl_pct"] < 0)

    return {
        "window_days": days,
        "trading_days_with_activity": n_trading_days,
        "total_return_pct": round(total_return, 2),
        "winning_days": winning_days,
        "losing_days": losing_days,
        "day_win_rate": round(winning_days / n_trading_days * 100, 1) if n_trading_days else 0.0,
        "sharpe": s,
        "sortino": so,
        "mdd_period": mdd,
        "mdd_weekly": wmdd,
        "equity_curve_tail": curve[-10:] if len(curve) > 10 else curve,
    }


# ─── CLI ───

def _parse_args(argv: list[str]) -> dict:
    args = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--") and i + 1 < len(argv):
            args[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return args


def main():
    init_db()
    if len(sys.argv) < 2:
        print(json.dumps({"error": "command: all | sharpe | sortino | mdd | equity | weekly-mdd"}))
        sys.exit(1)

    cmd = sys.argv[1]
    args = _parse_args(sys.argv[2:])
    days = int(args.get("days", "30"))
    rf = float(args.get("rf", "0"))

    if cmd == "all":
        out = summary(days=days, rf_annual=rf)
    elif cmd == "sharpe":
        out = sharpe_ratio(fetch_daily_returns(days=days), rf_annual=rf)
    elif cmd == "sortino":
        out = sortino_ratio(fetch_daily_returns(days=days), rf_annual=rf)
    elif cmd == "mdd":
        out = max_drawdown(fetch_daily_returns(days=days))
    elif cmd == "weekly-mdd":
        out = weekly_mdd()
    elif cmd == "equity":
        out = equity_curve(fetch_daily_returns(days=days))
    else:
        print(json.dumps({"error": f"unknown command: {cmd}"}))
        sys.exit(1)

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
