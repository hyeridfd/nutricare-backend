"""
seed_waste_logs.py — R001~R068 1일차 잔반 데이터 시드 스크립트
================================================================================
지금은 임의(랜덤) 잔반율로 채우지만, 나중에 누비랩(NuviLab) 스캐너가 JSON으로
실제 잔반 데이터를 주면 아래 구조에서 "데이터 생성" 부분만 교체하면 됨:

  ① _fetch_patients()      — 그대로 재사용 (이름 → patient_id 매핑)
  ② _generate_dummy_rate() — [교체 대상] 지금은 random, 나중엔 누비랩 JSON에서
                              실제 잔반율(중량 기반)을 파싱해서 반환하도록 바꾸면 됨
  ③ _build_waste_log()     — [교체 대상] 누비랩 JSON의 한 레코드를 이 함수와
                              같은 딕셔너리 형태(WasteLogCreate 스키마)로 변환
  ④ _post_waste_log()      — 그대로 재사용 (백엔드로 전송)
  ⑤ main()                 — 그대로 재사용 (반복 실행 로직)

즉 누비랩 연동 시에는 ②③만 "JSON 읽기 → 파싱" 코드로 바꾸고, 나머지는 그대로
쓰면 됨.

사용 전 준비:
  1. pip install requests
  2. 아래 FACILITY_ID를 실제 시설 UUID로 채우기
     (Supabase Table Editor → facilities 테이블에서 확인 가능)
  3. patients 테이블에 R001~R068이라는 name으로 환자가 이미 등록되어 있어야 함
     (등록 안 되어 있으면 해당 환자는 건너뛰고 경고만 출력됨)
"""

import random
import requests

BACKEND_BASE = "https://nutricare-backend-evp1.onrender.com"
FACILITY_ID = "fe95c924-0bfb-4ba1-b6f4-3fad523dc84c"   # Supabase facilities.id (UUID)

MEAL_TYPES = ["아침", "점심", "저녁"]
DAY_NUMBER = 1
PATIENT_COUNT = 68  # R001 ~ R068


# ════════════════════════════════════════════════════════════
# ① 환자 이름(R001 등) → 실제 Supabase patient_id(UUID) 조회
# ════════════════════════════════════════════════════════════
def _fetch_patients(facility_id: str) -> dict:
    res = requests.get(f"{BACKEND_BASE}/api/patients", params={"facility_id": facility_id})
    res.raise_for_status()
    patients = res.json()
    return {p["name"]: p["id"] for p in patients}


# ════════════════════════════════════════════════════════════
# ② [나중에 누비랩 JSON 파싱으로 교체할 부분]
#    지금은 임의 값 — 대부분 적당히 남기고(0.1~0.5), 가끔 완식(0)이나
#    거의 안 드심(0.5+)도 섞어서 그럴듯한 분포를 만듦.
# ════════════════════════════════════════════════════════════
def _generate_dummy_rate() -> float:
    r = random.random()
    if r < 0.2:
        return round(random.uniform(0.0, 0.1), 2)    # 거의 다 드심
    elif r < 0.85:
        return round(random.uniform(0.1, 0.5), 2)     # 보통
    else:
        return round(random.uniform(0.5, 1.0), 2)     # 많이 남기심


# ════════════════════════════════════════════════════════════
# ③ [나중에 누비랩 JSON 레코드 → 이 딕셔너리 형태로 변환하는 함수로 교체]
#    WasteLogCreate 스키마(routers/waste.py)와 정확히 일치해야 함.
# ════════════════════════════════════════════════════════════
def _build_waste_log(patient_id: str, day_number: int, meal_type: str) -> dict:
    return {
        "patient_id": patient_id,
        "day_number": day_number,
        "meal_type": meal_type,
        "rice_waste_rate": _generate_dummy_rate(),
        "soup_waste_rate": _generate_dummy_rate(),
        "main_dish_waste_rate": _generate_dummy_rate(),
        "side_dish_1_waste_rate": _generate_dummy_rate(),
        "side_dish_2_waste_rate": _generate_dummy_rate(),
        "kimchi_waste_rate": _generate_dummy_rate(),
        "recorded_by": "seed_script",
    }


# ════════════════════════════════════════════════════════════
# ④ 백엔드로 전송 (그대로 재사용)
# ════════════════════════════════════════════════════════════
def _post_waste_log(row: dict) -> bool:
    res = requests.post(f"{BACKEND_BASE}/api/waste-logs", json=row)
    if not res.ok:
        print(f"  [실패] patient_id={row['patient_id']} {row['meal_type']}: "
              f"{res.status_code} {res.text}")
        return False
    return True


# ════════════════════════════════════════════════════════════
# ⑤ 실행 (그대로 재사용)
# ════════════════════════════════════════════════════════════
def main():
    if "여기에" in FACILITY_ID:
        print("[중단] 스크립트 상단의 FACILITY_ID를 실제 시설 UUID로 채워주세요.")
        return

    print(f"[1/2] {FACILITY_ID} 시설의 환자 목록 조회 중...")
    name_to_id = _fetch_patients(FACILITY_ID)

    target_names = [f"R{str(i).zfill(3)}" for i in range(1, PATIENT_COUNT + 1)]
    missing = [n for n in target_names if n not in name_to_id]
    if missing:
        print(f"  [경고] patients 테이블에서 못 찾아 건너뜀 ({len(missing)}명): {missing}")

    print(f"[2/2] 1일차 아침/점심/저녁 잔반 데이터 전송 중...")
    success, fail = 0, 0
    for name in target_names:
        patient_id = name_to_id.get(name)
        if not patient_id:
            continue
        for meal in MEAL_TYPES:
            row = _build_waste_log(patient_id, DAY_NUMBER, meal)
            if _post_waste_log(row):
                success += 1
            else:
                fail += 1

    print(f"\n완료 — 성공 {success}건 / 실패 {fail}건 "
          f"(대상 {len(target_names) - len(missing)}명 × {len(MEAL_TYPES)}끼)")


if __name__ == "__main__":
    main()