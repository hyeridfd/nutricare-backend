"""
services/report_storage.py — 보고서 파일 → Supabase Storage 업로드
========================================================================
report_agent.py(agents/)가 로컬 디스크에 저장한 엑셀/텍스트 파일을
Supabase Storage(report-files 버킷)에 업로드하고 공개 URL을 반환합니다.

agents/report_agent.py 자체는 수정하지 않음 — 그 결과(report_paths,
로컬 파일 경로 dict)를 받아서 업로드만 담당하는 어댑터 역할.

[수정 — 2026-07-01] report_agent.py가 로컬에 저장하는 파일명이
"식단표_28일.xlsx" 등 한글 그대로라, 이걸 os.path.basename()으로 그대로
Storage 키에 써서 Supabase Storage가 InvalidKey(400)를 반환하던 문제를
수정. report_paths의 키(meal_plan/serving/cooking)는 이미 영문이므로,
로컬 파일명 대신 이 키 + 원래 확장자를 조합해 Storage 키를 만듦
(예: "{run_id}/meal_plan.xlsx"). 다운로드 시 사용자에게 보여줄 한글
파일명은 Content-Disposition 헤더로 별도 지정.
"""

import os
from urllib.parse import quote
from app.services.db_clients import get_supabase

BUCKET = "report-files"


def upload_report_files(run_id: str, report_paths: dict) -> dict:
    """
    report_paths: report_agent.py가 반환한 {"meal_plan": "식단표_28일.xlsx",
                   "serving": "개인별_배식량.xlsx", "cooking": "조리_지침서.txt"}
    반환: {"meal_plan": "https://.../meal_plan.xlsx", ...} (업로드 실패한
          항목은 키 자체가 빠짐 — 부분 실패를 허용해 한 파일 문제로 전체가
          막히지 않게 함)
    """
    sb = get_supabase()
    urls: dict = {}

    for key, local_path in report_paths.items():
        if not local_path or not os.path.exists(local_path):
            print(f"  [report_storage] 경고: {key} 파일이 로컬에 없음 ({local_path})")
            continue

        original_filename = os.path.basename(local_path)
        ext = os.path.splitext(original_filename)[1] or ""
        # Storage 키는 영문 key + 확장자로 구성(한글 파일명을 키로 쓰면
        # Supabase Storage가 InvalidKey를 반환함).
        safe_filename = f"{key}{ext}"
        storage_path = f"{run_id}/{safe_filename}"

        try:
            with open(local_path, "rb") as f:
                content = f.read()

            content_type = _guess_content_type(safe_filename)
            # Content-Disposition 헤더는 ASCII만 허용하므로, 한글이 포함된
            # original_filename을 그대로 넣으면 UnicodeEncodeError가 남
            # ('ascii' codec can't encode characters...). RFC 5987 방식으로
            # UTF-8 퍼센트 인코딩한 filename*과, 구형 클라이언트 호환을 위한
            # ASCII 폴백 filename(safe_filename)을 함께 지정.
            encoded_filename = quote(original_filename)
            content_disposition = (
                f"attachment; filename=\"{safe_filename}\"; "
                f"filename*=UTF-8''{encoded_filename}"
            )
            sb.storage.from_(BUCKET).upload(
                storage_path, content,
                file_options={
                    "content-type": content_type,
                    "upsert": "true",
                    "content-disposition": content_disposition,
                },
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