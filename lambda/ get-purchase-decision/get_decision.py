import json
import os
import boto3

from regret_risk import calculate_multi_factor_prediction

s3 = boto3.client("s3", region_name="ap-northeast-2")
BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "market-project-data-team6")

PRODUCT_ID_MAP = {
    "s23": "galaxy-s23",
    "s23ultra": "galaxy-s23-ultra",
    "s23-ultra": "galaxy-s23-ultra"
}

PRODUCT_PRICE_KEY_MAP = {
    "galaxy-s23": "price_history/galaxy_s23_256gb.json",
    "galaxy-s23-ultra": "price_history/galaxy_s23_ultra_256gb.json"
}


def normalize_product_id(product_id: str) -> str:
    product_id = product_id.lower().strip()
    return PRODUCT_ID_MAP.get(product_id, product_id)


def _load_price_history_from_s3(product_id: str) -> list[dict]:
    """S3 price_history JSON에서 해당 제품의 가격 이력을 조회한다."""
    key = PRODUCT_PRICE_KEY_MAP.get(product_id)

    if not key:
        print(f"ℹ️ 가격 이력 대상이 아닌 제품: {product_id}")
        return []

    try:
        response = s3.get_object(Bucket=BUCKET_NAME, Key=key)
        data = json.loads(response["Body"].read().decode("utf-8"))

        history = []

        for item in data.get("prices", []):
            try:
                date = str(item.get("date", "")).strip()
                price = int(float(item.get("price", 0)))

                if date and price > 0:
                    history.append({"date": date, "price": price})
            except (TypeError, ValueError):
                continue

        history.sort(key=lambda x: x["date"])

        print(f"✅ 가격 이력 로드 완료: {product_id} / {len(history)}건 / {key}")
        return history

    except s3.exceptions.NoSuchKey:
        print(f"ℹ️ 가격 이력 파일 없음: {key}")
        return []

    except Exception as e:
        print(f"⚠️ 가격 이력 JSON 읽기 실패 ({key}): {e}")
        return []


def _get_latest_json(prefix: str) -> dict | None:
    """해당 prefix에서 가장 최근 JSON 하나를 조회한다."""
    paginator = s3.get_paginator("list_objects_v2")
    latest_obj = None

    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".json"):
                continue

            if latest_obj is None or obj["LastModified"] > latest_obj["LastModified"]:
                latest_obj = obj

    if not latest_obj:
        print(f"ℹ️ 분석 데이터 없음: {prefix}")
        return None

    try:
        response = s3.get_object(Bucket=BUCKET_NAME, Key=latest_obj["Key"])
        return json.loads(response["Body"].read().decode("utf-8"))

    except Exception as e:
        print(f"⚠️ S3 JSON 읽기 실패 ({latest_obj['Key']}): {e}")
        return None


def _fetch_latest_news_analysis(product_id: str) -> dict | None:
    data = _get_latest_json(f"analyzed/news/{product_id}/")
    if not data:
        return None

    analysis = data.get("analysis", {})
    analysis["analyzed_at"] = data.get("analyzed_at")
    return analysis


def _fetch_latest_shopping_analysis(product_id: str) -> dict | None:
    data = _get_latest_json(f"analyzed/shopping_insight/{product_id}/")
    if not data:
        return None

    analysis = data.get("analysis", {})
    analysis["analyzed_at"] = data.get("analyzed_at")
    return analysis


def get_purchase_decision(product_id: str) -> dict:
    product_id = normalize_product_id(product_id)

    price_history = _load_price_history_from_s3(product_id)
    news_analysis = _fetch_latest_news_analysis(product_id)
    shopping_analysis = _fetch_latest_shopping_analysis(product_id)

    prediction = calculate_multi_factor_prediction(
        product_id=product_id,
        price_history=price_history,
        news_analysis=news_analysis,
        shopping_analysis=shopping_analysis
    )

    prediction["product_id"] = product_id
    prediction["price_history"] = price_history
    prediction["has_historical_data"] = len(price_history) > 0

    return prediction
