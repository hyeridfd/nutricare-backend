"""
routers/preferences.py — 메뉴 선호도 조회
=======================================
preference_persistence_patch.py가 파이프라인 실행 중(PreferenceUpdateAgent/
WeightAdaptAgent) Supabase에 저장해 두는 두 테이블을 읽어서, 프론트엔드
(src/lib/api.ts의 preferencesApi)가 기대하는 형태로 응답합니다.

  - pool_preference_scores: 시설 전체 메뉴 선호도 (facility_id, menu_name, score)
  - preference_scores:      환자별 메뉴 선호도 (patient_id, menu_name, score)

임계값(FACILITY_DISLIKE_THRESHOLD=0.65, PERSONAL_DISLIKE_THRESHOLD=0.6)은
agents/preference_update_agent.py, agents/personalize_agent.py에 있는
동일한 상수를 그대로 맞춰 둠. 이 라우터가 앱 시작 시점(agents/ 경로가
아직 sys.path에 없을 수 있는 시점)에 임포트될 수도 있어, 취약한
크로스디렉토리 임포트 대신 값을 그대로 복제해 둠 — 원본 상수가 바뀌면
여기도 같이 맞춰줘야 함.
"""

from fastapi import APIRouter
from app.services.db_clients import get_supabase

router = APIRouter()

FACILITY_DISLIKE_THRESHOLD = 0.65   # preference_update_agent.py와 동일
PERSONAL_DISLIKE_THRESHOLD = 0.6    # personalize_agent.py의 PERSONAL_DISLIKE와 동일


@router.get("/facility")
def get_facility_preferences(facility_id: str, limit: int = 50):
    """
    시설 전체 메뉴 선호도(=NSGA-II pool에 실제로 반영되는 점수) 목록.
    점수가 낮은(기피) 메뉴가 먼저 보이도록 오름차순 정렬.
    """
    sb = get_supabase()
    rows = (
        sb.table("pool_preference_scores")
          .select("menu_name,score,updated_at")
          .eq("facility_id", facility_id)
          .order("score")
          .limit(limit)
          .execute()
          .data
    )
    dislike_count = sum(1 for r in rows if (r.get("score") or 0) < FACILITY_DISLIKE_THRESHOLD)
    like_count = len(rows) - dislike_count

    return {
        "items": rows,
        "dislike_count": dislike_count,
        "like_count": like_count,
    }


@router.get("/patients")
def get_patient_preferences(facility_id: str, patient_id: str | None = None):
    """
    patient_id가 없으면: 시설 내 전체 환자의 선호도 요약 목록(카드/표용).
    patient_id가 있으면: 그 환자 한 명의 메뉴별 점수 상세.
    프론트가 같은 경로를 두 가지 용도로 쓰므로(preferencesApi.patientsList /
    patientDetail) 쿼리 파라미터 유무로 분기.
    """
    sb = get_supabase()

    patients = (
        sb.table("patients")
          .select("id,name")
          .eq("facility_id", facility_id)
          .eq("active", True)
          .execute()
          .data
    )
    if not patients:
        if patient_id:
            return {"patient_name": None, "items": [], "dislike_count": 0, "like_count": 0}
        return {"patients": []}

    id_to_name = {p["id"]: p["name"] for p in patients}

    if patient_id:
        # ── 단일 환자 상세 ──────────────────────────────────
        if patient_id not in id_to_name:
            return {"patient_name": None, "items": [], "dislike_count": 0, "like_count": 0}

        rows = (
            sb.table("preference_scores")
              .select("menu_name,score,updated_at")
              .eq("patient_id", patient_id)
              .order("score")
              .execute()
              .data
        )
        dislike_count = sum(1 for r in rows if (r.get("score") or 0) < PERSONAL_DISLIKE_THRESHOLD)
        like_count = len(rows) - dislike_count

        return {
            "patient_name": id_to_name[patient_id],
            "items": rows,
            "dislike_count": dislike_count,
            "like_count": like_count,
        }

    # ── 전체 환자 요약 목록 ────────────────────────────────
    patient_ids = list(id_to_name.keys())
    rows = (
        sb.table("preference_scores")
          .select("patient_id,score")
          .in_("patient_id", patient_ids)
          .execute()
          .data
    )

    by_patient: dict[str, list[float]] = {}
    for r in rows:
        by_patient.setdefault(r["patient_id"], []).append(r.get("score") or 0)

    summaries = []
    for pid, name in id_to_name.items():
        scores = by_patient.get(pid, [])
        dislike_count = sum(1 for s in scores if s < PERSONAL_DISLIKE_THRESHOLD)
        summaries.append({
            "patient_id": pid,
            "patient_name": name,
            "total_menus": len(scores),
            "dislike_count": dislike_count,
            "like_count": len(scores) - dislike_count,
        })

    return {"patients": summaries}