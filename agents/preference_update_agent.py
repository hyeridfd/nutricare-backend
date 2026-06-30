"""
preference_update_agent.py
===========================
잔반 데이터 기반 개인별 메뉴 선호도 점수 갱신 (EMA)
+ 시설 전체 기피 메뉴 → NSGA-II pool 가중치 반영
+ preference_weights.json 저장/로드 (실행 간 지속)
"""

import json
import os
from state import MealPlanState

DECAY              = 0.85   # 과거 기억 유지율
ALPHA              = 0.15   # 새 관측 반영률
SLOTS              = ["밥", "국", "주찬", "부찬1", "부찬2", "김치"]
MAJORITY_THRESHOLD = 0.5    # 과반수 기피 시 NSGA-II 반영
FACILITY_DISLIKE_THRESHOLD = 0.65
WEIGHTS_PATH       = "preference_weights.json"
POOL_SCORES_PATH   = "pool_preference_scores.json"

# ── pool 점수 저장/로드 ───────────────────────────────────────
def save_pool_scores(pool: dict):
    scores = {}
    for cat, menus in pool.items():
        for m in menus:
            scores[m["menu_name"]] = m.get("preference_score", 0.7)
    with open(POOL_SCORES_PATH, "w", encoding="utf-8") as f:
        json.dump(scores, f, ensure_ascii=False, indent=2)
    print(f"  [pool 점수 저장] {POOL_SCORES_PATH} ({len(scores)}건)")


def load_pool_scores() -> dict:
    if os.path.exists(POOL_SCORES_PATH):
        with open(POOL_SCORES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


# ── 저장/로드 헬퍼 ────────────────────────────────────────────
def save_weights(weights: dict):
    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(weights, f, ensure_ascii=False, indent=2)
    print(f"  [선호도 저장] {WEIGHTS_PATH} ({len(weights)}명)")


def load_weights() -> dict:
    if os.path.exists(WEIGHTS_PATH):
        with open(WEIGHTS_PATH, "r", encoding="utf-8") as f:
            weights = json.load(f)
        print(f"  [선호도 로드] {WEIGHTS_PATH} ({len(weights)}명)")
        return weights
    return {}


# ══════════════════════════════════════════════════════════════
# 1. PreferenceUpdateAgent
# ══════════════════════════════════════════════════════════════
def preference_update_agent(state: MealPlanState) -> dict:
    print("\n[PreferenceUpdateAgent] 선호도 점수 갱신 시작...")

    history = state.get("nutrition_history") or {}

    # state에 있으면 사용, 없으면 JSON에서 로드
    weights = dict(state.get("preference_weights") or {})
    if not weights:
        weights = load_weights()

    updated = 0

    for name, records in history.items():
        if name not in weights:
            weights[name] = {}

        # 최근 7일치만 반영 (3끼 × 7일 = 21개)
        recent = records[-21:]

        for rec in recent:
            waste_map = rec.get("waste", {})
            menu_map  = rec.get("menu",  {})

            for slot in SLOTS:
                menu       = menu_map.get(slot, "")
                waste_rate = waste_map.get(slot, 0.0)
                if not menu:
                    continue

                intake_rate = 1.0 - waste_rate
                old_score   = weights[name].get(menu, 0.7)
                new_score   = round(
                    max(0.1, min(1.0, DECAY * old_score + ALPHA * intake_rate)), 3
                )
                weights[name][menu] = new_score
                updated += 1

    # ── 진단 출력 ─────────────────────────────────────────────
    for name in list(weights.keys())[:3]:
        low = {m: s for m, s in weights[name].items() if s < 0.4}
        if low:
            print(f"  [{name}] 기피 메뉴: {low}")
        else:
            bottom3 = sorted(weights[name].items(), key=lambda x: x[1])[:3]
            print(f"  [{name}] 최저 점수 메뉴: {bottom3}")

    # ── JSON 저장 ─────────────────────────────────────────────
    save_weights(weights)

    print(f"[PreferenceUpdateAgent] 완료 — {updated}건 갱신")
    return {
        "preference_weights": weights,
        "messages": [f"[PreferenceUpdateAgent] {updated}건 선호도 갱신"],
    }


# ══════════════════════════════════════════════════════════════
# 2. WeightAdaptAgent
# ══════════════════════════════════════════════════════════════
def weight_adapt_agent(state: MealPlanState) -> dict:
    print("\n[WeightAdaptAgent] NSGA-II pool 가중치 적용 시작...")

    weights = state.get("preference_weights") or {}
    pool    = state.get("pool") or {}

    if not weights:
        weights = load_weights()

    # 메뉴별 전체 입소자 점수 수집
    menu_scores: dict = {}
    for name, prefs in weights.items():
        for menu, score in prefs.items():
            menu_scores.setdefault(menu, []).append(score)

    # # 기피 메뉴 현황 출력
    # facility_dislike = {
    #     m: round(sum(s)/len(s), 3)
    #     for m, s in menu_scores.items()
    #     if sum(s)/len(s) < 0.5
    # }
    facility_dislike = {
        m: round(sum(s)/len(s), 3)
        for m, s in menu_scores.items()
        if sum(s)/len(s) < FACILITY_DISLIKE_THRESHOLD   # ← 0.65
    }

    if facility_dislike:
        top5 = sorted(facility_dislike.items(), key=lambda x: x[1])[:5]
        print(f"  [시설 기피 메뉴 Top5]: {top5}")

    updated_pool = {}
    for cat, menus in pool.items():
        updated_pool[cat] = []
        for m in menus:
            scores = menu_scores.get(m["menu_name"], [])
            if scores:
                dislike_ratio = sum(
                    1 for s in scores
                    if s < FACILITY_DISLIKE_THRESHOLD
                ) / len(scores)
                facility_score = 0.3 if dislike_ratio > MAJORITY_THRESHOLD else 0.7
            else:
                facility_score = 0.7

            updated_pool[cat].append({
                **m,
                "preference_score": facility_score,
            })
    save_pool_scores(updated_pool)
    print(f"[WeightAdaptAgent] 완료 — pool 업데이트")
    return {
        "pool":     updated_pool,
        "messages": ["[WeightAdaptAgent] pool preference_score 갱신 완료"],
    }