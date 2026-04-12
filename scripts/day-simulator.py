#!/usr/bin/env python3
"""
day-simulator.py — 하루 트레이딩 시뮬레이션

실제 5분봉 데이터로 데몬의 하루를 재현합니다.

사용법:
  # 최근 거래일 시뮬레이션
  python3 scripts/day-simulator.py --symbols TSLA,NVDA,AAPL

  # 특정 날짜
  python3 scripts/day-simulator.py --symbols TSLA,NVDA --date 2026-04-10

  # 한국주식
  python3 scripts/day-simulator.py --symbols 005930.KS,035420.KS --market kr
"""

import json
import math
import sys
import statistics
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from pathlib import Path

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("pip install yfinance pandas")
    sys.exit(1)

SCRIPTS_DIR = Path(__file__).parent


# ─── 알파 계산 (signal-engine 동일) ───

def compute_alphas(o, c, h, l, ref_close):
    hl = h - l + 0.001
    alpha101 = (c - o) / hl if hl > 0 else 0
    alpha33 = (1 - o / c) if c > 0 else 0
    alpha54 = (c - l) / (h - l) if (h - l) > 0 else 0.5
    mean_rev = -math.log(o / ref_close) if o > 0 and ref_close > 0 else 0
    momentum = math.log(c / ref_close) if c > 0 and ref_close > 0 else 0
    score = max(0, min(10, (alpha101 + 1) * 5)) + max(0, min(10, (mean_rev * 100) + 5)) + alpha54 * 10
    return {"alpha101": alpha101, "alpha54": alpha54, "mean_rev": mean_rev, "momentum": momentum, "composite": score}


def score_bar(bar, ref_close, volume, is_kr=False):
    """5분봉 하나를 채점 (130점 만점)"""
    o, h, l, c = bar["Open"], bar["High"], bar["Low"], bar["Close"]
    change_rate = (c - ref_close) / ref_close if ref_close > 0 else 0

    # 거래량
    if is_kr:
        thresholds = [(5e6,30),(3e6,25),(1e6,20),(5e5,15),(1e5,10)]
    else:
        thresholds = [(50e6,30),(20e6,25),(10e6,20),(5e6,15),(1e6,10)]
    vol_score = 5
    for thr, sc in thresholds:
        if volume >= thr:
            vol_score = sc; break

    # 모멘텀
    if is_kr:
        if 0.01 <= change_rate <= 0.08: mom = 25
        elif 0.08 < change_rate <= 0.15: mom = 20
        elif 0.15 < change_rate <= 0.25: mom = 15
        elif change_rate > 0.25: mom = 5
        elif -0.03 <= change_rate < 0.01: mom = 15
        elif -0.08 <= change_rate < -0.03: mom = 10
        else: mom = 0
    else:
        if 0.01 <= change_rate <= 0.05: mom = 25
        elif 0.05 < change_rate <= 0.10: mom = 15
        elif change_rate > 0.10: mom = 5
        elif -0.02 <= change_rate < 0.01: mom = 15
        elif -0.05 <= change_rate < -0.02: mom = 10
        else: mom = 0

    # 가격위치
    pv = change_rate
    if is_kr:
        if -0.03 <= pv <= 0.03: price = 20
        elif 0.03 < pv <= 0.08: price = 15
        elif 0.08 < pv <= 0.15: price = 10
        elif pv > 0.15: price = 5
        elif -0.08 <= pv < -0.03: price = 25
        else: price = 10
    else:
        if -0.02 <= pv <= 0.02: price = 20
        elif 0.02 < pv <= 0.05: price = 15
        elif pv > 0.05: price = 5
        elif -0.05 <= pv < -0.02: price = 25
        else: price = 10

    port = 20  # 시뮬레이션에서는 여력 충분 가정
    alphas = compute_alphas(o, c, h, l, ref_close)
    alpha_sc = alphas["composite"]
    total = vol_score + mom + price + port + alpha_sc

    # 시장 보정 (데몬과 동일)
    regime_adj = 0
    if change_rate >= 0.05 or change_rate <= -0.05:
        regime_adj = -15  # crisis
    elif change_rate <= -0.01:
        regime_adj = -10  # bear
    elif change_rate >= 0.01:
        regime_adj = 5    # bull
    total += regime_adj

    # 추세 필터 (데몬과 동일: 하락추세 내 반등 매수 방지)
    trend_warning = None
    momentum_val = alphas.get("momentum", 0)
    mean_rev_val = alphas.get("mean_rev", 0)
    if momentum_val < -0.02 and mean_rev_val > 0.01:
        trend_warning = "falling_knife"
        total -= 5

    total = max(0, total)
    pct = total / 130 * 100

    if pct >= 70: grade = "A"
    elif pct >= 55: grade = "B"
    elif pct >= 40: grade = "C"
    else: grade = "D"

    return {
        "score": round(total, 1), "pct": round(pct, 1), "grade": grade,
        "change_rate": round(change_rate * 100, 2),
        "regime_adj": regime_adj, "trend_warning": trend_warning,
        "breakdown": {"vol": vol_score, "mom": mom, "price": price, "alpha": round(alpha_sc, 1), "regime": regime_adj},
        "alphas": alphas,
    }


# ─── 시뮬레이션 ───

@dataclass
class Position:
    symbol: str
    entry_price: float
    entry_time: str
    qty: int
    stop_loss: float
    take_profit: float
    entry_grade: str
    entry_score: float

@dataclass
class TradeRecord:
    symbol: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    qty: int
    pnl_pct: float
    pnl_amount: float
    exit_reason: str
    entry_grade: str
    entry_score: float

@dataclass
class DayResult:
    date: str
    symbols: list
    market: str
    total_bars: int = 0
    total_trades: int = 0
    winning: int = 0
    losing: int = 0
    total_pnl_pct: float = 0
    total_pnl_amount: float = 0
    max_drawdown_pct: float = 0
    timeline: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    score_history: dict = field(default_factory=dict)


def simulate_day(symbols: list, date_str: str = None, market: str = "us",
                 initial_capital: float = 10_000_000,
                 stop_loss_pct: float = -0.03,
                 take_profit_pct: float = 0.07,
                 trailing_trigger: float = 0.03,
                 trailing_distance: float = 0.02,
                 max_positions: int = 2,
                 position_pct: float = 0.10,
                 entry_grades: list = None) -> DayResult:

    if entry_grades is None:
        entry_grades = ["A", "B"]

    is_kr = market.lower() == "kr"

    # 데이터 다운로드 (5분봉)
    all_data = {}
    ref_closes = {}

    for sym in symbols:
        ticker = yf.Ticker(sym)

        if date_str:
            target = datetime.strptime(date_str, "%Y-%m-%d")
            start = target
            end = target + timedelta(days=1)
            hist = ticker.history(start=start, end=end, interval="5m")
            # 전일 종가
            prev = ticker.history(start=target - timedelta(days=5), end=target)
            ref_closes[sym] = prev.iloc[-1]["Close"] if len(prev) > 0 else hist.iloc[0]["Open"] if len(hist) > 0 else 0
        else:
            hist = ticker.history(period="5d", interval="5m")
            if len(hist) == 0:
                continue
            # 마지막 거래일만
            last_date = hist.index[-1].date()
            hist = hist[hist.index.date == last_date]
            # 전일 종가
            prev_hist = ticker.history(period="10d")
            prev_dates = sorted(set(prev_hist.index.date))
            if len(prev_dates) >= 2:
                prev_date = prev_dates[-2]
                prev_day = prev_hist[prev_hist.index.date == prev_date]
                ref_closes[sym] = prev_day.iloc[-1]["Close"] if len(prev_day) > 0 else hist.iloc[0]["Open"]
            else:
                ref_closes[sym] = hist.iloc[0]["Open"]

        if len(hist) > 0:
            all_data[sym] = hist

    if not all_data:
        return DayResult(date=date_str or "unknown", symbols=symbols, market=market)

    # 공통 시간축 생성
    first_sym = list(all_data.keys())[0]
    actual_date = all_data[first_sym].index[0].strftime("%Y-%m-%d")

    result = DayResult(date=actual_date, symbols=symbols, market=market)
    result.score_history = {sym: [] for sym in all_data}

    # 상태
    capital = initial_capital
    positions = {}  # symbol -> Position
    peak_capital = capital
    peak_prices = {}  # symbol -> 보유 중 최고가 (트레일링용)
    timeline = []
    trades = []
    cumulative_vol = {sym: 0 for sym in all_data}
    cooldown_until = {}  # symbol -> 재진입 금지 인덱스 (손절 후 6봉 = 30분 쿨다운)

    # 장 시작 스크리닝 (첫 30분 관찰 후)
    screened = False
    watchlist_scores = {}

    # 시간순 정렬된 모든 바
    all_times = sorted(set().union(*[set(df.index) for df in all_data.values()]))
    result.total_bars = len(all_times)

    for i, ts in enumerate(all_times):
        time_str = ts.strftime("%H:%M")
        events = []

        # 각 종목 데이터 확인
        for sym, df in all_data.items():
            if ts not in df.index:
                continue

            bar = df.loc[ts]
            ref = ref_closes.get(sym, bar["Open"])
            cumulative_vol[sym] += bar.get("Volume", 0)

            sc = score_bar(bar, ref, cumulative_vol[sym], is_kr)
            result.score_history[sym].append({
                "time": time_str, "score": sc["score"], "grade": sc["grade"],
                "price": round(bar["Close"], 2), "change": sc["change_rate"],
            })

            # ── 보유 중이면 손절/익절/트레일링 체크 ──
            if sym in positions:
                pos = positions[sym]
                current = bar["Close"]
                pnl = (current - pos.entry_price) / pos.entry_price
                low_pnl = (bar["Low"] - pos.entry_price) / pos.entry_price
                high_pnl = (bar["High"] - pos.entry_price) / pos.entry_price

                # 트레일링용 고점 추적
                if sym not in peak_prices:
                    peak_prices[sym] = pos.entry_price
                peak_prices[sym] = max(peak_prices[sym], bar["High"])

                exit_reason = None
                exit_price = current

                if low_pnl <= stop_loss_pct:
                    exit_reason = "STOP_LOSS"
                    exit_price = pos.entry_price * (1 + stop_loss_pct)
                elif high_pnl >= take_profit_pct:
                    exit_reason = "TAKE_PROFIT"
                    exit_price = pos.entry_price * (1 + take_profit_pct)
                elif pnl >= trailing_trigger:
                    # 트레일링 스톱: 고점 대비 distance 만큼 하락
                    trail_price = peak_prices[sym] * (1 - trailing_distance)
                    if bar["Low"] <= trail_price:
                        exit_reason = "TRAILING_STOP"
                        exit_price = trail_price

                if exit_reason:
                    gross = (exit_price - pos.entry_price) / pos.entry_price
                    net = gross - 0.002  # 수수료
                    pnl_amt = net * pos.entry_price * pos.qty

                    trade = TradeRecord(
                        symbol=sym, entry_time=pos.entry_time, exit_time=time_str,
                        entry_price=round(pos.entry_price, 2), exit_price=round(exit_price, 2),
                        qty=pos.qty, pnl_pct=round(net * 100, 2), pnl_amount=round(pnl_amt),
                        exit_reason=exit_reason, entry_grade=pos.entry_grade, entry_score=pos.entry_score,
                    )
                    trades.append(trade)
                    capital += pnl_amt
                    del positions[sym]
                    peak_prices.pop(sym, None)

                    # 손절 후 쿨다운 (6봉 = 30분)
                    if exit_reason == "STOP_LOSS":
                        cooldown_until[sym] = i + 6

                    icon = "🟢" if net > 0 else "🔴"
                    events.append(f"{icon} {sym} {exit_reason} @ {exit_price:.2f} ({net*100:+.2f}%)")

            # ── 매수 판단 ──
            elif screened and sym not in positions and len(positions) < max_positions:
                # 쿨다운 체크 (손절 후 30분)
                if sym in cooldown_until and i < cooldown_until[sym]:
                    pass  # 재진입 금지
                elif sc["grade"] in entry_grades:
                    invest = capital * position_pct
                    qty = max(1, int(invest / bar["Close"]))
                    entry_price = bar["Close"]

                    positions[sym] = Position(
                        symbol=sym, entry_price=entry_price, entry_time=time_str,
                        qty=qty, stop_loss=entry_price * (1 + stop_loss_pct),
                        take_profit=entry_price * (1 + take_profit_pct),
                        entry_grade=sc["grade"], entry_score=sc["score"],
                    )
                    events.append(f"🔵 {sym} 매수 {sc['grade']}등급({sc['score']}점) @ {entry_price:.2f} x{qty}")

        # 장 시작 30분 후 스크리닝 완료
        if i == 6 and not screened:  # 5분 x 6 = 30분
            screened = True
            # 초기 채점 요약
            for sym in all_data:
                scores = result.score_history.get(sym, [])
                if scores:
                    latest = scores[-1]
                    watchlist_scores[sym] = latest
                    events.append(f"📋 {sym} 스크리닝: {latest['grade']}등급 ({latest['score']}점) @ {latest['price']}")

        # 드로다운 추적
        total_value = capital + sum(
            all_data[s].loc[ts]["Close"] * p.qty
            for s, p in positions.items()
            if ts in all_data[s].index
        ) if positions else capital
        peak_capital = max(peak_capital, total_value)
        drawdown = (total_value - peak_capital) / peak_capital * 100

        if events:
            timeline.append({
                "time": time_str,
                "events": events,
                "capital": round(total_value),
                "positions": len(positions),
                "drawdown": round(drawdown, 2),
            })

    # 장 마감: 열린 포지션 청산
    for sym, pos in list(positions.items()):
        if sym in all_data and len(all_data[sym]) > 0:
            last_price = all_data[sym].iloc[-1]["Close"]
            gross = (last_price - pos.entry_price) / pos.entry_price
            net = gross - 0.002
            pnl_amt = net * pos.entry_price * pos.qty

            trade = TradeRecord(
                symbol=sym, entry_time=pos.entry_time, exit_time="CLOSE",
                entry_price=round(pos.entry_price, 2), exit_price=round(last_price, 2),
                qty=pos.qty, pnl_pct=round(net * 100, 2), pnl_amount=round(pnl_amt),
                exit_reason="MARKET_CLOSE", entry_grade=pos.entry_grade, entry_score=pos.entry_score,
            )
            trades.append(trade)
            capital += pnl_amt
            timeline.append({
                "time": "CLOSE",
                "events": [f"🏁 {sym} 장마감 청산 @ {last_price:.2f} ({net*100:+.2f}%)"],
                "capital": round(capital),
                "positions": 0,
                "drawdown": 0,
            })

    # 결과 집계
    result.timeline = timeline
    result.trades = [asdict(t) for t in trades]
    result.total_trades = len(trades)
    pnls = [t.pnl_pct for t in trades]
    if pnls:
        result.winning = len([p for p in pnls if p > 0])
        result.losing = len([p for p in pnls if p <= 0])
        result.total_pnl_pct = round(sum(pnls), 2)
        result.total_pnl_amount = round(sum(t.pnl_amount for t in trades))

    return result


# ─── 출력 ───

def print_report(r: DayResult):
    print(f"\n{'='*70}")
    print(f"  하루 시뮬레이션: {r.date} ({r.market.upper()})")
    print(f"  종목: {', '.join(r.symbols)}")
    print(f"  총 {r.total_bars}개 5분봉 | {r.total_trades}건 거래")
    print(f"{'='*70}")

    print(f"\n  ── 타임라인 ──")
    for t in r.timeline:
        pos_bar = f"[포지션 {t['positions']}]" if t["positions"] > 0 else ""
        dd = f"DD {t['drawdown']}%" if t["drawdown"] < -0.5 else ""
        for ev in t["events"]:
            print(f"  {t['time']}  {ev}  {pos_bar} {dd}")

    if r.trades:
        print(f"\n  ── 거래 내역 ──")
        print(f"  {'종목':>6} {'진입':>6} {'청산':>6} {'등급':>3} {'진입가':>10} {'청산가':>10} {'수익률':>8} {'금액':>10} {'사유':>12}")
        for t in r.trades:
            print(f"  {t['symbol']:>6} {t['entry_time']:>6} {t['exit_time']:>6} {t['entry_grade']:>3} {t['entry_price']:>10.2f} {t['exit_price']:>10.2f} {t['pnl_pct']:>+7.2f}% {t['pnl_amount']:>+10,} {t['exit_reason']:>12}")

    print(f"\n  ── 일일 리포트 ──")
    print(f"  총 거래    {r.total_trades}건 (승: {r.winning} / 패: {r.losing})")
    win_rate = r.winning / r.total_trades * 100 if r.total_trades else 0
    print(f"  승률       {win_rate:.1f}%")
    print(f"  총 수익    {r.total_pnl_pct:+.2f}% ({r.total_pnl_amount:+,}원)")

    # 종목별 점수 변화 요약
    print(f"\n  ── 종목별 점수 변화 ──")
    for sym, scores in r.score_history.items():
        if not scores:
            continue
        grades = [s["grade"] for s in scores]
        score_vals = [s["score"] for s in scores]
        prices = [s["price"] for s in scores]
        print(f"  {sym}: 점수 {min(score_vals):.0f}~{max(score_vals):.0f} | "
              f"가격 {min(prices):.2f}~{max(prices):.2f} | "
              f"등급 분포: A={grades.count('A')} B={grades.count('B')} C={grades.count('C')} D={grades.count('D')}")

    print(f"\n{'='*70}\n")


# ─── CLI ───

def main():
    args = {}
    i = 1
    while i < len(sys.argv):
        if sys.argv[i].startswith("--"):
            key = sys.argv[i][2:]
            if i + 1 < len(sys.argv) and not sys.argv[i+1].startswith("--"):
                args[key] = sys.argv[i + 1]; i += 2
            else:
                args[key] = "true"; i += 1
        else:
            i += 1

    symbols = args.get("symbols", "TSLA,NVDA,AAPL").split(",")
    market = args.get("market", "us")
    date_str = args.get("date", None)
    output = args.get("output", "table")

    result = simulate_day(
        symbols=[s.strip() for s in symbols],
        date_str=date_str,
        market=market,
    )

    if output == "table":
        print_report(result)
    elif output == "json":
        d = asdict(result)
        print(json.dumps(d, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
