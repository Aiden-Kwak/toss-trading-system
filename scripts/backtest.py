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
    """일일 종합 점수 계산 (100점 만점, signal-engine v2와 동일)

    v2: "오늘 사야 하는가"를 판단하는 타이밍 시그널 중심.
    """
    o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
    change_rate = (c - prev_close) / prev_close if prev_close > 0 else 0

    alphas = compute_alphas_row(o, c, h, l, prev_close)
    a101 = alphas["alpha101"]
    a54 = alphas["alpha54"]
    momentum_val = alphas["momentum"]
    mean_rev_val = alphas["mean_reversion"]

    total = 0

    # 1. 방향성 시그널 (0-30): 알파 동의 카운트
    bullish = sum(1 for v in [a101, alphas["alpha33"], a54 - 0.5, momentum_val * 10] if v > 0.05)
    bearish = sum(1 for v in [a101, alphas["alpha33"], a54 - 0.5, momentum_val * 10] if v < -0.05)
    if bullish >= 4: dir_score = 30
    elif bullish >= 3: dir_score = 22
    elif bullish >= 2: dir_score = 12
    elif bearish >= 3: dir_score = 0
    else: dir_score = 5
    total += dir_score

    # 2. 모멘텀 품질 (0-25)
    abs_change = abs(change_rate)
    sweet = (0.02 <= abs_change <= 0.08) if is_kr else (0.015 <= abs_change <= 0.05)
    too_hot = abs_change > (0.15 if is_kr else 0.10)
    if change_rate > 0 and sweet: mom = 25
    elif change_rate > 0 and not too_hot: mom = 15
    elif too_hot: mom = 0
    elif abs_change < 0.005: mom = 0  # 보합 → 진입 이유 없음
    elif change_rate < 0 and abs_change < 0.03: mom = 5
    else: mom = 0
    total += mom

    # 3. 가격 액션 (0-20): 캔들 품질
    if a101 > 0.5 and a54 > 0.7: action = 20
    elif a101 > 0.3 and a54 > 0.5: action = 15
    elif a101 > 0 and a54 > 0.4: action = 10
    elif a101 < -0.3: action = 0
    else: action = 5
    total += action

    # 4. 시장 컨텍스트 (0-25)
    if abs(change_rate) >= 0.05: regime = "crisis"; ctx = 0
    elif change_rate <= -0.01: regime = "bear"; ctx = 0
    elif change_rate >= 0.01: regime = "bull"; ctx = 20
    else: regime = "range"; ctx = 10

    trend_warning = None
    if momentum_val < -0.02 and mean_rev_val > 0.01:
        trend_warning = "falling_knife"
        ctx = max(0, ctx - 10)
    total += ctx

    total = max(0, total)
    pct = total  # 100점 만점이라 pct = total

    if pct >= 75: grade = "A"
    elif pct >= 55: grade = "B"
    elif pct >= 35: grade = "C"
    else: grade = "D"

    return {
        "total": round(total, 1),
        "pct": round(pct, 1),
        "grade": grade,
        "regime": regime,
        "trend_warning": trend_warning,
        "direction": dir_score,
        "momentum": mom,
        "action": action,
        "context": ctx,
        "change_rate": round(change_rate * 100, 2),
        "alphas": alphas,
    }


# ─── 백테스트 엔진 ───

@dataclass
class Trade:
    signal_date: str           # 시그널 발생일 (채점일)
    entry_date: str            # 실제 매수일 (T+1)
    entry_price: float = 0     # T+1 시가
    exit_date: str = ""
    exit_price: float = 0
    entry_grade: str = ""
    entry_score: float = 0
    pnl_pct: float = 0         # 수수료 차감 후 수익률
    pnl_gross_pct: float = 0   # 수수료 차감 전 수익률
    cost_pct: float = 0        # 수수료 비용
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
    buy_and_hold_pct: float = 0   # 벤치마크: 단순 보유 수익률
    excess_return_pct: float = 0  # 전략 - Buy&Hold
    total_cost_pct: float = 0     # 총 수수료 비용
    grade_distribution: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    daily_scores: list = field(default_factory=list)


def compute_atr(df, period=14) -> dict:
    """일별 ATR(Average True Range) 계산 → 종목 변동성 측정"""
    atrs = {}
    for i in range(period, len(df)):
        window = df.iloc[i-period:i]
        true_ranges = []
        for j in range(1, len(window)):
            h, l, pc = window.iloc[j]["High"], window.iloc[j]["Low"], window.iloc[j-1]["Close"]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            true_ranges.append(tr)
        atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0
        date_str = df.index[i].strftime("%Y-%m-%d")
        atrs[date_str] = atr
    return atrs


def run_backtest(
    symbol: str,
    period: str = "6mo",
    entry_grades: list = None,
    stop_loss: float = -0.03,
    take_profit: float = 0.07,
    max_hold_days: int = 5,
    cost_per_trade: float = 0.001,  # 편도 0.1% (왕복 0.2%)
    use_dip_buy: bool = True,       # 딥 바잉 (시가 대비 하락 시 매수)
    dip_pct: float = 0.005,         # 딥 바잉 기준: 시가 대비 -0.5%
    use_atr_stops: bool = True,     # ATR 기반 동적 손익절
    atr_sl_mult: float = 1.5,       # ATR x N = 손절 거리
    atr_tp_mult: float = 3.0,       # ATR x N = 익절 거리
) -> BacktestResult:
    """
    백테스트 실행

    전략 (미래 참조 제거):
    - T일 장 마감 후 OHLCV로 채점
    - A/B 등급이면 T+1일 시가에 매수 (실현 가능한 진입)
    - 보유 중 매일 체크: 손절/익절/최대보유일
    - 청산 시 수수료(왕복 0.2%) 차감
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

    # Buy & Hold 벤치마크
    bh_start = df.iloc[0]["Close"]
    bh_end = df.iloc[-1]["Close"]
    buy_and_hold = (bh_end - bh_start) / bh_start * 100

    result = BacktestResult(
        symbol=symbol,
        period=period,
        market=market,
        total_days=len(df),
        buy_and_hold_pct=round(buy_and_hold, 2),
    )

    # ATR 계산 (14일 평균 변동폭)
    atrs = compute_atr(df) if use_atr_stops else {}

    grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    trades = []
    daily_scores_list = []

    in_position = False
    current_trade = None
    pending_entry = None  # T일 시그널 → T+1 매수 대기

    for i in range(1, len(df)):
        prev_close = df.iloc[i - 1]["Close"]
        row = df.iloc[i]
        date_str = df.index[i].strftime("%Y-%m-%d")
        volume = row.get("Volume", 0)

        # T일 채점
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

        # T+1 매수 실행 (전일 시그널이 있었으면)
        if pending_entry and not in_position:
            open_price = row["Open"]

            if use_dip_buy:
                # 딥 바잉: 시가 대비 dip_pct 하락한 가격을 목표
                # 장중 저가가 목표가 이하면 → 목표가에 체결됐다고 가정
                dip_target = open_price * (1 - dip_pct)
                if row["Low"] <= dip_target:
                    entry_price = dip_target  # 딥에서 매수 성공
                else:
                    # 딥 없이 상승만 → 매수 포기 (다음 기회 대기)
                    pending_entry = None
                    continue
            else:
                entry_price = open_price  # 기존: 시가에 바로 매수

            # ATR 기반 동적 손익절
            atr_val = atrs.get(date_str, 0)
            if use_atr_stops and atr_val > 0:
                dynamic_sl = -(atr_val * atr_sl_mult / entry_price)  # ATR x 1.5 / 진입가 = 비율
                dynamic_tp = atr_val * atr_tp_mult / entry_price     # ATR x 3.0 / 진입가 = 비율
            else:
                dynamic_sl = stop_loss
                dynamic_tp = take_profit

            current_trade = Trade(
                signal_date=pending_entry["date"],
                entry_date=date_str,
                entry_price=round(entry_price, 2),
                entry_grade=pending_entry["grade"],
                entry_score=pending_entry["score"],
            )
            # 동적 손익절을 trade에 저장 (나중에 청산 시 사용)
            current_trade._dynamic_sl = dynamic_sl
            current_trade._dynamic_tp = dynamic_tp
            in_position = True
            pending_entry = None

        if in_position:
            # 포지션 보유 중 → 청산 조건 체크
            days_held = current_trade.holding_days + 1
            current_trade.holding_days = days_held

            # 장중 저가로 손절 체크
            low_pnl = (row["Low"] - current_trade.entry_price) / current_trade.entry_price
            # 장중 고가로 익절 체크
            high_pnl = (row["High"] - current_trade.entry_price) / current_trade.entry_price

            exit_reason = None
            exit_price = row["Close"]

            # 동적 손익절 사용 (ATR 기반)
            eff_sl = getattr(current_trade, '_dynamic_sl', stop_loss)
            eff_tp = getattr(current_trade, '_dynamic_tp', take_profit)

            if low_pnl <= eff_sl:
                exit_reason = "STOP_LOSS"
                exit_price = current_trade.entry_price * (1 + eff_sl)
            elif high_pnl >= eff_tp:
                exit_reason = "TAKE_PROFIT"
                exit_price = current_trade.entry_price * (1 + eff_tp)
            elif days_held >= max_hold_days:
                exit_reason = "MAX_HOLD"
                exit_price = row["Close"]

            if exit_reason:
                gross_pnl = (exit_price - current_trade.entry_price) / current_trade.entry_price
                cost = cost_per_trade * 2  # 왕복 수수료
                net_pnl = gross_pnl - cost

                current_trade.exit_date = date_str
                current_trade.exit_price = round(exit_price, 2)
                current_trade.pnl_gross_pct = round(gross_pnl * 100, 2)
                current_trade.pnl_pct = round(net_pnl * 100, 2)
                current_trade.cost_pct = round(cost * 100, 2)
                current_trade.exit_reason = exit_reason
                trades.append(current_trade)
                in_position = False
                current_trade = None

        # 포지션 없고 시그널 발생 → 다음 날 매수 예약
        if not in_position and not pending_entry:
            if grade in entry_grades:
                pending_entry = {
                    "date": date_str,
                    "grade": grade,
                    "score": score["total"],
                }

    # 마지막 포지션이 열려있으면 종가로 청산
    if in_position and current_trade:
        last = df.iloc[-1]
        gross_pnl = (last["Close"] - current_trade.entry_price) / current_trade.entry_price
        cost = cost_per_trade * 2
        current_trade.exit_date = df.index[-1].strftime("%Y-%m-%d")
        current_trade.exit_price = round(last["Close"], 2)
        current_trade.pnl_gross_pct = round(gross_pnl * 100, 2)
        current_trade.pnl_pct = round((gross_pnl - cost) * 100, 2)
        current_trade.cost_pct = round(cost * 100, 2)
        current_trade.exit_reason = "END_OF_DATA"
        trades.append(current_trade)

    # 결과 집계
    result.total_trades = len(trades)
    result.grade_distribution = grade_counts
    result.daily_scores = daily_scores_list

    if trades:
        pnls = [t.pnl_pct for t in trades]
        costs = [t.cost_pct for t in trades]
        result.winning_trades = sum(1 for p in pnls if p > 0)
        result.losing_trades = sum(1 for p in pnls if p <= 0)
        result.win_rate = round(result.winning_trades / len(trades) * 100, 1)
        result.avg_pnl_pct = round(sum(pnls) / len(pnls), 2)
        result.total_pnl_pct = round(sum(pnls), 2)
        result.max_win_pct = round(max(pnls), 2)
        result.max_loss_pct = round(min(pnls), 2)
        result.avg_holding_days = round(sum(t.holding_days for t in trades) / len(trades), 1)
        result.total_cost_pct = round(sum(costs), 2)
        result.excess_return_pct = round(result.total_pnl_pct - result.buy_and_hold_pct, 2)

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

    print(f"\n  거래 성과 (수수료 0.2%/왕복 차감):")
    print(f"    총 거래      {r.total_trades}건")
    print(f"    승리          {r.winning_trades}건")
    print(f"    패배          {r.losing_trades}건")
    print(f"    승률          {r.win_rate}%")
    print(f"    평균 수익     {'+' if r.avg_pnl_pct >= 0 else ''}{r.avg_pnl_pct}% (수수료 차감 후)")
    print(f"    누적 수익     {'+' if r.total_pnl_pct >= 0 else ''}{r.total_pnl_pct}%")
    print(f"    총 수수료     -{r.total_cost_pct}%")
    print(f"    최대 수익     +{r.max_win_pct}%")
    print(f"    최대 손실     {r.max_loss_pct}%")
    print(f"    평균 보유     {r.avg_holding_days}일")
    print(f"    샤프 비율     {r.sharpe_approx}")

    print(f"\n  벤치마크 비교:")
    print(f"    Buy & Hold   {'+' if r.buy_and_hold_pct >= 0 else ''}{r.buy_and_hold_pct}%")
    print(f"    전략 수익     {'+' if r.total_pnl_pct >= 0 else ''}{r.total_pnl_pct}%")
    excess = r.excess_return_pct
    label = "초과수익" if excess >= 0 else "미달수익"
    print(f"    {label}      {'+' if excess >= 0 else ''}{excess}%")

    print(f"\n  진입 방식: T일 채점 → T+1일 시가 매수 (미래 참조 없음)")

    if r.trades:
        print(f"\n  최근 거래 (최대 10건):")
        print(f"    {'시그널':>12} {'진입(T+1)':>12} {'청산':>12} {'등급':>3} {'진입가':>10} {'청산가':>10} {'순수익':>8} {'사유':>12}")
        print(f"    {'-'*12} {'-'*12} {'-'*12} {'-'*3} {'-'*10} {'-'*10} {'-'*8} {'-'*12}")
        for t in r.trades[-10:]:
            pnl = f"{'+' if t['pnl_pct'] >= 0 else ''}{t['pnl_pct']}%"
            print(f"    {t['signal_date']:>12} {t['entry_date']:>12} {t['exit_date']:>12} {t['entry_grade']:>3} {t['entry_price']:>10.2f} {t['exit_price']:>10.2f} {pnl:>8} {t['exit_reason']:>12}")

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
        print(f"  {'종목':>10} {'거래':>4} {'승률':>6} {'전략':>8} {'B&H':>8} {'초과':>8} {'수수료':>6} {'샤프':>6}")
        for r in all_results:
            ex = r['excess_return_pct']
            print(f"  {r['symbol']:>10} {r['total_trades']:>4} {r['win_rate']:>5.1f}% {r['total_pnl_pct']:>+7.2f}% {r['buy_and_hold_pct']:>+7.2f}% {ex:>+7.2f}% {r['total_cost_pct']:>5.2f}% {r['sharpe_approx']:>6.2f}")


if __name__ == "__main__":
    main()
