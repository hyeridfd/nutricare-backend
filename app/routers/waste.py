"""
routers/waste.py — 잔반 기록 입력/조회
==========================================
waste_monitoring_agent.py / preference_update_agent.py로 연결되는 입구.
실제 운영에서는 요양보호사가 태블릿으로 슬롯별 섭취율을 입력하는 화면과 연동.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from app.services.db_clients import get_supabase

router = APIRouter()


class WasteLogCreate(BaseModel):
    patient_id: str
    run_id: Optional[str] = None
    day_number: int
    meal_type: str  # "아침" | "점심" | "저녁"

    rice_waste_rate: float = Field(ge=0, le=1)
    soup_waste_rate: float = Field(ge=0, le=1)
    main_dish_waste_rate: float = Field(ge=0, le=1)
    side_dish_1_waste_rate: float = Field(ge=0, le=1)
    side_dish_2_waste_rate: float = Field(ge=0, le=1)
    kimchi_waste_rate: float = Field(ge=0, le=1)

    recorded_by: Optional[str] = None


@router.post("")
def create_waste_log(payload: WasteLogCreate):
    sb = get_supabase()
    row = payload.model_dump()
    result = sb.table("waste_logs").insert(row).execute()
    return result.data[0]


@router.get("")
def list_waste_logs(patient_id: str, limit: int = 30):
    sb = get_supabase()
    result = sb.table("waste_logs").select("*") \
               .eq("patient_id", patient_id) \
               .order("recorded_at", desc=True).limit(limit).execute()
    return result.data


@router.post("/run-preference-update")
def trigger_preference_update(facility_id: str):
    """
    누적된 waste_logs를 바탕으로 PreferenceUpdateAgent + WeightAdaptAgent를
    실행해 preference_scores / pool_preference_scores를 갱신.
    배치 작업(예: 매일 새벽 cron)으로 호출하는 걸 전제로 한 엔드포인트.
    """
    from app.services.preference_runner import run_preference_update

    try:
        updated = run_preference_update(facility_id)
        return {"status": "ok", "updated_count": updated}
    except Exception as e:
        raise HTTPException(500, f"선호도 갱신 실패: {e}")
