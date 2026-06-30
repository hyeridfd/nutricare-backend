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
