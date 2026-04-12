#!/usr/bin/env python3
"""
portfolio-backtest.py — 포트폴리오 레벨 백테스트

실제 데몬과 동일하게:
- 여러 종목을 매일 동시 채점
- 우선순위 정렬 → 포지션 한도까지 병렬 진입
- 자본 균등 배분
- 손절/익절/트레일링/ATR 적용
- 포트폴리오 일일 P&L 추적

사용법:
  # 기본 (US 대형주 10종목, 6개월)
  python3 scripts/portfolio-backtest.py

  # 커스텀
  python3 scripts/portfolio-backtest.py \
    --symbols TSLA,NVDA,AAPL,MSFT,GOOG,AMZN,META,NFLX \
    --period 6mo --capital 1000000 --max-positions 2 \
    --stop-loss -0.02 --take-profit 0.06 --max-hold 1

  # JSON 출력
  python3 scripts/portfolio-backtest.py --output json
"""

import json
import math
import sys
import statistics
from datetime import datetime
from dataclasses import dataclass, field, asdict
from pathlib import Path

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("pip install yfinance pandas")
    sys.exit(1)


# ─── 채점 (signal-engine v2 동일) ───

def compute_alphas(o, c, h, l, ref):
    hl = h - l + 0.001
    a101 = (c - o) / hl if hl > 0 else 0
    a33 = (1 - o / c) if c > 0 else 0
    a54 = (c - l) / (h - l) if (h - l) > 0 else 0.5
    mom = math.log(c / ref) if c > 0 and ref > 0 else 0
    mr = -math.log(o / ref) if o > 0 and ref > 0 else 0
    return {"a101": a101, "a33": a33, "a54": a54, "momentum": mom, "mean_rev": mr}


def score_day(row, prev_close, is_kr=False):
    """일봉 채점 (100점 만점, signal-engine v2)"""
    o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
    cr = (c - prev_close) / prev_close if prev_close > 0 else 0
    a = compute_alphas(o, c, h, l, prev_close)
    total = 0

    # 1. 방향성 (0-30)
    bull = sum(1 for v in [a["a101"], a["a33"], a["a54"] - 0.5, a["momentum"] * 10] if v > 0.05)
    bear = sum(1 for v in [a["a101"], a["a33"], a["a54"] - 0.5, a["momentum"] * 10] if v < -0.05)
    if bull >= 4: d = 30
    elif bull >= 3: d = 22
    elif bull >= 2: d = 12
    elif bear >= 3: d = 0
    else: d = 5
    total += d

    # 2. 모멘텀 품질 (0-25)
    ac = abs(cr)
    sweet = (0.02 <= ac <= 0.08) if is_kr else (0.015 <= ac <= 0.05)
    hot = ac > (0.15 if is_kr else 0.10)
    if cr > 0 and sweet: m = 25
    elif cr > 0 and not hot: m = 15
    elif hot or ac < 0.005: m = 0
    elif cr < 0 and ac < 0.03: m = 5
    else: m = 0
    total += m

    # 3. 가격 액션 (0-20)
    if a["a101"] > 0.5 and a["a54"] > 0.7: pa = 20
    elif a["a101"] > 0.3 and a["a54"] > 0.5: pa = 15
    elif a["a101"] > 0 and a["a54"] > 0.4: pa = 10
    elif a["a101"] < -0.3: pa = 0
    else: pa = 5
    total += pa

    # 4. 컨텍스트 (0-25)
    if abs(cr) >= 0.05: regime = "crisis"; ctx = 0
    elif cr <= -0.01: regime = "bear"; ctx = 0
    elif cr >= 0.01: regime = "bull"; ctx = 20
    else: regime = "range"; ctx = 10
    if a["momentum"] < -0.02 and a["mean_rev"] > 0.01:
        ctx = max(0, ctx - 10)
    total += ctx

    total = max(0, total)
    if total >= 75: grade = "A"
    elif total >= 55: grade = "B"
    elif total >= 35: grade = "C"
    else: grade = "D"

    return {"score": total, "grade": grade, "change": round(cr * 100, 2), "regime": regime}


# ─── ATR ───

def calc_atr(hist, period=14):
    if len(hist) < period + 1:
        return 0
    trs = []
    for j in range(1, min(period + 1, len(hist))):
        h, l, pc = hist.iloc[-j]["High"], hist.iloc[-j]["Low"], hist.iloc[-j - 1]["Close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0


# ─── 포트폴리오 백테스트 ───

@dataclass
class PfTrade:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    qty: int
    pnl_pct: float
    pnl_amount: float
    exit_reason: str
    grade: str
    score: float


@dataclass
class PfResult:
    period: str
    symbols: list
    initial_capital: float
    final_capital: float
    total_return_pct: float
    buy_and_hold_pct: float
    excess_pct: float
    total_trades: int
    winning: int
    losing: int
    win_rate: float
    sharpe: float
    max_drawdown_pct: float
    avg_positions: float
    daily_returns: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    by_symbol: dict = field(default_factory=dict)
    equity_curve: list = field(default_factory=list)


def run_portfolio_backtest(
    symbols: list,
    period: str = "6mo",
    initial_capital: float = 1_000_000,
    max_positions: int = 2,
    stop_loss: float = -0.02,
    take_profit: float = 0.06,
    max_hold: int = 1,
    entry_grades: list = None,
    cost: float = 0.001,
    dip_pct: float = 0.005,
    atr_sl_mult: float = 1.5,
    atr_tp_mult: float = 3.0,
) -> PfResult:

    if entry_grades is None:
        entry_grades = ["A"]

    # 데이터 다운로드
    all_data = {}
    for sym in symbols:
        ticker = yf.Ticker(sym)
        hist = ticker.history(period=period)
        if len(hist) > 20:
            all_data[sym] = hist

    if not all_data:
        return PfResult(period=period, symbols=symbols, initial_capital=initial_capital,
                        final_capital=initial_capital, total_return_pct=0, buy_and_hold_pct=0,
                        excess_pct=0, total_trades=0, winning=0, losing=0, win_rate=0,
                        sharpe=0, max_drawdown_pct=0, avg_positions=0)

    # 공통 날짜 축
    all_dates = sorted(set().union(*[set(df.index) for df in all_data.values()]))

    # B&H 벤치마크: 전 종목 균등 투자
    bh_start = sum(all_data[s].iloc[0]["Close"] for s in all_data) / len(all_data)
    bh_end = sum(all_data[s].iloc[-1]["Close"] for s in all_data) / len(all_data)
    bh_return = (bh_end - bh_start) / bh_start * 100

    # 상태
    capital = initial_capital
    cash = initial_capital
    positions = {}  # sym -> {entry_price, qty, entry_date, grade, score, sl, tp, hold_days}
    trades = []
    daily_returns = []
    equity_curve = []
    peak_equity = initial_capital
    max_dd = 0
    position_counts = []
    pending = {}  # sym -> {grade, score, date}

    for i, date in enumerate(all_dates):
        date_str = date.strftime("%Y-%m-%d")
        events = []

        # 각 종목 채점
        day_scores = {}
        for sym, df in all_data.items():
            if date not in df.index:
                continue
            idx = df.index.get_loc(date)
            if idx < 1:
                continue
            row = df.iloc[idx]
            prev = df.iloc[idx - 1]["Close"]
            sc = score_day(row, prev, ".KS" in sym or ".KQ" in sym)
            day_scores[sym] = {**sc, "close": row["Close"], "open": row["Open"],
                               "high": row["High"], "low": row["Low"]}

        # 1. 보유 포지션 청산 체크
        for sym in list(positions.keys()):
            if sym not in day_scores:
                continue
            pos = positions[sym]
            ds = day_scores[sym]
            pos["hold_days"] += 1

            low_pnl = (ds["low"] - pos["entry_price"]) / pos["entry_price"]
            high_pnl = (ds["high"] - pos["entry_price"]) / pos["entry_price"]

            exit_reason = None
            exit_price = ds["close"]

            if low_pnl <= pos["sl"]:
                exit_reason = "STOP_LOSS"
                exit_price = pos["entry_price"] * (1 + pos["sl"])
            elif high_pnl >= pos["tp"]:
                exit_reason = "TAKE_PROFIT"
                exit_price = pos["entry_price"] * (1 + pos["tp"])
            elif pos["hold_days"] >= max_hold:
                exit_reason = "MAX_HOLD"
                exit_price = ds["close"]

            if exit_reason:
                gross = (exit_price - pos["entry_price"]) / pos["entry_price"]
                net = gross - cost * 2
                pnl_amt = net * pos["entry_price"] * pos["qty"]
                cash += pos["entry_price"] * pos["qty"] + pnl_amt

                trades.append(PfTrade(
                    symbol=sym, entry_date=pos["entry_date"], exit_date=date_str,
                    entry_price=round(pos["entry_price"], 2), exit_price=round(exit_price, 2),
                    qty=pos["qty"], pnl_pct=round(net * 100, 2), pnl_amount=round(pnl_amt),
                    exit_reason=exit_reason, grade=pos["grade"], score=pos["score"],
                ))
                del positions[sym]

        # 2. 전일 시그널로 매수 실행 (T+1 시가)
        for sym in list(pending.keys()):
            if len(positions) >= max_positions:
                break
            if sym in positions or sym not in day_scores:
                pending.pop(sym, None)
                continue

            ds = day_scores[sym]
            p = pending.pop(sym)

            # 딥 바잉
            dip_target = ds["open"] * (1 - dip_pct)
            if ds["low"] > dip_target:
                continue  # 딥 없음 → 스킵

            entry_price = dip_target

            # ATR 동적 손익절 (사용자 설정 우선)
            sym_df = all_data.get(sym)
            if sym_df is not None:
                idx = sym_df.index.get_loc(date) if date in sym_df.index else -1
                atr = calc_atr(sym_df.iloc[:idx + 1]) if idx > 14 else 0
            else:
                atr = 0

            if atr > 0:
                sl = max(stop_loss, -(atr * atr_sl_mult / entry_price))
                tp = min(take_profit, atr * atr_tp_mult / entry_price)
            else:
                sl, tp = stop_loss, take_profit

            # 자본 배분: 현금을 빈 슬롯에 균등
            slots = max_positions - len(positions)
            per_slot = cash / slots if slots > 0 else 0
            if per_slot < entry_price:
                continue

            qty = max(1, int(per_slot / entry_price))
            invest = qty * entry_price
            cash -= invest

            positions[sym] = {
                "entry_price": entry_price, "qty": qty, "entry_date": date_str,
                "grade": p["grade"], "score": p["score"],
                "sl": sl, "tp": tp, "hold_days": 0,
            }

        # 3. 오늘 시그널 → 내일 매수 예약 (우선순위 정렬)
        candidates = []
        for sym, sc in day_scores.items():
            if sc["grade"] in entry_grades and sym not in positions and sym not in pending:
                # 다일 추세 필터
                sym_df = all_data.get(sym)
                if sym_df is not None:
                    idx = sym_df.index.get_loc(date) if date in sym_df.index else -1
                    if idx >= 5:
                        down = sum(1 for j in range(idx - 4, idx + 1) if sym_df.iloc[j]["Close"] < sym_df.iloc[j - 1]["Close"])
                        if down >= 4:
                            continue  # 5일 중 4일+ 하락 → 스킵

                candidates.append({"sym": sym, "grade": sc["grade"], "score": sc["score"]})

        # 우선순위: 등급 → 점수
        candidates.sort(key=lambda x: (-{"A": 3, "B": 2, "C": 1, "D": 0}.get(x["grade"], 0), -x["score"]))
        for c in candidates[:max_positions]:
            pending[c["sym"]] = {"grade": c["grade"], "score": c["score"], "date": date_str}

        # 일일 자산 계산
        pos_value = sum(
            day_scores[s]["close"] * p["qty"]
            for s, p in positions.items() if s in day_scores
        )
        total_equity = cash + pos_value
        daily_ret = (total_equity - capital) / capital * 100 if i > 0 else 0
        capital_prev = capital
        capital = total_equity

        peak_equity = max(peak_equity, total_equity)
        dd = (total_equity - peak_equity) / peak_equity * 100
        max_dd = min(max_dd, dd)

        position_counts.append(len(positions))
        daily_returns.append(round(daily_ret, 4))
        equity_curve.append({"date": date_str, "equity": round(total_equity), "positions": len(positions)})

    # 열린 포지션 청산
    for sym, pos in list(positions.items()):
        if sym in all_data:
            last = all_data[sym].iloc[-1]["Close"]
            gross = (last - pos["entry_price"]) / pos["entry_price"]
            net = gross - cost * 2
            pnl_amt = net * pos["entry_price"] * pos["qty"]
            trades.append(PfTrade(
                symbol=sym, entry_date=pos["entry_date"], exit_date="END",
                entry_price=round(pos["entry_price"], 2), exit_price=round(last, 2),
                qty=pos["qty"], pnl_pct=round(net * 100, 2), pnl_amount=round(pnl_amt),
                exit_reason="END", grade=pos["grade"], score=pos["score"],
            ))

    # 집계
    final = cash + sum(
        all_data[s].iloc[-1]["Close"] * p["qty"]
        for s, p in positions.items() if s in all_data
    )
    pnls = [t.pnl_pct for t in trades]
    wins = [p for p in pnls if p > 0]
    total_ret = (final - initial_capital) / initial_capital * 100

    # 종목별 집계
    by_sym = {}
    for t in trades:
        if t.symbol not in by_sym:
            by_sym[t.symbol] = {"trades": 0, "wins": 0, "total_pnl": 0}
        by_sym[t.symbol]["trades"] += 1
        if t.pnl_pct > 0:
            by_sym[t.symbol]["wins"] += 1
        by_sym[t.symbol]["total_pnl"] += t.pnl_pct

    return PfResult(
        period=period,
        symbols=symbols,
        initial_capital=initial_capital,
        final_capital=round(final),
        total_return_pct=round(total_ret, 2),
        buy_and_hold_pct=round(bh_return, 2),
        excess_pct=round(total_ret - bh_return, 2),
        total_trades=len(trades),
        winning=len(wins),
        losing=len(trades) - len(wins),
        win_rate=round(len(wins) / len(trades) * 100, 1) if trades else 0,
        sharpe=round(statistics.mean(pnls) / statistics.stdev(pnls), 2) if len(pnls) > 1 and statistics.stdev(pnls) > 0 else 0,
        max_drawdown_pct=round(max_dd, 2),
        avg_positions=round(sum(position_counts) / len(position_counts), 1) if position_counts else 0,
        daily_returns=daily_returns,
        trades=[asdict(t) for t in trades],
        by_symbol=by_sym,
        equity_curve=equity_curve[-60:],  # 최근 60일만
    )


def print_report(r: PfResult):
    print(f"\n{'='*65}")
    print(f"  포트폴리오 백테스트")
    print(f"  종목: {', '.join(r.symbols[:5])}{'...' if len(r.symbols)>5 else ''} ({len(r.symbols)}개)")
    print(f"  기간: {r.period} | 초기 자본: {r.initial_capital:,.0f}원")
    print(f"{'='*65}")

    print(f"\n  성과:")
    print(f"    최종 자본     {r.final_capital:,.0f}원")
    pc = 'positive' if r.total_return_pct >= 0 else 'negative'
    print(f"    전략 수익     {r.total_return_pct:+.2f}%")
    print(f"    Buy & Hold   {r.buy_and_hold_pct:+.2f}%")
    print(f"    초과 수익     {r.excess_pct:+.2f}%")
    print(f"    최대 드로다운 {r.max_drawdown_pct:.2f}%")

    print(f"\n  거래:")
    print(f"    총 {r.total_trades}건 | 승: {r.winning} | 패: {r.losing} | 승률: {r.win_rate}%")
    print(f"    샤프: {r.sharpe} | 평균 포지션: {r.avg_positions}개")

    if r.by_symbol:
        print(f"\n  종목별:")
        print(f"    {'종목':>8} {'거래':>4} {'승률':>6} {'누적PnL':>8}")
        for sym, d in sorted(r.by_symbol.items(), key=lambda x: -x[1]["total_pnl"]):
            wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            print(f"    {sym:>8} {d['trades']:>4} {wr:>5.1f}% {d['total_pnl']:>+7.2f}%")

    if r.trades:
        print(f"\n  최근 거래 (10건):")
        print(f"    {'종목':>6} {'진입':>12} {'청산':>12} {'등급':>3} {'진입가':>8} {'청산가':>8} {'수익률':>7} {'사유':>10}")
        for t in r.trades[-10:]:
            print(f"    {t['symbol']:>6} {t['entry_date']:>12} {t['exit_date']:>12} {t['grade']:>3} {t['entry_price']:>8.2f} {t['exit_price']:>8.2f} {t['pnl_pct']:>+6.2f}% {t['exit_reason']:>10}")

    print(f"\n{'='*65}\n")


def main():
    args = {}
    i = 1
    while i < len(sys.argv):
        if sys.argv[i].startswith("--"):
            key = sys.argv[i][2:]
            if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
                args[key] = sys.argv[i + 1]; i += 2
            else:
                args[key] = "true"; i += 1
        else:
            i += 1

    symbols = args.get("symbols", "TSLA,NVDA,AAPL,MSFT,GOOG,AMZN,META,NFLX,AMD,NFLX").split(",")
    symbols = [s.strip() for s in symbols]

    result = run_portfolio_backtest(
        symbols=symbols,
        period=args.get("period", "6mo"),
        initial_capital=float(args.get("capital", "1000000")),
        max_positions=int(args.get("max-positions", "2")),
        stop_loss=float(args.get("stop-loss", "-0.02")),
        take_profit=float(args.get("take-profit", "0.06")),
        max_hold=int(args.get("max-hold", "1")),
        entry_grades=args.get("grades", "A").split(","),
        cost=float(args.get("cost", "0.001")),
    )

    if args.get("output") == "json":
        d = asdict(result)
        d.pop("daily_returns", None)
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
