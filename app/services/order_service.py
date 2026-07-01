"""
services/order_service.py — 발주(구매) 미리보기/엑셀 산출
================================================================================
agents/candidate_agent.py의 CYPHER_QUERY는 메뉴 단위로 영양·비용을 합산해서
반환하기 때문에(식재료별 개별 수량은 버려짐), 발주서에 필요한 "식재료별
수량·단가"를 얻으려면 별도 쿼리가 필요함. Neo4j 그래프 구조 자체는 이미
Food-[:HAS_INGREDIENT {nutri_weight}]->Recipe-[:MAPPED_TO]->Product 로
필요한 정보를 다 갖고 있음(nutri_weight = 1인분 기준 그 재료의 사용량(g)).

흐름:
  1. meal_plan_runs.facility_id로 활성 환자 수를 구함
  2. meal_plan_slots에서 week_offset(0~3, 7일 단위)에 해당하는 구간의
     메뉴만 추림
  3. 그 구간에 쓰인 메뉴명들을 모아 Neo4j에서 메뉴별·식재료별 사용량/단가 조회
  4. (메뉴 등장 횟수 × 1인분 사용량 × PORTION_SCALE × 환자 수)로 각
     (메뉴, 식재료) 행의 총 소요량과 예상 비용을 계산

[수정 — 2026-07-01 #1] 응답 스키마를 프론트엔드(src/pages/OrderExcel.tsx)가
이미 기대하고 있던 형태에 정확히 맞춤. 기존 구현은 이 화면이 만들어지기
전에 작성되어 식재료 단위로만 합산한(메뉴 구분 없는) 다른 스키마를
반환하고 있었음 — 그 상태로는 프론트가 렌더링할 수 없었음(필드명이
전혀 안 맞음). 이제는 프론트가 그리는 표(메뉴명/재료/사용 끼니수/
총 중량/구매 상품/단가/예상 비용)와 1:1로 대응하는 (메뉴, 식재료) 단위
행을 반환함.

[수정 — 2026-07-01 #2] Neo4j Product 노드를 직접 확인한 결과, 쿼리가
기대하던 unit_g(숫자) 속성이 존재하지 않고 대신 unit(문자열, 예: "1kg")
만 있었음. coalesce(cheapest_p.unit_g, 1)이 항상 기본값 1을 써서
"1g당 단가"를 "10,933원 ÷ 1"처럼 잘못 계산하던 문제를 수정 — unit
문자열을 파싱해 실제 g 단위로 환산한 뒤 단가를 계산함.

[주의 — 근사치임을 명확히]
  - PersonalizeAgent가 환자별로 조정하는 개인화 배식 ratio(부찬 교체,
    양 조절)는 여기서는 반영하지 않음 — 환자 전원이 "기본 1인분
    (PORTION_SCALE 적용된)"을 먹는다고 가정한 근사 발주량임. 실제 필요량과
    다소 차이가 날 수 있으므로, 발주 담당자가 여유분을 고려해 참고용으로
    사용해야 함.
[수정 — 2026-07-01 #4] 매 요청마다 Neo4jGraph(...)를 새로 생성해 Aura와
TLS 핸드셰이크를 반복하던 문제를 수정. 발주 페이지는 주차를 바꿔가며
반복 조회하는 경우가 많아, 커넥션 재사용의 효과가 특히 큼. 모듈 전역에
드라이버 하나를 캐싱해 프로세스 내에서 재사용함(Render가 단일 워커
프로세스로 뜬다는 전제와 동일하게, 이 캐시도 프로세스 단위로 안전함).
"""

import os
import re
import time
from app.services.db_clients import get_supabase

PORTION_SCALE = 0.8  # agents/candidate_agent.py의 PORTION_SCALE과 반드시 동일하게 유지

_CACHE_TTL_SEC = 3600  # 1시간 — 식재료/가격 데이터는 하루 안에 자주 바뀌지 않으므로
_order_cache: dict[tuple, tuple[float, dict]] = {}

_graph_client = None  # Neo4jGraph 싱글턴(프로세스당 1회만 연결)

INGREDIENT_QUERY = """
    MATCH (f:Food) WHERE f.title IN $menu_names
    MATCH (f)-[hi:HAS_INGREDIENT]->(r:Recipe)
    OPTIONAL MATCH (r)-[:MAPPED_TO]->(p:Product)
    WITH f, r, hi, p
    ORDER BY p.price_today ASC
    WITH f, r, hi, collect(p) AS products
    WITH f, r, hi,
         CASE WHEN size(products) > 0 THEN products[0] ELSE null END AS cheapest_p
    RETURN f.title AS menu_name,
           r.title AS ingredient_name,
           toFloat(coalesce(hi.nutri_weight, 0)) AS grams_per_serving,
           cheapest_p.name AS product_name,
           cheapest_p.price_today AS price_today,
           cheapest_p.unit AS unit_str
"""

# "1kg" -> 1000, "500g" -> 500, "1.5kg" -> 1500 등. 매칭 안 되면 None(단가 계산 불가로 처리).
_UNIT_PATTERN = re.compile(r"([\d.]+)\s*(kg|g)", re.IGNORECASE)


def _parse_unit_to_grams(unit_str: str | None) -> float | None:
    if not unit_str:
        return None
    match = _UNIT_PATTERN.search(unit_str)
    if not match:
        return None
    value, unit = match.groups()
    try:
        value = float(value)
    except ValueError:
        return None
    return value * 1000 if unit.lower() == "kg" else value


def _get_neo4j_graph():
    global _graph_client
    if _graph_client is None:
        from langchain_neo4j import Neo4jGraph
        _graph_client = Neo4jGraph(
            url=os.getenv("NEO4J_URI"),
            username=os.getenv("NEO4J_USERNAME"),
            password=os.getenv("NEO4J_PASSWORD"),
            database=os.getenv("NEO4J_DATABASE", "neo4j"),
        )
    return _graph_client


def _week_day_range(week_offset: int) -> tuple[int, int]:
    """week_offset(0~3) → (시작일, 종료일). 28일 식단을 7일씩 4주로 나눔."""
    start = week_offset * 7 + 1
    end = min(start + 6, 28)
    return start, end


def build_order_data(run_id: str, week_offset: int = 0) -> dict:
    """
    발주 미리보기(JSON)와 엑셀 생성 양쪽에서 공유하는 핵심 계산.
    반환 스키마는 src/pages/OrderExcel.tsx의 OrderPreview 타입과 정확히 일치.
    같은 (run_id, week_offset)에 대해 5분 이내 재호출되면 캐시를 반환함
    (미리보기 직후 다운로드하는 흔한 흐름에서 Neo4j 재조회를 피하기 위함).
    """
    cache_key = (run_id, week_offset)
    cached = _order_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]

    result = _compute_order_data(run_id, week_offset)
    _order_cache[cache_key] = (time.time(), result)
    return result


def _compute_order_data(run_id: str, week_offset: int) -> dict:
    sb = get_supabase()

    run = sb.table("meal_plan_runs").select("facility_id").eq("id", run_id).single().execute()
    if not run.data:
        raise ValueError("실행 기록을 찾을 수 없습니다.")
    facility_id = run.data["facility_id"]

    patients = (
        sb.table("patients")
          .select("id", count="exact")
          .eq("facility_id", facility_id)
          .eq("active", True)
          .execute()
    )
    patient_count = patients.count or len(patients.data or [])

    start_day, end_day = _week_day_range(week_offset)
    week_range = f"{start_day}일~{end_day}일"

    slots = (
        sb.table("meal_plan_slots")
          .select("day_number,meal_type,rice,soup,main_dish,side_dish_1,side_dish_2,kimchi")
          .eq("run_id", run_id)
          .gte("day_number", start_day)
          .lte("day_number", end_day)
          .execute()
          .data
    )
    if not slots:
        return {
            "run_id": run_id,
            "week_range": week_range,
            "total_items": 0,
            "total_cost": 0,
            "items": [],
        }

    # 메뉴 등장 횟수 집계(같은 메뉴가 여러 끼니에 반복 등장할 수 있음)
    menu_occurrences: dict[str, int] = {}
    for s in slots:
        for col in ["rice", "soup", "main_dish", "side_dish_1", "side_dish_2", "kimchi"]:
            name = s.get(col)
            if name:
                menu_occurrences[name] = menu_occurrences.get(name, 0) + 1

    menu_names = list(menu_occurrences.keys())
    graph = _get_neo4j_graph()
    rows = graph.query(INGREDIENT_QUERY, params={"menu_names": menu_names})

    items = []
    total_cost = 0

    for row in rows:
        menu_name = row["menu_name"]
        ingredient = row["ingredient_name"]
        if not ingredient:
            continue

        occurrences = menu_occurrences.get(menu_name, 0)
        grams_per_serving = row.get("grams_per_serving") or 0.0
        total_weight_g = round(
            grams_per_serving * PORTION_SCALE * occurrences * patient_count, 1
        )

        price_today = row.get("price_today")
        unit_grams = _parse_unit_to_grams(row.get("unit_str"))
        unit_price_per_g = (
            float(price_today) / unit_grams
            if price_today and unit_grams else None
        )
        estimated_cost = (
            round(total_weight_g * unit_price_per_g)
            if unit_price_per_g is not None else None
        )
        if estimated_cost:
            total_cost += estimated_cost

        items.append({
            "menu_name": menu_name,
            "ingredient": ingredient,
            "servings_used": occurrences,
            "total_weight_g": total_weight_g,
            "product_name": row.get("product_name"),
            # 구매 단위(예: "1kg" 포장) 하나의 실제 판매가 — 프론트에는
            # 참고용 단가로 표시됨. 정밀 계산은 estimated_cost가 담당.
            "unit_price": round(price_today) if price_today else None,
            "estimated_cost": estimated_cost,
        })

    items.sort(key=lambda x: (x["menu_name"], x["ingredient"]))

    return {
        "run_id": run_id,
        "week_range": week_range,
        "total_items": len(items),
        "total_cost": total_cost,
        "items": items,
    }