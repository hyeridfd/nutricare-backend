"""
personalize_agent.py  ─  PersonalizeAgent 노드
================================================
[변경 사항 — 양(ratio) 조절 우선 + 부찬 교체(질환→선호도 순) + 사유 태깅]

배경: 시설 DB의 메뉴 1인분 양 자체가 많게 잡혀 있어, 끼니 합산 나트륨이
      평균 1,300~1,800mg으로 나옴. 고혈압 끼니 기준(800mg 미만)을 적용하면
      거의 모든 고혈압 환자가 위반으로 잡히는 상황(실측 H형 99.6% 위반).
      메뉴 자체(밥/국/주찬/김치)를 바꾸는 대신, 1인분 양을 줄이는 쪽이
      훨씬 현실적이고 운영 부담도 작음.

설계: 세 단계 패스.

  ① ratio 조정 — 끼니 합산 영양값이 "over" 위반(나트륨/에너지 상한 등)이면
     ratio를 낮춰서 모든 over 위반이 해소되는 가장 큰 ratio를 계산.
     단 energy_min(고령자 최소 보장 500kcal) 밑으로는 못 내려가게 하한선 적용.
     ratio만으로 over 위반이 다 해소되면 끝 — 메뉴는 전혀 안 건드림.

  ② 부찬 슬롯 교체 — 질환 위반 보정 (①로 못 푸는 경우만, 안전 최우선)
     - ratio 하한(energy_min) 때문에 over 위반이 ①만으로 안 풀리는 경우
     - 애초에 "under" 위반(예: 칼륨 부족, 식이섬유 부족, 치매 boost_nutrients
       부족)은 양을 줄이는 게 아니라 늘리는 방향이라 ratio로 해결 불가
     이 두 경우에 한해, 부찬1/부찬2 중 위반 기여도가 큰 슬롯만 교체.
     밥/국/주찬/김치는 절대 교체하지 않음(시설 조리 표준 유지).

     [수정 — 2026-07-01] 기존에는 조건을 만족하는 후보 중 "가장 좋은 값
     하나"(candidates[0])를 항상 결정론적으로 골라서, 같은 질환유형
     환자 전원에게 28일 내내 같은 대체 메뉴 하나만 반복되는 문제가 있었음
     (예: 나트륨 초과 → 전원 항상 '숙주액젓무침'). 이제 후보를 고를 때
     해당 환자에게 최근 RECENT_LOOKBACK_DAYS일(3주) 안에 이미 대체로
     사용한 메뉴는 제외하고, 조건을 만족하는 상위 후보들 중에서 무작위로
     선택함. 최근 이력을 다 제외하고도 후보가 없는 극단적인 경우(메뉴 풀이
     너무 작을 때)에는 안전(위반 해소)을 우선해 이력을 무시하고서라도
     최선의 후보를 반환함.

  ③ 부찬 슬롯 교체 — 선호도 기반 (①②에서 안 바뀐 부찬 슬롯에 한해)
     개인 선호도 < PERSONAL_DISLIKE AND 시설 전체 기피 Top N 인 메뉴만 대체.
     ②가 이미 바꾼 슬롯은 절대 건드리지 않음(질환 안전이 기호보다 우선).
     [수정 — 2026-07-01] 이 패스도 같은 3주 최근 이력 회피 + 상위 후보
     무작위 선택 방식을 적용해 반복을 줄임.

각 교체 건은 reason 태그("disease" | "preference")와 위반/기피 상세 사유를
함께 기록해 report_agent.py가 정확한 라벨("H형 나트륨 초과 보정" 등)을
표시할 수 있게 함. (이전 버전은 옛 report_agent.py 라벨이 모든 교체를
"~형 기피메뉴 대체"로 통일 표기해, 실제로는 질환 위반 보정인데도 선호도
대체처럼 보이는 불일치가 있었음 — 이번 변경으로 해소)
"""

import random
import pandas as pd
import registry
from state import MealPlanState
from preference_update_agent import FACILITY_DISLIKE_THRESHOLD


SLOTS      = ["밥", "국", "주찬", "부찬1", "부찬2", "김치"]
SLOT_CATS  = {"밥": "밥", "국": "국", "주찬": "주찬",
              "부찬1": "부찬", "부찬2": "부찬", "김치": "김치"}
SWAPPABLE_SLOTS = ["부찬1", "부찬2"]  # 메뉴 교체가 허용되는 슬롯만

# 끼니 합산에 쓰는 영양 필드명 (pool 메뉴 dict의 key와 동일해야 함)
NUTR_FIELDS = ["energy", "protein", "fat", "sugar", "fiber", "sodium",
               "carb", "sat_fat", "potassium", "vit_d",
               "iron", "vit_a", "thiamin", "vit_c"]

RATIO_MIN = 0.6   # 너무 적게 주지 않도록 하한 (serving_agent.py와 동일 범위)
RATIO_MAX = 1.3   # 너무 많이 주지 않도록 상한 (기존 ServingAgent와 동일 범위)

# "over" 위반에 해당하는 필드 = ratio를 낮춰서 줄일 수 있는 항목
RATIO_REDUCIBLE_FIELDS = {"energy", "sodium", "sugar", "sat_fat", "fat", "protein"}

# [추가 — 2026-07-01] "under" 위반이지만 절대량 기준이라 ratio를 늘려서도
# 해소 가능한 항목. sugar/sat_fat/fat처럼 "열량 대비 비율"로 판정하는
# 항목은 ratio를 곱해도 분자·분모가 같이 스케일되어 비율 자체가 안 바뀌므로
# (수학적으로 ratio로 해결 불가) 여기 포함하지 않음 — 그런 위반은 반드시
# 메뉴 구성 자체를 바꿔야 하므로 ②단계(부찬 교체)로 넘어감.
RATIO_BOOSTABLE_UNDER_FIELDS = {"potassium", "fiber"}

# ratio를 곱해 스케일링할 전체 필드(위반 판정 재계산 시 사용).
RATIO_SCALABLE_FIELDS = RATIO_REDUCIBLE_FIELDS | RATIO_BOOSTABLE_UNDER_FIELDS

# ── 선호도 패스(③) 파라미터 ──────────────────────────────────
PERSONAL_DISLIKE  = 0.6   # 개인 선호도 점수 임계값 (이하면 기피)
FACILITY_DISLIKE  = FACILITY_DISLIKE_THRESHOLD
MAX_DISLIKE_MENUS = 5
ALT_MIN_SCORE     = 0.5

# ── 대체 메뉴 다양성 파라미터 ─────────────────────────────────
# [추가 — 2026-07-01] 같은 환자에게 최근 N일 안에 이미 대체로 쓴 메뉴는
# 다시 고르지 않도록 함(3주 = 21일).
RECENT_LOOKBACK_DAYS = 21
# 조건을 만족하는 후보 중 상위 몇 개 안에서 무작위로 고를지
ALT_CANDIDATE_POOL = 5


def _day_num(day_str) -> int:
    """'1일' → 1. 파싱 실패 시 0(항상 최근 이력으로 취급되지 않도록 방어)."""
    try:
        return int(str(day_str).replace("일", ""))
    except (TypeError, ValueError):
        return 0


# ════════════════════════════════════════════════════════════
# 공통 유틸
# ════════════════════════════════════════════════════════════
def _sum_meal_nutrition(menu_by_slot: dict) -> dict:
    total = {k: 0.0 for k in NUTR_FIELDS}
    for menu in menu_by_slot.values():
        for k in NUTR_FIELDS:
            total[k] += menu.get(k, 0) or 0
    return total


def _check_violations(total: dict, c) -> list[dict]:
    """
    constraint(c) 대비 위반 항목 리스트.
    각 항목: {"field": ..., "direction": "over"|"under"}
    """
    violations = []
    energy = total.get("energy", 0) or 1e-6

    def add(field, direction):
        violations.append({"field": field, "direction": direction})

    if c.energy_min and total["energy"] < c.energy_min:
        add("energy", "under")
    if c.energy_max and total["energy"] > c.energy_max:
        add("energy", "over")

    if c.protein_min and total["protein"] < c.protein_min:
        add("protein", "under")
    if c.protein_max and total["protein"] > c.protein_max:
        add("protein", "over")

    if c.sodium_max and total["sodium"] > c.sodium_max:
        add("sodium", "over")

    if c.potassium_min and total["potassium"] < c.potassium_min:
        add("potassium", "under")

    if c.fiber_min and total["fiber"] < c.fiber_min:
        add("fiber", "under")

    if c.sugar_max:
        if (total["sugar"] * 4) / energy > 0.10:
            add("sugar", "over")

    if c.sat_fat_max:
        limit = 0.10 if c.sugar_max else 0.07  # 당뇨=10%, 그 외(고혈압 등)=7%
        if (total["sat_fat"] * 9) / energy > limit:
            add("sat_fat", "over")

    if c.fat_min or c.fat_max:
        fat_ratio = (total["fat"] * 9) / energy
        if c.fat_min and fat_ratio < 0.15:
            add("fat", "under")
        if c.fat_max and fat_ratio > 0.30:
            add("fat", "over")

    return violations


# ════════════════════════════════════════════════════════════
# ① ratio 조정
# ════════════════════════════════════════════════════════════
def _calc_violation_ratio(total: dict, c, over_violations: list[dict],
                           target_energy: float | None = None) -> float:
    """
    over 위반 항목들을 모두 해소하는 가장 큰 ratio(=가장 적게 줄이는 ratio)를 계산.
    각 필드에 대해 (기준치 / 현재값)을 구하고, 그 중 최솟값을 채택.
    energy_min 하한은 별도로 보호.

    [수정 — 2026-07-01] target_energy(환자 개인별로 산출된 1끼 필요 칼로리,
    BMI/체중 기반)가 주어지면 이것도 후보에 포함함. 기존에는 c.energy_max
    (시설 공통 800kcal 상한)만 봤는데, 그러면 몸이 작은 환자도 큰 환자도
    똑같이 800kcal까지는 허용되는 셈이라 개인별 필요량을 반영하지 못했음.
    이제 "그 환자 개인의 필요 칼로리에 맞추면 몇 배로 줘야 하는지"도 후보에
    넣어서, 더 엄격한(작은) 쪽이 최종 ratio로 채택되게 함.
    """
    candidates = [RATIO_MAX]

    field_limit_map = {
        "energy":  c.energy_max,
        "sodium":  c.sodium_max,
        "protein": c.protein_max,
    }
    for v in over_violations:
        field = v["field"]
        limit = field_limit_map.get(field)
        cur   = total.get(field, 0)
        if limit and cur > 0:
            candidates.append(limit / cur)

    if target_energy and total.get("energy", 0) > 0:
        candidates.append(target_energy / total["energy"])

    # sugar/sat_fat/fat은 "열량비" 위반이라 ratio를 곱해도 비율 자체는 안 바뀜
    # (분자분모 둘 다 ratio배 되므로) → ratio로 해결 불가, 부찬 교체로 넘김

    ratio = min(candidates)

    # energy_min 하한 보호: 이 ratio로 줄였을 때 energy_min 밑으로 가면 안 됨
    if c.energy_min and total.get("energy", 0) > 0:
        floor_ratio = c.energy_min / total["energy"]
        ratio = max(ratio, floor_ratio)

    return round(max(RATIO_MIN, min(RATIO_MAX, ratio)), 3)


def _calc_boost_ratio(total: dict, c, under_violations: list[dict]) -> float:
    """
    [추가 — 2026-07-01] 칼륨/식이섬유처럼 절대량 기준 "under" 위반은,
    over 위반이 없는 끼니에 한해 ratio를 늘려서(양을 더 줘서) 해소를
    시도함. sugar/sat_fat/fat과 달리 이 항목들은 "열량 대비 비율"이
    아니라 절대량 기준이라 ratio 증가가 실제로 유효함.
    """
    candidates = [RATIO_MIN]
    field_target_map = {
        "potassium": getattr(c, "potassium_min", None),
        "fiber":     getattr(c, "fiber_min", None),
    }
    for v in under_violations:
        field  = v["field"]
        target = field_target_map.get(field)
        cur    = total.get(field, 0)
        if target and cur > 0:
            candidates.append(target / cur)

    ratio = max(candidates)
    return round(max(RATIO_MIN, min(RATIO_MAX, ratio)), 3)


def _violations_after_ratio(total: dict, c, ratio: float) -> list[dict]:
    """ratio 적용 후에도 남는 위반(주로 ratio로 못 줄이는 열량비 항목, 또는
    energy_min 하한에 막혀 못 줄인 over 항목)을 다시 체크."""
    scaled = dict(total)
    # ratio로 조정되는 항목(절대량)만 스케일링. 열량비 항목(sugar/sat_fat/fat 비율)은
    # 분자분모 둘 다 ratio배라 비율 불변 → 그대로 둠(이미 total 기준으로 위반 판정됨).
    for f in RATIO_SCALABLE_FIELDS:
        scaled[f] = total.get(f, 0) * ratio
    for f in NUTR_FIELDS:
        if f not in scaled:
            scaled[f] = total.get(f, 0)
    return _check_violations(scaled, c)


# ════════════════════════════════════════════════════════════
# [현재 미사용 — 2026-07-01] 질환 위반 부찬 교체(구 ②단계)
# 최종 설계에서 당류/단백질/지방/포화지방/나트륨 위반은 ratio로만
# 처리하고 부찬 교체는 하지 않기로 결정되어, _apply_disease_pass에서
# 더 이상 이 함수들을 호출하지 않음. 나중에 "ratio로 다 못 줄인 나머지를
# 다시 부찬 교체로 보정"하는 방식으로 되돌리고 싶을 경우를 위해 삭제하지
# 않고 남겨둠.
# ════════════════════════════════════════════════════════════
def _pick_violation_slot(menu_by_slot: dict, field: str, direction: str):
    """SWAPPABLE_SLOTS(부찬1/부찬2) 안에서만 위반 기여 슬롯을 고름."""
    candidates = {s: m for s, m in menu_by_slot.items() if s in SWAPPABLE_SLOTS}
    if not candidates:
        return None
    items = [(slot, m.get(field, 0) or 0) for slot, m in candidates.items()]
    if direction == "over":
        slot, _ = max(items, key=lambda x: x[1])
    else:
        slot, _ = min(items, key=lambda x: x[1])
    return slot


def _find_better_alt(pool: dict, cat: str, current_menus: set,
                      field: str, direction: str, exclude_name: str,
                      recent_names: set | None = None):
    """
    같은 카테고리(부찬) 안에서 위반을 해소하는 후보들 중, 최근
    RECENT_LOOKBACK_DAYS일(3주) 안에 이 환자에게 이미 대체로 쓰인 메뉴는
    피해서 상위 ALT_CANDIDATE_POOL개 안에서 무작위로 하나 선택.

    [수정 — 2026-07-01] 기존에는 정렬 후 1등(candidates[0])만 항상 반환해
    같은 질환유형 환자 전원·전체 기간에 걸쳐 대체 메뉴가 한두 개로 고정
    반복되는 문제가 있었음. 무작위 선택으로 다양성을 확보하되, 정렬 자체는
    유지해 "위반 해소 효과가 큰 후보 안에서만" 고르도록 함(효과 없는
    후보가 뽑히는 일은 없음).
    """
    recent_names = recent_names or set()

    base_candidates = [
        m for m in pool.get(cat, [])
        if m["menu_name"] not in current_menus and m["menu_name"] != exclude_name
    ]
    if not base_candidates:
        return None

    if direction == "over":
        base_candidates.sort(key=lambda m: m.get(field, 0) or 0)
    else:
        base_candidates.sort(key=lambda m: m.get(field, 0) or 0, reverse=True)

    # 위반 해소 효과가 큰 상위 후보군 안에서만 다양성을 준다
    top_pool = base_candidates[:ALT_CANDIDATE_POOL]
    fresh = [m for m in top_pool if m["menu_name"] not in recent_names]
    if fresh:
        return random.choice(fresh)

    # 상위 후보가 전부 최근 3주 안에 이미 쓰였다면, 후보 풀을 넓혀서 재시도
    wider_fresh = [m for m in base_candidates if m["menu_name"] not in recent_names]
    if wider_fresh:
        return random.choice(wider_fresh[:ALT_CANDIDATE_POOL])

    # 그래도 없으면(메뉴 풀 자체가 작아 3주 회전이 불가능한 극단적인 경우)
    # 반복을 감수하더라도 위반 해소(안전)를 우선해 최선의 후보를 반환
    return base_candidates[0]


FIELD_LABELS = {
    "energy": "열량", "protein": "단백질", "sodium": "나트륨",
    "potassium": "칼륨", "fiber": "식이섬유", "sugar": "당류",
    "sat_fat": "포화지방", "fat": "지방",
    # [추가 — 2026-07-01] 치매 등 boost_nutrients 라벨용
    "iron": "철분", "vit_a": "비타민A", "thiamin": "티아민",
    "vit_c": "비타민C", "vit_d": "비타민D",
}

# [현재 미사용 — 2026-07-01] 질환 위반 부찬 교체(구 ②단계)가 ratio 전용으로
# 바뀌면서, 환자를 "위반 패턴 기준으로 그룹핑"할 필요 자체가 없어짐(더 이상
# 부찬을 교체하지 않으므로). 유형 그룹핑은 이제 report_agent.py가
# personal_menus(2단계/3단계 결과)의 실제 최종 결과값으로 직접 그룹핑함.
# 나중에 다시 필요할 경우를 위해 남겨둠.
_CONSTRAINT_FIELDS = [
    "energy_min", "energy_max", "protein_min", "protein_max", "sodium_max",
    "potassium_min", "fiber_min", "sugar_max", "sat_fat_max", "fat_min", "fat_max",
]


def _constraint_signature(c) -> tuple:
    return tuple(getattr(c, f, None) for f in _CONSTRAINT_FIELDS)


def _apply_disease_pass(df: pd.DataFrame, pool: dict, patients: list, pool_index: dict):
    """
    [수정 — 2026-07-01] 최종 설계에 맞춰 대폭 단순화. 당류/단백질/지방/
    포화지방/나트륨/열량 위반은 이제 오직 ratio 조정만으로 처리하고,
    ratio로 다 못 줄이더라도(RATIO_MIN/MAX 한계) 더 이상 부찬 교체로
    넘기지 않음 — 대체찬은 2단계(칼륨/식이섬유, 고혈압)와 3단계(치매
    영양소)에서만 발생하도록 역할을 완전히 분리함. 이렇게 하면 끼니당
    "유형"이 최대 3가지(기본형/2단계/3단계, 두 조건을 모두 만족하면
    2단계+3단계 동시 적용)로 예측 가능하게 수렴하고, 끼니당 대체찬도
    최대 2개(부찬1/부찬2 각각 최대 1개씩)로 고정됨.

    [수정 — 2026-07-01 #2] ratio는 이제 "위반이 있을 때만" 계산하는 게
    아니라, 위반 여부와 무관하게 항상 그 환자 개인의 target_energy(체중/
    키/나이 기반으로 산출된 1끼 필요 칼로리)를 향해 기본으로 맞춤. 그
    위에서 나트륨 등 절대량 기준 초과 위반이 남아있으면 ratio를 추가로
    더 낮춤(_calc_violation_ratio가 target_energy 후보와 위반 회피 후보
    중 더 엄격한 쪽을 채택).

    나트륨을 ratio만으로 다 못 낮추는 경우(예: 고혈압 800mg 기준이었을
    때처럼)는, patient_profile_final.py에서 나트륨 기준 자체를 1,350mg
    (당뇨 기준)으로 완화하고, 고혈압 환자는 조리 단계에서 저염 처리하는
    쪽으로 보완함(facility_optimization.py의 ProcessingAgent가 이미
    sodium_max가 설정된 환자를 "저염 대상"으로 분리해 조리 지침에 반영).

    반환: (ratio_map, {}, {}) — personal_menus/replace_reasons는 항상 빈
    딕셔너리(더 이상 이 패스에서 대체가 발생하지 않음). 파이프라인의 다른
    부분(report_agent.py 등)이 기대하는 반환 형태(3-tuple)는 유지함.
    """
    ratio_map: dict = {}

    for p in patients:
        c = p.constraint
        target_energy = getattr(p, "target_energy", None)

        for _, row in df.iterrows():
            menu_by_slot = {
                slot: pool_index.get((SLOT_CATS[slot], row.get(slot, "")), {})
                for slot in SLOTS
            }
            key = f"{p.name}||{row['일차']}||{row['끼니']}"

            if not all(menu_by_slot.values()):
                ratio_map[key] = 1.0
                continue

            total = _sum_meal_nutrition(menu_by_slot)
            violations = _check_violations(total, c)

            over_violations = [v for v in violations if v["direction"] == "over"]
            boostable_under = [
                v for v in violations
                if v["direction"] == "under" and v["field"] in RATIO_BOOSTABLE_UNDER_FIELDS
            ]

            if over_violations:
                # 나트륨 등 절대량 초과 위반이 있으면, target_energy도 함께
                # 후보에 넣어 더 엄격한 쪽을 채택
                ratio = _calc_violation_ratio(total, c, over_violations,
                                               target_energy=target_energy)
            elif boostable_under:
                ratio = _calc_boost_ratio(total, c, boostable_under)
            elif target_energy and total.get("energy", 0) > 0:
                # 위반이 없어도, 개인별 필요 칼로리에 맞춰 기본 배식량을 조정
                ratio = round(
                    max(RATIO_MIN, min(RATIO_MAX, target_energy / total["energy"])), 3
                )
            else:
                ratio = 1.0

            ratio_map[key] = ratio

    return ratio_map, {}, {}


# ════════════════════════════════════════════════════════════
# ②-B 부찬 슬롯 교체 ─ 부족 영양소 보강 (치매 등 boost_nutrients)
# ════════════════════════════════════════════════════════════
# [추가 — 2026-07-01] patient_profile_final.py의 치매 기준(NutritionConstraint.
# boost_nutrients=["iron","vit_a","thiamin","vit_c","vit_d"])은 상/하한이
# 없는 "많을수록 좋음" 목표라 _check_violations의 위반-회피 판정 대상이
# 아님. ①②(ratio 조정, 질환 위반 보정)가 안 건드린 부찬 슬롯 중 하나를
# 골라, boost_nutrients 함량이 높은 메뉴로 교체하는 별도 패스로 처리함.
# ①②가 이미 다 채운 끼니(부찬1/부찬2 둘 다 이미 교체됨)는 건드릴 자리가
# 없으므로 건너뜀 — 안전(질환 보정)이 보강보다 항상 우선.
# 보강은 "다양성"보다 "실제로 그 영양소가 얼마나 많은지"가 더 중요해서,
# 질환 위반 대체(ALT_CANDIDATE_POOL=5)보다 후보 폭을 좁게 잡음.
BOOST_CANDIDATE_POOL = 3


def _find_best_boost_alt(pool: dict, cat: str, current_menus: set,
                          boost_nutrients: list[str], exclude_name: str,
                          recent_names: set | None = None):
    """boost_nutrients 함량 합이 높은 메뉴를 우선으로, 최근 사용 이력을
    피해 상위 후보 중 무작위 선택(②단계와 동일한 다양성 원칙).

    [수정 — 2026-07-01] 초기 구현은 ②단계와 동일하게 "상위 5개 중 무작위"를
    그대로 썼는데, 후보가 몇 개 없는 카테고리에서는 보강 성분이 0인 메뉴가
    상위 5개 안에 끼어 뽑히는 문제가 있었음(테스트로 확인). 보강 목적에는
    "그 영양소가 실제로 있는 메뉴"가 훨씬 중요하므로, 점수가 0보다 큰
    후보를 우선하고(있으면), 후보 폭도 더 좁게(BOOST_CANDIDATE_POOL=3) 잡음.
    """
    recent_names = recent_names or set()

    base_candidates = [
        m for m in pool.get(cat, [])
        if m["menu_name"] not in current_menus and m["menu_name"] != exclude_name
    ]
    if not base_candidates or not boost_nutrients:
        return None

    # 철분(mg)·비타민A(㎍)·비타민C(mg) 등 단위가 서로 달라 그냥 더하면
    # 단위가 큰 영양소가 항상 점수를 지배함 → 후보군 내 최댓값 대비
    # 비율로 정규화한 뒤 합산.
    max_by_nutrient = {
        nut: (max((m.get(nut, 0) or 0) for m in base_candidates) or 1)
        for nut in boost_nutrients
    }

    def _boost_score(m: dict) -> float:
        return sum((m.get(nut, 0) or 0) / max_by_nutrient[nut] for nut in boost_nutrients)

    scored = sorted(base_candidates, key=_boost_score, reverse=True)

    # 보강 성분이 실제로 있는(점수 > 0) 후보만 우선 고려 — 전부 0이면
    # (메뉴 풀에 해당 영양소 데이터 자체가 없는 극단적인 경우) 전체로 폴백.
    positive = [m for m in scored if _boost_score(m) > 0]
    ranked_pool = positive if positive else scored

    top_pool = ranked_pool[:BOOST_CANDIDATE_POOL]
    fresh = [m for m in top_pool if m["menu_name"] not in recent_names]
    if fresh:
        return random.choice(fresh)

    wider_fresh = [m for m in ranked_pool if m["menu_name"] not in recent_names]
    if wider_fresh:
        return random.choice(wider_fresh[:BOOST_CANDIDATE_POOL])

    return ranked_pool[0]


# ════════════════════════════════════════════════════════════
# ②-A(2단계) 부찬 슬롯 교체 ─ 칼륨/식이섬유 보강 (고혈압 보유 환자 전원)
# ════════════════════════════════════════════════════════════
# [추가 — 2026-07-01] 요청에 따라 칼륨/식이섬유를 patient_profile_final.py의
# NSGA-II 공통 최적화 대상(①단계)에서 완전히 빼고, 여기서 "고혈압이 있는지"
# (단독이든 당뇨/치매와 병존이든 — 고혈압, 고혈압+당뇨, 고혈압+치매,
# 고혈압+당뇨+치매 전부 포함) 만으로 판단하는 전용 고정 대체찬 패스로
# 분리함. 임계값(700mg/7g)은 체중 등 개인 신체 정보와 무관한 고정값이라,
# 이 패스의 결과는 오직 "고혈압 보유 여부"에만 좌우됨 — 유형이 예측
# 가능한 소수로 수렴하는 핵심 장치.
HYPERTENSION_POTASSIUM_MIN = 700
HYPERTENSION_FIBER_MIN = 7
HYPERTENSION_BOOST_FIELDS = ["potassium", "fiber"]


# [수정 — 2026-07-01] 기존에는 2단계/3단계 모두 "환자 한 명씩 순회하며
# 독립적으로 무작위 대체 메뉴를 뽑는" 구조였음. 그런데 ①단계가 더 이상
# 대체를 하지 않게 되면서(순수 ratio 전용), 같은 (일차,끼니)에 대한
# 기본 식단·already_changed 상태가 자격이 같은 환자들 사이에서 완전히
# 동일해짐. 이 상태에서도 환자마다 독립적으로 random.choice를 호출하니,
# 파이썬의 전역 random 상태가 호출 순서에 따라 달라져 같은 조건인데도
# 서로 다른 대체 메뉴가 뽑히는 문제가 있었음(실측: 68명이 거의 전원
# 서로 다른 "유형"으로 쪼개짐). 이제 (일차, 끼니, already_changed 시그니처)
# 조합별로 대체 여부·대체 메뉴를 "딱 한 번만" 계산하고, 그 결과를 그
# 조합에 해당하는 모든 환자에게 동일하게 적용함 — 그룹핑이 오직 질환
# 보유 여부로만 갈리도록 보장하는 핵심 장치.
def _apply_group_boost_pass(df: pd.DataFrame, pool: dict, patients: list,
                             pool_index: dict, already_changed: dict,
                             eligibility_fn, boost_fields_fn, reason_type: str,
                             detail_fn, gate_fn=None):
    """
    공통 로직 — 2단계(고혈압)/3단계(치매)가 공유해서 씀.

    eligibility_fn(p)      -> bool   : 이 패스 대상 환자인지
    boost_fields_fn(p)     -> list   : 이 환자 기준 보강 대상 영양소 필드
                                        (2/3단계 전부 disease membership만
                                        으로 정해지므로 그룹 내에서는 항상
                                        동일한 값이 나옴)
    gate_fn(total)         -> (bool, list[str]) | None
                              None이면 무조건 대체(치매), 함수가 있으면
                              그 결과가 True일 때만 대체(고혈압 임계값 체크)
    detail_fn(disease_label, need_labels) -> str : 사유 텍스트
    """
    personal_menus: dict = {}
    replace_reasons: dict = {}

    eligible = [p for p in patients if eligibility_fn(p)]
    if not eligible:
        return personal_menus, replace_reasons

    group_recent_usage: dict[tuple, list[tuple[int, str]]] = {}

    for _, row in df.iterrows():
        day_num = _day_num(row.get("일차"))
        day, meal = row["일차"], row["끼니"]

        menu_by_slot = {
            slot: pool_index.get((SLOT_CATS[slot], row.get(slot, "")), {})
            for slot in SLOTS
        }
        if not all(menu_by_slot.values()):
            continue
        total = _sum_meal_nutrition(menu_by_slot)

        if gate_fn:
            proceed, need_labels = gate_fn(total)
            if not proceed:
                continue
        else:
            need_labels = None

        # already_changed 시그니처가 같은 환자끼리 서브그룹으로 묶음
        # (지금 구조상 대부분 하나의 시그니처로 통일되지만, 안전하게
        # 일반화해 둠 — 예: 고혈압+치매 환자는 2단계가 이미 부찬1을
        # 채운 상태로 3단계에 들어오므로 다른 시그니처를 가짐)
        subgroups: dict[tuple, list] = {}
        for p in eligible:
            key = f"{p.name}||{day}||{meal}"
            sig = tuple(sorted(already_changed.get(key, {}).items()))
            subgroups.setdefault(sig, []).append(p)

        for sig, group_patients in subgroups.items():
            already = dict(sig)
            available_slots = [s for s in SWAPPABLE_SLOTS if s not in already]
            if not available_slots:
                continue
            target_slot = available_slots[0]
            cat = SLOT_CATS[target_slot]
            current_menus = {m.get("menu_name", "") for m in menu_by_slot.values()}
            orig_name = menu_by_slot[target_slot].get("menu_name", "")

            boost_fields = boost_fields_fn(group_patients[0])
            if not boost_fields:
                continue

            cache_key = sig
            recent_usage = group_recent_usage.setdefault(cache_key, [])
            recent_names = {
                n for d, n in recent_usage if day_num - d < RECENT_LOOKBACK_DAYS
            }
            alt = _find_best_boost_alt(pool, cat, current_menus, boost_fields,
                                        orig_name, recent_names=recent_names)
            if not alt:
                continue

            recent_usage.append((day_num, alt["menu_name"]))
            group_recent_usage[cache_key] = [
                (d, n) for d, n in recent_usage
                if day_num - d < RECENT_LOOKBACK_DAYS
            ]

            for p in group_patients:
                key = f"{p.name}||{day}||{meal}"
                disease_label = getattr(p, "disease_type_label", "일반형")
                personal_menus.setdefault(key, {})[target_slot] = alt["menu_name"]
                replace_reasons.setdefault(key, []).append({
                    "slot": target_slot, "from": orig_name, "to": alt["menu_name"],
                    "reason": reason_type,
                    "detail": detail_fn(disease_label, need_labels),
                    "ratio": None,
                })

    return personal_menus, replace_reasons


def _apply_hypertension_boost_pass(df: pd.DataFrame, pool: dict, patients: list,
                                    pool_index: dict, already_changed: dict):
    def _eligible(p):
        resolved = p._resolve_diseases() if hasattr(p, "_resolve_diseases") else []
        return any("고혈압" in d for d in resolved)

    def _gate(total):
        needs_potassium = total.get("potassium", 0) < HYPERTENSION_POTASSIUM_MIN
        needs_fiber     = total.get("fiber", 0)     < HYPERTENSION_FIBER_MIN
        need_labels = []
        if needs_potassium: need_labels.append("칼륨")
        if needs_fiber:      need_labels.append("식이섬유")
        return (needs_potassium or needs_fiber), need_labels

    def _detail(disease_label, need_labels):
        return f"{disease_label} {'/'.join(need_labels)} 보강"

    return _apply_group_boost_pass(
        df, pool, patients, pool_index, already_changed,
        eligibility_fn=_eligible,
        boost_fields_fn=lambda p: HYPERTENSION_BOOST_FIELDS,
        reason_type="hypertension_boost",
        detail_fn=_detail,
        gate_fn=_gate,
    )


def _apply_boost_pass(df: pd.DataFrame, pool: dict, patients: list,
                       pool_index: dict, already_changed: dict):
    def _eligible(p):
        return bool(getattr(p.constraint, "boost_nutrients", None))

    def _boost_fields(p):
        return getattr(p.constraint, "boost_nutrients", None) or []

    def _detail(disease_label, need_labels):
        # 치매 등 boost_nutrients는 disease membership만으로 정해지고 eligible
        # 그룹 내에서 항상 동일하므로, 대표 환자 목록 대신 고정 라벨을 사용.
        nutrient_label = ", ".join(
            FIELD_LABELS.get(n, n) for n in
            ["iron", "vit_a", "thiamin", "vit_c", "vit_d"]
        )
        return f"{disease_label} 부족 영양소 보강 ({nutrient_label})"

    return _apply_group_boost_pass(
        df, pool, patients, pool_index, already_changed,
        eligibility_fn=_eligible,
        boost_fields_fn=_boost_fields,
        reason_type="boost",
        detail_fn=_detail,
        gate_fn=None,
    )


# ════════════════════════════════════════════════════════════
# ③ 부찬 슬롯 교체 ─ 선호도 기반 (①②에서 안 바뀐 슬롯만)
# ════════════════════════════════════════════════════════════
def _apply_preference_pass(df: pd.DataFrame, pool: dict, patients: list,
                            weights: dict, already_changed: dict, pool_index: dict):
    menu_avg: dict = {}
    for name, prefs in weights.items():
        for menu, score in prefs.items():
            menu_avg.setdefault(menu, []).append(score)
    menu_avg = {m: round(sum(s) / len(s), 3) for m, s in menu_avg.items()}

    dislike_candidates = sorted(
        [(m, s) for m, s in menu_avg.items() if s < FACILITY_DISLIKE],
        key=lambda x: x[1],
    )[:MAX_DISLIKE_MENUS]
    dislike_set = {m for m, _ in dislike_candidates}

    if not dislike_set:
        return {}, {}

    personal_menus: dict = {}
    replace_reasons: dict = {}

    for p in patients:
        pref = weights.get(p.name, {})
        disease_label = getattr(p, "disease_type_label", "일반형")
        alt_candidates = sorted(
            pool.get("부찬", []),
            key=lambda m: pref.get(m["menu_name"], 0.7),
            reverse=True,
        )

        # [추가 — 2026-07-01] 선호도 대체도 같은 3주 최근 이력 회피 적용
        recent_usage: list[tuple[int, str]] = []

        for _, row in df.iterrows():
            day_num = _day_num(row.get("일차"))
            key = f"{p.name}||{row['일차']}||{row['끼니']}"
            already = already_changed.get(key, {})  # ②에서 이미 바뀐 슬롯(질환 위반)
            override = {}

            for slot in ["부찬1", "부찬2"]:
                if slot in already:
                    continue  # 질환 위반으로 이미 바뀐 슬롯은 절대 건드리지 않음

                menu  = row.get(slot, "")
                score = pref.get(menu, 0.7)

                if score >= PERSONAL_DISLIKE:
                    continue
                if menu not in dislike_set:
                    continue

                current_menus = set(
                    row[s] for s in ["밥", "국", "주찬", "부찬1", "부찬2", "김치"]
                    if row.get(s)
                )

                recent_names = {
                    n for d, n in recent_usage if day_num - d < RECENT_LOOKBACK_DAYS
                }
                eligible = [
                    m["menu_name"] for m in alt_candidates
                    if m["menu_name"] not in current_menus
                    and pref.get(m["menu_name"], 0.7) >= ALT_MIN_SCORE
                ]
                fresh = [m for m in eligible if m not in recent_names]
                pick_pool = fresh[:ALT_CANDIDATE_POOL] if fresh else eligible[:ALT_CANDIDATE_POOL]
                alt = random.choice(pick_pool) if pick_pool else None

                if alt:
                    override[slot] = alt
                    recent_usage.append((day_num, alt))
                    recent_usage = [
                        (d, n) for d, n in recent_usage
                        if day_num - d < RECENT_LOOKBACK_DAYS
                    ]
                    replace_reasons.setdefault(key, []).append({
                        "slot": slot, "from": menu, "to": alt,
                        "reason": "preference",
                        "detail": f"{disease_label} 기피메뉴 대체 (선호도 {score:.2f})",
                        "ratio": None,
                    })
                    break  # 1개 슬롯만 대체

            if override:
                personal_menus[key] = override

    return personal_menus, replace_reasons


# ════════════════════════════════════════════════════════════
# 메인 엔트리포인트
# ════════════════════════════════════════════════════════════
def personalize_agent(state: MealPlanState) -> dict:
    print("\n[PersonalizeAgent] 개인화 시작 (① ratio → ② 질환위반 부찬교체 → ②-B 보강 → ③ 선호도 부찬교체)...")

    pool       = state.get("pool") or {}
    df_records = state.get("df_menu_records")
    patients   = registry.get(state["patients_key"]) if state.get("patients_key") else []
    weights    = state.get("preference_weights") or {}

    if not df_records or not pool or not patients:
        print("  [PersonalizeAgent] df_menu/pool/patients 없음 — 건너뜀")
        return {
            "personal_menus": {},
            "violation_ratio_map": {},
            "personalize_reasons": {},
            "messages": ["[PersonalizeAgent] 데이터 없음 — 건너뜀"],
        }

    df = pd.DataFrame(df_records, columns=state["df_menu_columns"])
    pool_index = {
        (cat, m["menu_name"]): m
        for cat, menus in pool.items() for m in menus
    }

    # ── ① ratio 조정 (개인별 target_energy 기반, 대체찬 없음) ────
    ratio_map, disease_menus, disease_reasons = _apply_disease_pass(
        df, pool, patients, pool_index
    )
    n_ratio_adjusted = sum(1 for r in ratio_map.values() if r != 1.0)

    print(f"  [① ratio 조정]     {n_ratio_adjusted}건 (개인별 필요 열량 + 절대기준 초과 보정)")

    # ── 2단계: 칼륨/식이섬유 보강 (고혈압 보유 환자 전원, ②가 안 건드린 슬롯만) ──
    hyper_menus, hyper_reasons = _apply_hypertension_boost_pass(
        df, pool, patients, pool_index, disease_menus
    )
    n_hyper_swap = sum(len(v) for v in hyper_reasons.values())
    print(f"  [2단계 칼륨/식이섬유 보강] {n_hyper_swap}건 (고혈압 보유 환자)")

    already_after_hyper = dict(disease_menus)
    for key, override in hyper_menus.items():
        if key in already_after_hyper:
            already_after_hyper[key] = {**override, **already_after_hyper[key]}
        else:
            already_after_hyper[key] = override

    # ── 3단계: 부족 영양소 보강 (치매 보유 환자 전원, ②·2단계가 안 건드린 슬롯만) ──
    boost_menus, boost_reasons = _apply_boost_pass(
        df, pool, patients, pool_index, already_after_hyper
    )
    n_boost_swap = sum(len(v) for v in boost_reasons.values())
    print(f"  [3단계 치매 영양소 보강]   {n_boost_swap}건 (치매 보유 환자)")

    # ②③ 둘 다에게 "이미 채워진 슬롯"으로 보이도록 병합(③이 겹쳐 쓰지 않게)
    already_for_pref = dict(already_after_hyper)
    for key, override in boost_menus.items():
        if key in already_for_pref:
            already_for_pref[key] = {**override, **already_for_pref[key]}
        else:
            already_for_pref[key] = override

    # ── ③ 선호도 패스 (②·2단계·3단계에서 안 바뀐 슬롯만) ──────────
    pref_menus, pref_reasons = {}, {}
    if weights:
        pref_menus, pref_reasons = _apply_preference_pass(
            df, pool, patients, weights, already_for_pref, pool_index
        )
        n_pref_swap = sum(len(v) for v in pref_reasons.values())
        print(f"  [③ 선호도 교체]    {n_pref_swap}건")
    else:
        n_pref_swap = 0
        print("  [③ 선호도 교체]    preference_weights 없음 — 건너뜀")

    # ── 병합: ② > 2단계 > 3단계 > ③ 순으로 우선순위(안전 > 만성질환 보강 > 인지증 보강 > 기호) ─
    personal_menus = dict(disease_menus)
    for key, override in hyper_menus.items():
        if key in personal_menus:
            personal_menus[key] = {**override, **personal_menus[key]}
        else:
            personal_menus[key] = override
    for key, override in boost_menus.items():
        if key in personal_menus:
            personal_menus[key] = {**override, **personal_menus[key]}
        else:
            personal_menus[key] = override
    for key, override in pref_menus.items():
        if key in personal_menus:
            personal_menus[key] = {**override, **personal_menus[key]}
        else:
            personal_menus[key] = override

    personalize_reasons = {}
    for key in set(disease_reasons) | set(hyper_reasons) | set(boost_reasons) | set(pref_reasons):
        personalize_reasons[key] = (
            disease_reasons.get(key, [])
            + hyper_reasons.get(key, [])
            + boost_reasons.get(key, [])
            + pref_reasons.get(key, [])
        )

    total = len(personal_menus)
    print(f"\n[PersonalizeAgent] 완료 — ratio조정 {n_ratio_adjusted}건 "
          f"+ 칼륨/식이섬유보강 {n_hyper_swap}건 + 치매영양소보강 {n_boost_swap}건 "
          f"+ 선호도교체 {n_pref_swap}건 = 끼니 {total}건에 개인화 적용")

    return {
        "personal_menus": personal_menus,
        "violation_ratio_map": ratio_map,        # ServingAgent가 기본 ratio와 곱해 최종 ratio 산출
        "personalize_reasons": personalize_reasons,  # ReportAgent가 정확한 라벨 표시에 사용
        "messages": [
            f"[PersonalizeAgent] ratio조정 {n_ratio_adjusted}건 + "
            f"칼륨/식이섬유보강 {n_hyper_swap}건 + "
            f"치매영양소보강 {n_boost_swap}건 + 선호도교체 {n_pref_swap}건 "
            f"(밥/국/주찬/김치 미변경)"
        ],
    }