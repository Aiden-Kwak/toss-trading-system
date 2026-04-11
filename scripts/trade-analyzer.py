#!/usr/bin/env python3
"""
trade-analyzer.py — 거래 패턴 분석 + 파라미터 조정 제안

축적된 trade-log.json을 분석하여:
1. 통계 (승률, 평균 수익, 샤프 등)
2. 패턴 분석 (등급별, 시장별, 사유별, 요일별)
3. signal-engine 파라미터 조정 제안

사용법:
  python3 trade-analyzer.py analyze
  python3 trade-analyzer.py suggest
"""

import json
import sys
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / "Library/Application Support/tossctl/trade-log.json"
CONFIG_FILE = Path.home() / "Library/Application Support/tossctl/signal-config.json"

# signal-engine 기본값
DEFAULT_PARAMS = {
    "stop_loss_pct": -3.0,
    "take_profit_pct": 7.0,
    "max_positions": 2,
    "max_position_pct": 10,
    "daily_loss_limit_pct": -2.0,
    "entry_grades": ["A", "B"],
}


def load_log() -> list:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return []


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return dict(DEFAULT_PARAMS)


def save_config(config: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2))


def analyze(trades: list) -> dict:
    """거래 로그 통계 분석"""
    closed = [t for t in trades if t["status"] == "closed" and t.get("pnl_pct") is not None]
    open_trades = [t for t in trades if t["status"] == "open"]

    if not closed:
        return {
            "total_trades": len(trades),
            "closed_trades": 0,
            "open_trades": len(open_trades),
            "message": "청산된 거래가 없어 분석 불가",
            "stats": {},
            "patterns": {},
            "lessons": [],
        }

    pnls = [t["pnl_pct"] for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # 기본 통계
    stats = {
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "open_trades": len(open_trades),
        "winning": len(wins),
        "losing": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "avg_pnl": round(statistics.mean(pnls), 2),
        "total_pnl": round(sum(pnls), 2),
        "max_win": round(max(pnls), 2) if pnls else 0,
        "max_loss": round(min(pnls), 2) if pnls else 0,
        "avg_win": round(statistics.mean(wins), 2) if wins else 0,
        "avg_loss": round(statistics.mean(losses), 2) if losses else 0,
        "profit_factor": round(abs(sum(wins) / sum(losses)), 2) if losses and sum(losses) != 0 else float('inf'),
        "sharpe": 0,
        "avg_holding_days": 0,
        "total_cost": round(len(closed) * 0.2, 1),  # 추정 총 수수료
    }

    if len(pnls) > 1:
        stdev = statistics.stdev(pnls)
        stats["sharpe"] = round(statistics.mean(pnls) / stdev, 2) if stdev > 0 else 0

    # 보유 기간 계산
    holding_days = []
    for t in closed:
        if t.get("entry_date") and t.get("exit_date"):
            try:
                ed = datetime.strptime(t["entry_date"], "%Y-%m-%d")
                xd = datetime.strptime(t["exit_date"], "%Y-%m-%d")
                holding_days.append((xd - ed).days)
            except ValueError:
                pass
    if holding_days:
        stats["avg_holding_days"] = round(statistics.mean(holding_days), 1)

    # 패턴 분석
    patterns = {}

    # 등급별 성과
    by_grade = defaultdict(list)
    for t in closed:
        by_grade[t.get("entry_grade", "?")].append(t["pnl_pct"])
    patterns["by_grade"] = {
        g: {
            "count": len(ps),
            "win_rate": round(len([p for p in ps if p > 0]) / len(ps) * 100, 1),
            "avg_pnl": round(statistics.mean(ps), 2),
            "total_pnl": round(sum(ps), 2),
        }
        for g, ps in sorted(by_grade.items())
    }

    # 시장별 성과
    by_market = defaultdict(list)
    for t in closed:
        by_market[t.get("market", "?")].append(t["pnl_pct"])
    patterns["by_market"] = {
        m: {
            "count": len(ps),
            "win_rate": round(len([p for p in ps if p > 0]) / len(ps) * 100, 1),
            "avg_pnl": round(statistics.mean(ps), 2),
        }
        for m, ps in sorted(by_market.items())
    }

    # 청산 사유별 성과
    by_exit = defaultdict(list)
    for t in closed:
        by_exit[t.get("exit_reason", "?")].append(t["pnl_pct"])
    patterns["by_exit_reason"] = {
        r: {
            "count": len(ps),
            "avg_pnl": round(statistics.mean(ps), 2),
        }
        for r, ps in sorted(by_exit.items())
    }

    # 요일별 성과
    by_weekday = defaultdict(list)
    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    for t in closed:
        if t.get("entry_date"):
            try:
                wd = datetime.strptime(t["entry_date"], "%Y-%m-%d").weekday()
                by_weekday[weekday_names[wd]].append(t["pnl_pct"])
            except ValueError:
                pass
    patterns["by_weekday"] = {
        d: {
            "count": len(ps),
            "win_rate": round(len([p for p in ps if p > 0]) / len(ps) * 100, 1),
            "avg_pnl": round(statistics.mean(ps), 2),
        }
        for d, ps in sorted(by_weekday.items(), key=lambda x: weekday_names.index(x[0]) if x[0] in weekday_names else 9)
    }

    # 모드별 (수동 vs 자동)
    by_mode = defaultdict(list)
    for t in closed:
        by_mode[t.get("mode", "manual")].append(t["pnl_pct"])
    patterns["by_mode"] = {
        m: {
            "count": len(ps),
            "win_rate": round(len([p for p in ps if p > 0]) / len(ps) * 100, 1),
            "avg_pnl": round(statistics.mean(ps), 2),
        }
        for m, ps in sorted(by_mode.items())
    }

    # 태그별 승률
    tag_pnls = defaultdict(list)
    for t in closed:
        for tag in t.get("tags", []):
            if tag not in ("win", "loss"):
                tag_pnls[tag].append(t["pnl_pct"])
    patterns["by_tag"] = {
        tag: {
            "count": len(ps),
            "win_rate": round(len([p for p in ps if p > 0]) / len(ps) * 100, 1),
            "avg_pnl": round(statistics.mean(ps), 2),
        }
        for tag, ps in sorted(tag_pnls.items(), key=lambda x: -len(x[1]))
    }

    # 연속 손실 최대
    max_consec_loss = 0
    current_streak = 0
    for p in pnls:
        if p <= 0:
            current_streak += 1
            max_consec_loss = max(max_consec_loss, current_streak)
        else:
            current_streak = 0
    stats["max_consecutive_losses"] = max_consec_loss

    # 교훈 모음
    lessons = []
    for t in closed:
        if t.get("lesson"):
            lessons.append({
                "symbol": t["symbol"],
                "date": t.get("exit_date", ""),
                "pnl": t["pnl_pct"],
                "lesson": t["lesson"],
                "score": t.get("lesson_score", 5),
                "grade": t.get("entry_grade", ""),
            })

    return {
        "stats": stats,
        "patterns": patterns,
        "lessons": sorted(lessons, key=lambda x: -x.get("score", 0)),
    }


def suggest(analysis: dict) -> list:
    """분석 결과를 기반으로 파라미터 조정 제안"""
    current = load_config()
    suggestions = []
    stats = analysis.get("stats", {})
    patterns = analysis.get("patterns", {})

    if stats.get("closed_trades", 0) < 5:
        return [{"id": "insufficient", "type": "info", "title": "데이터 부족",
                 "description": "최소 5건 이상의 청산 거래가 필요합니다.",
                 "current": None, "suggested": None, "param": None}]

    # 1. 손절선 조정
    avg_loss = stats.get("avg_loss", -3.0)
    stop_exits = patterns.get("by_exit_reason", {}).get("STOP_LOSS", {})
    stop_count = stop_exits.get("count", 0)
    total = stats.get("closed_trades", 1)
    stop_rate = stop_count / total * 100

    if stop_rate > 60:
        new_sl = round(avg_loss * 1.3, 1)  # 손절 폭 30% 확대
        suggestions.append({
            "id": "widen_stop_loss",
            "type": "warning",
            "title": "손절선 확대 제안",
            "description": f"손절 비율이 {stop_rate:.0f}%로 높습니다. 손절 폭을 넓혀 불필요한 손절을 줄이세요.",
            "evidence": f"손절 {stop_count}건/{total}건 ({stop_rate:.0f}%), 평균 손실 {avg_loss}%",
            "param": "stop_loss_pct",
            "current": current.get("stop_loss_pct", -3.0),
            "suggested": max(new_sl, -8.0),
        })
    elif stop_rate < 20 and stats.get("max_loss", 0) < -5:
        suggestions.append({
            "id": "tighten_stop_loss",
            "type": "info",
            "title": "손절선 축소 고려",
            "description": f"손절이 거의 발생하지 않지만 최대 손실이 {stats['max_loss']}%입니다. 손절선을 타이트하게 설정해보세요.",
            "param": "stop_loss_pct",
            "current": current.get("stop_loss_pct", -3.0),
            "suggested": round(current.get("stop_loss_pct", -3.0) * 0.7, 1),
        })

    # 2. 익절선 조정
    tp_exits = patterns.get("by_exit_reason", {}).get("TAKE_PROFIT", {})
    max_hold_exits = patterns.get("by_exit_reason", {}).get("MAX_HOLD", {})
    max_hold_avg = max_hold_exits.get("avg_pnl", 0) if max_hold_exits else 0

    if max_hold_avg > 3:
        suggestions.append({
            "id": "raise_take_profit",
            "type": "success",
            "title": "익절선 상향 제안",
            "description": f"MAX_HOLD 청산의 평균 수익이 +{max_hold_avg}%입니다. 익절선을 높이면 수익을 더 키울 수 있습니다.",
            "param": "take_profit_pct",
            "current": current.get("take_profit_pct", 7.0),
            "suggested": round(current.get("take_profit_pct", 7.0) * 1.3, 1),
        })
    elif tp_exits.get("count", 0) == 0 and total >= 10:
        suggestions.append({
            "id": "lower_take_profit",
            "type": "warning",
            "title": "익절선 하향 제안",
            "description": f"익절에 도달한 거래가 없습니다. 목표가를 낮춰 수익 실현 빈도를 높이세요.",
            "param": "take_profit_pct",
            "current": current.get("take_profit_pct", 7.0),
            "suggested": round(current.get("take_profit_pct", 7.0) * 0.7, 1),
        })

    # 3. 진입 등급 조정
    by_grade = patterns.get("by_grade", {})
    if "B" in by_grade and by_grade["B"].get("avg_pnl", 0) < -1:
        suggestions.append({
            "id": "restrict_grade",
            "type": "warning",
            "title": "B등급 진입 제한 제안",
            "description": f"B등급 진입의 평균 수익이 {by_grade['B']['avg_pnl']}%입니다. A등급만 진입하면 성과가 개선될 수 있습니다.",
            "evidence": f"B등급: {by_grade['B']['count']}건, 승률 {by_grade['B']['win_rate']}%, 평균 {by_grade['B']['avg_pnl']}%",
            "param": "entry_grades",
            "current": current.get("entry_grades", ["A", "B"]),
            "suggested": ["A"],
        })
    elif "A" in by_grade and by_grade["A"].get("win_rate", 0) < 30:
        suggestions.append({
            "id": "review_scoring",
            "type": "danger",
            "title": "채점 시스템 재검토 필요",
            "description": f"A등급 승률이 {by_grade['A']['win_rate']}%로 낮습니다. 채점 기준 자체를 재검토해야 합니다.",
            "param": None,
            "current": None,
            "suggested": None,
        })

    # 4. 시장별 조정
    by_market = patterns.get("by_market", {})
    for market, data in by_market.items():
        if data.get("avg_pnl", 0) < -2 and data.get("count", 0) >= 3:
            suggestions.append({
                "id": f"reduce_{market.lower()}",
                "type": "warning",
                "title": f"{market} 시장 비중 축소 제안",
                "description": f"{market} 시장 평균 수익이 {data['avg_pnl']}%입니다. 해당 시장 진입을 줄이거나 기준을 강화하세요.",
                "param": None,
                "current": None,
                "suggested": None,
            })

    # 5. 교훈 기반 제안
    lessons = analysis.get("lessons", [])
    loss_lessons = [l for l in lessons if l.get("pnl", 0) < 0]
    if loss_lessons:
        common_tags = Counter()
        for t in [tr for tr in load_log() if tr["status"] == "closed" and tr.get("pnl_pct", 0) < 0]:
            for tag in t.get("tags", []):
                if tag not in ("win", "loss"):
                    common_tags[tag] += 1
        if common_tags:
            top_tag, top_count = common_tags.most_common(1)[0]
            suggestions.append({
                "id": "lesson_pattern",
                "type": "info",
                "title": f"손실 패턴 감지: '{top_tag}'",
                "description": f"'{top_tag}' 태그가 손실 거래에서 {top_count}번 반복됩니다. 이 패턴의 진입을 피하세요.",
                "param": None,
                "current": None,
                "suggested": None,
            })

    # 양호한 경우
    if not suggestions:
        suggestions.append({
            "id": "all_good",
            "type": "success",
            "title": "현재 설정 양호",
            "description": f"승률 {stats.get('win_rate', 0)}%, 샤프 {stats.get('sharpe', 0)}. 현재 파라미터를 유지하세요.",
            "param": None,
            "current": None,
            "suggested": None,
        })

    return suggestions


def apply_suggestion(suggestion_id: str, suggestions: list):
    """제안을 실제 파라미터에 반영"""
    config = load_config()
    for s in suggestions:
        if s["id"] == suggestion_id and s.get("param") and s.get("suggested") is not None:
            config[s["param"]] = s["suggested"]
            save_config(config)
            print(json.dumps({"applied": True, "param": s["param"], "value": s["suggested"]}))
            return
    print(json.dumps({"applied": False, "error": "suggestion not found or not applicable"}))


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "command: analyze | suggest | apply"}))
        sys.exit(1)

    command = sys.argv[1]
    trades = load_log()

    if command == "analyze":
        result = analyze(trades)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif command == "suggest":
        result = analyze(trades)
        suggestions = suggest(result)
        print(json.dumps({"analysis": result["stats"], "suggestions": suggestions}, ensure_ascii=False, indent=2))

    elif command == "apply":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "usage: apply <suggestion_id>"}))
            return
        result = analyze(trades)
        suggestions = suggest(result)
        apply_suggestion(sys.argv[2], suggestions)

    else:
        print(json.dumps({"error": f"unknown: {command}"}))


if __name__ == "__main__":
    main()
