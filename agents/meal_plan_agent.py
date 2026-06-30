"""
meal_plan_agent.py  ─  MealPlanAgent 노드 (registry 버전)
"""

import os
import pandas as pd
import registry
from langchain_neo4j import Neo4jGraph
from optimizer_agent import DAILY_SLOTS, N_DAYS, N_SLOTS
from state import MealPlanState

MEAL_NAMES = ["아침", "점심", "저녁"]
SLOT_CATS  = [("밥","밥"),("국","국"),("주찬","주찬"),
              ("부찬1","부찬"),("부찬2","부찬"),("김치","김치")]


def _get_recommend_map(graph, diseases: list, menu_names: list) -> dict:
    # [수정] ri.name → ri.title
    # 원인: Recipe 노드는 'title' 속성만 갖고 있고 'name' 속성이 없음
    # (이전 진단에서 확인된 Recipe 노드 예: {"id": "Recipe_R318-...",
    # "title": "소금"} — name 필드 자체가 존재하지 않음). candidate_agent.py
    # 메인 쿼리는 일관되게 r.title을 쓰는데 이 함수만 ri.name으로 되어 있어
    # 항상 null만 모여 recommended_ingredients가 비고, 그 결과
    # 권장재료포함메뉴/권장재료포함수가 항상 "-"/0으로 나오고 있었음.
    query = """
        UNWIND $diseases AS disease_name
        MATCH (d:Disease {name: disease_name})-[:RECOMMENDED_INGREDIENT]->(ri:Recipe)
        MATCH (f:Food)-[:HAS_INGREDIENT]->(ri)
        WHERE f.title IN $menu_names
        RETURN f.title AS menu_name,
               collect(DISTINCT ri.title) AS recommended_ingredients
    """
    results = graph.query(query, params={
        "diseases": diseases, "menu_names": menu_names
    })
    return {r["menu_name"]: r["recommended_ingredients"] for r in results}


def meal_plan_agent(state: MealPlanState) -> dict:
    print("\n[MealPlanAgent] 식단표 생성 시작...")

    # ── registry에서 pymoo Result 꺼내기 ─────────────────────
    result     = registry.get(state["nsga_result_key"])
    pool       = state["pool"]
    best_idx   = result.F[:, 0].argmin()
    best_chrom = result.X[best_idx]
    best_F     = result.F[best_idx]

    print(f"  선택된 해: f1={best_F[0]:.4f} f2={best_F[1]:.4f} "
            f"f3={-best_F[2]:.1f} f4={best_F[3]:.4f}")

    # ── 28일 식단표 생성 ──────────────────────────────────────
    rows = []
    for day in range(N_DAYS):
        base = day * N_SLOTS
        for meal_idx, meal_name in enumerate(MEAL_NAMES):
            slot_base = meal_idx * 6
            row = {"일차": f"{day+1}일", "끼니": meal_name}
            meal_energy = meal_sodium = meal_protein = meal_cost = 0.0

            for s, (slot_name, cat) in enumerate(SLOT_CATS):
                chrom_idx = base + slot_base + s
                menu = pool[cat][int(best_chrom[chrom_idx]) % len(pool[cat])]
                row[slot_name]  = menu["menu_name"]
                meal_energy  += menu["energy"]
                meal_sodium  += menu["sodium"]
                meal_protein += menu["protein"]
                meal_cost    += menu["cost"]

            row["열량(kcal)"] = round(meal_energy, 1)
            row["나트륨(mg)"] = round(meal_sodium,  1)
            row["단백질(g)"]  = round(meal_protein, 1)
            row["비용(원)"]   = round(meal_cost,     0)
            rows.append(row)

    df = pd.DataFrame(rows, columns=[
        "일차","끼니","밥","국","주찬","부찬1","부찬2","김치",
        "열량(kcal)","나트륨(mg)","단백질(g)","비용(원)",
    ])

    # ── 권장재료 매핑 ─────────────────────────────────────────
    all_menus = list(set(
        m for col in ["밥","국","주찬","부찬1","부찬2","김치"]
        for m in df[col].unique()
    ))

    try:
        graph_db = Neo4jGraph(
            url=os.getenv("NEO4J_URI"),
            username=os.getenv("NEO4J_USERNAME"),
            password=os.getenv("NEO4J_PASSWORD"),
            database=os.getenv("NEO4J_DATABASE", "neo4j"),
        )
        recommend_map = _get_recommend_map(graph_db, state["diseases"], all_menus)
    except Exception as e:
        print(f"  [경고] 권장재료 조회 실패: {e}")
        recommend_map = {}

    def rec_summary(row):
        parts = []
        for col in ["밥","국","주찬","부찬1","부찬2","김치"]:
            ing = recommend_map.get(row[col], [])
            if ing:
                parts.append(f"{row[col]}({', '.join(ing)})")
        return " / ".join(parts) if parts else "-"

    df["권장재료포함메뉴"] = df.apply(rec_summary, axis=1)
    df["권장재료포함수"]   = df.apply(
        lambda r: sum(1 for col in ["밥","국","주찬","부찬1","부찬2","김치"]
                      if recommend_map.get(r[col])), axis=1
    )

    print(f"[MealPlanAgent] 완료 — {len(df)}행 식단표 생성")

    return {
        "df_menu_records": df.to_dict("records"),   # ← 직렬화 가능
        "df_menu_columns": list(df.columns),
        "recommend_map":   recommend_map,
        "messages":        ["[MealPlanAgent] 28일 식단표 생성 완료"],
    }