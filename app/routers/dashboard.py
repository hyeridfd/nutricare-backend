"""
routers/dashboard.py — 잔반/영양 알림 현황
=======================================
프론트엔드(src/lib/api.ts의 dashboardApi) 중 mealWaste, alerts 두 엔드포인트를
구현. summary/residents/nutritionIntake는 아직 구현하지 않음(nutritionIntake는
환자별 실제 섭취량을 저장하는 테이블이 아직 없어 별도 작업 필요 — 논의 후 진행).

[참고 — by_disease_type 라벨에 관해]
patients 테이블은 "당뇨병","고혈압" 같은 원본 질환 배열(diseases)만 갖고
있고, 리포트 엑셀에 쓰이는 "HM형" 같은 조합 라벨(disease_type_label)은
PatientProfile 객체가 런타임에 계산하는 값이라 DB에 저장되어 있지 않음.
여기서는 그 계산 로직을 다시 구현하지 않고, diseases 배열을 그대로
콤마로 이어붙인 문자열("당뇨병,고혈압")을 그룹 키로 사용함. 정확한
"HM형" 스타일 라벨이 필요하면 patients 테이블에 disease_type_label
컬럼을 추가해 환자 등록/수정 시점에 계산해 저장하는 방식으로 바꿔야 함.

[참고 — 알림 status에 관해]
nutrition_alerts.status는 현재 pipeline_runner.py가 항상 "sent"로만 저장함
(읽음/해결 처리 워크플로우가 아직 없음). 프론트가 기본값으로 보내는
status=open은 편의상 "sent"와 동일하게 취급함.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from app.services.db_clients import get_supabase

router = APIRouter()

WASTE_RATE_FIELDS = [
    "rice_waste_rate", "soup_waste_rate", "main_dish_waste_rate",
    "side_dish_1_waste_rate", "side_dish_2_waste_rate", "kimchi_waste_rate",
]

# [추가 — 2026-07-01] agents/waste_monitoring_agent.py의 DAILY_TARGETS와
# 동일한 값을 씀(환자 개별 constraint가 아니라 시설 공통 fallback 기준).
# 정확한 개인별 목표치가 필요하면 나중에 환자별 constraint를 조인하는
# 방식으로 확장 가능.
DAILY_TARGETS = {"energy": 1500.0, "protein": 60.0, "carb": 300.0}
DEFICIT_RATIO = 0.8
# 나트륨은 "이 이하로" 기준(상한)이라 energy/protein/carb와 의미가 반대임.
# pct_of_target이 100%를 넘으면 좋다는 뜻이 아니라 과다 섭취라는 뜻이므로
# 프론트에서 표시할 때 주의 필요.
SODIUM_UPPER_LIMIT = 2000.0


def _avg_waste_rate(row: dict) -> float:
    values = [row.get(f, 0) or 0 for f in WASTE_RATE_FIELDS]
    return sum(values) / len(values) if values else 0.0


@router.get("/meal-waste")
def get_meal_waste(facility_id: str, days: int = 7):
    """
    최근 N일 잔반 현황을 끼니별 / 질환조합별 평균 잔반율(%)로 집계.
    """
    sb = get_supabase()

    patients = (
        sb.table("patients")
          .select("id,diseases")
          .eq("facility_id", facility_id)
          .eq("active", True)
          .execute()
          .data
    )
    if not patients:
        return {"by_disease_type": {}, "by_meal": {}}

    patient_ids = [p["id"] for p in patients]
    disease_label_by_id = {
        p["id"]: ",".join(p.get("diseases") or []) or "미분류"
        for p in patients
    }

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    logs = (
        sb.table("waste_logs")
          .select("patient_id,meal_type,recorded_at," + ",".join(WASTE_RATE_FIELDS))
          .in_("patient_id", patient_ids)
          .gte("recorded_at", since)
          .execute()
          .data
    )

    by_meal_rates: dict[str, list[float]] = {}
    by_disease_rates: dict[str, list[float]] = {}

    for row in logs:
        rate = _avg_waste_rate(row)
        meal = row.get("meal_type") or "기타"
        by_meal_rates.setdefault(meal, []).append(rate)

        disease_label = disease_label_by_id.get(row["patient_id"], "미분류")
        by_disease_rates.setdefault(disease_label, []).append(rate)

    by_meal = {
        k: round(sum(v) / len(v) * 100, 1) for k, v in by_meal_rates.items()
    }
    by_disease_type = {
        k: round(sum(v) / len(v) * 100, 1) for k, v in by_disease_rates.items()
    }

    return {"by_disease_type": by_disease_type, "by_meal": by_meal}


@router.get("/nutrition-alerts")
def get_nutrition_alerts(facility_id: str, status: str = "open"):
    """
    영양 부족 알림 + (있으면) GPT가 생성한 처방 텍스트를 함께 반환.
    """
    sb = get_supabase()

    patients = (
        sb.table("patients")
          .select("id,name")
          .eq("facility_id", facility_id)
          .execute()
          .data
    )
    if not patients:
        return []

    id_to_name = {p["id"]: p["name"] for p in patients}
    patient_ids = list(id_to_name.keys())

    query = sb.table("nutrition_alerts").select("*").in_("patient_id", patient_ids)
    # nutrition_alerts.status는 현재 "sent"로만 저장됨(위 모듈 docstring 참고).
    # 프론트 기본값 "open"을 "sent"와 동일하게 취급.
    if status and status not in ("all", "open"):
        query = query.eq("status", status)
    elif status == "open":
        query = query.eq("status", "sent")

    alerts = query.order("sent_at", desc=True).execute().data
    if not alerts:
        return []

    alert_ids = [a["id"] for a in alerts]
    interventions = (
        sb.table("interventions")
          .select("alert_id,prescription_text")
          .in_("alert_id", alert_ids)
          .execute()
          .data
    )
    prescription_by_alert = {i["alert_id"]: i["prescription_text"] for i in interventions}

    result = []
    for a in alerts:
        result.append({
            "id": a["id"],
            "patient_id": a["patient_id"],
            "patient_name": id_to_name.get(a["patient_id"], "-"),
            "nutrient": a.get("nutrient"),
            "consecutive_days": a.get("consecutive_days"),
            "avg_intake": a.get("avg_intake"),
            "standard_value": a.get("standard_value"),
            "deficit_rate": a.get("deficit_rate"),
            "status": a.get("status"),
            "sent_at": a.get("sent_at"),
            "prescription": prescription_by_alert.get(a["id"]),
        })
    return result


def _summary(values: list[float], target: float) -> dict:
    if not values:
        return {"facility_avg": 0.0, "target": target, "pct_of_target": 0.0}
    avg = sum(values) / len(values)
    return {
        "facility_avg": round(avg, 1),
        "target": target,
        "pct_of_target": round(avg / target * 100, 1) if target else 0.0,
    }


@router.get("/nutrition-intake")
def get_nutrition_intake(facility_id: str, days: int = 7):
    """
    [추가 — 2026-07-01] nutrition_intake_logs(끼니별 실제 섭취량)를
    환자별 일일 합계 → 평균으로 재집계. pipeline_runner.py의
    _save_nutrition_intake가 이 테이블을 채움(그 전 실행분은 데이터가
    없을 수 있음 — 이 기능 배포 이후 실행된 것부터 채워짐).
    """
    sb = get_supabase()

    patients = (
        sb.table("patients")
          .select("id,name")
          .eq("facility_id", facility_id)
          .eq("active", True)
          .execute()
          .data
    )
    if not patients:
        return {"by_nutrient": {}, "by_patient": []}

    patient_ids = [p["id"] for p in patients]
    id_to_name = {p["id"]: p["name"] for p in patients}

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    logs = (
        sb.table("nutrition_intake_logs")
          .select("patient_id,day_number,energy_kcal,protein_g,sodium_mg,carb_g,computed_at")
          .in_("patient_id", patient_ids)
          .gte("computed_at", since)
          .execute()
          .data
    )
    if not logs:
        return {"by_nutrient": {}, "by_patient": []}

    # 끼니 단위 로그를 (환자, 일차) 기준 일일 합계로 재집계
    daily_totals: dict[tuple, dict] = {}
    for row in logs:
        key = (row["patient_id"], row["day_number"])
        d = daily_totals.setdefault(
            key, {"energy": 0.0, "protein": 0.0, "sodium": 0.0, "carb": 0.0}
        )
        d["energy"]  += row.get("energy_kcal") or 0
        d["protein"] += row.get("protein_g") or 0
        d["sodium"]  += row.get("sodium_mg") or 0
        d["carb"]    += row.get("carb_g") or 0

    by_patient_days: dict[str, list[dict]] = {}
    for (pid, _day), totals in daily_totals.items():
        by_patient_days.setdefault(pid, []).append(totals)

    by_patient = []
    all_energy, all_protein, all_carb, all_sodium = [], [], [], []

    for pid, day_list in by_patient_days.items():
        n = len(day_list)
        avg_energy  = sum(d["energy"]  for d in day_list) / n
        avg_protein = sum(d["protein"] for d in day_list) / n
        avg_carb    = sum(d["carb"]    for d in day_list) / n
        avg_sodium  = sum(d["sodium"]  for d in day_list) / n

        energy_pct = (
            round(avg_energy / DAILY_TARGETS["energy"] * 100, 1)
            if DAILY_TARGETS["energy"] else 0.0
        )
        is_deficit = avg_energy < DAILY_TARGETS["energy"] * DEFICIT_RATIO

        by_patient.append({
            "patient_id": pid,
            "patient_name": id_to_name.get(pid, "-"),
            "avg_energy_kcal": round(avg_energy, 1),
            "avg_protein_g":   round(avg_protein, 1),
            "avg_carb_g":      round(avg_carb, 1),
            "avg_sodium_mg":   round(avg_sodium, 1),
            "energy_pct_of_target": energy_pct,
            "is_deficit": is_deficit,
        })
        all_energy.append(avg_energy)
        all_protein.append(avg_protein)
        all_carb.append(avg_carb)
        all_sodium.append(avg_sodium)

    by_nutrient = {
        "energy_kcal": _summary(all_energy,  DAILY_TARGETS["energy"]),
        "protein_g":   _summary(all_protein, DAILY_TARGETS["protein"]),
        "carb_g":      _summary(all_carb,    DAILY_TARGETS["carb"]),
        "sodium_mg":   _summary(all_sodium,  SODIUM_UPPER_LIMIT),
    }

    return {"by_nutrient": by_nutrient, "by_patient": by_patient}