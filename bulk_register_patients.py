"""
bulk_register_patients.py — CSV → Supabase 환자 일괄 등록
=============================================================
어르신DB.csv를 읽어서 백엔드 POST /api/patients API로 한 명씩 등록합니다.
이미 만든 patient_logic.py / build_patient_profile 로직을 그대로 통과시켜
영양기준(constraint)이 정확히 계산된 상태로 Supabase에 저장됩니다.

사용법:
    python bulk_register_patients.py 어르신DB.csv <API_BASE_URL> <FACILITY_ID>

예:
    python bulk_register_patients.py 어르신DB.csv https://nutricare-backend-evp1.onrender.com 89b50218-...
"""

import sys
import csv
import json
import urllib.request
import urllib.error

# ── 식사형태 매핑 (CSV "일반식/일반찬" → API meal_texture_rice/side) ──
def parse_meal_texture(value: str) -> tuple[str, str]:
    rice_part, side_part = value.split("/")
    rice = "죽" if rice_part.startswith("죽") else "밥"
    side = side_part  # "일반찬" | "다진찬" | "갈찬" 그대로 사용
    return rice, side


def build_diseases(row: dict) -> list[str]:
    diseases = []
    if row["당뇨병"].strip().lower() == "true":
        diseases.append("당뇨병")
    if row["고혈압"].strip().lower() == "true":
        diseases.append("고혈압")
    if row["신장질환"].strip().lower() == "true":
        diseases.append("신장질환")
    if row["치매"].strip().lower() == "true":
        diseases.append("치매")
    return diseases


def build_payload(row: dict, facility_id: str) -> dict:
    has_kidney = row["신장질환"].strip().lower() == "true"
    rice, side = parse_meal_texture(row["현재식사현황"].strip())

    payload = {
        "facility_id": facility_id,
        "name": row["수급자명"].strip(),
        "sex": "male" if row["성별"].strip() == "남" else "female",
        "age": int(row["나이"]),
        "height_cm": float(row["신장"]),
        "weight_kg": float(row["체중"]),
        "diseases": build_diseases(row),
        "meal_texture_rice": rice,
        "meal_texture_side": side,
    }
    # 신장질환이 있으면 kidney_type 필수 (patient_profile_final.py의 _validate 규칙)
    # CSV에 투석 여부 구분이 없어 비투석(일반적인 요양원 케이스)으로 고정
    if has_kidney:
        payload["kidney_type"] = "신장질환"

    return payload


def register_patient(api_base: str, payload: dict) -> dict:
    url = f"{api_base.rstrip('/')}/api/patients"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise RuntimeError(f"HTTP {e.code}: {error_body}")


def main():
    if len(sys.argv) != 4:
        print("사용법: python bulk_register_patients.py <csv경로> <API_BASE_URL> <FACILITY_ID>")
        sys.exit(1)

    csv_path, api_base, facility_id = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    print(f"총 {len(rows)}명 등록을 시작합니다...")
    success, failed = 0, []

    for i, row in enumerate(rows, 1):
        name = row["수급자명"].strip()
        try:
            payload = build_payload(row, facility_id)
            result = register_patient(api_base, payload)
            print(f"  [{i}/{len(rows)}] {name} 등록 완료 — "
                  f"{result.get('disease_type_label')} | "
                  f"{result.get('target_energy')}kcal")
            success += 1
        except Exception as e:
            print(f"  [{i}/{len(rows)}] {name} 등록 실패 — {e}")
            failed.append((name, str(e)))

    print(f"\n완료: 성공 {success}명 / 실패 {len(failed)}명")
    if failed:
        print("\n실패 목록:")
        for name, err in failed:
            print(f"  - {name}: {err}")


if __name__ == "__main__":
    main()
