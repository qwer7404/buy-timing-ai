import json
import os
import boto3

# 💡 normalize_product_id 함수 import 추가
from get_decision import get_purchase_decision, normalize_product_id

bedrock = boto3.client("bedrock-runtime", region_name="ap-northeast-2")
s3 = boto3.client("s3", region_name="ap-northeast-2")

BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "anthropic.claude-3-haiku-20240307-v1:0"
)

BUCKET_NAME = os.environ.get(
    "S3_BUCKET_NAME",
    "market-project-data-team6"
)


# =========================================================
# 공통
# =========================================================

def _cors_headers():
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Amz-Date, X-Api-Key, X-Amz-Security-Token, *"
    }


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}") + 1

    if start == -1 or end <= start:
        raise ValueError("Bedrock 응답에서 JSON을 찾을 수 없습니다.")

    return json.loads(text[start:end])


# =========================================================
# 사용자 입력 읽기 (최소 보완: message, product_id, GET, POST 모두 수신)
# =========================================================

def _get_user_message(event) -> tuple[str, str]:
    """
    Lambda 직접 테스트, GET 쿼리파라미터, POST Body 요청을 모두 수신.
    (message, product_id) 튜플을 반환함.
    """
    message = ""
    product_id = ""

    # 1. Lambda 콘솔 직접 테스트
    if isinstance(event, dict):
        message = event.get("message", "")
        product_id = event.get("product_id", "")

    # 2. GET 쿼리 파라미터 (?message=... 또는 ?product_id=...)
    params = event.get("queryStringParameters") or {}
    if params:
        message = message or params.get("message", "")
        product_id = product_id or params.get("product_id", "")

    # 3. API Gateway POST body
    body = event.get("body")
    if body:
        try:
            if isinstance(body, str):
                body = json.loads(body)
            if isinstance(body, dict):
                message = message or body.get("message", "")
                product_id = product_id or body.get("product_id", "")
        except Exception as e:
            print(f"⚠️ 요청 body 파싱 실패: {e}")

    return str(message).strip(), str(product_id).strip()


# =========================================================
# Bedrock 1
# 자연어 → product_id
# =========================================================

def _extract_product_with_bedrock(message: str) -> dict | None:
    """
    사용자 질문에서 제품을 추출하고 product_id로 정규화.
    제품 목록을 하드코딩하지 않는다.
    """

    prompt = f"""
너는 전자기기 구매 타이밍 서비스의 제품 식별 모듈이다.

사용자의 질문에서 구매를 고려하고 있는 전자기기 제품을 식별하라.

[product_id 생성 규칙]

- 모두 영문 소문자로 작성
- 단어 사이는 하이픈(-) 사용
- 브랜드/제품군/모델명을 포함
- 제품명을 일반적으로 식별하는 데 불필요한 색상, 통신사 정보는 제외
- 저장용량은 제품 식별에 반드시 필요한 경우가 아니면 제외
- 사용자가 명확한 모델을 말하지 않았다면 UNKNOWN
- 제품을 임의로 추측하지 말 것

예시:

"갤럭시 S23 사는 거 어때?"
→ galaxy-s23

"갤럭시 S23 울트라 지금 살까?"
→ galaxy-s23-ultra

"갤럭시 S24 울트라는 어때?"
→ galaxy-s24-ultra

"아이폰 15 프로 지금 사도 돼?"
→ iphone-15-pro

"아이폰 지금 사도 돼?"
→ UNKNOWN

"휴대폰 하나 사고 싶어"
→ UNKNOWN


반드시 아래 JSON 형식으로만 출력한다.

{{
  "product_id": "정규화된 product_id 또는 UNKNOWN",
  "product_name": "사용자가 언급한 제품명 또는 null"
}}

사용자 질문:
{message}
"""

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 150,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}]
            }
        ]
    })

    try:
        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=body
        )

        response_body = json.loads(response["body"].read())
        text = response_body["content"][0]["text"].strip()
        result = _extract_json(text)

        product_id = str(result.get("product_id", "")).strip().lower()

        if not product_id or product_id == "unknown":
            return None

        return {
            "product_id": product_id,
            "product_name": result.get("product_name")
        }

    except Exception as e:
        print(f"⚠️ Bedrock 제품명 추출 실패: {e}")
        return None


# =========================================================
# S3 분석 데이터 존재 여부 확인
# =========================================================

def _prefix_has_object(prefix: str) -> bool:
    try:
        response = s3.list_objects_v2(
            Bucket=BUCKET_NAME,
            Prefix=prefix,
            MaxKeys=1
        )

        return response.get("KeyCount", 0) > 0

    except Exception as e:
        print(f"⚠️ S3 데이터 존재 여부 확인 실패 ({prefix}): {e}")
        return False


def _check_product_data(product_id: str) -> dict:
    """
    제품에 대해 Lambda 2 분석 결과가 존재하는지 확인.
    가격 이력은 없어도 됨.
    NEWS 또는 SHOPPING 데이터 중 최소 하나는 있어야 분석 진행.
    """

    has_news = _prefix_has_object(
        f"analyzed/news/{product_id}/"
    )

    has_shopping = _prefix_has_object(
        f"analyzed/shopping_insight/{product_id}/"
    )

    return {
        "has_news": has_news,
        "has_shopping": has_shopping,
        "has_market_data": has_news or has_shopping
    }


# =========================================================
# Bedrock 설명 실패 시 fallback
# =========================================================

def _fallback_reasons(decision: dict) -> list[str]:
    reasons = []

    news = decision.get("news_signal", {})
    shopping = decision.get("shopping_signal", {})
    decision_type = decision.get("decision")

    for signal in news.get("key_signals", [])[:2]:
        reasons.append(signal)

    if shopping.get("trend_direction") == "UP":
        momentum = shopping.get("momentum_pct")

        if momentum is not None:
            reasons.append(
                f"최근 쇼핑 관심도가 이전 구간 대비 "
                f"{abs(momentum):.1f}% 상승했습니다."
            )

    if shopping.get("spike_detected"):
        reasons.append(
            "최근 쇼핑 관심도의 단기 급등이 감지되었습니다."
        )

    if decision.get("price_prediction_available"):
        drop_pct = decision.get("drop_3m_pct", 0)
        drop_amount = decision.get("drop_3m_amount", 0)

        if decision_type == "WAIT":
            reasons.append(
                f"현재 가격 추세 기준 3개월 후 약 "
                f"{drop_amount:,}원({drop_pct}%)의 "
                "추가 가격 하락이 예상됩니다."
            )
        else:
            reasons.append(
                f"3개월 예상 추가 하락폭이 {drop_pct}% 수준으로, "
                "현재 구매 혜택과 비교했을 때 기다릴 이점이 크지 않습니다."
            )

    if not reasons:
        reasons.append(
            "최근 시장 데이터를 종합해 구매 시점을 판단했습니다."
        )

    return reasons[:3]


# =========================================================
# Bedrock 2
# 최종 판단 이유 자연어 생성
# =========================================================

def _generate_explanation(
    product_id: str,
    decision: dict
) -> list[str]:

    news = decision.get("news_signal", {})
    shopping = decision.get("shopping_signal", {})

    if decision.get("price_prediction_available") and decision.get("predictions"):
        price_function = decision.get("price_function", {})
        preds = decision.get("predictions", {})
        curr_price = decision.get("current_price", 0) or 0
        
        p1m = preds.get("1m", {}).get("price", 0) or 0
        p3m = preds.get("3m", {}).get("price", 0) or 0
        p6m = preds.get("6m", {}).get("price", 0) or 0

        price_info = f"""
현재 가격: {decision["current_price"]:,}원
가격 함수: {price_function.get("formula", "N/A")}
감가 계수 k: {price_function.get("k", "N/A")}
1개월 예상 가격: {p1m:,}원
3개월 예상 가격: {p3m:,}원
6개월 예상 가격: {p6m:,}원
3개월 예상 하락률: {decision.get("drop_3m_pct", 0)}%
"""
    else:
        price_info = """
가격 이력: 없음
가격 이력이 없으므로 현재 가격과 미래 가격은 추정하지 않았다.
최종 판단은 뉴스와 쇼핑 관심도 데이터만을 사용했다.
"""

    prompt = f"""
너는 전자기기 구매 타이밍 서비스의 설명 생성 AI다.

최종 판단은 이미 시스템 알고리즘으로 결정되었다.
절대로 BUY/WAIT 판단을 변경하거나 새로 판단하지 마라.
아래 데이터를 근거로 최종 판단의 이유만 설명한다.

제품: {product_id}
최종 판단: {decision["decision"]}
구매 타이밍 점수: {decision["timing_score"]}

[가격 분석]
{price_info}

[뉴스 분석]
방향: {news.get("purchase_signal")}
신호 강도: {news.get("signal_strength")}
요약: {news.get("summary")}
핵심 근거: {news.get("key_signals")}

[쇼핑 관심도]
방향: {shopping.get("trend_direction")}
모멘텀: {shopping.get("momentum_pct")}
급등 여부: {shopping.get("spike_detected")}
설명: {shopping.get("trend_summary")}

[설명 규칙]

- 반드시 최종 판단과 일치하는 이유만 작성한다.
- 가격이 하락할 예정이라는 사실 자체를 BUY의 이유로 설명하지 않는다.
- BUY_NOW이고 예상 하락폭이 작다면,
  추가 하락폭이 크지 않아 현재 혜택을 이용하는 것이 유리하다고 설명한다.
- WAIT이고 예상 하락폭이 크다면,
  추가 가격 하락이 예상되어 기다리는 것이 유리하다고 설명한다.
- 쇼핑 데이터가 UNKNOWN이거나 없으면
  데이터 부족 자체를 BUY 또는 WAIT의 이유로 사용하지 않는다.
- 가격 이력이 없으면 임의의 가격이나 미래 가격을 만들어내지 않는다.
- 제공되지 않은 사실을 만들지 않는다.

반드시 아래 JSON 형식으로만 출력한다.

{{
  "reasons": [
    "이유 1",
    "이유 2",
    "이유 3"
  ]
}}

각 이유는 한국어 한 문장으로 간결하게 작성한다.
"""

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 600,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}]
            }
        ]
    })

    try:
        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=body
        )

        response_body = json.loads(response["body"].read())
        text = response_body["content"][0]["text"].strip()
        result = _extract_json(text)

        reasons = result.get("reasons", [])

        if isinstance(reasons, list) and reasons:
            return reasons[:3]

    except Exception as e:
        print(f"⚠️ Bedrock 설명 생성 실패: {e}")

    return _fallback_reasons(decision)


# =========================================================
# 그래프 데이터
# =========================================================

def _build_chart_data(decision: dict) -> list[dict]:
    chart_series = []

    # 실제 가격 이력
    if decision.get("has_historical_data"):
        for item in decision.get("price_history", []):
            if isinstance(item, dict) and "price" in item:
                chart_series.append({
                    "label": item.get("date", ""),
                    "date": item.get("date", ""),
                    "price": item.get("price", 0),
                    "type": "ACTUAL"
            })

    # 미래 예측 가격
    if (
        decision.get("price_prediction_available")
        and decision.get("predictions")
    ):
        preds = decision["predictions"]

        chart_series.extend([
            {
                "label": "1개월 후",
                "date": "1개월 후",
                "month": 1,
                "price": preds["1m"]["price"],
                "type": "PREDICTED"
            },
            {
                "label": "3개월 후",
                "date": "3개월 후",
                "month": 3,
                "price": preds["3m"]["price"],
                "type": "PREDICTED"
            },
            {
                "label": "6개월 후",
                "date": "6개월 후",
                "month": 6,
                "price": preds["6m"]["price"],
                "type": "PREDICTED"
            }
        ])

    return chart_series


# =========================================================
# Handler
# =========================================================

def lambda_handler(event, context):
    try:
        # OPTIONS (HTTP API 및 REST API 방식 호환)
        method = (
            event.get("requestContext", {})
            .get("http", {})
            .get("method")
        ) or event.get("httpMethod")

        if method == "OPTIONS":
            return {
                "statusCode": 200,
                "headers": _cors_headers(),
                "body": ""
            }

        # -------------------------------------------------
        # 1. 사용자 질문 및 product_id 읽기
        # -------------------------------------------------

        message, direct_product_id = _get_user_message(event)

        if not message and not direct_product_id:
            return {
                "statusCode": 400,
                "headers": _cors_headers(),
                "body": json.dumps({
                    "message": "구매를 고민하는 제품을 입력해주세요."
                }, ensure_ascii=False)
            }

        print(f"📩 사용자 질문: {message or direct_product_id}")


        # -------------------------------------------------
        # 2. Bedrock으로 product_id 추출 (직접 입력시 생략)
        # -------------------------------------------------

        if direct_product_id:
            raw_product_id = direct_product_id
            product = {"product_id": direct_product_id, "product_name": direct_product_id}
        else:
            product = _extract_product_with_bedrock(message)

            if not product:
                return {
                    "statusCode": 400,
                    "headers": _cors_headers(),
                    "body": json.dumps({
                        "message": "질문에서 구체적인 제품명을 확인할 수 없습니다."
                    }, ensure_ascii=False)
                }
            raw_product_id = product["product_id"]

        print(f"🔎 추출된 product_id: {raw_product_id}")

        # 💡 [핵심 수정] S3 데이터 존재 여부 확인 전에 정규화를 먼저 수행함!
        product_id = normalize_product_id(raw_product_id)
        print(f"🔎 정규화 완료된 product_id: {product_id}")


        # -------------------------------------------------
        # 3. 실제 분석 데이터 존재 여부 확인
        # -------------------------------------------------

        data_status = _check_product_data(product_id)

        if not data_status["has_market_data"]:
            return {
                "statusCode": 404,
                "headers": _cors_headers(),
                "body": json.dumps({
                    "product_id": product_id,
                    "message": "현재 해당 제품을 분석할 수 있는 시장 데이터가 충분하지 않습니다."
                }, ensure_ascii=False)
            }


        # -------------------------------------------------
        # 4. S3 데이터 기반 최종 판단
        # -------------------------------------------------

        decision = get_purchase_decision(product_id)
        product_id = decision.get("product_id", product_id)


        # -------------------------------------------------
        # 5. Bedrock으로 판단 이유 생성
        # -------------------------------------------------

        why_reasons = _generate_explanation(
            product_id,
            decision
        )


        # -------------------------------------------------
        # 6. 그래프
        # -------------------------------------------------

        chart_series = _build_chart_data(
            decision
        )


        # -------------------------------------------------
        # 7. API 응답
        # -------------------------------------------------

        response_payload = {
            "user_message": message or f"{product_id} 조회",

            "product_id": product_id,

            "product_name": product.get(
                "product_name"
            ),

            "data_status": data_status,

            "has_historical_data":
                decision["has_historical_data"],

            "price_prediction_available":
                decision["price_prediction_available"],

            "current_price":
                decision["current_price"],

            "recommendation":
                decision["decision"],

            "timing_score":
                decision["timing_score"],

            "regret_risk_score":
                decision["regret_risk_score"],

            "target_buy_price":
                decision["target_buy_price"],

            "drop_3m_pct":
                decision["drop_3m_pct"],

            "drop_3m_amount":
                decision["drop_3m_amount"],

            "predictions":
                decision["predictions"],

            "price_function":
                decision.get("price_function"),

            "chart_data":
                chart_series,

            "news_signal":
                decision["news_signal"],

            "shopping_signal":
                decision["shopping_signal"],

            "why_reasons":
                why_reasons
        }

        return {
            "statusCode": 200,
            "headers": _cors_headers(),
            "body": json.dumps(
                response_payload,
                ensure_ascii=False
            )
        }

    except Exception as e:
        print(f"❌ Lambda 3 처리 실패: {e}")

        return {
            "statusCode": 500,
            "headers": _cors_headers(),
            "body": json.dumps({
                "message": "구매 타이밍 분석 중 오류가 발생했습니다.",
                "error": str(e)
            }, ensure_ascii=False)
        }
