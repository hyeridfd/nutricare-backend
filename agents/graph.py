"""
graph.py  ─  KG-MAS LangGraph 전체 그래프 (직렬화 안전 버전)
=============================================================
"""

import os
import pandas as pd
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command

import registry
from state import MealPlanState
from candidate_agent import candidate_agent
from optimizer_agent import optimizer_agent
from validator_agent import (
    validator_agent,
    route_after_validator,
    increment_violation_count,
)
from meal_plan_agent import meal_plan_agent
from serving_agent import serving_agent
from report_agent import report_agent
from waste_monitoring_agent import waste_monitoring_subgraph
from personalize_agent import personalize_agent
from preference_update_agent import preference_update_agent, weight_adapt_agent, load_weights
from orchestrator_agent import orchestrator_agent, route_from_orchestrator

load_dotenv()

WEIGHTS_PATH       = "preference_weights.json"
POOL_SCORES_PATH   = "pool_preference_scores.json"   # ← 추가

def save_pool_scores(pool: dict):
    """pool의 preference_score만 별도 저장"""
    scores = {}
    for cat, menus in pool.items():
        for m in menus:
            scores[m["menu_name"]] = m.get("preference_score", 0.7)
    with open(POOL_SCORES_PATH, "w", encoding="utf-8") as f:
        json.dump(scores, f, ensure_ascii=False, indent=2)
    print(f"  [pool 점수 저장] {POOL_SCORES_PATH}")

def load_pool_scores() -> dict:
    """저장된 preference_score 로드"""
    if os.path.exists(POOL_SCORES_PATH):
        with open(POOL_SCORES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


# ══════════════════════════════════════════════════════════════
# HITL 노드
# ══════════════════════════════════════════════════════════════
def hitl_node(state: MealPlanState):
    print("\n[HITL] 영양사 검토 대기 중...")
    print(f"  위반률: {state['violation_rate']:.4f} | "
          f"재최적화: {state.get('violation_count', 0)}회")

    response = interrupt({
        "message":        state.get("validator_msg", ""),
        "violation_rate": state["violation_rate"],
        "options":        ["approve", "revise", "reoptimize"],
    })

    action  = response.get("action", "approve")
    changes = response.get("changes", {})

    updates: dict = {
        "hitl_action":  action,
        "hitl_changes": changes,
        "messages":     [f"[HITL] action={action}"],
    }

    if action == "revise" and changes and state.get("df_menu_records"):
        # records → DataFrame → 수정 → 다시 records
        df = pd.DataFrame(state["df_menu_records"],
                          columns=state["df_menu_columns"])
        for key, new_menu in changes.items():
            day, meal, slot = key.split("_")
            mask = (df["일차"] == day) & (df["끼니"] == meal)
            df.loc[mask, slot] = new_menu
        updates["df_menu_records"] = df.to_dict("records")
        updates["messages"] = [f"[HITL] 수정 반영: {changes}"]

    return updates


def route_after_hitl(state: MealPlanState) -> str:
    return "reoptimize" if state.get("hitl_action") == "reoptimize" else "serving"


# ══════════════════════════════════════════════════════════════
# 그래프 조립
# ══════════════════════════════════════════════════════════════
def build_graph():
    builder = StateGraph(MealPlanState)

    builder.add_node("candidate",       candidate_agent)
    builder.add_node("optimizer",       optimizer_agent)
    builder.add_node("validator",       validator_agent)
    builder.add_node("increment_count", increment_violation_count)
    builder.add_node("orchestrator", orchestrator_agent)
    builder.add_node("meal_plan",       meal_plan_agent)
    builder.add_node("hitl",            hitl_node)
    builder.add_node("personalize", personalize_agent)
    builder.add_node("serving",         serving_agent)
    builder.add_node("report",          report_agent)
    builder.add_node("waste_monitoring",waste_monitoring_subgraph)
    # Individual 추가
    builder.add_node("preference_update", preference_update_agent)
    builder.add_node("weight_adapt",      weight_adapt_agent)

    builder.set_entry_point("candidate")
    builder.add_edge("candidate",  "optimizer")
    builder.add_edge("optimizer",  "validator")
    builder.add_edge("validator",  "orchestrator")


    builder.add_conditional_edges(
        "orchestrator",
        route_from_orchestrator,
        {
            "meal_plan":         "meal_plan",
            "reoptimize":        "increment_count",
            "personalize":       "personalize",
            "report":            "report",
            "waste_monitoring":  "waste_monitoring",
            "preference_update": "preference_update",
            "end":               END,
        },
    )
    builder.add_edge("increment_count", "optimizer")
    builder.add_edge("meal_plan",       "hitl")

    builder.add_conditional_edges(
        "hitl",
        route_after_hitl,
        {"serving": "orchestrator", "reoptimize": "increment_count"},
    )
    builder.add_edge("personalize", "serving")
    builder.add_edge("serving", "orchestrator")
    builder.add_edge("report", "orchestrator")
    builder.add_edge("waste_monitoring", "orchestrator")
    builder.add_edge("preference_update", "weight_adapt")
    builder.add_edge("weight_adapt",      END)

    # builder.add_conditional_edges(
    #     "report",
    #     lambda s: "waste_monitoring" if s.get("waste_log") else END,
    #     {"waste_monitoring": "waste_monitoring", END: END},
    # )
    # #Individual 추가
    # builder.add_edge("waste_monitoring",  "preference_update")
    # builder.add_edge("preference_update", "weight_adapt")
    # builder.add_edge("personalize", "serving")
    # builder.add_edge("weight_adapt",      END)

    return builder.compile(checkpointer=MemorySaver())


app = build_graph()


# ══════════════════════════════════════════════════════════════
# 구조도
# ══════════════════════════════════════════════════════════════
def show_graph():
    from IPython.display import display, Image
    display(Image(app.get_graph(xray=True).draw_mermaid_png()))

def print_mermaid():
    print(app.get_graph().draw_mermaid())


# ══════════════════════════════════════════════════════════════
# 실행 진입점
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    from facility_optimization import setup_facility

    fac = setup_facility("./data/고령자.xlsx", budget_per_meal=10000)

    # ── 직렬화 불가 객체 registry 등록 ───────────────────────
    registry.put("patients",      fac["patients"])
    registry.put("constraint",    fac["constraint"])
    registry.put("serving_agent", fac["serving"])
    prev_weights = load_weights()

    initial_state: MealPlanState = {
        "diseases":          fac["diseases"],
        "patients_key":      "patients",
        "constraint_key":    "constraint",
        "serving_agent_key": "serving_agent",
        "budget_per_meal":   10000,
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
        "waste_log":         None,
        "nutrition_history": None,
        "alert_queue":       None,
        "report_paths":      None,
        "orchestrator_phase": "optimize",
        "next_agent":         None,
        "preference_weights": prev_weights,
        "personal_menus":     None,
        "messages":          [],
        "waste_log": [
        # --- 1일차 ---
        {
            "name":  "구연직",
            "일차":  "1일",
            "끼니":  "아침",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "1일",
            "끼니":  "점심",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "1일",
            "끼니":  "저녁",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },

        # --- 2일차 ---
        {
            "name":  "구연직",
            "일차":  "2일",
            "끼니":  "아침",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "2일",
            "끼니":  "점심",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "2일",
            "끼니":  "저녁",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },

        # --- 3일차 ---
        {
            "name":  "구연직",
            "일차":  "3일",
            "끼니":  "아침",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "3일",
            "끼니":  "점심",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "3일",
            "끼니":  "저녁",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },

        # --- 4일차 ---
        {
            "name":  "구연직",
            "일차":  "4일",
            "끼니":  "아침",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "4일",
            "끼니":  "점심",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "4일",
            "끼니":  "저녁",
            "밥":    1.0, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.25, "김치": 0.0,
        },

        # --- 5일차 ---
        {
            "name":  "구연직",
            "일차":  "5일",
            "끼니":  "아침",
            "밥":    0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "5일",
            "끼니":  "점심",
            "밥":    0.25, "국": 0.0, "주찬": 0.5, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "5일",
            "끼니":  "저녁",
            "밥":    0.25, "국": 0.25, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0,
        },

        # --- 6일차 ---
        {
            "name":  "구연직",
            "일차":  "6일",
            "끼니":  "아침",
            "밥":    0.5, "국": 0.0, "주찬": 0.5, "부찬1": 0.25, "부찬2": 0.0, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "6일",
            "끼니":  "점심",
            "밥":    0.0, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "6일",
            "끼니":  "저녁",
            "밥":    0.25, "국": 0.5, "주찬": 0.75, "부찬1": 0.25, "부찬2": 0.25, "김치": 0.0,
        },

        # --- 7일차 ---
        {
            "name":  "구연직",
            "일차":  "7일",
            "끼니":  "아침",
            "밥":    0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "7일",
            "끼니":  "점심",
            "밥":    0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0,
        },
        {
            "name":  "구연직",
            "일차":  "7일",
            "끼니":  "저녁",
            "밥":    0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0,
        },

        # --- 8일차 ~ 14일차 (중간 생략 방지를 위해 반복 구조 생성) ---
        # (컨디션 저하 시기 가정: 8-10일)
        {"name": "구연직", "일차": "8일", "끼니": "아침", "밥": 0.5, "국": 0.25, "주찬": 0.75, "부찬1": 0.25, "부찬2": 0.5, "김치": 0.0},
        {"name": "구연직", "일차": "8일", "끼니": "점심", "밥": 0.75, "국": 0.5, "주찬": 0.5, "부찬1": 0.5, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "8일", "끼니": "저녁", "밥": 0.5, "국": 0.5, "주찬": 1.0, "부찬1": 0.75, "부찬2": 0.5, "김치": 0.25},
        {"name": "구연직", "일차": "9일", "끼니": "아침", "밥": 0.75, "국": 0.5, "주찬": 0.75, "부찬1": 0.5, "부찬2": 0.5, "김치": 0.0},
        {"name": "구연직", "일차": "9일", "끼니": "점심", "밥": 1.0, "국": 0.75, "주찬": 1.0, "부찬1": 0.75, "부찬2": 1.0, "김치": 0.5},
        {"name": "구연직", "일차": "9일", "끼니": "저녁", "밥": 0.75, "국": 1.0, "주찬": 1.0, "부찬1": 1.0, "부찬2": 0.75, "김치": 0.25},
        {"name": "구연직", "일차": "10일", "끼니": "아침", "밥": 0.5, "국": 0.25, "주찬": 0.5, "부찬1": 0.25, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "10일", "끼니": "점심", "밥": 0.25, "국": 0.0, "주찬": 0.5, "부찬1": 0.0, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "10일", "끼니": "저녁", "밥": 0.25, "국": 0.25, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        
        # (회복기: 11-14일)
        {"name": "구연직", "일차": "11일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "11일", "끼니": "점심", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "11일", "끼니": "저녁", "밥": 0.25, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "12일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "12일", "끼니": "점심", "밥": 0.0, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "12일", "끼니": "저녁", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "13일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "13일", "끼니": "점심", "밥": 0.25, "국": 0.0, "주찬": 0.5, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "13일", "끼니": "저녁", "밥": 0.25, "국": 0.25, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "14일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "14일", "끼니": "점심", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "14일", "끼니": "저녁", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},

        # --- 15일차 ~ 21일차 ---
        {"name": "구연직", "일차": "15일", "끼니": "아침", "밥": 0.25, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "15일", "끼니": "점심", "밥": 0.5, "국": 0.25, "주찬": 0.5, "부찬1": 0.25, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "15일", "끼니": "저녁", "밥": 0.25, "국": 0.0, "주찬": 0.75, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "16일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "16일", "끼니": "점심", "밥": 0.25, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "16일", "끼니": "저녁", "밥": 0.5, "국": 0.25, "주찬": 0.5, "부찬1": 0.25, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "17일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "17일", "끼니": "점심", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "17일", "끼니": "저녁", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "18일", "끼니": "아침", "밥": 0.25, "국": 0.0, "주찬": 0.5, "부찬1": 0.0, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "18일", "끼니": "점심", "밥": 0.5, "국": 0.25, "주찬": 0.75, "부찬1": 0.25, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "18일", "끼니": "저녁", "밥": 0.75, "국": 0.5, "주찬": 1.0, "부찬1": 0.5, "부찬2": 0.5, "김치": 0.25},
        {"name": "구연직", "일차": "19일", "끼니": "아침", "밥": 0.25, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "19일", "끼니": "점심", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "19일", "끼니": "저녁", "밥": 0.25, "국": 0.0, "주찬": 0.5, "부찬1": 0.0, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "20일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "20일", "끼니": "점심", "밥": 0.25, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "20일", "끼니": "저녁", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "21일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "21일", "끼니": "점심", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "21일", "끼니": "저녁", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},

        # --- 22일차 ~ 28일차 ---
        {"name": "구연직", "일차": "22일", "끼니": "아침", "밥": 0.5, "국": 0.25, "주찬": 0.75, "부찬1": 0.25, "부찬2": 0.5, "김치": 0.0},
        {"name": "구연직", "일차": "22일", "끼니": "점심", "밥": 0.25, "국": 0.0, "주찬": 0.5, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "22일", "끼니": "저녁", "밥": 0.5, "국": 0.5, "주찬": 0.5, "부찬1": 0.25, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "23일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "23일", "끼니": "점심", "밥": 0.0, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "23일", "끼니": "저녁", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "24일", "끼니": "아침", "밥": 0.25, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "24일", "끼니": "점심", "밥": 0.5, "국": 0.25, "주찬": 0.75, "부찬1": 0.5, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "24일", "끼니": "저녁", "밥": 0.25, "국": 0.25, "주찬": 0.5, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "25일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "25일", "끼니": "점심", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "25일", "끼니": "저녁", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "26일", "끼니": "아침", "밥": 0.25, "국": 0.0, "주찬": 0.5, "부찬1": 0.0, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "26일", "끼니": "점심", "밥": 0.75, "국": 0.5, "주찬": 1.0, "부찬1": 0.75, "부찬2": 0.5, "김치": 0.25},
        {"name": "구연직", "일차": "26일", "끼니": "저녁", "밥": 0.5, "국": 0.25, "주찬": 0.75, "부찬1": 0.25, "부찬2": 0.25, "김치": 0.0},
        {"name": "구연직", "일차": "27일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "27일", "끼니": "점심", "밥": 0.0, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "27일", "끼니": "저녁", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "28일", "끼니": "아침", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "28일", "끼니": "점심", "밥": 0.25, "국": 0.0, "주찬": 0.25, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
        {"name": "구연직", "일차": "28일", "끼니": "저녁", "밥": 0.0, "국": 0.0, "주찬": 0.0, "부찬1": 0.0, "부찬2": 0.0, "김치": 0.0},
]
    }

    config = {"configurable": {"thread_id": "run-001"}}

    print("=" * 60)
    print("  KG-MAS LangGraph 파이프라인 시작")
    print("=" * 60)

    # ── 1단계: candidate → optimizer → validator → meal_plan → hitl(interrupt) ──
    for event in app.stream(initial_state, config=config):
        if "__interrupt__" in event:
            interrupt_data = event["__interrupt__"]
            if isinstance(interrupt_data, (list, tuple)) and len(interrupt_data) > 0:
                payload = interrupt_data[0]
                print("\n" + "=" * 60)
                print("  [HITL INTERRUPT] 영양사 검토 필요")
                print("=" * 60)
                print(f"  메시지: {getattr(payload, 'value', {}).get('message', '')}")
                print(f"  위반률: {getattr(payload, 'value', {}).get('violation_rate', '')}")
            continue

        node = list(event.keys())[0]
        node_output = event[node]
        if isinstance(node_output, dict):
            for m in node_output.get("messages", []):
                print(f"  >> {m}")

    # ── 2단계: 영양사 직접 입력 ─────────────────────────────
    print("\n" + "=" * 60)
    print("  영양사 결정을 입력해 주세요")
    print("=" * 60)
    print("  1. approve    — 현재 식단표 승인")
    print("  2. reoptimize — NSGA-II 재최적화 요청")
    print("  3. revise     — 특정 메뉴 직접 수정")
    print("=" * 60)

    action = ""
    while action not in ("approve", "reoptimize", "revise"):
        action = input("  결정 입력 (approve / reoptimize / revise): ").strip().lower()
        if action not in ("approve", "reoptimize", "revise"):
            print("  잘못된 입력입니다. 다시 입력해 주세요.")

    changes = {}
    if action == "revise":
        print("\n  수정할 메뉴를 입력하세요.")
        print("  형식: 일차_끼니_슬롯=메뉴명  (예: 1일_점심_주찬=코다리조림)")
        print("  완료되면 빈 줄 입력")
        while True:
            line = input("  > ").strip()
            if not line:
                break
            if "=" in line:
                key, val = line.split("=", 1)
                changes[key.strip()] = val.strip()
        print(f"  수정 내용: {changes}")

    print(f"\n[HITL] 영양사 결정: {action}" + (f" | 수정: {changes}" if changes else ""))

    resume_payload = {"action": action}
    if changes:
        resume_payload["changes"] = changes

    # ── 3단계: HITL 재개 → serving → report ─────────────────
    print("\n[재개] serving → report 진행 중...")
    for event in app.stream(Command(resume=resume_payload), config=config):
        if "__interrupt__" in event:
            print("[HITL 재인터럽트 발생]")
            continue
        node = list(event.keys())[0]
        if isinstance(event[node], dict):
            for m in event[node].get("messages", []):
                print(f"  >> {m}")

    print("\n파이프라인 완료!")
    print("\n[Mermaid 구조도]")
    print_mermaid()