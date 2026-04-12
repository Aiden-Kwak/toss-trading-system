#!/usr/bin/env python3
"""
report-generator.py — 일일/주간/월간 트레이딩 보고서 생성

사용법:
  python3 scripts/report-generator.py daily
  python3 scripts/report-generator.py weekly
  python3 scripts/report-generator.py monthly
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

LOG_FILE = Path.home() / "Library/Application Support/tossctl/trade-log.json"
STATE_FILE = Path.home() / "Library/Application Support/tossctl/daemon-state.json"
CONFIG_FILE = Path.home() / "Library/Application Support/tossctl/signal-config.json"


def load_trades():
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return []


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def filter_trades(trades, days):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return [t for t in trades if t.get("entry_date", "") >= cutoff and t.get("status") == "closed"]


def generate_report(report_type="daily"):
    trades = load_trades()
    state = load_state()

    if report_type == "daily":
        period_trades = filter_trades(trades, 1)
        period_label = "일일"
        period_date = datetime.now().strftime("%Y-%m-%d")
    elif report_type == "weekly":
        period_trades = filter_trades(trades, 7)
        period_label = "주간"
        period_date = f"{(datetime.now()-timedelta(days=7)).strftime('%Y-%m-%d')} ~ {datetime.now().strftime('%Y-%m-%d')}"
    else:
        period_trades = filter_trades(trades, 30)
        period_label = "월간"
        period_date = f"{(datetime.now()-timedelta(days=30)).strftime('%Y-%m-%d')} ~ {datetime.now().strftime('%Y-%m-%d')}"

    open_trades = [t for t in trades if t.get("status") == "open"]
    all_closed = [t for t in trades if t.get("status") == "closed"]

    # 기간 통계
    pnls = [t.get("pnl_pct", 0) for t in period_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0
    max_win = max(pnls) if pnls else 0
    max_loss = min(pnls) if pnls else 0

    # 종목별 집계
    by_symbol = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0})
    for t in period_trades:
        s = t.get("symbol", "?")
        by_symbol[s]["trades"] += 1
        if t.get("pnl_pct", 0) > 0:
            by_symbol[s]["wins"] += 1
        by_symbol[s]["pnl"] += t.get("pnl_pct", 0)

    # 청산 사유별
    by_reason = defaultdict(int)
    for t in period_trades:
        by_reason[t.get("exit_reason", "?")] += 1

    # 교훈 모음
    lessons = []
    for t in period_trades:
        if t.get("lesson"):
            lessons.append({
                "symbol": t["symbol"],
                "pnl": t.get("pnl_pct", 0),
                "lesson": t["lesson"],
                "score": t.get("lesson_score", 5),
            })

    # 누적 통계
    all_pnls = [t.get("pnl_pct", 0) for t in all_closed]
    cumulative = {
        "total_trades": len(all_closed),
        "total_pnl": round(sum(all_pnls), 2),
        "win_rate": round(len([p for p in all_pnls if p > 0]) / len(all_pnls) * 100, 1) if all_pnls else 0,
    }

    report = {
        "type": report_type,
        "label": period_label,
        "date": period_date,
        "generated_at": datetime.now().isoformat(),
        "period": {
            "trades": len(period_trades),
            "winning": len(wins),
            "losing": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
            "max_win": round(max_win, 2),
            "max_loss": round(max_loss, 2),
        },
        "open_positions": len(open_trades),
        "by_symbol": {s: {"trades": v["trades"], "win_rate": round(v["wins"]/v["trades"]*100, 1) if v["trades"] else 0, "pnl": round(v["pnl"], 2)} for s, v in sorted(by_symbol.items(), key=lambda x: -x[1]["pnl"])},
        "by_reason": dict(by_reason),
        "lessons": lessons[:5],
        "cumulative": cumulative,
        "daemon": {
            "status": state.get("status", "unknown"),
            "cycles": state.get("cycle_count", 0),
            "today_trades": state.get("today_trades", 0),
            "consecutive_losses": state.get("consecutive_losses", 0),
        },
        # PDF용 HTML
        "html": _generate_html(report_type, period_label, period_date, period_trades, by_symbol, by_reason, lessons, cumulative, state, pnls, open_trades),
    }
    return report


def _generate_html(rtype, label, date, trades, by_sym, by_reason, lessons, cumul, state, pnls, open_trades):
    wins = len([p for p in pnls if p > 0])
    total = sum(pnls)
    wr = wins / len(pnls) * 100 if pnls else 0

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; color: #333; }}
h1 {{ border-bottom: 2px solid #333; padding-bottom: 8px; }}
h2 {{ color: #555; margin-top: 24px; }}
.stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 16px 0; }}
.stat {{ background: #f5f5f5; border-radius: 8px; padding: 12px; text-align: center; }}
.stat-value {{ font-size: 24px; font-weight: 700; }}
.stat-label {{ font-size: 11px; color: #888; }}
.positive {{ color: #22c55e; }} .negative {{ color: #ef4444; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 12px 0; }}
th {{ text-align: left; padding: 8px; border-bottom: 2px solid #ddd; color: #888; }}
td {{ padding: 8px; border-bottom: 1px solid #eee; }}
.footer {{ margin-top: 32px; font-size: 11px; color: #aaa; text-align: center; }}
</style></head><body>
<h1>Toss Trading System — {label} 보고서</h1>
<p style="color:#888">{date}</p>

<div class="stats">
<div class="stat"><div class="stat-value">{len(pnls)}</div><div class="stat-label">거래</div></div>
<div class="stat"><div class="stat-value {'positive' if total>=0 else 'negative'}">{total:+.2f}%</div><div class="stat-label">수익</div></div>
<div class="stat"><div class="stat-value">{wr:.1f}%</div><div class="stat-label">승률</div></div>
<div class="stat"><div class="stat-value">{len(open_trades)}</div><div class="stat-label">보유 중</div></div>
</div>

<h2>종목별 성과</h2>
<table><tr><th>종목</th><th>거래</th><th>승률</th><th>수익</th></tr>"""

    for s, v in sorted(by_sym.items(), key=lambda x: -x[1]["pnl"]):
        wr2 = v["wins"]/v["trades"]*100 if v["trades"] else 0
        cls = "positive" if v["pnl"] >= 0 else "negative"
        html += f'<tr><td><strong>{s}</strong></td><td>{v["trades"]}</td><td>{wr2:.0f}%</td><td class="{cls}">{v["pnl"]:+.2f}%</td></tr>'

    html += "</table>"

    if by_reason:
        html += "<h2>청산 사유</h2><table><tr><th>사유</th><th>건수</th></tr>"
        for r, c in sorted(by_reason.items(), key=lambda x: -x[1]):
            html += f"<tr><td>{r}</td><td>{c}</td></tr>"
        html += "</table>"

    if lessons:
        html += "<h2>교훈</h2>"
        for l in lessons:
            cls = "positive" if l["pnl"] >= 0 else "negative"
            html += f'<p><strong>{l["symbol"]}</strong> <span class="{cls}">{l["pnl"]:+.2f}%</span> — {l["lesson"]}</p>'

    html += f"""
<h2>누적 통계</h2>
<div class="stats">
<div class="stat"><div class="stat-value">{cumul["total_trades"]}</div><div class="stat-label">총 거래</div></div>
<div class="stat"><div class="stat-value {'positive' if cumul['total_pnl']>=0 else 'negative'}">{cumul["total_pnl"]:+.2f}%</div><div class="stat-label">누적 수익</div></div>
<div class="stat"><div class="stat-value">{cumul["win_rate"]}%</div><div class="stat-label">누적 승률</div></div>
<div class="stat"><div class="stat-value">{state.get("cycle_count",0)}</div><div class="stat-label">데몬 사이클</div></div>
</div>

<div class="footer">Generated by toss-trading-system</div>
</body></html>"""
    return html


def main():
    rtype = sys.argv[1] if len(sys.argv) > 1 else "daily"
    report = generate_report(rtype)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
