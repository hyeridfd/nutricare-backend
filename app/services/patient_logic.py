"""
services/patient_logic.py — patient_profile_final.py 래퍼
=============================================================
기존 파이프라인 코드(patient_profile_final.py)를 그대로 import해서 쓰되,
API 입력(체중/신장만 받고 허리둘레는 선택)에 맞춘 빌더 함수를 추가합니다.
파이프라인 본체는 수정하지 않고 agents/ 디렉토리를 그대로 패키지로 가져옵니다.
"""

import sys
from pathlib import Path

# agents/ 디렉토리(기존 LangGraph 파이프라인 코드)를 import 경로에 추가.
# 배포 시 이 디렉토리를 백엔드 레포에 그대로 포함시키거나, 서브모듈로 연결.
AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from patient_profile_final import (  # noqa: E402
    PatientProfile, Sex, KidneyType, NutritionConstraint,
    merge_constraints, calc_target_energy, ENERGY_MIN_SENIOR, ENERGY_MAX,
)


def build_patient_profile(
    name: str,
    sex: Sex,
    age: int,
    height_cm: float,
    weight_kg: float,
    diseases: list[str],
    waist_cm: float | None = None,
    kidney_type: KidneyType | None = None,
    meal_texture_rice: str = "밥",
    meal_texture_side: str = "일반찬",
    budget_per_meal: float = 10000,
) -> PatientProfile:
    """
    API 입력(체중/신장 단위)을 받아 PatientProfile을 생성.
    waist_cm이 없으면 patient_profile_final.load_patients_from_excel과
    동일한 방식으로 성별 정상범위 중간값을 기본값으로 사용.
    """
    bmi = round(weight_kg / ((height_cm / 100) ** 2), 1)

    if waist_cm is None:
        waist_cm = 87.0 if sex == Sex.MALE else 82.0

    return PatientProfile(
        name=name,
        sex=sex,
        age=age,
        bmi=bmi,
        waist_cm=waist_cm,
        diseases=diseases,
        budget_per_meal=budget_per_meal,
        kidney_type=kidney_type,
        meal_texture_rice=meal_texture_rice,
        meal_texture_side=meal_texture_side,
    )
