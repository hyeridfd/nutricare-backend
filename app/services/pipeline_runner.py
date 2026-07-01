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

[수정 — 2026-07-01] _save_waste_alerts가 waste_monitoring_agent.py의
nutrition_monitor_agent가 실제로 만드는 alert 딕셔너리 키("days",
"target", "deficit_pct")가 아니라 존재하지 않는 키("consecutive_days",
"standard_value", "deficit_rate")를 읽고 있어서, nutrition_alerts
테이블에 해당 컬럼들이 항상 0으로 저장되던 버그를 수정. 또한
"sent_at": "now()" 문자열 리터럴 버그(meal_plans.py의 approve_run과
동일 패턴)도 함께 수정.

[수정 — 2026-07-01 #2] personalized_swaps 같은 테이블은 한 run에 8천 건이
넘게 나올 수 있는데, 이걸 한 번의 insert() 호출로 통째로 보내다가
httpcore.RemoteProtocolError: Server disconnected로 실패하는 문제가
있었음(_save_personalized_swaps). 대용량 단일 요청이 원인으로 보여,
_batched_upsert 헬퍼를 추가해 모든 대량 저장(_save_meal_plan_slots,
_save_personalized_swaps, _save_servings, _save_nutrition_intake)을
일정 크기로 나눠 보내고, 일시적 네트워크 오류 시 자동 재시도하도록 변경.
"""

import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from app.services.db_clients import get_supabase
from app.services.patient_logic import build_patient_profile, Sex, KidneyType
from app.services.preference_persistence_patch import set_active_facility as _set_active_facility

import registry  # agents/registry.py


def _now_iso() -> str:
    """Supabase(Postgres) timestamp 컬럼에 안전하게 넣을 UTC ISO 문자열."""
    return datetime.now(timezone.utc).isoformat()


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


def _build_initial_state(run_id: str, facility_id: str, budget_per_meal: float):
    """graph.py __main__ 블록의 initial_state 구성을 그대로 따르되,
    waste_log는 하드코딩된 샘플 대신 Supabase에서 조회(없으면 None).

    [변경] diseases를 프론트엔드에서 사용자가 직접 선택하게 했었는데,
    원래 agents/facility_optimization.py의 설계 의도는 "시설에 등록된
    전체 환자의 질환을 자동으로 모아서" 최적화하는 것이었음
    (get_all_diseases가 그 역할 — 연하장애/치매는 텍스처 처리 대상이라
    제외). 이제 외부에서 diseases를 받지 않고, 여기서 환자 목록 기준으로
    자동 도출해 원래 에이전트 동작과 동일하게 맞춤."""
    import facility_optimization as fac
    from preference_update_agent import load_weights

    patients = _load_patients_for_facility(facility_id, budget_per_meal)
    if not patients:
        raise ValueError("활성 환자가 없습니다. 먼저 환자를 등록하세요.")

    diseases = fac.get_all_diseases(patients)
    if not diseases:
        raise ValueError(
            "등록된 환자들에게서 최적화 대상 질환(고혈압/당뇨병/신장질환)을 "
            "찾지 못했습니다. 환자 등록 시 질환 정보를 확인하세요."
        )

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
            # [추가 — 2026-07-01] nutrition_intake_logs 저장 시 고유키로
            # 쓰기 위해 원본 waste_logs 행 id를 그대로 실어 보냄.
            "waste_log_id": log.get("id"),
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
    budget_per_meal: float,
    auto_approve: bool,
):
    """
    BackgroundTasks에서 호출되는 메인 함수.
    graph.py의 app.stream()을 그대로 사용해 candidate→...→hitl(interrupt)까지
    실행하고, interrupt에서 멈추면 meal_plan_runs.status='pending_review'로
    기록. auto_approve=True면 곧바로 resume까지 이어서 실행.

    [변경] diseases는 더 이상 외부(프론트엔드 선택)에서 받지 않고,
    _build_initial_state 내부에서 시설 등록 환자 전체를 기준으로 자동 도출함
    (agents/facility_optimization.get_all_diseases와 동일한 원본 설계).
    """
    sb = get_supabase()
    _set_active_facility(facility_id)

    try:
        from graph import app as graph_app  # noqa: F401 (존재 확인용 import)

        initial_state, patients = _build_initial_state(
            run_id, facility_id, budget_per_meal
        )
        # 환자 리스트와 facility_id를 나중에(approve 시점에) 다시 찾을 수 있도록 registry에 보관
        registry.put(f"patients_for_run_{run_id}", patients)
        registry.put(f"facility_for_run_{run_id}", facility_id)

        # 자동 도출된 질환을 meal_plan_runs에 기록 (프론트 표시용)
        #
        # [수정] agents/candidate_agent.py는 INTERSECTION_EXCLUDED_DISEASES
        # (신장질환 등)를 내부에서 빼고 교집합을 계산하지만, 그 결과(어떤
        # 질환이 빠졌는지)를 print 로그에만 남기고 state나 반환값에는 남기지
        # 않음. 그래서 diseases_targeted=initial_state["diseases"](합집합
        # 전체, 신장질환 포함)만 저장되고 diseases_excluded는 항상 빈 채로
        # 남아있었음("교집합 제외 질환: 없음"으로 보였던 원인).
        # candidate_agent.py를 수정하지 않고, 여기서 같은 상수를 가져와
        # 동일한 분리 로직을 한 번 더 적용해 정확한 값을 채움.
        from candidate_agent import INTERSECTION_EXCLUDED_DISEASES
        all_diseases = initial_state["diseases"]
        diseases_targeted = [d for d in all_diseases if d not in INTERSECTION_EXCLUDED_DISEASES]
        diseases_excluded = [d for d in all_diseases if d in INTERSECTION_EXCLUDED_DISEASES]
        if not diseases_targeted:
            # candidate_agent.py와 동일한 폴백: 전부 제외 대상이면 첫 번째를 단독 사용
            diseases_targeted = diseases_excluded[:1]
            diseases_excluded = diseases_excluded[1:]

        # 치매는 diseases_targeted(메뉴 풀 필터링 대상)에는 포함되지 않지만
        # (get_all_diseases가 의도적으로 제외), PersonalizeAgent가 끼니 단위
        # boost_nutrients 보강으로 별도 처리하므로 그 인원수를 따로 기록해
        # 프론트에서 "치매가 무시되지 않았음"을 명확히 보여줄 수 있게 함.
        dementia_count = sum(
            1 for p in patients if "치매" in p._resolve_diseases()
        )
        sb.table("meal_plan_runs").update({
            "diseases_targeted": diseases_targeted,
            "diseases_excluded": diseases_excluded,
            "dementia_patient_count": dementia_count,
        }).eq("id", run_id).execute()

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
        _finalize_run(run_id, last_state, patients, facility_id)

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

    facility_key = f"facility_for_run_{run_id}"
    if registry.has(facility_key):
        _set_active_facility(registry.get(facility_key))

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

        facility_key = f"facility_for_run_{run_id}"
        if registry.has(facility_key):
            facility_id = registry.get(facility_key)
        else:
            # registry는 프로세스 메모리에만 있어 서버 재시작 시 비워질 수 있음 —
            # 이 경우 Supabase에서 facility_id를 다시 조회(환자 1명의 facility_id로 역추적)
            facility_id = None
            if patients:
                p_row = sb.table("patients").select("facility_id") \
                          .eq("id", getattr(patients[0], "_supabase_id", "")).execute().data
                if p_row:
                    facility_id = p_row[0]["facility_id"]

        _finalize_run(run_id, last_state, patients, facility_id)

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


def _finalize_run(run_id: str, state: dict, patients: list, facility_id: str | None):
    """
    파이프라인이 끝까지(report → ... → END) 실행된 뒤 호출.
    이 시점 state에는 personalize_reasons, serving_map, report_paths 등이
    모두 채워져 있어야 정상(orchestrator_agent의 분기를 다 거쳤다는 전제).

    [참고] preference_weights/pool(preference_score)은 여기서 따로 저장하지
    않음 — preference_persistence_patch.py가 agents/preference_update_agent.py의
    save_weights/save_pool_scores 자체를 Supabase 쓰기로 교체해 두었으므로,
    그래프 실행 중 PreferenceUpdateAgent/WeightAdaptAgent 노드가 이미
    Supabase에 직접 저장을 마친 상태임.
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
    if state.get("nutrition_history"):
        _save_nutrition_intake(state, patients)

    update_row = {
        "status":        "approved",
        "reviewed_by":   "auto",
        "review_action": "approve",
        "reviewed_at":   _now_iso(),
        "f1_violation":  _safe_float(state.get("violation_rate")),
        "reoptimize_count": state.get("violation_count", 0),
    }

    # report_agent.py가 만든 로컬 파일(엑셀/조리지침서)을 Supabase Storage에
    # 업로드하고 다운로드 URL을 같이 저장. Render는 재배포 시 로컬 파일이
    # 사라지므로, 영구 보관은 Storage가 담당함.
    report_paths = state.get("report_paths")
    if report_paths:
        from app.services.report_storage import upload_report_files
        urls = upload_report_files(run_id, report_paths)
        if "meal_plan" in urls:
            update_row["report_meal_plan_url"] = urls["meal_plan"]
        if "serving" in urls:
            update_row["report_serving_url"] = urls["serving"]
        if "cooking" in urls:
            update_row["report_cooking_url"] = urls["cooking"]

    sb.table("meal_plan_runs").update(update_row).eq("id", run_id).execute()


def _safe_float(val):
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


BATCH_SIZE = 500
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 2.0


def _is_transient_network_error(e: Exception) -> bool:
    """일시적 네트워크 오류(연결 끊김 등)로 재시도해볼 만한지 판단.
    httpcore/httpx 예외를 여기서 직접 import해서 isinstance로 비교하지
    않고 예외 이름/메시지로 판단함 — postgrest-py가 내부적으로 쓰는
    HTTP 클라이언트 구현이 버전에 따라 바뀔 수 있어, 문자열 매칭이 더
    안전하고 의존성도 덜 생김."""
    text = f"{type(e).__name__}: {e}".lower()
    return any(kw in text for kw in [
        "disconnected", "remoteprotocolerror", "connectionreset",
        "timeout", "connectionerror", "broken pipe",
    ])


def _batched_write(sb, table_name: str, rows: list[dict],
                    mode: str = "insert", on_conflict: str | None = None,
                    batch_size: int = BATCH_SIZE):
    """
    [추가 — 2026-07-01] 대량의 행을 한 번의 요청으로 보내다가
    httpcore.RemoteProtocolError: Server disconnected로 실패하는 문제
    (personalized_swaps처럼 한 run에 수천~수만 건이 나올 수 있는 테이블)를
    막기 위해, batch_size개씩 나눠서 여러 번 보내고 각 배치는 일시적
    네트워크 오류 시 최대 MAX_RETRIES회 재시도함.
    """
    if not rows:
        return

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        last_exc = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                query = sb.table(table_name)
                if mode == "upsert":
                    query = query.upsert(batch, on_conflict=on_conflict)
                else:
                    query = query.insert(batch)
                query.execute()
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                if attempt < MAX_RETRIES and _is_transient_network_error(e):
                    wait = RETRY_BACKOFF_SEC * attempt
                    print(f"  [{table_name}] 배치 {i // batch_size + 1} 저장 실패"
                          f"(일시적 오류로 추정) — {wait:.0f}초 후 재시도 "
                          f"({attempt}/{MAX_RETRIES}): {e}")
                    time.sleep(wait)
                    continue
                break

        if last_exc:
            raise last_exc


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
        _batched_write(sb, "meal_plan_slots", rows, mode="upsert",
                       on_conflict="run_id,day_number,meal_type")


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
        _batched_write(sb, "personalized_swaps", rows, mode="insert")


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
        _batched_write(sb, "servings", rows, mode="upsert",
                       on_conflict="run_id,patient_id,day_number,meal_type")


def _save_nutrition_intake(state: dict, patients: list):
    """
    [추가 — 2026-07-01] plate_waste_input_agent(waste_monitoring_agent.py)가
    계산한 끼니별 실제 섭취 영양(state["nutrition_history"])을
    nutrition_intake_logs에 저장. "영양소 섭취 현황"(환자별 평균 섭취량)
    대시보드가 이 테이블을 읽음.

    이전에는 이 계산 결과가 nutrition_history라는 인메모리 state에만
    존재하고 실행이 끝나면 사라졌음(선호도 점수로 변환되는 용도로만
    쓰이고, 원본 섭취량 자체는 저장되지 않았음).

    waste_log_id를 고유키로 upsert하므로, 같은 잔반 기록을 다시 계산해도
    중복 없이 최신 값으로 갱신됨(파이프라인이 매번 nutrition_history를
    처음부터 재계산하는 구조이기 때문에 중요함).
    """
    sb = get_supabase()
    id_map = _patient_id_lookup(patients)
    history = state.get("nutrition_history") or {}

    rows = []
    for name, records in history.items():
        patient_id = id_map.get(name)
        if not patient_id:
            continue
        for rec in records:
            waste_log_id = rec.get("waste_log_id")
            if not waste_log_id:
                continue  # 원본 잔반 기록과 연결 안 되는 항목은 저장하지 않음
            day_str = rec.get("day", "0일")
            try:
                day_number = int(str(day_str).replace("일", ""))
            except (TypeError, ValueError):
                day_number = 0

            rows.append({
                "waste_log_id": waste_log_id,
                "patient_id":   patient_id,
                "day_number":   day_number,
                "meal_type":    rec.get("meal"),
                "energy_kcal":  rec.get("energy", 0),
                "protein_g":    rec.get("protein", 0),
                "sodium_mg":    rec.get("sodium", 0),
                "carb_g":       rec.get("carb", 0),
            })

    if rows:
        _batched_write(sb, "nutrition_intake_logs", rows, mode="upsert",
                       on_conflict="waste_log_id")
        print(f"  [nutrition_intake_logs 저장] {len(rows)}건")


def _save_waste_alerts(run_id: str, state: dict, patients: list):
    """
    waste_monitoring_subgraph(NutritionMonitorAgent/AlertAgent/InterventionAgent)가
    state['alert_queue']에 쌓아둔 알림+처방을 nutrition_alerts/interventions에 저장.

    [수정 — 2026-07-01] waste_monitoring_agent.py의 nutrition_monitor_agent가
    실제로 만드는 alert 딕셔너리 키는 "days"/"target"/"deficit_pct"인데,
    여기서는 존재하지 않는 키("consecutive_days"/"standard_value"/
    "deficit_rate")를 읽고 있어 항상 0으로 저장되던 버그를 수정. 아래
    alert_agent.py의 _format_kakao_message가 쓰는 키와 동일하게 맞춤.
    "sent_at": "now()" 문자열 리터럴 버그도 함께 수정.
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
            "consecutive_days": alert.get("days", 0),
            "avg_intake":       alert.get("avg_intake", 0),
            "standard_value":   alert.get("target", 0),
            "deficit_rate":     alert.get("deficit_pct", 0),
            "status":           "sent",
            "sent_at":          _now_iso(),
        }
        result = sb.table("nutrition_alerts").insert(row).execute()
        alert_id = result.data[0]["id"]

        prescription = alert.get("intervention")
        if prescription:
            sb.table("interventions").insert({
                "alert_id": alert_id,
                "prescription_text": prescription,
            }).execute()