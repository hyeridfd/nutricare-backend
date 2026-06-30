"""
serving_agent.py  ─  ServingAgent 노드 (개선 버전)
===================================================
배식량 산출 기준:
  ① 기본 배식량 = pool["weight"] (CandidateAgent Cypher sum(nutri_weight))
  ② 개인 배식 비율 = clipped_target_energy / 시설평균
     - target_energy: BMI + 허리둘레 기반 개인별 목표 열량
     - clipping: p.constraint.energy_min ~ energy_max (질환별 기준 반영)

[변경 사항 — PersonalizeAgent의 위반 보정 ratio 반영]
PersonalizeAgent가 끼니 합산 영양값이 질환 기준(예: 고혈압 나트륨 800mg)을
초과할 때, 양을 줄여서 해소하는 ratio(violation_ratio_map)를 계산해 둠.
여기서는 기존 BMI/허리둘레 기반 ratio와 그 위반 보정 ratio를 곱해서
최종 배식량을 산출함. 또한 PersonalizeAgent가 부찬1/부찬2를 교체한 경우
(ratio만으로 못 푸는 잔여 위반), personal_menus를 반영해 영양값을 계산함.
밥/국/주찬/김치는 PersonalizeAgent가 교체하지 않으므로 항상 원본 메뉴 유지.
"""

import pandas as pd
import registry
from state import MealPlanState

SLOT_CATS = [
    ("밥",   "밥"),
    ("국",   "국"),
    ("주찬", "주찬"),
    ("부찬1","부찬"),
    ("부찬2","부찬"),
    ("김치", "김치"),
]

# ratio 안전 범위
RATIO_MIN = 0.6
RATIO_MAX = 1.3


def _calc_ratio(patient, facility_avg_energy: float) -> float:
    """
    개인별 배식 비율 계산
    ① p.constraint 범위로 target_energy 클리핑 (질환 기준 반영)
    ② clipped_target / 시설 평균 → ratio
    """
    c = patient.constraint

    # 질환별 에너지 범위 (constraint에 이미 반영됨)
    e_min = c.energy_min or 500
    e_max = c.energy_max or 800

    # target_energy를 질환 기준 범위 내로 클리핑
    raw_target    = getattr(patient, "target_energy", facility_avg_energy)
    clipped_target = max(e_min, min(e_max, raw_target))

    # 시설 평균 대비 비율
    ratio = clipped_target / facility_avg_energy

    # 안전 범위 적용
    return round(max(RATIO_MIN, min(RATIO_MAX, ratio)), 3)


def serving_agent(state: MealPlanState) -> dict:
    print("\n[ServingAgent] 개인별 배식량 산출 시작...")

    # ── registry에서 직렬화 불가 객체 꺼내기 ─────────────────
    patients = registry.get(state["patients_key"])

    # ── df_menu: records → DataFrame 복원 ────────────────────
    df = None
    if state.get("df_menu_records"):
        df = pd.DataFrame(
            state["df_menu_records"],
            columns=state["df_menu_columns"]
        )

    if df is None:
        print("[ServingAgent] df_menu 없음 — 건너뜀")
        return {"serving_map": {}, "messages": ["[ServingAgent] df_menu 없음"]}

    pool = state.get("pool") or {}

    # pool 빠른 조회 인덱스
    pool_index = {
        (cat, m["menu_name"]): m
        for cat, menus in pool.items()
        for m in menus
    }

    # ── 시설 평균 target_energy 계산 ─────────────────────────
    valid_energies = [
        getattr(p, "target_energy", None)
        for p in patients
        if getattr(p, "target_energy", None) is not None
    ]
    if not valid_energies:
        raise ValueError("[ServingAgent] 입소자 target_energy가 모두 없습니다.")

    facility_avg_energy = sum(valid_energies) / len(valid_energies)
    print(f"  시설 평균 목표 열량: {facility_avg_energy:.1f} kcal/끼니")

    # ── 개인별 ratio 사전 계산 (진단 출력 포함) ──────────────
    patient_ratios = {}
    for p in patients:
        ratio = _calc_ratio(p, facility_avg_energy)
        patient_ratios[p.name] = ratio

    # 대표 3명 출력
    sample = list(patient_ratios.items())[:3]
    for name, r in sample:
        print(f"  [{name}] ratio={r:.3f}")

    # ── PersonalizeAgent가 계산한 위반 보정 ratio ────────────
    violation_ratio_map = state.get("violation_ratio_map") or {}
    personal_menus       = state.get("personal_menus") or {}

    # ── 배식량 산출 ───────────────────────────────────────────
    serving_map: dict = {}

    for _, menu_row in df.iterrows():
        day  = menu_row["일차"]
        meal = menu_row["끼니"]

        menu_by_slot = {
            slot: pool_index.get((cat, menu_row.get(slot, "")), {})
            for slot, cat in SLOT_CATS
        }

        for p in patients:
            key_str = f"{p.name}||{day}||{meal}"

            # PersonalizeAgent가 부찬1/부찬2를 교체했으면 그 메뉴로 대체해서 계산
            # (밥/국/주찬/김치는 절대 교체되지 않으므로 그대로 둠)
            patient_menu_by_slot = dict(menu_by_slot)
            override = personal_menus.get(key_str, {})
            if override:
                for slot, new_name in override.items():
                    cat = dict(SLOT_CATS).get(slot)
                    new_menu = pool_index.get((cat, new_name))
                    if new_menu:
                        patient_menu_by_slot[slot] = new_menu

            # BMI/허리둘레 기반 ratio × PersonalizeAgent의 위반 보정 ratio
            base_ratio      = patient_ratios[p.name]
            violation_ratio = violation_ratio_map.get(key_str, 1.0)
            ratio = round(max(RATIO_MIN, min(RATIO_MAX, base_ratio * violation_ratio)), 3)

            # ① 기본 배식량 = pool weight (레시피 재료 총 사용량)
            srv = {}
            for slot, cat in SLOT_CATS:
                menu_data   = patient_menu_by_slot.get(slot, {})
                base_weight = menu_data.get("weight", 0) or 0
                srv[slot]   = round(base_weight * ratio, 1)

            # ② 예상 영양소 (ratio 반영)
            def _sum(key, _ratio=ratio, _menu_by_slot=patient_menu_by_slot):
                return sum(
                    _menu_by_slot[s].get(key, 0) or 0
                    for s, _ in SLOT_CATS
                ) * _ratio

            # ③ 영양 기준 충족 여부
            c     = p.constraint
            e_val = round(_sum("energy"),  1)
            p_val = round(_sum("protein"), 1)
            s_val = round(_sum("sodium"),  1)

            ok_e = (c.energy_min or 0) <= e_val <= (c.energy_max or 9999)
            ok_p = (p_val >= (c.protein_min or 0)) and \
                   (p_val <= (c.protein_max or 9999))
            ok_s = s_val <= (c.sodium_max or 9999)

            entry = {
                **srv,
                "ratio":          ratio,
                "예상열량":       e_val,
                "예상단백질":     p_val,
                "예상나트륨":     s_val,
                "예상탄수화물":   round(_sum("carb"), 1),
                "열량OK":  "✅" if ok_e else "⚠️",
                "단백질OK": "✅" if ok_p else "⚠️",
                "나트륨OK": "✅" if ok_s else "⚠️",
            }

            serving_map[key_str] = entry

    print(f"[ServingAgent] 완료 — {len(serving_map)}건")
    return {
        "serving_map": serving_map,
        "messages":    [f"[ServingAgent] {len(serving_map)}건 배식량 산출 완료"],
    }