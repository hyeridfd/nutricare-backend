"""
meal_plan_agent.py  ─  MealPlanAgent 노드 (registry 버전)
"""

import os
import numpy as np
import pandas as pd
import registry
from langchain_neo4j import Neo4jGraph
from optimizer_agent import DAILY_SLOTS, N_DAYS, N_SLOTS
from state import MealPlanState

MEAL_NAMES = ["아침", "점심", "저녁"]
SLOT_CATS  = [("밥","밥"),("국","국"),("주찬","주찬"),
              ("부찬1","부찬"),("부찬2","부찬"),("김치","김치")]

# orchestrator_agent.py의 VIOLATION_THRESH와 동일하게 유지
VIOLATION_THRESH = 1.0


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

    # [수정 — 2026-07-01] 기존에는 f1(영양 위반)이 가장 낮은 해 하나만
    # 고정으로 선택해서, f4(부찬 중복/14일 재등장 억제)가 계산되고 있었음에도
    # 최종 선택에서 완전히 무시되고 있었음. 그 결과 영양은 최적이지만 특정
    # 메뉴(예: 닭가슴살야채볶음, 방어구이)가 28일 안에서 8번씩 반복되는 등
    # 다양성이 크게 떨어지는 해가 계속 뽑히는 문제가 있었음.
    # 이제 f1이 목표치(VIOLATION_THRESH)를 충족하는 해들 중에서는 f4가
    # 가장 낮은(반복이 적은) 해를 선택함. f1을 충족하는 해가 하나도 없는
    # 예외 상황에서는 기존처럼 f1이 최소인 해로 안전하게 폴백함.
    F = result.F
    passing_mask = F[:, 0] <= VIOLATION_THRESH
    if passing_mask.any():
        passing_indices = np.nonzero(passing_mask)[0]
        best_idx = passing_indices[F[passing_indices, 3].argmin()]
        print(f"  [선택 기준] f1 통과 해 {len(passing_indices)}개 중 "
              f"f4(다양성) 최적 해 선택")
    else:
        best_idx = F[:, 0].argmin()
        print(f"  [선택 기준] f1 통과 해 없음 — f1 최소 해로 폴백")

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

    # [수정 — 2026-07-01] 기존에는 "권장재료가 하나라도 포함된 메뉴(슬롯)
    # 개수"를 세고 있었음(최대 6, 밥/국/주찬/부찬1/부찬2/김치 슬롯 단위).
    # 영양사가 실제로 궁금한 건 "이 끼니에 권장 식재료가 몇 종류 들어갔는가"
    # (식재료 단위)이므로, 슬롯을 다 순회해 권장재료를 모으고 중복 제거한
    # 뒤 그 개수를 세도록 변경. 표시 내용도 "메뉴명(재료...)" 형태 대신
    # 식재료 이름만 나열하도록 바꿈.
    def rec_ingredients(row) -> list[str]:
        ingredients: list[str] = []
        for col in ["밥","국","주찬","부찬1","부찬2","김치"]:
            ingredients.extend(recommend_map.get(row[col], []))
        # 순서를 유지하면서 중복만 제거(dict.fromkeys는 삽입 순서 보존)
        return list(dict.fromkeys(ingredients))

    def rec_summary(row):
        ingredients = rec_ingredients(row)
        return ", ".join(ingredients) if ingredients else "-"

    df["권장재료포함메뉴"] = df.apply(rec_summary, axis=1)
    df["권장재료포함수"]   = df.apply(lambda r: len(rec_ingredients(r)), axis=1)

    print(f"[MealPlanAgent] 완료 — {len(df)}행 식단표 생성")

    return {
        "df_menu_records": df.to_dict("records"),   # ← 직렬화 가능
        "df_menu_columns": list(df.columns),
        "recommend_map":   recommend_map,
        "messages":        ["[MealPlanAgent] 28일 식단표 생성 완료"],
    }