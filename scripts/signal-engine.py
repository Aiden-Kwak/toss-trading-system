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
    "max_position_pct": 10,        # 1회 최대 투자: 주문가능금액의 N%
    "stop_loss_pct": -3.0,         # 손절선: 진입가 대비 -N%
    "take_profit_pct": 7.0,        # 익절선: 진입가 대비 +N%
    "daily_loss_limit_pct": -2.0,  # 일일 최대 손실: 운용금 대비 -N%
    "max_positions": 2,            # 동시 보유 최대 종목 수
    "volume_spike_ratio": 1.5,     # 거래량 급증 기준: 평소 대비 N배
    "min_profit_rate_buy": -0.05,  # 매수 고려 최대 하락률 (과매도 진입)
    "momentum_threshold": 0.02,    # 모멘텀 기준: 일일 등락률 N% 이상
    "trailing_stop_trigger": 3.0,  # 트레일링 스톱 시작: 수익률 +N%
    "trailing_stop_distance": 2.0, # 트레일링 거리: 고점 대비 -N%
}


# ─── 시장 상황 판단 ───

def detect_market_regime(quote: dict) -> str:
    """단일 종목의 가격 동향으로 시장 상황을 근사 판단합니다.
    (정밀 판단은 SPY/KOSPI 지수 데이터 필요)

    Returns: "bull", "bear", "range", "crisis"
    """
    change_rate = quote.get("change_rate", 0)
    # 급변 (±5% 이상)
    if abs(change_rate) >= 0.05:
        return "crisis"
    # 상승
    if change_rate >= 0.01:
        return "bull"
    # 하락
    if change_rate <= -0.01:
        return "bear"
    return "range"


# ─── Kelly Criterion ───

def calculate_kelly(win_rate: float, avg_win: float, avg_loss: float,
                    fraction: float = 0.5) -> float:
    """Kelly Criterion으로 최적 포지션 비율 계산

    Args:
        win_rate: 승률 (0~1)
        avg_win: 평균 수익률 (양수, 예: 5.0)
        avg_loss: 평균 손실률 (양수, 예: 3.0)
        fraction: Kelly 비율 (0.5 = Half Kelly 권장)

    Returns:
        최적 투자 비율 (%)
    """
    if avg_loss <= 0 or win_rate <= 0:
        return 5.0  # 데이터 부족 시 보수적 기본값

    b = avg_win / avg_loss  # 수익/손실 비율
    p = win_rate
    q = 1 - p

    kelly = (b * p - q) / b
    if kelly <= 0:
        return 2.0  # 음수 Kelly = 전략 자체가 손해 → 최소값

    result = kelly * fraction * 100  # Half Kelly → 퍼센트
    return round(max(2.0, min(result, 25.0)), 1)  # 2~25% 범위 제한


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

        # 트레일링 스톱 계산
        trailing_trigger = config.get("trailing_stop_trigger", DEFAULT_CONFIG["trailing_stop_trigger"]) / 100
        trailing_dist = config.get("trailing_stop_distance", DEFAULT_CONFIG["trailing_stop_distance"]) / 100

        # 손절 시그널
        if profit_rate <= stop_loss:
            signal["action"] = "SELL_STOP_LOSS"
            signal["reason"] = f"손절선 도달: {profit_rate*100:.1f}% (기준: {stop_loss*100:.1f}%)"
            signal["urgency"] = "HIGH"
            signals.append(signal)

        # 트레일링 스톱 (근사): 수익 trigger 이상 + 당일 distance 이상 하락
        # 참고: 데몬/시뮬레이터는 고점 대비 하락으로 더 정밀하게 체크
        elif profit_rate >= trailing_trigger and daily_rate <= -trailing_dist:
            signal["action"] = "SELL_TRAILING_STOP"
            signal["reason"] = f"트레일링 스톱: 수익 {profit_rate*100:.1f}%에서 당일 -{abs(daily_rate)*100:.1f}% 하락"
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
    """종목의 매수 적합성을 점수(0-100)로 평가합니다.

    v2 재설계: "오늘 사야 하는가"를 판단하는 타이밍 시그널 중심.
    기존 문제: 대형주가 항상 A등급 → 매일 매수 → 단타가 아님.

    평가 항목:
    - 방향성 시그널 (0-30점): 알파들이 같은 방향을 가리키는가
    - 모멘텀 품질 (0-25점): 단순 등락이 아닌 방향의 질
    - 가격 액션 (0-20점): 캔들 형태, 위치
    - 시장 컨텍스트 (0-25점): 시장 상황 + 추세 필터

    여력은 점수가 아닌 gate check (리스크 게이트에서 처리)
    """
    symbol = quote.get("symbol", "")
    change_rate = quote.get("change_rate", 0)
    volume = quote.get("volume", 0)
    last_price = quote.get("last", 0)
    ref_price = quote.get("reference_price", 0)

    market_code = quote.get("market_code", "")
    is_kr = (
        market_code in ("KSP", "KSQ") or
        symbol.replace("A", "").isdigit() or
        (symbol.isdigit() and len(symbol) == 6)
    )
    market_label = "KR" if is_kr else "US"

    # 알파 계산
    alphas = compute_alphas(quote)
    a101 = alphas.get("alpha101", {}).get("value", 0) if isinstance(alphas.get("alpha101"), dict) else 0
    a33 = alphas.get("alpha33", {}).get("value", 0) if isinstance(alphas.get("alpha33"), dict) else 0
    a54 = alphas.get("alpha54", {}).get("value", 0) if isinstance(alphas.get("alpha54"), dict) else 0
    momentum_val = alphas.get("momentum", {}).get("value", 0) if isinstance(alphas.get("momentum"), dict) else 0
    mean_rev_val = alphas.get("mean_reversion", {}).get("value", 0) if isinstance(alphas.get("mean_reversion"), dict) else 0

    breakdown = {}
    total_score = 0

    # ─── 1. 방향성 시그널 (0-30점) ───
    # 핵심: 여러 알파가 동시에 "매수"를 가리킬 때만 높은 점수
    # 알파 동의 카운트: 양수 알파가 몇 개인지
    bullish_count = sum(1 for v in [a101, a33, a54 - 0.5, momentum_val * 10] if v > 0.05)
    bearish_count = sum(1 for v in [a101, a33, a54 - 0.5, momentum_val * 10] if v < -0.05)

    if bullish_count >= 4:
        direction_score = 30  # 모든 알파 동의 → 강한 매수
    elif bullish_count >= 3:
        direction_score = 22  # 3개 동의
    elif bullish_count >= 2:
        direction_score = 12  # 2개 동의 (약한 시그널)
    elif bearish_count >= 3:
        direction_score = 0   # 대부분 매도 시그널 → 진입 금지
    else:
        direction_score = 5   # 혼재 → 불확실
    breakdown["direction"] = {"score": direction_score, "max": 30, "bullish": bullish_count, "bearish": bearish_count}
    total_score += direction_score

    # ─── 2. 모멘텀 품질 (0-25점) ───
    # 단순 등락률이 아닌: 방향 + 강도 + 거래량 동반 여부
    abs_change = abs(change_rate)

    if is_kr:
        sweet_spot = 0.02 <= abs_change <= 0.08  # 한국: 2-8%가 적정
        too_hot = abs_change > 0.15
    else:
        sweet_spot = 0.015 <= abs_change <= 0.05  # 미국: 1.5-5%가 적정
        too_hot = abs_change > 0.10

    if change_rate > 0 and sweet_spot:
        mom_quality = 25  # 적정 상승
    elif change_rate > 0 and not too_hot:
        mom_quality = 15  # 상승이지만 약하거나 과열 직전
    elif too_hot:
        mom_quality = 0   # 과열 (고점 추격 위험)
    elif abs_change < 0.005:
        mom_quality = 0   # 보합 (방향 없음 → 진입 이유 없음)
    elif change_rate < 0 and abs_change < 0.03:
        mom_quality = 5   # 소폭 하락 (평균회귀 기회 가능)
    else:
        mom_quality = 0   # 하락
    breakdown["momentum"] = {"score": mom_quality, "max": 25, "change_rate": round(change_rate * 100, 2), "market": market_label}
    total_score += mom_quality

    # ─── 3. 가격 액션 (0-20점) ───
    # Alpha#101 (방향 강도) + Alpha#54 (가격 위치) 조합
    # 강한 양봉(a101>0.5) + 고가 마감(a54>0.7) = 최고 점수
    if a101 > 0.5 and a54 > 0.7:
        action_score = 20  # 강한 양봉 + 고가 마감
    elif a101 > 0.3 and a54 > 0.5:
        action_score = 15  # 양봉 + 중상위 마감
    elif a101 > 0 and a54 > 0.4:
        action_score = 10  # 약한 양봉
    elif a101 < -0.3:
        action_score = 0   # 음봉 → 진입 금지
    else:
        action_score = 5   # 도지/불확실
    breakdown["price_action"] = {"score": action_score, "max": 20, "alpha101": round(a101, 3), "alpha54": round(a54, 3)}
    total_score += action_score

    # ─── 4. 시장 컨텍스트 (0-25점) ───
    regime = detect_market_regime(quote)
    trend_warning = None

    # 시장 상황
    if regime == "bull":
        context_score = 20
    elif regime == "range":
        context_score = 10  # 보합 → 중립 (기존엔 모두 여기에 빠져서 높았음)
    elif regime == "bear":
        context_score = 0   # 하락 → 진입 금지
    else:  # crisis
        context_score = 0

    # 추세 필터: 하락추세 내 반등 매수 방지
    if momentum_val < -0.02 and mean_rev_val > 0.01:
        trend_warning = "하락추세 내 반등 매수 주의 (falling knife)"
        context_score = max(0, context_score - 10)

    breakdown["context"] = {"score": context_score, "max": 25, "regime": regime, "trend_warning": trend_warning}
    total_score += context_score

    # ─── 등급 산정 (100점 만점) ───
    total_score = max(0, total_score)
    pct = total_score / 100 * 100  # 이미 100점 만점이라 pct = total

    if pct >= 75:
        grade = "A"
        recommendation = "STRONG_BUY"
    elif pct >= 55:
        grade = "B"
        recommendation = "BUY"
    elif pct >= 35:
        grade = "C"
        recommendation = "WATCH"
    else:
        grade = "D"
        recommendation = "SKIP"

    return {
        "symbol": symbol,
        "name": quote.get("name", ""),
        "score": round(total_score, 1),
        "max_score": 100,
        "score_pct": round(pct, 1),
        "grade": grade,
        "recommendation": recommendation,
        "regime": regime,
        "trend_warning": trend_warning,
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

    # 1. 일일 손실 한도 (주문가능금액 + 데몬 투입금 기준, 보호종목 제외)
    # 분모: 주문가능금액 기준으로 데몬이 운용하는 규모만 반영
    daemon_capital = orderable + abs(today_pnl)  # 현재 여력 + 오늘 실현 손익
    if daemon_capital > 0:
        daily_loss_rate = (today_pnl / daemon_capital) * 100
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

    elif command == "kelly":
        win_rate = float(args.get("win-rate", "0.5"))
        avg_win = float(args.get("avg-win", "5.0"))
        avg_loss = float(args.get("avg-loss", "3.0"))
        fraction = float(args.get("fraction", "0.5"))
        result = calculate_kelly(win_rate, avg_win, avg_loss, fraction)
        print(json.dumps({
            "kelly_full_pct": round(result / fraction, 1) if fraction > 0 else 0,
            "kelly_fraction_pct": result,
            "fraction": fraction,
            "inputs": {"win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss},
            "recommendation": f"max_position_pct를 {result}%로 설정 권장"
        }, ensure_ascii=False, indent=2))

    elif command == "regime":
        quote = json.loads(args.get("quote", "{}"))
        regime = detect_market_regime(quote)
        print(json.dumps({"regime": regime, "change_rate": quote.get("change_rate", 0)}, ensure_ascii=False))

    else:
        print(json.dumps({"error": f"unknown command: {command}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
