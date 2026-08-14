import base64
import json
import os
import re
import html
import boto3
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from botocore.config import Config

s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime", region_name="ap-northeast-2", config=Config(retries={"max_attempts": 3, "mode": "standard"}))

BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "market-project-data-team6")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
KST = timezone(timedelta(hours=9))


def clean_text(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def save_to_s3(key, data):
    s3_client.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json"
    )
    print(f"✅ S3 저장 완료: s3://{BUCKET_NAME}/{key}")


def extract_json(text):
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError("Bedrock 응답에서 JSON을 찾지 못했습니다.")
    return json.loads(text[start:end])


def clamp(value, minimum=0, maximum=100):
    return max(minimum, min(maximum, value))


def analyze_news_with_bedrock(product_id, news_items):
    if not news_items:
        return None

    news_text_list = []
    for idx, item in enumerate(news_items[:20], 1):
        title = clean_text(item.get("title", ""))
        description = clean_text(item.get("description", ""))
        search_keyword = item.get("search_keyword", "")
        pub_date = item.get("pubDate", "")

        news_text_list.append(
            f"[{idx}]\n검색 키워드: {search_keyword}\n제목: {title}\n내용: {description}\n기사 시각: {pub_date}"
        )

    combined_news_text = "\n\n".join(news_text_list)

    prompt = f"""
너는 스마트폰 구매 타이밍 분석 시스템의 뉴스 분석 모듈이다.

분석 대상 제품 ID: {product_id}

아래 뉴스들을 종합해서 이 제품의 구매 타이밍에 어떤 방향의 신호가 있는지 분석한다.
여기서는 최종 구매 결정을 하지 않고, 뉴스가 BUY 쪽 근거인지 WAIT 쪽 근거인지와 그 근거가 얼마나 강한지만 추출한다.

[WAIT 신호]
- 후속 모델 출시 또는 발표 임박
- 신제품 언팩 행사
- 후속 제품 공식 발표
- 기존 제품의 추가 가격 인하 예상
- 더 큰 할인이나 프로모션 예상

[BUY 신호]
- 현재 즉시 적용되는 가격 인하
- 공시지원금 확대
- 자급제 할인
- 대규모 즉시 프로모션
- 재고 정리 할인

[NEUTRAL]
- 가격이나 구매 시점에 직접 영향이 없는 뉴스
- 단순 제품 리뷰
- 일반적인 기능 설명
- 구매 타이밍과 관계없는 기사

[루머]
공식 확인되지 않은 루머나 유출도 BUY 또는 WAIT 방향은 판단할 수 있지만 signal_strength는 낮게 평가한다.

[signal_strength]
0~100. 뉴스들이 BUY 또는 WAIT 판단 근거로 얼마나 강한지 나타낸다.
공식 발표/공식 할인: 80~100
여러 매체의 일관된 전망: 60~80
단일 루머: 30~50
관련성 낮음: 0~20

[market_impact_score]
0 = 매우 강한 BUY
50 = 중립
100 = 매우 강한 WAIT

반드시 아래 JSON 형식으로만 출력한다.
{{
  "purchase_signal": "BUY 또는 WAIT 또는 NEUTRAL",
  "signal_strength": 0,
  "market_impact_score": 50,
  "summary": "현재 뉴스 상황을 2문장 이내로 요약",
  "key_signals": ["핵심 근거 1", "핵심 근거 2"]
}}

[뉴스]
{combined_news_text}
"""

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    })

    try:
        response = bedrock_client.invoke_model(modelId=BEDROCK_MODEL_ID, body=body)
        response_body = json.loads(response["body"].read())
        result = extract_json(response_body["content"][0]["text"].strip())

        purchase_signal = result.get("purchase_signal", "NEUTRAL")
        if purchase_signal not in {"BUY", "WAIT", "NEUTRAL"}:
            purchase_signal = "NEUTRAL"

        try:
            signal_strength = int(float(result.get("signal_strength", 0)))
        except (TypeError, ValueError):
            signal_strength = 0

        try:
            market_impact_score = int(float(result.get("market_impact_score", 50)))
        except (TypeError, ValueError):
            market_impact_score = 50

        key_signals = result.get("key_signals", [])
        if not isinstance(key_signals, list):
            key_signals = []

        return {
            "purchase_signal": purchase_signal,
            "signal_strength": clamp(signal_strength),
            "market_impact_score": clamp(market_impact_score),
            "summary": result.get("summary", ""),
            "key_signals": key_signals
        }

    except Exception as e:
        print(f"❌ Bedrock 뉴스 분석 실패 ({product_id}): {e}")
        return {
            "purchase_signal": "NEUTRAL",
            "signal_strength": 0,
            "market_impact_score": 50,
            "summary": "뉴스 분석에 실패했습니다.",
            "key_signals": [],
            "error": str(e)
        }


def analyze_shopping_trend(record):
    raw_points = record.get("data", [])
    points = []

    for item in raw_points:
        try:
            period = item.get("period")
            ratio = float(item.get("ratio"))
            if period is not None:
                points.append({"period": period, "ratio": ratio})
        except (TypeError, ValueError):
            continue

    points.sort(key=lambda x: x["period"])

    if not points:
        return {
            "trend_direction": "UNKNOWN",
            "latest_ratio": None,
            "recent_3day_avg": None,
            "previous_4day_avg": None,
            "momentum_pct": None,
            "spike_detected": False,
            "spike_pct": None,
            "trend_strength": 0,
            "trend_summary": "분석 가능한 쇼핑 트렌드 데이터가 없습니다.",
            "data_points": []
        }

    ratios = [item["ratio"] for item in points]

    recent_values = ratios[-3:]
    recent_avg = sum(recent_values) / len(recent_values)

    previous_values = ratios[max(0, len(ratios) - 7):len(ratios) - 3] if len(ratios) > 3 else []
    previous_avg = sum(previous_values) / len(previous_values) if previous_values else recent_avg
    momentum_pct = ((recent_avg - previous_avg) / previous_avg * 100) if previous_avg > 0 else 0.0

    latest_ratio = ratios[-1]
    previous_3_values = ratios[-4:-1] if len(ratios) >= 4 else ratios[:-1]
    previous_3_avg = sum(previous_3_values) / len(previous_3_values) if previous_3_values else latest_ratio
    spike_pct = ((latest_ratio - previous_3_avg) / previous_3_avg * 100) if previous_3_avg > 0 else 0.0

    spike_detected = latest_ratio >= previous_3_avg * 1.5 and latest_ratio - previous_3_avg >= 10

    if momentum_pct >= 10:
        trend_direction = "UP"
    elif momentum_pct <= -10:
        trend_direction = "DOWN"
    else:
        trend_direction = "STABLE"

    trend_strength = int(min(100, abs(momentum_pct) + (25 if spike_detected else 0)))

    if trend_direction == "UP":
        trend_summary = f"최근 소비자 관심도가 상승 중입니다. 최근 3일 평균 관심 지수는 {recent_avg:.2f}이고, 이전 구간보다 {momentum_pct:.1f}% 증가했습니다."
    elif trend_direction == "DOWN":
        trend_summary = f"최근 소비자 관심도가 하락 중입니다. 최근 3일 평균 관심 지수는 {recent_avg:.2f}이고, 이전 구간보다 {abs(momentum_pct):.1f}% 감소했습니다."
    else:
        trend_summary = f"최근 소비자 관심도에는 큰 변화가 없습니다. 최근 3일 평균 관심 지수는 {recent_avg:.2f}입니다."

    if spike_detected:
        trend_summary += f" 최신 날짜에는 직전 평균 대비 {spike_pct:.1f}%의 관심도 급등이 감지되었습니다."

    return {
        "trend_direction": trend_direction,
        "latest_ratio": round(latest_ratio, 2),
        "recent_3day_avg": round(recent_avg, 2),
        "previous_4day_avg": round(previous_avg, 2),
        "momentum_pct": round(momentum_pct, 2),
        "spike_detected": spike_detected,
        "spike_pct": round(spike_pct, 2),
        "trend_strength": trend_strength,
        "trend_summary": trend_summary,
        "data_points": points
    }


def lambda_handler(event, context):
    now = datetime.now(KST)
    date_path = now.strftime("year=%Y/month=%m/day=%d")
    timestamp = now.strftime("%H%M%S%f")

    news_by_product = defaultdict(list)
    shopping_records = []
    processed = 0
    failed = 0

    # 1. Kinesis 데이터 분류
    for record in event.get("Records", []):
        try:
            raw_payload = base64.b64decode(record["kinesis"]["data"]).decode("utf-8")
            data = json.loads(raw_payload)

            record_type = data.get("type", "").upper()
            product_id = data.get("product_id", "unknown")

            if record_type == "NEWS":
                news_by_product[product_id].append(data)
            elif record_type == "SHOPPING_INSIGHT":
                shopping_records.append(data)
            else:
                print(f"⚠️ 알 수 없는 데이터 형식: {record_type}")

            processed += 1

        except Exception as e:
            failed += 1
            print(f"❌ Kinesis 레코드 디코딩 실패: {e}")

    total_news = sum(len(items) for items in news_by_product.values())
    print(f"📥 Kinesis 수신 완료 | NEWS: {total_news}건 | SHOPPING_INSIGHT: {len(shopping_records)}건")

    # 2. NEWS 저장 + 분석
    for product_id, news_items in news_by_product.items():
        raw_news_key = f"raw/news/{product_id}/{date_path}/news_raw_{timestamp}.json"
        save_to_s3(raw_news_key, news_items)

        news_analysis = analyze_news_with_bedrock(product_id, news_items)
        analyzed_news_payload = {
            "product_id": product_id,
            "analyzed_at": now.isoformat(),
            "news_count": len(news_items),
            "analysis": news_analysis
        }

        analyzed_news_key = f"analyzed/news/{product_id}/{date_path}/news_analysis_{timestamp}.json"
        save_to_s3(analyzed_news_key, analyzed_news_payload)

    # 3. SHOPPING_INSIGHT 저장 + 분석
    for shopping_record in shopping_records:
        product_id = shopping_record.get("product_id", "unknown")

        raw_shopping_key = f"raw/shopping_insight/{product_id}/{date_path}/shopping_raw_{timestamp}.json"
        save_to_s3(raw_shopping_key, shopping_record)

        trend_analysis = analyze_shopping_trend(shopping_record)
        analyzed_shopping_payload = {
            "product_id": product_id,
            "product_name": shopping_record.get("title"),
            "collected_at": shopping_record.get("collected_at"),
            "analyzed_at": now.isoformat(),
            "analysis": trend_analysis
        }

        analyzed_shopping_key = f"analyzed/shopping_insight/{product_id}/{date_path}/shopping_analysis_{timestamp}.json"
        save_to_s3(analyzed_shopping_key, analyzed_shopping_payload)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Lambda 2 processing success",
            "processed": processed,
            "failed": failed,
            "news_count": total_news,
            "shopping_count": len(shopping_records)
        }, ensure_ascii=False)
    }
