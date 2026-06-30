"""
facility_optimization.py
시설 전체 입소자 → 통합 최적화 연결

흐름:
  1. load_patients_from_excel() → PatientProfile 68명
  2. FacilityConstraint: 전체 환자 중 가장 엄격한 제약 도출
  3. get_candidates_by_category(): 전체 질환 통합 후보 메뉴 조회
  4. MealPlanProblem + NSGA-II: 시설 기준 식단 최적화
  5. ProcessingAgent: 조리 지침서 생성
  6. ServingAgent: 개인별 배식량 산출

[변경 사항 — derive_facility_constraint 정합성 수정]
기존: sodium_max만 환자 개인 constraint(min)로 도출하고,
      sugar_max/sat_fat_max/fat_min/fat_max는 2400kcal 고정 계산식으로
      하드코딩되어 있었음. 주석엔 "가장 엄격한 값", "교집합"이라고
      적혀 있었지만 실제로는 환자 constraint 리스트를 전혀 참조하지 않아
      patient_profile_final.py의 질환별 기준(당뇨 sugar_max, 고혈압
      sat_fat_max/fat_min/fat_max 등)과 어긋났음.
변경: sodium_max와 동일한 방식으로 모든 항목을 constraints 리스트에서
      가장 엄격한(보수적인) 값으로 도출하도록 통일.
      → OptimizerAgent(1차, 시설 공통 거친 필터)가 PersonalizeAgent(2차,
        개인별 정밀 필터)와 같은 기준 체계 위에서 동작하게 됨.
      → 1차에서 이미 보수적으로 잡아주므로 2차의 대체 부담이 줄어듦.

이 둘은 의도적으로 2단계 구조를 유지함:
  1단계(OptimizerAgent)  : 모든 환자에게 최소한 안전한 보수적 공통 식단 탐색
  2단계(PersonalizeAgent): 그 식단을 개인 constraint로 정밀 재검증, 위반 슬롯만 교체
  (boost_nutrients처럼 "최대화" 목표는 NSGA-II의 위반-회피 구조와 맞지 않아
   2단계에만 둠 — 1단계에 넣으려면 별도 목적함수 추가가 필요해 복잡도 대비
   이득이 작다고 판단)
"""
from dataclasses import dataclass
from collections import defaultdict
from patient_profile_final import (
    PatientProfile, NutritionConstraint,
    load_patients_from_excel, ENERGY_MIN_SENIOR
)


# ──────────────────────────────────────────────────────────────
# 1. FacilityConstraint: 전체 환자 중 가장 엄격한 제약 도출
# ──────────────────────────────────────────────────────────────
@dataclass
class FacilityConstraint:
    """
    시설 전체에 적용하는 통합 제약
    → 각 영양소별로 가장 엄격한(보수적인) 기준 채택
    → NSGA-II의 constraint 자리에 그대로 주입
    """
    energy_min:    float
    energy_max:    float
    sodium_max:    float
    protein_min:   float | None
    protein_max:   float | None
    sugar_max:     float | None
    fat_min:       float | None
    fat_max:       float | None
    sat_fat_max:   float | None
    potassium_min: float | None
    fiber_min:     float | None


def derive_facility_constraint(patients: list[PatientProfile]) -> FacilityConstraint:
    """
    전체 환자 제약을 순회하며 각 항목 최솟값(엄격한 쪽) 채택.

    예외 처리:
    - protein_max: 신장_비투석 환자 중 최솟값 (가장 엄격)
    - protein_min: 단백질 하한이 있는 환자 중 최솟값
      → 신장_비투석이 protein_max를 설정하므로,
         protein_max가 있으면 protein_min은 None으로 처리
         (신장 환자는 단백질 상한이 우선)

    [수정] sugar_max / sat_fat_max / fat_min / fat_max도 sodium_max와
    동일하게 constraints 리스트에서 직접 도출. 이전에는 2400kcal 고정
    계산식이 하드코딩되어 있어 환자별 patient_profile_final.py 기준과
    어긋났음(예: 당뇨 sugar_max는 본인 타겟열량×10%/4인데, 시설 기준은
    2400×20%/4/3 = 끼니당 40g으로 더 느슨하게 적용되고 있었음).

    [수정 2 — 신장질환 환자를 시설 공통 제약 계산에서 제외]
    원인: candidate_agent.py에서 신장질환을 교집합 대상에서 뺐는데(풀이
    신장 기준을 고려하지 않고 만들어짐), 이 함수는 여전히 신장질환 환자의
    매우 엄격한 protein_max를 포함시켜 시설 공통 제약을 계산하고 있었음.
    그 결과 OptimizerAgent가 "신장 기준을 모르는 풀"을 "신장 기준을 강제하는
    제약"으로 검증하게 되어, f1(영양위반)이 4회 재최적화해도 2.5~2.7에서
    전혀 줄지 않는 현상이 발생함(풀의 메뉴 대부분이 신장 단백질 상한을
    구조적으로 위반할 수밖에 없었음).
    해결: 신장질환을 가진 환자는 이 함수의 제약 계산에서 제외하여
    CandidateAgent와 동일한 기준으로 맞춤. 신장질환자 본인의 단백질 제한은
    여전히 PersonalizeAgent의 끼니 단위 정량 보정이 전담함(환자 개인의
    constraint 자체는 변경하지 않음 — 시설 공통 제약 계산에서만 제외).
    """
    # CandidateAgent의 INTERSECTION_EXCLUDED_DISEASES와 동일한 철학:
    # 신장질환 환자는 시설 공통 제약에 영향을 주지 않고, PersonalizeAgent가
    # 개인 단위로 처리함.
    KIDNEY_KEYWORDS = ("신장",)

    def _has_kidney_disease(p: PatientProfile) -> bool:
        return any(kw in d for d in p._resolve_diseases() for kw in KIDNEY_KEYWORDS)

    eligible_patients = [p for p in patients if not _has_kidney_disease(p)]
    excluded_count = len(patients) - len(eligible_patients)

    if not eligible_patients:
        # 전원이 신장질환이면 제외할 수 없으므로 원래 전체 환자로 계산
        print("  [경고] 전체 입소자가 신장질환 — 시설 제약에서 제외할 수 없어 전원 포함합니다.")
        eligible_patients = patients
        excluded_count = 0

    if excluded_count:
        print(f"  [시설 제약 계산] 신장질환 환자 {excluded_count}명 제외 "
              f"(전체 {len(patients)}명 중 {len(eligible_patients)}명으로 계산)")

    constraints = [p.constraint for p in eligible_patients]

    # ── 나트륨: 가장 엄격한(낮은) 값 ──────────────────────────
    sodium_vals = [c.sodium_max for c in constraints if c.sodium_max is not None]
    sodium_max  = min(sodium_vals) if sodium_vals else None

    # ── 단백질 상한 (신장_비투석): 가장 엄격한(낮은) 값 ───────
    # [참고] 신장질환 환자가 eligible_patients에서 제외되었으므로
    # prot_max_vals는 항상 비어있고 protein_max는 None이 됨(정상 동작).
    # 신장질환자 본인의 protein_max는 PersonalizeAgent가 개별 적용함.
    prot_max_vals = [c.protein_max for c in constraints if c.protein_max is not None]
    protein_max   = min(prot_max_vals) if prot_max_vals else None

    # ── 단백질 하한: 신장 환자 없을 때만 적용 (신장이 우선) ────
    if protein_max is None:
        prot_min_vals = [c.protein_min for c in constraints if c.protein_min is not None]
        protein_min   = min(prot_min_vals) if prot_min_vals else None
    else:
        protein_min = None  # 신장_비투석 기준이 우선

    # ── 당류: 가장 엄격한(낮은) 값 — 환자 constraint에서 직접 도출 ──
    sugar_vals = [c.sugar_max for c in constraints if c.sugar_max is not None]
    sugar_max  = min(sugar_vals) if sugar_vals else None

    # ── 포화지방: 가장 엄격한(낮은) 값 ─────────────────────────
    sat_fat_vals = [c.sat_fat_max for c in constraints if c.sat_fat_max is not None]
    sat_fat_max  = min(sat_fat_vals) if sat_fat_vals else None

    # ── 지방 범위: 교집합 (하한 최대, 상한 최소) ───────────────
    fat_min_vals = [c.fat_min for c in constraints if c.fat_min is not None]
    fat_max_vals = [c.fat_max for c in constraints if c.fat_max is not None]
    fat_min = max(fat_min_vals) if fat_min_vals else None
    fat_max = min(fat_max_vals) if fat_max_vals else None
    # 교집합이 깨지는 경우(fat_min > fat_max) 방어 — 더 엄격한 쪽(상한)을 우선
    if fat_min is not None and fat_max is not None and fat_min > fat_max:
        fat_min = fat_max

    # ── 칼륨·식이섬유: 가장 엄격한(높은) 값 ────────────────────
    pot_vals = [c.potassium_min for c in constraints if c.potassium_min is not None]
    fib_vals = [c.fiber_min     for c in constraints if c.fiber_min     is not None]

    return FacilityConstraint(
        energy_min    = ENERGY_MIN_SENIOR,
        energy_max    = 800,
        sodium_max    = sodium_max,
        protein_min   = protein_min,
        protein_max   = protein_max,
        sugar_max     = sugar_max,
        fat_min       = fat_min,
        fat_max       = fat_max,
        sat_fat_max   = sat_fat_max,
        potassium_min = max(pot_vals) if pot_vals else None,
        fiber_min     = max(fib_vals) if fib_vals else None,
    )


def print_facility_constraint(fc: FacilityConstraint):
    print("\n=== 시설 통합 영양 제약 (NSGA-II 입력) ===")
    print(f"  에너지:    {fc.energy_min}~{fc.energy_max} kcal/끼니")
    print(f"  나트륨:    ≤ {fc.sodium_max} mg")
    if fc.protein_max:
        print(f"  단백질:    ≤ {fc.protein_max:.1f} g  (신장_비투석 최엄격 기준)")
    elif fc.protein_min:
        print(f"  단백질:    ≥ {fc.protein_min:.1f} g")
    if fc.sugar_max:    print(f"  당류:      ≤ {fc.sugar_max:.1f} g")
    if fc.sat_fat_max:  print(f"  포화지방:  ≤ {fc.sat_fat_max:.1f} g")
    if fc.fat_min:      print(f"  지방:      {fc.fat_min:.1f}~{fc.fat_max:.1f} g")
    if fc.potassium_min:print(f"  칼륨:      ≥ {fc.potassium_min} mg")
    if fc.fiber_min:    print(f"  식이섬유:  ≥ {fc.fiber_min} g")


# ──────────────────────────────────────────────────────────────
# 2. 전체 질환 목록 → get_candidates_by_category() 입력
# ──────────────────────────────────────────────────────────────
def get_all_diseases(patients: list[PatientProfile]) -> list[str]:
    """전체 입소자 질환을 합집합으로 반환 (중복 제거)"""
    diseases = set()
    for p in patients:
        diseases.update(p._resolve_diseases())
    # 연하장애·치매는 텍스처 처리이므로 제외
    skip = {"연하장애", "치매"}
    return list(diseases - skip)


# ──────────────────────────────────────────────────────────────
# 3. MealPlanProblem에 FacilityConstraint 주입
#    → 기존 코드 변경 최소화: constraint 덕타이핑 활용
# ──────────────────────────────────────────────────────────────
class FacilityConstraintAdapter:
    def __init__(self, fc: FacilityConstraint):
        # ── 끼니 단위 기준 ───────────────────────
        self.energy_min    = fc.energy_min
        self.energy_max    = fc.energy_max
        self.sodium_max    = fc.sodium_max
        self.protein_min   = fc.protein_min
        self.protein_max   = fc.protein_max
        self.sugar_max     = fc.sugar_max
        self.fat_min       = fc.fat_min
        self.fat_max       = fc.fat_max
        self.sat_fat_max   = fc.sat_fat_max
        self.potassium_min = fc.potassium_min
        self.fiber_min     = fc.fiber_min

        # ── 일일 기준 (키 이름: daily_{Neo4j키}_{min/max}) ──
        self.daily_energy_min    = 1500    # KDRIs 65세↑ 최소
        self.daily_energy_max    = 2400    # KDRIs 65세↑ 최대
        self.daily_sodium_max    = 2400    # 특수의료용도식품 고혈압 * 3
        self.daily_sugar_max     = 120     # 총 에너지 섭취량의 20% 이내
        self.daily_sat_fat_max   = 7
        self.daily_potassium_min = 3500
        self.daily_fiber_min     = 25 
        self.daily_fat_min       = round(2400 * 0.15 / 9, 1)   # 40.0g
        self.daily_fat_max       = round(2400 * 0.30 / 9, 1)   # 80.0g

        # 끼니 기준 없이 일일로만 관리
        self.daily_protein_min = (fc.protein_min * 3) if fc.protein_min else 40.0
        self.daily_carb_max      = 390     # 탄수화물 1일 325g 이하 (65% × 2000kcal)

        # [제거] daily_vit_d_min — NSGA-II의 위반-회피 구조에서 항상 100% 위반으로
        # 잡혀(실측: daily_v 28.0/28일, 일별 위반의 절반 이상을 이 항목 혼자 차지)
        # f1을 의미 없이 부풀리고 다른 위반을 가렸음. 한식 일반 메뉴(밥/국/나물/김치)에
        # 비타민D가 거의 없어 "위반 회피"가 아니라 "최대화 보강"이 맞는 항목이므로
        # PersonalizeAgent의 boost_nutrients(끼니 단위 정량 보강)로 이전.
        # 모든 환자 공통 보강 대상이라 patient_profile_final.py의 COMMON_BOOST_NUTRIENTS에서 처리.


# ──────────────────────────────────────────────────────────────
# 4. ServingAgent: 개인별 배식량 산출
# ──────────────────────────────────────────────────────────────
class ServingAgent:
    """
    Neo4j 메뉴 실제 중량(weight) × ratio → 개인별 배식량 산출
    """

    def __init__(self, patients: list[PatientProfile]):
        self.patients = {p.name: p for p in patients}

    @staticmethod
    def calc_optimal_ratio(p: PatientProfile, meal_nutrition: dict) -> float:
        """
        끼니 전체 영양소 기준으로 개인 constraint 만족하는 최대 ratio 산출
        meal_nutrition: {"energy": 520, "protein": 22, "sodium": 680}
        """
        c = p.constraint
        ratio_candidates = [1.0]

        if c.energy_max and meal_nutrition.get("energy", 0) > 0:
            ratio_candidates.append(c.energy_max / meal_nutrition["energy"])

        if c.sodium_max and meal_nutrition.get("sodium", 0) > 0:
            ratio_candidates.append(c.sodium_max / meal_nutrition["sodium"])

        if c.protein_max and meal_nutrition.get("protein", 0) > 0:
            ratio_candidates.append(c.protein_max / meal_nutrition["protein"])

        ratio_max = min(ratio_candidates)

        ratio_min = 0.5
        if c.energy_min and meal_nutrition.get("energy", 0) > 0:
            ratio_min = max(ratio_min, c.energy_min / meal_nutrition["energy"])

        return round(max(ratio_min, ratio_max), 3)

    def get_serving(self, name: str, menu_by_category: dict) -> dict:
        """
        menu_by_category: 카테고리별 Neo4j 메뉴 dict
          {"밥": {"energy":280, "weight":210, ...},
           "국": {"energy":45,  "weight":200, ...}, ...}
        """
        p = self.patients[name]

        # 끼니 전체 영양소 합산
        meal_nutrition = {
            "energy":  sum(m.get("energy",  0) for m in menu_by_category.values()),
            "protein": sum(m.get("protein", 0) for m in menu_by_category.values()),
            "sodium":  sum(m.get("sodium",  0) for m in menu_by_category.values()),
        }

        ratio = self.calc_optimal_ratio(p, meal_nutrition)

        # 각 음식 실제 weight × ratio
        result = {
            cat: round(info.get("weight", 0) * ratio)
            for cat, info in menu_by_category.items()
        }
        result["ratio"] = ratio
        return result

    def serving_table(self) -> str:
        """전체 환자 타겟열량·식사형태 그룹 요약"""
        groups = defaultdict(list)
        for p in self.patients.values():
            key = (p.meal_texture_rice, p.meal_texture_side,
                   f"{int(p.target_energy)}kcal")
            groups[key].append(p.name)

        lines = ["\n=== 배식량 그룹 요약 ===",
                 f"{'밥형태':<6} {'찬형태':<8} {'타겟열량':>10}  {'인원':>4}  대표 대상자"]
        for (rice, side, kcal), names in sorted(groups.items()):
            rep = ", ".join(names[:3]) + ("..." if len(names) > 3 else "")
            lines.append(f"{rice:<6} {side:<8} {kcal:>10}  {len(names):>3}명  {rep}")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 5. ProcessingAgent: 조리 지침서 생성
# ──────────────────────────────────────────────────────────────
class ProcessingAgent:
    """오늘 메뉴 + 환자 구성 → 조리 지침서 + LLM 프롬프트"""

    def __init__(self, patients: list[PatientProfile]):
        self.patients = patients
        self.total    = len(patients)

        # 저염 대상: 나트륨 제한이 있는 환자
        self.low_salt = [p for p in patients if p.constraint.sodium_max is not None]
        self.normal   = [p for p in patients if p.constraint.sodium_max is None]

        # 텍스처 그룹
        self.minced  = [p for p in patients if p.meal_texture_side == "다진찬"]
        self.blended = [p for p in patients if p.meal_texture_side == "갈찬"]
        self.normal_side = [p for p in patients if p.meal_texture_side == "일반찬"]
        self.porridge = [p for p in patients if p.meal_texture_rice == "죽"]

        # 신장 환자 (칼륨 주의)
        self.kidney   = [p for p in patients
                         if any("신장" in d for d in p._resolve_diseases())]
        # 당뇨 환자 (잡곡밥)
        self.diabetes = [p for p in patients
                         if any("당뇨" in d for d in p._resolve_diseases())]

    def build_guide(self, menu: dict, day: int, meal: str) -> str:
        t = self.total
        ls = len(self.low_salt)
        nm = len(self.normal)
        mi = len(self.minced)
        bl = len(self.blended)
        po = len(self.porridge)
        ki = len(self.kidney)
        di = len(self.diabetes)

        lines = [
            f"\n{'='*58}",
            f"📋  {day}일차 {meal}  조리 지침서  (총 {t}명)",
            f"{'='*58}",
            f"메뉴: {menu.get('밥')} | {menu.get('국')} | {menu.get('주찬')}"
            f" | {menu.get('부찬1')} | {menu.get('부찬2')} | {menu.get('김치')}",
            "",
            "【 준비 단계 】",
            f"  ① 저염 분리:  {ls}명 / 일반 간: {nm}명",
            f"  ② 텍스처:     일반찬 {len(self.normal_side)}명 / "
            f"다진찬 {mi}명 / 갈찬 {bl}명",
            f"  ③ 죽식:       {po}명 별도 죽 조리",
            f"  ④ 신장 주의:  {ki}명 (고칼륨 식재료 제한)",
            f"  ⑤ 당뇨 잡곡:  {di}명 잡곡밥 제공",
            "",
            f"【 밥 조리 】",
            f"  - 일반 잡곡밥: {t - po}명",
        ]
        if di:
            di_names = ", ".join(p.name for p in self.diabetes[:5])
            lines.append(f"    → 당뇨 {di}명 잡곡 비율 현미40%+잡곡20%+백미40% 권장")
            lines.append(f"      대상: {di_names}{'...' if di > 5 else ''}")
        if po:
            po_names = ", ".join(p.name for p in self.porridge)
            lines.append(f"  - 죽 조리: {po}명 → {po_names}")

        lines += [
            "",
            f"【 {menu.get('국')} 】",
            f"  1. 재료 준비 후 끓이기 시작",
            f"  2. ★ 소금/된장 투입 전 → 저염 {ls}명분({ls/t*100:.0f}%) 먼저 덜기",
            f"     보관: '저염국' 라벨 표시",
            f"  3. 나머지 {nm}명분에 일반 간 맞추기",
        ]
        if ki:
            ki_names = ", ".join(p.name for p in self.kidney[:4])
            lines.append(f"  ⚠ 신장 {ki}명 고칼륨 재료(시금치·토마토) 배제 확인")
            lines.append(f"    대상: {ki_names}{'...' if ki > 4 else ''}")

        for dish_key in ["주찬", "부찬1", "부찬2"]:
            dish = menu.get(dish_key, "-")
            lines += [
                "",
                f"【 {dish_key}: {dish} 】",
                f"  1. 전체 {t}명분 조리",
                f"  2. ★ 저염 {ls}명분 먼저 덜기 (소스·양념 투입 전)",
                f"  3. 텍스처 처리:",
                f"     → 일반찬 {len(self.normal_side)}명: 그대로",
                f"     → 다진찬 {mi}명:  칼로 잘게 다지기",
            ]
            if bl:
                lines.append(f"     → 갈찬   {bl}명:  믹서기 (물 20% 추가)")

        lines += [
            "",
            "【 배식 순서 】",
            f"  ① 죽식 {po}명 먼저 배식",
            f"  ② 갈찬 {bl}명 → 블렌더 처리 후 배식",
            f"  ③ 다진찬 {mi}명 → 다진 찬 배식",
            f"  ④ 일반찬 {len(self.normal_side)}명 → 일반 배식",
            "",
            "【 개인별 특이사항 】",
        ]
        for p in self.patients:
            notes = self._individual_notes(p)
            if notes:
                lines.append(f"  • {p.name} ({p.disease_type_label}): {notes}")

        return "\n".join(lines)

    def _individual_notes(self, p: PatientProfile) -> str:
        notes = []
        resolved = p._resolve_diseases()
        if any("신장" in d for d in resolved):
            notes.append("고칼륨 재료 제한 (칼륨≤650mg/끼니)")
        if any("당뇨" in d for d in resolved):
            notes.append("잡곡밥 / 당류 엄격 제한")
        if p.meal_texture_rice == "죽":
            notes.append("죽식 제공")
        if p.meal_texture_side == "갈찬":
            notes.append("갈찬(믹서기)")
        return " / ".join(notes)

    def build_llm_prompt(self, menu: dict, recipe_nodes: dict = None) -> str:
        """GPT-4o에 넘길 조리법 생성 프롬프트 (Neo4j Recipe Node 연동)"""
        recipe_section = ""
        if recipe_nodes:
            recipe_section = "\n【 Neo4j 레시피 정보 】\n"
            for dish, info in recipe_nodes.items():
                recipe_section += f"  {dish}: {info}\n"

        return f"""당신은 요양원 영양사입니다.
아래 메뉴에 대해 {self.total}명분 상세 조리 지침서를 작성하세요.

【 오늘 메뉴 】
  밥: {menu.get('밥')} | 국: {menu.get('국')} | 주찬: {menu.get('주찬')}
  부찬1: {menu.get('부찬1')} | 부찬2: {menu.get('부찬2')} | 김치: {menu.get('김치')}

【 입소자 현황 ({self.total}명) 】
  저염 대상(고혈압/신장): {len(self.low_salt)}명
  다진찬: {len(self.minced)}명 / 갈찬: {len(self.blended)}명
  죽식:   {len(self.porridge)}명
  신장질환(칼륨 제한): {len(self.kidney)}명
  당뇨(잡곡밥): {len(self.diabetes)}명
{recipe_section}
【 작성 요구사항 】
1. 메뉴별 재료량 ({self.total}인분 기준)
2. 저염 처리: 소금/간장/된장 투입 시점 및 분리 방법
3. 다진찬 조리법 (칼 다지기, 크기 기준)
4. 갈찬 조리법 (믹서기, 물 비율, 점도 기준)
5. 죽 조리법 ({len(self.porridge)}명분)
6. 신장 환자용 고칼륨 재료 대체 방법
7. 당뇨 환자용 잡곡밥 비율 (현미/잡곡/백미)

실무자가 즉시 사용 가능하도록 단계별로 구체적으로 작성하세요.
""".strip()


# ──────────────────────────────────────────────────────────────
# 통합 실행 함수
# ──────────────────────────────────────────────────────────────
def setup_facility(excel_path: str, budget_per_meal: float = 10000):
    """
    전체 파이프라인 초기화.
    반환값을 run_nsga2()에 바로 연결.
    """
    patients   = load_patients_from_excel(excel_path, budget_per_meal)
    fc         = derive_facility_constraint(patients)
    adapter    = FacilityConstraintAdapter(fc)
    diseases   = get_all_diseases(patients)
    serving    = ServingAgent(patients)
    processing = ProcessingAgent(patients)

    return {
        "patients":   patients,
        "constraint": adapter,       # → MealPlanProblem(pool, constraint=adapter, ...)
        "diseases":   diseases,      # → get_candidates_by_category(diseases=diseases, ...)
        "serving":    serving,
        "processing": processing,
        "facility_constraint": fc,
    }


# ── 테스트 실행 ────────────────────────────────────────────────
# if __name__ == "__main__":
#     fac = setup_facility("고령자.xlsx", budget_per_meal=10000)

#     print_facility_constraint(fac["facility_constraint"])

#     print(f"\n=== Neo4j 질환 쿼리 입력값 ===")
#     print(f"  diseases = {fac['diseases']}")

#     print(fac["serving"].serving_table())

#     sample_menu = {
#         "밥": "잡곡밥", "국": "된장국",
#         "주찬": "생선조림", "부찬1": "시금치나물",
#         "부찬2": "콩자반", "김치": "배추김치"
#     }
#     proc = fac["processing"]
#     print(proc.build_guide(sample_menu, day=1, meal="점심"))

#     print("\n" + "="*58)
#     print("【 LLM 조리법 프롬프트 】")
#     print("="*58)
#     print(proc.build_llm_prompt(sample_menu))