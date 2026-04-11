#!/usr/bin/env python3
"""
backtest.py — 시그널 엔진 백테스트

과거 데이터(yfinance)로 우리의 알파 + 채점 시스템을 검증합니다.

사용법:
  # 단일 종목 백테스트
  python3 scripts/backtest.py --symbol PLTR --period 6mo

  # 여러 종목
  python3 scripts/backtest.py --symbol PLTR,TSLA,AAPL --period 1y

  # 한국주식 (야후 종목코드: 005930.KS, 035420.KS)
  python3 scripts/backtest.py --symbol 005930.KS --period 6mo

  # 결과를 JSON으로
  python3 scripts/backtest.py --symbol PLTR --period 6mo --output json
"""

import json
import math
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("yfinance, pandas 필요: pip install yfinance pandas")
    sys.exit(1)


# ─── 알파 계산 (signal-engine.py와 동일 로직) ───

def compute_alphas_row(o, c, h, l, ref_close):
    """단일 캔들의 알파 값을 계산"""
    hl = h - l + 0.001

    alpha101 = (c - o) / hl if hl > 0 else 0
    alpha33 = (1 - o / c) if c > 0 else 0
    alpha54 = (c - l) / (h - l) if (h - l) > 0 else 0.5
    mean_rev = -math.log(o / ref_close) if o > 0 and ref_close > 0 else 0
    momentum = math.log(c / ref_close) if c > 0 and ref_close > 0 else 0

    # 복합 점수 (0-30)
    score = 0
    score += max(0, min(10, (alpha101 + 1) * 5))
    score += max(0, min(10, (mean_rev * 100) + 5))
    score += alpha54 * 10

    return {
        "alpha101": alpha101,
        "alpha33": alpha33,
        "alpha54": alpha54,
        "mean_reversion": mean_rev,
        "momentum": momentum,
        "composite": score,
    }


def compute_daily_score(row, prev_close, volume, is_kr=False):
    """일일 종합 점수 계산 (130점 만점, signal-engine과 동일)"""
    o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
    change_rate = (c - prev_close) / prev_close if prev_close > 0 else 0

    # 1. 거래량 (0-30)
    if is_kr:
        thresholds = [(5e6, 30), (3e6, 25), (1e6, 20), (5e5, 15), (1e5, 10)]
    else:
        thresholds = [(50e6, 30), (20e6, 25), (10e6, 20), (5e6, 15), (1e6, 10)]
    vol_score = 5
    for threshold, score in thresholds:
        if volume >= threshold:
            vol_score = score
            break

    # 2. 모멘텀 (0-25)
    if is_kr:
        if 0.01 <= change_rate <= 0.08: mom_score = 25
        elif 0.08 < change_rate <= 0.15: mom_score = 20
        elif 0.15 < change_rate <= 0.25: mom_score = 15
        elif change_rate > 0.25: mom_score = 5
        elif -0.03 <= change_rate < 0.01: mom_score = 15
        elif -0.08 <= change_rate < -0.03: mom_score = 10
        else: mom_score = 0
    else:
        if 0.01 <= change_rate <= 0.05: mom_score = 25
        elif 0.05 < change_rate <= 0.10: mom_score = 15
        elif change_rate > 0.10: mom_score = 5
        elif -0.02 <= change_rate < 0.01: mom_score = 15
        elif -0.05 <= change_rate < -0.02: mom_score = 10
        else: mom_score = 0

    # 3. 가격위치 (0-25)
    price_vs_ref = (c - prev_close) / prev_close if prev_close > 0 else 0
    if is_kr:
        if -0.03 <= price_vs_ref <= 0.03: price_score = 20
        elif 0.03 < price_vs_ref <= 0.08: price_score = 15
        elif 0.08 < price_vs_ref <= 0.15: price_score = 10
        elif price_vs_ref > 0.15: price_score = 5
        elif -0.08 <= price_vs_ref < -0.03: price_score = 25
        else: price_score = 10
    else:
        if -0.02 <= price_vs_ref <= 0.02: price_score = 20
        elif 0.02 < price_vs_ref <= 0.05: price_score = 15
        elif price_vs_ref > 0.05: price_score = 5
        elif -0.05 <= price_vs_ref < -0.02: price_score = 25
        else: price_score = 10

    # 4. 여력: 백테스트에서는 항상 충분하다고 가정 (20점)
    port_score = 20

    # 5. 알파 (0-30)
    alphas = compute_alphas_row(o, c, h, l, prev_close)
    alpha_score = alphas["composite"]

    total = vol_score + mom_score + price_score + port_score + alpha_score
    pct = total / 130 * 100

    if pct >= 70: grade = "A"
    elif pct >= 55: grade = "B"
    elif pct >= 40: grade = "C"
    else: grade = "D"

    return {
        "total": round(total, 1),
        "pct": round(pct, 1),
        "grade": grade,
        "volume": vol_score,
        "momentum": mom_score,
        "price": price_score,
        "alpha": round(alpha_score, 1),
        "change_rate": round(change_rate * 100, 2),
        "alphas": alphas,
    }


# ─── 백테스트 엔진 ───

@dataclass
class Trade:
    entry_date: str
    entry_price: float
    exit_date: str = ""
    exit_price: float = 0
    entry_grade: str = ""
    entry_score: float = 0
    pnl_pct: float = 0
    holding_days: int = 0
    exit_reason: str = ""


@dataclass
class BacktestResult:
    symbol: str
    period: str
    market: str
    total_days: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0
    avg_pnl_pct: float = 0
    total_pnl_pct: float = 0
    max_win_pct: float = 0
    max_loss_pct: float = 0
    avg_holding_days: float = 0
    sharpe_approx: float = 0
    grade_distribution: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    daily_scores: list = field(default_factory=list)


def run_backtest(
    symbol: str,
    period: str = "6mo",
    entry_grades: list = None,
    stop_loss: float = -0.03,
    take_profit: float = 0.07,
    max_hold_days: int = 5,
) -> BacktestResult:
    """
    백테스트 실행

    전략:
    - entry_grades (기본 A/B) 등급이 나온 날 종가에 매수
    - 손절/익절/최대보유일 중 먼저 도달하면 청산
    - 한 번에 1 포지션만 보유
    """
    if entry_grades is None:
        entry_grades = ["A", "B"]

    is_kr = ".KS" in symbol or ".KQ" in symbol
    market = "KR" if is_kr else "US"

    # 데이터 가져오기
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period)

    if df.empty:
        return BacktestResult(symbol=symbol, period=period, market=market)

    result = BacktestResult(
        symbol=symbol,
        period=period,
        market=market,
        total_days=len(df),
    )

    grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    trades = []
    daily_scores_list = []

    # 일별 점수 계산
    in_position = False
    current_trade = None

    for i in range(1, len(df)):
        prev_close = df.iloc[i - 1]["Close"]
        row = df.iloc[i]
        date_str = df.index[i].strftime("%Y-%m-%d")
        volume = row.get("Volume", 0)

        score = compute_daily_score(row, prev_close, volume, is_kr)
        grade = score["grade"]
        grade_counts[grade] += 1

        daily_scores_list.append({
            "date": date_str,
            "score": score["total"],
            "pct": score["pct"],
            "grade": grade,
            "close": round(row["Close"], 2),
            "change": score["change_rate"],
        })

        if in_position:
            # 포지션 보유 중 → 청산 조건 체크
            current_pnl = (row["Close"] - current_trade.entry_price) / current_trade.entry_price
            days_held = current_trade.holding_days + 1
            current_trade.holding_days = days_held

            # 장중 저가로 손절 체크
            low_pnl = (row["Low"] - current_trade.entry_price) / current_trade.entry_price
            # 장중 고가로 익절 체크
            high_pnl = (row["High"] - current_trade.entry_price) / current_trade.entry_price

            exit_reason = None
            exit_price = row["Close"]

            if low_pnl <= stop_loss:
                exit_reason = "STOP_LOSS"
                exit_price = current_trade.entry_price * (1 + stop_loss)
            elif high_pnl >= take_profit:
                exit_reason = "TAKE_PROFIT"
                exit_price = current_trade.entry_price * (1 + take_profit)
            elif days_held >= max_hold_days:
                exit_reason = "MAX_HOLD"
                exit_price = row["Close"]

            if exit_reason:
                current_trade.exit_date = date_str
                current_trade.exit_price = round(exit_price, 2)
                current_trade.pnl_pct = round((exit_price - current_trade.entry_price) / current_trade.entry_price * 100, 2)
                current_trade.exit_reason = exit_reason
                trades.append(current_trade)
                in_position = False
                current_trade = None

        else:
            # 포지션 없음 → 진입 조건 체크
            if grade in entry_grades:
                current_trade = Trade(
                    entry_date=date_str,
                    entry_price=round(row["Close"], 2),
                    entry_grade=grade,
                    entry_score=score["total"],
                )
                in_position = True

    # 마지막 포지션이 열려있으면 종가로 청산
    if in_position and current_trade:
        last = df.iloc[-1]
        current_trade.exit_date = df.index[-1].strftime("%Y-%m-%d")
        current_trade.exit_price = round(last["Close"], 2)
        current_trade.pnl_pct = round((last["Close"] - current_trade.entry_price) / current_trade.entry_price * 100, 2)
        current_trade.exit_reason = "END_OF_DATA"
        trades.append(current_trade)

    # 결과 집계
    result.total_trades = len(trades)
    result.grade_distribution = grade_counts
    result.daily_scores = daily_scores_list

    if trades:
        pnls = [t.pnl_pct for t in trades]
        result.winning_trades = sum(1 for p in pnls if p > 0)
        result.losing_trades = sum(1 for p in pnls if p <= 0)
        result.win_rate = round(result.winning_trades / len(trades) * 100, 1)
        result.avg_pnl_pct = round(sum(pnls) / len(pnls), 2)
        result.total_pnl_pct = round(sum(pnls), 2)
        result.max_win_pct = round(max(pnls), 2)
        result.max_loss_pct = round(min(pnls), 2)
        result.avg_holding_days = round(sum(t.holding_days for t in trades) / len(trades), 1)

        if len(pnls) > 1:
            import statistics
            mean = statistics.mean(pnls)
            stdev = statistics.stdev(pnls)
            result.sharpe_approx = round(mean / stdev, 2) if stdev > 0 else 0

        result.trades = [asdict(t) for t in trades]

    return result


# ─── 출력 ───

def print_report(r: BacktestResult):
    """터미널용 리포트 출력"""
    print(f"\n{'='*60}")
    print(f"  백테스트 결과: {r.symbol} ({r.market})")
    print(f"  기간: {r.period} | 총 {r.total_days}일")
    print(f"{'='*60}")

    print(f"\n  등급 분포:")
    for g in ["A", "B", "C", "D"]:
        cnt = r.grade_distribution.get(g, 0)
        bar = "#" * min(cnt, 40)
        print(f"    {g}: {cnt:>4}일  {bar}")

    print(f"\n  거래 성과:")
    print(f"    총 거래      {r.total_trades}건")
    print(f"    승리          {r.winning_trades}건")
    print(f"    패배          {r.losing_trades}건")
    print(f"    승률          {r.win_rate}%")
    print(f"    평균 수익     {'+' if r.avg_pnl_pct >= 0 else ''}{r.avg_pnl_pct}%")
    print(f"    누적 수익     {'+' if r.total_pnl_pct >= 0 else ''}{r.total_pnl_pct}%")
    print(f"    최대 수익     +{r.max_win_pct}%")
    print(f"    최대 손실     {r.max_loss_pct}%")
    print(f"    평균 보유     {r.avg_holding_days}일")
    print(f"    샤프 비율     {r.sharpe_approx}")

    if r.trades:
        print(f"\n  최근 거래 (최대 10건):")
        print(f"    {'진입일':>12} {'청산일':>12} {'등급':>3} {'진입가':>10} {'청산가':>10} {'수익률':>8} {'사유':>12}")
        print(f"    {'-'*12} {'-'*12} {'-'*3} {'-'*10} {'-'*10} {'-'*8} {'-'*12}")
        for t in r.trades[-10:]:
            pnl = f"{'+' if t['pnl_pct'] >= 0 else ''}{t['pnl_pct']}%"
            print(f"    {t['entry_date']:>12} {t['exit_date']:>12} {t['entry_grade']:>3} {t['entry_price']:>10.2f} {t['exit_price']:>10.2f} {pnl:>8} {t['exit_reason']:>12}")

    print(f"\n{'='*60}\n")


# ─── CLI ───

def main():
    args = {}
    i = 1
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

    symbols = args.get("symbol", "PLTR").split(",")
    period = args.get("period", "6mo")
    output = args.get("output", "table")
    stop_loss = float(args.get("stop-loss", "-0.03"))
    take_profit = float(args.get("take-profit", "0.07"))
    max_hold = int(args.get("max-hold", "5"))
    grades = args.get("grades", "A,B").split(",")

    all_results = []

    for symbol in symbols:
        symbol = symbol.strip()
        result = run_backtest(
            symbol=symbol,
            period=period,
            entry_grades=grades,
            stop_loss=stop_loss,
            take_profit=take_profit,
            max_hold_days=max_hold,
        )

        if output == "table":
            print_report(result)
        all_results.append(asdict(result))

    if output == "json":
        # daily_scores 생략 (너무 길어서)
        for r in all_results:
            r.pop("daily_scores", None)
        print(json.dumps(all_results, ensure_ascii=False, indent=2))

    # 여러 종목 비교 요약
    if len(all_results) > 1 and output == "table":
        print(f"\n{'='*60}")
        print(f"  종목 비교 요약")
        print(f"{'='*60}")
        print(f"  {'종목':>8} {'거래':>4} {'승률':>6} {'누적수익':>8} {'평균수익':>8} {'샤프':>6}")
        for r in all_results:
            print(f"  {r['symbol']:>8} {r['total_trades']:>4} {r['win_rate']:>5.1f}% {r['total_pnl_pct']:>+7.2f}% {r['avg_pnl_pct']:>+7.2f}% {r['sharpe_approx']:>6.2f}")


if __name__ == "__main__":
    main()
