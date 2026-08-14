import os
import json
import urllib.request
import urllib.parse
import urllib.error
import boto3
from datetime import datetime, timedelta, timezone

# 한국 시간
KST = timezone(timedelta(hours=9))

# AWS 서비스 클라이언트 초기화
kinesis_client = boto3.client("kinesis")

# 환경 변수 로드
NCP_CLIENT_ID = os.environ.get("NCP_CLIENT_ID")
NCP_CLIENT_SECRET = os.environ.get("NCP_CLIENT_SECRET")
STREAM_NAME = os.environ.get("KINESIS_STREAM_NAME")


# -----------------------------------------------------------------
# 수집 대상 제품 및 키워드 설정
# -----------------------------------------------------------------
TARGET_CONFIG = {
    "galaxy-s23": {
        "news_keywords": [
            "갤럭시 S23",
            "갤럭시 S23 공시지원금",
            "갤럭시 S23 재고처리",
            "갤럭시 S23 중고"
        ],
        "shopping_keyword_group": {
            "name": "갤럭시 S23",
            "param": ["갤럭시 S23"]
        }
    },

    "galaxy-s23-ultra": {
        "news_keywords": [
            "갤럭시 S23 울트라",
            "S23 울트라 공시지원금",
            "S23 울트라 자급제 할인",
            "S23 울트라 중고"
        ],
        "shopping_keyword_group": {
            "name": "갤럭시 S23 울트라",
            "param": ["갤럭시 S23 울트라"]
        }
    }
}


# 네이버 쇼핑 디지털/가전 카테고리 ID
CATEGORY_ID = "50000008"


# -----------------------------------------------------------------
# 1. NAVER 뉴스 검색 API
# -----------------------------------------------------------------
def fetch_naver_news(query, display=5):
    api_url = "https://naverapihub.apigw.ntruss.com/search/v1/news"

    params = urllib.parse.urlencode({
        "query": query,
        "display": display,
        "start": 1,
        "sort": "date"
    })

    request = urllib.request.Request(
        f"{api_url}?{params}"
    )

    request.add_header(
        "X-NCP-APIGW-API-KEY-ID",
        NCP_CLIENT_ID
    )

    request.add_header(
        "X-NCP-APIGW-API-KEY",
        NCP_CLIENT_SECRET
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            if response.status == 200:
                res_data = json.loads(
                    response.read().decode("utf-8")
                )

                return res_data.get(
                    "items",
                    []
                )

            return []

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")

        print(
            f"❌ [NEWS] HTTP 에러 "
            f"({e.code} {e.reason}) "
            f"| 검색어: '{query}'"
        )

        print(
            f"   👉 상세 내용: "
            f"{error_body}"
        )

        return []

    except Exception as e:
        print(
            f"❌ [NEWS] 기타 예외 발생 "
            f"(검색어: '{query}'): {e}"
        )

        return []


# -----------------------------------------------------------------
# 2. NAVER Shopping Insight API
# -----------------------------------------------------------------
def fetch_shopping_insight(
    start_date,
    end_date,
    category_id,
    keyword_groups
):
    api_url = (
        "https://naverapihub.apigw.ntruss.com/"
        "shopping/v1/category/keywords"
    )

    body = {
        "startDate": start_date,
        "endDate": end_date,
        "timeUnit": "date",
        "category": category_id,
        "keyword": keyword_groups
    }

    json_payload = json.dumps(
        body
    ).encode("utf-8")

    request = urllib.request.Request(
        api_url,
        data=json_payload,
        method="POST"
    )

    request.add_header(
        "X-NCP-APIGW-API-KEY-ID",
        NCP_CLIENT_ID
    )

    request.add_header(
        "X-NCP-APIGW-API-KEY",
        NCP_CLIENT_SECRET
    )

    request.add_header(
        "Content-Type",
        "application/json"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            if response.status == 200:
                return json.loads(
                    response.read().decode("utf-8")
                )

            return {}

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")

        print(
            f"❌ [SHOPPING_INSIGHT] "
            f"HTTP 에러 "
            f"({e.code} {e.reason})"
        )

        print(
            f"   👉 상세 내용: "
            f"{error_body}"
        )

        return {}

    except Exception as e:
        print(
            f"❌ [SHOPPING_INSIGHT] "
            f"기타 예외 발생: {e}"
        )

        return {}


# -----------------------------------------------------------------
# Lambda Handler
# -----------------------------------------------------------------
def lambda_handler(event, context):
    kinesis_records = []

    # 한국 시간 기준
    now = datetime.now(KST)
    current_time = now.isoformat()

    # 실행 시점 기준 최근 7일 Shopping Insight 조회
    end_date = now.strftime(
        "%Y-%m-%d"
    )

    start_date = (
        now - timedelta(days=7)
    ).strftime(
        "%Y-%m-%d"
    )

    api_success_count = 0


    # =========================================================
    # 1. NEWS 데이터 수집
    # =========================================================
    for product_id, config in TARGET_CONFIG.items():

        for news_kw in config[
            "news_keywords"
        ]:

            raw_news = fetch_naver_news(
                news_kw,
                display=5
            )

            if raw_news:
                api_success_count += 1

                for item in raw_news:

                    news_payload = {
                        "type": "NEWS",
                        "product_id": product_id,
                        "search_keyword": news_kw,
                        "title": item.get(
                            "title"
                        ),
                        "originallink": item.get(
                            "originallink"
                        ),
                        "link": item.get(
                            "link"
                        ),
                        "description": item.get(
                            "description"
                        ),
                        "pubDate": item.get(
                            "pubDate"
                        ),
                        "collected_at":
                            current_time
                    }

                    kinesis_records.append({
                        "Data": json.dumps(
                            news_payload,
                            ensure_ascii=False
                        ).encode(
                            "utf-8"
                        ),

                        "PartitionKey":
                            product_id
                    })


    # =========================================================
    # 2. SHOPPING INSIGHT 데이터 수집
    # =========================================================
    shopping_keyword_groups = [
        config[
            "shopping_keyword_group"
        ]
        for config
        in TARGET_CONFIG.values()
    ]


    insight_res = fetch_shopping_insight(
        start_date,
        end_date,
        CATEGORY_ID,
        shopping_keyword_groups
    )


    if (
        insight_res
        and "results" in insight_res
    ):

        api_success_count += 1

        for result_item in insight_res.get(
            "results",
            []
        ):

            group_title = result_item.get(
                "title"
            )


            # ---------------------------------------------
            # Shopping Insight 결과와 product_id 매핑
            # ---------------------------------------------
            matched_product_id = "general"

            for (
                p_id,
                p_cfg
            ) in TARGET_CONFIG.items():

                if (
                    p_cfg[
                        "shopping_keyword_group"
                    ][
                        "name"
                    ]
                    == group_title
                ):
                    matched_product_id = p_id
                    break


            insight_payload = {
                "type":
                    "SHOPPING_INSIGHT",

                "product_id":
                    matched_product_id,

                "title":
                    group_title,

                "data":
                    result_item.get(
                        "data",
                        []
                    ),

                "collected_at":
                    current_time
            }


            kinesis_records.append({
                "Data": json.dumps(
                    insight_payload,
                    ensure_ascii=False
                ).encode(
                    "utf-8"
                ),

                "PartitionKey":
                    matched_product_id
            })


    # =========================================================
    # 3. Kinesis Data Stream 전송
    # =========================================================
    if kinesis_records:

        try:
            response = (
                kinesis_client.put_records(
                    Records=
                        kinesis_records,

                    StreamName=
                        STREAM_NAME
                )
            )

            failed_count = response.get(
                "FailedRecordCount",
                0
            )

            print(
                f"✅ Kinesis 전송 완료: "
                f"총 {len(kinesis_records)}건 "
                f"(실패: {failed_count}건)"
            )

        except Exception as e:
            print(
                f"❌ Kinesis 레코드 "
                f"전송 중 에러 발생: {e}"
            )

            raise


    else:
        print(
            "⚠️ 수집된 데이터가 없습니다."
        )


    # =========================================================
    # 4. API 호출 결과 확인
    # =========================================================
    if api_success_count == 0:

        return {
            "statusCode": 400,

            "body": json.dumps(
                "모든 API 호출에 실패했습니다. "
                "CloudWatch 상세 에러 로그를 확인하세요.",
                ensure_ascii=False
            )
        }


    return {
        "statusCode": 200,

        "body": json.dumps(
            f"성공적으로 "
            f"{len(kinesis_records)}건의 "
            f"데이터를 수집하여 "
            f"Kinesis로 전송했습니다.",
            ensure_ascii=False
        )
    }
