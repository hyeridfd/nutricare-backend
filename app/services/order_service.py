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
  3. 그 구간에 쓰인 메뉴명들을 모아 Neo4j에서 식재료별 사용량/단가 조회
  4. (메뉴 등장 횟수 × 1인분 사용량 × PORTION_SCALE × 환자 수)를
     식재료별로 합산해 총 소요량과 금액을 계산

[주의 — 근사치임을 명확히]
  - PersonalizeAgent가 환자별로 조정하는 개인화 배식 ratio(부찬 교체,
    양 조절)는 여기서는 반영하지 않음 — 환자 전원이 "기본 1인분
    (PORTION_SCALE 적용된)"을 먹는다고 가정한 근사 발주량임. 실제 필요량과
    다소 차이가 날 수 있으므로, 발주 담당자가 여유분을 고려해 참고용으로
    사용해야 함. 더 정밀한 값이 필요하면 servings 테이블(개인별 실제
    배식량)을 기준으로 재계산하는 방식으로 확장 가능.
"""

import os
from app.services.db_clients import get_supabase

PORTION_SCALE = 0.8  # agents/candidate_agent.py의 PORTION_SCALE과 반드시 동일하게 유지

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
           cheapest_p.unit_g AS unit_g
"""


def _get_neo4j_graph():
    from langchain_neo4j import Neo4jGraph
    return Neo4jGraph(
        url=os.getenv("NEO4J_URI"),
        username=os.getenv("NEO4J_USERNAME"),
        password=os.getenv("NEO4J_PASSWORD"),
        database=os.getenv("NEO4J_DATABASE", "neo4j"),
    )


def _week_day_range(week_offset: int) -> tuple[int, int]:
    """week_offset(0~3) → (시작일, 종료일). 28일 식단을 7일씩 4주로 나눔."""
    start = week_offset * 7 + 1
    end = min(start + 6, 28)
    return start, end


def _facility_name(facility_id: str) -> str:
    sb = get_supabase()
    try:
        row = sb.table("facilities").select("name").eq("id", facility_id).single().execute()
        return row.data.get("name", "") if row.data else ""
    except Exception:
        return ""


def build_order_data(run_id: str, week_offset: int = 0) -> dict:
    """
    발주 미리보기(JSON)와 엑셀 생성 양쪽에서 공유하는 핵심 계산.
    반환: {facility_name, day_range, patient_count, items, total_amount_won}
    """
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
            "facility_name": _facility_name(facility_id),
            "day_range": [start_day, end_day],
            "patient_count": patient_count,
            "items": [],
            "total_amount_won": 0,
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

    # 식재료별로 합산
    ingredient_totals: dict[str, dict] = {}
    for row in rows:
        ing = row["ingredient_name"]
        if not ing:
            continue
        menu_name = row["menu_name"]
        occurrences = menu_occurrences.get(menu_name, 0)
        grams_per_serving = row.get("grams_per_serving") or 0.0

        price_today = row.get("price_today")
        unit_g = row.get("unit_g")
        unit_price_per_g = (
            float(price_today) / float(unit_g)
            if price_today and unit_g else 0.0
        )

        total_grams_for_menu = (
            grams_per_serving * PORTION_SCALE * occurrences * patient_count
        )

        entry = ingredient_totals.setdefault(ing, {
            "ingredient_name": ing,
            "product_name": row.get("product_name") or "-",
            "unit_price_per_g": unit_price_per_g,
            "total_grams": 0.0,
        })
        entry["total_grams"] += total_grams_for_menu
        # 여러 상품이 매핑된 재료라도 최저가 하나로 통일(첫 값 유지)
        if not entry["unit_price_per_g"] and unit_price_per_g:
            entry["unit_price_per_g"] = unit_price_per_g

    items = []
    total_amount = 0
    for ing, data in sorted(ingredient_totals.items()):
        total_kg = round(data["total_grams"] / 1000, 2)
        amount = round(data["total_grams"] * data["unit_price_per_g"])
        total_amount += amount
        items.append({
            "ingredient_name": data["ingredient_name"],
            "product_name": data["product_name"],
            "spec": f"{round(data['total_grams'] / 1000, 1)}kg 상당",
            "quantity_kg": total_kg,
            "unit_price_won_per_kg": round(data["unit_price_per_g"] * 1000),
            "amount_won": amount,
        })

    return {
        "facility_name": _facility_name(facility_id),
        "day_range": [start_day, end_day],
        "patient_count": patient_count,
        "items": items,
        "total_amount_won": total_amount,
    }