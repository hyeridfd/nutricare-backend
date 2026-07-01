"""
candidate_agent.py  ─  CandidateAgent 노드
==========================================
Neo4j Graph-RAG 기반으로 질환별 후보 메뉴 풀을 생성합니다.

[변경 사항 1 — 질환 교집합 방식]
기존: $diseases 리스트를 한 번에 넘겨 forbidden/recommended를 합집합으로 처리
      → "당뇨엔 좋은데 고혈압엔 안 좋은" 메뉴가 통과할 수 있었음
변경: 질환별로 쿼리를 각각 실행한 뒤, Python에서 menu_name 기준 교집합을 취함
      → 모든 질환에 동시에 안전한 메뉴만 시설 공통 풀에 남음 (메뉴명 자체는 변경 없음)

단일 질환만 있으면 기존과 동일하게 동작합니다(교집합 대상이 1개뿐이므로).

[변경 사항 2 — 양념류 화이트리스트 (방향 A)]
원인: 소금/간장/참기름/식용유 등 범용 조미료는 같은 질환에 대해
      RECOMMENDED_INGREDIENT와 FORBIDDEN_INGREDIENT 양쪽에 동시에 연결된 경우가
      많음(예: 당뇨병 forbidden 195개 중 143개가 recommended와 중복).
      이는 데이터 오염이 아니라 "관점에 따라 권장/주의가 둘 다 근거 있는" 양념의
      특성이지만, NONE(forbidden) 조건이 이진 판정이라 양념 하나만 forbidden에
      걸려도 그 양념이 들어간 거의 모든 메뉴가 자동 탈락하는 문제가 있었음
      (실측: 당뇨병 단독 조회 시 국/주찬/부찬 카테고리가 0~수개로 붕괴).
해결: 모든 한식 메뉴의 베이스인 범용 조미료를 SEASONING_WHITELIST로 정의하고,
      forbidden 판정 시 이 화이트리스트에 속한 Recipe는 제외하고 검사함.
      recommended 판정에는 영향 없음(화이트리스트 재료가 recommended이면 여전히
      그 메뉴를 추천 사유로 인정).

[변경 사항 3 — 신장질환을 교집합 대상에서 제외]
원인: 신장질환은 forbidden 249개 중 170개(68%)가 recommended와도 겹침.
      그런데 겹치는 항목이 양념류가 아니라 가자미/새우살/오징어/달걀/사과 같은
      "단백질·과일 주재료"임(샘플 확인 완료). 이건 양념처럼 화이트리스트로
      빼버리면 안 됨 — 신장질환은 그 재료 자체가 금기가 아니라 "얼마나 먹느냐"
      (인/칼륨/단백질 총량)가 문제이기 때문에, 재료 단위 이진 배제가 아니라
      끼니 단위 정량 판단이 필요함.
      실측 결과 신장질환을 교집합에 포함시키면 주찬이 3개로 붕괴되어
      (고혈압 30개·당뇨 25개인데 신장이 3개로 전체를 끌어내림) NSGA-II가
      탐색할 공간이 거의 없어지고, PersonalizeAgent의 부찬 대체도 같은 메뉴로
      계속 수렴하는 문제가 있었음.
해결: 신장질환은 INTERSECTION_EXCLUDED_DISEASES에 포함시켜 CandidateAgent
      교집합 단계에서는 제외(단독 후보 조회는 로그용으로 계속 수행).
      대신 신장질환자의 단백질/칼륨 제한은 PersonalizeAgent가 끼니 합산
      기준으로 ratio 조정 → 부찬 교체 순으로 보정함(patient_profile_final.py의
      신장질환 protein_max, personalize_agent.py의 _check_violations가 이미
      이 역할을 하도록 구현되어 있음 — 별도 추가 작업 불필요).
      즉 "공통 최적화 단계에서는 느슨하게, 개인화 단계에서 정밀하게"라는
      기존 2단계 설계 철학을 신장질환에도 일관되게 적용.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from langchain_neo4j import Neo4jGraph
from state import MealPlanState
from preference_update_agent import load_pool_scores


# CandidateAgent 교집합 계산에서 제외할 질환.
# 단독 후보 조회(로그 출력)는 계속 수행하되, 교집합에는 포함시키지 않음.
# 이 질환들의 제약은 PersonalizeAgent가 끼니 단위 정량 보정으로 대신 처리.
INTERSECTION_EXCLUDED_DISEASES = {"신장질환", "신장_투석"}


# ── 양념류 화이트리스트 ──────────────────────────────────────
# forbidden 판정에서 제외할 범용 조미료/베이스 재료.
# 필요 시 시설 데이터 기준으로 추가/조정 가능.
SEASONING_WHITELIST = [
    "소금", "굵은소금", "맛소금",
    "간장", "간장(재래)", "진간장", "국간장", "양조간장",
    "참기름", "들기름", "식용유", "올리브유", "포도씨유",
    "고춧가루", "후춧가루", "후추", "마늘", "다진마늘",
    "생강", "다진생강", "설탕", "황설탕", "식초", "물엿",
    "참깨", "들깨", "깨소금", "맛술", "전분", "전분가루",
    "고추장", "된장", "쌀", "물",
]


# 질환 단건만 받도록 변경 ($diseases → $disease 단일 문자열)
CYPHER_QUERY = """
    CALL () {
        WITH $disease AS d_name
        MATCH (d:Disease {name: d_name})
        OPTIONAL MATCH (d)-[:FORBIDDEN_INGREDIENT]->(fi:Recipe)
        OPTIONAL MATCH (d)-[:RECOMMENDED_INGREDIENT]->(ri:Recipe)
        RETURN collect(DISTINCT elementId(fi)) AS forbidden_ids,
               collect(DISTINCT elementId(ri)) AS recommended_ids
    }
    MATCH (f:Food)-[:CATEGORY_IS]->(mc:Meal_Category)
    WHERE (
        NONE(r IN [(f)-[:HAS_INGREDIENT]->(recipe)|recipe]
             WHERE elementId(r) IN forbidden_ids
               AND NOT r.title IN $seasoning_whitelist)
        AND ANY(r IN [(f)-[:HAS_INGREDIENT]->(recipe)|recipe]
            WHERE elementId(r) IN recommended_ids)
    ) OR f.title IN ['쌀밥','배추김치']
    MATCH (f)-[hi:HAS_INGREDIENT]->(r:Recipe)-[:CONTAINS]->(n:Nutrition)
    WITH f, mc, r, hi,
        toFloat(coalesce(n.energy_kcal,0))       *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_energy,
        toFloat(coalesce(n.protein_g,0))         *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_protein,
        toFloat(coalesce(n.fat_g,0))             *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_fat,
        toFloat(coalesce(n.sugar_g,0))           *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_sugar,
        toFloat(coalesce(n.fiber_g,0))           *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_fiber,
        toFloat(coalesce(n.sodium_mg,0))         *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_sodium,
        toFloat(coalesce(n.carbo_g,0))           *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_carbo,
        toFloat(coalesce(n.saturated_fat_g,0))   *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_saturated_fat,
        toFloat(coalesce(n.potassium_mg,0))      *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_potassium,
        toFloat(coalesce(n.vitD_ug,0))           *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_vitD,
        toFloat(coalesce(n.iron_mg,0))           *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_iron,
        toFloat(coalesce(n.vitA_rae_ug,0))       *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_vitA,
        toFloat(coalesce(n.thiamin_mg,0))        *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_thiamin,
        toFloat(coalesce(n.vitC_mg,0))           *toFloat(coalesce(hi.nutri_weight,0))/100 AS r_vitC,
        hi.nutri_weight AS nutri_w
    OPTIONAL MATCH (r)-[:MAPPED_TO]->(p:Product)
    WITH f, mc, r, r_energy, r_protein, r_fat, r_sugar, r_fiber,
         r_sodium, r_carbo, r_saturated_fat, r_potassium, r_vitD,
         r_iron, r_vitA, r_thiamin, r_vitC, nutri_w,
         p ORDER BY p.price_today ASC
    WITH f, mc, r, r_energy, r_protein, r_fat, r_sugar, r_fiber,
         r_sodium, r_carbo, r_saturated_fat, r_potassium, r_vitD,
         r_iron, r_vitA, r_thiamin, r_vitC, nutri_w,
         head(collect(p)) AS cheapest_p
    WITH f, mc, r, r_energy, r_protein, r_fat, r_sugar, r_fiber,
         r_sodium, r_carbo, r_saturated_fat, r_potassium, r_vitD,
         r_iron, r_vitA, r_thiamin, r_vitC, nutri_w,
         CASE WHEN cheapest_p IS NOT NULL
              THEN toFloat(cheapest_p.price_today)/toFloat(coalesce(cheapest_p.unit_g,1))
              ELSE 0.0 END AS unit_price
    WITH f, mc,
         sum(r_energy) AS total_energy, sum(r_protein) AS total_protein,
         sum(r_fat) AS total_fat,       sum(r_sugar) AS total_sugar,
         sum(r_fiber) AS total_fiber,   sum(r_sodium) AS total_sodium,
         sum(r_carbo) AS total_carbo,   sum(r_saturated_fat) AS total_saturated_fat,
         sum(r_potassium) AS total_potassium, sum(r_vitD) AS total_vitD,
         sum(r_iron) AS total_iron, sum(r_vitA) AS total_vitA,
         sum(r_thiamin) AS total_thiamin, sum(r_vitC) AS total_vitC,
         sum(unit_price * nutri_w) AS total_cost, sum(nutri_w) AS total_weight
    RETURN mc.name AS category, f.title AS menu_name,
           round(total_energy,2) AS energy,       round(total_protein,2) AS protein,
           round(total_fat,2) AS fat,             round(total_sugar,2) AS sugar,
           round(total_fiber,2) AS fiber,         round(total_sodium,2) AS sodium,
           round(total_carbo,2) AS carb,          round(total_saturated_fat,2) AS sat_fat,
           round(total_potassium,2) AS potassium, round(total_vitD,2) AS vit_d,
           round(total_iron,2) AS iron,           round(total_vitA,2) AS vit_a,
           round(total_thiamin,3) AS thiamin,     round(total_vitC,2) AS vit_c,
           round(total_cost,0) AS cost,           round(total_weight,1) AS weight
    ORDER BY mc.name, total_energy ASC
"""

CATEGORIES = ["밥", "국", "주찬", "부찬", "김치"]

# ── 1인분 양 축소 비율 ────────────────────────────────────────
# 원인: 시설 DB의 메뉴 1인분 양 자체가 많게 설계되어 있어, 끼니 합산
# 나트륨이 기준 대비 구조적으로 초과함(실측: NSGA-II 최적화 후에도
# 끼니당 나트륨 위반비율 평균 1.03 — 기준의 약 2배. 메뉴 조합을 아무리
# 바꿔도 NSGA-II로는 해소 불가능한 수준이었음).
# 해결: pool 생성 단계에서 모든 영양값과 weight(실제 배식 중량)에
# PORTION_SCALE을 동일하게 곱해, 메뉴 구성 비율은 유지하면서 1인분
# 절대량만 줄임. cost도 weight에 비례해 같이 줄여 일관성 유지.
# PersonalizeAgent의 ratio 조정과는 별개 레이어: 여기서 "기본 1인분"
# 자체를 현실적인 크기로 맞추고, PersonalizeAgent는 그 위에서 개인별
# 추가 조정(질환 위반 보정)을 함.
PORTION_SCALE = 0.8

# 양에 비례해서 같이 줄여야 하는 필드(영양값 + 중량 + 비용).
# category, menu_name처럼 텍스트 필드와 preference_score(0~1 점수,
# 양과 무관)는 제외.
_SCALABLE_FIELDS = [
    "energy", "protein", "fat", "sugar", "fiber", "sodium", "carb",
    "sat_fat", "potassium", "vit_d", "iron", "vit_a", "thiamin", "vit_c",
    "cost", "weight",
]


def _apply_portion_scale(row: dict, scale: float = PORTION_SCALE) -> dict:
    """메뉴 1건의 영양값/중량/비용에 축소 비율을 적용한 새 dict 반환."""
    scaled = dict(row)
    for field in _SCALABLE_FIELDS:
        if field in scaled and scaled[field] is not None:
            scaled[field] = round(scaled[field] * scale, 3)
    return scaled


def _query_single_disease(graph: Neo4jGraph, disease: str) -> dict:
    """단일 질환에 대한 후보 풀(카테고리별 menu_name -> row dict)을 반환.
    영양값/중량/비용은 PORTION_SCALE만큼 축소해서 저장함."""
    results = graph.query(
        CYPHER_QUERY,
        params={"disease": disease, "seasoning_whitelist": SEASONING_WHITELIST},
    )

    pool: dict = {cat: {} for cat in CATEGORIES}  # cat -> {menu_name: row_dict}
    for row in results:
        cat = row["category"]
        if cat in pool:
            pool[cat][row["menu_name"]] = _apply_portion_scale(dict(row))
    return pool


def candidate_agent(state: MealPlanState) -> dict:
    print("\n[CandidateAgent] 후보 메뉴 조회 시작 (질환별 교집합 + 양념 화이트리스트)...")
    print(f"  양념 화이트리스트: {len(SEASONING_WHITELIST)}개 적용 "
          f"({', '.join(SEASONING_WHITELIST[:6])} 등)")
    print(f"  1인분 양 축소: {PORTION_SCALE}배 적용 (영양값·중량·비용 동일 비율 축소)")

    graph = Neo4jGraph(
        url=os.getenv("NEO4J_URI"),
        username=os.getenv("NEO4J_USERNAME"),
        password=os.getenv("NEO4J_PASSWORD"),
        database=os.getenv("NEO4J_DATABASE", "neo4j"),
    )

    diseases = state["diseases"]
    if not diseases:
        raise ValueError("[CandidateAgent] state['diseases']가 비어 있습니다.")

    # ── 교집합 대상과 제외 대상 분리 ──────────────────────────
    intersection_diseases = [d for d in diseases if d not in INTERSECTION_EXCLUDED_DISEASES]
    excluded_diseases     = [d for d in diseases if d in INTERSECTION_EXCLUDED_DISEASES]

    if not intersection_diseases:
        # 모든 질환이 제외 대상이면(예: 신장질환만 있는 경우) 교집합 의미가 없으므로
        # 제외 대상 중 첫 번째 질환을 그대로 사용 (단독 풀)
        intersection_diseases = excluded_diseases[:1]
        excluded_diseases = excluded_diseases[1:]
        print(f"  [안내] 교집합 가능한 질환이 없어 '{intersection_diseases[0]}' 단독 풀을 사용합니다.")

    # ── ① 질환별로 따로 쿼리 (제외 대상도 로그용으로 단독 조회) ─
    # [수정 — 2026-07-01] 질환별 쿼리는 서로 완전히 독립적인데(각자 다른
    # Disease 노드 기준으로 조회) 기존에는 for문으로 하나씩 순차 실행해
    # 질환이 2개 이상이면 그만큼 대기 시간이 배로 늘어났음. Neo4j 드라이버는
    # 멀티스레드 동시 호출을 지원하므로 ThreadPoolExecutor로 병렬 실행.
    per_disease_pools = {}
    with ThreadPoolExecutor(max_workers=min(len(diseases), 4)) as executor:
        future_to_disease = {
            executor.submit(_query_single_disease, graph, disease): disease
            for disease in diseases
        }
        for future, disease in future_to_disease.items():
            per_disease_pools[disease] = future.result()

    for disease in diseases:
        sizes = {cat: len(m) for cat, m in per_disease_pools[disease].items()}
        tag = " (교집합 제외 — PersonalizeAgent에서 정량 보정)" if disease in excluded_diseases else ""
        print(f"  [{disease}] 단독 후보: {sizes}{tag}")

    # ── ② 카테고리별로 menu_name 교집합 (제외 대상은 빠짐) ─────
    # 화이트리스트(쌀밥/배추김치)는 모든 질환 쿼리에 이미 포함되어 있어
    # 교집합에서도 자동으로 보존됨.
    pool: dict = {cat: [] for cat in CATEGORIES}

    for cat in CATEGORIES:
        name_sets = [set(per_disease_pools[d][cat].keys()) for d in intersection_diseases]
        common_names = set.intersection(*name_sets) if name_sets else set()

        # 상세 정보는 첫 번째 질환의 결과를 기준으로 채움
        # (영양/가격 값은 메뉴 고유 속성이라 질환별로 달라지지 않음)
        base_disease = intersection_diseases[0]
        for name in common_names:
            pool[cat].append(per_disease_pools[base_disease][cat][name])

        avg_n = sum(len(s) for s in name_sets) / len(name_sets) if name_sets else 0
        print(f"  [교집합] '{cat}': {len(common_names)}개 "
              f"(질환별 평균 {avg_n:.0f}개 → 교집합 후)")

    # ── ③ 카테고리별 메뉴 수 검증 ─────────────────────────────
    for cat, menus in pool.items():
        if len(menus) == 0:
            # 어느 질환이 0개(또는 최소)인지 짚어서 디버깅 시간을 줄임
            per_disease_counts = {
                d: len(per_disease_pools[d][cat]) for d in diseases
            }
            zero_diseases = [d for d in intersection_diseases
                              if per_disease_counts.get(d, 0) == 0]

            if zero_diseases:
                hint = (f"단독 후보부터 0개인 질환(교집합 대상): {zero_diseases} "
                        f"→ 교집합 문제가 아니라 해당 질환의 RECOMMENDED_INGREDIENT/"
                        f"FORBIDDEN_INGREDIENT 관계가 Neo4j에 비어있거나 누락됨")
            else:
                hint = (f"교집합 대상 질환({intersection_diseases})은 단독 후보가 있으나 "
                        f"({per_disease_counts}) 교집합이 비어있음 → 서로 겹치는 메뉴가 없음. "
                        f"제외 대상({excluded_diseases})은 영향 없음(PersonalizeAgent에서 처리)")

            raise ValueError(
                f"[CandidateAgent] '{cat}' 카테고리 교집합 후보가 0개입니다.\n"
                f"  질환별 단독 후보 수(전체): {per_disease_counts}\n"
                f"  교집합 계산 대상: {intersection_diseases} | 제외: {excluded_diseases}\n"
                f"  → {hint}"
            )

    # ── ④ 저장된 선호도 점수 적용 ─────────────────────────────
    saved_scores = load_pool_scores()
    if saved_scores:
        for cat, menus in pool.items():
            for m in menus:
                if m["menu_name"] in saved_scores:
                    m["preference_score"] = saved_scores[m["menu_name"]]
        print(f"  [CandidateAgent] 저장된 선호도 점수 {len(saved_scores)}건 적용")

    summary = {cat: len(m) for cat, m in pool.items()}
    excl_note = f" | 교집합 제외: {excluded_diseases}(PersonalizeAgent에서 정량 보정)" if excluded_diseases else ""
    print(f"[CandidateAgent] 완료 (교집합 대상 {intersection_diseases}{excl_note}): {summary}")

    return {
        "pool": pool,
        "messages": [
            f"[CandidateAgent] 교집합 {intersection_diseases} 풀 생성 완료 {summary}"
            f"{excl_note}"
        ],
    }