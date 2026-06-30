"""
services/preference_runner.py — 잔반 기반 선호도 갱신
=========================================================
Supabase waste_logs를 preference_update_agent.py가 기대하는 nutrition_history
형태로 변환해 기존 EMA 갱신 로직을 그대로 호출합니다.
"""

import sys
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from app.services.db_clients import get_supabase

SLOTS = ["밥", "국", "주찬", "부찬1", "부찬2", "김치"]
WASTE_FIELD_MAP = {
    "밥": "rice_waste_rate", "국": "soup_waste_rate", "주찬": "main_dish_waste_rate",
    "부찬1": "side_dish_1_waste_rate", "부찬2": "side_dish_2_waste_rate",
    "김치": "kimchi_waste_rate",
}


def run_preference_update(facility_id: str) -> int:
    from preference_update_agent import preference_update_agent

    sb = get_supabase()

    patients = sb.table("patients").select("id, name") \
                 .eq("facility_id", facility_id).eq("active", True).execute().data
    if not patients:
        return 0
    patient_ids = [p["id"] for p in patients]
    name_by_id = {p["id"]: p["name"] for p in patients}

    logs = sb.table("waste_logs").select("*") \
             .in_("patient_id", patient_ids) \
             .order("recorded_at", desc=True).limit(21 * len(patients)).execute().data

    # run_id별 메뉴 슬롯 정보가 필요하므로 관련 meal_plan_slots를 미리 조회
    run_ids = {log["run_id"] for log in logs if log.get("run_id")}
    slot_lookup = {}
    if run_ids:
        slots = sb.table("meal_plan_slots").select("*") \
                  .in_("run_id", list(run_ids)).execute().data
        for s in slots:
            key = (s["run_id"], s["day_number"], s["meal_type"])
            slot_lookup[key] = s

    # preference_update_agent가 기대하는 형태로 변환:
    # {name: [{"waste": {...}, "menu": {...}}, ...]}
    nutrition_history = {}
    for log in logs:
        name = name_by_id.get(log["patient_id"])
        if not name:
            continue
        slot_info = slot_lookup.get((log.get("run_id"), log["day_number"], log["meal_type"]))
        if not slot_info:
            continue

        waste = {slot: log.get(field, 0.0) for slot, field in WASTE_FIELD_MAP.items()}
        menu = {
            "밥": slot_info["rice"], "국": slot_info["soup"], "주찬": slot_info["main_dish"],
            "부찬1": slot_info["side_dish_1"], "부찬2": slot_info["side_dish_2"],
            "김치": slot_info["kimchi"],
        }
        nutrition_history.setdefault(name, []).append({"waste": waste, "menu": menu})

    if not nutrition_history:
        return 0

    state = {"nutrition_history": nutrition_history, "preference_weights": {}}
    result = preference_update_agent(state)
    weights = result["preference_weights"]

    # Supabase preference_scores 테이블에 upsert
    rows = []
    for name, prefs in weights.items():
        patient_id = next((p["id"] for p in patients if p["name"] == name), None)
        if not patient_id:
            continue
        for menu_name, score in prefs.items():
            rows.append({
                "patient_id": patient_id,
                "menu_name": menu_name,
                "score": score,
                "updated_at": "now()",
            })

    if rows:
        sb.table("preference_scores").upsert(
            rows, on_conflict="patient_id,menu_name"
        ).execute()

    return len(rows)
