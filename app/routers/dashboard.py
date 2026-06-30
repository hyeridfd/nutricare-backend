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
