import math
from datetime import datetime, timedelta

WAIT_THRESHOLD_RISK = 60
DEFAULT_MONTHLY_DECAY_K = 0.018


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def _parse_price_date(value: str) -> datetime:
    """2026-08 또는 2026-08-14 형식을 모두 지원."""
    value = str(value).strip()

    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass

    raise ValueError(f"지원하지 않는 날짜 형식: {value}")


def _calculate_base_decay(price_history: list[dict]) -> tuple[int | None, float | None]:
    """
    실제 가격 이력에서 지수 감가 계수 k 계산.
    P(t) = P0 * exp(-k * t)
    """
    if not price_history:
        return None, None

    valid = []

    for item in price_history:
        try:
            date = _parse_price_date(item["date"])
            price = int(item["price"])

            if price > 0:
                valid.append({"date": date, "price": price})
        except (ValueError, TypeError, KeyError):
            continue

    if not valid:
        return None, None

    valid.sort(key=lambda x: x["date"])
    current_price = valid[-1]["price"]

    cutoff = valid[-1]["date"] - timedelta(days=90)
    recent = [x for x in valid if x["date"] >= cutoff]

    if len(recent) < 2:
        return current_price, DEFAULT_MONTHLY_DECAY_K

    start = recent[0]
    end = recent[-1]

    days = max((end["date"] - start["date"]).days, 1)
    months = max(days / 30.0, 1.0)

    # 최근 가격이 상승 또는 유지된 경우 최소 감가율 사용
    if end["price"] >= start["price"]:
        return current_price, 0.005

    price_ratio = end["price"] / start["price"]
    k = -math.log(price_ratio) / months

    return current_price, clamp(k, 0.005, 0.08)


def _calculate_news_score(news_analysis: dict | None) -> tuple[float, dict]:
    """
    뉴스가 방향 결정.
    BUY = 음수
    WAIT = 양수
    """
    if not news_analysis:
        return 0.0, {
            "purchase_signal": "NEUTRAL",
            "signal_strength": 0,
            "market_impact_score": 50,
            "summary": "최근 뉴스 분석 데이터가 없습니다.",
            "key_signals": []
        }

    signal = news_analysis.get("purchase_signal", "NEUTRAL")

    try:
        strength = clamp(float(news_analysis.get("signal_strength", 0)), 0, 100)
    except (TypeError, ValueError):
        strength = 0

    if signal == "WAIT":
        score = strength
    elif signal == "BUY":
        score = -strength
    else:
        score = 0.0

    return score, {
        "purchase_signal": signal,
        "signal_strength": int(strength),
        "market_impact_score": news_analysis.get("market_impact_score", 50),
        "summary": news_analysis.get("summary", ""),
        "key_signals": news_analysis.get("key_signals", [])
    }


def _apply_shopping_confirmation(
    news_score: float,
    shopping_analysis: dict | None
) -> tuple[float, dict]:
    """
    쇼핑 트렌드는 BUY/WAIT 방향을 만들지 않는다.
    뉴스 신호의 강도만 강화하거나 약화한다.
    """
    if not shopping_analysis:
        return news_score, {
            "trend_direction": "UNKNOWN",
            "momentum_pct": None,
            "spike_detected": False,
            "spike_pct": None,
            "trend_strength": 0,
            "modifier": 1.0,
            "trend_summary": "최근 쇼핑 트렌드 분석 데이터가 없습니다."
        }

    direction = shopping_analysis.get("trend_direction", "UNKNOWN")
    spike = bool(shopping_analysis.get("spike_detected", False))

    if direction == "UP" and spike:
        modifier = 1.30
    elif direction == "UP":
        modifier = 1.15
    elif direction == "DOWN":
        modifier = 0.80
    else:
        modifier = 1.00

    adjusted_score = clamp(news_score * modifier, -100, 100)

    return adjusted_score, {
        "trend_direction": direction,
        "momentum_pct": shopping_analysis.get("momentum_pct"),
        "spike_detected": spike,
        "spike_pct": shopping_analysis.get("spike_pct"),
        "trend_strength": shopping_analysis.get("trend_strength", 0),
        "modifier": modifier,
        "trend_summary": shopping_analysis.get("trend_summary", "")
    }


def _build_no_price_result(
    raw_news_score: float,
    market_score: float,
    news_detail: dict,
    shopping_detail: dict
) -> dict:
    """
    가격 이력이 없으면 임의 가격을 생성하지 않는다.
    뉴스 + 쇼핑만 사용한다.

    충분히 강한 BUY 근거가 있을 때만 BUY_NOW.
    중립/근거 부족 상태는 WAIT.
    """
    timing_score = clamp(market_score, -100, 100)
    regret_risk_score = clamp(int(round((timing_score + 100) / 2)), 0, 100)

    decision = "BUY_NOW" if timing_score <= -20 else "WAIT"

    return {
        "current_price": None,
        "decision": decision,
        "timing_score": round(timing_score, 1),
        "regret_risk_score": regret_risk_score,
        "target_buy_price": None,
        "drop_3m_pct": None,
        "drop_3m_amount": None,
        "base_monthly_decay_pct": None,
        "adjusted_monthly_decay_pct": None,
        "base_decay_k": None,
        "adjusted_decay_k": None,
        "predictions": None,
        "price_prediction_available": False,
        "price_function": None,
        "news_signal": news_detail,
        "shopping_signal": shopping_detail,
        "score_detail": {
            "raw_news_score": round(raw_news_score, 1),
            "market_score_after_trend": round(market_score, 1),
            "price_wait_score": None,
            "final_timing_score": round(timing_score, 1)
        }
    }


def calculate_multi_factor_prediction(
    product_id: str,
    price_history: list[dict],
    news_analysis: dict | None,
    shopping_analysis: dict | None
) -> dict:

    # 1. 뉴스에서 기본 BUY/WAIT 방향 추출
    raw_news_score, news_detail = _calculate_news_score(news_analysis)

    # 2. 쇼핑 관심도로 뉴스 신호 강화/약화
    market_score, shopping_detail = _apply_shopping_confirmation(
        raw_news_score,
        shopping_analysis
    )

    # 3. 가격 이력 없음 → 뉴스 + 쇼핑으로만 판단
    if not price_history:
        return _build_no_price_result(
            raw_news_score,
            market_score,
            news_detail,
            shopping_detail
        )

    # 4. 가격 이력으로 현재가 + 감가 계수 계산
    current_price, k_base = _calculate_base_decay(price_history)

    if current_price is None or k_base is None:
        return _build_no_price_result(
            raw_news_score,
            market_score,
            news_detail,
            shopping_detail
        )

    # 5. 시장 신호를 감가계수에 약하게 반영
    # WAIT 쪽일수록 미래 가격 하락폭을 조금 증가
    # BUY 쪽일수록 미래 가격 하락폭을 조금 감소
    market_decay_adjustment = (market_score / 100.0) * 0.006
    k_adj = clamp(k_base + market_decay_adjustment, 0.003, 0.10)

    # 6. 지수 감가 모델로 1/3/6개월 가격 예측
    p_1m = int(round(current_price * math.exp(-k_adj * 1), -3))
    p_3m = int(round(current_price * math.exp(-k_adj * 3), -3))
    p_6m = int(round(current_price * math.exp(-k_adj * 6), -3))

    drop_1m_pct = round((current_price - p_1m) / current_price * 100, 1)
    drop_3m_pct = round((current_price - p_3m) / current_price * 100, 1)
    drop_6m_pct = round((current_price - p_6m) / current_price * 100, 1)
    drop_3m_amount = current_price - p_3m

    # 7. 가격 측 WAIT 위험
    # 3개월 2% 이하 = 0점
    # 3개월 8% 이상 = 100점
    price_wait_score = clamp((drop_3m_pct - 2.0) / 6.0 * 100, 0, 100)

    # 8. 최종 점수
    # 뉴스+쇼핑 70%, 가격 하락 전망 30%
    timing_score = clamp(
        market_score * 0.70 + price_wait_score * 0.30,
        -100,
        100
    )

    regret_risk_score = clamp(
        int(round((timing_score + 100) / 2)),
        0,
        100
    )

    # 후회 위험 ≥ 60 또는 3개월 하락률 ≥ 6% → WAIT
    decision = (
        "WAIT"
        if regret_risk_score >= WAIT_THRESHOLD_RISK or drop_3m_pct >= 6.0
        else "BUY_NOW"
    )

    target_buy_price = p_3m if decision == "WAIT" else current_price

    base_monthly_decay_pct = (1 - math.exp(-k_base)) * 100
    adjusted_monthly_decay_pct = (1 - math.exp(-k_adj)) * 100

    return {
        "current_price": current_price,
        "decision": decision,
        "timing_score": round(timing_score, 1),
        "regret_risk_score": regret_risk_score,
        "target_buy_price": target_buy_price,
        "drop_3m_pct": drop_3m_pct,
        "drop_3m_amount": drop_3m_amount,
        "base_monthly_decay_pct": round(base_monthly_decay_pct, 2),
        "adjusted_monthly_decay_pct": round(adjusted_monthly_decay_pct, 2),
        "base_decay_k": round(k_base, 6),
        "adjusted_decay_k": round(k_adj, 6),
        "price_prediction_available": True,

        "price_function": {
            "type": "EXPONENTIAL_DECAY",
            "formula": "P(t) = P0 * exp(-k * t)",
            "p0": current_price,
            "k": round(k_adj, 6),
            "time_unit": "month"
        },

        "predictions": {
            "1m": {"price": p_1m, "drop_pct": drop_1m_pct},
            "3m": {"price": p_3m, "drop_pct": drop_3m_pct},
            "6m": {"price": p_6m, "drop_pct": drop_6m_pct}
        },

        "news_signal": news_detail,
        "shopping_signal": shopping_detail,

        "score_detail": {
            "raw_news_score": round(raw_news_score, 1),
            "market_score_after_trend": round(market_score, 1),
            "price_wait_score": round(price_wait_score, 1),
            "final_timing_score": round(timing_score, 1)
        }
    }
