"""
routers/dashboard.py — 어르신 현황 / 식사·잔반 현황 페이지용 API
=====================================================================
nutricare_dashboard.html의 PAGE 1(어르신 현황), PAGE 2(식사·잔반 현황)에
필요한 집계 데이터를 제공합니다.
"""

from fastapi import APIRouter
from collections import defaultdict

from app.services.db_clients import get_supabase

router = APIRouter()


@router.get("/summary")
def get_summary(facility_id: str):
    """PAGE 1 상단 KPI 카드: 총 입소 인원, 식사 모니터링 필요, 영양 보강 알림, 평균 식사율."""
    sb = get_supabase()

    patients = sb.table("patients").select("id, name, disease_type_label") \
                 .eq("facility_id", facility_id).eq("active", True).execute().data
    total = len(patients)

    open_alerts = sb.table("nutrition_alerts").select("patient_id") \
                     .eq("status", "open").execute().data
    alert_patient_ids = {a["patient_id"] for a in open_alerts}

    # 질환유형 분포 (disease_type_label 기준)
    type_dist = defaultdict(int)
    for p in patients:
        type_dist[p.get("disease_type_label") or "일반형"] += 1

    return {
        "total_patients": total,
        "nutrition_alert_count": len(alert_patient_ids),
        "disease_type_distribution": dict(type_dist),
    }


@router.get("/residents")
def list_residents(facility_id: str):
    """PAGE 1 하단 테이블: 어르신 목록 + 최근 식사율/영양상태."""
    sb = get_supabase()

    patients = sb.table("patients").select("*") \
                 .eq("facility_id", facility_id).eq("active", True).execute().data

    open_alerts = sb.table("nutrition_alerts").select("patient_id, nutrient") \
                     .eq("status", "open").execute().data
    alerts_by_patient = defaultdict(list)
    for a in open_alerts:
        alerts_by_patient[a["patient_id"]].append(a["nutrient"])

    rows = []
    for p in patients:
        rows.append({
            "id": p["id"],
            "name": p["name"],
            "age": p["age"],
            "disease_type_label": p.get("disease_type_label") or "일반형",
            "meal_texture": f"{p['meal_texture_rice']}/{p['meal_texture_side']}",
            "alert_nutrients": alerts_by_patient.get(p["id"], []),
            "status": "보강필요" if p["id"] in alerts_by_patient else "정상",
        })
    return rows


@router.get("/meal-waste")
def get_meal_waste_summary(facility_id: str, days: int = 7):
    """PAGE 2: 식이유형별/끼니별 잔반율 집계."""
    sb = get_supabase()

    patients = sb.table("patients").select("id, disease_type_label") \
                 .eq("facility_id", facility_id).eq("active", True).execute().data
    patient_type = {p["id"]: p.get("disease_type_label") or "일반형" for p in patients}
    patient_ids = list(patient_type.keys())

    if not patient_ids:
        return {"by_disease_type": {}, "by_meal": {}}

    logs = sb.table("waste_logs").select("*") \
             .in_("patient_id", patient_ids) \
             .order("recorded_at", desc=True).limit(1000).execute().data

    waste_fields = [
        "rice_waste_rate", "soup_waste_rate", "main_dish_waste_rate",
        "side_dish_1_waste_rate", "side_dish_2_waste_rate", "kimchi_waste_rate",
    ]

    by_type, by_type_count = defaultdict(float), defaultdict(int)
    by_meal, by_meal_count = defaultdict(float), defaultdict(int)

    for log in logs:
        vals = [log[f] for f in waste_fields if log.get(f) is not None]
        if not vals:
            continue
        avg_waste = sum(vals) / len(vals)

        dtype = patient_type.get(log["patient_id"], "일반형")
        by_type[dtype] += avg_waste
        by_type_count[dtype] += 1

        by_meal[log["meal_type"]] += avg_waste
        by_meal_count[log["meal_type"]] += 1

    return {
        "by_disease_type": {
            k: round(v / by_type_count[k] * 100, 1) for k, v in by_type.items()
        },
        "by_meal": {
            k: round(v / by_meal_count[k] * 100, 1) for k, v in by_meal.items()
        },
    }


@router.get("/nutrition-alerts")
def list_nutrition_alerts(facility_id: str, status: str = "open"):
    """영양 보강 알림 목록 (PAGE 1 알림 카드)."""
    sb = get_supabase()

    patients = sb.table("patients").select("id, name") \
                 .eq("facility_id", facility_id).execute().data
    patient_name = {p["id"]: p["name"] for p in patients}

    alerts = sb.table("nutrition_alerts").select("*") \
               .eq("status", status).order("detected_at", desc=True).execute().data

    for a in alerts:
        a["patient_name"] = patient_name.get(a["patient_id"], "알 수 없음")
    return alerts


# ── KDRIs 일일 기준값 (agents/waste_monitoring_agent.py의 DAILY_TARGETS와 동일) ──
KDRI_DAILY_TARGETS = {
    "energy":  1500.0,   # kcal/일
    "protein":   60.0,   # g/일
    "carb":     300.0,   # g/일
    "sodium":  2000.0,   # mg/일 (상한 — 적게 먹는 게 더 안전하므로 별도 처리)
}
DEFICIT_RATIO = 0.8  # 권장량 80% 미만이면 "부족"


@router.get("/nutrition-intake")
def get_nutrition_intake(facility_id: str, days: int = 7):
    """
    PAGE 3(영양소 섭취 현황): waste_logs(잔반율) × servings(배식량 기준 예상
    영양소)를 곱해 "실제 섭취 영양소"를 계산하고, KDRIs 일일 기준 대비
    주간 평균 섭취율(%)을 반환.

    계산식 (agents/waste_monitoring_agent.py의 plate_waste_input_agent와 동일 원리):
        실제 섭취량 = servings.expected_* × (1 - 잔반율)
        (servings.expected_*는 이미 "배식량 기준 영양값"이므로 100g당 영양값을
         다시 곱할 필요 없이, 섭취율만 곱하면 됨)
    """
    sb = get_supabase()

    patients = sb.table("patients").select("id, name") \
                 .eq("facility_id", facility_id).eq("active", True).execute().data
    if not patients:
        return {"by_nutrient": {}, "by_patient": []}
    patient_ids = [p["id"] for p in patients]
    name_by_id = {p["id"]: p["name"] for p in patients}

    waste_logs = sb.table("waste_logs").select("*") \
                   .in_("patient_id", patient_ids) \
                   .order("recorded_at", desc=True).limit(500).execute().data
    if not waste_logs:
        return {"by_nutrient": {}, "by_patient": []}

    run_ids = {log["run_id"] for log in waste_logs if log.get("run_id")}
    servings_lookup = {}
    if run_ids:
        servings = sb.table("servings").select("*") \
                     .in_("run_id", list(run_ids)).execute().data
        for s in servings:
            key = (s["run_id"], s["patient_id"], s["day_number"], s["meal_type"])
            servings_lookup[key] = s

    WASTE_FIELD_MAP = {
        "밥": "rice_waste_rate", "국": "soup_waste_rate", "주찬": "main_dish_waste_rate",
        "부찬1": "side_dish_1_waste_rate", "부찬2": "side_dish_2_waste_rate",
        "김치": "kimchi_waste_rate",
    }

    # 환자별 끼니별 실제 섭취 영양소 합산 → 환자별 일일 평균
    patient_daily: dict = {}  # {patient_id: {day_number: {energy, protein, carb, sodium}}}

    for log in waste_logs:
        srv = servings_lookup.get(
            (log.get("run_id"), log["patient_id"], log["day_number"], log["meal_type"])
        )
        if not srv:
            continue

        # 6개 슬롯 잔반율 평균으로 그 끼니 전체의 섭취율 근사
        # (슬롯별 정확한 영양소 분해는 agents 쪽 pool 데이터가 필요해 더 복잡하므로,
        #  대시보드 집계 목적에서는 끼니 평균 섭취율로 단순화)
        waste_rates = [log.get(f, 0.0) or 0.0 for f in WASTE_FIELD_MAP.values()]
        avg_intake_rate = 1.0 - (sum(waste_rates) / len(waste_rates))

        pid = log["patient_id"]
        day = log["day_number"]
        patient_daily.setdefault(pid, {}).setdefault(
            day, {"energy": 0.0, "protein": 0.0, "carb": 0.0, "sodium": 0.0}
        )
        bucket = patient_daily[pid][day]
        bucket["energy"]  += (srv.get("expected_energy_kcal")   or 0) * avg_intake_rate
        bucket["protein"] += (srv.get("expected_protein_g")     or 0) * avg_intake_rate
        bucket["carb"]    += (srv.get("expected_carb_g")        or 0) * avg_intake_rate
        bucket["sodium"]  += (srv.get("expected_sodium_mg")     or 0) * avg_intake_rate

    # 환자별 일일 평균 → 시설 전체 평균(%) 집계
    nutrient_sums = {"energy": 0.0, "protein": 0.0, "carb": 0.0, "sodium": 0.0}
    nutrient_counts = {"energy": 0, "protein": 0, "carb": 0, "sodium": 0}
    by_patient = []

    for pid, daily in patient_daily.items():
        day_count = len(daily)
        if day_count == 0:
            continue
        avg = {
            nut: sum(d[nut] for d in daily.values()) / day_count
            for nut in ["energy", "protein", "carb", "sodium"]
        }
        for nut in nutrient_sums:
            nutrient_sums[nut] += avg[nut]
            nutrient_counts[nut] += 1

        by_patient.append({
            "patient_id": pid,
            "patient_name": name_by_id.get(pid, "알 수 없음"),
            "avg_energy_kcal": round(avg["energy"], 1),
            "avg_protein_g": round(avg["protein"], 1),
            "avg_carb_g": round(avg["carb"], 1),
            "avg_sodium_mg": round(avg["sodium"], 1),
            "energy_pct_of_target": round(avg["energy"] / KDRI_DAILY_TARGETS["energy"] * 100, 1),
            "is_deficit": avg["energy"] < KDRI_DAILY_TARGETS["energy"] * DEFICIT_RATIO,
        })

    by_nutrient = {}
    for nut, target in KDRI_DAILY_TARGETS.items():
        count = nutrient_counts.get(nut, 0)
        if count == 0:
            continue
        facility_avg = nutrient_sums[nut] / count
        by_nutrient[nut] = {
            "facility_avg": round(facility_avg, 1),
            "target": target,
            "pct_of_target": round(facility_avg / target * 100, 1),
        }

    return {
        "by_nutrient": by_nutrient,
        "by_patient": sorted(by_patient, key=lambda x: x["energy_pct_of_target"]),
    }