"""
validator_agent.py  ─  ValidatorAgent 노드 + conditional_edges 라우터
"""

import registry
from state import MealPlanState

VIOLATION_THRESH = 1.0 ## 고도화 필요(영양성분 지금 너무 안맞음)
MAX_REOPTIMIZE   = 1    ## 일닫 1번만 하기


def validator_agent(state: MealPlanState) -> dict:
    # ── registry에서 pymoo Result 꺼내기 ─────────────────────
    result = registry.get(state["nsga_result_key"])

    f1_min = float(result.F[:, 0].min())
    count  = state.get("violation_count", 0)

    f_summary = {
        "f1_영양위반": round(float(result.F[:, 0].min()), 4),
        "f2_예산초과": round(float(result.F[:, 1].min()), 4),
        "f3_다양성선호": round(float(result.F[:, 2].min()), 4),
        "f4_패턴위반": round(float(result.F[:, 3].min()), 4), # 기존 f4, f5가 통합됨
    }

    if f1_min <= VIOLATION_THRESH:
        msg = f"[ValidatorAgent] 통과 (f1={f1_min:.4f} ≤ {VIOLATION_THRESH})"
    elif count < MAX_REOPTIMIZE:
        msg = (f"[ValidatorAgent] 재최적화 요청 "
               f"(f1={f1_min:.4f} > {VIOLATION_THRESH}, "
               f"시도 {count+1}/{MAX_REOPTIMIZE})")
    else:
        msg = (f"[ValidatorAgent] 최대 재최적화 횟수 도달 "
               f"(f1={f1_min:.4f}, {MAX_REOPTIMIZE}회 완료) → 영양사 검토 요청")

    print(f"\n[ValidatorAgent] {msg}")
    print(f"  목적함수: {f_summary}")

    return {
        "violation_rate": f1_min,
        "validator_msg":  msg,
        "messages":       [msg],
    }


def route_after_validator(state: MealPlanState) -> str:
    f1    = state["violation_rate"]
    count = state.get("violation_count", 0)

    if f1 <= VIOLATION_THRESH:
        return "meal_plan"
    elif count < MAX_REOPTIMIZE:
        return "reoptimize"
    else:
        return "hitl"


def increment_violation_count(state: MealPlanState) -> dict:
    count = state.get("violation_count", 0) + 1
    print(f"\n[ViolationCounter] 재최적화 카운트: {count}")
    return {
        "violation_count": count,
        "messages": [f"[ViolationCounter] 재최적화 #{count} 진입"],
    }