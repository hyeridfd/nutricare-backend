"""
routers/preferences.py — 선호도 확인 페이지용 API
======================================================
preference_scores(개인별), pool_preference_scores(시설 전체)를 조회합니다.
이 점수들은 PreferenceUpdateAgent/WeightAdaptAgent가 매 파이프라인 실행마다
EMA로 갱신해 Supabase에 저장한 결과입니다(잔반 기록이 누적될수록 정확해짐).
"""

from fastapi import APIRouter
from collections import defaultdict

from app.services.db_clients import get_supabase

router = APIRouter()

DISLIKE_THRESHOLD = 0.5   # 이 점수 미만이면 "기피" 라벨
LIKE_THRESHOLD    = 0.8   # 이 점수 이상이면 "선호" 라벨


def _label_for(score: float) -> str:
    if score < DISLIKE_THRESHOLD:
        return "기피"
    if score >= LIKE_THRESHOLD:
        return "선호"
    return "보통"


@router.get("/facility")
def get_facility_preferences(facility_id: str, limit: int = 50):
    """
    시설 전체 메뉴 선호도(pool_preference_scores). 점수 낮은 순(기피 메뉴 위주)으로 정렬.
    MENTOR 식단 설계 시 WeightAdaptAgent가 다음 최적화에 반영하는 그 점수와 동일.
    """
    sb = get_supabase()
    rows = sb.table("pool_preference_scores").select("menu_name, score, updated_at") \
             .eq("facility_id", facility_id) \
             .order("score").limit(limit).execute().data

    return {
        "items": rows,
        "dislike_count": sum(1 for r in rows if r["score"] < DISLIKE_THRESHOLD),
        "like_count": sum(1 for r in rows if r["score"] >= LIKE_THRESHOLD),
    }


@router.get("/patients")
def get_patient_preferences(facility_id: str, patient_id: str | None = None):
    """
    개인별 메뉴 선호도(preference_scores). patient_id가 없으면 시설 전체
    환자의 선호도를 환자별로 묶어서 반환(요약 카드용), 있으면 그 환자만
    상세 목록(메뉴별 점수 전체)으로 반환.
    """
    sb = get_supabase()

    patients = sb.table("patients").select("id, name") \
                 .eq("facility_id", facility_id).eq("active", True).execute().data
    if not patients:
        return {"patients": []}
    name_by_id = {p["id"]: p["name"] for p in patients}
    patient_ids = [p["id"] for p in patients]

    if patient_id:
        if patient_id not in patient_ids:
            return {"patient_name": None, "items": []}
        rows = sb.table("preference_scores").select("menu_name, score, updated_at") \
                 .eq("patient_id", patient_id).order("score").execute().data
        return {
            "patient_name": name_by_id.get(patient_id),
            "items": rows,
            "dislike_count": sum(1 for r in rows if r["score"] < DISLIKE_THRESHOLD),
            "like_count": sum(1 for r in rows if r["score"] >= LIKE_THRESHOLD),
        }

    # 전체 환자 요약: 환자별 기피/선호 메뉴 수만 집계 (목록 화면용)
    scores = sb.table("preference_scores").select("patient_id, score") \
               .in_("patient_id", patient_ids).execute().data

    summary = defaultdict(lambda: {"dislike_count": 0, "like_count": 0, "total": 0})
    for row in scores:
        pid = row["patient_id"]
        summary[pid]["total"] += 1
        if row["score"] < DISLIKE_THRESHOLD:
            summary[pid]["dislike_count"] += 1
        elif row["score"] >= LIKE_THRESHOLD:
            summary[pid]["like_count"] += 1

    result = []
    for pid, name in name_by_id.items():
        s = summary.get(pid, {"dislike_count": 0, "like_count": 0, "total": 0})
        result.append({
            "patient_id": pid,
            "patient_name": name,
            "total_menus": s["total"],
            "dislike_count": s["dislike_count"],
            "like_count": s["like_count"],
        })

    return {"patients": sorted(result, key=lambda x: -x["dislike_count"])}