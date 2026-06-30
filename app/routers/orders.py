"""
routers/orders.py — 발주 엑셀 생성 페이지용 API
====================================================
승인된 식단표(meal_plan_slots)를 기준으로 필요한 식자재 수량을 집계합니다.
실제 단가는 Neo4j의 Product 노드(price_today)에서 가져옵니다.
"""

from fastapi import APIRouter, HTTPException
from collections import defaultdict

from app.services.db_clients import get_supabase, get_neo4j

router = APIRouter()


@router.get("/preview")
def get_order_preview(run_id: str, week_offset: int = 0):
    """
    PAGE 5 발주 미리보기 표.
    week_offset=0이면 1~7일차, 1이면 8~14일차 식으로 28일을 4주로 나눠 집계.
    """
    sb = get_supabase()

    day_start = week_offset * 7 + 1
    day_end   = day_start + 6

    slots = sb.table("meal_plan_slots").select("*") \
              .eq("run_id", run_id) \
              .gte("day_number", day_start).lte("day_number", day_end) \
              .execute().data

    if not slots:
        raise HTTPException(404, "해당 주차의 식단 데이터가 없습니다.")

    # 메뉴별 등장 횟수 집계 (밥/국/주찬/부찬1/부찬2/김치)
    menu_count = defaultdict(int)
    for s in slots:
        for col in ["rice", "soup", "main_dish", "side_dish_1", "side_dish_2", "kimchi"]:
            menu_count[s[col]] += 1

    # Neo4j에서 메뉴별 재료/단가 조회 (HAS_INGREDIENT → MAPPED_TO Product)
    graph = get_neo4j()
    menu_names = list(menu_count.keys())

    query = """
        UNWIND $menu_names AS mname
        MATCH (f:Food {title: mname})-[hi:HAS_INGREDIENT]->(r:Recipe)
        OPTIONAL MATCH (r)-[:MAPPED_TO]->(p:Product)
        WITH mname, r, hi, p
        ORDER BY p.price_today ASC
        WITH mname, r, hi, head(collect(p)) AS cheapest_p
        RETURN mname,
               r.title AS ingredient,
               hi.nutri_weight AS weight_per_serving,
               cheapest_p.title AS product_name,
               cheapest_p.price_today AS unit_price,
               cheapest_p.unit_g AS unit_g
    """
    ingredient_rows = graph.query(query, params={"menu_names": menu_names})

    order_items = []
    for row in ingredient_rows:
        servings = menu_count.get(row["mname"], 0)
        weight_per_serving = row.get("weight_per_serving") or 0
        total_weight_g = weight_per_serving * servings

        unit_price = row.get("unit_price")
        unit_g     = row.get("unit_g") or 1
        total_cost = round((unit_price / unit_g) * total_weight_g, 0) if unit_price else None

        order_items.append({
            "menu_name":      row["mname"],
            "ingredient":     row["ingredient"],
            "servings_used":  servings,
            "total_weight_g": round(total_weight_g, 1),
            "product_name":   row.get("product_name"),
            "unit_price":     unit_price,
            "estimated_cost": total_cost,
        })

    total_cost_sum = sum(i["estimated_cost"] for i in order_items if i["estimated_cost"])

    return {
        "run_id": run_id,
        "week_range": f"{day_start}~{day_end}일차",
        "total_items": len(order_items),
        "total_cost": total_cost_sum,
        "items": order_items,
    }
