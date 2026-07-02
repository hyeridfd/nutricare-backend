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
def _calc_violation_ratio(total: dict, c, over_violations: list[dict]) -> float:
    """
    over 위반 항목들을 모두 해소하는 가장 큰 ratio(=가장 적게 줄이는 ratio)를 계산.
    각 필드에 대해 (기준치 / 현재값)을 구하고, 그 중 최솟값을 채택.
    energy_min 하한은 별도로 보호.
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

    # sugar/sat_fat/fat은 "열량비" 위반이라 ratio를 곱해도 비율 자체는 안 바뀜
    # (분자분모 둘 다 ratio배 되므로) → ratio로 해결 불가, 부찬 교체로 넘김

    ratio = min(candidates)

    # energy_min 하한 보호: 이 ratio로 줄였을 때 energy_min 밑으로 가면 안 됨
    if c.energy_min and total.get("energy", 0) > 0:
        floor_ratio = c.energy_min / total["energy"]
        ratio = max(ratio, floor_ratio)

    return round(max(RATIO_MIN, min(RATIO_MAX, ratio)), 3)


def _violations_after_ratio(total: dict, c, ratio: float) -> list[dict]:
    """ratio 적용 후에도 남는 위반(주로 ratio로 못 줄이는 열량비 항목, 또는
    energy_min 하한에 막혀 못 줄인 over 항목)을 다시 체크."""
    scaled = dict(total)
    # ratio로 줄어드는 항목(절대량)만 스케일링. 열량비 항목(sugar/sat_fat/fat 비율)은
    # 분자분모 둘 다 ratio배라 비율 불변 → 그대로 둠(이미 total 기준으로 위반 판정됨).
    for f in RATIO_REDUCIBLE_FIELDS:
        scaled[f] = total.get(f, 0) * ratio
    for f in NUTR_FIELDS:
        if f not in scaled:
            scaled[f] = total.get(f, 0)
    return _check_violations(scaled, c)


# ════════════════════════════════════════════════════════════
# ② 부찬 슬롯 교체 ─ 질환 위반 보정
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
}


def _apply_disease_pass(df: pd.DataFrame, pool: dict, patients: list, pool_index: dict):
    """
    ① ratio 조정 + ② 부찬 교체(질환 위반 보정).
    반환: (ratio_map, personal_menus, replace_reasons)
      replace_reasons: {key: [{"slot":.., "from":.., "to":.., "reason":"disease",
                                "detail": "H형 나트륨 초과 보정", "ratio": 0.82}]}
    """
    ratio_map: dict = {}
    personal_menus: dict = {}
    replace_reasons: dict = {}

    # [수정 — 2026-07-01] 기존에는 recent_usage를 환자별로 매번 새로
    # 시작해서, 같은 질환유형(disease_type_label)이라 위반 발생 시점·항목이
    # 100% 동일한 환자들끼리도 대체 메뉴가 무작위로 서로 달라지는 문제가
    # 있었음(그 결과 "유형별_28일_식단표"에서 원래 같아야 할 환자들이
    # 서로 다른 유형으로 쪼개짐). 이제 recent_usage와 "이번 위반에 뭘로
    # 바꿨는지"를 환자 단위가 아니라 질환유형 단위로 공유해, 같은 유형은
    # 항상 같은 대체 결과를 받도록 함. 21일 다양성 로직 자체는 그대로
    # 유지되지만, 이제 "그 유형이 최근 21일 안에 뭘 썼는지" 기준으로 도는
    # 것으로 의미가 바뀜(환자별 회전 → 유형별 회전).
    recent_usage_by_type: dict[str, dict[str, list[tuple[int, str]]]] = {}
    # (질환유형, 일차, 끼니, 슬롯, 위반항목, 방향) → 이번에 선택된 대체 메뉴명.
    # 같은 유형·같은 위반이면 이 캐시를 그대로 재사용해 무작위 재추첨을 피함.
    chosen_alt_cache: dict[tuple, str] = {}

    for p in patients:
        c = p.constraint
        disease_label = getattr(p, "disease_type_label", "일반형")
        recent_usage = recent_usage_by_type.setdefault(disease_label, {})

        for _, row in df.iterrows():
            day_num = _day_num(row.get("일차"))

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

            if not violations:
                ratio_map[key] = 1.0
                continue

            # ── ① ratio로 over 위반 해소 ──────────────────────
            over_violations = [v for v in violations if v["direction"] == "over"]
            ratio = _calc_violation_ratio(total, c, over_violations) if over_violations else 1.0
            ratio_map[key] = ratio

            remaining = _violations_after_ratio(total, c, ratio)
            seen = set()
            remaining_unique = []
            for v in remaining + [v for v in violations if v["direction"] == "under"]:
                fk = (v["field"], v["direction"])
                if fk not in seen:
                    seen.add(fk)
                    remaining_unique.append(v)

            # ── ② 부찬1/부찬2 교체로 잔여 위반 보정 ────────────
            override = {}
            changed_slots = set()

            for v in remaining_unique:
                target_slot = _pick_violation_slot(
                    {s: m for s, m in menu_by_slot.items()
                     if s not in changed_slots and s in SWAPPABLE_SLOTS},
                    v["field"], v["direction"]
                )
                if not target_slot:
                    continue

                cat = SLOT_CATS[target_slot]
                current_menus = {m.get("menu_name", "") for m in menu_by_slot.values()}
                orig_name = menu_by_slot[target_slot].get("menu_name", "")

                # [수정 — 2026-07-01] 같은 (질환유형, 일차, 끼니, 슬롯, 위반항목,
                # 방향) 조합이 이미 다른 환자에서 처리된 적 있으면 그 결과를
                # 그대로 재사용 — 무작위 재추첨을 피해 같은 유형은 항상 같은
                # 대체 메뉴를 받게 함.
                cache_key = (disease_label, row["일차"], row["끼니"],
                             target_slot, v["field"], v["direction"])

                if cache_key in chosen_alt_cache:
                    alt_name = chosen_alt_cache[cache_key]
                    alt = pool_index.get((cat, alt_name))
                else:
                    history = recent_usage.get(cat, [])
                    recent_names = {
                        name for d, name in history
                        if day_num - d < RECENT_LOOKBACK_DAYS
                    }
                    alt = _find_better_alt(pool, cat, current_menus,
                                            v["field"], v["direction"], orig_name,
                                            recent_names=recent_names)
                    if alt:
                        chosen_alt_cache[cache_key] = alt["menu_name"]
                        # 이 유형이 처음 이 위반을 만났을 때만 이력에 기록
                        # (재사용 시에는 이미 기록되어 있으므로 중복 기록 방지)
                        updated_history = history + [(day_num, alt["menu_name"])]
                        recent_usage[cat] = [
                            (d, n) for d, n in updated_history
                            if day_num - d < RECENT_LOOKBACK_DAYS
                        ]

                if alt:
                    override[target_slot] = alt["menu_name"]
                    menu_by_slot[target_slot] = alt
                    changed_slots.add(target_slot)

                    label = FIELD_LABELS.get(v["field"], v["field"])
                    direction_kr = "초과" if v["direction"] == "over" else "부족"
                    replace_reasons.setdefault(key, []).append({
                        "slot": target_slot, "from": orig_name, "to": alt["menu_name"],
                        "reason": "disease",
                        "detail": f"{disease_label} {label} {direction_kr} 보정",
                        "ratio": ratio,
                    })

            if override:
                personal_menus[key] = override

    return ratio_map, personal_menus, replace_reasons


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
    print("\n[PersonalizeAgent] 개인화 시작 (① ratio → ② 질환위반 부찬교체 → ③ 선호도 부찬교체)...")

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

    # ── ①② 질환 위반 패스 ────────────────────────────────────
    ratio_map, disease_menus, disease_reasons = _apply_disease_pass(
        df, pool, patients, pool_index
    )
    n_ratio_adjusted = sum(1 for r in ratio_map.values() if r != 1.0)
    n_disease_swap    = sum(len(v) for v in disease_reasons.values())

    print(f"  [① ratio 조정]     {n_ratio_adjusted}건 (1.0이 아닌 ratio 적용)")
    print(f"  [② 질환위반 교체]  {n_disease_swap}건 (ratio로 못 푼 잔여 위반)")

    # ── ③ 선호도 패스 (②에서 안 바뀐 슬롯만) ──────────────────
    pref_menus, pref_reasons = {}, {}
    if weights:
        pref_menus, pref_reasons = _apply_preference_pass(
            df, pool, patients, weights, disease_menus, pool_index
        )
        n_pref_swap = sum(len(v) for v in pref_reasons.values())
        print(f"  [③ 선호도 교체]    {n_pref_swap}건")
    else:
        n_pref_swap = 0
        print("  [③ 선호도 교체]    preference_weights 없음 — 건너뜀")

    # ── 병합: ②가 ③을 항상 덮음(같은 슬롯이 겹칠 일은 없지만 방어) ─
    personal_menus = dict(disease_menus)
    for key, override in pref_menus.items():
        if key in personal_menus:
            personal_menus[key] = {**override, **personal_menus[key]}
        else:
            personal_menus[key] = override

    personalize_reasons = {}
    for key in set(disease_reasons) | set(pref_reasons):
        personalize_reasons[key] = disease_reasons.get(key, []) + pref_reasons.get(key, [])

    total = len(personal_menus)
    print(f"\n[PersonalizeAgent] 완료 — ratio조정 {n_ratio_adjusted}건 "
          f"+ 질환위반교체 {n_disease_swap}건 + 선호도교체 {n_pref_swap}건 "
          f"= 끼니 {total}건에 개인화 적용")

    return {
        "personal_menus": personal_menus,
        "violation_ratio_map": ratio_map,        # ServingAgent가 기본 ratio와 곱해 최종 ratio 산출
        "personalize_reasons": personalize_reasons,  # ReportAgent가 정확한 라벨 표시에 사용
        "messages": [
            f"[PersonalizeAgent] ratio조정 {n_ratio_adjusted}건 + "
            f"질환위반교체 {n_disease_swap}건 + 선호도교체 {n_pref_swap}건 "
            f"(밥/국/주찬/김치 미변경)"
        ],
    }