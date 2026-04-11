#!/usr/bin/env python3
"""
signal-engine.py
토스증권 단타 시그널 엔진 - 정량적 판단 로직

사용법:
  # 보유종목 손절/익절 체크
  python3 signal-engine.py check-positions --positions '<json>' --config '<json>'

  # 매수 시그널 평가
  python3 signal-engine.py evaluate-buy --quote '<json>' --portfolio '<json>' --config '<json>'

  # 리스크 게이트 체크
  python3 signal-engine.py risk-gate --portfolio '<json>' --config '<json>' --today-pnl <number>
"""

import json
import sys
from dataclasses import dataclass


# ─── 기본 설정 ───

DEFAULT_CONFIG = {
    "max_position_pct": 10,        # 1회 최대 투자: 총자산의 N%
    "stop_loss_pct": -3.0,         # 손절선: 진입가 대비 -N%
    "take_profit_pct": 7.0,        # 익절선: 진입가 대비 +N%
    "daily_loss_limit_pct": -2.0,  # 일일 최대 손실: 총자산의 -N%
    "max_positions": 2,            # 동시 보유 최대 종목 수
    "volume_spike_ratio": 1.5,     # 거래량 급증 기준: 평소 대비 N배
    "min_profit_rate_buy": -0.05,  # 매수 고려 최대 하락률 (과매도 진입)
    "momentum_threshold": 0.02,    # 모멘텀 기준: 일일 등락률 N% 이상
}


# ─── 손절/익절 체크 ───

def check_positions(positions: list, config: dict) -> list:
    """보유종목의 손절/익절 시그널을 체크합니다.

    Returns:
        list of dicts: [{"symbol", "action", "reason", "profit_rate", "urgency"}]
    """
    stop_loss = config.get("stop_loss_pct", DEFAULT_CONFIG["stop_loss_pct"]) / 100
    take_profit = config.get("take_profit_pct", DEFAULT_CONFIG["take_profit_pct"]) / 100

    signals = []
    for pos in positions:
        symbol = pos.get("symbol", pos.get("product_code", ""))
        profit_rate = pos.get("profit_rate", 0)
        daily_rate = pos.get("daily_profit_rate", 0)
        quantity = pos.get("quantity", 0)

        if quantity <= 0:
            continue

        signal = {
            "symbol": symbol,
            "name": pos.get("name", ""),
            "profit_rate": round(profit_rate * 100, 2),
            "daily_rate": round(daily_rate * 100, 2),
            "current_price": pos.get("current_price", 0),
            "average_price": pos.get("average_price", 0),
            "quantity": quantity,
        }

        # 손절 시그널
        if profit_rate <= stop_loss:
            signal["action"] = "SELL_STOP_LOSS"
            signal["reason"] = f"손절선 도달: {profit_rate*100:.1f}% (기준: {stop_loss*100:.1f}%)"
            signal["urgency"] = "HIGH"
            signals.append(signal)

        # 익절 시그널
        elif profit_rate >= take_profit:
            signal["action"] = "SELL_TAKE_PROFIT"
            signal["reason"] = f"익절선 도달: {profit_rate*100:.1f}% (기준: {take_profit*100:.1f}%)"
            signal["urgency"] = "MEDIUM"
            signals.append(signal)

        # 급락 경고 (일일 -5% 이상 하락)
        elif daily_rate <= -0.05:
            signal["action"] = "ALERT_SHARP_DROP"
            signal["reason"] = f"당일 급락: {daily_rate*100:.1f}%"
            signal["urgency"] = "HIGH"
            signals.append(signal)

        # 정상 범위
        else:
            signal["action"] = "HOLD"
            signal["reason"] = "정상 범위"
            signal["urgency"] = "NONE"
            signals.append(signal)

    return signals


# ─── 101 Formulaic Alphas (Kakushadze, 2016) ───

def compute_alphas(quote: dict) -> dict:
    """논문 '101 Formulaic Alphas'에서 tossctl 데이터로 계산 가능한 알파를 산출합니다.

    사용 가능 데이터: open, close(=last), high, low, volume, reference_price(전일종가)
    """
    import math

    o = quote.get("open", 0) or quote.get("reference_price", 0)  # 시가 (없으면 전일종가)
    c = quote.get("last", 0) or quote.get("close", 0)            # 현재가/종가
    h = quote.get("high", 0) or max(o, c)                        # 고가
    l = quote.get("low", 0) or min(o, c) if min(o, c) > 0 else c # 저가
    v = quote.get("volume", 0)                                    # 거래량
    ref = quote.get("reference_price", 0)                         # 전일종가

    alphas = {}

    # --- Alpha#101: 장중 방향 강도 (Intraday Direction Strength) ---
    # (close - open) / (high - low + 0.001)
    # +1 = 강한 양봉, -1 = 강한 음봉, 0 = 도지
    hl_range = h - l + 0.001
    alpha101 = (c - o) / hl_range if hl_range > 0 else 0
    alphas["alpha101"] = {
        "value": round(alpha101, 4),
        "name": "장중 방향 강도",
        "interpretation": "강한 양봉" if alpha101 > 0.5 else
                          "양봉" if alpha101 > 0.1 else
                          "도지(방향 불확실)" if alpha101 > -0.1 else
                          "음봉" if alpha101 > -0.5 else "강한 음봉"
    }

    # --- Alpha#33 변형: 시가-종가 괴리율 ---
    # 원본: rank(-1 * ((1 - (open / close))^1))
    # 단일종목 적용: (1 - open/close) → 양수면 상승, 음수면 하락
    if c > 0:
        alpha33 = 1 - (o / c)
    else:
        alpha33 = 0
    alphas["alpha33"] = {
        "value": round(alpha33, 4),
        "name": "시가-종가 괴리율",
        "interpretation": "장중 상승" if alpha33 > 0.01 else
                          "보합" if alpha33 > -0.01 else "장중 하락"
    }

    # --- Mean Reversion Alpha: 전일종가 대비 현재 괴리 ---
    # -ln(open / ref_close) → 양수면 갭다운(반등 기대), 음수면 갭업(되돌림 기대)
    if o > 0 and ref > 0:
        mean_rev = -math.log(o / ref)
    else:
        mean_rev = 0
    alphas["mean_reversion"] = {
        "value": round(mean_rev, 4),
        "name": "평균회귀 시그널",
        "interpretation": "갭다운 반등 기대" if mean_rev > 0.01 else
                          "중립" if mean_rev > -0.01 else "갭업 되돌림 기대"
    }

    # --- Momentum Alpha: 전일 캔들 방향성 ---
    # ln(ref_close / open_yesterday) → 전일 양봉이면 모멘텀 지속 기대
    # tossctl에서 전일 시가 없으므로, (ref_close vs current open)으로 근사
    if ref > 0 and o > 0:
        momentum = math.log(c / ref) if c > 0 else 0
    else:
        momentum = 0
    alphas["momentum"] = {
        "value": round(momentum, 4),
        "name": "모멘텀 시그널",
        "interpretation": "상승 모멘텀" if momentum > 0.01 else
                          "중립" if momentum > -0.01 else "하락 모멘텀"
    }

    # --- Alpha#54 변형: 장중 세력 방향 ---
    # 원본: (-1 * ((low - close) * (open^5))) / ((low - high) * (close^5))
    # 단순화: (close - low) / (high - low) → 0=저가 마감, 1=고가 마감
    if hl_range > 0.001:
        alpha54 = (c - l) / (h - l) if (h - l) > 0 else 0.5
    else:
        alpha54 = 0.5
    alphas["alpha54"] = {
        "value": round(alpha54, 4),
        "name": "장중 가격 위치",
        "interpretation": "고가권 마감" if alpha54 > 0.7 else
                          "중간" if alpha54 > 0.3 else "저가권 마감"
    }

    # --- 종합 알파 점수 (0-30점) ---
    # 각 알파를 정규화하여 합산
    score = 0

    # alpha101: -1~+1 → 0~10점 (양봉일수록 높음)
    score += max(0, min(10, (alpha101 + 1) * 5))

    # mean_reversion: 갭다운(양수)이면 가점 → 0~10점
    mr_score = max(0, min(10, (mean_rev * 100) + 5))
    score += mr_score

    # alpha54: 0~1 → 0~10점 (고가권 마감일수록 높음)
    score += alpha54 * 10

    alphas["composite_score"] = round(score, 1)
    alphas["composite_max"] = 30

    return alphas


# ─── 매수 시그널 평가 ───

def evaluate_buy(quote: dict, portfolio: dict, config: dict) -> dict:
    """종목의 매수 적합성을 점수(0-130)로 평가합니다.

    평가 항목:
    - 거래량 스파이크 (0-30점)
    - 일일 모멘텀 (0-25점)
    - 가격 위치 (0-25점)
    - 포트폴리오 적합성 (0-20점)
    - 101 Alphas 점수 (0-30점) ← NEW

    Returns:
        dict: {"symbol", "score", "grade", "breakdown", "recommendation", "alphas"}
    """
    volume_spike_ratio = config.get("volume_spike_ratio", DEFAULT_CONFIG["volume_spike_ratio"])
    momentum_threshold = config.get("momentum_threshold", DEFAULT_CONFIG["momentum_threshold"])

    symbol = quote.get("symbol", "")
    change_rate = quote.get("change_rate", 0)
    volume = quote.get("volume", 0)
    last_price = quote.get("last", 0)
    ref_price = quote.get("reference_price", 0)

    # 시장 판별: 한국주식 vs 미국주식
    # - market_code: KSP(코스피), KSQ(코스닥) → 한국 / NSQ, NYS 등 → 미국
    # - symbol이 숫자로만 구성 또는 A로 시작하는 6-7자리 → 한국
    market_code = quote.get("market_code", "")
    is_kr = (
        market_code in ("KSP", "KSQ") or
        symbol.replace("A", "").isdigit() or
        (symbol.isdigit() and len(symbol) == 6)
    )
    market_label = "KR" if is_kr else "US"

    breakdown = {}
    total_score = 0

    # 1. 거래량 점수 (0-30) — 시장별 기준 분리
    #
    # 미국: 대형주 중심, 일 거래량 수천만~수억주
    #   50M+→30  20M+→25  10M+→20  5M+→15  1M+→10
    #
    # 한국: 중소형주도 단타 대상, 거래량 자체가 적음
    #   - 코스피 대형주: 일 500만주면 활발
    #   - 코스닥 중소형: 일 100만주면 활발, 300만주면 폭발적
    #   5M+→30  3M+→25  1M+→20  500K+→15  100K+→10
    if is_kr:
        if volume >= 5_000_000:
            vol_score = 30
        elif volume >= 3_000_000:
            vol_score = 25
        elif volume >= 1_000_000:
            vol_score = 20
        elif volume >= 500_000:
            vol_score = 15
        elif volume >= 100_000:
            vol_score = 10
        else:
            vol_score = 5
    else:
        if volume >= 50_000_000:
            vol_score = 30
        elif volume >= 20_000_000:
            vol_score = 25
        elif volume >= 10_000_000:
            vol_score = 20
        elif volume >= 5_000_000:
            vol_score = 15
        elif volume >= 1_000_000:
            vol_score = 10
        else:
            vol_score = 5
    breakdown["volume"] = {"score": vol_score, "max": 30, "value": volume, "market": market_label}
    total_score += vol_score

    # 2. 모멘텀 점수 (0-25) — 시장별 기준 분리
    #
    # 미국: 일일 가격제한 없음. 10%+ 상승은 과열.
    #   +1~5%→25  +5~10%→15  +10%+→5(과열)
    #
    # 한국: 가격제한 ±30%. 테마주는 10-15% 상승이 흔함.
    #   10%+ 상승도 정상적인 모멘텀일 수 있음.
    #   +1~8%→25  +8~15%→20  +15~25%→15  +25%+→5(상한가 근접 과열)
    if is_kr:
        if 0.01 <= change_rate <= 0.08:
            mom_score = 25  # 적당한 상승
        elif 0.08 < change_rate <= 0.15:
            mom_score = 20  # 강한 상승 (한국에선 정상 범위)
        elif 0.15 < change_rate <= 0.25:
            mom_score = 15  # 급등 (테마주 가능성)
        elif change_rate > 0.25:
            mom_score = 5   # 상한가 근접 (과열)
        elif -0.03 <= change_rate < 0.01:
            mom_score = 15  # 보합/소폭 하락
        elif -0.08 <= change_rate < -0.03:
            mom_score = 10  # 하락 중
        else:
            mom_score = 0   # 급락 (-8% 이하)
    else:
        if 0.01 <= change_rate <= 0.05:
            mom_score = 25
        elif 0.05 < change_rate <= 0.10:
            mom_score = 15
        elif change_rate > 0.10:
            mom_score = 5
        elif -0.02 <= change_rate < 0.01:
            mom_score = 15
        elif -0.05 <= change_rate < -0.02:
            mom_score = 10
        else:
            mom_score = 0
    breakdown["momentum"] = {"score": mom_score, "max": 25, "value": round(change_rate * 100, 2), "market": market_label}
    total_score += mom_score

    # 3. 가격 위치 점수 (0-25) — 시장별 기준 분리
    #
    # 미국: 변동폭 작음. 전일比 ±2%가 보합, 5%+가 큰 움직임
    # 한국: 변동폭 큼. 전일比 ±3%가 보합, 10%+가 큰 움직임
    #   한국에서 +8% 진입은 미국의 +3% 진입과 비슷한 위험도
    if ref_price > 0:
        price_vs_ref = (last_price - ref_price) / ref_price
        if is_kr:
            if -0.03 <= price_vs_ref <= 0.03:
                price_score = 20  # 전일 근처 (안정적)
            elif 0.03 < price_vs_ref <= 0.08:
                price_score = 15  # 소폭 위
            elif 0.08 < price_vs_ref <= 0.15:
                price_score = 10  # 위 (추격 주의)
            elif price_vs_ref > 0.15:
                price_score = 5   # 크게 위 (추격 매수 위험)
            elif -0.08 <= price_vs_ref < -0.03:
                price_score = 25  # 소폭 아래 (매수 기회)
            else:
                price_score = 10  # 크게 아래 (추가 하락 위험)
        else:
            if -0.02 <= price_vs_ref <= 0.02:
                price_score = 20
            elif 0.02 < price_vs_ref <= 0.05:
                price_score = 15
            elif price_vs_ref > 0.05:
                price_score = 5
            elif -0.05 <= price_vs_ref < -0.02:
                price_score = 25
            else:
                price_score = 10
    else:
        price_score = 10
    breakdown["price_position"] = {"score": price_score, "max": 25, "value": round(last_price, 2), "market": market_label}
    total_score += price_score

    # 4. 포트폴리오 적합성 (0-20)
    total_asset = portfolio.get("total_asset_amount", 0)
    orderable = portfolio.get("orderable_amount_krw", 0)
    max_position_pct = config.get("max_position_pct", DEFAULT_CONFIG["max_position_pct"])

    if total_asset > 0:
        max_invest = total_asset * max_position_pct / 100
        if orderable >= max_invest:
            port_score = 20  # 여력 충분
        elif orderable >= max_invest * 0.5:
            port_score = 10  # 여력 부족
        else:
            port_score = 0   # 여력 없음
    else:
        port_score = 0
    breakdown["portfolio_fit"] = {"score": port_score, "max": 20, "value": orderable}
    total_score += port_score

    # 5. 101 Alphas 점수 (0-30) — Kakushadze (2016)
    alphas = compute_alphas(quote)
    alpha_score = alphas["composite_score"]
    breakdown["alphas"] = {"score": round(alpha_score), "max": 30, "detail": alphas}
    total_score += alpha_score

    # 등급 (총 130점 만점 기준)
    pct = total_score / 130 * 100  # 백분율로 정규화
    if pct >= 70:
        grade = "A"
        recommendation = "STRONG_BUY"
    elif pct >= 55:
        grade = "B"
        recommendation = "BUY"
    elif pct >= 40:
        grade = "C"
        recommendation = "WATCH"
    else:
        grade = "D"
        recommendation = "SKIP"

    return {
        "symbol": symbol,
        "name": quote.get("name", ""),
        "score": round(total_score, 1),
        "max_score": 130,
        "score_pct": round(pct, 1),
        "grade": grade,
        "recommendation": recommendation,
        "breakdown": breakdown,
        "alphas": {k: v for k, v in alphas.items() if k not in ("composite_score", "composite_max")},
        "current_price": last_price,
        "change_rate": round(change_rate * 100, 2),
    }


# ─── 리스크 게이트 ───

def risk_gate(portfolio: dict, config: dict, today_pnl: float = 0, active_positions: int = 0) -> dict:
    """리스크 게이트를 통과하는지 체크합니다.

    Returns:
        dict: {"passed", "checks", "blocked_reason"}
    """
    total_asset = portfolio.get("total_asset_amount", 0)
    orderable = portfolio.get("orderable_amount_krw", 0)
    max_positions = config.get("max_positions", DEFAULT_CONFIG["max_positions"])
    daily_loss_limit = config.get("daily_loss_limit_pct", DEFAULT_CONFIG["daily_loss_limit_pct"])
    max_position_pct = config.get("max_position_pct", DEFAULT_CONFIG["max_position_pct"])

    checks = []
    all_passed = True
    blocked_reason = None

    # 1. 일일 손실 한도
    if total_asset > 0:
        daily_loss_rate = (today_pnl / total_asset) * 100
    else:
        daily_loss_rate = 0

    daily_ok = daily_loss_rate > daily_loss_limit
    checks.append({
        "name": "daily_loss_limit",
        "passed": daily_ok,
        "value": round(daily_loss_rate, 2),
        "limit": daily_loss_limit,
        "description": f"일일 손실: {daily_loss_rate:.2f}% (한도: {daily_loss_limit}%)"
    })
    if not daily_ok:
        all_passed = False
        blocked_reason = f"일일 손실 한도 초과: {daily_loss_rate:.2f}% (한도: {daily_loss_limit}%)"

    # 2. 동시 포지션 한도
    pos_ok = active_positions < max_positions
    checks.append({
        "name": "max_positions",
        "passed": pos_ok,
        "value": active_positions,
        "limit": max_positions,
        "description": f"현재 포지션: {active_positions}개 (한도: {max_positions}개)"
    })
    if not pos_ok:
        all_passed = False
        blocked_reason = blocked_reason or f"동시 포지션 한도: {active_positions}/{max_positions}"

    # 3. 투자 여력
    min_invest = total_asset * max_position_pct / 100 * 0.3  # 최소 30%는 있어야
    fund_ok = orderable >= min_invest
    checks.append({
        "name": "available_funds",
        "passed": fund_ok,
        "value": orderable,
        "limit": round(min_invest),
        "description": f"주문 가능: {orderable:,.0f}원 (최소: {min_invest:,.0f}원)"
    })
    if not fund_ok:
        all_passed = False
        blocked_reason = blocked_reason or f"투자 여력 부족: {orderable:,.0f}원"

    return {
        "passed": all_passed,
        "checks": checks,
        "blocked_reason": blocked_reason,
    }


# ─── CLI ───

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "command required: check-positions | evaluate-buy | risk-gate | compute-alphas"}))
        sys.exit(1)

    command = sys.argv[1]

    # 인자 파싱
    args = {}
    i = 2
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

    config = json.loads(args.get("config", "{}"))
    config = {**DEFAULT_CONFIG, **config}

    if command == "check-positions":
        positions = json.loads(args.get("positions", "[]"))
        result = check_positions(positions, config)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif command == "evaluate-buy":
        quote = json.loads(args.get("quote", "{}"))
        portfolio = json.loads(args.get("portfolio", "{}"))
        result = evaluate_buy(quote, portfolio, config)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif command == "risk-gate":
        portfolio = json.loads(args.get("portfolio", "{}"))
        today_pnl = float(args.get("today-pnl", "0"))
        active_positions = int(args.get("active-positions", "0"))
        result = risk_gate(portfolio, config, today_pnl, active_positions)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif command == "compute-alphas":
        quote = json.loads(args.get("quote", "{}"))
        result = compute_alphas(quote)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        print(json.dumps({"error": f"unknown command: {command}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
