"""
orchestrator_agent.py  ─  OrchestratorAgent 노드
==================================================
전체 파이프라인의 흐름을 중앙에서 평가하고
다음 실행 에이전트를 동적으로 결정합니다.

담당 결정:
  ① optimize  단계: 검증 결과 → meal_plan / reoptimize / hitl
  ② confirm   단계: HITL 결과 → personalize / reoptimize
  ③ serve     단계: 배식 완료 → report
  ④ report    단계: 보고서 완료 → waste_monitoring / end
  ⑤ monitor   단계: 모니터링 완료 → preference_update
  ⑥ learn     단계: 학습 완료 → end (다음 실행 대기)
"""

from state import MealPlanState

VIOLATION_THRESH = 1.0
MAX_REOPTIMIZE   = 1   # 1번으로 변경


def orchestrator_agent(state: MealPlanState) -> dict:
    phase = state.get("orchestrator_phase", "optimize")

    print(f"\n[OrchestratorAgent] 현재 단계: {phase}")

    # ── ① 최적화 단계 ─────────────────────────────────────────
    if phase == "optimize":
        f1    = state.get("violation_rate", 999.0)
        count = state.get("violation_count", 0)

        if f1 <= VIOLATION_THRESH:
            next_agent = "meal_plan"
            next_phase = "confirm"
            reason = f"f1={f1:.4f} ≤ {VIOLATION_THRESH} → 식단표 생성"
        elif count < MAX_REOPTIMIZE:
            next_agent = "reoptimize"
            next_phase = "optimize"
            reason = f"f1={f1:.4f} > {VIOLATION_THRESH}, {count+1}/{MAX_REOPTIMIZE}회 → 재최적화"
        else:
            next_agent = "meal_plan"
            next_phase = "confirm"
            reason = f"최대 재최적화 {MAX_REOPTIMIZE}회 도달 → 영양사 검토"

    # ── ② 확정 단계 (HITL 결과 평가) ─────────────────────────
    elif phase == "confirm":
        action = state.get("hitl_action", "approve")

        if action == "reoptimize":
            next_agent = "reoptimize"
            next_phase = "optimize"
            reason = "영양사 재최적화 요청"
        else:
            next_agent = "personalize"
            next_phase = "serve"
            reason = f"영양사 {action} → 개인화 배식 진행"

    # ── ③ 배식 단계 ───────────────────────────────────────────
    elif phase == "serve":
        next_agent = "report"
        next_phase = "report"
        reason = "배식량 산출 완료 → 보고서 생성"

    # ── ④ 보고서 단계 ─────────────────────────────────────────
    elif phase == "report":
        if state.get("waste_log"):
            next_agent = "waste_monitoring"
            next_phase = "monitor"
            reason = "잔반 데이터 있음 → 모니터링 시작"
        else:
            next_agent = "end"
            next_phase = "done"
            reason = "잔반 데이터 없음 → 종료"

    # ── ⑤ 모니터링 단계 ───────────────────────────────────────
    elif phase == "monitor":
        next_agent = "preference_update"
        next_phase = "learn"
        reason = "모니터링 완료 → 선호도 학습"

    # ── ⑥ 학습 단계 ───────────────────────────────────────────
    elif phase == "learn":
        next_agent = "end"
        next_phase = "done"
        reason = "선호도 학습 완료 → 파이프라인 종료"

    else:
        next_agent = "end"
        next_phase = "done"
        reason = "알 수 없는 단계 → 종료"

    print(f"[OrchestratorAgent] {reason}")
    print(f"[OrchestratorAgent] → 다음: {next_agent}")

    return {
        "next_agent":        next_agent,
        "orchestrator_phase": next_phase,
        "messages": [f"[OrchestratorAgent] {phase} → {next_agent} ({reason})"],
    }


def route_from_orchestrator(state: MealPlanState) -> str:
    """OrchestratorAgent 결정에 따라 다음 노드를 반환합니다."""
    return state.get("next_agent", "end")