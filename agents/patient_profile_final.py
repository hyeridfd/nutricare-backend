# 초이스엔 고령자 파일 -> 당뇨병, 고혈압, 신장질환(요양원 내에서 통상 비투석) 기준으로 나눔(다수질환 고려)
# 영양기준 patient profile 기준 -> 현재식사현황 참고해 죽, 다진찬, 갈찬 등 메뉴 세분화
# 나이, 신장, 체중 고려해 칼로리 도출 -> 구성되 메뉴의 양 조정
# 유형별 메뉴 보고서, 개인별 보고서, 조리 지침서

"""
patient_profile_final.py
기존 PatientProfile 기준 유지 + 고령자 최소열량 보정 추가

[수정 — 2026-07-01] 3단계 설계로 재정리 (요청 반영)
====================================================
① 1단계(NSGA-II 최적화, 전체 환자 공통): 당뇨병+고혈압 합집합 기준을
   "고정된 기준 열량"으로 계산해 모든 환자에게 동일하게 적용.
   - 당류: 기준열량×0.1/4 미만
   - 단백질: 18g 이상
   - 지방: 기준열량×0.15/9 ~ 기준열량×0.30/9
   - 포화지방: 기준열량×0.07/9 이하
   - 나트륨: 1,350mg 미만 (고혈압 단독 기준 800mg은 메뉴 풀 자체가
     구조적으로 못 맞춰서, 당뇨병 기준으로 완화. 대신 고혈압 환자는
     조리 단계에서 저염 처리— facility_optimization.py의
     ProcessingAgent.build_guide()가 이미 sodium_max가 설정된 환자를
     "저염 대상"으로 분리해 조리 지침에 반영하고 있으므로 별도 코드
     추가 없이 그대로 적용됨)

   [원인] 기존에는 이 항목들이 각 환자 개인의 target_energy(BMI로 계산)에
   비례해서 계산되어, 같은 질환 조합이어도 체중·키가 다르면 기준치가
   미묘하게(소수점 단위로) 달라졌음. 그 결과 PersonalizeAgent의
   "유형별 그룹핑"이 예상보다 훨씬 잘게 쪼개지는 문제가 있었음(15개
   유형 중 8개가 1명짜리로 튀어나옴). 고정 기준열량(ENERGY_MAX=800,
   끼니 기준 상한과 동일)으로 계산하도록 바꿔서, 같은 질환 조합인
   환자는 항상 완전히 동일한 기준치를 갖도록 함 — BMI에 따른 실제
   배식량 조절은 여전히 ServingAgent의 ratio가 담당하므로 개인차가
   사라지는 게 아니라 "정성적 기준(무엇을 얼마나%)"과 "정량적 배식량
   (실제 몇 g)"의 역할이 분리됨.

② 2단계(PersonalizeAgent 고정 대체찬, 고혈압 보유 환자만): 칼륨≥700mg,
   식이섬유≥7g 미달 시 끼니당 부찬 1개를 보강 메뉴로 대체.
   고혈압 단독이든 당뇨/치매와 병존이든(고혈압+당뇨, 고혈압+치매,
   고혈압+당뇨+치매) "고혈압이 있는지" 여부로만 판단 — 다른 질환
   동반 여부나 체중 등 개인 신체 정보와 무관.
   [수정] potassium_min/fiber_min을 고혈압 DISEASE_CRITERIA에서 제거함
   (700/7 자체는 원래도 고정값이라 BMI 무관이었지만, ①단계 NSGA-II
   최적화 대상에서 완전히 빼서 personalize_agent.py의 전용 고정
   대체찬 로직으로 일원화 — 구현은 personalize_agent.py 참고).

③ 3단계(PersonalizeAgent 고정 대체찬, 치매 보유 환자만): 기존
   boost_nutrients(철분/비타민A/티아민/비타민C/비타민D) 로직 그대로
   유지 — 원래부터 disease membership만으로 결정되고 BMI와 무관했음.
"""
from dataclasses import dataclass, field
from enum import IntEnum, Enum
from typing import Optional
import pandas as pd


class DiseasePriority(IntEnum):
    KIDNEY       = 1
    DIABETES     = 2
    CANCER       = 2
    HYPERTENSION = 3
    OBESITY      = 3

DISEASE_KEY_MAP = {
    "당뇨병":      "DIABETES",
    "신장질환": "KIDNEY",
    "암":          "CANCER",
    "고혈압":      "HYPERTENSION",
    "비만":        "OBESITY",
    "연하장애":    None,
    "치매":        None,
}
    #"신장_투석":   "KIDNEY",
@dataclass
class NutritionConstraint:
    energy_min:    Optional[float] = None
    energy_max:    Optional[float] = None
    sugar_max:     Optional[float] = None
    protein_min:   Optional[float] = None
    protein_max:   Optional[float] = None
    fat_min:       Optional[float] = None
    fat_max:       Optional[float] = None
    sat_fat_max:   Optional[float] = None
    sodium_max:    Optional[float] = None
    potassium_min: Optional[float] = None
    fiber_min:     Optional[float] = None
    # 치매: 기준치 없이 "많을수록 좋음" 영양소 — 위반 판정에는 쓰지 않고
    # PersonalizeAgent에서 부족 시 보강 후보를 고르는 데만 사용
    boost_nutrients: list[str] = field(default_factory=list)

# ── 고령자 에너지 범위 ────────────────────────────────────────
# 요양원 노인 기준: 끼니당 최소 500kcal 보장 (근감소·영양불량 예방)
ENERGY_MIN_SENIOR = 500   # ← 핵심 보정값
ENERGY_MAX        = 800

# [추가 — 2026-07-01] 당뇨병/고혈압의 당류·지방·포화지방 기준을 계산할 때
# 쓰는 "고정 기준열량". 원래는 각 환자의 target_energy(BMI로 계산되어
# 사람마다 다름)를 썼는데, 그러면 같은 질환 조합이어도 체중이 다르면
# 기준치가 미묘하게 달라져 PersonalizeAgent의 유형 그룹핑이 잘게
# 쪼개지는 문제가 있었음. ENERGY_MAX(끼니 기준 상한, 800kcal)를 그대로
# 재사용해 모든 환자에게 동일한 기준을 적용함.
COMMON_CRITERIA_ENERGY_REF = ENERGY_MAX

DISEASE_CRITERIA = {
    "당뇨병": lambda e: NutritionConstraint(
        energy_min=ENERGY_MIN_SENIOR, energy_max=ENERGY_MAX,
        # [수정] e(개인별 target_energy) 대신 고정 기준열량 사용
        sugar_max=round(COMMON_CRITERIA_ENERGY_REF * 0.1 / 4, 2),
        protein_min=18,                          # 단백질 18g 이상
        sat_fat_max=round(COMMON_CRITERIA_ENERGY_REF * 0.1 / 9, 2),
        sodium_max=1350,                         # 나트륨 1,350mg 이하
    ),
    # "신장_투석": lambda e: NutritionConstraint(
    #     energy_min=ENERGY_MIN_SENIOR, energy_max=ENERGY_MAX,
    #     protein_min=round(e * 0.12 / 4, 2),
    #     sodium_max=650,
    # ),
    # 비투석
    "신장질환": lambda e: NutritionConstraint(
        energy_min=ENERGY_MIN_SENIOR, energy_max=ENERGY_MAX,
        protein_max=round(e * 0.1 / 4, 2)
        # [참고] 신장질환은 이번 재정리 범위에서 제외 — 체중 비례 단백질
        # 제한이 임상적으로 의미 있는 개인차이므로 target_energy(e) 그대로 유지.
        # facility_optimization.py가 이미 신장질환을 시설 공통 최적화에서
        # 제외하고 PersonalizeAgent가 개인 단위로 처리하도록 분리해 둠.
        #sodium_max=650,
    ),
    "암": lambda e: NutritionConstraint(
        energy_min=ENERGY_MIN_SENIOR, energy_max=ENERGY_MAX,
        protein_min=round(e * 0.18 / 4, 2),
        fat_min=round(e * 0.15 / 9, 2), fat_max=round(e * 0.35 / 9, 2),
        sat_fat_max=round(e * 0.07 / 9, 2),
        sodium_max=1350,
    ),
    "고혈압": lambda e: NutritionConstraint(
        energy_min=ENERGY_MIN_SENIOR, energy_max=ENERGY_MAX,
        # [수정] e(개인별 target_energy) 대신 고정 기준열량 사용
        fat_min=round(COMMON_CRITERIA_ENERGY_REF * 0.15 / 9, 2),
        fat_max=round(COMMON_CRITERIA_ENERGY_REF * 0.30 / 9, 2),
        sat_fat_max=round(COMMON_CRITERIA_ENERGY_REF * 0.07 / 9, 2),
        # [수정] 800 → 1350 (당뇨병 기준과 통일). 800mg은 메뉴 풀 구조상
        # NSGA-II로 도달 불가능한 수준이었음(실측 위반비율 평균 1.03).
        # 저염 처리는 조리 단계(ProcessingAgent의 low_salt 분리 지침)에서
        # 계속 담당 — sodium_max가 설정된 환자는 여전히 "저염 대상"으로
        # 분류되므로 이 부분은 코드 변경 없이 그대로 유지됨.
        sodium_max=1350,
        # [제거] potassium_min=700, fiber_min=7 — NSGA-II 공통 최적화
        # 대상에서 빼고, personalize_agent.py의 전용 2단계 고정 대체찬
        # 로직(고혈압 보유 환자 전원, 끼니당 최대 1개)으로 이전.
    ),
    "비만": lambda e: NutritionConstraint(
        energy_min=ENERGY_MIN_SENIOR, energy_max=700,  # 비만도 고령자는 700 상한
        sugar_max=round(e * 0.1 / 4, 2),
        protein_min=18,
        sat_fat_max=round(e * 0.1 / 9, 2),
        sodium_max=1350,  # 비만만
    ),
    "연하장애": lambda e: NutritionConstraint(
        energy_min=ENERGY_MIN_SENIOR, energy_max=ENERGY_MAX,
    ),
    "치매": lambda e: NutritionConstraint(
        energy_min=ENERGY_MIN_SENIOR, energy_max=ENERGY_MAX,
        # 기준치 없음 — 5개 영양소를 "끼니 내 최대한 많이" 포함하는 게 목표
        boost_nutrients=["iron", "vit_a", "thiamin", "vit_c", "vit_d"],
    ),
}


def merge_constraints(diseases: list[str], energy: float) -> NutritionConstraint:
    def priority_key(d):
        key = DISEASE_KEY_MAP.get(d)
        return DiseasePriority[key] if key and key in DiseasePriority.__members__ else 99

    sorted_diseases = sorted(diseases, key=priority_key)
    merged = NutritionConstraint()
    kidney_found = any("신장" in d for d in diseases)

    for d in sorted_diseases:
        c = DISEASE_CRITERIA[d](energy)
        if c.energy_min: merged.energy_min = max(merged.energy_min or 0,    c.energy_min)
        if c.energy_max: merged.energy_max = min(merged.energy_max or 9999, c.energy_max)
        if "신장" in d:
            merged.protein_min = c.protein_min
            merged.protein_max = c.protein_max
        elif not kidney_found:
            if c.protein_min: merged.protein_min = max(merged.protein_min or 0, c.protein_min)
        if c.sodium_max:  merged.sodium_max  = min(merged.sodium_max  or 9999, c.sodium_max)
        if c.sat_fat_max: merged.sat_fat_max = min(merged.sat_fat_max or 9999, c.sat_fat_max)
        if c.sugar_max:   merged.sugar_max   = min(merged.sugar_max   or 9999, c.sugar_max)
        if c.fat_min: merged.fat_min = max(merged.fat_min or 0,    c.fat_min)
        if c.fat_max: merged.fat_max = min(merged.fat_max or 9999, c.fat_max)
        if c.potassium_min: merged.potassium_min = c.potassium_min
        if c.fiber_min:     merged.fiber_min     = c.fiber_min
        if c.boost_nutrients:
            merged.boost_nutrients = list(set(merged.boost_nutrients) | set(c.boost_nutrients))

    return merged


class Sex(str, Enum):
    MALE   = "male"
    FEMALE = "female"

def bmi_score(bmi: float) -> float:
    if bmi < 18.5:   return 1.0
    elif bmi < 23.0: return 1.0 - (bmi - 18.5) / (23.0 - 18.5) * 0.4
    elif bmi < 25.0: return 0.6 - (bmi - 23.0) / (25.0 - 23.0) * 0.3
    else:            return max(0.0, 0.3 - (bmi - 25.0) * 0.03)

def waist_score(waist_cm: float, sex: Sex) -> float:
    threshold = 90.0 if sex == Sex.MALE else 85.0
    if waist_cm < threshold: return 0.0
    return -min(0.15, (waist_cm - threshold) * 0.03)

def calc_target_energy(bmi, waist_cm, sex,
                       energy_min=ENERGY_MIN_SENIOR,
                       energy_max=ENERGY_MAX) -> float:
    """
    BMI/허리 기반 score → 타겟열량
    고령자 최소 보장: energy_min = ENERGY_MIN_SENIOR (500kcal)

    [참고 — 2026-07-01] 이 target_energy는 여전히 BMI에 따라 사람마다
    다름 — ServingAgent가 실제 배식량(ratio)을 정할 때 계속 사용됨.
    바뀐 건 "무엇을 얼마나 %로 먹어야 하는지"를 정하는 DISEASE_CRITERIA
    쪽(당뇨/고혈압의 당류·지방·포화지방)만 고정 기준열량을 쓰도록 한 것.
    """
    score = max(0.0, min(1.0, bmi_score(bmi) + waist_score(waist_cm, sex)))
    return round(energy_min + score * (energy_max - energy_min), 0)


class MealTexture(str, Enum):
    REGULAR      = "일반식"
    REGULAR_SIDE = "일반찬"
    PORRIDGE     = "죽"
    MINCED       = "다진찬"
    PUREED       = "갈찬"

class KidneyType(str, Enum):
    DIALYSIS     = "신장_투석"
    NON_DIALYSIS = "신장질환"  #비투석


@dataclass
class PatientProfile:
    name:            str
    sex:             Sex
    age:             int
    bmi:             float
    waist_cm:        float
    diseases:        list[str]
    budget_per_meal: float
    kidney_type:     Optional[KidneyType] = None
    meal_texture_rice: str = "밥"
    meal_texture_side: str = "일반찬"

    target_energy: float = field(init=False)
    constraint:    NutritionConstraint = field(init=False)

    def __post_init__(self):
        self._validate()
        resolved = self._resolve_diseases()
        e_max = 700 if "비만" in resolved else ENERGY_MAX
        self.target_energy = calc_target_energy(
            self.bmi, self.waist_cm, self.sex,
            energy_min=ENERGY_MIN_SENIOR,
            energy_max=e_max,
        )
        # [참고] merge_constraints에는 여전히 self.target_energy(BMI 기반)를
        # 넘김 — 신장질환/암처럼 개인차를 유지해야 하는 질환 기준은 그대로
        # target_energy를 씀. 당뇨/고혈압의 당류·지방·포화지방만
        # DISEASE_CRITERIA 내부에서 COMMON_CRITERIA_ENERGY_REF(고정값)를
        # 쓰도록 이미 바뀌어 있어서, 여기서 넘기는 e 값과 무관하게 항상
        # 동일한 결과가 나옴.
        self.constraint = merge_constraints(resolved, self.target_energy)

    def _validate(self):
        valid = set(DISEASE_CRITERIA.keys())
        invalid = [d for d in self.diseases if d not in valid]
        if invalid:
            raise ValueError(f"알 수 없는 질환: {invalid}")
        if any("신장" in d for d in self.diseases) and self.kidney_type is None:
            raise ValueError("신장질환이 있으면 kidney_type 을 지정해야 합니다.")

    def _resolve_diseases(self) -> list[str]:
        resolved = []
        for d in self.diseases:
            if "신장" in d:
                resolved.append(self.kidney_type.value)
            else:
                resolved.append(d)
        return resolved

    @property
    def disease_type_label(self) -> str:
        resolved = self._resolve_diseases()
        flags = {
            "D": any("당뇨" in d for d in resolved),
            "H": any("고혈압" in d for d in resolved),
            "K": any("신장" in d for d in resolved),
            "M": any("치매" in d for d in resolved),   # ← 치매(Mind/dementia) 추가
        }
        code = "".join(k for k, v in flags.items() if v)
        return f"{code}형" if code else "일반형"

    def summary(self) -> str:
        c = self.constraint
        lines = [
            f"[{self.name}] {self.disease_type_label} | "
            f"{self.meal_texture_rice}/{self.meal_texture_side}",
            f"  BMI:{self.bmi:.1f} | 타겟열량:{self.target_energy:.0f}kcal/끼니"
            f"  ({c.energy_min or 500:.0f}~{c.energy_max or 800:.0f}kcal)",
            f"  나트륨≤{c.sodium_max or '-'}mg | 단백질:"
            + (f"≤{c.protein_max:.1f}g (신장질환)" if c.protein_max else
               f"≥{c.protein_min:.1f}g" if c.protein_min else "-"),
        ]
        extras = []
        if c.potassium_min: extras.append(f"칼륨≥{c.potassium_min}mg")
        if c.fiber_min:     extras.append(f"식이섬유≥{c.fiber_min}g")
        if c.sugar_max:     extras.append(f"당류≤{c.sugar_max:.1f}g")
        if c.sat_fat_max:   extras.append(f"포화지방≤{c.sat_fat_max:.1f}g")
        if extras: lines.append("  " + " | ".join(extras))
        return "\n".join(lines)


def load_patients_from_excel(path: str, budget_per_meal: float = 10000) -> list[PatientProfile]:
    df = pd.read_excel(path)
    has_dementia_col = "치매" in df.columns
    if not has_dementia_col:
        print("  [load_patients_from_excel] 경고: '치매' 컬럼이 엑셀에 없습니다. "
              "치매 환자는 일반형으로 처리됩니다.")

    patients = []
    for _, row in df.iterrows():
        h_m  = row["신장"] / 100
        bmi  = round(row["체중"] / (h_m ** 2), 1)
        sex  = Sex.MALE if row["성별"] == "남" else Sex.FEMALE
        # 허리둘레 없음 → 성별 정상범위 중간값
        waist = 87.0 if sex == Sex.MALE else 82.0

        has_kidney = row["신장질환"] == "O"
        diseases = []
        if row["당뇨병"] == "O": diseases.append("당뇨병")
        if row["고혈압"] == "O": diseases.append("고혈압")
        if has_kidney:           diseases.append("신장질환")
        if has_dementia_col and row["치매"] == "O":
            diseases.append("치매")

        meal_str = str(row["현재식사현황"])
        rice = "죽"   if "죽"  in meal_str else "밥"
        side = "갈찬" if "갈"  in meal_str else \
               "다진찬" if "다진" in meal_str else "일반찬"

        p = PatientProfile(
            name             = row["수급자명"],
            sex              = sex,
            age              = int(row["나이"]),
            bmi              = bmi,
            waist_cm         = waist,
            diseases         = diseases,
            budget_per_meal  = budget_per_meal,
            kidney_type      = KidneyType.NON_DIALYSIS if has_kidney else None,
            meal_texture_rice = rice,
            meal_texture_side = side,
        )
        patients.append(p)
    return patients


if __name__ == "__main__":
    patients = load_patients_from_excel("./data/고령자.xlsx")

    from collections import Counter, defaultdict

    print("=== 질환유형 분포 ===")
    type_count = Counter(p.disease_type_label for p in patients)
    for t, n in sorted(type_count.items(), key=lambda x: -x[1]):
        print(f"  {t}: {n}명")

    print("\n=== 식사형태 분포 ===")
    texture_count = Counter(f"{p.meal_texture_rice}/{p.meal_texture_side}" for p in patients)
    for t, n in sorted(texture_count.items(), key=lambda x: -x[1]):
        print(f"  {t}: {n}명")

    print("\n=== 개인별 요약 (전체) ===")
    for p in patients:
        print(p.summary())

    print("\n=== 질환유형별 영양기준 (대표값) ===")
    groups = defaultdict(list)
    for p in patients: groups[p.disease_type_label].append(p)

    for dtype, grp in sorted(groups.items(), key=lambda x: -len(x[1])):
        energies = [p.target_energy for p in grp]
        rep = grp[0].constraint
        print(f"\n[{dtype}] {len(grp)}명 | "
              f"타겟열량 {min(energies):.0f}~{max(energies):.0f}kcal/끼니")
        print(f"  에너지범위: {rep.energy_min}~{rep.energy_max}kcal")
        print(f"  나트륨 ≤ {rep.sodium_max or '제한없음'} mg")
        if rep.protein_max: print(f"  단백질 ≤ {rep.protein_max:.1f}g (신장_비투석, 열량비례)")
        if rep.protein_min: print(f"  단백질 ≥ {rep.protein_min:.1f}g")
        if rep.potassium_min: print(f"  칼륨 ≥ {rep.potassium_min}mg | 식이섬유 ≥ {rep.fiber_min}g")
        if rep.sugar_max:   print(f"  당류 ≤ {rep.sugar_max:.1f}g | 포화지방 ≤ {rep.sat_fat_max:.1f}g")
        if rep.fat_min:     print(f"  지방 {rep.fat_min:.1f}~{rep.fat_max:.1f}g")