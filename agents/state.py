"""
state.py  ─  KG-MAS 공유 상태 (직렬화 안전 버전)
==================================================
직렬화 불가 객체(FacilityConstraintAdapter, PatientProfile, pymoo Result)는
registry.py에 보관하고, state에는 문자열 키만 저장합니다.

  직렬화 불가 → registry  |  state에는 키(str)만
  ─────────────────────────────────────────────
  FacilityConstraintAdapter  →  "constraint_key"   : str
  list[PatientProfile]       →  "patients_key"     : str
  pymoo Result               →  "nsga_result_key"  : str
  ServingAgent               →  "serving_agent_key": str
"""

import operator
from typing import TypedDict, Annotated, Optional


class MealPlanState(TypedDict):

    # ── 1. 시설 기본 정보 ─────────────────────────────────────
    diseases:         list[str]    # ["고혈압", "당뇨", ...]
    patients_key:     str          # registry 키 → list[PatientProfile]
    constraint_key:   str          # registry 키 → FacilityConstraintAdapter
    budget_per_meal:  float

    # ── 2. CandidateAgent 출력 ───────────────────────────────
    pool:             Optional[dict]   # 직렬화 가능 (dict of list of dict)

    # ── 3. OptimizerAgent 출력 ──────────────────────────────
    nsga_result_key:  Optional[str]    # registry 키 → pymoo Result
    violation_count:  int

    # ── 4. MealPlanAgent 출력 ───────────────────────────────
    df_menu_records:  Optional[list]   # DataFrame → records (직렬화 가능)
    df_menu_columns:  Optional[list]   # 컬럼명 리스트
    recommend_map:    Optional[dict]

    # ── 5. ValidatorAgent 출력 ──────────────────────────────
    violation_rate:   float
    validator_msg:    str

    # ── 6. OrchestratorAgent ────────────────────────────────
    orchestrator_phase: Optional[str]  # optimize/confirm/serve/report/monitor/learn/done
    next_agent:         Optional[str]  # 다음 실행 에이전트 이름

    # ── 7. Human-in-the-loop ────────────────────────────────
    hitl_action:      Optional[str]    # "approve" | "revise" | "reoptimize"
    hitl_changes:     Optional[dict]

    # ── 8. ServingAgent 출력 ────────────────────────────────
    serving_agent_key: Optional[str]   # registry 키 → ServingAgent 객체
    serving_map:       Optional[dict]

    # ── 9. 잔반 모니터링 ─────────────────────────────────────
    waste_log:          Optional[list]
    nutrition_history:  Optional[dict]
    alert_queue:        Optional[list]

    # ── 10. 선호도 학습 ──────────────────────────────────────
    preference_weights: Optional[dict]  # {name: {menu_name: score 0~1}}
    personal_menus:     Optional[dict]  # {name||day||meal: {slot: menu}}
    violation_ratio_map: Optional[dict]  # {name||day||meal: ratio} - 질환 위반 보정용 (PersonalizeAgent → ServingAgent)
    personalize_reasons: Optional[dict]  # {name||day||meal: [{"slot","from","to","reason","detail","ratio"}]} - ReportAgent 라벨용

    # ── 11. ReportAgent 출력 ─────────────────────────────────
    report_paths:       Optional[dict]

    # ── 12. 메시지 로그 ─────────────────────────────────────
    messages: Annotated[list[str], operator.add]