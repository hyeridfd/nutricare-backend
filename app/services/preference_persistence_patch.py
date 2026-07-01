"""
services/preference_persistence_patch.py — 선호도 저장소를 Supabase로 전환
================================================================================
agents/preference_update_agent.py의 save_weights/load_weights/
save_pool_scores/load_pool_scores는 로컬 JSON 파일(preference_weights.json,
pool_preference_scores.json)을 씁니다. 이는 graph.py를 콘솔에서 단독
실행할 때는 적절하지만, Render는 재배포마다 파일시스템이 초기화되어
선호도 학습이 매번 사라지는 문제가 있었습니다.

apply_patch()는 FastAPI 앱이 시작될 때(main.py 최상단, 다른 어떤 agents
모듈이 import되기 전) 한 번 호출되어 위 네 함수를 Supabase 호출로
교체(monkey-patch)합니다. agents/ 폴더의 원본 코드는 그대로 두므로,
로컬에서 graph.py를 콘솔로 단독 실행할 때는(이 패치 없이) 여전히
파일 기반으로 동작합니다.

candidate_agent.py가 `from preference_update_agent import load_pool_scores`로
함수를 직접 import해 쓰고 있어서, 모듈 자체의 함수 객체를 교체해야
candidate_agent.py 쪽에서도 패치된 버전이 호출됩니다.

[모듈 전역 상태]
이전 버전은 클로저로 facility 컨텍스트를 가뒀는데, main.py와
pipeline_runner.py 양쪽에서 apply_patch()를 각각 호출하면 서로 다른
클로저(=서로 다른 컨텍스트)가 생겨 set_active_facility 호출이 엇갈리는
문제가 있었음. 모듈 전역 dict로 바꿔, apply_patch()는 main.py에서 단 한
번만 호출하고 set_active_facility()는 어디서든 import해서 같은 상태를
공유하도록 함.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

_facility_ctx = {"id": None}
_patched = False


def _now_iso() -> str:
    """Supabase(Postgres) timestamp 컬럼에 안전하게 넣을 UTC ISO 문자열.
    [수정 — 2026-07-01] 기존 "now()" 문자열 리터럴은 Postgres가 유효한
    timestamp로 인식하지 못해(괄호 포함 문자열은 함수 호출이 아니라 그냥
    텍스트로 처리됨) upsert가 실패했었음(meal_plans.py의 approve_run과
    동일한 버그 패턴)."""
    return datetime.now(timezone.utc).isoformat()


def set_active_facility(facility_id: str):
    """pipeline_runner.py가 파이프라인 실행 직전에 호출해, 이후
    save/load 함수들이 어느 facility의 데이터를 다룰지 지정."""
    _facility_ctx["id"] = facility_id


def apply_patch():
    """main.py 최상단에서 단 한 번만 호출. 이미 적용됐으면 재적용하지 않음."""
    global _patched
    if _patched:
        return
    _patched = True

    import preference_update_agent as pua
    from app.services.db_clients import get_supabase

    def save_weights(weights: dict):
        sb = get_supabase()
        names = list(weights.keys())
        if not names:
            return

        patients = sb.table("patients").select("id, name").in_("name", names).execute().data
        id_by_name = {p["name"]: p["id"] for p in patients}

        rows = []
        for name, prefs in weights.items():
            patient_id = id_by_name.get(name)
            if not patient_id:
                continue
            for menu_name, score in prefs.items():
                rows.append({
                    "patient_id": patient_id,
                    "menu_name": menu_name,
                    "score": score,
                    "updated_at": _now_iso(),
                })
        if rows:
            sb.table("preference_scores").upsert(
                rows, on_conflict="patient_id,menu_name"
            ).execute()
        print(f"  [선호도 저장 → Supabase] {len(rows)}건")

    def load_weights() -> dict:
        sb = get_supabase()
        facility_id = _facility_ctx["id"]
        if not facility_id:
            return {}

        patients = sb.table("patients").select("id, name") \
                     .eq("facility_id", facility_id).eq("active", True).execute().data
        if not patients:
            return {}
        patient_ids = [p["id"] for p in patients]
        name_by_id = {p["id"]: p["name"] for p in patients}

        scores = sb.table("preference_scores").select("patient_id, menu_name, score") \
                   .in_("patient_id", patient_ids).execute().data

        weights: dict = {}
        for row in scores:
            name = name_by_id.get(row["patient_id"])
            if not name:
                continue
            weights.setdefault(name, {})[row["menu_name"]] = row["score"]

        print(f"  [선호도 로드 ← Supabase] {len(weights)}명")
        return weights

    def save_pool_scores(pool: dict):
        sb = get_supabase()
        facility_id = _facility_ctx["id"]
        if not facility_id:
            print("  [경고] facility_id 미설정 — pool 점수 저장 건너뜀")
            return

        rows = []
        for cat, menus in pool.items():
            for m in menus:
                rows.append({
                    "facility_id": facility_id,
                    "menu_name": m["menu_name"],
                    "score": m.get("preference_score", 0.7),
                    "updated_at": _now_iso(),
                })
        if rows:
            sb.table("pool_preference_scores").upsert(
                rows, on_conflict="facility_id,menu_name"
            ).execute()
        print(f"  [pool 점수 저장 → Supabase] {len(rows)}건")

    def load_pool_scores() -> dict:
        sb = get_supabase()
        facility_id = _facility_ctx["id"]
        if not facility_id:
            return {}

        rows = sb.table("pool_preference_scores").select("menu_name, score") \
                 .eq("facility_id", facility_id).execute().data
        scores = {r["menu_name"]: r["score"] for r in rows}
        print(f"  [pool 점수 로드 ← Supabase] {len(scores)}건")
        return scores

    pua.save_weights = save_weights
    pua.load_weights = load_weights
    pua.save_pool_scores = save_pool_scores
    pua.load_pool_scores = load_pool_scores