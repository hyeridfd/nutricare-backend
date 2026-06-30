"""
services/report_storage.py — 보고서 파일 → Supabase Storage 업로드
========================================================================
report_agent.py(agents/)가 로컬 디스크에 저장한 엑셀/텍스트 파일을
Supabase Storage(report-files 버킷)에 업로드하고 공개 URL을 반환합니다.

agents/report_agent.py 자체는 수정하지 않음 — 그 결과(report_paths,
로컬 파일 경로 dict)를 받아서 업로드만 담당하는 어댑터 역할.
"""

import os
from app.services.db_clients import get_supabase

BUCKET = "report-files"


def upload_report_files(run_id: str, report_paths: dict) -> dict:
    """
    report_paths: report_agent.py가 반환한 {"meal_plan": "식단표_28일.xlsx",
                   "serving": "개인별_배식량.xlsx", "cooking": "조리_지침서.txt"}
    반환: {"meal_plan": "https://.../식단표_28일.xlsx", ...} (업로드 실패한
          항목은 키 자체가 빠짐 — 부분 실패를 허용해 한 파일 문제로 전체가
          막히지 않게 함)
    """
    sb = get_supabase()
    urls: dict = {}

    for key, local_path in report_paths.items():
        if not local_path or not os.path.exists(local_path):
            print(f"  [report_storage] 경고: {key} 파일이 로컬에 없음 ({local_path})")
            continue

        filename = os.path.basename(local_path)
        storage_path = f"{run_id}/{filename}"

        try:
            with open(local_path, "rb") as f:
                content = f.read()

            content_type = _guess_content_type(filename)
            sb.storage.from_(BUCKET).upload(
                storage_path, content,
                file_options={"content-type": content_type, "upsert": "true"},
            )
            public_url = sb.storage.from_(BUCKET).get_public_url(storage_path)
            urls[key] = public_url
            print(f"  [report_storage] {key} 업로드 완료 → {public_url}")

        except Exception as e:
            print(f"  [report_storage] {key} 업로드 실패: {e}")

    return urls


def _guess_content_type(filename: str) -> str:
    if filename.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if filename.endswith(".txt"):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"