"""
routers/auth.py — 요양시설 로그인
=====================================
facilities 테이블의 login_id/password_hash로 인증하고, 성공 시 JWT를
발급합니다. 프론트는 이 토큰을 localStorage에 보관했다가 API 요청 시
Authorization 헤더에 실어 보냅니다(이번 1차 구현에서는 토큰 검증
미들웨어까지는 두지 않고, 로그인 게이트 용도로만 사용).
"""

import os
import jwt
import bcrypt
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.db_clients import get_supabase

router = APIRouter()

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-in-production")
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24 * 7  # 7일


class LoginRequest(BaseModel):
    login_id: str
    password: str


class LoginResponse(BaseModel):
    token: str
    facility_id: str
    facility_name: str


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    sb = get_supabase()

    result = sb.table("facilities").select("id, name, login_id, password_hash") \
                .eq("login_id", payload.login_id).execute()

    if not result.data:
        # 존재하지 않는 아이디와 비밀번호 불일치를 같은 메시지로 응답해
        # 아이디 존재 여부가 노출되지 않게 함
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")

    facility = result.data[0]
    stored_hash = facility.get("password_hash")
    if not stored_hash:
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")

    if not bcrypt.checkpw(payload.password.encode(), stored_hash.encode()):
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")

    token = jwt.encode(
        {
            "facility_id": facility["id"],
            "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS),
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    return LoginResponse(
        token=token,
        facility_id=facility["id"],
        facility_name=facility["name"],
    )


class RegisterFacilityRequest(BaseModel):
    name: str
    login_id: str
    password: str
    budget_per_meal: float = 10000


@router.post("/register-facility")
def register_facility(payload: RegisterFacilityRequest):
    """
    신규 시설 가입. 운영 초기에는 관리자가 직접 호출하거나, 추후
    프론트에 가입 폼을 추가해 연결할 수 있음.
    """
    sb = get_supabase()

    existing = sb.table("facilities").select("id").eq("login_id", payload.login_id).execute()
    if existing.data:
        raise HTTPException(400, "이미 사용 중인 아이디입니다.")

    password_hash = bcrypt.hashpw(payload.password.encode(), bcrypt.gensalt()).decode()

    result = sb.table("facilities").insert({
        "name": payload.name,
        "login_id": payload.login_id,
        "password_hash": password_hash,
        "budget_per_meal": payload.budget_per_meal,
    }).execute()

    return {"id": result.data[0]["id"], "name": result.data[0]["name"]}


# [추가 — 2026-07-01] 아이디/비밀번호 변경
# ==========================================
# 아직 JWT 검증 미들웨어가 없는 상태(모듈 docstring 참고 — 로그인 게이트
# 용도로만 토큰을 씀)라, 이 엔드포인트만 토큰으로 신원을 확인할 방법이
# 없음. 대신 login과 동일한 신뢰 모델을 그대로 적용: "현재 비밀번호를
# 다시 입력해 본인 확인"하는 방식으로 안전성을 확보함. 나중에 JWT 검증
# 미들웨어가 추가되면, current_password 확인은 유지한 채 facility_id를
# 토큰에서 꺼내는 방식으로 바꾸는 게 더 안전함(지금은 프론트가 보내주는
# facility_id를 그대로 신뢰함).
class UpdateCredentialsRequest(BaseModel):
    facility_id: str
    current_password: str
    new_login_id: str | None = None
    new_password: str | None = None


class UpdateCredentialsResponse(BaseModel):
    message: str
    login_id: str


@router.patch("/credentials", response_model=UpdateCredentialsResponse)
def update_credentials(payload: UpdateCredentialsRequest):
    sb = get_supabase()

    result = sb.table("facilities").select("id, login_id, password_hash") \
                .eq("id", payload.facility_id).execute()
    if not result.data:
        raise HTTPException(404, "시설 정보를 찾을 수 없습니다.")

    facility = result.data[0]
    stored_hash = facility.get("password_hash")
    if not stored_hash or not bcrypt.checkpw(payload.current_password.encode(), stored_hash.encode()):
        raise HTTPException(401, "현재 비밀번호가 올바르지 않습니다.")

    update_row: dict = {}

    if payload.new_login_id and payload.new_login_id != facility["login_id"]:
        existing = sb.table("facilities").select("id") \
                     .eq("login_id", payload.new_login_id).execute()
        if existing.data:
            raise HTTPException(400, "이미 사용 중인 아이디입니다.")
        update_row["login_id"] = payload.new_login_id

    if payload.new_password:
        if len(payload.new_password) < 8:
            raise HTTPException(400, "새 비밀번호는 8자 이상이어야 합니다.")
        update_row["password_hash"] = bcrypt.hashpw(
            payload.new_password.encode(), bcrypt.gensalt()
        ).decode()

    if not update_row:
        raise HTTPException(400, "변경할 아이디 또는 비밀번호를 입력해 주세요.")

    sb.table("facilities").update(update_row).eq("id", payload.facility_id).execute()

    return UpdateCredentialsResponse(
        message="계정 정보가 수정되었습니다.",
        login_id=update_row.get("login_id", facility["login_id"]),
    )