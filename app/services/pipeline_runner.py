"""
services/pipeline_runner.py — LangGraph 파이프라인 ↔ Supabase 연결 (v2)
============================================================================
[중요 — v1과의 차이]
agents/graph.py의 실제 구조를 확인한 결과, hitl_node가 LangGraph의 진짜
interrupt()를 사용하고 MemorySaver 체크포인터로 그 시점 state 전체를
보존하는 구조였음. v1에서는 이걸 모르고 candidate→optimizer→validator를
수동 while 루프로 흉내내고 personalize/serving을 직접 호출했는데, 이러면
orchestrator_agent의 분기 로직(waste_monitoring, preference_update 등)이
전혀 반영되지 않고 report_agent(엑셀 산출)도 건너뛰게 됨.

v2는 graph.py의 build_graph()로 만들어진 app(컴파일된 StateGraph)을
그대로 가져와 app.stream()/Command(resume=...)로 실행함. 즉 이 파일은
파이프라인 로직을 다시 구현하지 않고, "FastAPI 요청 → app.stream() 호출
→ 이벤트를 Supabase에 저장"하는 어댑터 역할만 함.

thread_id는 Supabase의 meal_plan_runs.id(run_id)와 동일하게 사용해
LangGraph 체크포인터의 스레드와 Supabase 실행 기록을 1:1로 묶음.
"""

import sys
import traceback
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from app.services.db_clients import get_supabase
from app.services.patient_logic import build_patient_profile, Sex, KidneyType

import registry  # agents/registry.py


def _load_patients_for_facility(facility_id: str, budget_per_meal: float):
    """Supabase patients 테이블 → PatientProfile 리스트. 각 profile에
    Supabase id를 매달아 두어 나중에 결과를 이름 대신 id로 정확히 매핑."""
    sb = get_supabase()
    rows = sb.table("patients").select("*") \
             .eq("facility_id", facility_id).eq("active", True).execute().data

    patients = []
    for row in rows:
        profile = build_patient_profile(
            name=row["name"],
            sex=Sex.MALE if row["sex"] == "male" else Sex.FEMALE,
            age=row["age"],
            height_cm=row["height_cm"],
            weight_kg=row["weight_kg"],
            waist_cm=row.get("waist_cm"),
            diseases=row["diseases"],
            kidney_type=KidneyType(row["kidney_type"]) if row.get("kidney_type") else None,
            meal_texture_rice=row.get("meal_texture_rice", "밥"),
            meal_texture_side=row.get("meal_texture_side", "일반찬"),
            budget_per_meal=budget_per_meal,
        )
        profile._supabase_id = row["id"]
        patients.append(profile)

    return patients


def _build_initial_state(run_id: str, facility_id: str, diseases: list[str],
                          budget_per_meal: float):
    """graph.py __main__ 블록의 initial_state 구성을 그대로 따르되,
    waste_log는 하드코딩된 샘플 대신 Supabase에서 조회(없으면 None)."""
    import facility_optimization as fac
    from preference_update_agent import load_weights

    patients = _load_patients_for_facility(facility_id, budget_per_meal)
    if not patients:
        raise ValueError("활성 환자가 없습니다. 먼저 환자를 등록하세요.")

    fc = fac.derive_facility_constraint(patients)
    constraint_adapter = fac.FacilityConstraintAdapter(fc)

    patients_key   = f"patients_{run_id}"
    constraint_key = f"constraint_{run_id}"
    registry.put(patients_key, patients)
    registry.put(constraint_key, constraint_adapter)

    # 잔반 기록이 누적되어 있으면 waste_log 형태로 변환해 waste_monitoring_subgraph가
    # 바로 활용하게 함. 없으면 None으로 둬 orchestrator_agent의 report 단계에서
    # waste_monitoring을 건너뛰게 함
    # (orchestrator_agent.py: `if state.get("waste_log"): ... else: end`).
    waste_log = _load_recent_waste_log(facility_id, patients)

    prev_weights = load_weights()

    initial_state = {
        "diseases":          diseases,
        "patients_key":      patients_key,
        "constraint_key":    constraint_key,
        "budget_per_meal":   budget_per_meal,
        "pool":              None,
        "nsga_result_key":   None,
        "violation_count":   0,
        "df_menu_records":   None,
        "df_menu_columns":   None,
        "recommend_map":     None,
        "violation_rate":    0.0,
        "validator_msg":     "",
        "hitl_action":       None,
        "hitl_changes":      None,
        "serving_map":       None,
        "waste_log":         waste_log,
        "nutrition_history": None,
        "alert_queue":       None,
        "report_paths":      None,
        "orchestrator_phase": "optimize",
        "next_agent":         None,
        "preference_weights": prev_weights,
        "personal_menus":     None,
        "messages":          [],
    }
    return initial_state, patients


def _load_recent_waste_log(facility_id: str, patients: list) -> list | None:
    """Supabase waste_logs를 graph.py가 기대하는 waste_log 형태로 변환.
    누적 데이터가 없으면 None을 반환해 waste_monitoring 단계를 건너뛰게 함."""
    sb = get_supabase()
    patient_ids = [getattr(p, "_supabase_id", None) for p in patients]
    patient_ids = [pid for pid in patient_ids if pid]
    if not patient_ids:
        return None

    name_by_id = {getattr(p, "_supabase_id", None): p.name for p in patients}

    logs = sb.table("waste_logs").select("*") \
             .in_("patient_id", patient_ids) \
             .order("recorded_at", desc=True).limit(500).execute().data
    if not logs:
        return None

    waste_log = []
    for log in logs:
        waste_log.append({
            "name": name_by_id.get(log["patient_id"], ""),
            "일차": f"{log['day_number']}일",
            "끼니": log["meal_type"],
            "밥":   log.get("rice_waste_rate", 0.0),
            "국":   log.get("soup_waste_rate", 0.0),
            "주찬": log.get("main_dish_waste_rate", 0.0),
            "부찬1": log.get("side_dish_1_waste_rate", 0.0),
            "부찬2": log.get("side_dish_2_waste_rate", 0.0),
            "김치": log.get("kimchi_waste_rate", 0.0),
        })
    return waste_log


def run_pipeline_for_run(
    run_id: str,
    facility_id: str,
    diseases: list[str],
    budget_per_meal: float,
    auto_approve: bool,
):
    """
    BackgroundTasks에서 호출되는 메인 함수.
    graph.py의 app.stream()을 그대로 사용해 candidate→...→hitl(interrupt)까지
    실행하고, interrupt에서 멈추면 meal_plan_runs.status='pending_review'로
    기록. auto_approve=True면 곧바로 resume까지 이어서 실행.
    """
    sb = get_supabase()

    try:
        from graph import app as graph_app  # noqa: F401 (존재 확인용 import)

        initial_state, patients = _build_initial_state(
            run_id, facility_id, diseases, budget_per_meal
        )
        # 환자 리스트를 나중에(approve 시점에) 다시 찾을 수 있도록 registry에 보관
        registry.put(f"patients_for_run_{run_id}", patients)

        config = {"configurable": {"thread_id": run_id}}

        interrupted, last_state = _drain_stream(graph_app.stream(initial_state, config=config))

        if interrupted:
            sb.table("meal_plan_runs").update({
                "status": "pending_review",
                "f1_violation": _safe_float(last_state.get("violation_rate")),
                "reoptimize_count": last_state.get("violation_count", 0),
            }).eq("id", run_id).execute()

            if last_state.get("df_menu_records"):
                _save_meal_plan_slots(run_id, last_state)

            if auto_approve:
                _resume(run_id, action="approve")
            return

        # interrupt 없이 끝까지 갔다면(이론상 hitl 노드를 항상 거치므로
        # 거의 발생하지 않지만 방어적으로 처리) 그대로 완료 처리
        _finalize_run(run_id, last_state, patients)

    except Exception as e:
        sb.table("meal_plan_runs").update({"status": "rejected"}).eq("id", run_id).execute()
        print(f"[pipeline_runner] run_id={run_id} 실패: {e}")
        traceback.print_exc()


def resume_after_approval(run_id: str, action: str = "approve", changes: dict | None = None):
    """
    HITL 승인/반려 시 호출. graph.py의 Command(resume=...)로 정확히
    interrupt 지점부터 재개. auto_approve 흐름에서도 내부적으로 이 함수를 씀.
    """
    _resume(run_id, action=action, changes=changes)


def _resume(run_id: str, action: str, changes: dict | None = None):
    from graph import app as graph_app
    from langgraph.types import Command

    sb = get_supabase()
    config = {"configurable": {"thread_id": run_id}}

    resume_payload = {"action": action}
    if changes:
        resume_payload["changes"] = changes

    try:
        interrupted, last_state = _drain_stream(
            graph_app.stream(Command(resume=resume_payload), config=config)
        )

        if interrupted:
            # reoptimize 후 다시 hitl에 걸린 경우 — pending_review 유지
            sb.table("meal_plan_runs").update({
                "status": "pending_review",
                "f1_violation": _safe_float(last_state.get("violation_rate")),
                "reoptimize_count": last_state.get("violation_count", 0),
            }).eq("id", run_id).execute()
            if last_state.get("df_menu_records"):
                _save_meal_plan_slots(run_id, last_state)
            return

        patients_key = f"patients_for_run_{run_id}"
        patients = registry.get(patients_key) if registry.has(patients_key) else []
        _finalize_run(run_id, last_state, patients)

    except Exception as e:
        sb.table("meal_plan_runs").update({"status": "rejected"}).eq("id", run_id).execute()
        print(f"[pipeline_runner] resume 실패 run_id={run_id}: {e}")
        traceback.print_exc()


def _drain_stream(stream_iter):
    """
    app.stream()의 모든 이벤트를 소비하고, 마지막으로 관측된 state 조각들을
    누적해 반환. __interrupt__ 이벤트를 만나면 (True, 누적 state)를 반환.
    완주하면 (False, 누적 state)를 반환.

    [주의] LangGraph node 함수는 자신이 갱신한 키만 반환하므로(state 전체가
    아님), 여기서 누적(dict.update)해서 "현재까지의 전체 state 스냅샷"을
    재구성함. 이는 graph.py __main__ 블록의 출력 로직과 동일한 패턴.
    """
    accumulated: dict = {}
    for event in stream_iter:
        if "__interrupt__" in event:
            return True, accumulated

        node = list(event.keys())[0]
        node_output = event[node]
        if isinstance(node_output, dict):
            accumulated.update(node_output)

    return False, accumulated


def _finalize_run(run_id: str, state: dict, patients: list):
    """
    파이프라인이 끝까지(report → ... → END) 실행된 뒤 호출.
    이 시점 state에는 personalize_reasons, serving_map, report_paths 등이
    모두 채워져 있어야 정상(orchestrator_agent의 분기를 다 거쳤다는 전제).
    """
    sb = get_supabase()

    if state.get("df_menu_records"):
        _save_meal_plan_slots(run_id, state)
    if state.get("personalize_reasons"):
        _save_personalized_swaps(run_id, state, patients)
    if state.get("serving_map"):
        _save_servings(run_id, state, patients)
    if state.get("waste_log"):
        _save_waste_alerts(run_id, state, patients)

    sb.table("meal_plan_runs").update({
        "status":        "approved",
        "reviewed_by":   "auto",
        "review_action": "approve",
        "reviewed_at":   "now()",
        "f1_violation":  _safe_float(state.get("violation_rate")),
        "reoptimize_count": state.get("violation_count", 0),
    }).eq("id", run_id).execute()


def _safe_float(val):
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# ════════════════════════════════════════════════════════════
# Supabase 저장 헬퍼
# ════════════════════════════════════════════════════════════
def _save_meal_plan_slots(run_id: str, state: dict):
    sb = get_supabase()
    records = state.get("df_menu_records", [])
    rows = []
    for r in records:
        rows.append({
            "run_id":      run_id,
            "day_number":  int(r["일차"].replace("일", "")),
            "meal_type":   r["끼니"],
            "rice":        r["밥"],
            "soup":        r["국"],
            "main_dish":   r["주찬"],
            "side_dish_1": r["부찬1"],
            "side_dish_2": r["부찬2"],
            "kimchi":      r["김치"],
            "energy_kcal": r.get("열량(kcal)"),
            "sodium_mg":   r.get("나트륨(mg)"),
            "protein_g":   r.get("단백질(g)"),
            "cost_won":    r.get("비용(원)"),
            "recommended_menu_summary": r.get("권장재료포함메뉴", "-"),
            "recommended_menu_count":   r.get("권장재료포함수", 0),
        })
    if rows:
        sb.table("meal_plan_slots").upsert(
            rows, on_conflict="run_id,day_number,meal_type"
        ).execute()


def _patient_id_lookup(patients: list) -> dict:
    return {p.name: getattr(p, "_supabase_id", None) for p in patients}


def _save_personalized_swaps(run_id: str, state: dict, patients: list):
    sb = get_supabase()
    id_map = _patient_id_lookup(patients)
    reasons = state.get("personalize_reasons", {})

    rows = []
    for key, changes in reasons.items():
        name, day, meal = key.split("||")
        patient_id = id_map.get(name)
        if not patient_id:
            continue
        for change in changes:
            rows.append({
                "run_id":        run_id,
                "patient_id":    patient_id,
                "day_number":    int(day.replace("일", "")),
                "meal_type":     meal,
                "slot":          change["slot"],
                "original_menu": change["from"],
                "replaced_menu": change["to"],
                "reason_type":   change["reason"],
                "reason_detail": change["detail"],
                "serving_ratio": change.get("ratio"),
            })
    if rows:
        sb.table("personalized_swaps").insert(rows).execute()


def _save_servings(run_id: str, state: dict, patients: list):
    sb = get_supabase()
    id_map = _patient_id_lookup(patients)
    serving_map = state.get("serving_map", {})

    rows = []
    for key, srv in serving_map.items():
        name, day, meal = key.split("||")
        patient_id = id_map.get(name)
        if not patient_id:
            continue
        rows.append({
            "run_id":      run_id,
            "patient_id":  patient_id,
            "day_number":  int(day.replace("일", "")),
            "meal_type":   meal,
            "ratio":       srv.get("ratio"),
            "rice_g":      srv.get("밥", srv.get("죽", 0)),
            "soup_ml":     srv.get("국"),
            "main_dish_g": srv.get("주찬"),
            "side_dish_1_g": srv.get("부찬1"),
            "side_dish_2_g": srv.get("부찬2"),
            "kimchi_g":      srv.get("김치"),
            "expected_energy_kcal": srv.get("예상열량"),
            "expected_protein_g":   srv.get("예상단백질"),
            "expected_sodium_mg":   srv.get("예상나트륨"),
            "expected_carb_g":      srv.get("예상탄수화물"),
            "energy_ok":  srv.get("열량OK")  == "✅",
            "protein_ok": srv.get("단백질OK") == "✅",
            "sodium_ok":  srv.get("나트륨OK") == "✅",
        })
    if rows:
        sb.table("servings").upsert(
            rows, on_conflict="run_id,patient_id,day_number,meal_type"
        ).execute()


def _save_waste_alerts(run_id: str, state: dict, patients: list):
    """
    waste_monitoring_subgraph(NutritionMonitorAgent/AlertAgent/InterventionAgent)가
    state['alert_queue']에 쌓아둔 알림+처방을 nutrition_alerts/interventions에 저장.
    alert_queue 구조는 waste_monitoring_agent.py의 alert_agent 출력을 따름.
    """
    sb = get_supabase()
    id_map = _patient_id_lookup(patients)
    alert_queue = state.get("alert_queue") or []

    for alert in alert_queue:
        name = alert.get("name")
        patient_id = id_map.get(name)
        if not patient_id:
            continue

        row = {
            "patient_id":       patient_id,
            "nutrient":         alert.get("nutrient"),
            "consecutive_days": alert.get("consecutive_days", 0),
            "avg_intake":       alert.get("avg_intake", 0),
            "standard_value":   alert.get("standard_value", 0),
            "deficit_rate":     alert.get("deficit_rate", 0),
            "status":           "sent",
            "sent_at":          "now()",
        }
        result = sb.table("nutrition_alerts").insert(row).execute()
        alert_id = result.data[0]["id"]

        prescription = alert.get("prescription")
        if prescription:
            sb.table("interventions").insert({
                "alert_id": alert_id,
                "prescription_text": prescription,
            }).execute()
