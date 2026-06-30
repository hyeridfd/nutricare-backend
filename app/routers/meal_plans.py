"""
routers/meal_plans.py — 식단 설계 파이프라인 실행 + HITL
=============================================================
LangGraph 파이프라인(CandidateAgent → ... → PreferenceUpdateAgent)을
백그라운드 태스크로 실행하고, 진행 상태와 결과를 Supabase에 기록합니다.

HITL 흐름:
  - 1차 구현: auto_approve=True가 기본값. ValidatorAgent가 통과를 못 해도
    최대 재최적화 횟수에 도달하면 자동으로 'approved' 처리하고 다음 단계로
    진행. 콘솔 input() 대신 review_action='auto'로 기록.
  - approve 엔드포인트는 미리 만들어 둠 — 나중에 auto_approve=False로 바꾸고
    프론트에서 영양사가 직접 승인 버튼을 누르는 흐름으로 전환 가능.
"""

import uuid
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app.services.db_clients import get_supabase
from app.services.pipeline_runner import run_pipeline_for_run

router = APIRouter()


class RunRequest(BaseModel):
    facility_id: str
    budget_per_meal: float = 10000
    auto_approve: bool = True        # 1차 구현: HITL 자동 승인


class RunResponse(BaseModel):
    run_id: str
    status: str


@router.post("/run", response_model=RunResponse)
def start_run(payload: RunRequest, background_tasks: BackgroundTasks):
    """
    파이프라인 실행을 시작하고 즉시 run_id를 반환.
    실제 NSGA-II 최적화는 background_tasks로 비동기 실행되며,
    프론트는 GET /{run_id}로 폴링해 진행 상태를 확인.

    [변경] diseases는 더 이상 요청에서 받지 않음 — 시설에 등록된 전체
    활성 환자를 기준으로 pipeline_runner가 자동 도출함(원본 agents 설계와
    동일). diseases_targeted는 일단 빈 배열로 시작하고, 파이프라인이
    질환을 도출한 직후 실제 값으로 채워짐.
    """
    sb = get_supabase()

    run_row = {
        "facility_id":       payload.facility_id,
        "status":            "optimizing",
        "diseases_targeted": [],
    }
    result = sb.table("meal_plan_runs").insert(run_row).execute()
    run_id = result.data[0]["id"]

    background_tasks.add_task(
        run_pipeline_for_run,
        run_id=run_id,
        facility_id=payload.facility_id,
        budget_per_meal=payload.budget_per_meal,
        auto_approve=payload.auto_approve,
    )

    return RunResponse(run_id=run_id, status="optimizing")


@router.get("/{run_id}")
def get_run_status(run_id: str):
    """
    진행 상태 + (완료 시) 결과 요약 조회.
    프론트가 2~3초 간격으로 폴링하다가 status가 'approved'/'rejected'가 되면 멈춤.
    """
    sb = get_supabase()
    run = sb.table("meal_plan_runs").select("*").eq("id", run_id).single().execute()
    if not run.data:
        raise HTTPException(404, "실행 기록을 찾을 수 없습니다.")

    response = dict(run.data)

    if run.data["status"] in ("approved", "approving", "pending_review"):
        slots = sb.table("meal_plan_slots").select("*").eq("run_id", run_id) \
                  .order("day_number").execute()
        response["meal_plan_slots"] = slots.data

    return response


@router.post("/{run_id}/approve")
def approve_run(run_id: str, reviewed_by: str = "영양사", background_tasks: BackgroundTasks = None):
    """
    HITL 승인 엔드포인트. 현재는 auto_approve 흐름이 기본이라 직접 호출할
    일은 적지만, pending_review 상태로 멈춰있는 실행을 영양사가 수동으로
    승인할 때 사용. 승인 후 PersonalizeAgent→ServingAgent→ReportAgent까지
    이어서 실행되므로 시간이 걸릴 수 있어 백그라운드로 처리.
    """
    from app.services.pipeline_runner import resume_after_approval

    sb = get_supabase()
    run = sb.table("meal_plan_runs").select("*").eq("id", run_id).single().execute()
    if not run.data:
        raise HTTPException(404, "실행 기록을 찾을 수 없습니다.")
    if run.data["status"] != "pending_review":
        raise HTTPException(400, f"승인 대기 상태가 아닙니다 (현재: {run.data['status']})")

    sb.table("meal_plan_runs").update({
        "status": "approving",
        "reviewed_by": reviewed_by,
        "review_action": "approve",
        "reviewed_at": "now()",
    }).eq("id", run_id).execute()

    background_tasks.add_task(resume_after_approval, run_id, "approve")
    return {"status": "approving", "run_id": run_id}



@router.post("/{run_id}/reject")
def reject_run(run_id: str, reviewed_by: str = "영양사", background_tasks: BackgroundTasks = None):
    """
    영양사가 재최적화를 요청하는 경우.
    graph.py의 route_after_hitl이 hitl_action=='reoptimize'를 보고
    increment_count → optimizer로 되돌리므로, 단순 상태 변경이 아니라
    실제로 Command(resume={"action":"reoptimize"})를 호출해야 함.
    """
    from app.services.pipeline_runner import resume_after_approval

    sb = get_supabase()
    run = sb.table("meal_plan_runs").select("*").eq("id", run_id).single().execute()
    if not run.data:
        raise HTTPException(404, "실행 기록을 찾을 수 없습니다.")

    sb.table("meal_plan_runs").update({
        "status": "optimizing",
        "reviewed_by": reviewed_by,
        "review_action": "reoptimize",
        "reviewed_at": "now()",
    }).eq("id", run_id).execute()

    background_tasks.add_task(resume_after_approval, run_id, "reoptimize")
    return {"status": "reoptimizing", "run_id": run_id}


@router.get("/{run_id}/personalized-swaps")
def get_personalized_swaps(run_id: str, patient_id: str | None = None):
    """개인화_부찬대체 시트에 해당하는 데이터 — 프론트 표 렌더링용."""
    sb = get_supabase()
    query = sb.table("personalized_swaps").select("*").eq("run_id", run_id)
    if patient_id:
        query = query.eq("patient_id", patient_id)
    result = query.order("day_number").execute()
    return result.data


@router.get("/{run_id}/servings")
def get_servings(run_id: str, patient_id: str | None = None):
    """개인별 배식량 — 프론트 표 렌더링용."""
    sb = get_supabase()
    query = sb.table("servings").select("*").eq("run_id", run_id)
    if patient_id:
        query = query.eq("patient_id", patient_id)
    result = query.order("day_number").execute()
    return result.data